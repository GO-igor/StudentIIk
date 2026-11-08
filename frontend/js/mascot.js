// Ишка на главной смотрит на курсор: наклоняется в его сторону и переводит туда глаза (CSS: .px-mascot.follow).
// Мышь замерла или ушла со страницы — плавно выпрямляется и снова сам выглядывает то влево, то вправо.
// На телефоне (мыши нет) и с «Уменьшить движение» в настройках системы — всё как раньше.
(function () {
  "use strict";
  const mascot = document.getElementById("mascot");
  if (!mascot || !window.matchMedia || matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  const svg = mascot.querySelector(".mascot-svg");
  const parts = [svg, mascot.querySelector(".m-tassel"), mascot.querySelector(".m-look"), mascot.querySelector(".mascot-shadow")];
  const IDLE = 2500;   // мс без движения мыши — снова сам по себе
  const SETTLE = 500;  // мс на то, чтобы выпрямиться (как transition в CSS)
  let x = 0, y = 0, frame = 0, idleTimer = 0, settleTimer = 0;

  function look(lean, ex, ey) {
    mascot.style.setProperty("--lean", lean.toFixed(3));
    mascot.style.setProperty("--ex", ex);
    mascot.style.setProperty("--ey", ey);
  }

  function start() {
    clearTimeout(settleTimer);
    if (mascot.classList.contains("follow")) return;
    // начинаем с позы, в которой Ишка сейчас (посреди «выглядывания»), — без рывка
    const pose = parts.map((el) => getComputedStyle(el).transform);
    mascot.classList.add("follow");
    parts.forEach((el, i) => {
      el.style.transition = "none";
      el.style.transform = pose[i] === "none" ? "" : pose[i];
    });
    void mascot.offsetWidth;
    parts.forEach((el) => { el.style.transition = ""; el.style.transform = ""; });
  }

  function stop() {
    clearTimeout(idleTimer);
    if (!mascot.classList.contains("follow")) return;
    look(0, 0, 0);
    clearTimeout(settleTimer);
    settleTimer = setTimeout(() => mascot.classList.remove("follow"), SETTLE);
  }

  function update() {
    frame = 0;
    const r = svg.getBoundingClientRect();
    if (!r.width) return; // приветствие скрыто: идёт разговор
    start();
    const dx = x - (r.left + r.width * 0.45), dy = y - (r.top + r.height * 0.52); // от точки между глаз
    const lean = Math.max(-1, Math.min(1, dx / (innerWidth * 0.4)));
    const ex = Math.abs(dx) > r.width * 0.6 ? Math.sign(dx) : 0;
    const ey = dy < -r.height * 0.6 ? -1 : dy > r.height * 0.8 ? 1 : 0;
    look(lean, ex, ey);
    clearTimeout(idleTimer);
    idleTimer = setTimeout(stop, IDLE);
  }

  document.addEventListener("pointermove", (e) => {
    if (e.pointerType !== "mouse" && e.pointerType !== "pen") return;
    x = e.clientX;
    y = e.clientY;
    if (!frame) frame = requestAnimationFrame(update);
  }, { passive: true });
  document.documentElement.addEventListener("mouseleave", stop);
  window.addEventListener("blur", stop);
})();
