define(["jquery","helpers","iFrameResize"],function(e){function t(e){e&&setTimeout(function(){window.scrollTo(0,document.body.scrollHeight)},300)}e(document).ready(function(){e("html").css("background-color","#ebeef1");var n=e("#logIFrame"),r=e("#scrollOpt"),i=!1;n.iFrameResize({autoResize:!0,sizeWidth:!0,resizedCallback:function(){t(r.prop("checked"))}}),e(document).keyup(function(e){if(e.which===83&&i===!1){var n=!r.prop("checked");i=!0,r.prop("checked",n),t(n),setTimeout(function(){i=!1},300)}})})});