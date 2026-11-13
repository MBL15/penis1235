// Показывает код подключения виджета с адресом этого сервера. Отдельный файл: встроенные скрипты запрещены CSP.
document.getElementById('snippet').textContent =
  '<div id="afisha"></div>\n' +
  '<script src="' + location.origin + '/widget.js"\n' +
  '        data-target="#afisha" data-when="weekend" data-limit="5"><\/script>';
