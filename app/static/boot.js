// Выполняется до отрисовки (подключён в <head> без defer). Вынесен из страницы в файл, чтобы политика
// безопасности (CSP) могла запретить любые встроенные скрипты.
document.documentElement.classList.add('js');
// Внутри Telegram параметры запуска приходят в хеше адреса: включаем режим мини-приложения сразу, без мигания вёрстки.
if (/tgWebApp(Data|Platform)=/.test(location.hash)) document.documentElement.classList.add('tg');
// Стили, помеченные data-async-css, грузятся как «печатные» и не задерживают отрисовку; включаем их, когда пришли.
document.querySelectorAll('link[data-async-css]').forEach(function (link) {
  var enable = function () { link.media = 'all'; };
  if (link.sheet) enable(); else link.addEventListener('load', enable);
});
