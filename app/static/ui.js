// Общие элементы интерфейса на всех страницах (кроме админки): панель разделов, уведомления, «Поделиться», «Следить».
// Правило: любое действие не дальше трёх нажатий. Раздел открывается одним нажатием с любой страницы, подписка с карточки события
// — одним (для вошедшего) или двумя (вход и подписка). Скрипт внешний: политика безопасности запрещает встроенные.
(() => {
  const ICONS = {
    home: '<path d="M3 11.5 12 4l9 7.5"/><path d="M5.5 10v9.5h13V10"/><path d="M10 19.5v-5h4v5"/>',
    chat: '<path d="M4 5h16v11H9l-5 4z"/><path d="M8.5 9.5h7M8.5 12.5h4"/>',
    plan: '<rect x="4" y="5" width="16" height="15" rx="2.5"/><path d="M4 10h16M9 3v4M15 3v4"/><path d="m9.5 15 2 2 3.5-4"/>',
    user: '<circle cx="12" cy="8.5" r="3.6"/><path d="M5 20c.8-3.6 3.6-5.4 7-5.4s6.2 1.8 7 5.4"/>',
    help: '<path d="M4 6.5h16v10H12l-4.5 3.5v-3.5H4z"/><path d="M12 9.5v3.5M12 14.8v.1"/>',
  };
  const path = location.pathname.replace(/\/+$/, '') || '/';
  const root = document.documentElement;
  // Без анимаций: системная настройка или выбор в панели «Удобство просмотра» (проверяем каждый раз: выбор меняется на ходу)
  const reduce = () => (window.afishaPrefs ? window.afishaPrefs.reduceMotion() : !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches));

  // ---------- уведомления ----------
  let box = null;
  /** ui.toast('Готово'), ui.toast('Ошибка', {bad: true}), ui.toast('Войдите', {href: '/account', label: 'Войти'}) */
  function toast(text, options = {}) {
    if (!box) {
      box = document.createElement('div');
      box.className = 'ui-toasts';
      box.setAttribute('role', 'status');
      box.setAttribute('aria-live', 'polite');
      document.body.appendChild(box);
    }
    const item = document.createElement('div');
    item.className = `ui-toast${options.bad ? ' ui-bad' : ''}`;
    const label = document.createElement('span');
    label.textContent = text;
    item.appendChild(label);
    if (options.href) {
      const link = document.createElement('a');
      link.href = options.href;
      link.textContent = options.label || 'Открыть';
      item.appendChild(link);
    }
    while (box.children.length >= 3) box.firstChild.remove();
    box.appendChild(item);
    const hide = () => { item.classList.add('ui-out'); setTimeout(() => item.remove(), reduce() ? 0 : 280); };
    setTimeout(hide, options.ms || (options.href ? 7000 : 3800));
    item.addEventListener('click', (event) => { if (!event.target.closest('a')) hide(); });
    return item;
  }

  /** Кнопка на время запроса: крутится индикатор, повторное нажатие невозможно. */
  async function busy(button, action) {
    if (!button || button.getAttribute('aria-busy') === 'true') return undefined;
    button.setAttribute('aria-busy', 'true');
    try { return await action(); } finally { button.removeAttribute('aria-busy'); }
  }

  // ---------- панель разделов ----------
  function mountNav() {
    if (document.querySelector('.ui-nav') || document.body.hasAttribute('data-ui-nonav')) return;
    const items = [
      { href: '/', label: 'Афиша', icon: 'home', on: path === '/' },
      { href: '/?chat=1', label: 'Помощник', icon: 'chat', on: false },
      { href: '/plan', label: 'Выходной', icon: 'plan', on: path === '/plan' },
      { href: '/account', label: 'Кабинет', icon: 'user', on: path === '/account' },
      { href: '/feedback', label: 'Связь', icon: 'help', on: path === '/feedback' },
    ];
    const nav = document.createElement('nav');
    nav.className = 'ui-nav';
    nav.setAttribute('aria-label', 'Разделы');
    nav.innerHTML = items.map((i) => `<a href="${i.href}"${i.on ? ' aria-current="page"' : ''}${i.icon === 'chat' ? ' data-ui-chat' : ''}>
      <svg viewBox="0 0 24 24" aria-hidden="true">${ICONS[i.icon]}</svg><span>${i.label}</span></a>`).join('');
    document.body.appendChild(nav);
    document.body.classList.add('ui-has-nav');
    if (path === '/') document.body.classList.add('ui-home');
    // На главной «Помощник» открывает чат сразу, без перезагрузки страницы
    nav.addEventListener('click', (event) => {
      const link = event.target.closest('[data-ui-chat]');
      const opener = path === '/' && document.querySelector('[data-open-chats]');
      if (link && opener) { event.preventDefault(); opener.click(); }
    });
    // Зелёная точка у «Кабинета», если человек вошёл: видно, что вход сохранён
    fetch('/api/v1/me', { cache: 'no-store' }).then((r) => r.json()).then((me) => {
      if (me && me.authenticated) nav.querySelector('a[href="/account"]').insertAdjacentHTML('beforeend', '<i class="ui-dot" title="Вы вошли"></i>');
    }).catch(() => {});
  }

  // На главной ссылка /?chat=1 открывает чат-помощника
  function openChatFromLink() {
    if (path !== '/' || new URLSearchParams(location.search).get('chat') !== '1') return;
    const open = () => { const opener = document.querySelector('[data-open-chats]'); if (opener) { opener.click(); history.replaceState(null, '', '/'); } };
    if (document.readyState === 'complete') open(); else window.addEventListener('load', open);
  }

  // ---------- «Поделиться» и «Следить» на карточке события ----------
  async function share(button) {
    const data = { title: document.title, url: button.dataset.url || location.href };
    if (navigator.share) {
      try { await navigator.share(data); return; } catch (error) { if (error && error.name === 'AbortError') return; }
    }
    try { await navigator.clipboard.writeText(data.url); toast('Ссылка скопирована'); } catch (_) { toast(data.url, { ms: 9000 }); }
  }

  async function follow(button) {
    const headers = { 'Content-Type': 'application/json' };
    const app = window.Telegram && window.Telegram.WebApp;
    if (app && app.initData) headers['X-Telegram-Init-Data'] = app.initData;
    const label = button.dataset.label || 'такие события';
    const response = await fetch('/api/v1/subscriptions', {
      method: 'POST', headers, body: JSON.stringify({ city: button.dataset.city || '', category: button.dataset.category || '' }),
    });
    if (response.status === 401 || response.status === 503) {
      toast('Чтобы следить за новыми событиями, войдите через Telegram', { href: '/account', label: 'Войти' });
      return;
    }
    const data = await response.json().catch(() => ({}));
    if (response.status === 201 || response.status === 409) {
      button.classList.add('ui-done');
      button.setAttribute('aria-pressed', 'true');
      const text = button.querySelector('.ui-heart');
      if (text) text.textContent = `Вы следите: ${label}`; // состояние видно на самой кнопке, а не только в уведомлении
      toast(response.status === 201 ? `Готово: пришлём новое про «${label}»` : `Вы уже следите: ${label}`, { href: '/account#subs', label: 'Мои подписки' });
      return;
    }
    toast((data.error && data.error.message) || 'Не получилось подписаться', { bad: true });
  }

  document.addEventListener('click', (event) => {
    const shareButton = event.target.closest('[data-share]');
    if (shareButton) { busy(shareButton, () => share(shareButton)); return; }
    const followButton = event.target.closest('[data-follow]');
    if (followButton) busy(followButton, () => follow(followButton)).catch(() => toast('Нет соединения: попробуйте ещё раз', { bad: true }));
  });

  // Появление блоков по очереди: любой элемент с data-rise получает порядок и анимацию
  function stagger() {
    if (reduce()) return;
    document.querySelectorAll('[data-rise]').forEach((node, index) => { node.style.setProperty('--i', String(index)); node.classList.add('ui-rise'); });
  }

  // Картинка, которая не загрузилась (data-hide-on-error), убирается: под ней остаётся запасная иконка.
  document.addEventListener('error', (event) => {
    const img = event.target;
    if (img && img.tagName === 'IMG' && img.hasAttribute('data-hide-on-error')) img.remove();
  }, true);

  // ---------- «Удобство просмотра»: тема, размер текста, контраст, различение цветов, интервалы, анимация ----------
  // Настройки хранит и применяет boot.js (до отрисовки страницы). Здесь: кнопка темы в шапке, боковые кнопки размера
  // шрифта и полная панель. Каждое изменение видно сразу, все три места показывают одно и то же состояние.
  const A11Y_ICONS = {
    person: '<circle cx="12" cy="4.6" r="1.9"/><path d="M4.5 8.6c2.4.9 4.9 1.3 7.5 1.3s5.1-.4 7.5-1.3"/><path d="M12 9.9v4.4l-3.2 6.2M12 14.3l3.2 6.2"/>',
    sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2.5 12h2M19.5 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
    moon: '<path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/>',
    auto: '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/>',
    close: '<path d="M6 6l12 12M18 6 6 18"/>',
  };
  const svg = (name) => `<svg viewBox="0 0 24 24" aria-hidden="true">${A11Y_ICONS[name]}</svg>`;
  const prefsApi = window.afishaPrefs;
  const systemReduce = () => !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  const systemContrast = () => !!(window.matchMedia && window.matchMedia('(prefers-contrast: more)').matches);
  const TEXT_STEPS = [
    { size: '1rem', label: '100%', name: 'Обычный размер текста' },
    { size: '1.15rem', label: '115%', name: 'Текст крупнее, 115 процентов' },
    { size: '1.3rem', label: '130%', name: 'Крупный текст, 130 процентов' },
    { size: '1.5rem', label: '150%', name: 'Очень крупный текст, 150 процентов' },
  ];
  let a11yDialog = null;
  let a11yOpener = null;

  function buildA11y() {
    const dialog = document.createElement('dialog');
    dialog.className = 'ui-a11y';
    dialog.setAttribute('aria-labelledby', 'ui-a11y-title');
    const theme = (value, label, icon) => `<label><input type="radio" name="theme" value="${value}">${svg(icon)}<span>${label}</span></label>`;
    dialog.innerHTML = `<form method="dialog">
      <header><h2 id="ui-a11y-title">Удобство просмотра</h2>
        <button type="submit" class="ui-a11y-close" aria-label="Закрыть">${svg('close')}</button></header>
      <fieldset><legend>Тема</legend><div class="ui-seg">
        ${theme('light', 'Светлая', 'sun')}${theme('dark', 'Тёмная', 'moon')}${theme('system', 'Как в системе', 'auto')}
      </div></fieldset>
      <fieldset><legend>Размер текста</legend><div class="ui-seg">
        ${TEXT_STEPS.map((s, i) => `<label><input type="radio" name="text" value="${i}" aria-label="${s.name}"><span class="ui-a" style="font-size:${s.size}" aria-hidden="true">А</span><small aria-hidden="true">${s.label}</small></label>`).join('')}
      </div><p class="ui-a11y-hint">Быстро менять размер можно и кнопками «А+» и «А−» сбоку экрана. Для ещё более крупного масштаба есть увеличение браузера (Ctrl и «+»).</p></fieldset>
      <label class="ui-switch"><span>Высокий контраст<small>Сплошные фоны, заметные рамки, чёткий вторичный текст</small></span><input type="checkbox" name="contrast"><span class="ui-track" aria-hidden="true"></span></label>
      <label class="ui-switch"><span>Различение цветов<small>Для дальтонизма: синий и оранжевый вместо зелёного и красного, выбранное отмечено галочкой, ссылки подчёркнуты</small></span><input type="checkbox" name="cvd"><span class="ui-track" aria-hidden="true"></span></label>
      <label class="ui-switch"><span>Увеличенные интервалы<small>Больше места между буквами, словами и строками</small></span><input type="checkbox" name="spacing"><span class="ui-track" aria-hidden="true"></span></label>
      <label class="ui-switch"><span>Без анимации<small data-motion-hint>Ничего не двигается и не мерцает</small></span><input type="checkbox" name="motion"><span class="ui-track" aria-hidden="true"></span></label>
      <footer><button type="button" class="ui-a11y-reset" data-a11y-reset>Вернуть как было</button><button type="submit" class="ui-a11y-done">Готово</button></footer>
    </form>`;
    document.body.appendChild(dialog);
    const form = dialog.querySelector('form');

    // Любое изменение сразу сохраняется и применяется: результат виден за панелью.
    form.addEventListener('change', () => {
      const prev = prefsApi.read();
      prefsApi.save({
        theme: form.elements.theme.value || 'system',
        text: Number(form.elements.text.value) || 0,
        contrast: form.elements.contrast.checked ? 'more' : (systemContrast() || prev.contrast !== 'auto' ? 'normal' : 'auto'),
        cvd: form.elements.cvd.checked,
        spacing: form.elements.spacing.checked,
        motion: form.elements.motion.checked ? 'reduce' : 'auto',
      });
    });
    dialog.querySelector('[data-a11y-reset]').addEventListener('click', () => {
      prefsApi.save({ ...prefsApi.defaults });
      toast('Настройки сброшены');
    });
    // Нажатие на затемнение вокруг панели закрывает её
    dialog.addEventListener('click', (event) => { if (event.target === dialog) dialog.close(); });
    dialog.addEventListener('close', () => { if (a11yOpener && a11yOpener.isConnected) a11yOpener.focus(); });
    return dialog;
  }

  function fillA11y() {
    if (!a11yDialog) return;
    const form = a11yDialog.querySelector('form');
    const prefs = prefsApi.read();
    form.elements.theme.value = prefs.theme;
    form.elements.text.value = String(prefs.text);
    form.elements.contrast.checked = prefs.contrast === 'more' || (prefs.contrast === 'auto' && systemContrast());
    form.elements.cvd.checked = prefs.cvd;
    form.elements.spacing.checked = prefs.spacing;
    // Если анимацию выключили в настройках устройства, переключатель включён и не снимается: решение уже принято там
    const locked = systemReduce();
    form.elements.motion.checked = locked || prefs.motion === 'reduce';
    form.elements.motion.disabled = locked;
    a11yDialog.querySelector('[data-motion-hint]').textContent = locked ? 'Включено в настройках устройства' : 'Ничего не двигается и не мерцает';
  }

  function openA11y(opener) {
    if (!prefsApi) return;
    if (!a11yDialog) a11yDialog = buildA11y();
    a11yOpener = opener || document.activeElement;
    fillA11y();
    a11yDialog.showModal();
  }

  // ---- кнопка «светлая / тёмная тема» в шапке: одно нажатие переключает на противоположную действующей ----
  function syncThemeButtons() {
    const dark = prefsApi.theme() === 'dark';
    document.querySelectorAll('.ui-theme-btn').forEach((button) => {
      button.innerHTML = svg(dark ? 'sun' : 'moon'); // значок показывает, что включится по нажатию
      const label = dark ? 'Включить светлую тему' : 'Включить тёмную тему';
      button.setAttribute('aria-label', label);
      button.title = label;
    });
  }
  function toggleTheme() {
    const next = prefsApi.theme() === 'dark' ? 'light' : 'dark';
    prefsApi.update({ theme: next });
    toast(next === 'dark' ? 'Тёмная тема включена' : 'Светлая тема включена', { ms: 2200 });
  }

  // ---- боковые кнопки «А+» и «А−»: размер текста в один клик с любой страницы ----
  let fontBar = null;
  function syncFontBar() {
    if (!fontBar) return;
    const step = prefsApi.read().text;
    fontBar.querySelector('[data-font="up"]').disabled = step >= TEXT_STEPS.length - 1;
    fontBar.querySelector('[data-font="down"]').disabled = step <= 0;
    fontBar.querySelector('output').textContent = TEXT_STEPS[step].label;
  }
  function mountFontBar() {
    if (fontBar || document.body.hasAttribute('data-ui-nofont')) return;
    fontBar = document.createElement('div');
    fontBar.className = 'ui-fontbar';
    fontBar.setAttribute('role', 'group');
    fontBar.setAttribute('aria-label', 'Размер текста');
    fontBar.innerHTML = `<button type="button" data-font="up" aria-label="Увеличить шрифт" title="Увеличить шрифт"><span class="ui-big" aria-hidden="true">А+</span></button>
      <output aria-live="polite" aria-label="Размер текста"></output>
      <button type="button" data-font="down" aria-label="Уменьшить шрифт" title="Уменьшить шрифт"><span aria-hidden="true">А−</span></button>`;
    fontBar.addEventListener('click', (event) => {
      const button = event.target.closest('[data-font]');
      if (!button || button.disabled) return;
      const step = prefsApi.read().text + (button.dataset.font === 'up' ? 1 : -1);
      prefsApi.update({ text: Math.max(0, Math.min(TEXT_STEPS.length - 1, step)) });
    });
    document.body.appendChild(fontBar);
    syncFontBar();
  }

  /** Кнопки в шапке каждой страницы (тема и панель): место для них помечено data-a11y-slot. */
  function mountA11y() {
    if (!prefsApi) return;
    document.querySelectorAll('[data-a11y-slot]').forEach((slot) => {
      if (slot.querySelector('.ui-a11y-btn')) return;
      const themeButton = document.createElement('button');
      themeButton.type = 'button';
      themeButton.className = 'ui-a11y-btn ui-theme-btn';
      themeButton.addEventListener('click', toggleTheme);
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'ui-a11y-btn';
      button.setAttribute('aria-haspopup', 'dialog');
      button.setAttribute('aria-label', 'Удобство просмотра: тема, размер текста, контраст, цвета');
      button.title = 'Удобство просмотра';
      button.dataset.a11yOpen = '';
      button.innerHTML = svg('person');
      slot.append(themeButton, button);
    });
    syncThemeButtons();
    mountFontBar();
    // Настройку поменяли в любом месте (панель, шапка, боковые кнопки) — остальные места показывают то же самое
    window.addEventListener('afisha:prefs', () => { syncThemeButtons(); syncFontBar(); fillA11y(); });
    // Тема «как в системе»: система переключилась на тёмную или светлую — меняем значок
    if (window.matchMedia) window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', syncThemeButtons);
  }
  document.addEventListener('click', (event) => {
    const opener = event.target.closest('[data-a11y-open]');
    if (opener) openA11y(opener);
  });

  window.ui = { toast, busy, openA11y };
  const start = () => { mountNav(); mountA11y(); stagger(); openChatFromLink(); root.classList.add('ui-ready'); };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
})();
