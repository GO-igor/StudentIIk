// Страница входа: логин и пароль или вход без аккаунта, потом — на главную с чатом.
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const form = $("login-form"), card = $("card"), login = $("login"), password = $("password");
  const submit = $("submit"), submitText = $("submit-text"), guest = $("guest");
  const error = $("error"), errorText = $("error-text");

  function showError(text) {
    errorText.textContent = text;
    error.classList.add("show");
    card.classList.remove("shake");
    void card.offsetWidth; // перезапустить анимацию
    card.classList.add("shake");
  }

  function busy(on, button, label) {
    submit.disabled = guest.disabled = on;
    if (button === submit) {
      submitText.textContent = on ? "Входим…" : label;
      if (on) {
        const s = document.createElement("span");
        s.className = "spinner";
        s.id = "spin";
        submit.prepend(s);
      } else if ($("spin")) {
        $("spin").remove();
      }
    }
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    error.classList.remove("show");
    if (!login.value.trim()) {
      showError("Введи логин.");
      login.focus();
      return;
    }
    if (!password.value) {
      showError("Введи пароль.");
      password.focus();
      return;
    }
    busy(true, submit);
    try {
      await Api.post("/api/login", { login: login.value.trim(), password: password.value });
      location.replace("/");
    } catch (err) {
      busy(false, submit, "Войти");
      showError(err.message);
      password.select();
      password.focus();
    }
  });

  guest.addEventListener("click", async () => {
    error.classList.remove("show");
    busy(true, guest);
    try {
      await Api.post("/api/guest");
      location.replace("/");
    } catch (err) {
      busy(false, guest);
      showError(err.message);
    }
  });

  $("eye").addEventListener("click", () => {
    const show = password.type === "password";
    password.type = show ? "text" : "password";
    $("eye").setAttribute("aria-pressed", String(show));
    $("eye").setAttribute("aria-label", show ? "Скрыть пароль" : "Показать пароль");
    $("eye-open").toggleAttribute("hidden", show); // у svg нет свойства hidden — только атрибут
    $("eye-closed").toggleAttribute("hidden", !show);
    password.focus();
  });

  // предупреждение о включённом Caps Lock
  ["keydown", "keyup"].forEach((ev) =>
    password.addEventListener(ev, (e) => {
      if (e.getModifierState) {
        $("caps").textContent = e.getModifierState("CapsLock") ? "Включён Caps Lock" : "";
      }
    })
  );

  login.focus();
})();
