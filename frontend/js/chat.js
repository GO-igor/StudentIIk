// Главная: чат с Ишкой. Сообщения — в /api/chat, раз в 30 секунд /api/updates: напоминания, утренний брифинг
// и ответы, которых страница ещё не видела (перезагрузили во время ответа, написали из другой вкладки).
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const scroller = $("scroller"), messages = $("messages"), welcome = $("welcome");
  const form = $("composer"), input = $("input"), send = $("send"), counter = $("counter"), toBottom = $("to-bottom");
  const MAX = 2000, POLL = 30000, BUSY_POLL = 2500;
  const TOOLS = {
    get_schedule: "📅 расписание", find_room: "🧭 аудитории", get_teacher: "👩‍🏫 преподаватели",
    check_travel_time: "🚶 дорога", find_food: "🍔 перекус", get_session: "🎓 сессия", get_events: "🎉 мероприятия",
    get_daily_brief: "☀️ брифинг", add_note: "📝 заметки", add_homework_reminder: "📚 напоминание о Д/З",
    list_notes: "📝 заметки", delete_items: "🗑 удаление", group_announce: "📢 объявление группе",
    group_add_homework: "📚 Д/З группе", group_delete_post: "🗑 объявления группы",
  };
  const SUGGESTIONS = [
    ["📅", "Что завтра по парам?"],
    ["🧭", "Как найти 529 аудиторию в 19 корпусе?"],
    ["🍔", "Где перекусить на перемене?"],
    ["⏰", "Напомни завтра в 9:00 позвонить в деканат"],
    ["📝", "Мои заметки"],
    ["🎓", "Сколько до сессии?"],
  ];
  const STAROSTA = ["📢", "Объяви группе: собрание в пятницу в 15:00"];

  let user = null, sending = false, polling = false, typingEl = null;
  let lastN = 0; // номер последнего показанного события из истории на сервере
  let gen = 0;   // растёт при «Заново»: ответы на запросы до сброса не показываем
  let soon = null;

  // ---------- вывод ----------
  function linkify(text) {
    // текст ответа — только как текст; ссылки — отдельными узлами <a>
    const frag = document.createDocumentFragment();
    const re = /https?:\/\/[^\s<>"«»]+[^\s<>"«».,;:!?)]/g;
    let last = 0, m;
    while ((m = re.exec(text))) {
      frag.append(text.slice(last, m.index));
      const a = document.createElement("a");
      a.href = m[0];
      a.textContent = m[0];
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      frag.append(a);
      last = m.index + m[0].length;
    }
    frag.append(text.slice(last));
    return frag;
  }

  function nearBottom() {
    return scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 140;
  }

  // автопрокрутка — мгновенно: плавная не успевает за новыми сообщениями и оставляет их под полем ввода
  function scrollDown(stick) {
    if (!stick) return toBottom.classList.toggle("show", !nearBottom());
    scroller.scrollTop = scroller.scrollHeight;
    requestAnimationFrame(() => { scroller.scrollTop = scroller.scrollHeight; });
  }

  // новое сообщение — перед «Ишка печатает», чтобы точки оставались последними
  function put(row) {
    const stick = nearBottom();
    if (typingEl) messages.insertBefore(row, typingEl);
    else messages.append(row);
    return stick;
  }

  function hideWelcome() {
    welcome.hidden = true;
  }

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  function ishkaPic() {
    const img = el("img", "pic");
    img.src = "/static/img/ishka.svg";
    img.alt = "";
    return img;
  }

  function addUser(text, at) {
    hideWelcome();
    const row = el("div", "msg user");
    const body = el("div", "body");
    const bubble = el("div", "bubble");
    bubble.append(linkify(text));
    body.append(bubble, el("div", "meta", at || timeNow()));
    row.append(body);
    put(row);
    scrollDown(true);
    return row;
  }

  function addReminder(text) {
    hideWelcome();
    const row = el("div", "reminder");
    const span = el("span", "text");
    span.append(linkify(text.replace(/^⏰\s*/, "")));
    row.append(el("span", "bell", "⏰"), span);
    scrollDown(put(row));
  }

  function addIshka(ev, onRetry) {
    if (!ev.text) return; // запретная фраза с пустым ответом: Ишка промолчал
    hideWelcome();
    const row = el("div", `msg ishka ${ev.kind}${ev.brief ? " brief" : ""}`);
    const body = el("div", "body");
    const bubble = el("div", "bubble");
    if (ev.brief) bubble.append(el("span", "brief-title", "☀️ Утренний брифинг"));
    bubble.append(linkify(ev.text));
    if (ev.kind === "fallback" && ev.error) {
      bubble.append(el("span", "err-detail", ev.error));
    }
    if (onRetry) {
      const b = el("button", "retry", "Повторить");
      b.type = "button";
      b.addEventListener("click", () => { if (onRetry() !== false) row.remove(); });
      bubble.append(b);
    }
    const meta = el("div", "meta");
    meta.append(el("span", "", ev.at || timeNow()));
    const seen = new Set();
    for (const t of ev.tools || []) {
      const label = TOOLS[t] || t;
      if (seen.has(label)) continue;
      seen.add(label);
      const chip = el("span", "tool", label);
      chip.title = "Ишка взял это из базы, а не придумал";
      meta.append(chip);
    }
    body.append(bubble, meta);
    row.append(ishkaPic(), body);
    scrollDown(put(row));
  }

  function render(ev) {
    if (ev.n) {
      if (ev.n <= lastN) return; // уже на странице
      lastN = ev.n;
    }
    if (ev.kind === "user") addUser(ev.text, ev.at);
    else if (ev.kind === "reminder") { addReminder(ev.text); attention("⏰ Напоминание"); }
    else addIshka(ev);
  }

  function showTyping(on, force) {
    if (on) {
      const stick = force || nearBottom();
      if (!typingEl) {
        typingEl = el("div", "msg ishka typing");
        typingEl.setAttribute("aria-label", "Ишка печатает");
        const bubble = el("div", "bubble");
        bubble.append(el("span", "dot"), el("span", "dot"), el("span", "dot"));
        const body = el("div", "body");
        body.append(bubble);
        typingEl.append(ishkaPic(), body);
      }
      messages.append(typingEl); // уже есть — переносим в конец, под новый вопрос
      scrollDown(stick);
    } else if (!on && typingEl) {
      typingEl.remove();
      typingEl = null;
    }
  }

  function timeNow() {
    return new Date().toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  }

  let toastTimer = null;
  function toast(text) {
    const t = $("toast");
    t.textContent = text;
    t.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.remove("show"), 2600);
  }

  // заголовок вкладки мигает, пока страница скрыта
  const baseTitle = document.title;
  function attention(text) {
    if (!document.hidden) return;
    document.title = `${text} — Ишка`;
  }
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) {
      document.title = baseTitle;
      poll();
    }
  });

  // ---------- пользователь ----------
  function initials(name) {
    const parts = name.trim().split(/\s+/);
    return ((parts[0] || "?")[0] + (parts[1] ? parts[1][0] : "")).toUpperCase();
  }

  function showUser(me) {
    user = me.user;
    $("user-name").textContent = user.name;
    $("avatar").textContent = user.guest ? "👋" : initials(user.name);
    const sub = $("user-sub");
    sub.textContent = "";
    if (user.guest) {
      sub.textContent = "без входа";
    } else {
      sub.append(`Группа ${user.group}` + (user.subgroup ? `, подгр. ${user.subgroup}` : ""));
      if (user.role === "starosta") sub.append(el("span", "badge", "староста"));
    }
    $("user-chip").title = user.guest ? "Вход без аккаунта" : `${user.name} (${user.login})`;
    if (me.notice) {
      $("notice-text").textContent = me.notice.replace(/^⚠️\s*/, "");
      $("notice").hidden = false;
    }
    $("welcome-title").textContent = user.guest ? "Привет! Я Ишка 👋" : `Привет, ${user.name.split(/\s+/)[0]}! Я Ишка 👋`;
    const list = $("suggestions");
    list.textContent = "";
    const items = SUGGESTIONS.slice(0, user.role === "starosta" ? 5 : 6);
    if (user.role === "starosta") items.push(STAROSTA);
    items.forEach(([icon, text], i) => {
      const b = el("button", "suggestion");
      b.type = "button";
      b.style.animationDelay = `${0.05 + i * 0.05}s`;
      b.append(el("span", "s-icon", icon), el("span", "", text));
      b.addEventListener("click", () => ask(text));
      list.append(b);
    });
  }

  function toLogin() {
    location.replace("/login");
  }

  // ---------- разговор ----------
  async function ask(text) {
    text = text.trim();
    if (!text || sending) return;
    if (text.length > MAX) {
      toast(`Не больше ${MAX} символов`);
      return;
    }
    sending = true;
    updateSend();
    const row = addUser(text), mine = gen;
    input.value = "";
    autosize();
    showTyping(true, true);
    try {
      // after: в ответе будет и то, что пришло, пока сообщение ждало очереди (например, брифинг)
      const data = await Api.post("/api/chat", { text, after: lastN });
      if (mine !== gen) return; // пока ждали, начали заново
      if (data.at) row.querySelector(".meta").textContent = data.at; // время по часам Ишки, как в истории
      showTyping(false);
      for (const ev of data.events) {
        if (ev.n === data.n) lastN = Math.max(lastN, ev.n); // само сообщение уже на странице
        else render(ev);
      }
    } catch (err) {
      showTyping(false);
      if (err.status === 401) return toLogin();
      if (mine !== gen) return;
      addIshka({ kind: "fallback", text: "Не получилось отправить сообщение 😅", error: err.message }, () => {
        if (sending) {
          toast("Дождись ответа Ишки и нажми ещё раз");
          return false;
        }
        row.remove();
        ask(text);
      });
    } finally {
      sending = false;
      updateSend();
      input.focus();
    }
  }

  async function poll() {
    if (polling || sending || !user) return;
    polling = true;
    clearTimeout(soon);
    const mine = gen;
    // брифинг собирает модель: если ответ долгий, показываем, что Ишка печатает
    const slow = setTimeout(() => showTyping(true), 1500);
    try {
      const data = await Api.get(`/api/updates?after=${lastN}`);
      clearTimeout(slow);
      if (mine !== gen) return;
      data.events.forEach(render);
      // занято не своим сообщением: Ишка отвечает на вопрос, отправленный до перезагрузки или из другой вкладки
      if (!sending) {
        showTyping(data.busy);
        if (data.busy) soon = setTimeout(poll, BUSY_POLL);
      }
    } catch (err) {
      clearTimeout(slow);
      if (!sending) showTyping(false);
      if (err.status === 401) toLogin();
    } finally {
      polling = false;
    }
  }

  // ---------- поле ввода ----------
  function autosize() {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 168) + "px";
  }

  function updateSend() {
    const n = input.value.length;
    send.disabled = sending || !input.value.trim() || n > MAX;
    $("reset").disabled = sending; // сброс подождёт ответа: пока Ишка отвечает, кнопка неактивна
    counter.textContent = n > MAX - 300 ? `${n} / ${MAX}` : "";
    counter.className = n > MAX ? "over" : "";
  }

  input.addEventListener("input", () => { autosize(); updateSend(); });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing && e.keyCode !== 229) {
      e.preventDefault();
      ask(input.value);
    }
  });
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    ask(input.value);
  });

  // ---------- кнопки ----------
  scroller.addEventListener("scroll", () => toBottom.classList.toggle("show", !nearBottom()));
  toBottom.addEventListener("click", () => scroller.scrollTo({ top: scroller.scrollHeight, behavior: "smooth" }));

  $("logout").addEventListener("click", async (e) => {
    e.currentTarget.disabled = true;
    try {
      await Api.post("/api/logout");
    } finally {
      toLogin();
    }
  });

  const modal = $("modal"), page = document.querySelector(".chat-page"), modalOk = $("modal-ok");
  function openModal() {
    modal.classList.add("show");
    page.inert = true; // пока открыт вопрос, остальная страница недоступна (Tab, Enter в поле ввода)
    modalOk.focus();
  }
  function closeModal() {
    if (modalOk.disabled) return; // уже сбрасываем
    modal.classList.remove("show");
    page.inert = false;
    $("reset").focus();
  }
  $("reset").addEventListener("click", openModal);
  $("modal-cancel").addEventListener("click", closeModal);
  modal.addEventListener("click", (e) => { if (e.target === modal) closeModal(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && modal.classList.contains("show")) closeModal(); });
  modalOk.addEventListener("click", async () => {
    modalOk.disabled = true;
    try {
      await Api.post("/api/reset");
      gen++;
      clearTimeout(soon);
      showTyping(false);
      messages.textContent = "";
      welcome.hidden = false;
      toast("Начали заново ✨");
    } catch (err) {
      if (err.status === 401) return toLogin();
      toast(err.message);
    } finally {
      modalOk.disabled = false;
      modal.classList.remove("show");
      page.inert = false;
      input.focus();
    }
  });
  $("notice-login").addEventListener("click", async (e) => {
    e.preventDefault();
    try { await Api.post("/api/logout"); } finally { toLogin(); }
  });

  // ---------- старт ----------
  (async function start() {
    let me;
    try {
      me = await Api.get("/api/me");
    } catch (err) {
      if (err.status === 401) return toLogin();
      addIshka({ kind: "fallback", text: "Не получилось загрузить чат 😅", error: err.message });
      return;
    }
    showUser(me);
    if (me.history.length) {
      me.history.forEach(render);
      scrollDown(true);
    } else if (!me.busy) {
      welcome.hidden = false;
    }
    if (me.busy) showTyping(true, true); // перезагрузили во время ответа: ответ придёт в poll()
    input.focus();
    poll();
    setInterval(poll, POLL);
  })();
})();
