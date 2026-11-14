// Кабинет: вход через Telegram, история посещений, подписки, сохранённые поиски, обращения, ключи Министерства.
// Все данные приходят из /api/v1/me/*, кука входа HttpOnly: скрипт её не видит и не хранит. Сайт работает и без аккаунта.
(() => {
  const $ = (selector) => document.querySelector(selector);
  const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const CARD = 'grid gap-3 rounded-panel border border-stroke bg-glass-hi p-6 shadow-surface w600:p-4';
  const H2 = 'm-0 text-[1.2rem] font-extrabold';
  const BTN = 'min-h-[44px] rounded-ctl bg-brand px-5 font-bold text-white shadow-cta tx-btn hover:bg-brand-hover disabled:cursor-progress disabled:opacity-60';
  const BTN2 = 'min-h-[40px] rounded-field border border-stroke bg-glass-hi px-4 font-[650] text-brand-ink shadow-surface hover:bg-surface';
  const BTN_BAD = 'min-h-[40px] rounded-field border border-stroke bg-bad-bg px-4 font-[650] text-bad hover:opacity-90';
  const INPUT = 'min-h-[44px] w-full rounded-field border border-line bg-glass-field px-[14px] py-[8px] text-base font-medium text-ink tx-field focus-visible:border-brand focus-visible:shadow-focus focus-visible:outline-none';
  const LABEL = 'grid gap-[6px] text-[.82rem] font-bold tracking-[.02em] text-ink-3';
  const MUTED = 'm-0 text-[.9rem] text-ink-3';
  const ROW = 'flex flex-wrap items-center justify-between gap-2 rounded-ctl border border-stroke bg-surface/60 px-3 py-2';
  const LINK = 'font-semibold text-accent underline underline-offset-2';
  const KIND = { view: 'открыта карточка', ticket: 'переход по ссылке источника' };
  const inTelegram = /tgWebApp(Data|Platform)=/.test(location.hash);
  let me = null;
  let directory = { cities: [], categories: [] };
  let searches = {};

  // ---------- вспомогательное ----------

  const api = afishaApi.create({ cache: 'no-store' }); // общий клиент: /static/api.js

  const fmt = (iso) => {
    const m = /^(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d)/.exec(iso || '');
    return m ? `${m[3]}.${m[2]}.${m[1]} ${m[4]}:${m[5]}` : '';
  };

  /** Результат действия показывается уведомлением внизу экрана (ui.js): его видно, где бы ни стоял человек. Без ui.js: строка вверху. */
  function say(text, bad) {
    if (window.ui && text) { window.ui.toast(text, { bad: !!bad }); return; }
    const box = $(bad ? '#problem' : '#notice');
    $(bad ? '#notice' : '#problem').hidden = true;
    box.textContent = text || '';
    box.hidden = !text;
  }

  /** Кнопка крутит индикатор, пока идёт запрос; строка списка плавно исчезает перед удалением. */
  const busy = (button, action) => (window.ui && button ? window.ui.busy(button, action) : action());
  async function fadeOut(node) {
    if (!node) return;
    node.style.height = `${node.offsetHeight}px`;
    void node.offsetHeight;
    node.classList.add('ui-gone');
    await new Promise((resolve) => setTimeout(resolve, 280));
  }

  async function guarded(action) {
    try { await action(); } catch (error) {
      if (error.status === 401) { me = null; render(); say('Вход закончился: войдите снова.', true); } else say(error.message, true);
    }
  }

  function setHistoryFlag(on) { // читает visit.js на странице события: без согласия он ничего не отправляет
    try { localStorage.setItem('afisha.history', on ? '1' : '0'); } catch (_) { /* хранилище недоступно */ }
  }

  // ---------- вход ----------

  function telegramInitData() {
    return new Promise((resolve) => {
      const ready = () => (window.Telegram && window.Telegram.WebApp && window.Telegram.WebApp.initData) || '';
      if (ready()) return resolve(ready());
      const script = document.createElement('script');
      script.src = '/static/telegram-web-app.js';
      script.onload = () => resolve(ready());
      script.onerror = () => resolve('');
      document.head.appendChild(script);
    });
  }

  function showLogin() {
    $('#login').hidden = false;
    $('#app').hidden = true;
    $('#logout').hidden = true;
    const box = $('#tg-login');
    box.textContent = '';
    if (!me.login.enabled) { $('#login-off').hidden = false; return; }
    if (inTelegram) { $('#tg-miniapp').hidden = false; return; }
    $('#bot-login').hidden = false;
    $('#tg-login-alt').hidden = false;
    // Одноразовый nonce: кука и адрес возврата должны совпасть, иначе чужая ссылка могла бы войти в чужой аккаунт за посетителя.
    const bytes = crypto.getRandomValues(new Uint8Array(18));
    const nonce = btoa(String.fromCharCode(...bytes)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
    document.cookie = `afisha_login_nonce=${nonce}; Max-Age=600; Path=/; SameSite=Lax${location.protocol === 'https:' ? '; Secure' : ''}`;
    const script = document.createElement('script');
    script.async = true;
    script.src = 'https://telegram.org/js/telegram-widget.js?22';
    script.setAttribute('data-telegram-login', me.login.bot_username);
    script.setAttribute('data-size', 'large');
    script.setAttribute('data-request-access', 'write'); // чтобы бот мог написать об ответе на обращение и новых событиях
    script.setAttribute('data-auth-url', `${location.origin}/api/v1/auth/telegram/callback/${nonce}`);
    box.appendChild(script);
  }

  // Вход через бота: не нужен ни виджет telegram.org (в России открывается не всегда), ни привязка домена в BotFather.
  // Сайт берёт одноразовую ссылку в бот, человек подтверждает вход кнопкой в боте, страница сама забирает вход.
  let botPoll = null;
  $('#bot-login-btn').addEventListener('click', () => guarded(async () => {
    const status = $('#bot-login-status');
    const start = await api.post('/api/v1/auth/telegram/bot', {});
    window.open(start.url, '_blank', 'noopener');
    status.textContent = 'Ждём подтверждения в боте… Если бот не открылся, перейдите по ссылке: ' + start.url;
    clearInterval(botPoll);
    const until = Date.now() + start.expires_in * 1000;
    botPoll = setInterval(async () => {
      if (Date.now() > until) { clearInterval(botPoll); status.textContent = 'Ссылка устарела. Нажмите «Войти через Telegram» ещё раз.'; return; }
      try {
        const res = await fetch(`/api/v1/auth/telegram/bot/${encodeURIComponent(start.code)}`, { credentials: 'same-origin', headers: { Accept: 'application/json' } });
        if (res.status === 202) return;
        clearInterval(botPoll);
        if (res.ok) { me = await res.json(); render(); return; }
        status.textContent = res.status === 410 ? 'Ссылка устарела. Нажмите «Войти через Telegram» ещё раз.' : 'Вход не выполнен. Попробуйте ещё раз.';
      } catch (_) { /* сеть моргнула — спросим ещё раз через пару секунд */ }
    }, 2000);
  }));

  $('#tg-miniapp').addEventListener('click', () => guarded(async () => {
    const initData = await telegramInitData();
    if (!initData) { say('Не удалось получить подпись Telegram. Откройте кабинет из бота.', true); return; }
    me = await api.post('/api/v1/auth/telegram', { init_data: initData });
    render();
  }));

  // ---------- отрисовка ----------

  function render() {
    $('#loading').hidden = true;
    if (!me || !me.authenticated) { setHistoryFlag(false); showLogin(); return; }
    $('#login').hidden = true;
    $('#logout').hidden = false;
    setHistoryFlag(me.user.history_enabled);
    const user = me.user;
    const role = user.role;
    const app = $('#app');
    app.hidden = false;
    app.innerHTML = `
      <nav class="ui-jump" id="jump" aria-label="Разделы кабинета">
        <a href="#recs">Для вас</a><a href="#hist">История</a><a href="#subs">Подписки</a><a href="#srch">Поиски</a><a href="#fb">Обращения</a>${role === 'ministry' ? '<a href="#min">Ключи</a>' : ''}<a href="#data">Мои данные</a>
      </nav>
      <section class="${CARD}" aria-labelledby="who">
        <h2 id="who" class="${H2}">${esc(user.display_name || 'Без имени')}</h2>
        <p class="m-0 text-ink-2">Роль: <b>${esc(user.role_label)}</b></p>
        ${role === 'moderator' ? `<p class="m-0"><a class="${LINK}" href="/admin">Открыть администрирование афиши</a></p>` : ''}
        ${role === 'organizer' ? `<p class="m-0"><a class="${LINK}" href="/feedback?kind=organizer">Отправить заявку: добавить или уточнить событие</a></p>` : ''}
        ${role === 'user' ? `<p class="${MUTED}">Организатору или представителю Министерства роль выдаёт администратор афиши. Чтобы получить её, напишите через <a class="${LINK}" href="/feedback?kind=organizer">форму обратной связи</a>.</p>` : ''}
      </section>

      <section class="${CARD}" id="recs" aria-labelledby="recs-title">
        <h2 id="recs-title" class="${H2}">Рекомендации для вас</h2>
        <p class="${MUTED}" id="recs-why">Подбираем по вашей истории посещений, подпискам и сохранённым поискам.</p>
        <div id="recs-profile" class="flex flex-wrap gap-2"></div>
        <div id="recs-list" class="grid grid-cols-[repeat(auto-fill,minmax(230px,1fr))] gap-3"></div>
      </section>

      <section class="${CARD}" id="hist" aria-labelledby="hist-title">
        <h2 id="hist-title" class="${H2}">История посещений</h2>
        <label class="ui-switch"><input type="checkbox" role="switch" id="hist-switch"><span class="ui-track"></span><span id="hist-switch-text">История выключена</span></label>
        <p class="m-0 text-ink-2" id="hist-state"></p>
        <div class="flex flex-wrap gap-2">
          <button type="button" class="${BTN2} ui-secondary" data-act="clear-history">Очистить историю</button>
        </div>
        <div id="hist-list" class="grid gap-2"></div>
        <p class="${MUTED}">Хранится не дольше ${esc(user.history_retention_days)} дней. Мы записываем только номер карточки, тип действия и время. Выключите историю, и всё собранное стирается.</p>
      </section>

      <section class="${CARD}" id="subs" aria-labelledby="subs-title">
        <h2 id="subs-title" class="${H2}">Подписки на новые события</h2>
        <div id="subs-list" class="grid gap-2"></div>
        <form id="subs-form" class="grid grid-cols-[1fr_1fr_auto] gap-3 w600:grid-cols-1">
          <label class="${LABEL}"><span>Город</span><select id="subs-city" class="${INPUT}"></select></label>
          <label class="${LABEL}"><span>Категория</span><select id="subs-cat" class="${INPUT}"></select></label>
          <button type="submit" class="${BTN} self-end">Подписаться</button>
        </form>
        <p class="${MUTED}">Уведомления приходят в Telegram от бота не чаще раза в сутки, днём. Отписаться можно здесь или командой /unsubscribe.</p>
      </section>

      <section class="${CARD}" id="srch" aria-labelledby="srch-title">
        <h2 id="srch-title" class="${H2}">Сохранённые поиски</h2>
        <div id="srch-list" class="grid gap-2"></div>
        <form id="srch-form" class="grid gap-3">
          <div class="grid grid-cols-2 gap-3 w600:grid-cols-1">
            <label class="${LABEL}"><span>Название</span><input id="s-name" class="${INPUT}" maxlength="80" required placeholder="например, джаз в Уфе"></label>
            <label class="${LABEL}"><span>Слова</span><input id="s-q" class="${INPUT}" maxlength="200" placeholder="джаз, выставка…"></label>
            <label class="${LABEL}"><span>Город</span><select id="s-city" class="${INPUT}"></select></label>
            <label class="${LABEL}"><span>Категория</span><select id="s-cat" class="${INPUT}"></select></label>
            <label class="${LABEL}"><span>Цена не выше, ₽</span><input id="s-price" type="number" min="0" max="1000000" class="${INPUT}"></label>
          </div>
          <div class="flex flex-wrap gap-4 text-ink-2">
            <label class="flex items-center gap-2"><input id="s-free" type="checkbox"> Только бесплатные</label>
            <label class="flex items-center gap-2"><input id="s-kids" type="checkbox"> Для детей</label>
          </div>
          <div><button type="submit" class="${BTN}">Сохранить поиск</button></div>
        </form>
      </section>

      <section class="${CARD}" id="fb" aria-labelledby="fb-title" hidden>
        <h2 id="fb-title" class="${H2}">Мои обращения</h2>
        <div id="fb-list" class="grid gap-2"></div>
      </section>

      ${role === 'ministry' ? `
      <section class="${CARD}" id="min" aria-labelledby="min-title">
        <h2 id="min-title" class="${H2}">Министерство: ключи и сводка</h2>
        <div id="min-stats" class="grid gap-1 text-ink-2"></div>
        <h3 class="m-0 mt-2 text-[1rem] font-extrabold">Ключи для встраивания</h3>
        <p class="${MUTED}">Ключ только для чтения афиши. Передавайте его в заголовке <code>X-Partner-Key</code>: у ключа свой лимит запросов в минуту, записать им ничего нельзя.</p>
        <div id="keys-new" class="grid gap-2 rounded-ctl bg-brand-tint px-4 py-3" role="status" hidden></div>
        <div id="keys-list" class="grid gap-2"></div>
        <form id="keys-form" class="grid grid-cols-[1fr_auto] gap-3 w600:grid-cols-1">
          <label class="${LABEL}"><span>Для чего ключ</span><input id="k-name" class="${INPUT}" maxlength="80" required placeholder="сайт министерства"></label>
          <button type="submit" class="${BTN} self-end">Выпустить ключ</button>
        </form>
      </section>` : ''}

      <section class="${CARD}" id="data" aria-labelledby="data-title">
        <h2 id="data-title" class="${H2}">Мои данные</h2>
        <p class="m-0 text-ink-2">Мы храним номер вашего Telegram, имя оттуда же, роль и то, что вы сами сохранили здесь. Телефона, почты и паролей нет.</p>
        <div class="flex flex-wrap gap-2">
          <a class="${BTN2} inline-flex items-center no-underline" href="/api/v1/me/export" download="afisha-my-data.json">Скачать мои данные (JSON)</a>
          <button type="button" class="${BTN_BAD}" data-act="delete-account">Удалить аккаунт и все данные</button>
        </div>
      </section>`;
    fillSelects();
    // Блоки появляются по очереди; пункт панели переходов подсвечивается по мере прокрутки
    [...app.children].forEach((node, index) => { node.style.setProperty('--i', String(index)); node.classList.add('ui-rise'); });
    watchSections();
    loadInsights(); loadHistory(); loadSubs(); loadSearches(); loadFeedback();
    if (role === 'ministry') { loadKeys(); loadStats(); }
  }

  function watchSections() {
    if (!('IntersectionObserver' in window)) return;
    const links = [...document.querySelectorAll('#jump a')];
    const seen = new IntersectionObserver((entries) => {
      entries.filter((e) => e.isIntersecting).forEach((e) => links.forEach((a) => a.setAttribute('aria-current', String(a.getAttribute('href') === `#${e.target.id}`))));
    }, { rootMargin: '-20% 0px -65% 0px' });
    links.forEach((a) => { const target = document.querySelector(a.getAttribute('href')); if (target) seen.observe(target); });
  }

  function fillSelects() {
    const cities = ['<option value="">Вся республика</option>', ...directory.cities.map((c) => `<option>${esc(c.name)}</option>`)].join('');
    const cats = ['<option value="">Любая категория</option>', ...directory.categories.map((c) => `<option value="${esc(c.slug)}">${esc(c.label)}</option>`)].join('');
    ['#subs-city', '#s-city'].forEach((id) => { $(id).innerHTML = cities; });
    ['#subs-cat', '#s-cat'].forEach((id) => { $(id).innerHTML = cats; });
  }

  // ---------- история ----------

  // Рекомендации: профиль интересов и события, которых вы ещё не открывали, с объяснением (GET /api/v1/me/insights)
  async function loadInsights() {
    const list = $('#recs-list');
    list.innerHTML = '<div class="ui-skeleton"></div><div class="ui-skeleton"></div><div class="ui-skeleton"></div>';
    try {
      const data = await api('/api/v1/me/insights');
      const p = data.profile;
      const chips = [
        ...p.top_categories.map((c) => esc(c.label)),
        ...p.top_cities.map((c) => esc(c.name)),
        p.median_price ? `около ${Number(p.median_price)} ₽` : '',
        p.events_viewed ? `открыто событий: ${Number(p.events_viewed)}` : '',
      ].filter(Boolean);
      $('#recs-profile').innerHTML = chips.map((c) => `<span class="rounded-full border border-stroke bg-glass px-3 py-1 text-[.88rem] font-semibold">${c}</span>`).join('');
      $('#recs-why').textContent = data.personal
        ? 'Подобрано по вашим интересам: ниже под каждым событием — почему оно здесь.'
        : 'Пока мы мало о вас знаем: показываем популярное. Включите историю посещений или оформите подписку — подборка станет личной.';
      list.innerHTML = data.recommendations.length ? data.recommendations.map(({ event: e, reasons }) => `
        <a class="grid content-start gap-1 rounded-ctl border border-stroke bg-glass p-4 text-inherit no-underline shadow-surface transition hover:-translate-y-0.5 hover:bg-surface" href="/event/${Number(e.id) || 0}">
          <span class="text-[.74rem] font-extrabold uppercase tracking-[.08em] text-accent">${esc(e.category_label)}</span>
          <strong class="text-[1.02rem] leading-[1.25]">${esc(e.title)}</strong>
          <span class="text-[.9rem] text-ink-2">${esc(new Date(e.start_at).toLocaleString('ru-RU', { day: 'numeric', month: 'long', hour: '2-digit', minute: '2-digit' }))} · ${esc([e.venue, e.city].filter(Boolean).join(', '))}</span>
          <span class="text-[.9rem] font-semibold">${esc(e.price_text)}</span>
          <span class="text-[.85rem] text-ink-3">${reasons.map(esc).join(' · ')}</span>
        </a>`).join('') : `<p class="${MUTED}">Подходящих будущих событий пока нет.</p>`;
    } catch (_) {
      list.innerHTML = `<p class="${MUTED}">Не удалось загрузить рекомендации. Обновите страницу.</p>`;
    }
  }

  async function loadHistory() {
    await guarded(async () => {
      const page = await api('/api/v1/me/history?limit=50');
      const on = page.enabled;
      $('#hist-state').textContent = on ? 'История включена: мы запоминаем открытые вами карточки событий.' : 'История выключена. Включите её, если хотите видеть, какие события вы смотрели.';
      $('#hist-switch').checked = on;
      $('#hist-switch-text').textContent = on ? 'История включена' : 'История выключена';
      setHistoryFlag(on);
      $('#hist-list').innerHTML = page.items.length
        ? page.items.map((v) => `<div class="${ROW}"><span>${v.available ? `<a class="${LINK}" href="/event/${Number(v.event_id)}">${esc(v.title)}</a>` : '<span class="text-ink-3">карточка удалена из афиши</span>'}
            <span class="${MUTED}"> · ${esc(KIND[v.kind] || v.kind)}${v.available && v.start_at ? `, событие ${esc(fmt(v.start_at))}` : ''}</span></span>
            <span class="${MUTED}">${esc(fmt(v.viewed_at))}</span></div>`).join('') + (page.total > page.items.length ? `<p class="${MUTED}">Показаны последние ${page.items.length} из ${page.total}.</p>` : '')
        : (on ? `<p class="${MUTED}">Пока пусто: откройте карточку любого события.</p>` : '');
    });
  }

  // ---------- подписки ----------

  async function loadSubs() {
    await guarded(async () => {
      try {
        const subs = await api('/api/v1/subscriptions');
        $('#subs-list').innerHTML = subs.length
          ? subs.map((s) => `<div class="${ROW}"><span>${esc(s.label)}</span><button type="button" class="${BTN2}" data-act="unsub" data-id="${Number(s.id)}">Отписаться</button></div>`).join('')
          : `<p class="${MUTED}">Подписок нет.</p>`;
      } catch (error) {
        if (error.status === 401) throw error;
        $('#subs-list').innerHTML = `<p class="${MUTED}">${esc(error.message)}</p>`;
      }
    });
  }

  document.addEventListener('submit', (event) => {
    const id = event.target.id;
    if (!['subs-form', 'srch-form', 'keys-form'].includes(id)) return;
    event.preventDefault();
    busy(event.submitter, () => guarded(async () => {
      if (id === 'subs-form') {
        await api.post('/api/v1/subscriptions', { city: $('#subs-city').value, category: $('#subs-cat').value });
        say('Подписка оформлена.');
        await loadSubs();
      } else if (id === 'srch-form') {
        const filters = { q: $('#s-q').value.trim(), city: $('#s-city').value, category: $('#s-cat').value,
          free: $('#s-free').checked, kids: $('#s-kids').checked };
        if ($('#s-price').value !== '') filters.price_max = Number($('#s-price').value);
        await api.post('/api/v1/me/searches', { name: $('#s-name').value.trim(), filters });
        event.target.reset();
        say('Поиск сохранён.');
        await loadSearches();
      } else {
        const created = await api.post('/api/v1/me/api-keys', { name: $('#k-name').value.trim() });
        event.target.reset();
        const box = $('#keys-new');
        box.hidden = false;
        box.innerHTML = `<b>Ключ «${esc(created.name)}»: скопируйте сейчас, потом он не покажется</b>
          <code class="break-all rounded-field bg-surface px-3 py-2 text-[.95rem]">${esc(created.key)}</code>
          <span class="${MUTED}">Пример: <code>curl -H "X-Partner-Key: ${esc(created.key)}" ${esc(location.origin)}/api/v1/events</code></span>`;
        await loadKeys();
      }
    }));
  });

  // ---------- сохранённые поиски ----------

  const FILTER_NAMES = { q: 'слова', city: 'город', venue: 'площадка', category: 'категория', when: 'период', date_from: 'с', date_to: 'по', price_max: 'цена до', age: 'возраст', free: 'бесплатно', kids: 'для детей' };
  const describe = (filters) => Object.entries(filters).map(([k, v]) => (v === true ? FILTER_NAMES[k] : `${FILTER_NAMES[k] || k}: ${v}`)).join(', ');

  async function loadSearches() {
    await guarded(async () => {
      const items = await api('/api/v1/me/searches');
      $('#srch-list').innerHTML = items.length
        ? items.map((s) => `<div data-search="${Number(s.id)}" class="grid gap-2"><div class="${ROW}"><span><b>${esc(s.name)}</b> <span class="${MUTED}">${esc(describe(s.filters))}</span></span>
            <span class="flex gap-2"><button type="button" class="${BTN2}" data-act="run-search" data-id="${Number(s.id)}">Показать</button>
            <button type="button" class="${BTN2}" data-act="del-search" data-id="${Number(s.id)}">Удалить</button></span></div>
            <div class="grid gap-1 pl-3" data-results></div></div>`).join('')
        : `<p class="${MUTED}">Сохранённых поисков нет.</p>`;
      searches = Object.fromEntries(items.map((s) => [s.id, s.filters]));
    });
  }

  async function runSearch(id) {
    const params = new URLSearchParams({ limit: '5' });
    Object.entries(searches[id] || {}).forEach(([k, v]) => params.set(k, v === true ? 'true' : String(v)));
    const found = await api(`/api/v1/events?${params}`);
    const box = document.querySelector(`[data-search="${Number(id)}"] [data-results]`);
    box.innerHTML = found.items.length
      ? found.items.map((e) => `<a class="${LINK}" href="/event/${Number(e.id)}">${esc(e.title)}</a> <span class="${MUTED}">${esc(fmt(e.start_at))}</span>`).join('<br>')
        + (found.total > found.items.length ? `<span class="${MUTED}">…и ещё ${found.total - found.items.length}</span>` : '')
      : `<span class="${MUTED}">Сейчас ничего не нашлось.</span>`;
  }

  // ---------- обращения ----------

  async function loadFeedback() {
    await guarded(async () => {
      const items = await api('/api/v1/me/feedback');
      $('#fb').hidden = !items.length && me.user.role !== 'organizer';
      $('#fb-list').innerHTML = items.length
        ? items.map((f) => `<div class="${ROW} !grid"><span><b>${esc(f.kind_label)}</b> <span class="${MUTED}">· ${esc(fmt(f.created_at))} · ${esc(f.status_label)}</span></span>
            <span class="text-ink-2">${esc(f.message)}</span>${f.reply ? `<span class="rounded-field bg-brand-tint px-3 py-2 text-brand-ink"><b>Ответ:</b> ${esc(f.reply)}</span>` : ''}</div>`).join('')
        : `<p class="${MUTED}">Пока обращений нет. Отправьте заявку через <a class="${LINK}" href="/feedback?kind=organizer">форму</a>, пока вы вошли: здесь появятся статус и ответ.</p>`;
    });
  }

  // ---------- Министерство ----------

  async function loadKeys() {
    await guarded(async () => {
      const keys = await api('/api/v1/me/api-keys');
      $('#keys-list').innerHTML = keys.length ? keys.map((k) => `<div class="${ROW}"><span><b>${esc(k.name)}</b> <code>${esc(k.prefix)}…</code>
          <span class="${MUTED}"> · ${esc(k.rate_limit)} запросов/мин · запросов: ${esc(k.requests_total)}${k.last_used_at ? ` · последний ${esc(fmt(k.last_used_at))}` : ''}${k.revoked_at ? ' · отозван' : ''}</span></span>
          ${k.revoked_at ? '' : `<button type="button" class="${BTN2}" data-act="revoke-key" data-id="${Number(k.id)}">Отозвать</button>`}</div>`).join('')
        : `<p class="${MUTED}">Ключей нет.</p>`;
    });
  }

  async function loadStats() {
    await guarded(async () => {
      const s = await api('/api/v1/ministry/stats');
      const row = (label, value) => `<div class="flex justify-between gap-3"><span>${esc(label)}</span><b>${esc(value)}</b></div>`;
      const kinds = Object.entries(s.feedback_by_kind).map(([k, v]) => `${k}: ${v}`).join(', ') || '—';
      $('#min-stats').innerHTML = row('Событий в афише', s.events_published) + row('В очереди модерации', s.events_in_queue)
        + row('Обращений всего', s.feedback_total) + row('По видам', kinds) + row('Подписок на новые события', s.subscriptions_total);
    });
  }

  // ---------- действия ----------

  document.addEventListener('click', (click) => {
    const button = click.target.closest('[data-act]');
    if (!button) return;
    const id = button.dataset.id;
    busy(button, () => guarded(async () => {
      switch (button.dataset.act) {
        case 'clear-history': {
          if (!confirm('Стереть всю историю посещений?')) return;
          const done = await api.delete('/api/v1/me/history');
          say(`Стёрто записей: ${done.removed}.`);
          await loadHistory();
          break;
        }
        case 'unsub': await api.delete(`/api/v1/subscriptions/${Number(id)}`); await fadeOut(button.closest('div')); await loadSubs(); say('Вы отписались.'); break;
        case 'run-search': await runSearch(id); break;
        case 'del-search': await api.delete(`/api/v1/me/searches/${Number(id)}`); await fadeOut(button.closest('[data-search]')); await loadSearches(); say('Поиск удалён.'); break;
        case 'revoke-key':
          if (!confirm('Отозвать ключ? Сайты, которые его используют, перестанут получать данные.')) return;
          await api.delete(`/api/v1/me/api-keys/${Number(id)}`);
          await loadKeys();
          break;
        case 'delete-account':
          if (!confirm('Удалить аккаунт и все данные о себе: историю, подписки, сохранённые поиски, ключи? Это нельзя отменить.')) return;
          await api.delete('/api/v1/me');
          me = await api('/api/v1/me');
          render();
          say('Аккаунт и данные удалены.');
          break;
        default:
      }
    }));
  });

  // Переключатель истории: одно нажатие вместо «кнопка, потом подтверждение»; выключение стирает собранное, поэтому спрашиваем
  document.addEventListener('change', (event) => {
    if (event.target.id !== 'hist-switch') return;
    const box = event.target;
    const turnOn = box.checked;
    if (!turnOn && !confirm('Выключить историю? Всё, что собрано, будет стёрто.')) { box.checked = true; return; }
    guarded(async () => {
      me = await api.patch('/api/v1/me', { history_enabled: turnOn });
      say(turnOn ? 'История включена: открытые карточки попадут сюда.' : 'История выключена и стёрта.');
      await loadHistory();
    });
  });

  $('#logout').addEventListener('click', () => guarded(async () => {
    await api.post('/api/v1/auth/logout');
    me = await api('/api/v1/me');
    render();
    say('Вы вышли.');
  }));

  // ---------- старт ----------

  (async () => {
    const result = new URLSearchParams(location.search).get('login');
    if (result) history.replaceState(null, '', '/account');
    try {
      me = await api('/api/v1/me');
      const [cities, categories] = await Promise.all([api('/api/v1/cities').catch(() => []), api('/api/v1/categories').catch(() => [])]);
      directory = { cities, categories };
    } catch (error) {
      $('#loading').hidden = true;
      say('Не удалось загрузить кабинет. Проверьте соединение и обновите страницу.', true);
      return;
    }
    render();
    if (result === 'failed') say('Не удалось войти: подпись Telegram неверна или устарела. Попробуйте ещё раз.', true);
    if (result === 'limit') say('Слишком много попыток входа. Подождите минуту и повторите.', true);
  })();
})();
