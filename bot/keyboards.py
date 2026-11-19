from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.models import Lab


def labs_keyboard(labs: list[Lab], *, can_sign: dict[int, bool]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for lab in labs:
        if can_sign.get(lab.id):
            builder.row(
                InlineKeyboardButton(
                    text=f"Записаться · {lab.subject.name}",
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


def signup_keyboard(lab_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Записаться", callback_data=f"signup:{lab_id}"),
                InlineKeyboardButton(text="Очередь", callback_data=f"queue:{lab_id}"),
            ]
        ]
    )
