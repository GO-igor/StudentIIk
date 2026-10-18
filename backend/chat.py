"""Чат с моделью в терминале: модель + настоящие инструменты из backend.py.
По умолчанию нужен llama-server (llama.cpp):
    llama-server -m ishka-Q4_K_M.gguf --jinja --port 8080 -c 16384
chat.py сам собирает промпт ровно как при обучении (prompt_format.py, шаблон чата берёт из .gguf) и шлёт его
в /completion, так что шаблон чата и разбор вызовов в llama.cpp не участвуют. Вызвать инструмент или ответить
текстом, решает по P(<tool_call>) на первом токене ответа (порог CALL_THRESHOLD), а не жадным выбором токена.
Запуск (аккаунт заводится в accounts.py, профиль, заметки и напоминания хранятся в db/ishka.sqlite):
    pip install jinja2 && python chat.py --login ivanov
Без входа (--guest или пустой логин) Ишка работает, но не знает группу и преподавателей, а заметки и напоминания
живут до закрытия чата — об этом он предупреждает (store.GUEST_NOTICE).
Сработавшие напоминания Ишка показывает при входе, перед каждым ответом и раз в минуту, пока чат открыт; утренний
брифинг — сам, при первом входе за день с 7:30 до 12:00 или ровно в 7:30, если чат открыт (не ответил сервер — повторит).
Запретные фразы (db/blocklist.json, guard.py): на такой вопрос Ишка отвечает отказом из списка, ответ модели или
вызов инструмента с такой фразой не выводится. Список можно править, не закрывая чат.
Сам диалог (модель, инструменты, напоминания, брифинг) — в ishka.py, общий с сайтом (server.py).
Другие серверы (OpenAI-совместимый API, шаблон и вызовы разбирает сервер):
    ollama serve  → pip install openai && python chat.py --api openai --url http://localhost:11434/v1 --name ishka
    vllm serve out/merged --served-model-name ishka --enable-auto-tool-choice --tool-call-parser hermes --port 8080
                  → python chat.py --api openai
"""
import argparse, datetime as dt, getpass, os, select, sys, unicodedata
from guard import Guard
from ishka import Dialog, Model
from store import DEFAULT_DB, GUEST_NOTICE, Store, guest

ap = argparse.ArgumentParser()
ap.add_argument("--url", default="http://localhost:8080", help="адрес сервера (для --api openai — с /v1)")
ap.add_argument("--api", choices=("completion", "openai"), default="completion",
                help="completion — llama-server, промпт как при обучении; openai — Ollama, vLLM и т. п.")
ap.add_argument("--name", default="ishka", help="имя модели (только для --api openai)")
ap.add_argument("--now", help="подменить текущее время: '2026-10-06 12:10' (база тогда не меняется: прошедшее не удаляется)")
ap.add_argument("--login", help="логин (пароль спросит); без него спросит и логин, пустой — без входа")
ap.add_argument("--guest", action="store_true", help="без входа: Ишка не знает группу и преподавателей")
ap.add_argument("--db", help="база аккаунтов (по умолчанию db/ishka.sqlite)")
ap.add_argument("--debug", action="store_true", help="печатать сырой ответ модели")
a = ap.parse_args()

now = dt.datetime.fromisoformat(a.now) if a.now else dt.datetime.now().replace(second=0, microsecond=0)
guard = Guard()  # запретные фразы из db/blocklist.json: в вопросе, в ответе и в вызовах инструментов
if guard.error:
    raise SystemExit(f"Не читается список запретных фраз {guard.error.split(';')[0]}. Исправь файл и запусти снова.")
path = a.db or os.environ.get("ISHKA_DB") or str(DEFAULT_DB)
hint = (f"  python accounts.py{f' --db {a.db}' if a.db else ''} add {a.login or 'ivanov'} --group 8К51 --subgroup 2 --english Аксёнова")
login = "" if a.guest else a.login
if login is None:
    try:
        login = input("логин (Enter — без входа): ").strip() if os.path.exists(path) else ""
    except EOFError:
        login = ""
if login:
    if not os.path.exists(path):
        raise SystemExit(f"Базы аккаунтов {path} нет: сначала заведи аккаунт, например\n{hint}")
    store = Store(path)
    password = os.environ.get("ISHKA_PASSWORD")  # для проверок без ввода руками
    if password is None:
        password = getpass.getpass("пароль: ")
    acc = store.check_login(unicodedata.normalize("NFC", login), password)
    if not acc:
        raise SystemExit("Неверный логин или пароль")
    if not a.now:  # прошедшее удаляем только по настоящим часам: с --now (проверка) база не трогается
        store.purge_posts(now.strftime("%Y-%m-%d %H:%M"))
    print(f"Привет, {acc['name'] or acc['login']}! Группа {acc['grp']}" + (
        ", ты староста: можешь отправлять группе объявления и Д/З." if acc["role"] == "starosta" else "."))
else:
    store, acc = guest()  # всё записанное — в памяти, до закрытия чата
    print(GUEST_NOTICE)
    print("Войти: python chat.py --login <логин>" + ("" if os.path.exists(path) else f" (аккаунт заводится так:\n{hint})"))
model = Model(a.url, a.api, a.name, a.debug)
dialog = Dialog(model, guard, store, acc, now, frozen=bool(a.now), debug=a.debug)
model.prepare()  # номер токена <tool_call> (заодно проверка, что сервер отвечает) и шаблон чата из .gguf


def say(r):
    """Ответ Ишки, отказ из списка запретных фраз (пустой — промолчать) или «не получилось» с причиной."""
    if r["kind"] == "blocked":
        if r["text"]:
            print("Ишка>", r["text"])
        if a.debug:  # какая фраза сработала, видно только с --debug
            print(f"  [{r['why']}: «{r['phrase']}» из группы «{r['group']}»]")
        return
    print("Ишка>", r["text"])
    if r["kind"] == "fallback":
        if r["error"]:
            print(f"  [ошибка сервера: {r['error']}]")
        elif not r["interrupted"]:
            print(f"  [сырой ответ модели: {r['raw'][:300]!r}]" if r["raw"].strip() else "  [модель вернула пустой ответ]")


def show(event):
    """Сработавшее напоминание или утренний брифинг из tick()."""
    if event["kind"] == "reminder":
        print(event["text"])
    else:
        say(event)


def tick():
    """Перед каждым вопросом и раз в минуту, пока студент молчит: сработавшие напоминания и утренний брифинг.
    Возвращает True, если что-то напечатал."""
    return dialog.tick(show)


IDLE_CHECK = 60  # секунд: пока студент молчит, так часто проверяем напоминания и брифинг


def read_line(prompt):
    """Сообщение студента. Пока он молчит, раз в минуту tick(): напоминание или брифинг в 7:30 приходят сами,
    даже если чат просто открыт. Где ждать ввод с таймаутом нельзя (Windows, ввод не с клавиатуры) — обычный input."""
    if os.name == "nt" or not sys.stdin.isatty():
        return input(prompt)
    print(prompt, end="", flush=True)
    while not select.select([sys.stdin], [], [], IDLE_CHECK)[0]:
        if tick():
            print(prompt, end="", flush=True)
    line = sys.stdin.readline()
    if not line:
        raise EOFError
    return line.rstrip("\n")


guard_error = [None]  # о сломанном файле при перечитывании говорим один раз


tick()
while True:
    try:
        q = unicodedata.normalize("NFC", read_line("ты> "))
    except (EOFError, KeyboardInterrupt):
        break
    blocked = dialog.check(q)
    if guard.error != guard_error[0]:
        guard_error[0] = guard.error
        if guard.error:
            print(f"  [{guard.error}]")
    if blocked:  # модели вопрос не показываем и в историю не кладём
        say(blocked)
        continue
    tick()
    say(dialog.turn(q))
    tick()
