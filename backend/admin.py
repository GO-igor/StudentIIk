"""Страница администратора сайта (/admin): аккаунты, записи студентов, объявления старост и справочники db/*.json.
Вход — по ссылке с кодом: server.py придумывает код при каждом запуске и печатает ссылку в своём окне, поэтому
войти может только тот, кто запустил сайт. Код из прошлого запуска уже не подходит.

API (JSON; только после входа по коду, куки ishka_admin; всё, что меняет данные, печатается в окне сайта):
    POST /api/admin/enter {code}   POST /api/admin/logout
    GET  /api/admin/state — аккаунты, группы, преподаватели английского, справочники
    POST /api/admin/account/add {login, name, group, subgroup, role, english, adaptation, password?}
         /api/admin/account/edit {login, ...те же поля без password}   /api/admin/account/password {login, password?}
         /api/admin/account/remove {login}   /api/admin/account/import {csv}
    GET  /api/admin/notes?login=  POST /api/admin/notes/delete {login, ids}
    GET  /api/admin/posts         POST /api/admin/posts/delete {ids}   POST /api/admin/cleanup
    GET  /api/admin/file?name=    POST /api/admin/file {name, text, mtime, check?}
Пустой password — пароль придумывается и возвращается один раз (в базе только хеш). Справочники сохраняются,
только если файл правильный; прежняя версия остаётся рядом (university.json.bak и т. п.)."""
import csv, datetime as dt, io, json, os, pathlib, re, shutil, sqlite3, unicodedata
from contextlib import contextmanager
import campus
from campus import ENG_TEACHERS, SUBJECTS, english_teacher
from guard import _parse as parse_blocklist
from store import ROLES, Store, group_key, login_key, new_password

DB_DIR = pathlib.Path(__file__).resolve().parent / "db"
MIN_PASSWORD, MAX_FIELD, MAX_IMPORT = 6, 100, 500
ROLE_RU = {"student": "студент", "starosta": "староста"}
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_TIME = re.compile(r"([01]\d|2[0-3]):[0-5]\d\Z")
_JSON_ERRORS = {  # сообщения json по-русски
    "Expecting ',' delimiter": "не хватает запятой",
    "Expecting ':' delimiter": "не хватает двоеточия после имени поля",
    "Expecting property name enclosed in double quotes": "имя поля должно быть в двойных кавычках (или лишняя запятая перед })",
    "Expecting value": "ожидалось значение (лишняя запятая или пропущено значение)",
    "Extra data": "лишний текст после конца файла",
    "Unterminated string starting at": "не закрыта кавычка",
    "Invalid control character at": "перенос строки внутри текста в кавычках: замени его на \\n",
    "Invalid \\escape": "неверная обратная косая черта: напиши \\\\",
}


class Problem(Exception):
    """Ошибка для страницы: HTTP-код и текст (и для JSON — место ошибки)."""
    def __init__(self, code, text, **extra):
        super().__init__(text)
        self.code, self.text, self.extra = code, text, extra


def _text(v, what, limit=MAX_FIELD):
    if v is None:
        return ""
    if not isinstance(v, (str, int)) or isinstance(v, bool):
        raise Problem(400, f"{what}: ждали текст")
    v = unicodedata.normalize("NFC", str(v)).strip()
    if len(v) > limit:
        raise Problem(400, f"{what}: не длиннее {limit} символов")
    return v


def _login(data):
    login = _text(data.get("login"), "Логин", 64)
    if not login:
        raise Problem(400, "Введи логин")
    if re.search(r"\s", login):
        raise Problem(400, "В логине не должно быть пробелов")
    return login


def _password(v):
    if v in (None, ""):
        return None
    if not isinstance(v, str):
        raise Problem(400, "Пароль: ждали текст")
    if len(v) < MIN_PASSWORD:
        raise Problem(400, f"Пароль короче {MIN_PASSWORD} символов")
    if len(v) > 200:
        raise Problem(400, "Слишком длинный пароль")
    return v


def _profile(data):
    """Поля формы -> аргументы Store.update_account. Неверное поле — Problem с понятным текстом."""
    group = _text(data.get("group"), "Группа", 20)
    if not group_key(group):
        raise Problem(400, "Укажи группу, например 8К51")
    role = data.get("role") or "student"
    if role not in ROLES:
        raise Problem(400, "Роль — студент или староста")
    sub = data.get("subgroup")
    if sub in (None, ""):
        sub = None
    else:
        try:
            sub = int(sub)
        except (TypeError, ValueError):
            sub = 0
        if sub not in (1, 2, 3) or isinstance(data.get("subgroup"), bool):
            raise Problem(400, "Подгруппа — 1, 2 или 3 (или не указана)")
    english = _text(data.get("english"), "Преподаватель английского")
    if english:
        full = english_teacher(english)
        if not full:
            raise Problem(400, f"Преподаватель английского «{english}» не найден, варианты: {', '.join(ENG_TEACHERS)}")
        english = full
    return {"grp": group_key(group), "role": role, "name": _text(data.get("name"), "Имя") or None, "subgroup": sub,
            "english_teacher": english or None, "adaptation": bool(data.get("adaptation"))}


def _json_error(text, e):
    msg = next((ru for en, ru in _JSON_ERRORS.items() if e.msg.startswith(en)), e.msg)
    pos = len(text[:e.pos].encode("utf-16-le")) // 2  # место в тексте для страницы (JavaScript считает в UTF-16)
    return Problem(400, f"Ошибка в JSON, строка {e.lineno}, символ {e.colno}: {msg}", pos=pos, line=e.lineno)


# ---------- проверка справочников: файл, который сломает Ишку, не сохраняем ----------
def _check_date(v, where):
    if not isinstance(v, str) or not _DATE.match(v):
        raise ValueError(f"{where}: дата пишется так: \"2026-10-24\"")
    try:
        dt.date.fromisoformat(v)
    except ValueError:
        raise ValueError(f"{where}: такой даты нет ({v})")


def _check_time(v, where):
    if v is not None and (not isinstance(v, str) or not _TIME.match(v)):
        raise ValueError(f"{where}: время пишется так: \"16:30\" (или null)")


def _check_strings(item, keys, where):
    for k in keys:
        if item.get(k) is not None and not isinstance(item[k], str):
            raise ValueError(f"{where}: {k} — текст в кавычках (или null)")


def check_university(data):
    if not isinstance(data, dict):
        raise ValueError("нужен объект { \"раздел\": {...} } в фигурных скобках")


def check_blocklist(data):
    try:
        parse_blocklist(data)
    except (TypeError, ValueError) as e:
        raise ValueError(str(e))


def check_events(data):
    if not isinstance(data, dict) or not isinstance(data.get("events"), list):
        raise ValueError("нужен объект с полем events — списком мероприятий в [ ]")
    for i, e in enumerate(data["events"], 1):
        where = f"мероприятие №{i}"
        if not isinstance(e, dict):
            raise ValueError(f"{where}: каждое мероприятие — объект в {{ }}")
        if not isinstance(e.get("title"), str) or not e["title"].strip():
            raise ValueError(f"{where}: нет названия (title)")
        where += f" «{e['title']}»"
        _check_date(e.get("date"), f"{where}, date")
        _check_time(e.get("time"), f"{where}, time")
        _check_strings(e, ("kind", "place", "description", "url"), where)


def check_session(data):
    if not isinstance(data, dict):
        raise ValueError("нужен объект с полями start, end и exams")
    _check_date(data.get("start"), "start (первый день сессии)")
    _check_date(data.get("end"), "end (последний день сессии)")
    if data["end"] < data["start"]:
        raise ValueError("end раньше start")
    exams = data.get("exams", [])
    if not isinstance(exams, list):
        raise ValueError("exams — список экзаменов в [ ]")
    for i, e in enumerate(exams, 1):
        where = f"экзамен №{i}"
        if not isinstance(e, dict):
            raise ValueError(f"{where}: каждый экзамен — объект в {{ }}")
        if e.get("subject") not in SUBJECTS:
            raise ValueError(f"{where}: subject — код предмета, один из: {', '.join(SUBJECTS)}")
        if e.get("date") is not None:
            _check_date(e["date"], f"{where}, date")
        _check_time(e.get("time"), f"{where}, time")
        _check_strings(e, ("kind",), where)
        for k in ("building", "room"):
            if e.get(k) is not None and (not isinstance(e[k], (str, int)) or isinstance(e[k], bool)):
                raise ValueError(f"{where}: {k} — текст в кавычках (или null)")


def apply_events(data):
    campus.EVENTS[:] = data["events"]  # тот же список, что у всех открытых разговоров: новые мероприятия видны сразу


def apply_session(data):
    campus.SESSION.update(data)  # сначала новое, потом убираем лишнее: разговор в другом потоке не увидит пустой словарь
    for k in [k for k in campus.SESSION if k not in data]:
        del campus.SESSION[k]


# имя -> (название на странице, проверка, применить на ходу или None — справочник и так перечитывается сам)
FILES = {
    "university.json": ("Справка о ТПУ и ИШИТР", check_university, None),
    "events.json": ("Мероприятия", check_events, apply_events),
    "session.json": ("Сессия и экзамены", check_session, apply_session),
    "blocklist.json": ("Запретные фразы", check_blocklist, None),
}


class Admin:
    def __init__(self, db_path, now, log=print):
        """now() — текущее время сайта (с --now — подменённое), log — куда писать, что сделал администратор."""
        self.db_path, self.now, self.log = db_path, now, log

    @contextmanager
    def store(self):
        """База на один запрос: открыли, сделали, закрыли (у входов на сайте — свои соединения)."""
        st = Store(self.db_path, check_same_thread=False)
        try:
            yield st
        finally:
            st.db.close()

    def note(self, line):
        self.log(f"[админ] {line}")

    # ---------- чтение ----------
    def get(self, what, query):
        """(код, ответ) для GET /api/admin/<what>."""
        if what == "state":
            return 200, self.state()
        if what == "notes":
            return 200, self.notes(query.get("login", [""])[0])
        if what == "posts":
            return 200, self.posts()
        if what == "file":
            return 200, self.read_file(query.get("name", [""])[0])
        raise Problem(404, "Нет такого запроса")

    def state(self):
        with self.store() as st:
            counts = dict(st.db.execute("SELECT account_id, COUNT(*) FROM notes GROUP BY account_id").fetchall())
            created = dict(st.db.execute("SELECT id, created FROM accounts").fetchall())
            accounts = [a | {"notes": counts.get(a["id"], 0), "created": created.get(a["id"]),
                             "can_login": bool(a["can_login"]), "telegram": bool(a["telegram"]),
                             "adaptation": bool(a["adaptation"])} for a in st.accounts()]
            posts = dict(st.db.execute("SELECT grp, COUNT(*) FROM group_posts GROUP BY grp").fetchall())
        groups = sorted({a["grp"] for a in accounts} | set(posts))
        return {"db": str(pathlib.Path(self.db_path).resolve()), "now": self.now().strftime("%Y-%m-%d %H:%M"),
                "accounts": accounts, "groups": [{"name": g, "posts": posts.get(g, 0)} for g in groups],
                "teachers": ENG_TEACHERS,
                "files": [{"name": n, "title": t} for n, (t, _, _) in FILES.items()]}

    def account(self, st, login):
        acc = st.account(login=login)
        if not acc:
            raise Problem(404, f"Аккаунта «{login}» нет")
        return acc

    def notes(self, login):
        with self.store() as st:
            acc = self.account(st, login)
            out = []
            for n in st.notes(acc["id"]):
                kind = "homework" if n["subject"] else "reminder" if n["fire_at"] else "note"
                subject = SUBJECTS.get(n["subject"], {}).get("name", n["subject"]) if n["subject"] else None
                out.append({"id": n["id"], "kind": kind, "text": n["text"], "fire_at": n["fire_at"], "subject": subject,
                            "class_at": n["class_at"], "created": n["created"]})
        return {"login": acc["login"], "notes": out}

    def posts(self):
        with self.store() as st:
            rows = st.db.execute("SELECT p.*, a.login AS author FROM group_posts p LEFT JOIN accounts a ON a.id = p.author_id "
                                 "ORDER BY p.grp, p.until, p.id").fetchall()
        out = []
        for r in rows:
            try:
                data = json.loads(r["data"])
            except ValueError:
                data = {}
            if r["kind"] == "homework":
                subject = SUBJECTS.get(data.get("subject"), {}).get("name", data.get("subject"))
                text, at = f"{subject}: {data.get('task') or ''}", data.get("due")
            else:
                text, at = data.get("text") or "", data.get("at")
            out.append({"id": r["id"], "group": r["grp"], "subgroup": r["subgroup"], "kind": r["kind"], "text": text,
                        "at": at, "until": r["until"], "created": r["created"], "author": r["author"]})
        return {"posts": out}

    def read_file(self, name):
        if name not in FILES:
            raise Problem(404, "Нет такого справочника")
        path = DB_DIR / name
        try:
            return {"name": name, "text": path.read_text(encoding="utf-8"), "mtime": str(path.stat().st_mtime_ns)}
        except FileNotFoundError:
            return {"name": name, "text": "", "mtime": None}

    # ---------- изменения ----------
    def post(self, what, data):
        """(код, ответ) для POST /api/admin/<what>."""
        actions = {"account/add": self.add, "account/edit": self.edit, "account/password": self.password,
                   "account/remove": self.remove, "account/import": self.import_csv, "notes/delete": self.delete_notes,
                   "posts/delete": self.delete_posts, "cleanup": self.cleanup, "file": self.save_file}
        if what not in actions:
            raise Problem(404, "Нет такого запроса")
        return 200, actions[what](data)

    def add(self, data):
        login, profile = _login(data), _profile(data)
        pw = _password(data.get("password"))
        generated = pw is None
        pw = pw or new_password()
        with self.store() as st:
            if st.account(login=login):
                raise Problem(409, f"Логин «{login_key(login)}» уже занят")
            try:
                st.add_account(login, pw, profile.pop("grp"), **profile)
            except sqlite3.IntegrityError:
                raise Problem(409, f"Логин «{login_key(login)}» уже занят")
            acc = st.account(login=login)
        self.note(f"добавлен аккаунт {acc['login']} ({ROLE_RU[acc['role']]}, группа {acc['grp']})")
        return {"login": acc["login"], "password": pw if generated else None}

    def edit(self, data):
        login, profile = _login(data), _profile(data)
        with self.store() as st:
            self.account(st, login)
            st.update_account(login, **profile)
        self.note(f"изменён аккаунт {login_key(login)}")
        return {"login": login_key(login)}

    def password(self, data):
        login, pw = _login(data), _password(data.get("password"))
        generated = pw is None
        pw = pw or new_password()
        with self.store() as st:
            acc = self.account(st, login)
            st.set_password(login, pw)
        self.note(f"новый пароль у {acc['login']}")
        return {"login": acc["login"], "password": pw if generated else None, "telegram_off": bool(acc.get("tg_chat_id"))}

    def remove(self, data):
        login = _login(data)
        with self.store() as st:
            self.account(st, login)
            st.remove_account(login)
        self.note(f"удалён аккаунт {login_key(login)}")
        return {"login": login_key(login)}

    def import_csv(self, data):
        """Таблица как у accounts.py import: login,password,role,group,subgroup,english,name,adaptation."""
        text = data.get("csv")
        if not isinstance(text, str) or not text.strip():
            raise Problem(400, "Таблица пустая")
        text = text.lstrip("﻿")
        head = text.split("\n", 1)[0]
        rows = csv.DictReader(io.StringIO(text, newline=""), delimiter=";" if head.count(";") > head.count(",") else ",")
        if "login" not in [(k or "").strip().lower() for k in rows.fieldnames or []]:
            raise Problem(400, "В первой строке таблицы нет колонки login. Нужен заголовок: "
                               "login,password,role,group,subgroup,english,name,adaptation")
        added, skipped = [], []
        with self.store() as st:
            for row in rows:
                line = rows.line_num
                row = {k.strip().lower(): (v or "").strip() for k, v in row.items() if isinstance(k, str)}
                if not any(row.values()):
                    continue
                if len(added) + len(skipped) >= MAX_IMPORT:
                    raise Problem(400, f"За раз — не больше {MAX_IMPORT} строк (добавлено уже {len(added)})")
                role = (row.get("role") or "student").lower()
                fields = {"login": row.get("login"), "group": row.get("group"), "name": row.get("name"),
                          "subgroup": row.get("subgroup"), "english": row.get("english"),
                          "role": {"староста": "starosta", "студент": "student"}.get(role, role),
                          "adaptation": row.get("adaptation", "").lower() in ("1", "yes", "да", "true", "+")}
                try:
                    login, profile = _login(fields), _profile(fields)
                    pw = _password(row.get("password")) or new_password()
                    if st.account(login=login):
                        raise Problem(409, "логин уже занят")
                    st.add_account(login, pw, profile.pop("grp"), **profile)
                except Problem as e:
                    skipped.append({"line": line, "login": row.get("login") or "", "why": e.text[:1].lower() + e.text[1:]})
                    continue
                except sqlite3.IntegrityError:
                    skipped.append({"line": line, "login": row.get("login") or "", "why": "логин уже занят"})
                    continue
                added.append({"login": login_key(login), "password": None if row.get("password") else pw})
        self.note(f"из таблицы добавлено аккаунтов: {len(added)}, пропущено строк: {len(skipped)}")
        return {"added": added, "skipped": skipped}

    def delete_notes(self, data):
        login, ids = _login(data), self.ids(data)
        with self.store() as st:
            acc = self.account(st, login)
            n = st.delete_notes(acc["id"], ids)
        self.note(f"удалено записей у {acc['login']}: {n}")
        return {"deleted": n}

    def delete_posts(self, data):
        ids = self.ids(data)
        with self.store() as st:
            n = st.db.execute(f"DELETE FROM group_posts WHERE id IN ({', '.join('?' * len(ids))})", ids).rowcount
            st.db.commit()
        self.note(f"удалено объявлений и Д/З старост: {n}")
        return {"deleted": n}

    @staticmethod
    def ids(data):
        ids = data.get("ids")
        if not isinstance(ids, list) or not ids or len(ids) > 1000 or \
                not all(isinstance(i, int) and not isinstance(i, bool) for i in ids):
            raise Problem(400, "Не выбрано, что удалить")
        return ids

    def cleanup(self, data):
        now = self.now().strftime("%Y-%m-%d %H:%M")
        with self.store() as st:
            posts, notes = st.purge_posts(now), st.purge_notes(now)
        self.note(f"удалено прошедшее: объявлений и Д/З старост {posts}, напоминаний {notes}")
        return {"posts": posts, "notes": notes}

    def save_file(self, data):
        """Проверить и (без check) сохранить справочник. mtime — версия, которую открыли: файл успели поменять
        (вручную или в другой вкладке) — не затираем, 409."""
        name, text = data.get("name"), data.get("text")
        if name not in FILES:
            raise Problem(404, "Нет такого справочника")
        if not isinstance(text, str):
            raise Problem(400, "Нет текста файла")
        title, check, apply = FILES[name]
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as e:
            raise _json_error(text, e)
        except RecursionError:
            raise Problem(400, "Слишком глубокая вложенность")
        try:
            check(parsed)
        except ValueError as e:
            raise Problem(400, f"Не сохранено: {e}")
        if data.get("check"):
            return {"ok": True}
        path = DB_DIR / name
        try:
            mtime = str(path.stat().st_mtime_ns)
        except FileNotFoundError:
            mtime = None
        if data.get("mtime") != mtime:
            raise Problem(409, "Файл изменили, пока он был открыт здесь (вручную или в другой вкладке). "
                               "Скопируй свои правки и загрузи файл заново.")
        tmp = path.with_name(name + ".tmp")
        tmp.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
        if mtime is not None:
            shutil.copy2(path, path.with_name(name + ".bak"))
        os.replace(tmp, path)  # целиком: Ишка не прочитает полфайла
        if apply:
            apply(parsed)
        self.note(f"сохранён справочник {name} ({title})")
        return {"ok": True, "mtime": str(path.stat().st_mtime_ns)}
