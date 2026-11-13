/*
 * Встраиваемый виджет афиши. Подключение на любом сайте, без правок его систем:
 *
 *   <div id="afisha"></div>
 *   <script src="https://ВАШ-СЕРВЕР/widget.js" data-target="#afisha" data-city="Уфа" data-limit="5"></script>
 *
 * Атрибуты (все необязательные): data-target, data-title, data-limit, data-city, data-category,
 * data-when (today|tomorrow|weekend|week), data-free="true", data-kids="true", data-height (высота блока: «400» или «60vh»;
 * длинный список прокручивается внутри), data-disclaimer (свой текст пометки
 * «неофициальный сервис»; пометка в виджете есть всегда).
 * Виджет рисуется в Shadow DOM, поэтому стили сайта ему не мешают, а он не ломает стили сайта.
 */
(function () {
  var script = document.currentScript || document.querySelector('script[src*="widget.js"]');
  if (!script) return;

  // Пометка, что афиша не официальный сервис министерства (ТЗ §13). Сайт-хозяин может заменить текст атрибутом data-disclaimer.
  var DISCLAIMER = 'Неофициальный сервис: не является сервисом Министерства культуры Республики Башкортостан. Данные из открытых источников, уточняйте у организатора.';
  var origin = new URL(script.src, location.href).origin;
  var cfg = script.dataset;
  var host = cfg.target ? document.querySelector(cfg.target) : null;
  if (!host) {
    host = document.createElement('div');
    script.parentNode.insertBefore(host, script.nextSibling);
  }
  var root = host.attachShadow({ mode: 'open' });

  var css =
    ':host{display:block;font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;color:inherit}' +
    '.w{border:1px solid rgba(127,127,127,.35);border-radius:12px;padding:14px 16px}' +
    'h2{margin:0 0 8px;font-size:1.1em}' +
    'ul{list-style:none;margin:0;padding:0}' +
    'li{padding:9px 0;border-top:1px solid rgba(127,127,127,.25)}' +
    'li:first-child{border-top:0}' +
    '.t{font-weight:600}.t a{color:inherit;text-decoration:none}.t a:hover{text-decoration:underline}' +
    '.m{opacity:.75;font-size:.92em}' +
    '.b{display:inline-block;margin-left:6px;padding:0 7px;border-radius:999px;font-size:.78em;background:rgba(200,120,0,.18)}' +
    '.b.cancelled{background:rgba(200,30,30,.18)}' +
    '.f{margin-top:8px;font-size:.88em}.f a{color:inherit}' +
    '.n{margin-top:6px;opacity:.7;font-size:.8em}' +
    '.e{opacity:.75}';
  root.innerHTML = '<style>' + css + '</style><div class="w"><h2></h2><div class="body e">Загрузка афиши…</div></div>';
  root.querySelector('h2').textContent = cfg.title || 'Афиша событий';
  // Высота: только число (пиксели) или число с px, vh, em, rem; другое значение игнорируется, чтобы атрибутом нельзя было вставить стиль
  var heightMatch = /^(\d{2,4})(px|vh|em|rem)?$/.exec(String(cfg.height || '').trim());
  if (heightMatch) {
    var box = root.querySelector('.w');
    box.style.maxHeight = heightMatch[1] + (heightMatch[2] || 'px');
    box.style.overflowY = 'auto';
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  var dayFmt = new Intl.DateTimeFormat('ru-RU', { weekday: 'short', day: 'numeric', month: 'long' });
  var timeFmt = new Intl.DateTimeFormat('ru-RU', { hour: '2-digit', minute: '2-digit' });
  function when(e) {
    var d = new Date(e.start_at);
    var end = e.end_at ? new Date(e.end_at) : null;
    if (end && end.toDateString() !== d.toDateString() && d <= new Date()) {
      return 'до ' + new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'long' }).format(end);
    }
    var has = d.getHours() !== 0 || d.getMinutes() !== 0;
    var single = dayFmt.format(d) + (has ? ', ' + timeFmt.format(d) : '');
    var last = e.series_until ? new Date(e.series_until) : null;
    if (last && last.toDateString() !== d.toDateString()) {
      // Серия показов: известна дата одного показа, остальные даты источник не сообщает.
      var until = 'показы до ' + new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'long' }).format(last);
      return d <= new Date() ? until : single + ' · ' + until;
    }
    return single;
  }

  var params = new URLSearchParams({ limit: String(Math.min(Math.max(parseInt(cfg.limit, 10) || 5, 1), 20)) });
  ['city', 'category', 'when'].forEach(function (k) { if (cfg[k]) params.set(k, cfg[k]); });
  if (cfg.free === 'true') params.set('free', 'true');
  if (cfg.kids === 'true') params.set('kids', 'true');

  fetch(origin + '/api/v1/events?' + params)
    .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
    .then(function (data) {
      var body = root.querySelector('.body');
      body.className = 'body';
      if (!data.items.length) { body.className = 'body e'; body.textContent = 'Ближайших событий не найдено.'; return; }
      body.innerHTML = '<ul>' + data.items.map(function (e) {
        var link = origin + '/event/' + (Number(e.id) || 0);  // карточка события на нашем сервисе: оттуда есть ссылка на источник
        var title = link ? '<a href="' + esc(link) + '" target="_blank" rel="noopener noreferrer">' + esc(e.title) + '</a>' : esc(e.title);
        var badge = e.status !== 'scheduled' ? '<span class="b ' + esc(e.status) + '">' + esc(e.status_label) + '</span>' : '';
        if (e.is_demo) badge += '<span class="b">демо</span>';
        var place = [e.venue, e.city].filter(Boolean).join(', ');
        return '<li><div class="t">' + title + badge + '</div><div class="m">' + esc(when(e)) +
          (place ? ' · ' + esc(place) : '') + ' · ' + esc(e.price_text) + (e.age_limit !== null && e.age_limit !== undefined ? ' · ' + Number(e.age_limit) + '+' : '') + '</div></li>';
      }).join('') + '</ul><div class="f"><a href="' + esc(origin) + '/" target="_blank" rel="noopener">Вся афиша →</a></div>' +
        '<div class="n">' + esc(cfg.disclaimer || DISCLAIMER) + '</div>';
    })
    .catch(function () {
      var body = root.querySelector('.body');
      body.className = 'body e';
      body.textContent = 'Афиша временно недоступна.';
    });
})();
