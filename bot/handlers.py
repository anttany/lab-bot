from __future__ import annotations

import re
from datetime import timedelta
from uuid import uuid4

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart, Filter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, ChatMemberUpdated, InlineKeyboardMarkup, Message
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
from bot.itip import default_kind, has_dual_queues, spec_by_kind
from bot.keyboards import invite_keyboard, labs_keyboard, queues_keyboard, seats_keyboard
from bot.models import Lab, QueueEntry, SeatInvite, Subject, User
from bot.queue import (
    accept_invite,
    as_utc,
    cancel_invite,
    create_invite,
    decline_invite,
    get_lab,
    signup_is_open,
    take_seat,
    utcnow,
)
from bot.schedule_source import build_schedule_source
from bot.sync import sync_schedule
from bot.texts import (
    ASK_SURNAME,
    GIVE_BAD,
    GIVE_CANCELLED,
    GIVE_CLOSED,
    GIVE_MISSING,
    GIVE_SELF,
    GIVE_UNDELIVERED,
    HELP,
    INVITE_STALE,
    MANY_SEATS,
    SEAT_HINT,
    START,
    SURNAME_BAD,
    at_username,
    give_ask,
    give_sent,
    give_unknown,
    invite_accepted_from,
    invite_accepted_to,
    invite_declined_from,
    invite_declined_to,
    invite_text,
    invite_withdrawn,
    lab_card,
    lab_preview,
    left_notice,
    person_label,
    queues_text,
)
from bot.users import has_surname, resolve_group_chat_id, set_state

router = Router()
admin_router = Router()
group_router = Router()

router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")
admin_router.message.filter(F.chat.type == "private")


class AdminFilter(Filter):
    async def __call__(self, event: Message | CallbackQuery, settings: Settings) -> bool:
        user = event.from_user
        return bool(user and user.id in settings.admins)


admin_router.message.filter(AdminFilter())

SURNAME_RE = re.compile(r"^[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё\- ]{1,39}$")


class Register(StatesGroup):
    last_name = State()


class Handoff(StatesGroup):
    username = State()


class NeedSurname(Filter):
    async def __call__(self, event: Message | CallbackQuery, db_user: User) -> bool:
        if isinstance(event, CallbackQuery) and (event.data or "").startswith(("iok:", "ino:")):
            return False
        return not has_surname(db_user)


USERNAME_RE = re.compile(r"^@?[A-Za-z][A-Za-z0-9_]{4,31}$")


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
        .limit(24)
    )


TELEGRAM_TEXT_LIMIT = 3500


async def _answer_chunks(message: Message, text: str, reply_markup=None) -> None:
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for block in text.split("\n\n"):
        extra = len(block) + (2 if current else 0)
        if current and size + extra > TELEGRAM_TEXT_LIMIT:
            chunks.append("\n\n".join(current))
            current = [block]
            size = len(block)
        else:
            current.append(block)
            size += extra
    if current:
        chunks.append("\n\n".join(current))
    for index, chunk in enumerate(chunks):
        markup = reply_markup if index == len(chunks) - 1 else None
        await message.answer(chunk, parse_mode="HTML", reply_markup=markup)


async def _send_labs(message: Message, session: AsyncSession, settings: Settings) -> None:
    labs = list(await session.scalars(_upcoming_query()))
    if not labs:
        await message.answer("Ближайших лабораторных пока нет. Расписание подтянется после /sync.")
        return
    can_sign = {lab.id: signup_is_open(lab) for lab in labs}
    lines = ["<b>Ближайшие лабораторные</b>"]
    for lab in labs:
        lines.append(lab_preview(lab, settings.tz, open_signup=can_sign[lab.id]))
    await _answer_chunks(
        message,
        "\n\n".join(lines),
        labs_keyboard(labs, can_sign=can_sign, tz=settings.tz),
    )


async def _edit_or_send(query: CallbackQuery, text: str, markup) -> None:
    if query.message is None:
        return
    try:
        await query.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
    except TelegramBadRequest:
        await query.message.answer(text, parse_mode="HTML", reply_markup=markup)


async def _show_queue_picker(
    query: CallbackQuery,
    lab: Lab,
    settings: Settings,
    *,
    notice: str | None = None,
) -> None:
    text = (
        f"{lab_card(lab, settings.tz)}\n\n"
        "Две очереди на это занятие. Можно записаться в обе: "
        "сначала сдают текущую лабу, затем старые."
    )
    if notice:
        text = f"{notice}\n\n{text}"
    markup = queues_keyboard(lab) if signup_is_open(lab) else None
    await _edit_or_send(query, text, markup)


async def _show_seats(
    query: CallbackQuery,
    lab: Lab,
    db_user: User,
    settings: Settings,
    kind: str,
    *,
    notice: str | None = None,
) -> None:
    spec = spec_by_kind(lab, kind, settings.tz)
    if spec is None:
        if has_dual_queues(lab, settings.tz):
            await _show_queue_picker(query, lab, settings)
            return
        kind = default_kind(lab, settings.tz)
        spec = spec_by_kind(lab, kind, settings.tz)
    title = spec.title if spec else "Очередь"
    text = f"{lab_card(lab, settings.tz)}\n\n<b>{title}</b>\nВыберите свободное место.\n{SEAT_HINT}"
    if notice:
        text = f"{notice}\n\n{text}"
    markup = seats_keyboard(lab, db_user.id, kind) if signup_is_open(lab) else None
    await _edit_or_send(query, text, markup)


def _queue_title(lab: Lab, kind: str, settings: Settings) -> str:
    spec = spec_by_kind(lab, kind, settings.tz)
    if spec and has_dual_queues(lab, settings.tz):
        return spec.title
    return "Очередь"


async def _tell(bot: Bot, tg_id: int, text: str, reply_markup=None) -> bool:
    try:
        await bot.send_message(tg_id, text, parse_mode="HTML", reply_markup=reply_markup)
    except (TelegramForbiddenError, TelegramBadRequest):
        return False
    return True


async def _tell_withdrawn(bot: Bot, session: AsyncSession, invites: list[SeatInvite], giver: User) -> None:
    for invite in invites:
        user = await session.get(User, invite.to_user_id)
        if user is None:
            continue
        await _tell(bot, user.tg_id, invite_withdrawn(person_label(giver), invite.place))


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


@group_router.my_chat_member()
async def on_bot_chat_member(event: ChatMemberUpdated, session: AsyncSession, bot: Bot) -> None:
    if event.chat.type not in {"group", "supergroup"}:
        return
    if event.new_chat_member.user.id != bot.id:
        return
    if event.new_chat_member.status not in {"member", "administrator"}:
        return
    await set_state(session, "group_chat_id", str(event.chat.id))
    try:
        await bot.send_message(
            event.chat.id,
            "Группа привязана. Очередь на лабораторные будет приходить сюда.",
        )
    except TelegramBadRequest:
        pass


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


@router.message(Handoff.username, F.text)
async def take_handoff_username(
    message: Message,
    session: AsyncSession,
    settings: Settings,
    db_user: User,
    state: FSMContext,
) -> None:
    raw = (message.text or "").strip()
    if raw.startswith("/"):
        await state.clear()
        command = raw.split()[0].split("@")[0]
        if command == "/labs":
            await _send_labs(message, session, settings)
            return
        if command == "/help":
            await message.answer(HELP)
            return
        if command == "/my":
            await cmd_my(message, session, db_user, settings)
            return
        await message.answer(GIVE_CANCELLED)
        return
    if not USERNAME_RE.fullmatch(raw):
        await message.answer(GIVE_BAD)
        return
    data = await state.get_data()
    try:
        lab_id = int(data["lab_id"])
        place = int(data["place"])
        kind = str(data["kind"])
    except (KeyError, TypeError, ValueError):
        await state.clear()
        await message.answer(GIVE_MISSING)
        return
    username = raw.removeprefix("@")
    recipient = await session.scalar(
        select(User).where(func.lower(User.username) == username.lower(), User.is_active.is_(True))
    )
    if recipient is None:
        await message.answer(give_unknown(f"@{username}"))
        return
    if recipient.id == db_user.id:
        await message.answer(GIVE_SELF)
        return
    lab = await get_lab(session, lab_id)
    if lab is None:
        await state.clear()
        await message.answer(GIVE_MISSING)
        return
    status, invite, old = await create_invite(
        session, lab.id, db_user.id, recipient.id, place, kind
    )
    if status == "closed":
        await state.clear()
        await message.answer(GIVE_CLOSED)
        return
    if status != "ok" or invite is None:
        await state.clear()
        await message.answer(GIVE_MISSING)
        return
    sent = await _tell(
        message.bot,
        recipient.tg_id,
        invite_text(person_label(db_user), place, _queue_title(lab, kind, settings), lab_card(lab, settings.tz)),
        invite_keyboard(invite.id),
    )
    if not sent:
        await cancel_invite(session, invite.id)
        await message.answer(GIVE_UNDELIVERED)
        await _tell_withdrawn(message.bot, session, old, db_user)
        return
    await state.clear()
    await message.answer(give_sent(at_username(recipient), place), parse_mode="HTML")
    await _tell_withdrawn(message.bot, session, old, db_user)


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
    live.sort(key=lambda entry: (entry.lab.starts_at, entry.queue_kind, entry.place))
    lines = ["<b>Ваши записи</b>\n"]
    for entry in live:
        spec = spec_by_kind(entry.lab, entry.queue_kind, settings.tz)
        queue_label = f" · {spec.short}" if spec and has_dual_queues(entry.lab, settings.tz) else ""
        lines.append(f"{entry.place} место{queue_label}\n{lab_card(entry.lab, settings.tz)}\n")
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
    if has_dual_queues(lab, settings.tz):
        await _show_queue_picker(query, lab, settings)
        return
    await _show_seats(query, lab, db_user, settings, default_kind(lab, settings.tz))


@router.callback_query(F.data.startswith("qpick:"))
async def cb_queue_pick(
    query: CallbackQuery,
    session: AsyncSession,
    db_user: User,
    settings: Settings,
) -> None:
    if query.data is None:
        await query.answer()
        return
    _, lab_id_raw, kind = query.data.split(":", 2)
    lab = await get_lab(session, int(lab_id_raw))
    if lab is None:
        await query.answer("Лабораторная не найдена", show_alert=True)
        return
    if spec_by_kind(lab, kind, settings.tz) is None:
        await query.answer("Такой очереди нет", show_alert=True)
        return
    if not signup_is_open(lab):
        await query.answer("Запись сейчас закрыта", show_alert=True)
        return
    await query.answer()
    await _show_seats(query, lab, db_user, settings, kind)


@router.callback_query(F.data.startswith("seat:"))
async def cb_seat(
    query: CallbackQuery,
    session: AsyncSession,
    db_user: User,
    settings: Settings,
    bot: Bot,
) -> None:
    if query.data is None:
        await query.answer()
        return
    parts = query.data.split(":")
    if len(parts) == 4:
        _, lab_id_raw, kind, place_raw = parts
    elif len(parts) == 3:
        _, lab_id_raw, place_raw = parts
        kind = ""
    else:
        await query.answer()
        return
    lab = await get_lab(session, int(lab_id_raw))
    if lab is None:
        await query.answer("Лабораторная не найдена", show_alert=True)
        return
    if not kind:
        kind = default_kind(lab, settings.tz)
    if spec_by_kind(lab, kind, settings.tz) is None:
        await query.answer("Такой очереди нет", show_alert=True)
        return
    try:
        status, place, cancelled = await take_seat(session, lab, db_user, int(place_raw), kind)
    except IntegrityError:
        await session.rollback()
        status, place, cancelled = "taken", None, []
    lab = await get_lab(session, lab.id) or lab
    spec = spec_by_kind(lab, kind, settings.tz)
    queue_note = f" ({spec.short})" if spec and has_dual_queues(lab, settings.tz) else ""
    if status == "ok":
        await query.answer(f"Вы заняли {place} место{queue_note}")
        await _show_seats(
            query,
            lab,
            db_user,
            settings,
            kind,
            notice=f"Готово: <b>{place}</b> место{queue_note}.",
        )
        return
    if status == "moved":
        await query.answer(f"Пересели на {place} место{queue_note}")
        await _show_seats(
            query,
            lab,
            db_user,
            settings,
            kind,
            notice=f"Теперь вы на <b>{place}</b> месте{queue_note}.",
        )
        await _tell_withdrawn(bot, session, cancelled, db_user)
        return
    if status == "left":
        await query.answer(f"Вы снялись с {place} места")
        await _show_seats(query, lab, db_user, settings, kind, notice=left_notice(place or 0))
        await _tell_withdrawn(bot, session, cancelled, db_user)
        return
    if status == "many":
        await query.answer(MANY_SEATS, show_alert=True)
        return
    if status == "taken":
        await query.answer("Это место уже заняли", show_alert=True)
        await _show_seats(query, lab, db_user, settings, kind)
        return
    if status == "invalid":
        await query.answer("Такого места нет", show_alert=True)
        return
    await query.answer("Запись сейчас закрыта", show_alert=True)


@router.callback_query(F.data.startswith("give:"))
async def cb_give(
    query: CallbackQuery,
    session: AsyncSession,
    db_user: User,
    settings: Settings,
    state: FSMContext,
) -> None:
    if query.data is None:
        await query.answer()
        return
    _, lab_raw, kind, place_raw = query.data.split(":", 3)
    lab = await get_lab(session, int(lab_raw))
    place = int(place_raw)
    if lab is None:
        await query.answer("Лабораторная не найдена", show_alert=True)
        return
    owns = any(
        entry.user_id == db_user.id and entry.queue_kind == kind and entry.place == place
        for entry in lab.queue_entries
    )
    if not owns:
        await query.answer(GIVE_MISSING, show_alert=True)
        return
    if not signup_is_open(lab):
        await query.answer(GIVE_CLOSED, show_alert=True)
        return
    await state.set_state(Handoff.username)
    await state.update_data(lab_id=lab.id, kind=kind, place=place)
    await query.answer()
    if query.message:
        await query.message.answer(
            give_ask(place, _queue_title(lab, kind, settings)),
            parse_mode="HTML",
        )


async def _finish_invite(
    query: CallbackQuery,
    text: str,
) -> None:
    await query.answer()
    if query.message is None:
        return
    try:
        await query.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[]),
        )
    except TelegramBadRequest:
        await query.message.answer(text, parse_mode="HTML")


@router.callback_query(F.data.startswith("iok:"))
async def cb_accept_invite(
    query: CallbackQuery,
    session: AsyncSession,
    db_user: User,
    settings: Settings,
    bot: Bot,
) -> None:
    if query.data is None:
        await query.answer()
        return
    status, invite = await accept_invite(session, int(query.data.split(":", 1)[1]), db_user.id)
    if status != "accepted" or invite is None:
        await query.answer(INVITE_STALE, show_alert=True)
        return
    lab = await get_lab(session, invite.lab_id)
    queue = _queue_title(lab, invite.queue_kind, settings) if lab is not None else "Очередь"
    card = lab_card(lab, settings.tz) if lab is not None else ""
    await _finish_invite(query, invite_accepted_to(invite.place, queue, card))
    giver = await session.get(User, invite.from_user_id)
    if giver is not None:
        await _tell(bot, giver.tg_id, invite_accepted_from(at_username(db_user), invite.place))


@router.callback_query(F.data.startswith("ino:"))
async def cb_decline_invite(
    query: CallbackQuery,
    session: AsyncSession,
    db_user: User,
    bot: Bot,
) -> None:
    if query.data is None:
        await query.answer()
        return
    status, invite = await decline_invite(session, int(query.data.split(":", 1)[1]), db_user.id)
    if status != "declined" or invite is None:
        await query.answer(INVITE_STALE, show_alert=True)
        return
    await _finish_invite(query, invite_declined_to(invite.place))
    giver = await session.get(User, invite.from_user_id)
    if giver is not None:
        await _tell(bot, giver.tg_id, invite_declined_from(at_username(db_user), invite.place))


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
    markup = None
    if signup_is_open(lab):
        markup = queues_keyboard(lab) if has_dual_queues(lab, settings.tz) else seats_keyboard(lab, db_user.id)
    await query.message.answer(
        f"{lab_card(lab, settings.tz)}\n\n{queues_text(lab, settings.tz)}",
        parse_mode="HTML",
        reply_markup=markup,
    )


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
