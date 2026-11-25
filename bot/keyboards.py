from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.itip import default_kind, has_dual_queues, queue_specs, short_subject, spec_by_kind
from bot.models import Lab
from bot.queue import as_utc, seat_count, taken_places

BUTTON_LIMIT = 64


def _lab_when(lab: Lab, tz: ZoneInfo) -> str:
    return as_utc(lab.starts_at).astimezone(tz).strftime("%d.%m %H:%M")


def _button_text(prefix: str, name: str, when: str) -> str:
    extra = f" · {when}"
    room = BUTTON_LIMIT - len(prefix) - len(extra) - 3
    if room < 1:
        return f"{prefix}{extra}"[:BUTTON_LIMIT]
    label = name if len(name) <= room else name[: room - 1] + "…"
    return f"{prefix} · {label}{extra}"


def labs_keyboard(
    labs: list[Lab],
    *,
    can_sign: dict[int, bool],
    tz: ZoneInfo,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for lab in labs:
        name = short_subject(lab)
        when = _lab_when(lab, tz)
        if can_sign.get(lab.id):
            action = "Выбрать очередь" if has_dual_queues(lab) else "Выбрать место"
            builder.row(
                InlineKeyboardButton(
                    text=_button_text(action, name, when),
                    callback_data=f"signup:{lab.id}",
                )
            )
        builder.row(
            InlineKeyboardButton(
                text=_button_text("Очередь", name, when),
                callback_data=f"queue:{lab.id}",
            )
        )
    return builder.as_markup()


def queues_keyboard(lab: Lab) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for spec in queue_specs(lab):
        builder.row(
            InlineKeyboardButton(
                text=spec.title,
                callback_data=f"qpick:{lab.id}:{spec.kind}",
            )
        )
    builder.row(
        InlineKeyboardButton(text="Обе очереди", callback_data=f"queue:{lab.id}"),
    )
    return builder.as_markup()


def seats_keyboard(
    lab: Lab,
    user_id: int | None = None,
    kind: str | None = None,
) -> InlineKeyboardMarkup:
    kind = kind or default_kind(lab)
    builder = InlineKeyboardBuilder()
    occupied = taken_places(lab, kind)
    for place in range(1, seat_count(lab) + 1):
        entry = occupied.get(place)
        if entry is None:
            text = str(place)
            data = f"seat:{lab.id}:{kind}:{place}"
        elif user_id is not None and entry.user_id == user_id:
            text = f"✅{place}"
            data = f"seat:{lab.id}:{kind}:{place}"
        else:
            text = f"🔒{place}"
            data = f"busy:{lab.id}:{kind}:{place}"
        builder.add(InlineKeyboardButton(text=text, callback_data=data))
    builder.adjust(5)
    if user_id is not None:
        for place, entry in sorted(occupied.items()):
            if entry.user_id == user_id:
                builder.row(
                    InlineKeyboardButton(
                        text=f"Отдать {place} место",
                        callback_data=f"give:{lab.id}:{kind}:{place}",
                    )
                )
    if has_dual_queues(lab):
        builder.row(
            InlineKeyboardButton(text="К очередям", callback_data=f"signup:{lab.id}"),
        )
    builder.row(
        InlineKeyboardButton(text="Очередь", callback_data=f"queue:{lab.id}"),
    )
    return builder.as_markup()


def signup_keyboard(lab: Lab, user_id: int | None = None) -> InlineKeyboardMarkup:
    if has_dual_queues(lab):
        return queues_keyboard(lab)
    return seats_keyboard(lab, user_id, default_kind(lab))


def invite_keyboard(invite_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="Принять место", callback_data=f"iok:{invite_id}"),
        InlineKeyboardButton(text="Отклонить", callback_data=f"ino:{invite_id}"),
    )
    return builder.as_markup()


def queue_kind_title(lab: Lab, kind: str) -> str:
    spec = spec_by_kind(lab, kind)
    return spec.title if spec else "Очередь"
