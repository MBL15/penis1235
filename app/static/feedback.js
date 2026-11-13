// Форма обратной связи. Защита от спама: серверная метка времени формы (GET /api/v1/feedback/form) и скрытое поле-ловушка.
(() => {
  const $ = (selector) => document.querySelector(selector);
  const params = new URLSearchParams(location.search);
  const eventId = /^\d{1,10}$/.test(params.get('event') || '') ? Number(params.get('event')) : null;
  let token = '';

  async function loadForm() {
    try {
      const response = await fetch('/api/v1/feedback/form', { cache: 'no-store' });
      const data = await response.json();
      token = data.token;
      if (!$('#kind').options.length) {
        $('#kind').innerHTML = data.kinds.map((k) => `<option value="${k.slug}">${k.label.replace(/[&<>"']/g, '')}</option>`).join('');
        const wanted = params.get('kind');
        $('#kind').value = data.kinds.some((k) => k.slug === wanted) ? wanted : (eventId ? 'correction' : 'other');
      }
    } catch (_) {
      problem('Не удалось открыть форму. Проверьте соединение и обновите страницу.');
    }
  }

  function problem(text) {
    const box = $('#problem');
    box.textContent = text || '';
    box.hidden = !text;
  }

  if (eventId) {
    const note = $('#event-note');
    note.textContent = `Обращение о событии №${eventId}`;
    note.hidden = false;
  }
  $('#message').addEventListener('input', () => { $('#counter').textContent = `${$('#message').value.length} / 2000`; });

  $('#form').addEventListener('submit', async (submit) => {
    submit.preventDefault();
    problem('');
    const message = $('#message').value.trim();
    if (message.length < 10) { problem('Опишите обращение подробнее: хотя бы 10 символов.'); $('#message').focus(); return; }
    const send = $('#send');
    send.disabled = true;
    try {
      const response = await fetch('/api/v1/feedback', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ kind: $('#kind').value, event_id: eventId, message, contact: $('#contact').value.trim(), token, website: $('#website').value }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        const error = data.error || {};
        const details = (error.details || []).map((d) => d.message).join('; ');
        problem([error.message || `Ошибка ${response.status}`, details].filter(Boolean).join(': '));
        await loadForm(); // метка одноразовая по смыслу: берём свежую для повторной отправки
        return;
      }
      $('#form').hidden = true;
      $('#done').hidden = false;
      $('#done-text').textContent = data.message || 'Обращение принято.';
    } catch (_) {
      problem('Не удалось отправить обращение. Проверьте соединение и попробуйте ещё раз.');
    } finally {
      send.disabled = false;
    }
  });

  loadForm();
})();
