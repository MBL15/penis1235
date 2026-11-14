// Выполняется до отрисовки (подключён в <head> без defer). Вынесен из страницы в файл, чтобы политика
// безопасности (CSP) могла запретить любые встроенные скрипты.
(function () {
  var root = document.documentElement;
  root.classList.add('js');
  // Внутри Telegram параметры запуска приходят в хеше адреса: включаем режим мини-приложения сразу, без мигания вёрстки.
  if (/tgWebApp(Data|Platform)=/.test(location.hash)) root.classList.add('tg');
  // Стили, помеченные data-async-css, грузятся как «печатные» и не задерживают отрисовку; включаем их, когда пришли.
  document.querySelectorAll('link[data-async-css]').forEach(function (link) {
    var enable = function () { link.media = 'all'; };
    if (link.sheet) enable(); else link.addEventListener('load', enable);
  });

  // ---------- «Удобство просмотра»: тема, размер текста, контраст, различение цветов, интервалы, анимация ----------
  // Применяем до отрисовки, чтобы страница не мигнула светлой темой или мелким шрифтом. Панель настроек — в ui.js.
  var KEY = 'afisha.a11y.v1';
  var DEFAULTS = { theme: 'system', text: 0, contrast: 'auto', cvd: false, spacing: false, motion: 'auto' };
  var pick = function (value, allowed, fallback) { return allowed.indexOf(value) >= 0 ? value : fallback; };

  function read() {
    var saved = {};
    try { saved = JSON.parse(localStorage.getItem(KEY) || '{}') || {}; } catch (e) { saved = {}; }
    return {
      theme: pick(saved.theme, ['system', 'light', 'dark'], DEFAULTS.theme),
      text: pick(Number(saved.text), [0, 1, 2, 3], DEFAULTS.text),
      contrast: pick(saved.contrast, ['auto', 'normal', 'more'], DEFAULTS.contrast),
      cvd: saved.cvd === true, // палитра для нарушений цветовосприятия и дополнительные метки формой
      spacing: saved.spacing === true,
      motion: pick(saved.motion, ['auto', 'reduce'], DEFAULTS.motion),
    };
  }

  // Тема Telegram: цвет фона приходит в параметрах запуска. Нужна, когда человек оставил тему «как в системе».
  var telegramScheme = '';
  try {
    var match = /tgWebAppThemeParams=([^&]+)/.exec(location.hash);
    var hex = match ? String(JSON.parse(decodeURIComponent(match[1])).bg_color || '').replace('#', '') : '';
    if (hex.length === 6) {
      var lum = (0.2126 * parseInt(hex.slice(0, 2), 16) + 0.7152 * parseInt(hex.slice(2, 4), 16) + 0.0722 * parseInt(hex.slice(4, 6), 16)) / 255;
      telegramScheme = lum < 0.5 ? 'dark' : 'light';
    }
  } catch (e) { telegramScheme = ''; }

  function toggle(name, value) { if (value) root.setAttribute(name, value); else root.removeAttribute(name); }

  function apply(prefs) {
    var theme = prefs.theme === 'system' ? telegramScheme : prefs.theme;
    toggle('data-theme', theme); // без атрибута действует системная тема (prefers-color-scheme)
    toggle('data-text', prefs.text > 0 ? String(prefs.text) : '');
    toggle('data-contrast', prefs.contrast === 'auto' ? '' : prefs.contrast);
    toggle('data-cvd', prefs.cvd ? 'on' : '');
    toggle('data-spacing', prefs.spacing ? 'wide' : '');
    toggle('data-motion', prefs.motion === 'reduce' ? 'reduce' : '');
  }

  apply(read());

  window.afishaPrefs = {
    defaults: DEFAULTS,
    read: read,
    /** Сохранить и применить; страница узнаёт об этом по событию afisha:prefs (например, чтобы перекрасить шапку Telegram). */
    save: function (prefs) {
      try { localStorage.setItem(KEY, JSON.stringify(prefs)); } catch (e) { /* хранилище недоступно: действует до перезагрузки */ }
      apply(prefs);
      window.dispatchEvent(new CustomEvent('afisha:prefs', { detail: prefs }));
    },
    /** Поменять одну-две настройки, не трогая остальные: update({ text: 2 }). */
    update: function (patch) {
      var next = read();
      for (var key in patch) if (Object.prototype.hasOwnProperty.call(patch, key)) next[key] = patch[key];
      this.save(next);
      return next;
    },
    /** Мини-приложение сообщает тему Telegram (при запуске и когда человек её меняет). */
    setTelegramScheme: function (scheme) {
      telegramScheme = scheme === 'dark' || scheme === 'light' ? scheme : '';
      apply(read());
    },
    /** Действующая тема: выбранная, тема Telegram или системная. */
    theme: function () {
      var t = root.getAttribute('data-theme');
      if (t) return t;
      return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    },
    /** Без анимаций: системная настройка или выбор в панели. */
    reduceMotion: function () {
      return root.getAttribute('data-motion') === 'reduce' || !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
    },
  };
})();
