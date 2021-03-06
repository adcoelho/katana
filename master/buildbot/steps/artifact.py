from buildbot.process.buildstep import LoggingBuildStep, SUCCESS, SKIPPED
from twisted.internet import defer
from buildbot.steps.shell import ShellCommand
import re
from buildbot.util import epoch2datetime
from buildbot.util import safeTranslate
from buildbot.process.slavebuilder import IDLE, BUILDING
from buildbot.steps.resumebuild import ResumeBuild, ShellCommandResumeBuild

# Change artifact location in August
# datetime.datetime(2017, 7, 31, 23, 59, 59, tzinfo=UTC)
ARTIFACT_LOCATION_CHANGE_DATE = epoch2datetime(1501545599)

def FormatDatetime(value):
    return value.strftime("%d_%m_%Y_%H_%M_%S_%z")

def mkdt(epoch):
    if epoch:
        return epoch2datetime(epoch)

def getBuildSourceStamps(build, build_sourcestamps):
    # every build will generate at least one sourcestamp
    sourcestamps = build.build_status.getSourceStamps()

    # when running rebuild or passing revision as parameter
    for ss in sourcestamps:
        build_sourcestamps.append(
            {'b_codebase': ss.codebase, 'b_revision': ss.revision, 'b_branch': ss.branch,
             'b_sourcestampsetid': ss.sourcestampsetid})

def forceRebuild(build):
    force_rebuild = build.getProperty("force_rebuild", False)
    if type(force_rebuild) != bool:
        force_rebuild = (force_rebuild.lower() == "true")

    force_chain_rebuild = build.getProperty("force_chain_rebuild", False)
    if type(force_chain_rebuild) != bool:
        force_chain_rebuild = (force_chain_rebuild.lower() == "true")

    return force_chain_rebuild or force_rebuild

class FindPreviousSuccessfulBuild(ResumeBuild):
    name = "Find Previous Successful Build"
    description="Searching for a previous successful build at the appropriate revision(s)..."
    descriptionDone="Searching complete."

    def __init__(self, **kwargs):
        self.build_sourcestamps = []
        self.master = None
        ResumeBuild.__init__(self, **kwargs)

    @defer.inlineCallbacks
    def start(self):
        if self.master is None:
            self.master = self.build.builder.botmaster.parent

        yield getBuildSourceStamps(self.build, self.build_sourcestamps)

        if forceRebuild(self.build):
            self.step_status.setText(["Skipping previous build check (forcing a rebuild)."])
            # update merged buildrequest to reuse artifact generated by current buildrequest
            if len(self.build.requests) > 1:
                yield self.master.db.buildrequests.updateMergedBuildRequest(self.build.requests)
            self.finished(SKIPPED)
            return

        prevBuildRequest = yield self.master.db.buildrequests\
            .getBuildRequestBySourcestamps(buildername=self.build.builder.config.name,
                                           sourcestamps=self.build_sourcestamps)

        if prevBuildRequest:
            build_list = yield self.master.db.builds.getBuildsForRequest(prevBuildRequest['brid'])
            # there can be many builds per buildrequest for example (retry) when slave lost connection
            # in this case we will display all the builds related to this build request
            for build in build_list:
                build_num = build['number']
                friendly_name = self.build.builder.builder_status.getFriendlyName()
                url = yield self.master.status.getURLForBuildRequest(prevBuildRequest['brid'],
                                                                     self.build.builder.config.name, build_num,
                                                                     friendly_name, self.build_sourcestamps)
                self.addURL(url['text'], url['path'])
            # we are not building but reusing a previous build
            reuse = yield self.master.db.buildrequests.reusePreviousBuild(self.build.requests, prevBuildRequest['brid'])
            self.step_status.setText(["Found previous successful build."])
            self.step_status.stepFinished(SUCCESS)
            self.build.result = SUCCESS
            self.build.setProperty("reusedOldBuild", True)
            self.build.allStepsDone()
            self.resumeBuild = False
        else:
            if len(self.build.requests) > 1:
                yield self.master.db.buildrequests.updateMergedBuildRequest(self.build.requests)
            self.step_status.setText(["Running build (previous sucessful build not found)."])

        self.finished(SUCCESS)
        return


class CheckArtifactExists(ShellCommandResumeBuild):
    name = "Check if Artifact Exists"
    description="Checking if artifacts exist from a previous build at the appropriate revision(s)..."
    descriptionDone="Searching complete."

    def __init__(self, artifact=None, artifactDirectory=None, artifactServer=None, artifactServerDir=None,
                 artifactServerURL=None, artifactServerPort=None, stopBuild=True, resumeBuild=None, **kwargs):
        self.master = None
        self.build_sourcestamps = []
        if not isinstance(artifact, list):
            artifact = [artifact]
        self.artifact = artifact
        self.artifactDirectory = artifactDirectory
        self.artifactServer = artifactServer
        self.artifactServerDir = artifactServerDir
        self.artifactServerURL = artifactServerURL
        self.artifactServerPort = artifactServerPort
        self.artifactBuildrequest = None
        self.artifactPath = None
        self.artifactURL = None
        self.stopBuild = stopBuild
        resume_build_val = stopBuild if resumeBuild is None else resumeBuild
        ShellCommandResumeBuild.__init__(self, resumeBuild=resume_build_val, **kwargs)

    @defer.inlineCallbacks
    def createSummary(self, log):
        artifactlist = list(self.artifact)
        stdio = self.getLog('stdio').readlines()
        notfoundregex = re.compile(r'Not found!!')
        for l in stdio:
            m = notfoundregex.search(l)
            if m:
                break
            if len(artifactlist) == 0:
                break
            for a in artifactlist:
                artifact = a
                if artifact.endswith("/"):
                    artifact = artifact[:-1]
                foundregex = re.compile(r'(%s)' % artifact)
                m = foundregex.search(l)
                if (m):
                    artifactURL = self.artifactServerURL + "/" + self.artifactPath + "/" + a
                    self.addURL(a, artifactURL)
                    artifactlist.remove(a)

        if len(artifactlist) == 0:
            artifactsfound = self.build.getProperty("artifactsfound", True)

            if not artifactsfound:
                return

            self.build.setProperty("artifactsfound", True, "CheckArtifactExists %s" % self.artifact)
            self.build.setProperty("reusedOldBuild", True)
            self.resumeBuild = False

            if self.stopBuild:
                # update buildrequest (artifactbrid) with self.artifactBuildrequest
                reuse = yield self.master.db.buildrequests.reusePreviousBuild(self.build.requests,
                                                                              self.artifactBuildrequest['brid'])
                self.step_status.stepFinished(SUCCESS)
                self.build.result = SUCCESS
                self.build.allStepsDone()
        else:
            self.build.setProperty("artifactsfound", False, "CheckArtifactExists %s" % self.artifact)
            self.descriptionDone = ["Artifact not found on server %s." % self.artifactServerURL]
            # update merged buildrequest to reuse artifact generated by current buildrequest
            if len(self.build.requests) > 1:
                yield self.master.db.buildrequests.updateMergedBuildRequest(self.build.requests)

    @defer.inlineCallbacks
    def start(self):
        if self.master is None:
            self.master = self.build.builder.botmaster.parent

        yield getBuildSourceStamps(self.build, self.build_sourcestamps)

        if forceRebuild(self.build):
            self.step_status.setText(["Skipping artifact check (forcing a rebuild)."])
            # update merged buildrequest to reuse artifact generated by current buildrequest
            if len(self.build.requests) > 1:
                yield self.master.db.buildrequests.updateMergedBuildRequest(self.build.requests)
            self.finished(SKIPPED)
            return

        self.artifactBuildrequest = yield self.master.db.\
            buildrequests.getBuildRequestBySourcestamps(buildername=self.build.builder.config.name,
                                                        sourcestamps=self.build_sourcestamps)

        if self.artifactBuildrequest:
            self.step_status.setText(["Artifact has been already generated."])

            if self.artifactBuildrequest["submitted_at"] > ARTIFACT_LOCATION_CHANGE_DATE:
                self.artifactPath = "%s/%s_%s" % (self.build.builder.config.builddir,
                                                  self.artifactBuildrequest['brid'],
                                                  FormatDatetime(self.artifactBuildrequest['submitted_at']))
            else:
                self.artifactPath = "%s_%s_%s" % (self.build.builder.config.builddir,
                                                  self.artifactBuildrequest['brid'],
                                                  FormatDatetime(self.artifactBuildrequest['submitted_at']))

            if self.artifactDirectory:
                self.artifactPath += "/%s" %  self.artifactDirectory

            search_artifact = ""
            for a in self.artifact:
                if a.endswith("/"):
                    a = a[:-1]
                    if "/" in a:
                        index = a.rfind("/")
                        a = a[:index] + "/*"
                search_artifact += "; ls %s" % a

            command = ["ssh", self.artifactServer]
            if self.artifactServerPort:
                command += ["-p %s" % self.artifactServerPort]
            command += ["cd %s;" % self.artifactServerDir,
                       "if [ -d %s ]; then echo 'Exists'; else echo 'Not found!!'; fi;" % self.artifactPath,
                       "cd %s" % self.artifactPath, search_artifact, "; ls"]
            # ssh to the server to check if it artifact is there
            self.setCommand(command)
            ShellCommandResumeBuild.start(self)
            return

        if len(self.build.requests) > 1:
            yield self.master.db.buildrequests.updateMergedBuildRequest(self.build.requests)
        self.step_status.setText(["Artifact not found."])
        self.finished(SUCCESS)
        return


class CreateArtifactDirectory(ShellCommand):

    name = "Create Remote Artifact Directory"
    description="Creating the artifact directory on the remote artifacts server..."
    descriptionDone="Remote artifact directory created."

    def __init__(self,  artifactDirectory=None, artifactServer=None, artifactServerDir=None, artifactServerPort=None,
                **kwargs):
        self.artifactDirectory = artifactDirectory
        self.artifactServer = artifactServer
        self.artifactServerDir = artifactServerDir
        self.artifactServerPort = artifactServerPort
        ShellCommand.__init__(self, **kwargs)

    def start(self):
        br = self.build.requests[0]
        if mkdt(br.submittedAt) > ARTIFACT_LOCATION_CHANGE_DATE:
            artifactPath  = "%s/%s_%s" % (self.build.builder.config.builddir,
                                          br.id, FormatDatetime(mkdt(br.submittedAt)))
        else:
            artifactPath  = "%s_%s_%s" % (self.build.builder.config.builddir,
                                          br.id, FormatDatetime(mkdt(br.submittedAt)))

        if (self.artifactDirectory):
            artifactPath += "/%s" % self.artifactDirectory


        command = ["ssh", self.artifactServer]
        if self.artifactServerPort:
            command += ["-p %s" % self.artifactServerPort]
        command += ["cd %s;" % self.artifactServerDir, "mkdir -p ",
                    artifactPath]

        self.setCommand(command)
        ShellCommand.start(self)


def checkWindowsSlaveEnvironment(step, key):
    return key in step.build.slavebuilder.slave.slave_environ.keys() \
           and step.build.slavebuilder.slave.slave_environ[key] == 'Windows_NT'


def _isWindowsSlave(step):
        slave_os = step.build.slavebuilder.slave.os and step.build.slavebuilder.slave.os == 'Windows'
        slave_env = checkWindowsSlaveEnvironment(step, 'os') or checkWindowsSlaveEnvironment(step, 'OS')
        return slave_os or slave_env


def retryCommandLinuxOS(command):
    return 'for i in 1 2 3 4 5; do ' + command + '; if [ $? -eq 0 ]; then exit 0; else sleep 5; fi; done; exit -1'


def retryCommandWindowsOS(command):
    return 'for /L %%i in (1,1,5) do (sleep 5 & ' + command + ' && exit 0)'

def retryCommandWindowsOSPwShell(command):
    return 'powershell.exe -C for ($i=1; $i -le  5; $i++) { '+ command \
           +'; if ($?) { exit 0 } else { sleep 5} } exit -1'

def rsyncWithRetry(step, origin, destination, port=None):

    rsync_command = "rsync -var --progress --partial '%s' '%s'" % (origin, destination)
    if port:
        rsync_command += " --rsh='ssh -p %s'" % port
    if _isWindowsSlave(step):
        if step.usePowerShell:
            return retryCommandWindowsOSPwShell(rsync_command)
        return retryCommandWindowsOS(rsync_command)

    return retryCommandLinuxOS(rsync_command)

def getRemoteLocation(artifactServer, artifactServerDir, artifactPath, artifact):
    return artifactServer + ":" + artifactServerDir + "/" + artifactPath + "/" + artifact.replace(" ", r"\ ")

class UploadArtifact(ShellCommand):

    name = "Upload Artifact(s)"
    description="Uploading artifact(s) to remote artifact server..."
    descriptionDone="Artifact(s) uploaded."

    def __init__(self, artifact=None, artifactDirectory=None, artifactServer=None, artifactServerDir=None,
                 artifactServerURL=None, artifactServerPort=None, usePowerShell=True, **kwargs):
        self.artifact=artifact
        self.artifactURL = None
        self.artifactDirectory = artifactDirectory
        self.artifactServer = artifactServer
        self.artifactServerDir = artifactServerDir
        self.artifactServerURL = artifactServerURL
        self.artifactServerPort = artifactServerPort
        self.usePowerShell = usePowerShell
        ShellCommand.__init__(self, **kwargs)

    @defer.inlineCallbacks
    def start(self):
        br = self.build.requests[0]

        # this means that we are merging build requests with this one
        if len(self.build.requests) > 1:
            master = self.build.builder.botmaster.parent
            reuse = yield master.db.buildrequests.updateMergedBuildRequest(self.build.requests)

        if mkdt(br.submittedAt) > ARTIFACT_LOCATION_CHANGE_DATE:
            artifactPath  = "%s/%s_%s" % (self.build.builder.config.builddir, br.id, FormatDatetime(mkdt(br.submittedAt)))
        else:
            artifactPath = "%s_%s_%s" % (self.build.builder.config.builddir, br.id, FormatDatetime(mkdt(br.submittedAt)))

        artifactServerPath = self.build.getProperty("artifactServerPath", None)
        if artifactServerPath is None:
            self.build.setProperty("artifactServerPath", self.artifactServerURL + "/" + artifactPath, "UploadArtifact")

        if (self.artifactDirectory):
            artifactPath += "/%s" % self.artifactDirectory

        remotelocation = getRemoteLocation(self.artifactServer, self.artifactServerDir, artifactPath, self.artifact)

        command = rsyncWithRetry(self, self.artifact, remotelocation, self.artifactServerPort)

        self.artifactURL = self.artifactServerURL + "/" + artifactPath + "/" + self.artifact
        self.setCommand(command)
        ShellCommand.start(self)

    def finished(self, results):
        if results == SUCCESS:
            self.addURL(self.artifact, self.artifactURL)
        ShellCommand.finished(self, results)


class DownloadArtifact(ShellCommand):
    name = "Download Artifact(s)"
    description="Downloading artifact(s) from the remote artifacts server..."
    descriptionDone="Artifact(s) downloaded."

    def __init__(self, artifactBuilderName=None, artifact=None, artifactDirectory=None, artifactDestination=None,
                 artifactServer=None, artifactServerDir=None, artifactServerPort=None, usePowerShell=True, **kwargs):
        self.artifactBuilderName = artifactBuilderName
        self.artifact = artifact
        self.artifactDirectory = artifactDirectory
        self.artifactServer = artifactServer
        self.artifactServerDir = artifactServerDir
        self.artifactServerPort = artifactServerPort
        self.artifactDestination = artifactDestination or artifact
        self.master = None
        self.usePowerShell = usePowerShell
        name = "Download Artifact for '%s'" % artifactBuilderName
        description = "Downloading artifact '%s'..." % artifactBuilderName
        descriptionDone="Downloaded '%s'." % artifactBuilderName
        ShellCommand.__init__(self, name=name, description=description, descriptionDone=descriptionDone,  **kwargs)


    @defer.inlineCallbacks
    def start(self):
        if self.master is None:
            self.master = self.build.builder.botmaster.parent

        #find artifact dependency
        triggeredbybrid = self.build.requests[0].id
        br = yield self.master.db.buildrequests.getBuildRequestTriggered(triggeredbybrid, self.artifactBuilderName)

        if br["submitted_at"] > ARTIFACT_LOCATION_CHANGE_DATE:
            artifactPath  = "%s/%s_%s" % (safeTranslate(self.artifactBuilderName),
                                          br['brid'], FormatDatetime(br["submitted_at"]))
        else:
            artifactPath  = "%s_%s_%s" % (safeTranslate(self.artifactBuilderName),
                                          br['brid'], FormatDatetime(br["submitted_at"]))

        if (self.artifactDirectory):
            artifactPath += "/%s" % self.artifactDirectory

        remotelocation = getRemoteLocation(self.artifactServer, self.artifactServerDir, artifactPath, self.artifact)

        command = rsyncWithRetry(self, remotelocation, self.artifactDestination, self.artifactServerPort)

        self.setCommand(command)
        ShellCommand.start(self)


class AcquireBuildLocks(LoggingBuildStep):
    name = "Acquire Build Slave"
    description="Acquiring build slave..."
    descriptionDone="Build slave acquired."

    def __init__(self, hideStepIf = True, locks=None, **kwargs):
        LoggingBuildStep.__init__(self, hideStepIf = hideStepIf, locks=locks, **kwargs)

    def start(self):
        self.step_status.setText(["Acquiring build slave to complete build."])
        self.build.locks = self.locks

        if self.build.slavebuilder.state == IDLE:
            self.build.slavebuilder.state = BUILDING

        if self.build.builder.builder_status.currentBigState == "idle":
            self.build.builder.builder_status.setBigState("building")

        self.build.releaseLockInstance = self
        self.finished(SUCCESS)
        return

    def releaseLocks(self):
        return


class ReleaseBuildLocks(LoggingBuildStep):
    name = "Release Builder Locks"
    description="Releasing builder locks..."
    descriptionDone="Build locks released."

    def __init__(self, hideStepIf=True, **kwargs):
        self.releaseLockInstance = None
        LoggingBuildStep.__init__(self, hideStepIf=hideStepIf, **kwargs)

    def start(self):
        self.step_status.setText(["Releasing build locks."])
        self.locks = self.build.locks
        self.releaseLockInstance = self.build.releaseLockInstance
        # release slave lock
        self.build.slavebuilder.state = IDLE
        self.build.builder.builder_status.setBigState("idle")
        self.finished(SUCCESS)
        # notify that the slave may now be available to start a build.
        self.build.builder.botmaster.maybeStartBuildsForSlave(self.buildslave.slavename)
        return
