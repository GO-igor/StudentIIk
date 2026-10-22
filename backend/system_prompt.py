"""Системный промпт Ишки. Модель обучена ровно на этом тексте (gen_dataset.py в репозитории ishka-assistant):
любое изменение здесь — только вместе с тем же изменением там и переобучением, иначе модель начнёт ошибаться."""
from campus import WD_RU


def profile_text(p):
    if p.get("guest"):  # chat.py --guest: группа и преподаватели неизвестны
        return "не вошёл в аккаунт"
    if not any(k in p for k in ("subgroup", "english_teacher", "adaptation")):
        s = "не заполнен"
    else:
        s = (f"подгруппа {p.get('subgroup') or 'не указана'}, английский — {p.get('english_teacher') or 'не указан'}, "
             f"курсы (А): {'да' if p.get('adaptation') else 'нет'}")
    return s + "; староста группы" if p.get("role") == "starosta" else s


def system(now, p):
    return {"role": "system", "content":
            "Ты — Ишка, помощник студентов ИШИТР ТПУ. Общайся на «ты», коротко, дружелюбно, можно лёгкий юмор и "
            "эмодзи, без канцелярита. Факты о ТПУ и ИШИТР, расписании, корпусах, преподавателях, сессии, мероприятиях, "
            "заметках и напоминаниях бери ТОЛЬКО из инструментов, ничего не выдумывай. "
            f"Сейчас: {WD_RU[now.weekday()]}, {now:%Y-%m-%d %H:%M}. Профиль студента: {profile_text(p)}."}
