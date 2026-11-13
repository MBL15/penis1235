/**
 * Tailwind CSS для фронтенда афиши. Дизайн-токены («матовое стекло», индиго, мягкие кривые анимации) живут здесь,
 * а разметка в app/static/index.html и шаблоны в app/static/app.js используют только утилитные классы.
 * Сборка: npm run build:css (готовый app/static/tailwind.css лежит в репозитории, на сервере сборка не нужна).
 */
const plugin = require('tailwindcss/plugin');

// Цвета плиток-иконок по категориям: пастель, которая читается и на стекле, и на цветном фоне. [фон плитки, цвет иконки]
const TONES = {
  theatre: ['#e6e4ff', '#4338ca'],
  concert: ['#ffe8d6', '#b94d08'],
  exhibition: ['#ffe3ee', '#be185d'],
  festival: ['#fff2c7', '#946000'],
  kids: ['#d9f6e6', '#15803d'],
  lecture: ['#dce9ff', '#1d4ed8'],
  cinema: ['#f0e2ff', '#7e22ce'],
  excursion: ['#d6f5f1', '#0f766e'],
  sport: ['#dbf0ff', '#0369a1'],
  other: ['#e8ebf4', '#475569'],
};

const EASE = 'cubic-bezier(.22,1,.36,1)'; // мягкое торможение: быстро стартует и плавно «садится»
const SPRING = 'cubic-bezier(.34,1.56,.64,1)';

/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ['./app/static/index.html', './app/static/app.js', './app/static/boot.js', './app/pages.py', './app/plan_page.py', './app/static/admin.html', './app/static/admin.js', './app/static/feedback.html', './app/static/feedback.js'],
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
        ink: { DEFAULT: '#172036', 2: '#34405f', 3: '#4a5675', 4: '#5b6684' },
        brand: { DEFAULT: '#4f46e5', hover: '#4338ca', ink: '#3730a3', tint: 'rgba(79,70,229,.13)' },
        glass: {
          DEFAULT: 'rgba(255,255,255,.64)',
          hi: 'rgba(255,255,255,.82)',
          solid: 'rgba(255,255,255,.96)',
          soft: 'rgba(255,255,255,.4)',
          field: 'rgba(255,255,255,.72)',
        },
        stroke: 'rgba(255,255,255,.85)',
        line: 'rgba(23,32,54,.12)',
        ok: { DEFAULT: '#065f46', bg: '#d1fae5' },
        warn: { DEFAULT: '#78350f', bg: '#fef3c7', line: '#fde68a' },
        bad: { DEFAULT: '#9b1c1c', bg: '#fee2e2' },
      },
      borderRadius: { card: '22px', ctl: '18px', field: '14px', panel: '28px' },
      boxShadow: {
        // Названия не совпадают с цветами темы (glass, brand): иначе shadow-glass превратился бы ещё и в цвет тени.
        surface: '0 10px 30px rgba(52,64,120,.14), inset 0 1px 0 rgba(255,255,255,.9)',
        'surface-2': '0 18px 44px rgba(52,64,120,.22), inset 0 1px 0 rgba(255,255,255,.9)',
        cta: '0 10px 24px rgba(79,70,229,.38)',
        selected: '0 0 0 2px #4f46e5, 0 8px 20px rgba(79,70,229,.2)',
        focus: '0 0 0 4px rgba(79,70,229,.13)',
      },
      // Яркий фон, сквозь который видно стекло (цвет #eef2fb задаётся отдельно: bg-[#eef2fb]).
      backgroundImage: {
        art: [
          'radial-gradient(60vmax 60vmax at 8% 0%, #ffd9c7 0%, transparent 60%)',
          'radial-gradient(55vmax 55vmax at 96% 8%, #c8bbff 0%, transparent 55%)',
          'radial-gradient(60vmax 60vmax at 82% 100%, #a9e2ff 0%, transparent 60%)',
          'radial-gradient(50vmax 50vmax at 0% 100%, #b9f1dc 0%, transparent 55%)',
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
        'rise-open': `rise .35s ${EASE}`, // раскрытые «Ещё фильтры»
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

      addBase({
        // Этот ключевой кадр используется только в составном классе .scroll-progress, а не через animate-*,
        // поэтому Tailwind сам его не добавил бы.
        '@keyframes grow': { to: { transform: 'scaleX(1)' } },
        '[hidden]': { display: 'none !important' },
        ':focus-visible': { outline: '3px solid rgba(79,70,229,.55)', outlineOffset: '2px' },
        '.chat-open body, .tg.sheet-open body': { overflow: 'hidden' },
        '.tg body': { '-webkit-tap-highlight-color': 'transparent', overscrollBehaviorY: 'none' },
        // Пользователям с «уменьшить движение» оставляем состояние без анимаций.
        '@media (prefers-reduced-motion: reduce)': {
          '*, *::before, *::after': {
            animation: 'none !important',
            transitionDuration: '.01ms !important',
            scrollBehavior: 'auto !important',
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
          background: 'radial-gradient(260px circle at var(--mx) var(--my), rgba(255,255,255,.5), transparent 70%)',
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
            background: 'linear-gradient(90deg, #4f46e5, #8b5cf6, #38bdf8)',
            transform: 'scaleX(0)',
            transformOrigin: 'left',
            animation: 'grow linear both',
            animationTimeline: 'scroll(root)',
          },
        },
        '@media (prefers-reduced-motion: reduce)': {
          '.js .reveal:not(.in)': { opacity: '1', transform: 'none' },
          '.scroll-progress::before': { display: 'none' },
        },
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
