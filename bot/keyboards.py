from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.models import Lab
from bot.queue import seat_count, taken_places


def labs_keyboard(labs: list[Lab], *, can_sign: dict[int, bool]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for lab in labs:
        if can_sign.get(lab.id):
            builder.row(
                InlineKeyboardButton(
                    text=f"Выбрать место · {lab.subject.name}",
                    callback_data=f"signup:{lab.id}",
                )
            )
        builder.row(
            InlineKeyboardButton(
                text=f"Очередь · {lab.subject.name}",
                callback_data=f"queue:{lab.id}",
            )
        )
    return builder.as_markup()


def seats_keyboard(lab: Lab, user_id: int | None = None) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    occupied = taken_places(lab)
    for place in range(1, seat_count(lab) + 1):
        entry = occupied.get(place)
        if entry is None:
            text = str(place)
            data = f"seat:{lab.id}:{place}"
        elif user_id is not None and entry.user_id == user_id:
            text = f"✅{place}"
            data = f"seat:{lab.id}:{place}"
        else:
            text = f"🔒{place}"
            data = f"busy:{lab.id}:{place}"
        builder.add(InlineKeyboardButton(text=text, callback_data=data))
    builder.adjust(5)
    builder.row(
        InlineKeyboardButton(text="Очередь", callback_data=f"queue:{lab.id}"),
    )
    return builder.as_markup()


def signup_keyboard(lab: Lab, user_id: int | None = None) -> InlineKeyboardMarkup:
    return seats_keyboard(lab, user_id)
