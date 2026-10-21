"""Диалог с Ишкой: модель на сервере, настоящие инструменты из backend.py и запретные фразы.
Общий для chat.py (терминал) и server.py (сайт), поэтому Ишка отвечает в них одинаково.

Model — соединение с сервером модели, одно на всех: llama-server (/completion, промпт собираем сами ровно как
при обучении, шаблон чата берём из .gguf) или OpenAI-совместимый API (Ollama, vLLM). Вызвать инструмент или
ответить текстом, решает по P(<tool_call>) на первом токене ответа (порог CALL_THRESHOLD), а не жадным выбором.
Dialog — один разговор: история, номера показанных списков, сработавшие напоминания и утренний брифинг.
Пишет заметки по ходу (вызовы инструментов, ошибки сервера) через log(строка): chat.py их печатает.
"""
import datetime as dt, json, threading, urllib.error, urllib.request
from backend import Backend
from dates import with_words
from conversation import Conversation
from system_prompt import system
from records import reminder_line
from prompt_format import (TOOLS, tools_for, BROKEN, CALL_THRESHOLD, render, answer_prefix, parse_calls, answer_text,
                           call_msg, tool_msg, first_token_probs, tool_call_prob, pick_template)

NAMES = {t["function"]["name"] for t in TOOLS}
MAX_ROUNDS = 4    # вызовов инструментов подряд на один вопрос; в датасете больше 4 не бывает
KEEP_TURNS = 8    # сколько последних вопросов помнить, чтобы диалог не переполнил контекст сервера
N_PREDICT = 1024  # предел длины ответа в токенах; самые длинные ответы в датасете ~400
FALLBACK = "Не получилось ответить. Попробуй сформулировать иначе."
BRIEF_Q = "[авто] утренний брифинг"
BRIEF_FROM, BRIEF_TO = dt.time(7, 30), dt.time(12, 0)  # утренний брифинг сам — при первом входе за день в это время
TG_GRACE = 2  # минут: у кого подключён Telegram, напоминание сначала ждёт бота (notify.py), потом его покажет чат


class ContextOverflow(Exception):
    pass


class Blocked(Exception):
    """Модель сама написала запретную фразу (в ответе или в аргументах вызова): такой ход не выводим."""
    def __init__(self, hit):
        super().__init__(hit["phrase"])
        self.hit = hit


class ServerError(Exception):
    pass


def minute_now():
    return dt.datetime.now().replace(second=0, microsecond=0)


def _quiet(_):
    pass


class Model:
    """Сервер модели. Один объект на всех собеседников: шаблон чата и номер токена <tool_call> узнаём один раз."""

    def __init__(self, url="http://localhost:8080", api="completion", name="ishka", debug=False, log=print):
        self.url, self.api, self.name, self.debug, self.log = url, api, name, debug, log
        self.base = url.rstrip("/")
        if api == "completion" and self.base.endswith("/v1"):
            self.base = self.base[:-3]  # старый адрес вида http://localhost:8080/v1 тоже подходит
        self._http = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # локальный сервер: прокси не нужен
        self._lock = threading.Lock()
        self.tool_call_id = 151657  # номер <tool_call> у Qwen3; prepare() спросит у сервера
        self.template, self.prefix, self.ready = None, answer_prefix(), False
        if api == "openai":
            import openai
            self.openai = openai
            self.client = openai.OpenAI(base_url=self.base if self.base.endswith("/v1") else self.base + "/v1",
                                        api_key="local")

    def prepare(self, log=None):
        """При запуске: номер токена <tool_call> (заодно проверка, что сервер отвечает) и шаблон чата из .gguf.
        Если сервер не ответил, шаблон спросим перед первым ответом."""
        if self.api == "completion":
            self.tool_call_id = self.special_id("<tool_call>", 151657, log)
            self.template, self.prefix, self.ready = self.model_template(log)

    # ---------- llama-server: /completion с готовым промптом ----------
    def post(self, path, body=None):
        """POST с телом body (без тела — GET)."""
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data, {"Content-Type": "application/json"})
        try:
            with self._http.open(req, timeout=900) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                err = json.loads(raw).get("error") or {}
            except ValueError:
                err = {}
            err = err if isinstance(err, dict) else {"message": str(err)}
            msg = err.get("message") or raw[:300] or e.reason
            if err.get("type") == "exceed_context_size_error" or "context size" in msg:
                raise ContextOverflow(msg)
            if e.code == 404:
                msg = f"{self.base}{path} не найден — это не llama-server? Для Ollama/vLLM запусти с --api openai"
            raise ServerError(f"HTTP {e.code}: {msg}")
        except (urllib.error.URLError, OSError) as e:
            raise ServerError(f"сервер {self.base} не отвечает ({getattr(e, 'reason', e)}): запущен ли llama-server в окне 1?")

    def special_id(self, text, default, log=None):
        """Номер спецтокена в словаре модели (у Qwen3 <tool_call> = 151657)."""
        try:
            ids = self.post("/tokenize", {"content": text, "add_special": False, "parse_special": True}).get("tokens", [])
            return ids[0] if len(ids) == 1 and isinstance(ids[0], int) else default
        except ServerError as e:
            (log or self.log)(f"  [{e}]")
            return default

    def model_template(self, log=None):
        """Шаблон чата из .gguf (с ним модель обучалась) и начало ответа текстом в этом шаблоне:
        (шаблон или None — тогда chat_template.jinja, начало ответа, ответил ли сервер)."""
        log = log or self.log
        try:
            tpl = self.post("/props").get("chat_template")
        except ServerError:
            return None, answer_prefix(), False  # сервер ещё грузит модель или не запущен: спросим перед ответом
        tpl, prefix, why = pick_template(tpl)
        if why:
            log(f"  [{why}: беру chat_template.jinja]")
        if self.debug:
            log(f"  [шаблон: {'из .gguf' if tpl else 'chat_template.jinja'}, начало ответа {prefix!r}]")
        return tpl, prefix, True

    def generate(self, msgs, force_text, log=None):
        """Ответ модели на историю msgs: (текст, вызовы). force_text — только текст, без вызовов."""
        log = log or self.log
        if self.api == "openai":
            return self._generate_openai(msgs, force_text)
        if not self.ready:  # при запуске сервер не ответил
            with self._lock:
                if not self.ready:
                    self.template, self.prefix, self.ready = self.model_template(log)
        template, prefix = self.template, self.prefix
        prompt = render(msgs, add_generation_prompt=True, template=template)
        if not force_text:
            # Сначала один токен с вероятностями. Вызов — это один токен <tool_call>, а текст размазан по тысячам
            # первых слов, поэтому жадный выбор берёт вызов даже при P = 27%. Решаем по сумме (CALL_THRESHOLD).
            probs = first_token_probs(self.post("/completion", {"prompt": prompt, "n_predict": 1, "temperature": 0,
                                                                "n_probs": 10, "cache_prompt": True}))
            if probs is not None:  # сервер без вероятностей: остаётся жадный выбор
                p = tool_call_prob(probs, self.tool_call_id)
                if self.debug:
                    log(f"  [P(<tool_call>) = {p:.0%}]")
                force_text = p < CALL_THRESHOLD
        body = {"prompt": prompt, "n_predict": N_PREDICT,
                "temperature": 0, "stop": ["<|im_end|>"], "cache_prompt": True}  # temperature 0: как при оценке
        if force_text:
            # Ответ текстом: начинаем его так же, как начинались ответы при обучении (у официального шаблона
            # Qwen3-2507 это '<think>\n\n</think>\n\n'), и запрещаем токен <tool_call>
            body["prompt"] = prompt + prefix
            body["logit_bias"] = [[self.tool_call_id, False]]
        r = self.post("/completion", body)
        text = r.get("content") or ""
        if r.get("stop_type") == "limit":
            log(f"  [ответ обрезан: длиннее {N_PREDICT} токенов]" if (r.get("tokens_predicted") or 0) >= N_PREDICT
                else "  [ответ обрезан: кончился контекст сервера, запусти llama-server с -c 16384]")
        return text, parse_calls(text)

    # ---------- OpenAI-совместимый API: шаблон и вызовы разбирает сервер ----------
    def _generate_openai(self, msgs, force_text):
        kw = dict(model=self.name, messages=to_openai(msgs), tools=tools_for(msgs), temperature=0,
                  tool_choice="none" if force_text else "auto")
        try:
            m = self.client.chat.completions.create(**kw).choices[0].message
        except self.openai.BadRequestError as e:
            if "context" in str(e):
                raise ContextOverflow(str(e))
            raise ServerError(str(e))
        except self.openai.APIStatusError as e:  # 500 и прочие ошибки сервера
            raise ServerError(f"HTTP {e.status_code}: {e.message}")
        except self.openai.APIConnectionError as e:
            raise ServerError(f"сервер {self.url} не отвечает ({e})")
        text = m.content or ""
        calls = [{"name": c.function.name, "arguments": c.function.arguments} for c in m.tool_calls or []]
        return text, calls or parse_calls(text)  # сервер без разбора вызовов вернёт <tool_call> текстом


def to_openai(history):
    """История в формате обучения -> формат OpenAI (id вызовов, аргументы строкой)."""
    out, ids = [], []
    for i, m in enumerate(history):
        m = dict(m)
        if m.get("tool_calls"):
            ids = [f"call_{i}_{j}" for j in range(len(m["tool_calls"]))]
            m["tool_calls"] = [{"id": k, "type": "function", "function": {
                "name": c["function"]["name"], "arguments": json.dumps(c["function"]["arguments"], ensure_ascii=False)}}
                for k, c in zip(ids, m["tool_calls"])]
        elif m["role"] == "tool":
            m["tool_call_id"] = ids.pop(0) if ids else "call"
            m.pop("name", None)
        out.append(m)
    return out


def norm_args(args):
    try:
        while isinstance(args, str):  # иногда JSON приходит упакованным в строку дважды
            args = json.loads(args or "{}")
    except ValueError:
        pass
    return {} if args is None else args  # у инструментов без аргументов бывает null


class Dialog:
    """Один разговор с Ишкой. now — время начала (по умолчанию сейчас). frozen=True — время подменено (проверка):
    часы стоят, прошедшее не удаляется (напоминания показываются, но остаются в базе), а записанное в разговоре
    сохраняется в базу как обычно."""

    def __init__(self, model, guard, store, acc, now=None, frozen=False, debug=False, log=print):
        self.model, self.guard, self.store, self.acc, self.debug, self.log = model, guard, store, acc, debug, log
        self.frozen = frozen
        self.now = now or minute_now()
        self.conv = Conversation()  # номера показанных списков и «удалить все?» — на весь разговор
        self.backend = Backend(self.now, store.profile(acc), store, acc["id"], self.conv)
        self.msgs = [system(self.now, self.backend.profile)]  # история в формате обучения (как в датасете)
        self.last_raw = ""  # последний сырой ответ модели: покажем, если ответить не вышло
        self.tools = []     # вызовы инструментов в текущем ходе: [(имя, аргументы)]
        self.brief_day = acc.get("brief_date")
        self.brief_retry = [0, None]  # брифинг не получился: сколько раз и когда пробовать снова (через 5 минут, до 3 раз)
        self.told = set()  # с подменённым временем напоминания не удаляются: каждое показываем один раз

    def set_profile(self, profile):
        """Профиль поменяли в базе (accounts.py edit): дальше Ишка отвечает по новому."""
        if dict(profile) != self.backend.profile:
            self.backend.profile = dict(profile)
            self.msgs[0] = system(self.now, self.backend.profile)

    def reset(self):
        """Начать разговор заново: история и номера списков забываются, база не меняется."""
        self.conv = self.backend.conv = Conversation()
        self.msgs[1:] = []

    def forget_old(self, keep):
        """Оставляет system и последние keep вопросов пользователя (с ответами)."""
        users = [i for i, x in enumerate(self.msgs) if x["role"] == "user"]
        if len(users) > keep:
            del self.msgs[1:users[-keep]]

    def ask(self, force_text):
        try:
            text, calls = self.model.generate(self.msgs, force_text, self.log)
        except ContextOverflow:
            self.forget_old(1)  # не влезло: забываем всё, кроме текущего вопроса
            self.log("  [начало диалога забыто: не хватило контекста сервера]")
            text, calls = self.model.generate(self.msgs, force_text, self.log)
        self.last_raw = text
        if self.debug:  # запретную фразу не показываем и здесь
            self.log(f"  [модель: {text!r}]" if not self.guard.check(text or "") else "  [модель: скрыто, в ответе запретная фраза]")
        return text, calls

    def run_tools(self, calls):
        """Выполняет вызовы и дописывает их в историю так же, как в датасете."""
        results = []
        for name, args in calls:
            try:
                if name not in NAMES:
                    raise ValueError(f"нет такого инструмента: {name}")
                if not isinstance(args, dict):
                    raise ValueError("аргументы должны быть объектом")
                res = self.backend.call(name, args)
            except Exception as e:  # чужой инструмент или аргументы не по схеме
                res = {"error": f"{type(e).__name__}: {e}"}
            self.log(f"  [{name} {args}]")
            self.tools.append((name, args))
            results.append(tool_msg(name, res))
        self.msgs.append(call_msg(calls))
        self.msgs.extend(results)

    def answer(self):
        """Один вопрос: вызовы инструментов, пока модель не ответит текстом. Возвращает текст или None."""
        seen, force_text = set(), False
        for step in range(MAX_ROUNDS + 1):
            force_text = force_text or step >= MAX_ROUNDS
            text, calls = self.ask(force_text)
            if calls and not force_text:
                calls = [(c.get("name"), norm_args(c.get("arguments"))) for c in calls]
                key = json.dumps(calls, ensure_ascii=False, sort_keys=True, default=str)
                if any(name in BROKEN for name, _ in calls):
                    self.log("  [битый вызов инструмента: прошу ответить без инструментов]")
                    force_text = True
                elif key in seen:
                    # тот же вызов ещё раз: результат у модели уже есть, она зациклилась — просим ответить словами
                    self.log(f"  [повтор {calls[0][0]}: прошу ответить без инструментов]")
                    force_text = True
                else:
                    hit = next(filter(None, (self.guard.check_obj(args) for _, args in calls)), None)
                    if hit:  # инструмент с запретной фразой не выполняем
                        raise Blocked(hit)
                    seen.add(key)
                    self.run_tools(calls)
                continue
            clean = answer_text(text)
            hit = self.guard.check(clean or text or "")  # пустой ответ: проверяем сырой, его покажем вместо ответа
            if hit:
                raise Blocked(hit)
            return clean or None  # при force_text лишние вызовы просто отбрасываем
        return None

    def check(self, q):
        """Запретная фраза в вопросе: ответ-отказ (как у turn с kind="blocked") или None. Такой вопрос модели не
        показываем и в историю не кладём."""
        hit = self.guard.check(q)
        return blocked_reply(hit, "запретная фраза в вопросе") if hit else None

    def turn(self, q):
        """Один вопрос студента (или BRIEF_Q): ответ модели, отказ или «не получилось». Возвращает словарь:
        kind — "answer", "blocked" или "fallback"; text — что сказать (у "blocked" бывает None: промолчать);
        tools — [(имя, аргументы)], ok — получилось ли ответить; у "fallback" ещё error (ошибка сервера или None),
        interrupted и raw (сырой ответ модели)."""
        self.msgs.append({"role": "user", "content": q})
        self.forget_old(KEEP_TURNS)
        self.last_raw, self.tools = "", []
        snap = self.conv.state()  # номера списков и «удалить все?»: если ход уберём из истории, вернём как было
        self.conv.new_turn()
        interrupted = False
        try:
            text, err = self.answer(), None
        except (ContextOverflow, ServerError) as e:  # даже один вопрос не влез в контекст или сервер недоступен
            text, err = None, e
        except KeyboardInterrupt:  # Ctrl+C во время ответа: вопрос отменяется, чат продолжается
            text, err, interrupted = None, None, True
            self.log("  [прервано]")
        except Blocked as e:
            u = max(i for i, x in enumerate(self.msgs) if x["role"] == "user")
            if any(x["role"] == "tool" for x in self.msgs[u:]):
                # инструменты уже сработали (например, объявление ушло): ход оставляем, ответ модели заменяем отказом
                self.msgs.append({"role": "assistant", "content": e.hit["answer"] or self.guard.answer})
                # списков из этого хода студент не видел: «удали 2» — про прежний список; но сообщение было, и «да» на
                # «удалить все?» из позапрошлого сообщения уже не подтверждает
                self.conv.restore(dict(snap, turn=self.conv.turn))
            else:  # ход целиком убираем из истории, чтобы модель не продолжила эту тему
                del self.msgs[u:]
                self.conv.restore(snap)
            return blocked_reply(e.hit, "запретная фраза в ответе модели", self.tools)
        if text:
            self.msgs.append({"role": "assistant", "content": text})
            return {"kind": "answer", "text": text, "tools": self.tools, "ok": True}
        # Неудачный ход в историю не кладём: модель не должна видеть «не получилось» как образец ответа
        del self.msgs[max(i for i, x in enumerate(self.msgs) if x["role"] == "user"):]
        self.conv.restore(snap)
        return {"kind": "fallback", "text": FALLBACK, "tools": self.tools, "ok": False,
                "error": str(err) if err else None, "interrupted": interrupted, "raw": self.last_raw}

    def clock(self):
        """Часы Ишки — на текущую минуту (с --now стоят) и дата в системном промпте. После полуночи даты словами
        в прошлых ответах инструментов пересчитываются: «завтра в 09:00» становится «сегодня в 09:00»."""
        if not self.frozen:
            day, self.now = self.now.date(), minute_now()
            self.backend.now = self.now
            self.msgs[0] = system(self.now, self.backend.profile)
            if self.now.date() != day:
                self.msgs[1:] = [tool_msg(m["name"], with_words(json.loads(m["content"]), self.now)) if m["role"] == "tool" else m
                                 for m in self.msgs[1:]]

    def tick(self, on_event):
        """Перед каждым вопросом и раз в минуту, пока студент молчит: часы, сработавшие напоминания и утренний
        брифинг. on_event({"kind": "reminder", "text": ...}) или on_event(ответ turn с "brief": True) — сразу,
        по порядку. Возвращает True, если что-то было."""
        printed = False
        self.clock()
        # notify.py мог отметить брифинг или подключить Telegram
        cur = None if self.frozen else self.store.account(account_id=self.acc["id"])
        due = self.backend.due_reminders(delete=False)
        if cur and cur.get("tg_chat_id"):  # напоминание пришлёт бот; чат покажет его, только если бот не успел
            late = (self.now - dt.timedelta(minutes=TG_GRACE)).strftime("%Y-%m-%d %H:%M")
            due = [r for r in due if r["fire_at"] <= late]
        if due and not self.frozen:
            self.store.delete_notes(self.acc["id"], [r["id"] for r in due])
        for r in due:
            key = json.dumps(r, sort_keys=True)
            if key not in self.told:
                self.told.add(key)
                on_event({"kind": "reminder", "text": reminder_line(r)})
                printed = True
        if cur and (cur.get("brief_date") or "") > (self.brief_day or ""):
            self.brief_day = cur["brief_date"]
        today = self.now.date().isoformat()
        if (BRIEF_FROM <= self.now.time() < BRIEF_TO and self.brief_day != today
                and (self.brief_retry[1] is None or self.now >= self.brief_retry[1])):
            r = self.turn(BRIEF_Q)
            on_event(r | {"brief": True})
            printed = True
            self.brief_retry[0] += not r["ok"]
            if r["ok"] or self.brief_retry[0] >= 3:  # 3 неудачи — больше не пробуем, но день не отмечаем: перезапуск покажет брифинг
                self.brief_day, self.brief_retry[:] = today, [0, None]
                if r["ok"] and not self.frozen:
                    self.store.set_brief_date(self.acc["id"], today)
            else:
                self.brief_retry[1] = self.now + dt.timedelta(minutes=5)
        return printed


def blocked_reply(hit, why, tools=()):
    """Отказ из списка запретных фраз; пустой answer — промолчать (text None)."""
    return {"kind": "blocked", "text": hit["answer"] or None, "tools": list(tools), "ok": True, "why": why,
            "phrase": hit["phrase"], "group": hit["group"]}
