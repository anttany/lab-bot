from datetime import datetime
from zoneinfo import ZoneInfo

from bot.config import DEFAULT_LAB_SEATS
from bot.itip import entries_for, itip_session_for, queue_specs
from bot.models import Lab, QueueEntry, User

WEEKDAYS = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
)


def format_dt(dt: datetime, tz: ZoneInfo) -> str:
    local = dt.astimezone(tz)
    weekday = WEEKDAYS[local.weekday()]
    return f"{local.strftime('%d.%m.%Y %H:%M')} ({weekday})"


def display_name(user: User) -> str:
    surname = user.last_name.strip()
    if surname:
        return surname
    if user.username:
        return f"@{user.username}"
    return f"id {user.tg_id}"


def lab_title(lab: Lab) -> str:
    extra = []
    if lab.classroom:
        extra.append(f"ауд. {lab.classroom}")
    if lab.teacher:
        extra.append(lab.teacher)
    suffix = f" · {' · '.join(extra)}" if extra else ""
    return f"{lab.subject.name}{suffix}"


def lab_card(lab: Lab, tz: ZoneInfo) -> str:
    lines = [
        f"<b>{lab.subject.name}</b>",
        f"📅 {format_dt(lab.starts_at, tz)}",
    ]
    if lab.classroom:
        lines.append(f"📍 ауд. {lab.classroom}")
    if lab.teacher:
        lines.append(f"👤 {lab.teacher}")
    session = itip_session_for(lab, tz)
    if session is not None:
        specs = queue_specs(lab, tz)
        lines.append(session.headline)
        lines.append(f"Сначала {specs[0].short}, затем {specs[1].short}")
        lines.append(f"🪑 по {lab.max_seats or DEFAULT_LAB_SEATS} мест в каждой очереди")
    else:
        lines.append(f"🪑 мест: {lab.max_seats or DEFAULT_LAB_SEATS}")
    return "\n".join(lines)


def lab_preview(lab: Lab, tz: ZoneInfo, *, open_signup: bool) -> str:
    status = "запись открыта" if open_signup else "ожидает открытия"
    mark = "🟢" if open_signup else "⏳"
    lines = [
        f"{mark} <b>{lab.subject.name}</b>",
        f"{format_dt(lab.starts_at, tz)} · {status}",
    ]
    session = itip_session_for(lab, tz)
    if session is not None:
        specs = queue_specs(lab, tz)
        lines.append(f"{session.headline} · {specs[0].short} → {specs[1].short}")
    return "\n".join(lines)


def queue_list(entries: list[QueueEntry], max_seats: int | None = None) -> str:
    seats = max_seats or DEFAULT_LAB_SEATS
    by_place = {entry.place: entry for entry in entries}
    lines: list[str] = []
    for place in range(1, seats + 1):
        entry = by_place.get(place)
        if entry:
            lines.append(f"{place}. {display_name(entry.user)}")
        else:
            lines.append(f"{place}. —")
    return "\n".join(lines)


def queues_text(lab: Lab, tz: ZoneInfo) -> str:
    specs = queue_specs(lab, tz)
    blocks = []
    for spec in specs:
        title = spec.title if len(specs) > 1 else "Очередь"
        blocks.append(f"<b>{title}</b>\n{queue_list(entries_for(lab, spec.kind), lab.max_seats)}")
    return "\n\n".join(blocks)


HELP = (
    "Бот записывает на лабораторные.\n\n"
    "За 15 минут до открытия записи придёт напоминание. "
    "Сама запись открывается за 24 часа до лабы и идёт до её начала. "
    "На каждой лабе 25 мест — выбираете свободное сами. "
    "Через 15 минут после открытия текущая очередь уходит в общую группу.\n\n"
    "Своё место отмечено галочкой. Нажмите его ещё раз, чтобы сняться. "
    "Кнопка «Отдать место» отправляет приглашение по юзернейму: "
    "место перейдёт только если человек его примет. "
    "Принять можно несколько мест на одну лабу.\n\n"
    "На ИТиП у занятия две очереди: сначала текущая лаба до дедлайна, "
    "потом старые. Можно записаться в обе.\n\n"
    "/labs — ближайшие лабораторные\n"
    "/my — мои записи\n"
    "/surname — сменить фамилию"
)

SEAT_HINT = "Своё место отмечено галочкой. Нажмите его ещё раз, чтобы сняться с очереди."
GIVE_BAD = "Нужен юзернейм в Telegram, например @ivanov."
GIVE_SELF = "Себе отдать место нельзя."
GIVE_CANCELLED = "Передача места отменена. Место остаётся вашим."
GIVE_UNDELIVERED = (
    "Не удалось отправить приглашение. Человек не открыл бота или заблокировал его.\n"
    "Место остаётся вашим."
)
GIVE_CLOSED = "Запись уже закрыта, отдать место нельзя."
GIVE_MISSING = "Это место уже не ваше."
INVITE_STALE = "Приглашение недействительно: место уже не у того, кто его отдавал."
MANY_SEATS = "У вас уже несколько мест в этой очереди. Нажмите на своё, чтобы сняться."


def person_label(user: User) -> str:
    surname = user.last_name.strip()
    if surname and user.username:
        return f"{surname} (@{user.username})"
    return display_name(user)


def at_username(user: User) -> str:
    if user.username:
        return f"@{user.username}"
    return display_name(user)


def give_ask(place: int, queue: str) -> str:
    return (
        f"Кому отдать <b>{place}</b> место?\n"
        f"{queue}\n\n"
        "Напишите юзернейм в Telegram, например @ivanov\n\n"
        "/cancel — оставить место себе"
    )


def give_unknown(username: str) -> str:
    return (
        f"Не нашёл {username} среди тех, кто уже открывал бота.\n"
        "Пусть сначала нажмёт /start."
    )


def give_sent(username: str, place: int) -> str:
    return (
        f"Приглашение на <b>{place}</b> место отправлено {username}.\n"
        "Пока его не приняли, место остаётся вашим."
    )


def invite_text(giver: str, place: int, queue: str, card: str) -> str:
    return (
        f"{giver} отдаёт вам <b>{place}</b> место.\n\n"
        f"{card}\n\n"
        f"<b>{queue}</b>\n"
        "Примите приглашение — место перейдёт вам. "
        "На одну лабу можно принять несколько мест."
    )


def invite_accepted_to(place: int, queue: str, card: str) -> str:
    return f"Вы приняли <b>{place}</b> место.\n\n{card}\n\n<b>{queue}</b>"


def invite_accepted_from(username: str, place: int) -> str:
    return f"{username} принял ваше <b>{place}</b> место. Оно больше не ваше."


def invite_declined_to(place: int) -> str:
    return f"Вы отклонили <b>{place}</b> место."


def invite_declined_from(username: str, place: int) -> str:
    return f"{username} отклонил <b>{place}</b> место. Оно остаётся за вами."


def invite_withdrawn(giver: str, place: int) -> str:
    return f"{giver} больше не отдаёт <b>{place}</b> место. Приглашение отменено."


def left_notice(place: int) -> str:
    return f"Вы снялись с <b>{place}</b> места."

START = "Привет! Я бот записи на лабораторные."
ASK_SURNAME = "Напишите свою фамилию — она будет в очереди."
SURNAME_BAD = "Фамилия должна быть из букв, без цифр. Например: Иванов"
