from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from bot.config import Settings
from bot.db import session_scope
from bot.keyboards import seats_keyboard
from bot.models import Lab, QueueEntry, User
from bot.queue import as_utc, get_lab, utcnow
from bot.texts import lab_card, queue_list
from bot.users import resolve_group_chat_id

JOB_PREFIXES = ("notify", "open", "post", "close")


@dataclass
class Runtime:
    bot: Bot
    settings: Settings
    scheduler: AsyncIOScheduler


runtime: Runtime | None = None


def set_runtime(value: Runtime) -> None:
    global runtime
    runtime = value


def require_runtime() -> Runtime:
    if runtime is None:
        raise RuntimeError("Бот ещё не запущен")
    return runtime


def lab_timeline(lab: Lab, settings: Settings):
    starts = as_utc(lab.starts_at)
    open_at = starts - timedelta(hours=settings.signup_open_before_hours)
    notify_at = open_at - timedelta(minutes=settings.notify_before_open_minutes)
    post_at = open_at + timedelta(minutes=settings.post_after_open_minutes)
    return notify_at, open_at, post_at, starts


async def _active_users(session: AsyncSession) -> list[User]:
    result = await session.scalars(select(User).where(User.is_active.is_(True)))
    return list(result)


async def _broadcast(bot: Bot, users: list[User], text: str, reply_markup=None) -> None:
    for user in users:
        try:
            await bot.send_message(user.tg_id, text, reply_markup=reply_markup, parse_mode="HTML")
        except TelegramForbiddenError:
            user.is_active = False
        except TelegramBadRequest:
            continue


async def notify_signup(lab_id: int) -> None:
    ctx = require_runtime()
    async with session_scope() as session:
        lab = await get_lab(session, lab_id)
        if lab is None or lab.cancelled or lab.notified_at is not None:
            return
        users = await _active_users(session)
        minutes = ctx.settings.notify_before_open_minutes
        hours = ctx.settings.signup_open_before_hours
        text = (
            f"⏰ Через {minutes} мин. откроется запись на лабораторную.\n\n"
            f"{lab_card(lab, ctx.settings.tz)}\n\n"
            f"Запись откроется за {hours} ч. до начала и будет идти до самой лабы."
        )
        await _broadcast(ctx.bot, users, text)
        lab.notified_at = utcnow()


async def open_signup(lab_id: int) -> None:
    ctx = require_runtime()
    async with session_scope() as session:
        lab = await get_lab(session, lab_id)
        if lab is None or lab.cancelled or lab.opened_at is not None:
            return
        lab.opened_at = utcnow()
        users = await _active_users(session)
        text = (
            "🟢 Запись открыта.\n\n"
            f"{lab_card(lab, ctx.settings.tz)}\n\n"
            "Выберите свободное место — всего 25. "
            f"Через {ctx.settings.post_after_open_minutes} мин. текущая очередь уйдёт в общую группу, "
            "но записаться можно до начала лабораторной."
        )
        await _broadcast(ctx.bot, users, text, seats_keyboard(lab))


async def post_queue(lab_id: int) -> None:
    ctx = require_runtime()
    async with session_scope() as session:
        lab = await get_lab(session, lab_id)
        if lab is None or lab.cancelled or lab.posted_at is not None:
            return
        group_id = await resolve_group_chat_id(session, ctx.settings.group_chat_id)
        text = (
            "📋 Очередь на лабораторную\n\n"
            f"{lab_card(lab, ctx.settings.tz)}\n\n"
            f"{queue_list(list(lab.queue_entries), lab.max_seats)}\n\n"
            f"Снимок через {ctx.settings.post_after_open_minutes} мин. после открытия записи. "
            "Дальше очередь ещё можно пополнить в боте."
        )
        if group_id is None:
            admins = await session.scalars(
                select(User).where(User.tg_id.in_(ctx.settings.admins), User.is_active.is_(True))
            )
            warn = "Не привязана общая группа. Добавьте бота в чат и отправьте /bind.\n\n" + text
            await _broadcast(ctx.bot, list(admins), warn)
        else:
            try:
                await ctx.bot.send_message(group_id, text, parse_mode="HTML")
            except (TelegramForbiddenError, TelegramBadRequest):
                pass
        lab.posted_at = utcnow()


async def close_signup(lab_id: int) -> None:
    async with session_scope() as session:
        lab = await get_lab(session, lab_id)
        if lab is None or lab.closed_at is not None:
            return
        lab.closed_at = utcnow()


def add_lab_job(scheduler: AsyncIOScheduler, func, when, lab_id: int, kind: str) -> None:
    if when <= utcnow():
        return
    scheduler.add_job(
        func,
        trigger=DateTrigger(run_date=when),
        id=f"{kind}:{lab_id}",
        replace_existing=True,
        kwargs={"lab_id": lab_id},
        misfire_grace_time=120,
    )


def clear_lab_jobs(scheduler: AsyncIOScheduler, lab_id: int) -> None:
    for prefix in JOB_PREFIXES:
        job_id = f"{prefix}:{lab_id}"
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)


def schedule_lab_jobs(lab: Lab, settings: Settings, scheduler: AsyncIOScheduler) -> None:
    clear_lab_jobs(scheduler, lab.id)
    if lab.cancelled:
        return
    notify_at, open_at, post_at, starts = lab_timeline(lab, settings)
    if lab.notified_at is None:
        add_lab_job(scheduler, notify_signup, notify_at, lab.id, "notify")
    if lab.opened_at is None:
        add_lab_job(scheduler, open_signup, open_at, lab.id, "open")
    if lab.posted_at is None:
        add_lab_job(scheduler, post_queue, post_at, lab.id, "post")
    if lab.closed_at is None:
        add_lab_job(scheduler, close_signup, starts, lab.id, "close")


async def rebuild_jobs() -> None:
    ctx = require_runtime()
    pending_notify: list[int] = []
    pending_open: list[int] = []
    pending_post: list[int] = []

    async with session_scope() as session:
        labs = list(
            await session.scalars(
                select(Lab).options(
                    selectinload(Lab.subject),
                    selectinload(Lab.queue_entries).selectinload(QueueEntry.user),
                )
            )
        )
        now = utcnow()
        for lab in labs:
            if lab.cancelled:
                continue
            notify_at, open_at, post_at, starts = lab_timeline(lab, ctx.settings)
            if now >= starts and lab.closed_at is None:
                lab.closed_at = now
                continue
            if now >= notify_at and now < open_at and lab.notified_at is None:
                pending_notify.append(lab.id)
            if now >= open_at and now < starts and lab.opened_at is None:
                if now < post_at:
                    pending_open.append(lab.id)
                else:
                    lab.opened_at = now
            if now >= post_at and now < starts and lab.posted_at is None:
                pending_post.append(lab.id)

    for lab_id in pending_notify:
        await notify_signup(lab_id)
    for lab_id in pending_open:
        await open_signup(lab_id)
    for lab_id in pending_post:
        await post_queue(lab_id)

    async with session_scope() as session:
        labs = list(await session.scalars(select(Lab)))
        for lab in labs:
            schedule_lab_jobs(lab, ctx.settings, ctx.scheduler)
