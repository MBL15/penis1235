/**
 * Tailwind CSS для фронтенда афиши. Дизайн-токены (стекло на кремовом полотне, три краски: крем, тёмно-синий, башкирский красный; мягкие кривые
 * анимации) живут здесь; орнаменты — в app/static/folk.css,
 * а разметка в app/static/index.html и шаблоны в app/static/app.js используют только утилитные классы.
 * Сборка: npm run build:css (готовый app/static/tailwind.css лежит в репозитории, на сервере сборка не нужна).
 *
 * Цвета — CSS-переменные (каналы «R G B»), поэтому тема переключается без пересборки и без дублирования классов:
 *   светлая — по умолчанию; тёмная — html[data-theme="dark"] или системная тёмная тема, если светлую не выбрали явно;
 *   высокий контраст — html[data-contrast="more"] или системная настройка «повышенный контраст».
 * Атрибуты на <html> ставит boot.js до отрисовки, выбор человек делает в панели «Удобство просмотра» (ui.js).
 */
const plugin = require('tailwindcss/plugin');

// Цвета плиток-иконок по категориям: пастель, которая читается и на стекле, и на цветном фоне. [фон плитки, цвет иконки]
const TONES = {
  // Две краски, как на праздничном полотенце: красная и тёмно-синяя на кремовом. Категорию различает значок, а не цвет;
  // соседние в списке категории чередуют краски. Контраст значка к плитке ≥ 5:1.
  theatre: ['#f6e3dc', '#a8261d'],
  concert: ['#e4e5ef', '#262c62'],
  exhibition: ['#f6e3dc', '#a8261d'],
  festival: ['#e4e5ef', '#262c62'],
  kids: ['#f6e3dc', '#a8261d'],
  lecture: ['#e4e5ef', '#262c62'],
  cinema: ['#f6e3dc', '#a8261d'],
  excursion: ['#e4e5ef', '#262c62'],
  sport: ['#f6e3dc', '#a8261d'],
  other: ['#e4e5ef', '#262c62'],
};

// ---- палитры тем: значения CSS-переменных ----
// Каналы цвета без rgb(): так к ним применяются прозрачности Tailwind (bg-surface/70) и rgb(var(--brand)/.3) в разметке.
const LIGHT = {
  // Светлая: кремовое полотно, тёмно-синий текст и узоры, башкирский красный для кнопок и акцентов. Других красок нет.
  page: '244 239 230', // фон страницы
  surface: '255 253 248', // «белый» слой: наведение, поле в фокусе, плашки
  ink: '30 34 78', 'ink-2': '52 57 102', 'ink-3': '78 82 122', 'ink-4': '98 100 136',
  brand: '178 38 30', 'brand-hover': '152 30 24', 'brand-ink': '150 30 24', accent: '178 38 30',
  tint: '178 38 30', 'tint-a': '.1',
  glass: '255 253 248', 'glass-a': '.7', 'glass-hi-a': '.86', 'glass-solid-a': '.97', 'glass-soft-a': '.45', 'glass-field-a': '.78',
  stroke: '255 252 245', 'stroke-a': '.9', 'line-a': '.12',
  ok: '6 95 70', 'ok-bg': '209 250 229',
  warn: '120 53 15', 'warn-bg': '254 243 199', 'warn-line': '253 230 138',
  bad: '155 28 28', 'bad-bg': '254 226 226',
  shade: '38 44 98', // тени с синевой
  scrim: '30 34 78', // затемнение под шторками
  hl: '255 253 248', 'hl-a': '.9', // светлая кромка стекла сверху
  focus: '38 44 98', 'focus-a': '.55',
  ornament: '38 44 98', // узоры: тёмно-синий (только украшение)
  'art-1': '244 239 230', 'art-2': '240 233 220', 'art-3': '244 239 230', 'art-4': '238 230 216',
  'blob-a': '178 38 30 / .06', 'blob-b': '38 44 98 / .05',
  spot: '255 253 248 / .5', // блик под курсором
  toast: '30 34 78', track: '206 200 190', 'switch-on': '22 120 70',
  'shadow-surface': '0 10px 30px rgb(38 44 98 / .1), inset 0 1px 0 rgb(255 253 248 / .9)',
  'shadow-surface-2': '0 18px 44px rgb(38 44 98 / .17), inset 0 1px 0 rgb(255 253 248 / .9)',
  'shadow-focus': '0 0 0 4px rgb(38 44 98 / .14)',
  'shadow-focus-glass': '0 0 0 4px rgb(38 44 98 / .14), 0 10px 30px rgb(38 44 98 / .1), inset 0 1px 0 rgb(255 253 248 / .9)',
};
// Тёмная: глубокий синий фон, стекло темнее фона пятен, светлый текст. Контраст основного и вторичного текста ≥ 7:1,
// белый текст на кнопках бренда ≥ 4.5:1.
const DARK = {
  // Тёмная: ночной синий фон, кремовый текст и узоры, красный для кнопок. Контраст текста ≥ 8:1, белый на красном ≥ 5:1.
  page: '18 21 44',
  surface: '40 45 84',
  ink: '244 239 230', 'ink-2': '222 216 204', 'ink-3': '190 186 200', 'ink-4': '160 158 178',
  brand: '196 52 40', 'brand-hover': '176 44 34', 'brand-ink': '255 180 164', accent: '255 180 164',
  tint: '230 96 72', 'tint-a': '.2',
  glass: '30 34 66', 'glass-a': '.74', 'glass-hi-a': '.88', 'glass-solid-a': '.97', 'glass-soft-a': '.5', 'glass-field-a': '.84',
  stroke: '236 226 206', 'stroke-a': '.09', 'line-a': '.14',
  ok: '110 231 183', 'ok-bg': '12 58 45',
  warn: '252 211 77', 'warn-bg': '66 45 10', 'warn-line': '120 85 20',
  bad: '252 165 165', 'bad-bg': '72 20 24',
  shade: '0 0 0', scrim: '0 0 0',
  hl: '236 226 206', 'hl-a': '.06',
  focus: '255 190 160', 'focus-a': '.9',
  ornament: '236 226 206', // узоры: кремовые
  'art-1': '18 21 44', 'art-2': '22 25 52', 'art-3': '18 21 44', 'art-4': '24 26 50',
  'blob-a': '196 52 40 / .1', 'blob-b': '236 226 206 / .04',
  spot: '236 226 206 / .06',
  toast: '44 50 92', track: '74 80 120', 'switch-on': '34 140 84',
  'shadow-surface': '0 10px 30px rgb(0 0 0 / .38), inset 0 1px 0 rgb(236 226 206 / .06)',
  'shadow-surface-2': '0 18px 44px rgb(0 0 0 / .52), inset 0 1px 0 rgb(236 226 206 / .08)',
  'shadow-focus': '0 0 0 4px rgb(255 190 160 / .22)',
  'shadow-focus-glass': '0 0 0 4px rgb(255 190 160 / .22), 0 10px 30px rgb(0 0 0 / .38), inset 0 1px 0 rgb(236 226 206 / .06)',
};
// Высокий контраст поверх любой темы: стекло становится сплошным, вторичный текст — как основной, рамки заметные.
const CONTRAST = {
  'ink-3': 'var(--ink-2)', 'ink-4': 'var(--ink-2)',
  'glass-a': '.97', 'glass-hi-a': '.98', 'glass-soft-a': '.95', 'glass-field-a': '.98',
  stroke: 'var(--ink)', 'stroke-a': '.32', 'line-a': '.32', 'focus-a': '1',
};
// Различение цветов (дальтонизм): пары «зелёный — красный», которые путаются при протанопии и дейтеранопии, заменены
// на синий и оранжевый из палитры Окабе — Ито, она различима при всех трёх видах. Цвет дублируют текст и значки,
// а здесь он перестаёт мешать: «бесплатно» и «отменено» больше не одного тона для глаза. Контраст текста ≥ 5:1.
const CVD_LIGHT = {
  ok: '0 90 160', 'ok-bg': '219 234 254',
  bad: '166 54 3', 'bad-bg': '255 230 213',
  warn: '122 82 0', 'warn-bg': '255 244 194', 'warn-line': '240 200 80',
  'switch-on': '0 114 178',
};
const CVD_DARK = {
  ok: '125 190 255', 'ok-bg': '10 40 75',
  bad: '255 160 110', 'bad-bg': '75 30 10',
  warn: '250 215 90', 'warn-bg': '60 45 5', 'warn-line': '120 90 20',
  'switch-on': '30 110 190', // белая галочка на дорожке ≥ 3:1
};

// Меньше прозрачности (системная настройка): только сплошное стекло.
const SOLID = { 'glass-a': '.97', 'glass-hi-a': '.98', 'glass-soft-a': '.95', 'glass-field-a': '.98' };

const vars = (map) => Object.fromEntries(Object.entries(map).map(([k, v]) => [`--${k}`, v]));
const hexRgb = (hex) => [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16));
// Плитки в тёмной теме: фон — цвет иконки на 20 % прозрачности, иконка — тот же цвет, осветлённый наполовину.
const darkTones = Object.fromEntries(Object.entries(TONES).map(([name, [, fg]]) => {
  const [r, g, b] = hexRgb(fg);
  const lift = (c) => Math.round(c + (255 - c) * 0.5);
  return [`.tone-${name}`, { '--tile-bg': `rgb(${r} ${g} ${b} / .22)`, '--tile-fg': `rgb(${lift(r)} ${lift(g)} ${lift(b)})` }];
}));
// Префикс к каждому селектору списка («a, b» → «P a, P b»), иначе правило утекло бы на всю страницу
const scoped = (prefix, rules) => Object.fromEntries(Object.entries(rules).map(([sel, body]) => [
  sel.split(',').map((one) => `${prefix} ${one.trim()}`).join(', '), body,
]));

const DARK_ON = ':root[data-theme="dark"]';
const DARK_AUTO = ':root:not([data-theme="light"])'; // внутри @media (prefers-color-scheme: dark)
const CONTRAST_ON = ':root[data-contrast="more"]';
const CONTRAST_AUTO = ':root:not([data-contrast="normal"])'; // внутри @media (prefers-contrast: more)
const MOTION_OFF = ':root[data-motion="reduce"]';
const CVD_ON = ':root[data-cvd="on"]';

const v = (name) => `rgb(var(--${name}) / <alpha-value>)`;
const EASE = 'cubic-bezier(.22,1,.36,1)'; // мягкое торможение: быстро стартует и плавно «садится»
const SPRING = 'cubic-bezier(.34,1.56,.64,1)';

// Без анимаций: системная настройка или выбор в панели. Одни и те же правила для обоих случаев.
const STILL = {
  '*, *::before, *::after': { animation: 'none !important', transitionDuration: '.01ms !important', scrollBehavior: 'auto !important' },
};
const STILL_COMPONENTS = {
  '.js .reveal:not(.in)': { opacity: '1', transform: 'none' },
  '.scroll-progress::before': { display: 'none' },
};

/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ['./app/static/index.html', './app/static/app.js', './app/static/boot.js', './app/pages.py', './app/plan_page.py', './app/cinema_page.py', './app/static/admin.html', './app/static/admin.js', './app/static/feedback.html', './app/static/feedback.js', './app/static/account.html', './app/static/account.js'],
  // Классы тонов собираются в app.js из слага категории, поэтому в тексте их целиком нет.
  safelist: [{ pattern: /^tone-/ }],
  theme: {
    // Только «до N пикселей»: интерфейс проектировался от широкого экрана к узкому.
    screens: {
      w1000: { max: '1000px' }, // планшет: сетки в 2 колонки
      w900: { max: '900px' }, // чат: история диалогов уезжает в выдвижную панель
      w760: { max: '760px' }, // фильтры в одну колонку
      w600: { max: '600px' }, // телефон
    },
    extend: {
      fontFamily: {
        sans: ['Inter', 'system-ui', '-apple-system', '"Segoe UI"', 'Roboto', '"Helvetica Neue"', 'Arial', 'sans-serif'],
      },
      colors: {
        page: v('page'),
        surface: v('surface'),
        ink: { DEFAULT: v('ink'), 2: v('ink-2'), 3: v('ink-3'), 4: v('ink-4') },
        brand: { DEFAULT: v('brand'), hover: v('brand-hover'), ink: v('brand-ink'), tint: 'rgb(var(--tint) / var(--tint-a))' },
        accent: v('accent'), // акцентный текст: в тёмной теме светлее, чем фон кнопок бренда
        glass: {
          DEFAULT: 'rgb(var(--glass) / var(--glass-a))',
          hi: 'rgb(var(--glass) / var(--glass-hi-a))',
          solid: 'rgb(var(--glass) / var(--glass-solid-a))',
          soft: 'rgb(var(--glass) / var(--glass-soft-a))',
          field: 'rgb(var(--glass) / var(--glass-field-a))',
        },
        stroke: 'rgb(var(--stroke) / var(--stroke-a))',
        line: 'rgb(var(--ink) / var(--line-a))',
        ok: { DEFAULT: v('ok'), bg: v('ok-bg') },
        warn: { DEFAULT: v('warn'), bg: v('warn-bg'), line: v('warn-line') },
        bad: { DEFAULT: v('bad'), bg: v('bad-bg') },
      },
      borderRadius: { card: '22px', ctl: '18px', field: '14px', panel: '28px' },
      boxShadow: {
        // Названия не совпадают с цветами темы (glass, brand): иначе shadow-glass превратился бы ещё и в цвет тени.
        surface: 'var(--shadow-surface)',
        'surface-2': 'var(--shadow-surface-2)',
        cta: '0 10px 24px rgb(var(--brand) / .38)',
        selected: '0 0 0 2px rgb(var(--brand)), 0 8px 20px rgb(var(--brand) / .2)',
        focus: 'var(--shadow-focus)',
        'focus-glass': 'var(--shadow-focus-glass)', // стеклянное поле в фокусе: кольцо, тень и кромка
      },
      // Яркий фон, сквозь который видно стекло (базовый цвет — bg-page).
      backgroundImage: {
        art: [
          'radial-gradient(60vmax 60vmax at 8% 0%, rgb(var(--art-1)) 0%, transparent 60%)',
          'radial-gradient(55vmax 55vmax at 96% 8%, rgb(var(--art-2)) 0%, transparent 55%)',
          'radial-gradient(60vmax 60vmax at 82% 100%, rgb(var(--art-3)) 0%, transparent 60%)',
          'radial-gradient(50vmax 50vmax at 0% 100%, rgb(var(--art-4)) 0%, transparent 55%)',
        ].join(', '),
      },
      transitionTimingFunction: { soft: EASE, spring: SPRING },
      // Анимации только на transform и opacity (их считает видеокарта) и без бесконечных петель, кроме фона и загрузки.
      keyframes: {
        settle: { from: { opacity: '0', transform: 'translate3d(0, 44px, 0) scale(.94)' } }, // выезжает снизу и встаёт на место
        rise: { from: { opacity: '0', transform: 'translate3d(0, 16px, 0)' } },
        draw: { to: { transform: 'scaleX(1)' } },
        pop: { from: { opacity: '0', transform: 'scale(.4)' }, '60%': { transform: 'scale(1.1)' } },
        appear: { from: { opacity: '0', transform: 'scale(.985)' } },
        bounce: {
          '0%, 60%, 100%': { transform: 'translateY(0)', opacity: '.5' },
          '30%': { transform: 'translateY(-5px)', opacity: '1' },
        },
        sweep: { to: { transform: 'translateX(100%)' } },
        'drift-a': { to: { transform: 'translate3d(8vmax, -6vmax, 0) scale(1.12)' } },
        'drift-b': { to: { transform: 'translate3d(-10vmax, 8vmax, 0) scale(1.1)' } },
        fade: { from: { opacity: '0' } },
        'bubble-in': { from: { opacity: '0', transform: 'translate3d(0, 12px, 0) scale(.92)' } },
      },
      animation: {
        'drift-a': `drift-a 28s ease-in-out infinite alternate`,
        'drift-b': `drift-b 34s ease-in-out infinite alternate`,
        'nav-in': `rise .8s ${EASE} backwards`,
        'rise-fast': `rise .3s ${EASE} backwards`, // строка истории чата
        'rise-msg': `rise .4s ${EASE}`, // сообщение в ленте
        'rise-soft': `rise .5s ${EASE} backwards`, // кнопка «Показать ещё», панель выбранных фильтров
        'rise-open': `rise .35s ${EASE}`, // раскрывающиеся блоки
        'rise-flash': `rise .5s ${EASE}`, // счётчик «Найдено N» при каждом обновлении
        'settle-empty': `settle .7s ${EASE} backwards`,
        'settle-chip': `settle .85s ${EASE} backwards`,
        'pop-chip': `pop .45s ${SPRING} backwards`,
        'pop-avatar': `pop .7s ${SPRING} backwards`,
        'pop-badge': `pop .35s ${SPRING}`,
        'bubble-in': `bubble-in .45s ${SPRING} backwards`,
        appear: `appear .32s ${EASE}`,
        dot: `bounce 1s ${EASE} infinite`,
        sweep: 'sweep 1.4s ease-in-out infinite',
        'fade-in': 'fade .5s ease backwards',
        draw: `draw .9s ${EASE} .55s forwards`,
      },
    },
  },
  plugins: [
    plugin(({ addBase, addComponents, addUtilities, addVariant }) => {
      // ---- состояния страницы, которые ставит JavaScript на <html> и на предков ----
      addVariant('tg', '.tg &'); // внутри Telegram: интерфейс перестроен «как приложение»
      addVariant('tg-sheet', '.tg.sheet-open &'); // в Telegram открыта шторка фильтров
      addVariant('side-open', '.side-open &'); // в чате открыт список диалогов (узкий экран)
      addVariant('in-chat', '.thread &'); // карточка события внутри ленты чата
      addVariant('touch', '@media (hover: none)');
      addVariant('big-text', ':root:is([data-text="2"], [data-text="3"]) &'); // выбран крупный текст (130 % и больше)

      // ---- темы: порядок важен — тёмная после светлой, контраст после обеих ----
      addBase({
        ':root': { ...vars(LIGHT), colorScheme: 'light' },
        [DARK_ON]: { ...vars(DARK), colorScheme: 'dark' },
        '@media (prefers-color-scheme: dark)': { [DARK_AUTO]: { ...vars(DARK), colorScheme: 'dark' } },
      });
      addBase({
        ...scoped(DARK_ON, darkTones),
        '@media (prefers-color-scheme: dark)': scoped(DARK_AUTO, darkTones),
      });
      addBase({
        '@media (prefers-reduced-transparency: reduce)': { ':root': vars(SOLID) },
      });
      addBase({
        [CONTRAST_ON]: vars(CONTRAST),
        '@media (prefers-contrast: more)': { [CONTRAST_AUTO]: vars(CONTRAST) },
        // В высоком контрасте ссылки в тексте подчёркнуты: их видно не только по цвету.
        [`${CONTRAST_ON} :is(p, li, footer) a`]: { textDecoration: 'underline' },
      });
      // Различение цветов: светлая палитра, тёмная (явная и системная) — после неё, поэтому перекрывает
      addBase({
        [CVD_ON]: vars(CVD_LIGHT),
        [`${CVD_ON}[data-theme="dark"]`]: vars(CVD_DARK),
        '@media (prefers-color-scheme: dark)': { [`${CVD_ON}:not([data-theme="light"])`]: vars(CVD_DARK) },
        // Ссылки в тексте подчёркнуты, выбранные чипы отмечены галочкой: состояние читается без различения оттенков
        [`${CVD_ON} :is(p, li, footer) a`]: { textDecoration: 'underline' },
        [`${CVD_ON} #when-chips [aria-pressed="true"]::before, ${CVD_ON} [role="radiogroup"] label:has(> input:checked)::before`]: {
          content: '"✓"', fontWeight: '900', marginRight: '.35em',
        },
      });

      addBase({
        // Этот ключевой кадр используется только в составном классе .scroll-progress, а не через animate-*,
        // поэтому Tailwind сам его не добавил бы.
        '@keyframes grow': { to: { transform: 'scaleX(1)' } },
        '[hidden]': { display: 'none !important' },
        ':focus-visible': { outline: '3px solid rgb(var(--focus) / var(--focus-a))', outlineOffset: '2px' },
        '.chat-open body, .tg.sheet-open body': { overflow: 'hidden' },
        '.tg body': { '-webkit-tap-highlight-color': 'transparent', overscrollBehaviorY: 'none' },

        // ---- размер текста: всё в интерфейсе задано в rem, поэтому растёт вместе с корнем ----
        ':root[data-text="1"]': { fontSize: '115%' },
        ':root[data-text="2"]': { fontSize: '130%' },
        ':root[data-text="3"]': { fontSize: '150%' },

        // ---- увеличенные интервалы (WCAG 1.4.12): легче читать при дислексии и слабом зрении ----
        ':root[data-spacing="wide"] body, :root[data-spacing="wide"] body *': {
          letterSpacing: '.05em !important',
          wordSpacing: '.16em !important',
        },
        ':root[data-spacing="wide"] :is(p, li, dd, blockquote)': { lineHeight: '1.75 !important' },

        // Пользователям с «уменьшить движение» оставляем состояние без анимаций.
        '@media (prefers-reduced-motion: reduce)': STILL,
        ...scoped(MOTION_OFF, STILL),

        // Режим высокой контрастности Windows: фон кнопок браузер перекрашивает, поэтому выбранное отмечаем рамкой.
        '@media (forced-colors: active)': {
          '[aria-pressed="true"], [aria-checked="true"], [aria-selected="true"], label:has(> input:checked)': {
            outline: '3px solid Highlight',
            outlineOffset: '2px',
          },
        },
      });

      // ---- цвета плиток категорий: tone-theatre, tone-concert, ... задают --tile-bg и --tile-fg ----
      addUtilities(
        Object.fromEntries(Object.entries(TONES).map(([name, [bg, fg]]) => [`.tone-${name}`, { '--tile-bg': bg, '--tile-fg': fg }])),
      );

      addComponents({
        // Линейная иконка из спрайта: размер по умолчанию 1.25em, переопределяется обычными w-*/h-*.
        '.icon': {
          width: '1.25em',
          height: '1.25em',
          flex: 'none',
          fill: 'none',
          stroke: 'currentColor',
          strokeWidth: '1.8',
          strokeLinecap: 'round',
          strokeLinejoin: 'round',
          display: 'inline',
          verticalAlign: 'baseline',
        },

        // Плавное появление при прокрутке: app.js ставит .in, когда блок попал в экран, а порядок задаёт --i.
        '.js .reveal:not(.in)': { opacity: '0', transform: 'translate3d(0, 44px, 0) scale(.94)', willChange: 'transform, opacity' },
        '.js .reveal.in': { animation: `settle 1s ${EASE} backwards`, animationDelay: 'calc(var(--i, 0) * 80ms)' },
        // Чипы в группах встают по одному.
        '.js .reveal.in .stagger': { animation: `settle .85s ${EASE} backwards`, animationDelay: 'calc(var(--n, 0) * 70ms + .2s)' },

        // Подсветка стекла под курсором: координаты --mx/--my задаёт app.js.
        '.spot': { '--mx': '50%', '--my': '0%' },
        '.spot::before': {
          content: '""',
          position: 'absolute',
          inset: '0',
          zIndex: '1',
          borderRadius: 'inherit',
          pointerEvents: 'none',
          opacity: '0',
          background: 'radial-gradient(260px circle at var(--mx) var(--my), rgb(var(--spot)), transparent 70%)',
          transition: `opacity .5s ${EASE}`,
        },
        '@media (hover: hover)': { '.spot:hover::before': { opacity: '1' } },

        // Блик на главных кнопках при наведении.
        '.shine': { position: 'relative', overflow: 'hidden' },
        '.shine::after': {
          content: '""',
          position: 'absolute',
          inset: '0',
          pointerEvents: 'none',
          background: 'linear-gradient(105deg, transparent 35%, rgba(255,255,255,.38) 50%, transparent 65%)',
          transform: 'translateX(-130%)',
        },
        '.shine:hover::after': { transform: 'translateX(130%)', transition: `transform .9s ${EASE}` },

        // Полоса прогресса прокрутки: без JS, только там, где браузер умеет связывать анимацию с прокруткой.
        '@supports (animation-timeline: scroll())': {
          '.scroll-progress::before': {
            content: '""',
            position: 'fixed',
            top: '0',
            left: '0',
            right: '0',
            zIndex: '100',
            height: '3px',
            background: 'linear-gradient(90deg, rgb(var(--brand)), rgb(var(--brand)))', // полоса прочитанного: цвета вышивки
            transform: 'scaleX(0)',
            transformOrigin: 'left',
            animation: 'grow linear both',
            animationTimeline: 'scroll(root)',
          },
        },
        '@media (prefers-reduced-motion: reduce)': STILL_COMPONENTS,
        // Класс .js стоит на самом <html>, поэтому здесь без него; специфичность всё равно выше, чем у «.js .reveal:not(.in)»
        [`${MOTION_OFF} .reveal:not(.in)`]: { opacity: '1', transform: 'none' },
        [`${MOTION_OFF} .scroll-progress::before`]: { display: 'none' },
      });

      // ---- составные переходы: у каждого свойства своя длительность, одним утилитным классом ----
      const zoom = `.6s ${EASE}`;
      addUtilities({
        '.tx-glass': { transition: `transform ${zoom}, box-shadow ${zoom}, background .35s` }, // карточки, плитки, подсказки
        '.tx-btn': { transition: `transform .45s ${EASE}, box-shadow .45s ${EASE}, background .3s` },
        '.tx-chip': { transition: `transform .45s ${EASE}, background .3s, color .3s, box-shadow .45s ${EASE}` },
        '.tx-icon': { transition: `transform .45s ${EASE}, background .3s` },
        '.tx-send': { transition: `transform .45s ${EASE}, opacity .2s, background .3s` },
        '.tx-focus': { transition: 'border-color .2s, box-shadow .2s' },
        '.tx-field': { transition: 'border-color .2s, box-shadow .2s, background .2s' },
        '.tx-link': { transition: 'background .2s, color .2s' },
      });
    }),
  ],
};
