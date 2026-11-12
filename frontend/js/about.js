// «О нас»: заголовок «Наша команда» и карточки появляются, когда до них докрутили.
// Без JS и с «Уменьшить движение» в настройках системы всё видно сразу.
(function () {
  "use strict";
  if (!("IntersectionObserver" in window) || matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  const io = new IntersectionObserver((entries) => {
    entries.forEach((e) => {
      if (!e.isIntersecting) return;
      e.target.classList.add("in");
      io.unobserve(e.target);
    });
  }, { threshold: 0.2 });
  document.querySelectorAll(".reveal").forEach((el) => { el.classList.add("rv"); io.observe(el); });
})();
