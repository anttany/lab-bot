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
    "На ИТиП у занятия две очереди: сначала текущая лаба до дедлайна, "
    "потом старые. Можно записаться в обе.\n\n"
    "/labs — ближайшие лабораторные\n"
    "/my — мои записи\n"
    "/surname — сменить фамилию"
)

START = "Привет! Я бот записи на лабораторные."
ASK_SURNAME = "Напишите свою фамилию — она будет в очереди."
SURNAME_BAD = "Фамилия должна быть из букв, без цифр. Например: Иванов"
