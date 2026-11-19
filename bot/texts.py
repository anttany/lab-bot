from datetime import datetime
from zoneinfo import ZoneInfo

from bot.config import DEFAULT_LAB_SEATS
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


HELP = (
    "Бот записывает на лабораторные.\n\n"
    "За 15 минут до открытия записи придёт напоминание. "
    "Сама запись открывается за 24 часа до лабы и идёт до её начала. "
    "На каждой лабе 25 мест — выбираете свободное сами. "
    "Через 15 минут после открытия текущая очередь уходит в общую группу.\n\n"
    "/labs — ближайшие лабораторные\n"
    "/my — мои записи\n"
    "/surname — сменить фамилию"
)

START = "Привет! Я бот записи на лабораторные."
ASK_SURNAME = "Напишите свою фамилию — она будет в очереди."
SURNAME_BAD = "Фамилия должна быть из букв, без цифр. Например: Иванов"
