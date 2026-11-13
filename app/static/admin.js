// Интерфейс модератора. Данные приходят из /api/v1/admin/* и открываются только по ключу X-API-Key.
// Скрипт внешний (политика безопасности запрещает встроенные), все значения из базы экранируются.
(() => {
  const $ = (selector, root = document) => root.querySelector(selector);
  const esc = (value) =>
    String(value ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const STORE = 'afisha.admin.key'; // sessionStorage: ключ живёт только пока открыта вкладка
  const STATUS = { scheduled: 'Состоится', rescheduled: 'Перенесено', sold_out: 'Билеты проданы', cancelled: 'Отменено' };
  const ACTIONS = { create: 'создание', update: 'обновление', merge: 'слияние источников', status: 'статус', publication: 'публикация', edit: 'правка', delete: 'удаление', feedback: 'обращение', import: 'импорт' };
  const BTN = 'min-h-[36px] rounded-field border border-stroke bg-glass-hi px-3 text-[.9rem] font-semibold text-brand-ink hover:bg-white';
  const BTN_MAIN = 'min-h-[40px] rounded-ctl bg-brand px-4 font-bold text-white shadow-cta hover:bg-brand-hover';
  const FIELD = 'min-h-[40px] rounded-field border border-line bg-glass-field px-3 text-base font-medium text-ink focus-visible:border-brand focus-visible:outline-none';
  const TH = 'px-3 py-2 text-left text-[.76rem] font-extrabold uppercase tracking-[.08em] text-ink-3';
  const TD = 'border-t border-line px-3 py-3 align-top';

  let key = '';
  let categories = [];
  let tab = 'queue';
  const view = { offset: 0, query: '', status: 'all', publication: 'all' };
  let editing = null; // событие, которое правим; null — создаём новое

  // ---------- обмен с сервером ----------
  async function api(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      headers: { 'X-API-Key': key, ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...options.headers },
    });
    if (response.status === 401) { logout('Ключ не подошёл или устарел. Войдите снова.'); throw new Error('Нужен вход'); }
    let data = null;
    try { data = await response.json(); } catch (_) { /* тело может быть пустым */ }
    if (!response.ok) {
      const error = (data && data.error) || {};
      const details = (error.details || []).map((d) => `${d.field}: ${d.message}`).join('; ');
      throw new Error([error.message || `Ошибка ${response.status}`, details].filter(Boolean).join(' — '));
    }
    return data;
  }
  const post = (path, body) => api(path, { method: 'POST', body: JSON.stringify(body) });
  const patch = (path, body) => api(path, { method: 'PATCH', body: JSON.stringify(body) });

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
    try {
      const session = await api('/api/v1/admin/session');
      sessionStorage.setItem(STORE, key);
      $('#login').hidden = true; $('#app').hidden = false; $('#logout').hidden = false;
      $('#actor').hidden = false; $('#actor').textContent = `Вы вошли как ${session.actor}`;
      categories = await fetch('/api/v1/categories').then((r) => r.json());
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
    ({ queue: showEvents, events: showEvents, import: showImport, audit: showAudit, feedback: showFeedback }[name])();
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
        <div class="mt-1 text-[.85rem] text-ink-3">${esc(e.category_label)} · источников: ${Number(e.source_count) || 0}</div></td>
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
      <select id="f-status" class="${FIELD}"><option value="new">новые</option><option value="processed">обработанные</option><option value="all">все</option></select></label></div><div id="f-body"></div>`;
    const load = async () => {
      try {
        const items = await api(`/api/v1/admin/feedback?status=${$('#f-status').value}&limit=100`);
        $('#f-body').innerHTML = items.length ? items.map((f) => `<article class="mb-3 grid gap-2 rounded-ctl border border-line bg-white/60 p-4" data-id="${Number(f.id)}">
          <div class="flex flex-wrap items-center justify-between gap-2"><strong>${esc(f.kind_label)}</strong><span class="text-[.85rem] text-ink-3">${esc(when(f.created_at))}${f.event_id ? ` · <a class="underline" href="/event/${Number(f.event_id)}" target="_blank" rel="noopener noreferrer">событие №${Number(f.event_id)}</a>` : ''}</span></div>
          <p class="m-0 whitespace-pre-wrap [overflow-wrap:anywhere]">${esc(f.message)}</p>
          <div class="flex flex-wrap items-center justify-between gap-2 text-[.88rem] text-ink-3"><span>${f.contact ? `Контакт: ${esc(f.contact)}` : 'Контакт не указан'}${f.status === 'processed' ? ` · обработал ${esc(f.processed_by)}` : ''}</span>
            <button type="button" data-set="${f.status === 'new' ? 'processed' : 'new'}" class="${BTN}">${f.status === 'new' ? 'Отметить обработанным' : 'Вернуть в новые'}</button></div></article>`).join('')
          : '<p class="text-ink-3">Обращений нет.</p>';
      } catch (error) { if (error.message !== 'Нужен вход') say(error.message, true); }
    };
    $('#f-status').addEventListener('change', load);
    $('#f-body').addEventListener('click', async (event) => {
      const button = event.target.closest('[data-set]');
      if (!button) return;
      try { await patch(`/api/v1/admin/feedback/${button.closest('[data-id]').dataset.id}`, { status: button.dataset.set }); refreshCounts(); load(); } catch (error) { if (error.message !== 'Нужен вход') say(error.message, true); }
    });
    load();
  }

  // ---------- запуск ----------
  document.querySelectorAll('[data-tab]').forEach((button) => button.addEventListener('click', () => open(button.dataset.tab)));
  $('#logout').addEventListener('click', () => logout());
  $('#login-form').addEventListener('submit', (submit) => { submit.preventDefault(); enter($('#key').value.trim()); $('#key').value = ''; });
  const saved = sessionStorage.getItem(STORE);
  if (saved) enter(saved);
})();
