"""Сайт Ишки: страницы из папки frontend/ и API для входа и чата. Диалог тот же, что в chat.py (ishka.py).
    python server.py                      # сайт на http://localhost:8000, модель — llama-server на :8080
    python server.py --port 8001 --url http://localhost:8081
    python server.py --now "2026-10-15 08:00"   # подменить время для проверки (база тогда не меняется)
Нужен только jinja2 (как для chat.py). Аккаунты заводятся в терминале: python accounts.py add ...

Каждый вход (вкладка браузера с куки) — свой разговор: история, номера списков, напоминания и брифинг. Аккаунт —
только данные студента и права старосты, поэтому один аккаунт могут открыть одновременно в нескольких местах.
Сменили пароль (accounts.py passwd) или удалили аккаунт — вход на сайте сбрасывается при следующем запросе.

API (JSON):
    POST /api/login {login, password}   POST /api/guest   POST /api/logout   POST /api/reset (новый разговор)
    GET  /api/me — кто вошёл и история    POST /api/chat {text}    GET /api/updates — напоминания и брифинг
Ответ /api/chat и /api/updates: {"events": [...]}, событие — {"kind": "reminder", "text"} или ответ Ишки
{"kind": "answer" | "blocked" | "fallback", "text", "tools": [имена инструментов], "brief": true?, "error"?}.
"""
import argparse, datetime as dt, json, mimetypes, os, pathlib, secrets, threading, time, unicodedata
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from guard import Guard
from ishka import Dialog, Model
from store import DEFAULT_DB, GUEST_NOTICE, Store, guest

HERE = pathlib.Path(__file__).resolve().parent
FRONTEND = (HERE.parent / "frontend").resolve()
COOKIE = "ishka_sid"
IDLE = {False: 12 * 3600, True: 6 * 3600}  # секунд без запросов, после которых вход забывается (аккаунт, гость)
MAX_SESSIONS = 300   # больше — забываем тех, кто дольше всех молчит
MAX_TEXT = 2000      # символов в одном сообщении
MAX_BODY = 64 * 1024
HISTORY = 200        # сколько последних сообщений показывать после перезагрузки страницы
LOGIN_TRIES, LOGIN_WINDOW = 5, 300  # неудачных входов с одного адреса за LOGIN_WINDOW секунд, потом пауза
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; font-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
for _ext, _type in ((".woff2", "font/woff2"), (".svg", "image/svg+xml"), (".js", "text/javascript"), (".css", "text/css")):
    mimetypes.add_type(_type, _ext)  # на Windows и старых Mac типы файлов берутся из системы и бывают неверными


class Session:
    def __init__(self, dialog, store, acc, pw_hash=None):
        self.dialog, self.store, self.acc, self.pw_hash = dialog, store, acc, pw_hash
        self.guest = bool(acc.get("guest"))
        self.lock = threading.Lock()  # запросы одного разговора — по очереди (две вкладки, опрос напоминаний)
        self.last = time.time()
        self.history = []
        self.closed = False

    def remember(self, item):
        self.history.append(item)
        del self.history[:-HISTORY]

    def close(self):
        """Закрыть базу, дождавшись ответа, который сейчас идёт (выход в другой вкладке)."""
        got = self.lock.acquire(timeout=900)
        self.closed = True
        try:
            self.store.db.close()
        except Exception:
            pass
        finally:
            if got:
                self.lock.release()


class App:
    def __init__(self, args):
        self.args = args
        self.db_path = args.db or os.environ.get("ISHKA_DB") or str(DEFAULT_DB)
        self.now = dt.datetime.fromisoformat(args.now) if args.now else None
        self.guard = Guard()
        if self.guard.error:
            raise SystemExit(f"Не читается список запретных фраз {self.guard.error.split(';')[0]}. Исправь файл и запусти снова.")
        self.model = Model(args.url, args.api, args.name, args.debug, log=self.log)
        self.sessions, self.lock = {}, threading.Lock()
        self.fails = {}  # адрес -> времена неудачных входов

    def log(self, line):
        if self.args.debug:
            print(line, flush=True)

    # ---------- входы ----------
    def new_session(self, dialog, store, acc, pw_hash=None):
        token = secrets.token_urlsafe(32)
        with self.lock:
            old = self.sweep()
            self.sessions[token] = Session(dialog, store, acc, pw_hash)
        for s in old:
            s.close()
        return token

    def sweep(self):
        """Под self.lock: забыть старые входы. Возвращает их: закрыть базы — уже без self.lock."""
        t = time.time()
        old = [k for k, s in self.sessions.items() if t - s.last > IDLE[s.guest]]
        if len(self.sessions) - len(old) >= MAX_SESSIONS:
            rest = sorted((s.last, k) for k, s in self.sessions.items() if k not in old)
            old += [k for _, k in rest[:len(self.sessions) - len(old) - MAX_SESSIONS + 1]]
        return [self.sessions.pop(k) for k in old]

    def drop(self, token):
        with self.lock:
            s = self.sessions.pop(token, None)
        if s:
            s.close()

    def session(self, token):
        """Вход по куки или None (давно не заходили — забываем)."""
        with self.lock:
            s = self.sessions.get(token or "")
            expired = s and time.time() - s.last > IDLE[s.guest]
            if expired:
                self.sessions.pop(token)
        if expired:
            s.close()
            return None
        if s:
            s.last = time.time()
        return s

    @staticmethod
    def refresh(s):
        """Под s.lock: аккаунт из базы. Пароль сменили (accounts.py passwd) или аккаунт удалили — False, вход
        сбрасывается; профиль или роль поменяли (accounts.py edit) — дальше Ишка отвечает по новому."""
        if s.closed:
            return False
        if s.guest:
            return True
        row = s.store.account(account_id=s.acc["id"])
        if not row or row["pw_hash"] != s.pw_hash:
            return False
        row.pop("pw_hash")
        s.acc = s.dialog.acc = row
        s.dialog.set_profile(s.store.profile(row))
        return True

    def failed_recently(self, ip):
        t = time.time()
        with self.lock:
            tries = [x for x in self.fails.get(ip, []) if t - x < LOGIN_WINDOW]
            self.fails[ip] = tries
            return len(tries) >= LOGIN_TRIES

    def failed(self, ip):
        with self.lock:
            self.fails.setdefault(ip, []).append(time.time())
            if len(self.fails) > 10000:
                self.fails.clear()

    def login(self, login, password, ip):
        """(token, None) или (None, (код, текст ошибки))."""
        if self.failed_recently(ip):
            return None, (429, "Слишком много неудачных попыток. Подожди пару минут и попробуй снова.")
        if not login or not password:
            return None, (400, "Введи логин и пароль.")
        if not os.path.exists(self.db_path):
            return None, (503, "Аккаунтов ещё нет: заведи их в терминале командой python accounts.py add … "
                               "или войди без аккаунта.")
        store = Store(self.db_path, check_same_thread=False)
        acc = store.check_login(login, password)
        if not acc:
            store.db.close()
            self.failed(ip)
            return None, (401, "Неверный логин или пароль.")
        if not self.now:  # прошедшее удаляем только по настоящим часам: с --now (проверка) база не трогается
            store.purge_posts(dt.datetime.now().strftime("%Y-%m-%d %H:%M"))
        pw_hash = store.account(account_id=acc["id"])["pw_hash"]
        dialog = Dialog(self.model, self.guard, store, acc, self.now, frozen=bool(self.now), log=self.log)
        return self.new_session(dialog, store, acc, pw_hash), None

    def guest(self):
        store, acc = guest()  # всё записанное — в памяти, пока не выйдешь
        dialog = Dialog(self.model, self.guard, store, acc, self.now, frozen=bool(self.now), log=self.log)
        return self.new_session(dialog, store, acc)

    # ---------- разговор ----------
    def me(self, s):
        """Кто вошёл и история разговора; None — вход сброшен. Пока Ишка отвечает в другой вкладке, не ждём его."""
        if s.lock.acquire(blocking=False):
            try:
                if not self.refresh(s):
                    return None
            finally:
                s.lock.release()
        a = s.acc
        user = {"guest": True, "name": "Гость"} if s.guest else {
            "guest": False, "login": a["login"], "name": a.get("name") or a["login"], "group": a["grp"],
            "role": a["role"], "subgroup": a.get("subgroup"), "english_teacher": a.get("english_teacher")}
        return {"user": user, "notice": GUEST_NOTICE if s.guest else None, "history": list(s.history)}

    def events(self, s, run):
        """run(on_event) под замком разговора; события и в ответ, и в историю для перезагрузки страницы."""
        out = []

        def on_event(e):
            out.append(event_json(e) | {"at": s.dialog.now.strftime("%H:%M")})
            if out[-1].get("text"):
                s.remember(out[-1])
        run(on_event)
        return out

    def chat(self, s, text):
        """{"events": события в ответ на сообщение, "at": время сообщения по часам Ишки} или None, если вход сброшен."""
        with s.lock:
            if not self.refresh(s):
                return None
            at = s.dialog.now.strftime("%H:%M")

            def run(on_event):
                d = s.dialog
                blocked = d.check(text)
                s.remember({"kind": "user", "text": text, "at": at})
                if blocked:  # модели вопрос не показываем и в историю не кладём
                    on_event(blocked)
                    return
                d.tick(on_event)
                on_event(d.turn(text))
                d.tick(on_event)
            return {"events": self.events(s, run), "at": at}

    def updates(self, s):
        """Сработавшие напоминания и брифинг; None — вход сброшен."""
        if not s.lock.acquire(blocking=False):
            return []  # идёт ответ на сообщение: напоминания придут вместе с ним
        try:
            if not self.refresh(s):
                return None
            return self.events(s, s.dialog.tick)
        finally:
            s.lock.release()

    def reset(self, s):
        with s.lock:
            if s.closed:
                return False
            s.dialog.reset()
            s.history.clear()
            return True


def event_json(e):
    if e["kind"] == "reminder":
        return {"kind": "reminder", "text": e["text"]}
    out = {"kind": e["kind"], "text": e.get("text"), "tools": [name for name, _ in e.get("tools") or []]}
    if e.get("brief"):
        out["brief"] = True
    if e["kind"] == "fallback" and e.get("error"):
        out["error"] = e["error"]
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "Ishka"
    sys_version = ""
    app = None  # App, задаёт main()

    def log_message(self, fmt, *args):
        if self.app.args.debug:
            super().log_message(fmt, *args)

    # ---------- ответы ----------
    def headers_common(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Cache-Control", "no-store")

    def send_json(self, code, obj, cookie=None):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.headers_common()
        self.end_headers()
        self.wfile.write(body)

    def error(self, code, text):
        self.send_json(code, {"error": text})

    def redirect(self, to):
        self.send_response(303)
        self.send_header("Location", to)
        self.send_header("Content-Length", "0")
        self.headers_common()
        self.end_headers()

    def send_file(self, path):
        try:
            data = path.read_bytes()
        except OSError:
            return self.error(404, "Нет такой страницы")
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype == "image/svg+xml":
            ctype += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.headers_common()
        self.end_headers()
        self.wfile.write(data)

    @staticmethod
    def cookie(token, max_age):
        return f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}"

    # ---------- запрос ----------
    def token(self):
        c = SimpleCookie()
        try:
            c.load(self.headers.get("Cookie") or "")
        except Exception:
            return None
        return c[COOKIE].value if COOKIE in c else None

    def same_origin(self):
        """Запросы, меняющие что-то, — только со страниц самого сайта (защита от чужих сайтов)."""
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).netloc != self.headers.get("Host"):
            return False
        return (self.headers.get("Content-Type") or "").split(";")[0].strip() == "application/json"

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ValueError("слишком большой запрос")
        raw = self.rfile.read(n) if n else b"{}"
        data = json.loads(raw.decode("utf-8") or "{}")
        if not isinstance(data, dict):
            raise ValueError("ждали объект JSON")
        return data

    def do_GET(self):
        path = urlsplit(self.path).path
        app = self.app
        if path in ("/api/me", "/api/updates"):
            token = self.token()
            s = app.session(token)
            out = s and (app.me(s) if path == "/api/me" else {"events": app.updates(s)})
            if out is None or out == {"events": None}:
                app.drop(token)
                return self.error(401, "Нужно войти")
            return self.send_json(200, out)
        if path in ("/", "/index.html"):
            return self.send_file(FRONTEND / "index.html") if app.session(self.token()) else self.redirect("/login")
        if path in ("/login", "/login.html"):  # гость тоже может открыть вход: войти в аккаунт
            s = app.session(self.token())
            return self.redirect("/") if s and not s.guest else self.send_file(FRONTEND / "login.html")
        if path == "/favicon.ico":
            return self.send_file(FRONTEND / "img" / "favicon.svg")
        if path.startswith("/static/"):
            f = (FRONTEND / path[len("/static/"):]).resolve()
            if FRONTEND in f.parents and f.is_file() and not f.name.startswith("."):
                return self.send_file(f)
        self.error(404, "Нет такой страницы")

    def do_POST(self):
        path = urlsplit(self.path).path
        app = self.app
        if not self.same_origin():
            return self.error(403, "Запрос не со страницы сайта")
        try:
            data = self.body()
        except (ValueError, UnicodeDecodeError) as e:
            return self.error(400, f"Непонятный запрос: {e}")
        if path == "/api/login":
            login = unicodedata.normalize("NFC", str(data.get("login") or "")).strip()
            token, err = app.login(login, str(data.get("password") or ""), self.client_address[0])
            if err:
                return self.error(*err)
            app.drop(self.token())  # прежний вход в этом браузере (например, гостем) больше не нужен
            return self.send_json(200, {"ok": True}, self.cookie(token, IDLE[False]))
        if path == "/api/guest":
            app.drop(self.token())
            return self.send_json(200, {"ok": True}, self.cookie(app.guest(), IDLE[True]))
        if path == "/api/logout":
            app.drop(self.token())
            return self.send_json(200, {"ok": True}, self.cookie("", 0))
        token = self.token()
        s = app.session(token)
        if not s:
            return self.error(401, "Нужно войти")
        if path == "/api/chat":
            text = unicodedata.normalize("NFC", str(data.get("text") or "")).strip()
            if not text:
                return self.error(400, "Пустое сообщение")
            if len(text) > MAX_TEXT:
                return self.error(400, f"Слишком длинное сообщение: не больше {MAX_TEXT} символов")
            out = app.chat(s, text)
            if out is None:
                app.drop(token)
                return self.error(401, "Нужно войти")
            return self.send_json(200, out)
        if path == "/api/reset":
            if not app.reset(s):
                return self.error(401, "Нужно войти")
            return self.send_json(200, {"ok": True})
        self.error(404, "Нет такого запроса")


def main():
    ap = argparse.ArgumentParser(description="Сайт Ишки")
    ap.add_argument("--host", default="127.0.0.1", help="0.0.0.0 — открыть сайт другим устройствам в этой сети")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--url", default="http://localhost:8080", help="адрес сервера модели (для --api openai — с /v1)")
    ap.add_argument("--api", choices=("completion", "openai"), default="completion",
                    help="completion — llama-server, промпт как при обучении; openai — Ollama, vLLM и т. п.")
    ap.add_argument("--name", default="ishka", help="имя модели (только для --api openai)")
    ap.add_argument("--db", help="база аккаунтов (по умолчанию db/ishka.sqlite)")
    ap.add_argument("--now", help="подменить время: '2026-10-15 08:00' (база тогда не меняется: прошедшее не удаляется)")
    ap.add_argument("--debug", action="store_true", help="печатать запросы, вызовы инструментов и ответы модели")
    args = ap.parse_args()
    if not (FRONTEND / "index.html").is_file():
        raise SystemExit(f"Нет страниц сайта: {FRONTEND}. Запусти server.py из папки backend проекта StudentIIk.")
    app = Handler.app = App(args)
    if not os.path.exists(app.db_path):
        print(f"Базы аккаунтов {app.db_path} нет: на сайте работает только вход без аккаунта. Завести аккаунт:\n"
              f"  python accounts.py add ivanov --group 8К51 --subgroup 2 --english Аксёнова")
    app.model.prepare(log=print)  # номер токена <tool_call> и шаблон чата; сервер модели не ответил — спросим позже
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    host = "localhost" if args.host in ("127.0.0.1", "localhost") else args.host
    print(f"Сайт Ишки: http://{host}:{args.port}  (остановить: Ctrl+C)", flush=True)
    if args.host not in ("127.0.0.1", "localhost"):
        print("⚠️ Сайт открыт другим устройствам этой сети. Соединение без HTTPS: пароли идут открытым текстом.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nСайт остановлен.")


if __name__ == "__main__":
    main()
