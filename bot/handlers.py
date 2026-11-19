from __future__ import annotations

import re
from datetime import timedelta
from uuid import uuid4

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart, Filter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from bot.config import DEFAULT_LAB_SEATS, Settings
from bot.jobs import (
    add_lab_job,
    close_signup,
    notify_signup,
    open_signup,
    post_queue,
    rebuild_jobs,
    require_runtime,
)
from bot.keyboards import labs_keyboard, seats_keyboard
from bot.models import Lab, QueueEntry, Subject, User
from bot.queue import as_utc, get_lab, seat_count, signup_is_open, take_seat, utcnow
from bot.schedule_source import build_schedule_source
from bot.sync import sync_schedule
from bot.texts import ASK_SURNAME, HELP, START, SURNAME_BAD, lab_card, queue_list
from bot.users import has_surname, resolve_group_chat_id, set_state

router = Router()
admin_router = Router()


class AdminFilter(Filter):
    async def __call__(self, event: Message | CallbackQuery, settings: Settings) -> bool:
        user = event.from_user
        return bool(user and user.id in settings.admins)


admin_router.message.filter(AdminFilter())

SURNAME_RE = re.compile(r"^[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё\- ]{1,39}$")


class Register(StatesGroup):
    last_name = State()


class NeedSurname(Filter):
    async def __call__(self, event: Message | CallbackQuery, db_user: User) -> bool:
        return not has_surname(db_user)


def parse_surname(text: str) -> str | None:
    cleaned = " ".join(text.split())
    if not SURNAME_RE.fullmatch(cleaned):
        return None
    return cleaned


async def _ask_surname(message: Message, state: FSMContext) -> None:
    await state.set_state(Register.last_name)
    await message.answer(ASK_SURNAME)


def _upcoming_query():
    return (
        select(Lab)
        .options(selectinload(Lab.subject), selectinload(Lab.queue_entries).selectinload(QueueEntry.user))
        .where(Lab.cancelled.is_(False), Lab.starts_at > utcnow())
        .order_by(Lab.starts_at)
        .limit(12)
    )


async def _send_labs(message: Message, session: AsyncSession, settings: Settings) -> None:
    labs = list(await session.scalars(_upcoming_query()))
    if not labs:
        await message.answer("Ближайших лабораторных пока нет. Расписание подтянется после /sync.")
        return
    can_sign = {lab.id: signup_is_open(lab) for lab in labs}
    lines = ["<b>Ближайшие лабораторные</b>\n"]
    for lab in labs:
        status = "запись открыта" if can_sign[lab.id] else "ожидает открытия"
        lines.append(f"{lab_card(lab, settings.tz)}\nСтатус: {status}\n")
    await message.answer(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=labs_keyboard(labs, can_sign=can_sign),
    )


async def _show_seats(
    query: CallbackQuery,
    lab: Lab,
    db_user: User,
    settings: Settings,
    *,
    notice: str | None = None,
) -> None:
    if query.message is None:
        return
    text = f"{lab_card(lab, settings.tz)}\n\nВыберите свободное место:"
    if notice:
        text = f"{notice}\n\n{text}"
    markup = seats_keyboard(lab, db_user.id) if signup_is_open(lab) else None
    try:
        await query.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
    except TelegramBadRequest:
        await query.message.answer(text, parse_mode="HTML", reply_markup=markup)


@router.message(CommandStart())
async def cmd_start(
    message: Message,
    session: AsyncSession,
    settings: Settings,
    db_user: User,
    state: FSMContext,
) -> None:
    if not has_surname(db_user):
        await message.answer(START)
        await _ask_surname(message, state)
        return
    await state.clear()
    await message.answer(START, parse_mode="HTML")
    await _send_labs(message, session, settings)


@router.message(Command("surname"))
async def cmd_surname(message: Message, state: FSMContext) -> None:
    await _ask_surname(message, state)


@router.message(Register.last_name, F.text)
async def save_surname(
    message: Message,
    session: AsyncSession,
    settings: Settings,
    db_user: User,
    state: FSMContext,
) -> None:
    if (message.text or "").startswith("/"):
        await message.answer(ASK_SURNAME)
        return
    surname = parse_surname(message.text or "")
    if surname is None:
        await message.answer(SURNAME_BAD)
        return
    db_user.last_name = surname
    await state.clear()
    await message.answer(f"Фамилия сохранена: <b>{surname}</b>", parse_mode="HTML")
    await _send_labs(message, session, settings)


@router.message(NeedSurname())
async def need_surname_message(message: Message, state: FSMContext) -> None:
    await _ask_surname(message, state)


@router.callback_query(NeedSurname())
async def need_surname_callback(query: CallbackQuery, state: FSMContext) -> None:
    await query.answer("Сначала напишите фамилию боту", show_alert=True)
    if query.message:
        await _ask_surname(query.message, state)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP)


@router.message(Command("labs"))
async def cmd_labs(message: Message, session: AsyncSession, settings: Settings) -> None:
    await _send_labs(message, session, settings)


@router.message(Command("my"))
async def cmd_my(message: Message, session: AsyncSession, db_user: User, settings: Settings) -> None:
    entries = list(
        await session.scalars(
            select(QueueEntry)
            .options(selectinload(QueueEntry.lab).selectinload(Lab.subject))
            .where(QueueEntry.user_id == db_user.id)
        )
    )
    live = [
        entry
        for entry in entries
        if as_utc(entry.lab.starts_at) > utcnow() and not entry.lab.cancelled
    ]
    if not live:
        await message.answer("Вы пока никуда не записаны. Откройте /labs.")
        return
    live.sort(key=lambda entry: entry.lab.starts_at)
    lines = ["<b>Ваши записи</b>\n"]
    for entry in live:
        lines.append(f"{entry.place} место · {lab_card(entry.lab, settings.tz)}\n")
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.callback_query(F.data.startswith("signup:"))
async def cb_signup(
    query: CallbackQuery,
    session: AsyncSession,
    db_user: User,
    settings: Settings,
) -> None:
    if query.data is None:
        await query.answer()
        return
    lab_id = int(query.data.split(":", 1)[1])
    lab = await get_lab(session, lab_id)
    if lab is None:
        await query.answer("Лабораторная не найдена", show_alert=True)
        return
    if not signup_is_open(lab):
        await query.answer("Запись сейчас закрыта", show_alert=True)
        return
    await query.answer()
    await _show_seats(query, lab, db_user, settings)


@router.callback_query(F.data.startswith("seat:"))
async def cb_seat(
    query: CallbackQuery,
    session: AsyncSession,
    db_user: User,
    settings: Settings,
) -> None:
    if query.data is None:
        await query.answer()
        return
    _, lab_id_raw, place_raw = query.data.split(":", 2)
    lab = await get_lab(session, int(lab_id_raw))
    if lab is None:
        await query.answer("Лабораторная не найдена", show_alert=True)
        return
    try:
        status, place = await take_seat(session, lab, db_user, int(place_raw))
    except IntegrityError:
        await session.rollback()
        status, place = "taken", None
    lab = await get_lab(session, lab.id) or lab
    if status == "ok":
        await query.answer(f"Вы заняли {place} место")
        await _show_seats(query, lab, db_user, settings, notice=f"Готово: <b>{place}</b> место.")
        return
    if status == "moved":
        await query.answer(f"Пересели на {place} место")
        await _show_seats(query, lab, db_user, settings, notice=f"Теперь вы на <b>{place}</b> месте.")
        return
    if status == "already":
        await query.answer(f"Это уже ваше место: {place}")
        return
    if status == "taken":
        await query.answer("Это место уже заняли", show_alert=True)
        await _show_seats(query, lab, db_user, settings)
        return
    if status == "invalid":
        await query.answer("Такого места нет", show_alert=True)
        return
    await query.answer("Запись сейчас закрыта", show_alert=True)


@router.callback_query(F.data.startswith("busy:"))
async def cb_busy(query: CallbackQuery) -> None:
    await query.answer("Это место уже занято", show_alert=True)


@router.callback_query(F.data.startswith("queue:"))
async def cb_queue(
    query: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    db_user: User,
) -> None:
    if query.data is None or query.message is None:
        await query.answer()
        return
    lab_id = int(query.data.split(":", 1)[1])
    lab = await get_lab(session, lab_id)
    if lab is None:
        await query.answer("Лабораторная не найдена", show_alert=True)
        return
    await query.answer()
    await query.message.answer(
        f"{lab_card(lab, settings.tz)}\n\n{queue_list(list(lab.queue_entries), seat_count(lab))}",
        parse_mode="HTML",
        reply_markup=seats_keyboard(lab, db_user.id) if signup_is_open(lab) else None,
    )


@admin_router.message(Command("bind"))
async def cmd_bind(message: Message, session: AsyncSession) -> None:
    if message.chat.type not in {"group", "supergroup"}:
        await message.answer("Отправьте /bind в общей группе, куда публиковать очередь.")
        return
    await set_state(session, "group_chat_id", str(message.chat.id))
    await message.answer(f"Группа привязана. id: <code>{message.chat.id}</code>", parse_mode="HTML")


@admin_router.message(Command("sync"))
async def cmd_sync(message: Message, session: AsyncSession, settings: Settings) -> None:
    source = build_schedule_source(settings)
    created, updated = await sync_schedule(session, source)
    await session.commit()
    await rebuild_jobs()
    await message.answer(f"Расписание обновлено. Новых: {created}, обновлено: {updated}.")


@admin_router.message(Command("test_lab"))
async def cmd_test_lab(message: Message, session: AsyncSession, settings: Settings) -> None:
    name = (message.text or "").partition(" ")[2].strip() or "Тестовая лаба"
    ctx = require_runtime()
    now = utcnow()
    subject = await session.scalar(select(Subject).where(Subject.name == name))
    if subject is None:
        subject = Subject(name=name)
        session.add(subject)
        await session.flush()
    lab = Lab(
        external_id=f"test-{uuid4().hex[:10]}",
        subject_id=subject.id,
        starts_at=now + timedelta(minutes=10),
        classroom="T-1",
        teacher="тест",
        max_seats=DEFAULT_LAB_SEATS,
    )
    lab.subject = subject
    session.add(lab)
    await session.flush()
    add_lab_job(ctx.scheduler, notify_signup, now + timedelta(seconds=20), lab.id, "notify")
    add_lab_job(ctx.scheduler, open_signup, now + timedelta(seconds=40), lab.id, "open")
    add_lab_job(ctx.scheduler, post_queue, now + timedelta(seconds=70), lab.id, "post")
    add_lab_job(ctx.scheduler, close_signup, as_utc(lab.starts_at), lab.id, "close")
    await message.answer(
        "Тестовая лабораторная создана.\n"
        "Напоминание через 20 сек, запись через 40 сек, "
        "пост в группу через 70 сек, закрытие через 10 мин.\n\n"
        f"{lab_card(lab, settings.tz)}\n"
        f"id: {lab.id}",
        parse_mode="HTML",
    )


@admin_router.message(Command("status"))
async def cmd_status(message: Message, session: AsyncSession, settings: Settings) -> None:
    labs = list(await session.scalars(_upcoming_query()))
    group_id = await resolve_group_chat_id(session, settings.group_chat_id)
    users_count = await session.scalar(
        select(func.count()).select_from(User).where(User.is_active.is_(True))
    )
    await message.answer(
        "Админ-статус\n"
        f"Активных пользователей: {int(users_count or 0)}\n"
        f"Группа: {group_id or 'не привязана'}\n"
        f"Ближайших лаб: {len(labs)}\n"
        f"Часовой пояс: {settings.timezone}"
    )
