from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from bot.models import Lab, QueueEntry, User

_locks: dict[int, asyncio.Lock] = {}


def _lock_for(lab_id: int) -> asyncio.Lock:
    lock = _locks.get(lab_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[lab_id] = lock
    return lock


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


async def get_lab(session: AsyncSession, lab_id: int) -> Lab | None:
    result = await session.execute(
        select(Lab)
        .options(selectinload(Lab.subject), selectinload(Lab.queue_entries).selectinload(QueueEntry.user))
        .where(Lab.id == lab_id)
    )
    return result.scalar_one_or_none()


def signup_is_open(lab: Lab, now: datetime | None = None) -> bool:
    now = now or utcnow()
    if lab.cancelled or lab.closed_at is not None:
        return False
    if lab.opened_at is None:
        return False
    return as_utc(lab.starts_at) > now


async def join_queue(session: AsyncSession, lab: Lab, user: User) -> tuple[str, int | None]:
    async with _lock_for(lab.id):
        await session.flush()
        await session.refresh(
            lab,
            attribute_names=["opened_at", "closed_at", "cancelled", "starts_at", "max_seats"],
        )
        if not signup_is_open(lab):
            return "closed", None

        existing = await session.scalar(
            select(QueueEntry).where(
                QueueEntry.lab_id == lab.id,
                QueueEntry.user_id == user.id,
            )
        )
        if existing:
            return "already", existing.place

        taken = await session.scalar(
            select(func.count()).select_from(QueueEntry).where(QueueEntry.lab_id == lab.id)
        )
        taken = int(taken or 0)
        if lab.max_seats is not None and taken >= lab.max_seats:
            return "full", None

        entry = QueueEntry(lab_id=lab.id, user_id=user.id, place=taken + 1)
        session.add(entry)
        await session.flush()
        return "ok", entry.place
