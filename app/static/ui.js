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
  const reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

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
    const hide = () => { item.classList.add('ui-out'); setTimeout(() => item.remove(), reduce ? 0 : 280); };
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
    if (reduce) return;
    document.querySelectorAll('[data-rise]').forEach((node, index) => { node.style.setProperty('--i', String(index)); node.classList.add('ui-rise'); });
  }

  // Картинка, которая не загрузилась (data-hide-on-error), убирается: под ней остаётся запасная иконка.
  document.addEventListener('error', (event) => {
    const img = event.target;
    if (img && img.tagName === 'IMG' && img.hasAttribute('data-hide-on-error')) img.remove();
  }, true);

  window.ui = { toast, busy };
  const start = () => { mountNav(); stagger(); openChatFromLink(); root.classList.add('ui-ready'); };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
})();
