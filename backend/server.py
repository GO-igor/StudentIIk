"""Сайт Ишки: страницы из папки frontend/ и API для входа и чата. Диалог тот же, что в chat.py (ishka.py).
    python server.py                      # сайт на http://localhost:8000, модель — llama-server на :8080
    python server.py --port 8001 --url http://localhost:8081
    python server.py --now "2026-10-15 08:00"   # подменить время для проверки (часы стоят, прошедшее не удаляется)
Нужен только jinja2 (как для chat.py). Аккаунты заводятся в терминале: python accounts.py add ...

Каждый вход (вкладка браузера с куки) — свой разговор: история, номера списков, напоминания и брифинг. Аккаунт —
только данные студента и права старосты, поэтому один аккаунт могут открыть одновременно в нескольких местах.
Сменили пароль (accounts.py passwd) или удалили аккаунт — вход на сайте сбрасывается при следующем запросе.
Две вкладки одного браузера — один вход (куки общая), поэтому и разговор у них общий.

API (JSON):
    POST /api/login {login, password}   POST /api/guest   POST /api/logout   POST /api/reset (новый разговор)
    GET  /api/me — кто вошёл и история    POST /api/chat {text, after?}    GET /api/updates?after=N — что нового
Событие — {"kind": "user" | "reminder", "text"} или ответ Ишки {"kind": "answer" | "blocked" | "fallback", "text",
"tools": [имена инструментов], "brief": true?, "error"?}; у событий из истории ещё "n" (номер по порядку) и "at".
/api/chat отвечает {"events", "at", "n"}: события после сообщения (с after — вся история после номера after,
и само сообщение тоже), его время и номер. /api/updates — {"events": история после номера N (сработавшие напоминания,
брифинг, ответы из других вкладок), "busy"}. "busy" (и в /api/me) — Ишка ещё отвечает: страницу перезагрузили во
время ответа, ответ придёт в /api/updates. "sid" — номер входа: сменился — куки уже от другого входа (в другой
вкладке вошли заново), страницу нужно перезагрузить.
"""
import argparse, datetime as dt, ipaddress, json, mimetypes, os, pathlib, secrets, socket, sys, threading, time
import traceback, unicodedata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from guard import Guard
from ishka import Dialog, Model
from store import DEFAULT_DB, GUEST_NOTICE, Store, guest

HERE = pathlib.Path(__file__).resolve().parent
FRONTEND = (HERE.parent / "frontend").resolve()
COOKIE = "ishka_sid"
IDLE = {False: 12 * 3600, True: 6 * 3600}  # секунд без запросов, после которых вход забывается (аккаунт, гость)
MAX_SESSIONS = {False: 300, True: 100}  # входов с аккаунтом и гостей; больше — забываем тех, кто дольше всех молчит
MAX_TEXT = 2000      # символов в одном сообщении
MAX_BODY = 64 * 1024
HISTORY = 200        # сколько последних сообщений показывать после перезагрузки страницы
LOGIN_TRIES, LOGIN_WINDOW = 5, 300  # неудачных входов с одного адреса за LOGIN_WINDOW секунд, потом пауза
CLIENT_TIMEOUT = 60  # секунд на чтение запроса и отправку ответа (ответ модели сюда не входит)
DROPPED = (BrokenPipeError, ConnectionResetError, socket.timeout, TimeoutError)  # клиент ушёл, не дождавшись
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; font-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
for _ext, _type in ((".woff2", "font/woff2"), (".svg", "image/svg+xml"), (".js", "text/javascript"), (".css", "text/css")):
    mimetypes.add_type(_type, _ext)  # на Windows и старых Mac типы файлов берутся из системы и бывают неверными


class Session:
    def __init__(self, dialog, store, acc, pw_hash=None):
        self.dialog, self.store, self.acc, self.pw_hash = dialog, store, acc, pw_hash
        self.guest = bool(acc.get("guest"))
        self.sid = secrets.token_hex(4)  # не секрет: страница по нему замечает, что куки теперь от другого входа
        self.lock = threading.Lock()  # запросы одного разговора — по очереди (две вкладки, опрос напоминаний)
        self.last = time.time()
        self.history = []
        self.seq = 0  # номер последнего события в истории: по нему страница узнаёт, что нового
        self.closed = False

    def remember(self, item):
        self.seq += 1
        item["n"] = self.seq
        self.history.append(item)
        del self.history[:-HISTORY]

    def after(self, n):
        return [h for h in list(self.history) if h["n"] > n]

    def close(self):
        """Вход уже забыт (closed — запросы, ждущие замка, получат 401). Базу закрываем в фоне, когда закончится
        ответ, который сейчас идёт: выход в другой вкладке не ждёт модель."""
        self.closed = True

        def wait_and_close():
            with self.lock:
                try:
                    self.store.db.close()
                except Exception:
                    pass
        threading.Thread(target=wait_and_close, daemon=True).start()


class App:
    def __init__(self, args):
        self.args = args
        self.db_path = args.db or os.environ.get("ISHKA_DB") or str(DEFAULT_DB)
        self.now = dt.datetime.fromisoformat(args.now) if args.now else None
        self.guard = Guard()
        if self.guard.error:
            raise SystemExit(f"Не читается список запретных фраз {self.guard.error.split(';')[0]}. Исправь файл и запусти снова.")
        self.guard_error = None  # о сломанном файле при перечитывании говорим один раз, как chat.py
        self.model = Model(args.url, args.api, args.name, args.debug, log=self.log)
        self.sessions, self.lock = {}, threading.Lock()
        self.fails = {}  # адрес -> времена неудачных (и идущих сейчас) попыток входа
        self.hint = f"python accounts.py{f' --db {args.db}' if args.db else ''} add ivanov --group 8К51"

    def log(self, line):
        if self.args.debug:
            print(line, flush=True)

    # ---------- входы ----------
    def new_session(self, dialog, store, acc, pw_hash=None):
        token = secrets.token_urlsafe(32)
        s = Session(dialog, store, acc, pw_hash)
        with self.lock:
            old = self.sweep(s.guest)
            self.sessions[token] = s
        for x in old:
            x.close()
        return token

    def sweep(self, guest):
        """Под self.lock: забыть старые входы и освободить место новому (гости вытесняют только гостей).
        Возвращает забытые: закрыть их базы — уже без self.lock."""
        t = time.time()
        old = {k for k, s in self.sessions.items() if t - s.last > IDLE[s.guest]}
        rest = sorted((s.last, k) for k, s in self.sessions.items() if k not in old and s.guest == guest)
        old.update(k for _, k in rest[:max(0, len(rest) - MAX_SESSIONS[guest] + 1)])
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

    def try_login(self, ip):
        """Занять попытку входа до проверки пароля (иначе параллельные запросы обходят лимит); None — лимит."""
        t = time.time()
        with self.lock:
            if len(self.fails) > 10000:
                self.fails.clear()
            tries = [x for x in self.fails.get(ip, []) if t - x < LOGIN_WINDOW]
            if len(tries) >= LOGIN_TRIES:
                self.fails[ip] = tries
                return None
            self.fails[ip] = tries + [t]
            return t

    def login_ok(self, ip, t):
        """Пароль верный — попытка не считается неудачной."""
        with self.lock:
            tries = self.fails.get(ip, [])
            if t in tries:
                tries.remove(t)

    def login(self, login, password, ip):
        """(token, None) или (None, (код, текст ошибки))."""
        if not login or not password:
            return None, (400, "Введи логин и пароль.")
        if not os.path.exists(self.db_path):
            return None, (503, "Вход по аккаунтам пока не настроен. Можно продолжить без входа.")
        t = self.try_login(ip)
        if t is None:
            return None, (429, f"Слишком много неудачных попыток. Подожди {LOGIN_WINDOW // 60} минут и попробуй снова.")
        store = Store(self.db_path, check_same_thread=False)
        acc = store.check_login(login, password)
        if not acc:
            store.db.close()
            return None, (401, "Неверный логин или пароль.")
        self.login_ok(ip, t)
        if not self.now:  # прошедшее удаляем только по настоящим часам, с --now (проверка) — нет
            store.purge_posts(dt.datetime.now().strftime("%Y-%m-%d %H:%M"))
        pw_hash = store.account(account_id=acc["id"])["pw_hash"]
        dialog = Dialog(self.model, self.guard, store, acc, self.now, frozen=bool(self.now), debug=self.args.debug,
                        log=self.log)
        return self.new_session(dialog, store, acc, pw_hash), None

    def guest(self):
        store, acc = guest()  # всё записанное — в памяти, пока не выйдешь
        dialog = Dialog(self.model, self.guard, store, acc, self.now, frozen=bool(self.now), debug=self.args.debug,
                        log=self.log)
        return self.new_session(dialog, store, acc)

    # ---------- разговор ----------
    def me(self, s):
        """Кто вошёл и история разговора; None — вход сброшен. Пока Ишка отвечает (в другой вкладке или до
        перезагрузки страницы), не ждём его: busy, и ответ придёт в /api/updates."""
        busy = not s.lock.acquire(blocking=False)
        if not busy:
            try:
                if not self.refresh(s):
                    return None
            finally:
                s.lock.release()
        elif s.closed:
            return None
        a = s.acc
        user = {"guest": True, "name": "Гость"} if s.guest else {
            "guest": False, "login": a["login"], "name": a.get("name") or a["login"], "group": a["grp"],
            "role": a["role"], "subgroup": a.get("subgroup"), "english_teacher": a.get("english_teacher")}
        return {"user": user, "notice": GUEST_NOTICE if s.guest else None, "history": s.after(0), "busy": busy,
                "sid": s.sid}

    def events(self, s, run):
        """run(on_event) под замком разговора; события и в ответ, и в историю для перезагрузки страницы."""
        out = []

        def on_event(e):
            out.append(event_json(e) | {"at": s.dialog.now.strftime("%H:%M")})
            if out[-1].get("text"):
                s.remember(out[-1])
            if e["kind"] == "blocked" and self.args.debug:  # какая фраза сработала, как в chat.py --debug
                print(f"  [{e['why']}: «{e['phrase']}» из группы «{e['group']}»]", flush=True)
        run(on_event)
        return out

    def guard_changed(self):
        """Сломали db/blocklist.json, пока сайт работает: сказать в окне сайта (один раз), как chat.py."""
        err = self.guard.error
        with self.lock:
            if err == self.guard_error:
                return
            self.guard_error = err
        if err:
            print(f"  [{err}]", flush=True)

    def chat(self, s, text, after=None):
        """{"events": события после сообщения или вся история после номера after, "at": время сообщения по часам
        Ишки, "n": его номер в истории} или None, если вход сброшен. С after страница не теряет то, что пришло в
        опрос, пока она ждала ответ (брифинг собирался в /api/updates, а сообщение ждало своей очереди)."""
        with s.lock:
            if not self.refresh(s):
                return None
            d = s.dialog
            d.clock()
            at = d.now.strftime("%H:%M")
            blocked = d.check(text)
            self.guard_changed()
            user = {"kind": "user", "text": text, "at": at}
            s.remember(user)

            def run(on_event):
                if blocked:  # модели вопрос не показываем и в историю не кладём
                    on_event(blocked)
                    return
                d.tick(on_event)
                on_event(d.turn(text))
                d.tick(on_event)
            new = self.events(s, run)
            return {"events": new if after is None else s.after(after), "at": at, "n": user["n"], "sid": s.sid}

    def updates(self, s, after):
        """{"events": история после номера after и только что сработавшее, "busy"}; None — вход сброшен."""
        if not s.lock.acquire(blocking=False):  # Ишка отвечает: напоминания придут вместе с ответом
            return None if s.closed else {"events": [], "busy": True, "sid": s.sid}
        try:
            if not self.refresh(s):
                return None
            new = self.events(s, s.dialog.tick)
            return {"events": new if after is None else s.after(after), "busy": False, "sid": s.sid}
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
    timeout = CLIENT_TIMEOUT
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

    def send_response(self, code, message=None):
        self.replied = True
        super().send_response(code, message)

    def send_json(self, code, obj, cookie=None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8", "replace")
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

    def renew(self, token, s):
        """Куки живёт IDLE секунд с последнего запроса, как и вход на сервере. Вход уже забыт (вышли в другой
        вкладке, пока шёл ответ) — не продлеваем: иначе вернули бы браузеру старую куки вместо новой."""
        return None if s.closed else self.cookie(token, IDLE[s.guest])

    # ---------- запрос ----------
    def token(self):
        # куки разбираем сами: SimpleCookie бросает весь заголовок, если в нём есть чужая «неправильная» куки
        # (на localhost браузер присылает и куки других сайтов с других портов)
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE:
                return v.strip().strip('"') or None
        return None

    def known_host(self):
        """Сайт открыт по адресу, который он знает: IP, localhost, имя Mac в сети (*.local) или --host.
        Чужое доменное имя — подмена DNS (DNS rebinding): такая страница не должна говорить с сайтом."""
        try:
            host = urlsplit("//" + (self.headers.get("Host") or "")).hostname or ""
            if host in ("localhost", self.app.args.host.lower()) or host.endswith(".local"):
                return True
            ipaddress.ip_address(host)
            return True
        except ValueError:
            return False

    def same_origin(self):
        """Запросы, меняющие что-то, — только со страниц самого сайта (защита от чужих сайтов)."""
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).netloc != self.headers.get("Host"):
            return False
        return (self.headers.get("Content-Type") or "").split(";")[0].strip() == "application/json"

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n < 0 or n > MAX_BODY:
            raise ValueError("неверная длина запроса" if n < 0 else "слишком большой запрос")
        raw = self.rfile.read(n) if n else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except RecursionError:
            raise ValueError("слишком сложный запрос")
        if not isinstance(data, dict):
            raise ValueError("ждали объект JSON")
        try:  # одиночные суррогаты (\ud83d) из JSON потом не записать ни в ответ, ни в базу
            json.dumps(data, ensure_ascii=False).encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("в тексте непонятные символы")
        return data

    def handle_safely(self, fn):
        """Вкладку закрыли во время ответа — молча; любая другая ошибка — 500 и подробности в окне сайта."""
        self.replied = False
        try:
            if not self.known_host():
                return self.error(403, "Открой сайт по адресу http://localhost:ПОРТ")
            fn()
        except DROPPED:
            pass
        except Exception:
            traceback.print_exc()
            if not self.replied:
                try:
                    self.error(500, "Ошибка на сервере: подробности в окне, где запущен server.py")
                except OSError:
                    pass

    def do_GET(self):
        self.handle_safely(self.get)

    def do_POST(self):
        self.handle_safely(self.post)

    def get(self):
        url = urlsplit(self.path)
        path = url.path
        app = self.app
        if path in ("/api/me", "/api/updates"):
            token = self.token()
            s = app.session(token)
            if s and path == "/api/me":
                out = app.me(s)
            elif s:
                after = parse_qs(url.query).get("after", [None])[0]
                try:
                    after = None if after is None else int(after)
                except ValueError:
                    return self.error(400, "after — номер события")
                out = app.updates(s, after)
            if not s or out is None:
                app.drop(token)
                return self.error(401, "Нужно войти")
            return self.send_json(200, out, self.renew(token, s))
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

    def post(self):
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
            after = data.get("after")
            if after is not None and (not isinstance(after, int) or isinstance(after, bool)):
                return self.error(400, "after — номер события")
            out = app.chat(s, text, after)
            if out is None:
                app.drop(token)
                return self.error(401, "Нужно войти")
            return self.send_json(200, out, self.renew(token, s))
        if path == "/api/reset":
            if not app.reset(s):
                return self.error(401, "Нужно войти")
            return self.send_json(200, {"ok": True}, self.renew(token, s))
        self.error(404, "Нет такого запроса")


class Server(ThreadingHTTPServer):
    request_queue_size = 64  # очередь соединений (по умолчанию 5: при всплеске запросов лишние сбрасывались)

    def handle_error(self, request, client_address):
        """Обрыв соединения (закрыли вкладку, телефон ушёл из сети) — не ошибка, трейсбек не печатаем."""
        if not isinstance(sys.exc_info()[1], DROPPED):
            super().handle_error(request, client_address)


def main():
    ap = argparse.ArgumentParser(description="Сайт Ишки")
    ap.add_argument("--host", default="127.0.0.1", help="0.0.0.0 — открыть сайт другим устройствам в этой сети")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--url", default="http://localhost:8080", help="адрес сервера модели (для --api openai — с /v1)")
    ap.add_argument("--api", choices=("completion", "openai"), default="completion",
                    help="completion — llama-server, промпт как при обучении; openai — Ollama, vLLM и т. п.")
    ap.add_argument("--name", default="ishka", help="имя модели (только для --api openai)")
    ap.add_argument("--db", help="база аккаунтов (по умолчанию db/ishka.sqlite)")
    ap.add_argument("--now", help="подменить время: '2026-10-15 08:00' (часы стоят, прошедшее не удаляется; "
                                  "записанное в чате сохраняется в базу)")
    ap.add_argument("--debug", action="store_true", help="печатать запросы, вызовы инструментов и ответы модели")
    args = ap.parse_args()
    if not (FRONTEND / "index.html").is_file():
        raise SystemExit(f"Нет страниц сайта: {FRONTEND}. Запусти server.py из папки backend проекта StudentIIk.")
    app = Handler.app = App(args)
    if not os.path.exists(app.db_path):
        print(f"Базы аккаунтов {app.db_path} нет: на сайте работает только вход без аккаунта. Завести аккаунт:\n"
              f"  {app.hint} --subgroup 2 --english Аксёнова")
    app.model.prepare(log=print)  # номер токена <tool_call> и шаблон чата; сервер модели не ответил — спросим позже
    httpd = Server((args.host, args.port), Handler)
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
