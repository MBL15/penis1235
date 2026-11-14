// Единый клиент API сайта. Подключается перед скриптом страницы: <script src="/static/api.js"></script>.
//
//   const api = afishaApi.create();                    // по умолчанию: куки, JSON, понятные ошибки
//   const events = await api.get('/api/v1/events', { city: 'Уфа', limit: 12 });
//   await api.post('/api/v1/subscriptions', { city, category });   // тело — объект, JSON собирается сам
//   await api.patch('/api/v1/me', { history_enabled: true });
//   await api.delete(`/api/v1/me/searches/${id}`);
//   await api('/api/v1/meta');                         // сам api тоже функция: api(path, fetchOptions)
//
// Ошибка сервера приходит как ApiError: message — готовый текст для человека (с перечнем полей, если сервер их назвал),
// status — код ответа, data — тело ответа. Ответ 204 даёт null. Параметры create(...):
//   headers         — объект или функция, которая возвращает объект (ключ администратора меняется на ходу)
//   cache           — режим fetch, например 'no-store'
//   onUnauthorized  — вызывается при 401; может вернуть другую ошибку, её и получит вызывающий код
(() => {
  class ApiError extends Error {
    constructor(message, status, data) {
      super(message);
      this.name = 'ApiError';
      this.status = status;
      this.data = data;
    }
  }

  function messageOf(data, status) {
    const error = (data && data.error) || {};
    const details = Array.isArray(error.details) ? error.details.map((d) => `${d.field}: ${d.message}`).join('; ') : '';
    const text = typeof error.message === 'string' && error.message ? error.message : `Ошибка ${status}`;
    return [text, details].filter(Boolean).join(' — ');
  }

  /** Строка запроса из объекта или URLSearchParams; пустые значения (null, undefined, '') пропускаются. */
  function withQuery(path, query) {
    if (!query) return path;
    const params = query instanceof URLSearchParams ? query : new URLSearchParams();
    if (!(query instanceof URLSearchParams)) {
      Object.entries(query).forEach(([name, value]) => {
        if (value === null || value === undefined || value === '') return;
        (Array.isArray(value) ? value : [value]).forEach((item) => params.append(name, item));
      });
    }
    const text = params.toString();
    return text ? `${path}${path.includes('?') ? '&' : '?'}${text}` : path;
  }

  function create({ headers: baseHeaders, onUnauthorized, ...baseOptions } = {}) {
    async function call(path, { json, headers, ...options } = {}) {
      const body = json !== undefined ? JSON.stringify(json) : options.body;
      const own = typeof baseHeaders === 'function' ? baseHeaders() : baseHeaders;
      const response = await fetch(path, {
        ...baseOptions,
        ...options,
        body,
        headers: { ...(body ? { 'Content-Type': 'application/json' } : {}), ...own, ...headers },
      });
      if (response.status === 204) return null;
      const data = await response.json().catch(() => null);
      if (!response.ok) {
        const error = new ApiError(messageOf(data, response.status), response.status, data);
        throw (response.status === 401 && onUnauthorized && onUnauthorized(error)) || error;
      }
      return data;
    }

    const api = (path, options) => call(path, options);
    api.get = (path, query, options) => call(withQuery(path, query), { ...options, method: 'GET' });
    api.post = (path, json, options) => call(path, { ...options, method: 'POST', json });
    api.put = (path, json, options) => call(path, { ...options, method: 'PUT', json });
    api.patch = (path, json, options) => call(path, { ...options, method: 'PATCH', json });
    api.delete = (path, options) => call(path, { ...options, method: 'DELETE' });
    return api;
  }

  window.afishaApi = { create, ApiError, withQuery };
})();
