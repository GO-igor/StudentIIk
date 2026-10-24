// Страница администратора: вход по коду из окна server.py, аккаунты, записи студентов и старост, справочники.
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const KIND = { note: "Заметка", reminder: "Напоминание", homework: "Д/З", announcement: "Объявление" };
  const FILE_HINTS = {
    "university.json": "Справка о ТПУ и ИШИТР. Поле со значением null — ещё не внесли, Ишка так и скажет. Правки видны со следующего вопроса.",
    "events.json": "Мероприятия и хакатоны. Прошедшие Ишка не показывает, удалять их не обязательно. Правки видны сразу.",
    "session.json": "Даты сессии и экзаменов. subject — код предмета, дата экзамена null — ещё не объявили. Правки видны сразу.",
    "blocklist.json": "Запретные фразы: вопрос с такой фразой Ишка не обсуждает. Правки видны со следующего вопроса.",
  };
  let state = null;

  // ---------- мелочи ----------
  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  }

  function cell(text, label, cls) { // на телефоне таблица — карточки, у значения своя подпись
    const td = el("td", cls, text);
    if (label) td.dataset.label = label;
    return td;
  }

  function when(s) { // "2026-10-24 08:30" -> "24.10.2026 08:30"
    const m = /^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}:\d{2}))?/.exec(s || "");
    return m ? `${m[3]}.${m[2]}.${m[1]}${m[4] ? " " + m[4] : ""}` : s || "—";
  }

  function plural(n, one, few, many) {
    const d = n % 10, h = n % 100;
    return d === 1 && h !== 11 ? one : d >= 2 && d <= 4 && (h < 12 || h > 14) ? few : many;
  }

  let toastTimer = null;
  function toast(text) {
    const t = $("toast");
    t.textContent = text;
    t.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.remove("show"), 2800);
  }

  function showError(id, text) {
    $(id).textContent = text || "";
    $(id).hidden = !text;
  }

  // запрос к API; вход администратора истёк (сайт перезапустили) — снова экран с кодом
  async function call(method, path, body) {
    try {
      return method === "GET" ? await Api.get(path) : await Api.post(path, body);
    } catch (e) {
      if (e.status === 401) {
        closeModals();
        showLock("Вход администратора закончился: открой ссылку из окна server.py ещё раз.");
      }
      throw e;
    }
  }

  // ---------- окна ----------
  let opener = null;
  function openModal(id, focus) {
    opener = document.activeElement;
    $(id).classList.add("show");
    const f = focus ? $(focus) : $(id).querySelector("input:not([type=checkbox]):not([readonly]), button.btn-primary");
    if (f) setTimeout(() => f.focus(), 30);
  }
  function closeModal(id) {
    $(id).classList.remove("show");
    if (opener && document.contains(opener)) opener.focus();
  }
  function closeModals() {
    document.querySelectorAll(".modal-back.show").forEach((m) => m.classList.remove("show"));
  }
  document.querySelectorAll(".modal-back").forEach((m) => {
    m.addEventListener("click", (e) => { if (e.target === m || e.target.closest("[data-close]")) closeModal(m.id); });
  });
  document.addEventListener("keydown", (e) => {
    const open = document.querySelector(".modal-back.show");
    if (e.key === "Escape" && open) closeModal(open.id);
  });

  let confirmAction = null;
  function ask(title, text, button, action) {
    $("confirm-title").textContent = title;
    $("confirm-text").textContent = text;
    $("confirm-ok").textContent = button;
    confirmAction = action;
    openModal("confirm-modal", "confirm-ok");
  }
  $("confirm-ok").addEventListener("click", async () => {
    const btn = $("confirm-ok");
    btn.disabled = true;
    try {
      await confirmAction();
      closeModal("confirm-modal");
    } catch (e) {
      if (e.status !== 401) {
        closeModal("confirm-modal");
        toast(e.message);
      }
    } finally {
      btn.disabled = false;
    }
  });

  function showPassword(title, text, password) {
    $("done-title").textContent = title;
    $("done-text").textContent = text;
    $("done-pass").textContent = password;
    openModal("done-modal", "done-copy");
  }
  $("done-copy").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText($("done-pass").textContent);
      toast("Пароль скопирован");
    } catch (e) { // http://IP:порт — не «безопасный» адрес, буфер обмена недоступен: выделяем, чтобы скопировать руками
      const r = document.createRange();
      r.selectNodeContents($("done-pass"));
      getSelection().removeAllRanges();
      getSelection().addRange(r);
      toast("Выделил пароль: скопируй его (Ctrl+C)");
    }
  });

  // ---------- вход по коду ----------
  function showLock(error) {
    $("loading").hidden = true;
    $("panel").hidden = true;
    $("logout").hidden = true;
    $("lock").hidden = false;
    showError("lock-error", error);
    $("code").focus();
  }

  async function enter(code) {
    await Api.post("/api/admin/enter", { code });
    $("code").value = "";
    await load();
  }

  $("lock-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    let code = $("code").value.trim();
    try { // вставили ссылку целиком
      const u = new URL(code);
      code = u.searchParams.get("code") || code;
    } catch (err) { /* просто код */ }
    if (!code) return showError("lock-error", "Вставь ссылку из окна, где запущен server.py.");
    $("lock-submit").disabled = true;
    try {
      await enter(code);
    } catch (err) {
      showError("lock-error", err.message);
      $("code").select();
    } finally {
      $("lock-submit").disabled = false;
    }
  });

  const mascot = $("mascot");
  mascot.addEventListener("click", () => {
    mascot.classList.remove("jump");
    void mascot.offsetWidth;
    mascot.classList.add("jump");
  });
  mascot.addEventListener("animationend", (e) => { if (e.target.classList.contains("mascot-svg")) mascot.classList.remove("jump"); });

  $("logout").addEventListener("click", async () => {
    if (!leaveFile()) return;
    try { await Api.post("/api/admin/logout"); } catch (e) { /* всё равно на экран входа */ }
    showLock();
  });

  // ---------- общее состояние ----------
  async function load() {
    state = await call("GET", "/api/admin/state");
    $("loading").hidden = true;
    $("lock").hidden = true;
    $("panel").hidden = false;
    $("logout").hidden = false;
    renderAccounts();
    fillSelects();
    renderFileList();
    selectTab(tabFromHash(), false);
  }

  async function reload() {
    state = await call("GET", "/api/admin/state");
    renderAccounts();
    fillSelects();
  }

  function fillSelects() {
    const keep = (sel, items, first) => {
      const old = sel.value;
      sel.replaceChildren(new Option(first, ""), ...items.map(([v, t]) => new Option(t, v)));
      sel.value = items.some(([v]) => v === old) ? old : "";
    };
    const groups = state.groups.map((g) => [g.name, g.name]);
    keep($("acc-group"), groups, "Все группы");
    keep($("posts-group"), groups, "Все группы");
    keep($("notes-login"), state.accounts.map((a) => [a.login, `${a.login}${a.name ? " — " + a.name : ""} (${a.grp})`]), "Выбери аккаунт");
    $("groups").replaceChildren(...state.groups.map((g) => new Option(g.name)));
    $("f-english").replaceChildren(new Option("не указан", ""), ...state.teachers.map((t) => new Option(t)));
    $("count-accounts").textContent = state.accounts.length;
    $("db-path").textContent = state.db;
  }

  // ---------- вкладки ----------
  const TABS = ["accounts", "records", "files"];
  function tabFromHash() {
    const t = location.hash.slice(1);
    return TABS.includes(t) ? t : "accounts";
  }
  function selectTab(name, focus) {
    TABS.forEach((t) => {
      const on = t === name;
      $("t-" + t).setAttribute("aria-selected", String(on));
      $("t-" + t).tabIndex = on ? 0 : -1;
      $("p-" + t).hidden = !on;
    });
    if (focus) $("t-" + name).focus();
    history.replaceState(null, "", "#" + name);
    if (name === "records") {
      loadPosts();
      if ($("notes-login").value) loadNotes();
    }
    if (name === "files" && !file.name && state.files.length) openFile(state.files[0].name);
  }
  document.querySelectorAll(".ad-tab").forEach((b) => b.addEventListener("click", () => selectTab(b.dataset.tab)));
  $("panel").querySelector(".ad-tabs").addEventListener("keydown", (e) => {
    const i = TABS.indexOf(tabFromHash());
    if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
      e.preventDefault();
      selectTab(TABS[(i + (e.key === "ArrowRight" ? 1 : TABS.length - 1)) % TABS.length], true);
    }
  });

  // ---------- аккаунты ----------
  function badge(text, cls) { return el("span", "ad-badge " + cls, text); }

  function iconButton(label, cls, path, onClick) {
    const b = el("button", "ad-icon " + cls);
    b.type = "button";
    b.title = label;
    b.setAttribute("aria-label", label);
    b.innerHTML = `<svg viewBox="0 0 9 9" shape-rendering="crispEdges" aria-hidden="true"><path d="${path}"/></svg>`;
    b.addEventListener("click", onClick);
    return b;
  }
  const ICON_EDIT = "M6 0h1v1H6zM7 1h1v1H7zM8 2h1v1H8zM5 1h1v1H5zM4 2h1v1H4zM3 3h1v1H3zM2 4h1v1H2zM1 5h1v1H1zM6 2h1v1H6zM7 3h1v1H7zM5 4h1v1H5zM4 5h1v1H4zM3 6h1v1H3zM0 6h1v3H0zM1 8h2v1H1zM2 7h1v1H2z";
  const ICON_KEY = "M1 1h3v1H1zM0 2h1v2H0zM4 2h1v2H4zM1 4h3v1H1zM5 3h4v1H5zM7 4h1v2H7zM5 4h1v1H5zM2 2h1v1H2z";
  const ICON_TRASH = "M3 0h3v1H3zM0 1h9v1H0zM1 2h1v7H1zM7 2h1v7H7zM2 8h5v1H2zM3 3h1v4H3zM5 3h1v4H5z";

  function renderAccounts() {
    const q = $("acc-search").value.trim().toLowerCase().replace(/ё/g, "е");
    const g = $("acc-group").value;
    const rows = state.accounts.filter((a) =>
      (!g || a.grp === g) && (!q || `${a.login} ${a.name || ""}`.toLowerCase().replace(/ё/g, "е").includes(q)));
    $("acc-rows").replaceChildren(...rows.map((a) => {
      const tr = el("tr");
      const login = el("td", "ad-login");
      login.append(el("b", null, a.login));
      if (!a.can_login) login.append(badge("вход закрыт", "off"));
      if (a.telegram) login.append(badge("Telegram", "tg"));
      const role = cell(null, "Роль");
      role.append(a.role === "starosta" ? badge("староста", "star") : document.createTextNode("студент"));
      const notes = cell(null, "Записей", "num");
      if (a.notes) {
        const link = el("button", "ad-link", String(a.notes));
        link.type = "button";
        link.title = "Открыть записи";
        link.addEventListener("click", () => { $("notes-login").value = a.login; selectTab("records"); });
        notes.append(link);
      } else {
        notes.textContent = "0";
      }
      const english = cell(a.english_teacher || "—", "Английский", "ad-nowrap");
      const acts = el("td", "ad-acts");
      acts.append(
        iconButton(`Изменить ${a.login}`, "", ICON_EDIT, () => editAccount(a)),
        iconButton(`Новый пароль для ${a.login}`, "", ICON_KEY, () => newPassword(a)),
        iconButton(`Удалить ${a.login}`, "danger", ICON_TRASH, () => removeAccount(a)));
      tr.append(login, cell(a.name || "—", "Имя"), cell(a.grp, "Группа"), role,
        cell(a.subgroup || "—", "Подгруппа", "num"), english, notes, acts);
      return tr;
    }));
    const empty = $("acc-empty");
    empty.hidden = rows.length > 0;
    empty.textContent = state.accounts.length ? "Никого не нашлось." : "Аккаунтов пока нет. Нажми «Добавить» или загрузи таблицу группы.";
    $("acc-rows").closest(".ad-table-wrap").hidden = !rows.length;
  }
  $("acc-search").addEventListener("input", renderAccounts);
  $("acc-group").addEventListener("change", renderAccounts);

  let editing = null; // логин аккаунта в окне правки; null — новый
  function accountForm(a) {
    editing = a ? a.login : null;
    $("acc-title").textContent = a ? `Аккаунт ${a.login}` : "Новый аккаунт";
    $("acc-ok").textContent = a ? "Сохранить" : "Добавить";
    $("f-login").value = a ? a.login : "";
    $("f-login").readOnly = !!a;
    $("f-name").value = a ? a.name || "" : "";
    $("f-group").value = a ? a.grp : $("acc-group").value || (state.groups.length === 1 ? state.groups[0].name : "");
    $("f-subgroup").value = a && a.subgroup ? String(a.subgroup) : "";
    $("f-role").value = a ? a.role : "student";
    $("f-english").value = a ? a.english_teacher || "" : "";
    $("f-adaptation").checked = !!(a && a.adaptation);
    $("f-password").value = "";
    $("f-password-row").hidden = !!a;
    showError("acc-error", "");
    openModal("acc-modal", a ? "f-name" : "f-login");
  }
  function editAccount(a) { accountForm(a); }
  $("acc-add").addEventListener("click", () => accountForm(null));

  $("acc-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = {
      login: $("f-login").value.trim(), name: $("f-name").value.trim(), group: $("f-group").value.trim(),
      subgroup: $("f-subgroup").value, role: $("f-role").value, english: $("f-english").value,
      adaptation: $("f-adaptation").checked,
    };
    if (!body.login) return showError("acc-error", "Введи логин.");
    if (!body.group) return showError("acc-error", "Укажи группу, например 8К51.");
    if (!editing) body.password = $("f-password").value;
    $("acc-ok").disabled = true;
    try {
      const out = await call("POST", editing ? "/api/admin/account/edit" : "/api/admin/account/add", body);
      closeModal("acc-modal");
      await reload();
      if (out.password) {
        showPassword(`Аккаунт ${out.login} готов`, "Передай студенту логин и пароль:", out.password);
      } else {
        toast(editing ? "Сохранено" : `Аккаунт ${out.login} добавлен`);
      }
    } catch (err) {
      if (err.status !== 401) showError("acc-error", err.message);
    } finally {
      $("acc-ok").disabled = false;
    }
  });

  let pwLogin = null;
  function newPassword(a) {
    pwLogin = a.login;
    $("pw-title").textContent = `Новый пароль для ${a.login}`;
    $("pw-input").value = "";
    showError("pw-error", "");
    openModal("pw-modal", "pw-input");
  }
  $("pw-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    $("pw-ok").disabled = true;
    try {
      const out = await call("POST", "/api/admin/account/password", { login: pwLogin, password: $("pw-input").value });
      closeModal("pw-modal");
      await reload();
      const tg = out.telegram_off ? " Telegram этого аккаунта отключён: снова подключиться — /login логин пароль боту." : "";
      if (out.password) showPassword(`Новый пароль для ${out.login}`, "Передай его студенту." + tg, out.password);
      else toast("Пароль изменён." + tg);
    } catch (err) {
      if (err.status !== 401) showError("pw-error", err.message);
    } finally {
      $("pw-ok").disabled = false;
    }
  });

  function removeAccount(a) {
    const n = a.notes ? ` и ${a.notes} ${plural(a.notes, "запись", "записи", "записей")}` : "";
    ask(`Удалить ${a.login}?`, `Аккаунт${n} удалятся насовсем, вход на сайте сбросится. Вернуть нельзя.`, "Удалить",
      async () => {
        await call("POST", "/api/admin/account/remove", { login: a.login });
        await reload();
        toast(`Аккаунт ${a.login} удалён`);
      });
  }

  // ---------- аккаунты из таблицы ----------
  let imported = [];
  function importForm() {
    $("imp-text").value = "";
    $("imp-file").value = "";
    $("imp-input").hidden = false;
    $("imp-result").hidden = true;
    $("imp-ok").hidden = false;
    $("imp-download").hidden = true;
    $("imp-cancel").textContent = "Отмена";
    showError("imp-error", "");
    openModal("imp-modal", "imp-text");
  }
  $("acc-import").addEventListener("click", importForm);
  $("imp-file").addEventListener("change", async () => {
    const f = $("imp-file").files[0];
    if (!f) return;
    if (f.size > 512 * 1024) return showError("imp-error", "Файл слишком большой: таблица группы весит несколько килобайт.");
    const buf = await f.arrayBuffer();
    try {
      $("imp-text").value = new TextDecoder("utf-8", { fatal: true }).decode(buf).replace(/^﻿/, "");
      showError("imp-error", "");
    } catch (e) {
      showError("imp-error", "Таблица не в UTF-8: сохрани её как «CSV UTF-8» (Excel) или «CSV, Юникод (UTF-8)».");
    }
  });
  $("imp-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const text = $("imp-text").value;
    if (!text.trim()) return showError("imp-error", "Выбери файл или вставь таблицу.");
    $("imp-ok").disabled = true;
    try {
      const out = await call("POST", "/api/admin/account/import", { csv: text });
      showError("imp-error", "");
      imported = out.added;
      $("imp-summary").textContent = `Добавлено: ${out.added.length}` + (out.skipped.length ? `, пропущено строк: ${out.skipped.length}` : "") +
        (out.added.some((a) => a.password) ? ". Пароли показываются только сейчас — скачай их." : ".");
      $("imp-added").replaceChildren(...out.added.map((a) => {
        const tr = el("tr");
        tr.append(el("td", null, a.login), el("td", "ad-mono", a.password || "из таблицы"));
        return tr;
      }));
      $("imp-added-wrap").hidden = !out.added.length;
      $("imp-skipped").replaceChildren(...out.skipped.map((s) => el("li", null, `Строка ${s.line}${s.login ? " (" + s.login + ")" : ""}: ${s.why}`)));
      $("imp-input").hidden = true;
      $("imp-result").hidden = false;
      $("imp-ok").hidden = true;
      $("imp-download").hidden = !out.added.some((a) => a.password);
      $("imp-cancel").textContent = "Закрыть";
      await reload();
    } catch (err) {
      if (err.status !== 401) showError("imp-error", err.message);
    } finally {
      $("imp-ok").disabled = false;
    }
  });
  $("imp-download").addEventListener("click", () => {
    const rows = imported.filter((a) => a.password).map((a) => `${a.login},${a.password}`);
    const blob = new Blob(["﻿login,password\r\n" + rows.join("\r\n") + "\r\n"], { type: "text/csv;charset=utf-8" });
    const a = el("a");
    a.href = URL.createObjectURL(blob);
    a.download = "passwords.csv";
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  });

  // ---------- записи ----------
  function checkTable(rowsId, allId, deleteId) {
    const boxes = () => [...$(rowsId).querySelectorAll("input[type=checkbox]")];
    const sync = () => {
      const on = boxes().filter((b) => b.checked).length;
      $(deleteId).disabled = !on;
      $(deleteId).textContent = on ? `Удалить выбранные (${on})` : "Удалить выбранные";
      $(allId).checked = on > 0 && on === boxes().length;
      $(allId).indeterminate = on > 0 && on < boxes().length;
    };
    $(rowsId).addEventListener("change", sync);
    $(allId).addEventListener("change", () => { boxes().forEach((b) => { b.checked = $(allId).checked; }); sync(); });
    return { sync, ids: () => boxes().filter((b) => b.checked).map((b) => Number(b.value)) };
  }
  const notesCheck = checkTable("notes-rows", "notes-all", "notes-delete");
  const postsCheck = checkTable("posts-rows", "posts-all", "posts-delete");

  function checkCell(id, label) {
    const td = el("td", "cb");
    const cb = el("input");
    cb.type = "checkbox";
    cb.value = id;
    cb.setAttribute("aria-label", label);
    td.append(cb);
    return td;
  }

  async function loadNotes() {
    const login = $("notes-login").value;
    $("notes-rows").replaceChildren();
    $("notes-wrap").hidden = true;
    if (!login) {
      $("notes-empty").hidden = false;
      $("notes-empty").textContent = "Выбери аккаунт, чтобы увидеть его заметки и напоминания.";
      return notesCheck.sync();
    }
    let out;
    try {
      out = await call("GET", "/api/admin/notes?login=" + encodeURIComponent(login));
    } catch (e) {
      if (e.status !== 401) toast(e.message);
      return;
    }
    if (out.login !== $("notes-login").value) return; // пока ждали, выбрали другого
    $("notes-rows").replaceChildren(...out.notes.map((n) => {
      const tr = el("tr");
      const at = n.kind === "homework" ? `${n.subject}: к паре ${when(n.class_at)}` : n.fire_at ? when(n.fire_at) : "—";
      tr.className = "has-cb";
      tr.append(checkCell(n.id, n.text), cell(KIND[n.kind], null, "ad-kind"), cell(n.text, null, "ad-text"), cell(at, "Когда", "ad-nowrap"),
        cell(when(n.created), "Записано", "ad-nowrap"));
      return tr;
    }));
    $("notes-wrap").hidden = !out.notes.length;
    $("notes-empty").hidden = !!out.notes.length;
    $("notes-empty").textContent = "У этого аккаунта нет заметок и напоминаний.";
    notesCheck.sync();
  }
  $("notes-login").addEventListener("change", loadNotes);
  $("notes-delete").addEventListener("click", () => {
    const ids = notesCheck.ids(), login = $("notes-login").value;
    ask(`Удалить ${ids.length} ${plural(ids.length, "запись", "записи", "записей")}?`,
      `Заметки и напоминания ${login} удалятся насовсем.`, "Удалить", async () => {
        const out = await call("POST", "/api/admin/notes/delete", { login, ids });
        await Promise.all([loadNotes(), reload()]);
        toast(`Удалено: ${out.deleted}`);
      });
  });

  let posts = [];
  async function loadPosts() {
    try {
      posts = (await call("GET", "/api/admin/posts")).posts;
    } catch (e) {
      if (e.status !== 401) toast(e.message);
      return;
    }
    renderPosts();
  }
  function renderPosts() {
    const g = $("posts-group").value;
    const rows = posts.filter((p) => !g || p.group === g);
    $("posts-rows").replaceChildren(...rows.map((p) => {
      const tr = el("tr");
      tr.className = "has-cb";
      tr.append(checkCell(p.id, p.text), cell(p.group + (p.subgroup ? `, подгр. ${p.subgroup}` : ""), "Группа", "ad-nowrap"),
        cell(KIND[p.kind], null, "ad-kind"), cell(p.text, null, "ad-text"), cell(p.at ? when(p.at) : "—", "Когда", "ad-nowrap"),
        cell(when(p.until), "Удалится", "ad-nowrap"), cell(p.author || "—", "Староста"));
      return tr;
    }));
    $("posts-wrap").hidden = !rows.length;
    $("posts-empty").hidden = !!rows.length;
    $("posts-empty").textContent = posts.length ? "У этой группы объявлений и Д/З нет." : "Объявлений и Д/З от старост нет.";
    $("posts-all").checked = false;
    postsCheck.sync();
  }
  $("posts-group").addEventListener("change", renderPosts);
  $("posts-delete").addEventListener("click", () => {
    const ids = postsCheck.ids();
    ask(`Удалить ${ids.length} ${plural(ids.length, "объявление", "объявления", "объявлений")}?`,
      "Они пропадут у всей группы. Вернуть нельзя.", "Удалить", async () => {
        const out = await call("POST", "/api/admin/posts/delete", { ids });
        await Promise.all([loadPosts(), reload()]);
        toast(`Удалено: ${out.deleted}`);
      });
  });

  $("cleanup").addEventListener("click", () => {
    ask("Удалить прошедшее?", `Прошедшие объявления и Д/З старост и напоминания, время которых прошло (по часам сайта сейчас ${when(state.now)}), ` +
      "удалятся у всех, даже если студент их ещё не видел.", "Удалить", async () => {
      const out = await call("POST", "/api/admin/cleanup", {});
      await Promise.all([loadPosts(), reload(), $("notes-login").value ? loadNotes() : null]);
      toast(`Удалено: объявлений и Д/З ${out.posts}, напоминаний ${out.notes}`);
    });
  });

  // ---------- справочники ----------
  const file = { name: null, mtime: null, saved: "" };
  const editor = $("file-text");
  const dirty = () => file.name !== null && editor.value !== file.saved;

  function leaveFile() {
    return !dirty() || window.confirm(`В ${file.name} есть несохранённые правки. Уйти без сохранения?`);
  }
  window.addEventListener("beforeunload", (e) => {
    if (dirty()) {
      e.preventDefault();
      e.returnValue = "";
    }
  });

  function renderFileList() {
    $("file-list").replaceChildren(...state.files.map((f) => {
      const b = el("button", "ad-file", null);
      b.type = "button";
      b.dataset.name = f.name;
      b.append(el("b", null, f.title), el("small", null, f.name));
      b.setAttribute("aria-pressed", String(f.name === file.name));
      b.addEventListener("click", () => { if (f.name !== file.name && leaveFile()) openFile(f.name); });
      return b;
    }));
  }

  function setStatus(text, cls) {
    $("file-status").textContent = text;
    $("file-status").className = "ad-status" + (cls ? " " + cls : "");
  }

  async function openFile(name) {
    let out;
    try {
      out = await call("GET", "/api/admin/file?name=" + encodeURIComponent(name));
    } catch (e) {
      if (e.status !== 401) toast(e.message);
      return;
    }
    Object.assign(file, { name: out.name, mtime: out.mtime, saved: out.text });
    editor.value = out.text;
    editor.scrollTop = 0;
    $("file-hint").textContent = FILE_HINTS[name] || "";
    showError("file-error", "");
    setStatus(out.mtime ? `db/${name}` : `db/${name} — файла ещё нет`);
    renderFileList();
  }

  // высота текста до места pos в редакторе (строки переносятся): копия текста в невидимом блоке той же ширины
  function textHeight(pos) {
    const cs = getComputedStyle(editor), m = el("div");
    ["fontFamily", "fontSize", "lineHeight", "letterSpacing", "tabSize", "whiteSpace", "overflowWrap", "wordBreak",
      "paddingLeft", "paddingRight"].forEach((k) => { m.style[k] = cs[k]; });
    Object.assign(m.style, { position: "absolute", visibility: "hidden", left: "-9999px", top: "0",
      boxSizing: "border-box", width: editor.clientWidth + "px" });
    m.textContent = editor.value.slice(0, pos) + "\u200b";
    document.body.append(m);
    const h = m.scrollHeight;
    m.remove();
    return h;
  }

  function showJsonError(err) {
    showError("file-error", err.message);
    setStatus("Есть ошибка", "bad");
    const pos = err.data && err.data.pos;
    if (typeof pos === "number") { // выделить место ошибки и прокрутить к нему
      editor.focus();
      editor.setSelectionRange(pos, Math.min(pos + 1, editor.value.length));
      editor.scrollTop = Math.max(0, textHeight(pos) - editor.clientHeight / 3);
    }
  }

  async function saveFile(checkOnly) {
    if (!file.name) return;
    const btn = checkOnly ? $("file-check") : $("file-save");
    btn.disabled = true;
    try {
      const out = await call("POST", "/api/admin/file", { name: file.name, text: editor.value, mtime: file.mtime, check: checkOnly });
      showError("file-error", "");
      if (checkOnly) {
        setStatus("Ошибок нет", "good");
      } else {
        file.mtime = out.mtime;
        file.saved = editor.value;
        const t = new Date();
        setStatus(`Сохранено в ${String(t.getHours()).padStart(2, "0")}:${String(t.getMinutes()).padStart(2, "0")}`, "good");
        toast(`${file.name} сохранён`);
      }
    } catch (err) {
      if (err.status === 401) return;
      if (err.status === 409) {
        showError("file-error", err.message);
        setStatus("Не сохранено", "bad");
      } else {
        showJsonError(err);
      }
    } finally {
      btn.disabled = false;
    }
  }
  $("file-check").addEventListener("click", () => saveFile(true));
  $("file-save").addEventListener("click", () => saveFile(false));
  $("file-reload").addEventListener("click", () => { if (leaveFile()) openFile(file.name); });
  editor.addEventListener("input", () => { if (dirty()) setStatus("Есть несохранённые правки", "warn"); });
  editor.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
      e.preventDefault();
      saveFile(false);
    }
  });

  // ---------- старт: код из ссылки сразу убираем из адресной строки ----------
  (async () => {
    const code = new URLSearchParams(location.search).get("code");
    if (code) {
      history.replaceState(null, "", "/admin" + location.hash);
      try {
        await enter(code);
      } catch (e) {
        showLock(e.message);
      }
      return;
    }
    try {
      await load();
    } catch (e) {
      if (e.status !== 401) showLock(e.message);
    }
  })();
})();
