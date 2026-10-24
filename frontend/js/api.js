// Запросы к server.py. Ответ — JSON; при ошибке бросаем ApiError с текстом от сервера.
(function () {
  "use strict";

  class ApiError extends Error {
    constructor(status, message, data) {
      super(message);
      this.status = status;
      this.data = data || {}; // весь ответ сервера: у ошибок бывают подробности (например, место ошибки в JSON)
    }
  }

  async function request(method, path, body) {
    const opts = { method, headers: {}, credentials: "same-origin" };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    let res;
    try {
      res = await fetch(path, opts);
    } catch (e) {
      throw new ApiError(0, "Сайт не отвечает: запущен ли server.py?");
    }
    let data = {};
    try {
      data = await res.json();
    } catch (e) {
      data = {};
    }
    if (!res.ok) {
      throw new ApiError(res.status, data.error || `Ошибка ${res.status}`, data);
    }
    return data;
  }

  window.Api = {
    ApiError,
    get: (path) => request("GET", path),
    post: (path, body) => request("POST", path, body || {}),
  };
})();
