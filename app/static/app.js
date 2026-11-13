(() => {
  const $ = (selector, root = document) => root.querySelector(selector);
  const esc = (value) =>
    String(value ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const safeUrl = (url) => (/^https?:\/\//i.test(url || '') ? url : '');
  const icon = (name, cls = '') => `<svg class="icon${cls ? ` ${cls}` : ''}" aria-hidden="true"><use href="#i-${name}"/></svg>`;

  // Стили задаются утилитами Tailwind прямо в разметке; повторяющиеся наборы вынесены в константы.
  // Tailwind ищет классы в этом файле целиком, поэтому строки нельзя собирать из кусков.
  const ARROW = 'transition-transform duration-[.25s] ease-soft group-hover/btn:translate-x-1'; // стрелка в кнопке сдвигается при наведении
  const CHIP = 'min-h-[40px] rounded-full border border-stroke bg-glass px-4 py-2 text-[.93rem] font-semibold shadow-[0_4px_14px_rgba(52,64,120,.08)] tx-chip hover:scale-[1.07] hover:bg-glass-hi hover:shadow-[0_8px_20px_rgba(52,64,120,.16)] active:scale-[.97] active:duration-[.12s]';
  const CHIP_REMOVABLE = `min-h-[36px] animate-pop-chip rounded-full border border-transparent bg-brand-tint px-[14px] py-[5px] text-[.93rem] font-bold text-brand-ink tx-chip after:ml-2 after:text-[.75em] after:content-['✕'] hover:scale-[1.07] active:scale-[.97] active:duration-[.12s]`;
  const EMPTY = 'col-span-full grid animate-settle-empty justify-items-center gap-[14px] rounded-panel border border-dashed border-[rgba(79,70,229,.4)] bg-glass-soft px-4 py-12 text-center text-ink-2';
  const CHECK = 'absolute right-3 top-3 grid h-[26px] w-[26px] scale-0 place-items-center rounded-full bg-brand text-white opacity-0 [transition:transform_.35s_cubic-bezier(.34,1.56,.64,1),opacity_.15s] tg:right-2 tg:top-2 tg:h-[22px] tg:w-[22px]';

  const CATS = {
    theatre: 'Спектакли, опера, балет', concert: 'Концерты и музыка', exhibition: 'Выставки и музеи',
    festival: 'Праздники и фестивали', kids: 'Детям и семьям', lecture: 'Лекции, встречи, мастер-классы',
    cinema: 'Кино и показы', excursion: 'Прогулки и экскурсии', sport: 'Спорт и забеги', other: 'Всё остальное',
  };
  const WHEN_LABEL = { today: 'Сегодня', tomorrow: 'Завтра', weekend: 'Выходные', week: 'Неделя' };
  const PAGE = 12; // 12 карточек = 16 «клеток» bento (1 крупная 2×2, 1 широкая 2×1, 10 обычных): сетка из 4 колонок без дыр
  const state = { when: '', categories: [], free: false, offset: 0, rendered: 0 };
  const categories = {}; // slug → название
  const reduceMotion = matchMedia('(prefers-reduced-motion: reduce)').matches;

  const plural = (n) => (n % 10 === 1 && n % 100 !== 11 ? 'событие' : [2, 3, 4].includes(n % 10) && ![12, 13, 14].includes(n % 100) ? 'события' : 'событий');

  async function api(path, options) {
    const response = await fetch(path, options);
    if (!response.ok) {
      let detail = '';
      try { const body = await response.json(); detail = body.error && typeof body.error.message === 'string' ? body.error.message : ''; } catch (_) { /* пусто */ }
      throw new Error(detail || `Ошибка ${response.status}`);
    }
    return response.json();
  }

  // ---------- движение ----------
  /** Элементы с .reveal плавно появляются, когда попадают в экран; задержка берётся из --i. */
  const observer = 'IntersectionObserver' in window
    ? new IntersectionObserver((entries) => {
      // Блоки, появившиеся в экране одновременно, выезжают по порядку: сверху вниз и слева направо.
      entries
        .filter((entry) => entry.isIntersecting)
        .sort((a, b) => (a.boundingClientRect.top - b.boundingClientRect.top) || (a.boundingClientRect.left - b.boundingClientRect.left))
        .forEach((entry, order) => {
          entry.target.style.setProperty('--i', Math.min(order, 9));
          entry.target.classList.add('in');
          observer.unobserve(entry.target);
        });
    }, { rootMargin: '0px 0px -6% 0px', threshold: 0.05 })
    : null;
  const reveal = (root = document) => root.querySelectorAll('.reveal:not(.in)').forEach((el) => (observer ? observer.observe(el) : el.classList.add('in')));

  /** Число «накатывается» от прежнего значения к новому. */
  function countTo(el, to, suffix = '') {
    const target = Number(to);
    if (!Number.isFinite(target) || reduceMotion) { el.textContent = `${to}${suffix}`; return; }
    const from = Number(el.dataset.v || 0);
    el.dataset.v = target;
    const start = performance.now();
    const step = (now) => {
      const p = Math.min(1, (now - start) / 700);
      el.textContent = `${Math.round(from + (target - from) * (1 - Math.pow(1 - p, 3)))}${suffix}`;
      if (p < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }

  // ---------- форматирование ----------
  const dayFmt = new Intl.DateTimeFormat('ru-RU', { weekday: 'short', day: 'numeric', month: 'long' });
  const shortFmt = new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'long' });
  const timeFmt = new Intl.DateTimeFormat('ru-RU', { hour: '2-digit', minute: '2-digit' });

  function whenText(event) {
    // Дата приходит без часового пояса (местное время Уфы), браузер показывает её как есть.
    const start = new Date(event.start_at);
    const end = event.end_at ? new Date(event.end_at) : null;
    if (end && end.toDateString() !== start.toDateString()) {
      return start <= new Date() ? `до ${shortFmt.format(end)}` : `${shortFmt.format(start)} – ${shortFmt.format(end)}`;
    }
    const hasTime = start.getHours() !== 0 || start.getMinutes() !== 0;
    const single = hasTime ? `${dayFmt.format(start)}, ${timeFmt.format(start)}` : dayFmt.format(start);
    // Серия показов: известна дата одного показа, остальные даты источник не сообщает.
    const last = event.series_until ? new Date(event.series_until) : null;
    if (last && last.toDateString() !== start.toDateString()) {
      const year = last.getFullYear() !== new Date().getFullYear() ? ` ${last.getFullYear()}` : '';
      const until = `показы до ${shortFmt.format(last)}${year}`;
      return start <= new Date() ? until : `${single} · ${until}`;
    }
    return single;
  }

  const bentoClass = (index) => ({ 0: 'feature', 6: 'wide' }[index % PAGE] || '');

  const BADGE = 'rounded-full px-[10px] py-[3px] text-[.76rem] font-[750]';
  const BADGE_TONE = { free: 'bg-ok-bg text-ok', cancelled: 'bg-bad-bg text-bad', rescheduled: 'bg-warn-bg text-warn', sold_out: 'bg-warn-bg text-warn', demo: 'bg-warn-bg text-warn' };
  const badge = (kind, text, attrs = '') => `<span class="${BADGE} ${BADGE_TONE[kind] || 'bg-brand-tint text-brand-ink'}"${attrs}>${text}</span>`;

  // Карточка: обычная, крупная (feature: 2×2) или широкая (wide: 2×1). В мини-приложении Telegram она становится строкой.
  const CARD = 'group/card spot relative flex min-w-0 flex-col overflow-hidden rounded-card border border-stroke bg-glass shadow-surface tx-glass hover:z-[3] hover:-translate-y-[6px] hover:scale-[1.035] hover:bg-white/80 hover:shadow-surface-2 has-[h3_a:focus-visible]:outline has-[h3_a:focus-visible]:outline-[3px] has-[h3_a:focus-visible]:outline-offset-[3px] has-[h3_a:focus-visible]:outline-[rgba(79,70,229,.55)] tg:rounded-[20px] tg:hover:translate-y-0 tg:hover:scale-100 tg:hover:shadow-surface tg:active:scale-[.985] tg:in-chat:block';
  const CARD_KIND = {
    feature: 'col-span-2 row-span-2 w600:col-auto w600:row-auto tg:col-auto tg:row-auto tg:flex tg:flex-col',
    wide: 'col-span-2 w600:col-auto tg:col-auto tg:grid tg:grid-cols-[104px_minmax(0,1fr)]',
    '': 'tg:grid tg:grid-cols-[104px_minmax(0,1fr)]',
  };
  const MEDIA = 'relative grid place-items-center overflow-hidden bg-[var(--tile-bg)] text-[var(--tile-fg)] in-chat:hidden';
  const MEDIA_KIND = {
    feature: 'h-[132px] min-h-[250px] flex-1 w600:h-[176px] w600:min-h-0 w600:flex-none tg:h-[190px] tg:min-h-0 tg:flex-none',
    wide: 'h-[156px] flex-none tg:h-auto tg:min-h-[120px]',
    '': 'h-[132px] flex-none tg:h-auto tg:min-h-[120px]',
  };
  const MEDIA_ICON = 'stroke-[1.5] opacity-[.85] transition-transform duration-500 ease-spring group-hover/card:-rotate-[6deg] group-hover/card:scale-[1.12]';
  const MEDIA_ICON_SIZE = {
    feature: 'h-[92px] w-[92px] w600:h-16 w600:w-16 tg:h-[72px] tg:w-[72px]',
    wide: 'h-[54px] w-[54px] tg:h-[34px] tg:w-[34px]',
    '': 'h-[54px] w-[54px] tg:h-[34px] tg:w-[34px]',
  };
  const TITLE = {
    feature: 'text-[1.65rem] font-extrabold leading-[1.28] tracking-[-.025em] [overflow-wrap:anywhere] w600:text-[1.3rem] tg:text-[1.3rem]',
    wide: 'text-[1.12rem] font-bold leading-[1.28] tracking-[-.015em] [overflow-wrap:anywhere] tg:text-[1.02rem]',
    '': 'text-[1.12rem] font-bold leading-[1.28] tracking-[-.015em] [overflow-wrap:anywhere] tg:text-[1.02rem]',
  };
  const PILL = 'rounded-full border px-3 py-[3px] text-[.85rem] tg:px-[10px] tg:py-[2px] tg:text-[.8rem]'; // тон добавляется отдельно
  const PILL_TONE = { plain: 'border-stroke bg-white/[.78] font-semibold text-ink-2', price: 'border-stroke bg-white/[.78] font-[750] text-brand-ink', verified: 'border-transparent bg-ok-bg font-semibold text-ok' };

  function cardHtml(event, cls = '', index = 0, animate = true) {
    const image = safeUrl(event.image_url);
    const slug = event.category in CATS ? event.category : 'other';
    const kind = cls in CARD_KIND ? cls : '';
    const cancelled = event.status === 'cancelled';
    const badges = [];
    if (event.status !== 'scheduled') badges.push(badge(event.status, esc(event.status_label)));
    if (event.is_free) badges.push(badge('free', 'Бесплатно'));
    if (event.is_demo) badges.push(badge('demo', 'демо', ' title="Выдуманное событие для демонстрации"'));
    const place = [event.venue, event.city].filter(Boolean).join(', ');
    const linkClass = `text-inherit font-[inherit] ${cancelled ? 'line-through decoration-[1.5px]' : 'no-underline'} after:absolute after:inset-0 after:z-[1] after:rounded-[inherit] after:content-[''] focus-visible:outline-none`;
    // Заголовок ведёт на карточку события с постоянной ссылкой; источник и билеты открываются уже оттуда.
    const title = `<a class="${linkClass}" href="/event/${Number(event.id) || 0}">${esc(event.title)}</a>`;
    const pills = [];
    if (!event.is_free) pills.push(`<span class="${PILL} ${PILL_TONE.price}">${esc(event.price_text)}</span>`);
    if (Number(event.age_limit) > 0) pills.push(`<span class="${PILL} ${PILL_TONE.plain}">${Number(event.age_limit)}+</span>`);
    if (event.source_count > 1) {
      pills.push(`<span class="${PILL} ${PILL_TONE.verified}" title="${esc((event.sources || []).map((s) => s.name).join(', '))}">✓ ${Number(event.source_count) || 0} источника</span>`);
    }
    return `<article class="${CARD} ${CARD_KIND[kind]} tone-${slug}${animate ? ' reveal' : ''}" style="--i:${index % 6}">
      <div class="${MEDIA} ${MEDIA_KIND[kind]}${cancelled ? ' grayscale-[.85]' : ''}">${icon(slug, `${MEDIA_ICON} ${MEDIA_ICON_SIZE[kind]}`)}${image ? `<img class="absolute inset-0 h-full w-full object-cover transition-transform duration-700 ease-soft group-hover/card:scale-[1.08]" src="${esc(image)}" alt="" loading="lazy" referrerpolicy="no-referrer">` : ''}</div>
      <div class="flex flex-none flex-col gap-[6px] px-[18px] pb-[18px] pt-4 tg:gap-1 tg:px-[14px] tg:py-3">
        <div class="flex flex-wrap items-center justify-between gap-2"><span class="text-[.74rem] font-extrabold uppercase tracking-[.08em] text-[var(--tile-fg)]">${esc(event.category_label)}</span><span>${badges.join(' ')}</span></div>
        <p class="${kind === 'feature' ? 'text-[1.1rem]' : ''} mt-[2px] flex items-center gap-[7px] font-bold text-brand-ink tg:text-[.92rem]">${icon('calendar', 'h-[17px] w-[17px]')}<span>${esc(whenText(event))}</span></p>
        <h3 class="${TITLE[kind]}">${title}</h3>
        ${place ? `<p class="m-0 flex items-start gap-[7px] text-[.93rem] text-ink-3">${icon('pin', 'mt-[3px] h-4 w-4')}<span>${esc(place)}</span></p>` : ''}
        ${pills.length ? `<p class="mt-[6px] flex flex-wrap gap-2 tg:mt-[2px]">${pills.join('')}</p>` : ''}
        ${event.description ? `<p class="m-0 line-clamp-3 text-[.93rem] text-ink-3">${esc(event.description)}</p>` : ''}
      </div>
    </article>`;
  }

  // Заглушка на время загрузки: повторяет раскладку карточки.
  const skeletonHtml = (cls) => {
    const kind = cls in CARD_KIND ? cls : '';
    const line = 'mx-[18px] mt-3 h-[14px] rounded-lg bg-[rgba(23,32,54,.08)]';
    return `<div class="relative flex min-h-[250px] min-w-0 animate-fade-in flex-col overflow-hidden rounded-card border border-stroke bg-glass shadow-surface pointer-events-none after:absolute after:inset-0 after:-translate-x-full after:animate-sweep after:bg-[linear-gradient(100deg,transparent_30%,rgba(255,255,255,.7)_50%,transparent_70%)] after:content-[''] [&:nth-child(2n)]:[animation-delay:.07s] [&:nth-child(3n)]:[animation-delay:.14s] tg:block tg:min-h-[112px] tg:rounded-[20px] ${kind === 'feature' ? 'col-span-2 row-span-2 w600:col-auto w600:row-auto tg:col-auto tg:row-auto' : kind === 'wide' ? 'col-span-2 w600:col-auto tg:col-auto' : ''}" aria-hidden="true"><div class="relative grid place-items-center overflow-hidden bg-white/50 ${MEDIA_KIND[kind]} tg:hidden"></div><div class="${line}"></div><div class="${line} w-[55%]"></div><div class="${line}"></div></div>`;
  };

  // Если картинка не загрузилась, остаётся плитка с иконкой категории.
  document.addEventListener('error', (e) => { if (e.target.tagName === 'IMG') e.target.remove(); }, true);

  // ---------- фильтры ----------
  function filterParams() {
    const params = new URLSearchParams();
    const set = (key, value) => { if (value !== '' && value !== false && value != null) params.set(key, value); };
    const date = $('#f-date').value;
    if (date) { set('date_from', date); set('date_to', date); } else { set('when', state.when); }
    set('category', state.categories.join(','));
    set('q', $('#f-q').value.trim());
    set('city', $('#f-city').value);
    set('venue', $('#f-venue').value.trim());
    set('price_max', $('#f-price').value);
    set('age', $('#f-age').value);
    if (state.free) params.set('free', 'true');
    if ($('#f-kids').checked) params.set('kids', 'true');
    if ($('#f-cancelled').checked) params.set('status', 'all');
    if ($('#f-sort').value !== 'date') params.set('sort', $('#f-sort').value);
    return params;
  }

  /** Состояние фильтров в адресной строке: можно скопировать ссылку на подборку и вернуться кнопкой «назад». */
  function writeUrl() {
    const qs = filterParams().toString();
    history.replaceState(null, '', qs ? `?${qs}` : location.pathname);
  }

  function readUrl() {
    const p = new URLSearchParams(location.search);
    state.when = WHEN_LABEL[p.get('when')] ? p.get('when') : '';
    state.categories = (p.get('category') || '').split(',').filter(Boolean);
    state.free = p.get('free') === 'true';
    $('#f-q').value = p.get('q') || '';
    $('#f-city').value = p.get('city') || '';
    $('#f-venue').value = p.get('venue') || '';
    $('#f-price').value = p.get('price_max') || '';
    $('#f-age').value = p.get('age') || '';
    $('#f-date').value = p.get('date_from') && p.get('date_from') === p.get('date_to') ? p.get('date_from') : '';
    $('#f-kids').checked = p.get('kids') === 'true';
    $('#f-cancelled').checked = p.get('status') === 'all';
    $('#f-sort').value = p.get('sort') === 'price' ? 'price' : 'date';
    if ($('#f-date').value || $('#f-price').value || $('#f-age').value || $('#f-venue').value || $('#f-kids').checked || $('#f-cancelled').checked) {
      $('#more-filters').open = true;
    }
  }

  function syncChips() {
    $('#when-chips').querySelectorAll('[data-when]').forEach((el) => el.setAttribute('aria-pressed', String(!$('#f-date').value && el.dataset.when === state.when)));
    $('#category-chips').querySelectorAll('[data-category]').forEach((el) => el.setAttribute('aria-pressed', String(state.categories.includes(el.dataset.category))));
    $('#free-card').setAttribute('aria-checked', String(state.free));
  }

  /** Выбранные условия чипами с крестиком: видно, почему показано именно это, и каждое снимается одним касанием. */
  function activeFilters() {
    const list = [];
    const add = (label, clear) => list.push({ label, clear });
    if ($('#f-date').value) add(`Дата: ${shortFmt.format(new Date($('#f-date').value))}`, () => { $('#f-date').value = ''; });
    else if (state.when) add(WHEN_LABEL[state.when], () => { state.when = ''; });
    state.categories.forEach((slug) => add(categories[slug] || slug, () => { state.categories = state.categories.filter((s) => s !== slug); }));
    if ($('#f-q').value.trim()) add(`«${$('#f-q').value.trim()}»`, () => { $('#f-q').value = ''; });
    if ($('#f-city').value) add($('#f-city').value, () => { $('#f-city').value = ''; });
    if ($('#f-venue').value.trim()) add(`Площадка: ${$('#f-venue').value.trim()}`, () => { $('#f-venue').value = ''; });
    if ($('#f-price').value) add(`до ${$('#f-price').value} ₽`, () => { $('#f-price').value = ''; });
    if ($('#f-age').value) add(`${$('#f-age').value}+ лет`, () => { $('#f-age').value = ''; });
    if (state.free) add('Бесплатно', () => { state.free = false; });
    if ($('#f-kids').checked) add('Детям и семьям', () => { $('#f-kids').checked = false; });
    if ($('#f-cancelled').checked) add('С отменёнными', () => { $('#f-cancelled').checked = false; });
    return list;
  }

  let currentActive = [];
  function renderActive() {
    currentActive = activeFilters();
    $('#active-bar').hidden = currentActive.length === 0;
    const badge = $('#tg-count');
    badge.textContent = currentActive.length;
    badge.hidden = currentActive.length === 0;
    $('#active-filters').innerHTML = currentActive
      .map((f, i) => `<button type="button" class="${CHIP_REMOVABLE}" data-i="${i}" aria-label="Убрать: ${esc(f.label)}">${esc(f.label)}</button>`)
      .join('');
  }

  function resetAll() {
    ['f-q', 'f-date', 'f-price', 'f-age', 'f-venue'].forEach((id) => { $('#' + id).value = ''; });
    $('#f-city').value = '';
    ['f-kids', 'f-cancelled'].forEach((id) => { $('#' + id).checked = false; });
    state.when = ''; state.categories = []; state.free = false;
    load();
  }

  /** Раскладка карточек категорий: первая крупная, последняя растягивается так, чтобы последний ряд был заполнен. */
  function layoutCats() {
    const cards = [...$('#category-chips').children];
    const cols = matchMedia('(max-width: 1000px)').matches ? 2 : 4;
    cards.forEach((c) => { c.style.gridColumn = ''; c.style.gridRow = ''; c.classList.toggle('big', c === cards[0]); });
    if (!cards.length) return;
    if (cols === 4) {
      cards[0].style.gridColumn = 'span 2'; cards[0].style.gridRow = 'span 2';
      if (cards[1]) cards[1].style.gridColumn = 'span 2';
      const rest = cards.slice(4);
      if (rest.length) rest[rest.length - 1].style.gridColumn = `span ${4 - ((rest.length - 1) % 4)}`;
    } else {
      cards[0].style.gridColumn = 'span 2';
      const rest = cards.slice(1);
      if (rest.length % 2 === 1) rest[rest.length - 1].style.gridColumn = 'span 2';
    }
  }
  matchMedia('(max-width: 1000px)').addEventListener('change', layoutCats);

  // ---------- каталог ----------
  function updateCta(total) {
    const cta = $('#cta');
    cta.disabled = !total;
    cta.innerHTML = total ? `Показать ${Number(total) || 0} ${plural(total)} ${icon('arrow', ARROW)}` : 'Нет событий по этим условиям';
    updateMainButton(total);
  }

  let loadSeq = 0; // номер последнего запроса: ответ на более старый уже не нужен
  async function load(append = false) {
    const seq = ++loadSeq;
    if (!append) { state.offset = 0; state.rendered = 0; }
    syncChips();
    renderActive();
    writeUrl();
    const params = filterParams();
    params.set('limit', PAGE);
    params.set('offset', state.offset);
    const results = $('#results');
    if (!append) {
      results.setAttribute('aria-busy', 'true');
      // Уже есть карточки: просто приглушаем их, пока идёт запрос; заглушки нужны только при первой загрузке.
      if (results.querySelector('article')) results.classList.add('dim');
      else results.innerHTML = Array.from({ length: PAGE }, (_, i) => skeletonHtml(bentoClass(i))).join('');
    }
    try {
      const data = await api(`/api/v1/events?${params}`);
      if (seq !== loadSeq) return; // фильтры уже поменялись, показывать этот ответ нельзя
      const html = data.items.map((event, i) => cardHtml(event, bentoClass(state.rendered + i), i)).join('');
      if (append) results.insertAdjacentHTML('beforeend', html);
      else results.innerHTML = html || `<div class="${EMPTY}"><p class="m-0 text-[1.05rem]">По этим условиям ничего не нашлось.</p>
          <button type="button" class="${CHIP}" id="empty-reset">Сбросить фильтры</button></div>`;
      reveal(results);
      state.offset += data.items.length;
      state.rendered += data.items.length;
      const countEl = $('#count');
      countEl.textContent = data.total ? `Найдено ${data.total}${state.offset < data.total ? ` · показано ${state.offset}` : ''}` : '';
      countEl.classList.remove('flash'); void countEl.offsetWidth; countEl.classList.add('flash'); // анимация играет заново
      $('#more').hidden = state.offset >= data.total;
      $('#more').textContent = `Показать ещё ${Math.min(PAGE, data.total - state.offset)}`;
      updateCta(data.total);
    } catch (error) {
      if (seq !== loadSeq) return;
      results.innerHTML = `<div class="${EMPTY}"><p class="m-0 text-[1.05rem]">Не удалось загрузить афишу: ${esc(error.message)}</p>
        <button type="button" class="${CHIP}" id="retry">Повторить</button></div>`;
      $('#more').hidden = true;
    } finally {
      if (seq === loadSeq) { results.removeAttribute('aria-busy'); results.classList.remove('dim'); }
    }
  }

  const goTo = (selector) => $(selector).scrollIntoView({ behavior: reduceMotion ? 'auto' : 'smooth', block: 'start' });

  async function loadMeta() {
    let meta;
    try { meta = await api('/api/v1/meta'); } catch (_) { return; }
    $('#demo-banner').hidden = !meta.demo_data;
    const citySelect = $('#f-city');
    meta.cities.forEach((c) => citySelect.insertAdjacentHTML('beforeend', `<option value="${esc(c.name)}">${esc(c.name)} (${Number(c.count) || 0})</option>`));
    meta.categories.forEach((c) => { categories[c.slug] = c.label; });
    const grid = $('#category-chips');
    grid.innerHTML = meta.categories
      .filter((c) => c.count > 0)
      .sort((a, b) => b.count - a.count)
      .map((c, i) => `<button type="button" class="reveal spot group/cat tone-${esc(c.slug)} relative flex min-h-[124px] min-w-0 flex-col gap-1 rounded-card border border-stroke bg-glass p-4 text-left shadow-surface tx-glass aria-pressed:border-transparent aria-pressed:bg-glass-hi aria-pressed:shadow-selected hover:z-[2] hover:-translate-y-1 hover:scale-[1.04] hover:bg-white/80 hover:shadow-surface-2 active:translate-y-0 active:scale-[1.01] active:duration-[.15s] tg:!col-auto tg:!row-auto tg:min-h-[112px] tg:w-[134px] tg:flex-none tg:snap-start tg:p-3" style="--i:${i % 5}" data-category="${esc(c.slug)}" aria-pressed="false">
          <span class="${CHECK} group-aria-pressed/cat:scale-100 group-aria-pressed/cat:opacity-100" aria-hidden="true">${icon('check', 'h-[15px] w-[15px] [stroke-width:2.8]')}</span>
          <span class="mb-auto grid h-12 w-12 flex-none place-items-center rounded-field bg-[var(--tile-bg)] text-[var(--tile-fg)] transition-transform duration-[.35s] ease-spring group-aria-pressed/cat:-rotate-[6deg] group-aria-pressed/cat:scale-[1.1] group-hover/cat:-rotate-[6deg] group-hover/cat:scale-[1.06] group-[.big]/cat:h-[60px] group-[.big]/cat:w-[60px] group-[.big]/cat:rounded-[18px] tg:h-10 tg:w-10 tg:rounded-[12px] tg:group-[.big]/cat:h-10 tg:group-[.big]/cat:w-10 tg:group-[.big]/cat:rounded-[12px]">${icon(c.slug in CATS ? c.slug : 'other', 'h-6 w-6 group-[.big]/cat:h-[30px] group-[.big]/cat:w-[30px] tg:group-[.big]/cat:h-6 tg:group-[.big]/cat:w-6')}</span>
          <span class="mt-3 flex items-baseline gap-2 text-[1.02rem] font-bold leading-[1.25] group-[.big]/cat:text-[1.6rem] group-[.big]/cat:font-extrabold group-[.big]/cat:tracking-[-.025em] w600:group-[.big]/cat:text-[1.3rem] tg:mt-2 tg:flex-wrap tg:gap-x-[6px] tg:gap-y-[2px] tg:text-[1rem] tg:tracking-normal tg:group-[.big]/cat:text-[1rem] tg:group-[.big]/cat:tracking-normal">${esc(c.label)}<span class="rounded-full bg-white/75 px-2 py-px text-[.78rem] font-bold text-ink-2">${Number(c.count) || 0}</span></span>
          <span class="text-[.88rem] leading-[1.35] text-ink-3 tg:hidden">${esc(CATS[c.slug] || '')}</span>
        </button>`).join('');
    layoutCats();
    reveal(grid);
    countTo($('#total-events'), meta.cities.reduce((sum, c) => sum + c.count, 0), ' событий');
    countTo($('#stat-cities strong'), meta.cities.length);
  }

  /** Подсказки для поля «Площадка»: самые частые площадки из справочника. */
  async function loadVenues() {
    try {
      const venues = await api('/api/v1/venues?limit=200');
      $('#venue-options').innerHTML = venues.map((v) => `<option value="${esc(v.name)}">${esc(v.city)}, событий: ${Number(v.count) || 0}</option>`).join('');
    } catch (_) { /* подсказки необязательны */ }
  }

  async function loadStats() {
    const count = (query) => api(`/api/v1/events?${query}&limit=1`).then((d) => d.total).catch(() => '–');
    const [today, weekend, free] = await Promise.all([count('when=today'), count('when=weekend'), count('free=true')]);
    countTo($('#stat-today strong'), today);
    countTo($('#stat-weekend strong'), weekend);
    countTo($('#stat-free strong'), free);
  }

  // ---------- Telegram Mini App ----------
  /** Работает только внутри Telegram (есть initData); в обычном браузере всё это просто не включается. */
  let tg = null;
  function initTelegram() {
    const app = window.Telegram && window.Telegram.WebApp;
    if (!app || !app.initData || tg) return;
    tg = app;
    document.documentElement.classList.add('tg');
    tg.ready();
    tg.expand();
    try { tg.setHeaderColor('#eef2fb'); tg.setBackgroundColor('#eef2fb'); } catch (_) { /* старые клиенты */ }
    tg.MainButton.setParams({ color: '#4f46e5', text_color: '#ffffff' });
    // Главная кнопка Telegram работает только вместе со шторкой: «Показать N событий» закрывает её и ведёт к списку.
    tg.MainButton.onClick(() => { closeSheet(); goTo('#results-section'); });
    tg.BackButton.onClick(handleBack);
    updateMainButton();
    // Ссылки на источники открываем во встроенном браузере Telegram, а не в новой вкладке.
    document.addEventListener('click', (e) => {
      const link = e.target.closest('a[href^="http"]');
      if (link && !link.href.startsWith(location.origin)) { e.preventDefault(); tg.openLink(link.href); }
    });
  }
  let lastTotal = null;
  function updateMainButton(total = lastTotal) {
    lastTotal = total;
    if (!tg) return;
    if (!sheetOpen() || total == null) { tg.MainButton.hide(); return; }
    tg.MainButton.setText(total ? `Показать ${total} ${plural(total)}` : 'Нет событий по этим условиям');
    if (total) tg.MainButton.enable(); else tg.MainButton.disable();
    tg.MainButton.show();
  }

  // Шторка фильтров (в мини-приложении выезжает снизу; в браузере это обычный блок и эти функции не используются).
  const root = document.documentElement;
  const sheetOpen = () => root.classList.contains('sheet-open');
  function openSheet() {
    if (!root.classList.contains('tg')) return;
    haptic();
    root.classList.add('sheet-open');
    if (tg) tg.BackButton.show();
    updateMainButton();
  }
  function closeSheet() {
    root.classList.remove('sheet-open');
    if (tg) tg.BackButton.hide();
    updateMainButton();
  }
  $('#open-sheet').addEventListener('click', openSheet);
  $('#sheet-close').addEventListener('click', closeSheet);
  $('#sheet-backdrop').addEventListener('click', closeSheet);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && sheetOpen()) closeSheet(); });
  const haptic = () => { if (tg && tg.HapticFeedback) tg.HapticFeedback.selectionChanged(); };
  window.addEventListener('load', initTelegram);

  // ---------- Чат-помощник с историей диалогов ----------
  // Диалоги хранятся в localStorage этого браузера (в Telegram — в хранилище мини-приложения): без входа и без сервера.
  const CHAT_KEY = 'afisha.chats.v1';
  const MAX_CHATS = 30;
  const MAX_MESSAGES = 60;
  let chats = [];
  const HEX32 = /^[0-9a-f]{32}$/;
  /** Предложенные агентом действия из сохранённой истории: только ожидаемая форма, состояние по умолчанию — «ждёт подтверждения». */
  function cleanActions(raw) {
    if (!Array.isArray(raw)) return [];
    return raw
      .filter((a) => a && typeof a === 'object' && HEX32.test(a.id || '') && typeof a.summary === 'string')
      .slice(0, 3)
      .map((a) => ({
        id: a.id,
        kind: String(a.kind || '').slice(0, 30),
        summary: a.summary.slice(0, 400),
        state: ['pending', 'done', 'failed'].includes(a.state) ? a.state : 'pending',
        result: String(a.result || '').slice(0, 300),
      }));
  }
  /** История лежит в localStorage и могла быть испорчена или подменена: берём только записи ожидаемой формы. */
  function cleanChats(raw) {
    if (!Array.isArray(raw)) return [];
    return raw
      .filter((c) => c && typeof c === 'object' && typeof c.id === 'string' && Array.isArray(c.messages))
      .slice(0, MAX_CHATS)
      .map((c) => ({
        id: c.id.slice(0, 40),
        title: String(c.title || 'Диалог').slice(0, 80),
        updated: Number(c.updated) || 0,
        messages: c.messages
          .filter((m) => m && typeof m === 'object' && (m.role === 'user' || m.role === 'assistant') && typeof m.content === 'string')
          .slice(-MAX_MESSAGES)
          .map((m) => ({
            ...m,
            events: Array.isArray(m.events) ? m.events.filter((e) => e && typeof e === 'object') : [],
            trace_id: HEX32.test(m.trace_id || '') ? m.trace_id : '',
            actions: cleanActions(m.actions),
          })),
      }));
  }
  try { chats = cleanChats(JSON.parse(localStorage.getItem(CHAT_KEY))); } catch (_) { chats = []; }
  let currentId = null;
  let pendingId = null; // id диалога, для которого ждём ответ
  const chatApp = $('#chatapp');
  const chatOpen = () => root.classList.contains('chat-open');
  const currentChat = () => chats.find((c) => c.id === currentId) || null;

  function saveChats() {
    chats.sort((a, b) => b.updated - a.updated);
    chats = chats.slice(0, MAX_CHATS);
    try { localStorage.setItem(CHAT_KEY, JSON.stringify(chats)); } catch (_) { /* хранилище недоступно или переполнено */ }
  }

  function dayGroup(ts) {
    const startOf = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate());
    const days = Math.round((startOf(new Date()) - startOf(new Date(ts))) / 864e5);
    return days <= 0 ? 'Сегодня' : days === 1 ? 'Вчера' : days < 7 ? 'На этой неделе' : 'Раньше';
  }

  // Кнопка удаления диалога видна при наведении; на сенсорных экранах всегда, а после первого нажатия просит подтверждения.
  const CHAT_DEL = 'mr-1 grid h-10 w-10 flex-none place-items-center rounded-[12px] bg-transparent text-ink-3 opacity-0 [transition:opacity_.15s,color_.15s,background_.15s] hover:text-bad group-hover/item:opacity-100 group-focus-within/item:opacity-100 touch:opacity-75 [&.confirm]:w-auto [&.confirm]:bg-bad-bg [&.confirm]:px-3 [&.confirm]:text-[.82rem] [&.confirm]:font-bold [&.confirm]:text-bad [&.confirm]:opacity-100';

  function renderList() {
    const list = $('#chat-list');
    if (!chats.length) { list.innerHTML = '<p class="m-0 px-2 py-[14px] text-[.95rem] text-ink-3">Здесь появятся ваши диалоги</p>'; return; }
    let html = '';
    let lastGroup = '';
    chats.forEach((c, i) => {
      const group = dayGroup(c.updated);
      if (group !== lastGroup) { html += `<p class="mb-[6px] mt-4 mx-2 text-[.74rem] font-extrabold uppercase tracking-[.09em] text-ink-3">${group}</p>`; lastGroup = group; }
      html += `<div class="group/item flex animate-rise-fast items-center rounded-field [transition:background_.15s] hover:bg-brand-tint ${c.id === currentId ? 'bg-brand-tint' : ''}" style="animation-delay:${Math.min(i, 8) * 30}ms">
        <button type="button" class="min-h-[44px] min-w-0 flex-1 overflow-hidden text-ellipsis whitespace-nowrap bg-transparent px-3 py-[10px] text-left text-[.96rem] ${c.id === currentId ? 'font-bold text-brand-ink' : ''}" data-id="${esc(c.id)}" title="${esc(c.title)}">${esc(c.title)}</button>
        <button type="button" class="${CHAT_DEL}" data-del="${esc(c.id)}" aria-label="Удалить диалог «${esc(c.title)}»">${icon('trash', 'h-[18px] w-[18px]')}</button>
      </div>`;
    });
    list.innerHTML = html;
  }

  const AVATAR = 'grid h-8 w-8 flex-none place-items-center rounded-[11px] border border-stroke bg-glass-hi text-brand';
  const MSG = 'flex animate-rise-msg gap-3';
  const DOT = 'h-2 w-2 animate-dot rounded-full bg-brand';
  const typingHtml = `<div class="${MSG}"><span class="${AVATAR}" aria-hidden="true">${icon('chat', 'h-[18px] w-[18px]')}</span><div class="grid min-w-0 flex-1 content-start gap-[10px]"><span class="inline-flex gap-[5px] px-[2px] py-2" role="status" aria-label="Подбираю"><span class="${DOT}"></span><span class="${DOT} [animation-delay:.15s]"></span><span class="${DOT} [animation-delay:.3s]"></span></span></div></div>`;

  function emptyHtml() {
    const starters = [...document.querySelectorAll('#ask-examples [data-q]')]
      .map((b) => `<button type="button" class="spot relative rounded-ctl border border-stroke bg-glass px-4 py-[14px] text-left text-[.95rem] font-semibold leading-[1.35] shadow-surface tx-glass hover:z-[2] hover:-translate-y-[3px] hover:scale-[1.04] hover:bg-glass-hi hover:shadow-surface-2 active:translate-y-0 active:scale-[1.01] active:duration-[.15s]" data-q="${esc(b.dataset.q)}">${esc(b.dataset.q)}</button>`).join('');
    return `<div class="my-auto grid justify-items-center gap-[10px] py-6 text-center"><span class="grid h-[58px] w-[58px] flex-none animate-pop-avatar place-items-center rounded-[19px] border border-stroke bg-brand text-white shadow-cta" aria-hidden="true">${icon('chat', 'h-7 w-7')}</span>
      <h3 class="mt-2 text-[1.8rem] font-extrabold tracking-[-.03em] w600:text-[1.5rem]">Чем помочь?</h3><p class="m-0 max-w-[40ch] text-ink-3">Подберу события: учту дату, город, бюджет и возраст, а если не хватит деталей, уточню.</p>
      <div class="mt-[14px] grid w-full max-w-[640px] grid-cols-2 gap-[10px] w600:grid-cols-1">${starters}</div></div>`;
  }

  const TRACE_LINK = 'font-semibold text-brand-ink underline-offset-2 hover:underline';
  const ACT_BTN = 'inline-flex min-h-[44px] items-center rounded-field px-[18px] text-[.95rem] font-bold tx-btn active:scale-[.97]';
  const ACT_OK = `${ACT_BTN} bg-brand text-white shadow-cta hover:bg-brand-hover`;
  const ACT_NO = `${ACT_BTN} border border-stroke bg-glass-hi hover:bg-white`;

  /** Действие, которое предложил агент: данные меняются только после нажатия «Подтвердить». */
  function actionHtml(a) {
    const body = a.state === 'pending'
      ? `<div class="flex flex-wrap gap-2"><button type="button" class="${ACT_OK}" data-act="ok" data-act-id="${esc(a.id)}">Подтвердить</button><button type="button" class="${ACT_NO}" data-act="no" data-act-id="${esc(a.id)}">Отмена</button></div>`
      : `<p class="m-0 text-[.9rem] ${a.state === 'failed' ? 'text-bad' : 'text-ink-3'}" role="status">${esc(a.result)}</p>`;
    return `<div class="grid gap-2 rounded-ctl border border-stroke bg-glass px-4 py-3"><p class="m-0 text-[.95rem] font-semibold">${esc(a.summary)}</p>${body}</div>`;
  }

  function messageHtml(m) {
    if (m.role === 'user') return `<div class="${MSG} justify-end"><div class="max-w-[86%] origin-bottom-right animate-bubble-in whitespace-pre-wrap rounded-[20px] rounded-br-[6px] bg-brand px-4 py-3 text-white shadow-[0_8px_20px_rgba(79,70,229,.25)] [overflow-wrap:anywhere]">${esc(m.content)}</div></div>`;
    // в истории могли остаться ответы старого провайдера: показываем только Gemini, остальные подписываем нейтрально
    const tag = m.mode === 'llm' ? `Подбор сделал ИИ-агент${m.provider === 'gemini' ? ' · Gemini' : ''}` : m.mode === 'rules' ? 'Режим без ИИ: разбор запроса по правилам' : '';
    const cards = m.events && m.events.length ? `<div class="grid auto-rows-auto grid-flow-dense grid-cols-[repeat(auto-fill,minmax(240px,1fr))] gap-3 tg:grid-cols-1">${m.events.map((e, i) => cardHtml(e, '', i, false)).join('')}</div>` : '';
    const trace = HEX32.test(m.trace_id || '') ? `<a class="${TRACE_LINK}" href="/trace/${m.trace_id}" target="_blank" rel="noopener">Как получен ответ</a>` : '';
    const footer = tag || trace ? `<span class="mt-[6px] block text-[.8rem] text-ink-3">${tag}${tag && trace ? ' · ' : ''}${trace}</span>` : '';
    const actions = (m.actions || []).map(actionHtml).join('');
    return `<div class="${MSG}"><span class="${AVATAR}" aria-hidden="true">${icon('chat', 'h-[18px] w-[18px]')}</span>
      <div class="grid min-w-0 flex-1 content-start gap-[10px]"><div class="whitespace-pre-wrap pt-1 [overflow-wrap:anywhere]${m.error ? ' text-bad' : ''}">${esc(m.content)}</div>${footer}${actions}${cards}</div></div>`;
  }

  function scrollThread() {
    const thread = $('#thread');
    thread.scrollTo({ top: thread.scrollHeight, behavior: reduceMotion ? 'auto' : 'smooth' });
  }

  function renderThread() {
    const chat = currentChat();
    const thread = $('#thread');
    $('#chat-title').textContent = chat ? chat.title : 'Новый чат';
    if (!chat || !chat.messages.length) { thread.innerHTML = emptyHtml(); return; }
    thread.innerHTML = chat.messages.map(messageHtml).join('') + (pendingId === chat.id ? typingHtml : '');
    scrollThread();
  }

  const openSide = () => chatApp.classList.add('side-open');
  const closeSide = () => chatApp.classList.remove('side-open');
  const isTouch = matchMedia('(pointer: coarse)').matches;

  function openChat(text) {
    root.classList.add('chat-open');
    chatApp.hidden = false;
    if (tg) tg.BackButton.show();
    if (text) currentId = null; // вопрос из шапки всегда начинает новый диалог
    else if (!currentChat()) currentId = chats[0] ? chats[0].id : null;
    closeSide();
    renderList();
    renderThread();
    if (text) sendMessage(text);
    else if (!isTouch) $('#composer-input').focus({ preventScroll: true });
  }

  function closeChat() {
    root.classList.remove('chat-open');
    chatApp.hidden = true;
    closeSide();
    if (tg && !sheetOpen()) tg.BackButton.hide();
  }

  /** Системная кнопка «Назад» (Telegram) и Esc: сначала закрываем список чатов, потом сам чат, потом шторку. */
  function handleBack() {
    if (chatOpen()) { if (chatApp.classList.contains('side-open')) closeSide(); else closeChat(); return; }
    closeSheet();
  }

  function newChat() {
    currentId = null;
    closeSide();
    renderList();
    renderThread();
    if (!isTouch) $('#composer-input').focus({ preventScroll: true });
  }

  async function sendMessage(raw) {
    const text = raw.trim();
    if (!text || pendingId) return;
    let chat = currentChat();
    if (!chat) {
      chat = { id: `${Date.now()}${Math.random().toString(36).slice(2, 6)}`, title: text.length > 44 ? `${text.slice(0, 44)}…` : text, updated: Date.now(), messages: [] };
      chats.unshift(chat);
      currentId = chat.id;
    }
    const history = chat.messages.filter((m) => !m.error).slice(-10).map(({ role, content }) => ({ role, content: String(content).slice(0, 6000) }));
    // События из прошлого ответа: на них опираются «ещё», «в другой день» и «что-нибудь похожее»
    const lastReply = [...chat.messages].reverse().find((m) => m.role === 'assistant' && !m.error);
    const context_event_ids = lastReply && Array.isArray(lastReply.events)
      ? lastReply.events.map((e) => e.id).filter(Number.isInteger).slice(0, 10) : [];
    chat.messages.push({ role: 'user', content: text, ts: Date.now() });
    chat.updated = Date.now();
    pendingId = chat.id;
    $('#send').disabled = true;
    saveChats(); renderList(); renderThread();
    try {
      const data = await api('/api/v1/ask', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: text, history, context_event_ids }),
      });
      chat.messages.push({
        role: 'assistant', content: data.reply, events: data.events, mode: data.mode, provider: data.provider, ts: Date.now(),
        trace_id: HEX32.test(data.trace_id || '') ? data.trace_id : '',
        actions: cleanActions((data.pending_actions || []).map((a) => ({ ...a, state: 'pending' }))),
      });
    } catch (error) {
      chat.messages.push({ role: 'assistant', content: `Не получилось ответить: ${error.message}. Попробуйте ещё раз.`, error: true, ts: Date.now() });
    }
    chat.messages = chat.messages.slice(-MAX_MESSAGES);
    chat.updated = Date.now();
    pendingId = null;
    $('#send').disabled = false;
    saveChats(); renderList(); renderThread();
  }

  // поле ввода: растёт по мере набора; Enter отправляет (на телефонах Enter — перенос строки)
  const composerInput = $('#composer-input');
  const autoGrow = () => { composerInput.style.height = 'auto'; composerInput.style.height = `${Math.min(composerInput.scrollHeight, 140)}px`; };
  composerInput.addEventListener('input', autoGrow);
  composerInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !isTouch) { e.preventDefault(); $('#composer').requestSubmit(); }
  });
  $('#composer').addEventListener('submit', (e) => {
    e.preventDefault();
    const text = composerInput.value;
    composerInput.value = '';
    autoGrow();
    sendMessage(text);
  });

  /** Подтверждение или отмена действия, предложенного агентом. Ответ сервера (в том числе «предложение устарело») показываем как есть. */
  async function resolveAction(id, confirm) {
    const action = (currentChat()?.messages || []).flatMap((m) => m.actions || []).find((a) => a.id === id);
    if (!action || action.state !== 'pending') return;
    action.state = 'busy';
    action.result = 'Выполняю…';
    renderThread();
    try {
      const data = await api(`/api/v1/agent/actions/${encodeURIComponent(id)}/${confirm ? 'confirm' : 'cancel'}`, { method: 'POST' });
      action.state = data.ok ? 'done' : 'failed';
      action.result = String(data.message || '');
    } catch (error) {
      action.state = 'failed';
      action.result = error.message;
    }
    saveChats();
    renderThread();
  }

  $('#thread').addEventListener('click', (e) => {
    const act = e.target.closest('[data-act-id]');
    if (act) { resolveAction(act.dataset.actId, act.dataset.act === 'ok'); return; }
    const starter = e.target.closest('[data-q]');
    if (starter) sendMessage(starter.dataset.q);
  });
  $('#chat-list').addEventListener('click', (e) => {
    const del = e.target.closest('[data-del]');
    if (del) {
      // удаление в два шага: первое нажатие просит подтверждения, второе удаляет
      if (!del.classList.contains('confirm')) {
        del.classList.add('confirm');
        del.textContent = 'Удалить?';
        setTimeout(() => { del.classList.remove('confirm'); del.innerHTML = icon('trash', 'h-[18px] w-[18px]'); }, 3000);
        return;
      }
      chats = chats.filter((c) => c.id !== del.dataset.del);
      if (currentId === del.dataset.del) currentId = null;
      saveChats(); renderList(); renderThread();
      return;
    }
    const pick = e.target.closest('[data-id]');
    if (pick) { currentId = pick.dataset.id; closeSide(); renderList(); renderThread(); }
  });
  $('#new-chat').addEventListener('click', newChat);
  $('#new-chat-top').addEventListener('click', newChat);
  $('#side-open').addEventListener('click', openSide);
  $('#side-close').addEventListener('click', closeSide);
  $('#side-backdrop').addEventListener('click', closeSide);
  $('#chat-back').addEventListener('click', closeChat);
  document.querySelectorAll('[data-open-chats]').forEach((b) => b.addEventListener('click', () => { haptic(); openChat(); }));
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && chatOpen()) handleBack(); });

  // ---------- события интерфейса ----------
  $('#ask-form').addEventListener('submit', (e) => {
    e.preventDefault();
    const input = $('#ask-input');
    const text = input.value.trim();
    if (!text) return;
    input.value = '';
    openChat(text);
  });
  $('#ask-examples').addEventListener('click', (e) => {
    const chip = e.target.closest('[data-q]');
    if (chip) openChat(chip.dataset.q);
  });
  $('#when-chips').addEventListener('click', (e) => {
    const chip = e.target.closest('[data-when]');
    if (!chip) return;
    haptic();
    state.when = chip.dataset.when;
    $('#f-date').value = '';
    load();
  });
  $('#category-chips').addEventListener('click', (e) => {
    const card = e.target.closest('[data-category]');
    if (!card) return;
    haptic();
    const slug = card.dataset.category; // клик добавляет категорию в выбор, повторный — снимает
    state.categories = state.categories.includes(slug) ? state.categories.filter((s) => s !== slug) : [...state.categories, slug];
    load();
  });
  $('#free-card').addEventListener('click', () => { haptic(); state.free = !state.free; load(); });
  $('#active-filters').addEventListener('click', (e) => {
    const chip = e.target.closest('[data-i]');
    if (!chip) return;
    currentActive[Number(chip.dataset.i)].clear();
    load();
  });
  $('#results').addEventListener('click', (e) => {
    if (e.target.closest('#empty-reset')) resetAll();
    if (e.target.closest('#retry')) load();
  });
  $('#f-date').addEventListener('change', () => { if ($('#f-date').value) state.when = ''; load(); });
  ['f-city', 'f-price', 'f-age', 'f-venue', 'f-kids', 'f-cancelled', 'f-sort'].forEach((id) => $('#' + id).addEventListener('change', () => load()));
  let timer;
  $('#f-q').addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(() => load(), 300); });
  $('#more').addEventListener('click', () => load(true));
  $('#f-reset').addEventListener('click', resetAll);
  $('#cta').addEventListener('click', () => goTo('#results-section'));

  // Плитки-счётчики в hero — быстрые фильтры.
  const quick = (apply) => () => { apply(); load(); goTo('#filters'); };
  $('#stat-today').addEventListener('click', quick(() => { state.when = 'today'; $('#f-date').value = ''; }));
  $('#stat-weekend').addEventListener('click', quick(() => { state.when = 'weekend'; $('#f-date').value = ''; }));
  $('#stat-free').addEventListener('click', quick(() => { state.free = true; }));
  $('#stat-cities').addEventListener('click', () => { goTo('#filters'); $('#f-city').focus({ preventScroll: true }); });

  // Стеклянные плитки подсвечиваются под курсором, шапка «оседает» при прокрутке.
  if (!reduceMotion) {
    document.addEventListener('pointermove', (e) => {
      if (e.pointerType === 'touch') return;
      const el = e.target.closest && e.target.closest('.spot');
      if (!el) return;
      const box = el.getBoundingClientRect();
      el.style.setProperty('--mx', `${e.clientX - box.left}px`);
      el.style.setProperty('--my', `${e.clientY - box.top}px`);
    }, { passive: true });
  }
  const navBar = $('#site-nav');
  let scrollTick = false;
  const markScrolled = () => { scrollTick = false; navBar.classList.toggle('scrolled', scrollY > 8); };
  addEventListener('scroll', () => { if (!scrollTick) { scrollTick = true; requestAnimationFrame(markScrolled); } }, { passive: true });
  markScrolled();

  reveal();
  // Страховка: то, что уже в экране, не должно остаться скрытым, даже если наблюдатель сработает с опозданием.
  setTimeout(() => document.querySelectorAll('.reveal:not(.in)').forEach((el) => {
    if (el.getBoundingClientRect().top < innerHeight) el.classList.add('in');
  }), 900);
  loadMeta().then(() => { readUrl(); load(); });
  loadStats();
  loadVenues();
})();
