// Страница регистрации — пока только образец («открытка»): что ввели, никуда не отправляется и не проверяется.
// Здесь только то, что видно глазу: показать пароль, Ишка подпрыгивает, подсказка по кнопке «Зарегистрироваться».
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const mascot = $("mascot"), notice = $("notice");

  // Enter в поле ничего не отправляет
  $("reg-form").addEventListener("submit", (e) => e.preventDefault());

  // глаз у каждого пароля: показать или скрыть
  document.querySelectorAll(".lg-eye").forEach((eye) => {
    const input = $(eye.dataset.for);
    const [open, closed] = eye.querySelectorAll("svg");
    eye.addEventListener("click", () => {
      const show = input.type === "password";
      input.type = show ? "text" : "password";
      eye.setAttribute("aria-pressed", String(show));
      eye.setAttribute("aria-label", show ? "Скрыть пароль" : "Показать пароль");
      open.toggleAttribute("hidden", show); // у svg нет свойства hidden — только атрибут
      closed.toggleAttribute("hidden", !show);
      input.focus();
    });
  });

  // Ишка подпрыгивает по щелчку (на телефоне наведения нет)
  function jump() {
    mascot.classList.remove("jump");
    void mascot.offsetWidth; // заново запустить анимацию
    mascot.classList.add("jump");
  }
  mascot.addEventListener("click", jump);
  mascot.addEventListener("animationend", (e) => { if (e.target.classList.contains("mascot-svg")) mascot.classList.remove("jump"); });

  // «Зарегистрироваться»: регистрации пока нет — подсказка вместо отправки
  $("submit").addEventListener("click", () => {
    notice.hidden = false;
    notice.classList.remove("show");
    void notice.offsetWidth;
    notice.classList.add("show");
    notice.scrollIntoView({ block: "nearest", behavior: "smooth" }); // на ноутбуке кнопка у самого низа окна
    jump();
  });
})();
