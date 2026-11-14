// Интерфейс модератора. Данные приходят из /api/v1/admin/* и открываются только по ключу X-API-Key.
// Скрипт внешний (политика безопасности запрещает встроенные), все значения из базы экранируются.
(() => {
  const $ = (selector, root = document) => root.querySelector(selector);
  const esc = (value) =>
    String(value ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const STORE = 'afisha.admin.key'; // sessionStorage: ключ живёт только пока открыта вкладка
  const STATUS = { scheduled: 'Состоится', rescheduled: 'Перенесено', sold_out: 'Билеты проданы', cancelled: 'Отменено' };
  const ACTIONS = { create: 'создание', update: 'обновление', merge: 'слияние источников', status: 'статус', publication: 'публикация', edit: 'правка', delete: 'удаление', feedback: 'обращение', import: 'импорт', role: 'роль', api_key: 'ключ Министерства' };
  const ROLES = { user: 'Житель или гость', organizer: 'Организатор', moderator: 'Модератор', ministry: 'Министерство' };
  const FB_STATUS = { new: 'Принято', in_progress: 'В работе', processed: 'Обработано' };
  const BTN = 'min-h-[36px] rounded-field border border-stroke bg-glass-hi px-3 text-[.9rem] font-semibold text-brand-ink hover:bg-surface';
  const BTN_MAIN = 'min-h-[40px] rounded-ctl bg-brand px-4 font-bold text-white shadow-cta hover:bg-brand-hover';
  const FIELD = 'min-h-[40px] rounded-field border border-line bg-glass-field px-3 text-base font-medium text-ink focus-visible:border-brand focus-visible:outline-none';
  const TH = 'px-3 py-2 text-left text-[.76rem] font-extrabold uppercase tracking-[.08em] text-ink-3';
  const TD = 'border-t border-line px-3 py-3 align-top';

  let key = '';
  let via = 'key'; // key: вход по ключу X-API-Key; account: вход в аккаунт с ролью «модератор» (кука, ключ не нужен)
  let quiet = false; // первая попытка войти по аккаунту не должна показывать ошибку «ключ не подошёл»
  let categories = [];
  let tab = 'queue';
  const view = { offset: 0, query: '', status: 'all', publication: 'all' };
  let editing = null; // событие, которое правим; null — создаём новое

  // ---------- обмен с сервером ----------
  // Общий клиент (/static/api.js): ключ подставляется в каждый запрос, а при 401 показывается форма входа.
  const api = afishaApi.create({
    headers: () => (key ? { 'X-API-Key': key } : {}),
    onUnauthorized: () => { logout(quiet ? '' : 'Ключ не подошёл или устарел. Войдите снова.'); return new Error('Нужен вход'); },
  });
  const post = api.post;
  const patch = api.patch;

  function say(text, bad = false) {
    const ok = $('#notice'); const err = $('#problem');
    ok.hidden = bad || !text; err.hidden = !bad || !text;
    (bad ? err : ok).textContent = text || '';
  }

  // ---------- вход ----------
  function logout(message = '') {
    key = ''; sessionStorage.removeItem(STORE);
    $('#app').hidden = true; $('#login').hidden = false; $('#logout').hidden = true; $('#actor').hidden = true;
    const error = $('#login-error'); error.textContent = message; error.hidden = !message;
  }

  async function enter(candidate) {
    key = candidate;
    quiet = !candidate; // без ключа пробуем вход по аккаунту: если его нет, просто показываем форму
    try {
      const session = await api('/api/v1/admin/session');
      quiet = false;
      via = session.via || 'key';
      if (key) sessionStorage.setItem(STORE, key);
      $('#login').hidden = true; $('#app').hidden = false; $('#logout').hidden = false;
      $('#tab-users').hidden = !session.can_manage_users; // роли и ключи Министерства выдаёт только владелец ключа
      $('#actor').hidden = false;
      $('#actor').textContent = via === 'account' ? `Вы вошли аккаунтом модератора (${session.actor})` : `Вы вошли как ${session.actor}`;
      categories = await api.get('/api/v1/categories');
      $('#edit-form [name=category]').innerHTML = categories.map((c) => `<option value="${esc(c.slug)}">${esc(c.label)}</option>`).join('');
      refreshCounts();
      open(tab);
    } catch (error) {
      if (error.message !== 'Нужен вход') logout(error.message);
    }
  }

  async function refreshCounts() {
    try {
      const queue = await api('/api/v1/admin/events?publication=draft&limit=1');
      const fresh = await api('/api/v1/admin/feedback?status=new&limit=200');
      $('#queue-count').textContent = queue.total || '';
      $('#feedback-count').textContent = fresh.length || '';
    } catch (_) { /* счётчики необязательны */ }
  }

  // ---------- разделы ----------
  function open(name) {
    tab = name; say('');
    document.querySelectorAll('[data-tab]').forEach((b) => b.setAttribute('aria-selected', String(b.dataset.tab === name)));
    view.offset = 0; view.query = '';
    view.publication = name === 'queue' ? 'draft' : 'all';
    ({ queue: showEvents, events: showEvents, import: showImport, audit: showAudit, sync: showSync, feedback: showFeedback, users: showUsers }[name])();
  }

  const when = (iso) => (iso ? iso.replace('T', ' ').slice(0, 16) : '');

  async function showEvents(append = false) {
    const panel = $('#panel');
    if (!append) {
      panel.innerHTML = `<div class="mb-4 flex flex-wrap items-end gap-3">
        <label class="grid gap-1 text-[.82rem] font-bold text-ink-3"><span>Поиск</span><input id="q" type="search" value="${esc(view.query)}" placeholder="название или площадка" class="${FIELD}"></label>
        ${tab === 'events' ? `<label class="grid gap-1 text-[.82rem] font-bold text-ink-3"><span>Публикация</span><select id="pub" class="${FIELD}"><option value="all">любая</option><option value="published">опубликованные</option><option value="draft">черновики</option></select></label>
        <label class="grid gap-1 text-[.82rem] font-bold text-ink-3"><span>Статус</span><select id="st" class="${FIELD}"><option value="all">любой</option>${Object.entries(STATUS).map(([k, v]) => `<option value="${k}">${v}</option>`).join('')}</select></label>` : ''}
        <button type="button" id="find" class="${BTN_MAIN}">Найти</button>
        <button type="button" id="add" class="${BTN}">Добавить событие</button>
      </div>
      <p id="summary" class="mb-2 mt-0 text-[.95rem] font-semibold text-ink-3"></p>
      <div class="overflow-x-auto"><table class="w-full border-collapse"><thead><tr><th class="${TH}">№</th><th class="${TH}">Событие</th><th class="${TH}">Когда и где</th><th class="${TH}">Статус</th><th class="${TH}">Действия</th></tr></thead><tbody id="rows"></tbody></table></div>
      <div class="mt-4 text-center"><button type="button" id="more" class="${BTN}" hidden>Показать ещё</button></div>`;
      if (tab === 'events') { $('#pub').value = view.publication; $('#st').value = view.status; }
      $('#find').addEventListener('click', () => { view.query = $('#q').value.trim(); if (tab === 'events') { view.publication = $('#pub').value; view.status = $('#st').value; } view.offset = 0; showEvents(); });
      $('#q').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('#find').click(); });
      $('#add').addEventListener('click', () => openEditor(null));
      $('#more').addEventListener('click', () => showEvents(true));
      $('#rows').addEventListener('click', onRowAction);
    }
    const params = new URLSearchParams({ limit: 25, offset: view.offset, publication: view.publication, status: tab === 'events' ? view.status : 'all' });
    if (view.query) params.set('q', view.query);
    try {
      const data = await api(`/api/v1/admin/events?${params}`);
      $('#rows').insertAdjacentHTML('beforeend', data.items.map(rowHtml).join(''));
      view.offset += data.items.length;
      $('#summary').textContent = data.total ? `Найдено ${data.total}, показано ${view.offset}` : (tab === 'queue' ? 'Очередь модерации пуста: все события опубликованы.' : 'Ничего не найдено.');
      $('#more').hidden = view.offset >= data.total;
    } catch (error) { if (error.message !== 'Нужен вход') say(error.message, true); }
  }

  function rowHtml(e) {
    const draft = e.publication === 'draft';
    const place = [e.venue, e.city].filter(Boolean).join(', ');
    return `<tr data-id="${Number(e.id)}">
      <td class="${TD}">${Number(e.id)}</td>
      <td class="${TD}"><a class="font-bold text-brand-ink underline underline-offset-2" href="/event/${Number(e.id)}" target="_blank" rel="noopener noreferrer">${esc(e.title)}</a>
        <div class="mt-1 text-[.85rem] text-ink-3">${esc(e.category_label)} · источников: ${Number(e.source_count) || 0}${e.confidence != null ? ` · уверенность ${Number(e.confidence).toFixed(2)}` : ''}</div>
        ${draft && e.review_reason ? `<div class="mt-1 text-[.85rem] font-semibold text-warn">${esc(e.review_reason)}</div>` : ''}</td>
      <td class="${TD}">${esc(when(e.start_at))}<div class="text-[.85rem] text-ink-3">${esc(place)}</div></td>
      <td class="${TD}"><span class="${draft ? 'font-bold text-warn' : 'font-semibold text-ok'}">${draft ? 'Черновик' : 'Опубликовано'}</span>
        <select data-act="status" aria-label="Статус события ${Number(e.id)}" class="mt-1 block ${FIELD} !min-h-[34px] !text-[.9rem]">${Object.entries(STATUS).map(([k, v]) => `<option value="${k}"${k === e.status ? ' selected' : ''}>${v}</option>`).join('')}</select></td>
      <td class="${TD}"><div class="flex flex-wrap gap-2">
        <button type="button" data-act="${draft ? 'publish' : 'unpublish'}" class="${draft ? BTN_MAIN : BTN}">${draft ? 'Опубликовать' : 'Снять с публикации'}</button>
        <button type="button" data-act="edit" class="${BTN}">Править</button>
        <button type="button" data-act="history" class="${BTN}">Журнал</button></div></td></tr>`;
  }

  async function onRowAction(event) {
    const row = event.target.closest('tr[data-id]');
    const control = event.target.closest('[data-act]');
    if (!row || !control || (control.tagName === 'SELECT' && event.type === 'click')) return;
    const id = Number(row.dataset.id);
    try {
      if (control.dataset.act === 'publish' || control.dataset.act === 'unpublish') {
        await post(`/api/v1/admin/events/${id}/publication`, { publication: control.dataset.act === 'publish' ? 'published' : 'draft' });
        say(control.dataset.act === 'publish' ? `Событие ${id} опубликовано.` : `Событие ${id} снято с публикации.`);
        refreshCounts(); view.offset = 0; showEvents();
      } else if (control.dataset.act === 'edit') {
        openEditor(await api(`/api/v1/admin/events/${id}`));
      } else if (control.dataset.act === 'history') {
        openHistory(id);
      }
    } catch (error) { if (error.message !== 'Нужен вход') say(error.message, true); }
  }

  // смена статуса — событие change у списка в строке
  document.addEventListener('change', async (event) => {
    const select = event.target.closest('select[data-act="status"]');
    if (!select) return;
    const id = Number(select.closest('tr').dataset.id);
    try {
      await patch(`/api/v1/events/${id}/status`, { status: select.value });
      say(`Статус события ${id}: ${STATUS[select.value]}.`);
    } catch (error) { if (error.message !== 'Нужен вход') say(error.message, true); }
  });

  // ---------- создание и правка ----------
  const NUMBERS = ['price_min', 'price_max'];
  const DATES = ['start_at', 'end_at'];

  function openEditor(event) {
    editing = event;
    const form = $('#edit-form');
    form.reset();
    $('#edit-title').textContent = event ? `Правка события №${event.id}` : 'Новое событие';
    $('#create-only').hidden = !!event;
    $('#edit-error').hidden = true;
    if (event) {
      ['title', 'description', 'category', 'city', 'venue', 'address', 'url', 'price_min', 'price_max'].forEach((n) => { form.elements[n].value = event[n] ?? ''; });
      form.elements.age_limit.value = event.age_limit ? String(event.age_limit) : '';
      DATES.forEach((n) => { form.elements[n].value = event[n] ? event[n].slice(0, 16) : ''; });
    }
    $('#edit-dialog').showModal();
  }

  $('#edit-cancel').addEventListener('click', () => $('#edit-dialog').close());
  $('#edit-form').addEventListener('submit', async (submit) => {
    submit.preventDefault();
    const form = $('#edit-form');
    const value = (n) => form.elements[n].value.trim();
    const fields = {};
    for (const name of ['title', 'description', 'category', 'city', 'venue', 'address', 'url', 'age_limit']) fields[name] = value(name);
    for (const name of NUMBERS) fields[name] = value(name) === '' ? null : Number(value(name));
    for (const name of DATES) fields[name] = value(name) === '' ? null : `${value(name)}:00`;
    try {
      if (editing) {
        // отправляем только то, что поменялось: в журнале будут именно эти поля
        const changed = {};
        for (const [name, newValue] of Object.entries(fields)) {
          const old = name === 'age_limit' ? (editing.age_limit ? String(editing.age_limit) : '')
            : DATES.includes(name) ? (editing[name] ? editing[name].slice(0, 16) + ':00' : null)
              : (editing[name] ?? (NUMBERS.includes(name) ? null : ''));
          if (newValue !== old) changed[name] = name === 'age_limit' && newValue === '' ? null : newValue;
        }
        if (!Object.keys(changed).length) { $('#edit-dialog').close(); say('Ничего не изменилось.'); return; }
        await patch(`/api/v1/admin/events/${editing.id}`, changed);
        say(`Событие ${editing.id} сохранено.`);
      } else {
        const body = { ...fields, source: 'manual', publication: form.elements.publication.value };
        Object.keys(body).forEach((n) => { if (body[n] === '' || body[n] === null) delete body[n]; });
        const result = await post('/api/v1/events', body);
        say(result.outcome === 'created' ? `Событие добавлено (№${result.event.id}).` : `Такое событие уже есть (№${result.event.id}): данные дополнены.`);
      }
      $('#edit-dialog').close(); refreshCounts(); view.offset = 0; if (tab === 'queue' || tab === 'events') showEvents();
    } catch (error) {
      if (error.message === 'Нужен вход') return;
      const box = $('#edit-error'); box.textContent = error.message; box.hidden = false;
    }
  });

  // ---------- журнал ----------
  const short = (v) => { const s = typeof v === 'object' && v !== null ? JSON.stringify(v) : String(v ?? '—'); return s.length > 90 ? `${s.slice(0, 90)}…` : s; };
  function changesHtml(changes) {
    const items = Object.entries(changes || {}).map(([field, v]) => (Array.isArray(v) && v.length === 2
      ? `<li><b>${esc(field)}</b>: ${esc(short(v[0]))} → ${esc(short(v[1]))}</li>` : `<li><b>${esc(field)}</b>: ${esc(short(v))}</li>`));
    return items.length ? `<ul class="m-0 list-none p-0 text-[.88rem]">${items.join('')}</ul>` : '<span class="text-ink-3">без изменений полей</span>';
  }
  const auditRows = (entries) => entries.map((e) => `<tr><td class="${TD}">${esc(when(e.at))}</td><td class="${TD}">${esc(e.actor)}</td>
      <td class="${TD}">${esc(ACTIONS[e.action] || e.action)}</td><td class="${TD}">${e.event_id ? `<a class="underline" href="/event/${Number(e.event_id)}" target="_blank" rel="noopener noreferrer">№${Number(e.event_id)}</a>` : ''}</td>
      <td class="${TD}">${changesHtml(e.changes)}</td></tr>`).join('');
  const auditTable = (entries) => (entries.length
    ? `<div class="overflow-x-auto"><table class="w-full border-collapse"><thead><tr><th class="${TH}">Когда</th><th class="${TH}">Кто</th><th class="${TH}">Что</th><th class="${TH}">Событие</th><th class="${TH}">Изменения</th></tr></thead><tbody>${auditRows(entries)}</tbody></table></div>`
    : '<p class="text-ink-3">Записей нет.</p>');

  async function openHistory(id) {
    $('#audit-title').textContent = `Журнал события №${id}`;
    $('#audit-body').innerHTML = auditTable(await api(`/api/v1/admin/audit?event_id=${id}&limit=100`));
    $('#audit-dialog').showModal();
  }
  $('#audit-close').addEventListener('click', () => $('#audit-dialog').close());

  async function showAudit() {
    $('#panel').innerHTML = `<div class="mb-4 flex flex-wrap items-end gap-3">
      <label class="grid gap-1 text-[.82rem] font-bold text-ink-3"><span>Номер события</span><input id="a-event" type="number" min="1" class="${FIELD}"></label>
      <label class="grid gap-1 text-[.82rem] font-bold text-ink-3"><span>Кто</span><input id="a-actor" maxlength="100" class="${FIELD}"></label>
      <button type="button" id="a-find" class="${BTN_MAIN}">Показать</button></div><div id="a-body"></div>`;
    const load = async () => {
      const params = new URLSearchParams({ limit: 100 });
      if ($('#a-event').value) params.set('event_id', $('#a-event').value);
      if ($('#a-actor').value.trim()) params.set('actor', $('#a-actor').value.trim());
      try { $('#a-body').innerHTML = auditTable(await api(`/api/v1/admin/audit?${params}`)); } catch (error) { if (error.message !== 'Нужен вход') say(error.message, true); }
    };
    $('#a-find').addEventListener('click', load);
    load();
  }

  // ---------- сбор по расписанию ----------
  const SYNC_STATUS = { ok: 'ок', failed: 'сбой', running: 'идёт' };
  async function showSync() {
    $('#panel').innerHTML = '<div id="s-body"></div>';
    try {
      const runs = await api('/api/v1/admin/sync-runs?limit=50');
      $('#s-body').innerHTML = runs.length
        ? `<div class="overflow-x-auto"><table class="w-full border-collapse"><thead><tr><th class="${TH}">Когда</th><th class="${TH}">Задача</th><th class="${TH}">Итог</th><th class="${TH}">Получено</th><th class="${TH}">Создано</th><th class="${TH}">Обновлено</th><th class="${TH}">Опубликовано</th><th class="${TH}">В очереди</th><th class="${TH}">Пропало</th><th class="${TH}">Вернулось</th></tr></thead><tbody>`
          + runs.map((r) => `<tr><td class="${TD}">${esc(when(r.started_at))}</td><td class="${TD}">${esc(r.job)}</td><td class="${TD}"><span class="${r.status === 'failed' ? 'font-bold text-warn' : 'font-semibold text-ok'}">${esc(SYNC_STATUS[r.status] || r.status)}</span>${r.error ? `<div class="text-[.82rem] text-ink-3">${esc(r.error)}</div>` : ''}</td><td class="${TD}">${Number(r.fetched)}</td><td class="${TD}">${Number(r.created)}</td><td class="${TD}">${Number(r.merged)}</td><td class="${TD}">${Number(r.published)}</td><td class="${TD}">${Number(r.queued)}</td><td class="${TD}">${Number(r.vanished)}</td><td class="${TD}">${Number(r.revived)}</td></tr>`).join('')
          + '</tbody></table></div>'
        : '<p class="text-ink-3">Запусков пока не было. Расписание: <code>python -m app.scheduler</code>, подробности в docs/04.</p>';
    } catch (error) { if (error.message !== 'Нужен вход') say(error.message, true); }
  }

  // ---------- импорт ----------
  function showImport() {
    $('#panel').innerHTML = `<h2 class="m-0 mb-2 text-[1.1rem] font-extrabold">Загрузка событий из файла</h2>
      <p class="mt-0 text-ink-3">Формат JSON (список событий) или CSV (первая строка — названия полей: title, start_at, source — обязательные; city, venue, address, category, price_min, price_max, age_limit, url, description). Повторная загрузка того же файла дублей не создаёт.</p>
      <form id="import-form" class="grid max-w-[560px] gap-3">
        <input id="file" type="file" accept=".json,.csv,application/json,text/csv" required class="${FIELD}">
        <label class="flex items-center gap-2 font-medium"><input id="as-draft" type="checkbox" class="h-5 w-5 accent-brand"> Сохранить новые события черновиками (на модерацию)</label>
        <div><button type="submit" class="${BTN_MAIN}">Загрузить</button></div></form>
      <div id="report" class="mt-5"></div>`;
    $('#import-form').addEventListener('submit', async (submit) => {
      submit.preventDefault();
      const file = $('#file').files[0];
      if (!file) return;
      if (file.size > 4_000_000) { say('Файл больше 4 МБ: разделите его на части.', true); return; }
      try {
        const report = await post('/api/v1/admin/import', { filename: file.name, content: await file.text(), publish: !$('#as-draft').checked });
        say('');
        $('#report').innerHTML = `<p class="m-0 mb-3 text-[1.05rem] font-bold">Создано: ${report.created} · обновлено: ${report.updated} · без изменений: ${report.unchanged} · отклонено: ${report.rejected.length}</p>
          ${report.rejected.length ? `<h3 class="m-0 mb-2 text-[.95rem] font-extrabold text-bad">Отклонённые записи и причины</h3><div class="overflow-x-auto"><table class="w-full border-collapse"><thead><tr><th class="${TH}">Запись</th><th class="${TH}">Название</th><th class="${TH}">Почему</th></tr></thead><tbody>${report.rejected.map((r) => `<tr><td class="${TD}">${Number(r.row)}</td><td class="${TD}">${esc(r.title)}</td><td class="${TD}">${r.reasons.map(esc).join('<br>')}</td></tr>`).join('')}</tbody></table></div>` : ''}
          ${report.events.length ? `<h3 class="mb-2 mt-4 text-[.95rem] font-extrabold">Затронутые события${report.total > report.events.length ? ' (первые 100)' : ''}</h3><ul class="m-0 grid list-none gap-1 p-0">${report.events.map((e) => `<li><a class="text-brand-ink underline" href="/event/${Number(e.id)}" target="_blank" rel="noopener noreferrer">${esc(e.title)}</a> <span class="text-ink-3">· ${e.publication === 'draft' ? 'черновик' : 'опубликовано'}</span></li>`).join('')}</ul>` : ''}`;
        refreshCounts();
      } catch (error) { if (error.message !== 'Нужен вход') say(error.message, true); }
    });
  }

  // ---------- обращения ----------
  async function showFeedback() {
    $('#panel').innerHTML = `<div class="mb-4 flex flex-wrap items-end gap-3"><label class="grid gap-1 text-[.82rem] font-bold text-ink-3"><span>Показать</span>
      <select id="f-status" class="${FIELD}"><option value="new">новые</option><option value="in_progress">в работе</option><option value="processed">обработанные</option><option value="all">все</option></select></label></div><div id="f-body"></div>`;
    const load = async () => {
      try {
        const items = await api(`/api/v1/admin/feedback?status=${$('#f-status').value}&limit=100`);
        $('#f-body').innerHTML = items.length ? items.map((f) => `<article class="mb-3 grid gap-2 rounded-ctl border border-line bg-surface/60 p-4" data-id="${Number(f.id)}">
          <div class="flex flex-wrap items-center justify-between gap-2"><strong>${esc(f.kind_label)}${f.user_id ? ` <span class="rounded-full bg-brand-tint px-2 py-[2px] text-[.78rem] font-bold text-brand-ink">аккаунт${f.author_role && f.author_role !== 'user' ? `: ${esc(ROLES[f.author_role] || f.author_role)}` : ''}</span>` : ''}</strong><span class="text-[.85rem] text-ink-3">${esc(FB_STATUS[f.status] || f.status)} · ${esc(when(f.created_at))}${f.event_id ? ` · <a class="underline" href="/event/${Number(f.event_id)}" target="_blank" rel="noopener noreferrer">событие №${Number(f.event_id)}</a>` : ''}</span></div>
          <p class="m-0 whitespace-pre-wrap [overflow-wrap:anywhere]">${esc(f.message)}</p>
          ${f.reply ? `<p class="m-0 whitespace-pre-wrap rounded-field bg-brand-tint px-3 py-2 text-brand-ink [overflow-wrap:anywhere]"><b>Ответ автору:</b> ${esc(f.reply)}</p>` : ''}
          <div class="flex flex-wrap items-center justify-between gap-2 text-[.88rem] text-ink-3"><span>${f.contact ? `Контакт: ${esc(f.contact)}` : 'Контакт не указан'}${f.status === 'processed' ? ` · обработал ${esc(f.processed_by)}` : ''}</span>
            <span class="flex flex-wrap gap-2">${f.status !== 'in_progress' && f.status !== 'processed' ? `<button type="button" data-set="in_progress" class="${BTN}">Взять в работу</button>` : ''}
              <button type="button" data-set="${f.status === 'processed' ? 'new' : 'processed'}" class="${BTN}">${f.status === 'processed' ? 'Вернуть в новые' : 'Отметить обработанным'}</button></span></div>
          ${f.user_id ? `<div class="grid gap-2"><textarea rows="2" maxlength="1000" placeholder="Ответ автору (придёт ему в Telegram от бота)" class="${FIELD} w-full py-2" data-reply></textarea>
            <div><button type="button" data-send-reply class="${BTN}">Отправить ответ</button></div></div>` : '<p class="m-0 text-[.85rem] text-ink-3">Автор не входил в аккаунт: ответить ему из афиши нельзя, используйте контакт.</p>'}</article>`).join('')
          : '<p class="text-ink-3">Обращений нет.</p>';
      } catch (error) { if (error.message !== 'Нужен вход') say(error.message, true); }
    };
    $('#f-status').addEventListener('change', load);
    $('#f-body').addEventListener('click', async (event) => {
      const card = event.target.closest('[data-id]');
      const button = event.target.closest('[data-set], [data-send-reply]');
      if (!button) return;
      try {
        if (button.dataset.set) {
          await patch(`/api/v1/admin/feedback/${card.dataset.id}`, { status: button.dataset.set });
        } else {
          const reply = card.querySelector('[data-reply]').value.trim();
          if (!reply) { say('Напишите ответ.', true); return; }
          await patch(`/api/v1/admin/feedback/${card.dataset.id}`, { reply, status: 'processed' });
          say('Ответ записан, бот отправит его автору.');
        }
        refreshCounts(); load();
      } catch (error) { if (error.message !== 'Нужен вход') say(error.message, true); }
    });
    load();
  }

  // ---------- пользователи, роли и ключи Министерства (только по ключу администратора) ----------
  async function showUsers() {
    const roleOptions = (current) => Object.entries(ROLES).map(([k, v]) => `<option value="${k}"${k === current ? ' selected' : ''}>${esc(v)}</option>`).join('');
    $('#panel').innerHTML = `
      <h2 class="m-0 mb-2 text-[1.1rem] font-extrabold">Роли</h2>
      <p class="mb-3 mt-0 text-[.9rem] text-ink-3">Роль можно выдать заранее по номеру Telegram: она появится у человека при первом входе в кабинет. Номер человек видит в боте командой /start или у @userinfobot.</p>
      <form id="role-form" class="mb-4 flex flex-wrap items-end gap-3">
        <label class="grid gap-1 text-[.82rem] font-bold text-ink-3"><span>Номер Telegram</span><input id="r-id" type="number" min="1" required class="${FIELD}"></label>
        <label class="grid gap-1 text-[.82rem] font-bold text-ink-3"><span>Имя (для списка)</span><input id="r-name" maxlength="100" class="${FIELD}"></label>
        <label class="grid gap-1 text-[.82rem] font-bold text-ink-3"><span>Роль</span><select id="r-role" class="${FIELD}">${roleOptions('organizer')}</select></label>
        <button type="submit" class="${BTN_MAIN}">Назначить</button>
      </form>
      <div class="mb-4 flex flex-wrap items-end gap-3">
        <label class="grid gap-1 text-[.82rem] font-bold text-ink-3"><span>Показать</span><select id="u-role" class="${FIELD}"><option value="">все роли</option>${roleOptions('')}</select></label>
        <label class="grid gap-1 text-[.82rem] font-bold text-ink-3"><span>Имя или номер</span><input id="u-q" type="search" class="${FIELD}"></label>
        <button type="button" id="u-find" class="${BTN}">Найти</button>
      </div>
      <div id="u-body" class="overflow-x-auto"></div>
      <h2 class="mb-2 mt-6 text-[1.1rem] font-extrabold">Ключи для встраивания (Министерство)</h2>
      <p class="mb-3 mt-0 text-[.9rem] text-ink-3">Ключ только для чтения афиши, передаётся в заголовке X-Partner-Key. Свой лимит запросов в минуту вместо общего лимита по адресу. Сам ключ показывается один раз.</p>
      <div id="k-new" class="mb-3 grid gap-2 rounded-ctl bg-brand-tint px-4 py-3" role="status" hidden></div>
      <form id="key-form" class="mb-4 flex flex-wrap items-end gap-3">
        <label class="grid gap-1 text-[.82rem] font-bold text-ink-3"><span>Название</span><input id="k-name" required maxlength="80" class="${FIELD}"></label>
        <label class="grid gap-1 text-[.82rem] font-bold text-ink-3"><span>Запросов в минуту</span><input id="k-limit" type="number" min="1" max="6000" value="600" class="${FIELD}"></label>
        <label class="grid gap-1 text-[.82rem] font-bold text-ink-3"><span>Владелец (номер Telegram)</span><input id="k-owner" type="number" min="1" class="${FIELD}" placeholder="необязательно"></label>
        <button type="submit" class="${BTN_MAIN}">Выпустить ключ</button>
      </form>
      <div id="k-body" class="overflow-x-auto"></div>`;
    const guarded = async (action) => { try { await action(); } catch (error) { if (error.message !== 'Нужен вход') say(error.message, true); } };

    const loadUsers = () => guarded(async () => {
      const params = new URLSearchParams({ limit: '100' });
      if ($('#u-role').value) params.set('role', $('#u-role').value);
      if ($('#u-q').value.trim()) params.set('q', $('#u-q').value.trim());
      const users = await api(`/api/v1/admin/users?${params}`);
      $('#u-body').innerHTML = users.length ? `<table class="w-full border-collapse"><thead><tr><th class="${TH}">Имя</th><th class="${TH}">Telegram</th><th class="${TH}">Роль</th><th class="${TH}">Входил</th><th class="${TH}"></th></tr></thead><tbody>${users.map((u) => `<tr data-user="${Number(u.id)}" data-tg="${Number(u.telegram_id)}">
        <td class="${TD}">${esc(u.display_name || '—')}</td><td class="${TD}">${Number(u.telegram_id)}</td>
        <td class="${TD}"><select data-role class="${FIELD}">${roleOptions(u.role)}</select></td>
        <td class="${TD}">${u.signed_in ? esc(when(u.last_seen_at) || 'да') : 'роль ждёт первого входа'}</td>
        <td class="${TD}">${u.role !== 'user' ? `<button type="button" data-revoke class="${BTN}">Отозвать доступ</button>` : ''}</td></tr>`).join('')}</tbody></table>` : '<p class="text-ink-3">Никого нет. Назначьте роль выше.</p>';
    });

    const loadKeys = () => guarded(async () => {
      const keys = await api('/api/v1/admin/api-keys');
      $('#k-body').innerHTML = keys.length ? `<table class="w-full border-collapse"><thead><tr><th class="${TH}">Ключ</th><th class="${TH}">Владелец</th><th class="${TH}">Лимит/мин</th><th class="${TH}">Запросов</th><th class="${TH}">Последний</th><th class="${TH}"></th></tr></thead><tbody>${keys.map((k) => `<tr data-key="${Number(k.id)}">
        <td class="${TD}"><b>${esc(k.name)}</b><br><code>${esc(k.prefix)}…</code>${k.revoked_at ? ' · <span class="text-bad">отозван</span>' : ''}</td>
        <td class="${TD}">${k.owner_user_id ? `аккаунт №${Number(k.owner_user_id)}` : '—'}</td>
        <td class="${TD}">${k.revoked_at ? Number(k.rate_limit) : `<input data-limit type="number" min="1" max="6000" value="${Number(k.rate_limit)}" class="${FIELD} w-[100px]"> <button type="button" data-save-limit class="${BTN}">Сохранить</button>`}</td>
        <td class="${TD}">${Number(k.requests_total)}</td><td class="${TD}">${esc(when(k.last_used_at) || '—')}</td>
        <td class="${TD}">${k.revoked_at ? '' : `<button type="button" data-revoke-key class="${BTN}">Отозвать</button>`}</td></tr>`).join('')}</tbody></table>` : '<p class="text-ink-3">Ключей нет.</p>';
    });

    $('#role-form').addEventListener('submit', (submit) => {
      submit.preventDefault();
      guarded(async () => {
        await post('/api/v1/admin/users', { telegram_id: Number($('#r-id').value), role: $('#r-role').value, display_name: $('#r-name').value.trim() });
        $('#role-form').reset(); say('Роль назначена, действие записано в журнал.'); loadUsers();
      });
    });
    $('#u-find').addEventListener('click', loadUsers);
    $('#u-body').addEventListener('change', (event) => {
      const select = event.target.closest('[data-role]');
      if (!select) return;
      guarded(async () => {
        await post('/api/v1/admin/users', { telegram_id: Number(select.closest('[data-tg]').dataset.tg), role: select.value });
        say('Роль изменена.'); loadUsers();
      });
    });
    $('#u-body').addEventListener('click', (event) => {
      const button = event.target.closest('[data-revoke]');
      if (!button || !confirm('Отозвать доступ? Роль станет «житель», все входы закроются, личные ключи отзовутся.')) return;
      guarded(async () => { await post(`/api/v1/admin/users/${button.closest('[data-user]').dataset.user}/revoke`, {}); say('Доступ отозван.'); loadUsers(); loadKeys(); });
    });
    $('#key-form').addEventListener('submit', (submit) => {
      submit.preventDefault();
      guarded(async () => {
        const body = { name: $('#k-name').value.trim(), rate_limit: Number($('#k-limit').value) || 600 };
        if ($('#k-owner').value) body.owner_telegram_id = Number($('#k-owner').value);
        const created = await post('/api/v1/admin/api-keys', body);
        $('#key-form').reset(); $('#k-limit').value = '600';
        const box = $('#k-new'); box.hidden = false;
        box.innerHTML = `<b>Ключ «${esc(created.name)}»: скопируйте сейчас, потом он не покажется</b><code class="break-all rounded-field bg-surface px-3 py-2">${esc(created.key)}</code>`;
        loadKeys();
      });
    });
    $('#k-body').addEventListener('click', (event) => {
      const row = event.target.closest('[data-key]');
      if (!row) return;
      if (event.target.closest('[data-save-limit]')) {
        guarded(async () => { await patch(`/api/v1/admin/api-keys/${row.dataset.key}`, { rate_limit: Number(row.querySelector('[data-limit]').value) }); say('Лимит изменён.'); loadKeys(); });
      } else if (event.target.closest('[data-revoke-key]') && confirm('Отозвать ключ? Он перестанет работать сразу.')) {
        guarded(async () => { await api.delete(`/api/v1/admin/api-keys/${row.dataset.key}`); say('Ключ отозван.'); loadKeys(); });
      }
    });
    loadUsers(); loadKeys();
  }

  // ---------- запуск ----------
  document.querySelectorAll('[data-tab]').forEach((button) => button.addEventListener('click', () => open(button.dataset.tab)));
  $('#logout').addEventListener('click', async () => {
    if (via === 'account') { try { await api.post('/api/v1/auth/logout'); } catch (_) { /* выход всё равно показываем */ } }
    logout();
  });
  $('#login-form').addEventListener('submit', (submit) => { submit.preventDefault(); enter($('#key').value.trim()); $('#key').value = ''; });
  enter(sessionStorage.getItem(STORE) || ''); // ключ из этой вкладки или вход аккаунтом модератора
})();
