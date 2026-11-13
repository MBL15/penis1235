// Страница события: переход по ссылке источника или билетов попадает в «Мои посещения». Только если человек вошёл в кабинет и
// согласился вести историю (флаг ставит account.js); иначе скрипт ничего не делает и ничего не отправляет.
(() => {
  let enabled = false;
  try { enabled = localStorage.getItem('afisha.history') === '1'; } catch (_) { /* хранилище недоступно: история не ведётся */ }
  if (!enabled) return;
  document.addEventListener('click', (click) => {
    const link = click.target.closest && click.target.closest('a[data-visit]');
    const id = link && Number(link.dataset.event);
    if (!id) return;
    fetch('/api/v1/me/history', {
      method: 'POST', keepalive: true, headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ event_id: id, kind: link.dataset.visit === 'ticket' ? 'ticket' : 'view' }),
    }).catch(() => {});
  });
})();
