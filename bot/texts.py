from datetime import datetime
from zoneinfo import ZoneInfo

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
    parts = [user.last_name.strip(), user.first_name.strip()]
    name = " ".join(part for part in parts if part)
    if name:
        return name
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
    if lab.max_seats:
        lines.append(f"🪑 мест: {lab.max_seats}")
    return "\n".join(lines)


def queue_list(entries: list[QueueEntry]) -> str:
    if not entries:
        return "Пока никто не записался."
    return "\n".join(
        f"{entry.place}. {display_name(entry.user)}"
        for entry in sorted(entries, key=lambda item: item.place)
    )


HELP = (
    "Бот записывает на лабораторные.\n\n"
    "За 15 минут до открытия записи придёт напоминание. "
    "Сама запись открывается за 24 часа до лабы и идёт до её начала. "
    "Через 15 минут после открытия текущая очередь уходит в общую группу.\n\n"
    "/labs — ближайшие лабораторные\n"
    "/my — мои записи\n"
    "Для очереди лучше указать фамилию в профиле Telegram."
)

START = (
    "Привет! Я бот записи на лабораторные.\n\n"
    "Имя в очереди берётся из профиля Telegram.\n\n"
    + HELP
)
