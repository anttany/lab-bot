from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from bot.config import DEFAULT_LAB_SEATS
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


def seat_count(_lab: Lab) -> int:
    return DEFAULT_LAB_SEATS


async def get_lab(session: AsyncSession, lab_id: int) -> Lab | None:
    result = await session.execute(
        select(Lab)
        .options(selectinload(Lab.subject), selectinload(Lab.queue_entries).selectinload(QueueEntry.user))
        .where(Lab.id == lab_id)
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


def signup_is_open(lab: Lab, now: datetime | None = None) -> bool:
    now = now or utcnow()
    if lab.cancelled or lab.closed_at is not None:
        return False
    if lab.opened_at is None:
        return False
    return as_utc(lab.starts_at) > now


def taken_places(lab: Lab, kind: str = "main") -> dict[int, QueueEntry]:
    return {entry.place: entry for entry in lab.queue_entries if entry.queue_kind == kind}


async def _assign_seat(
    session: AsyncSession,
    lab_id: int,
    user_id: int,
    place: int,
    kind: str,
) -> tuple[str, int | None]:
    lab = await get_lab(session, lab_id)
    if lab is None or not signup_is_open(lab):
        return "closed", None
    if place < 1 or place > seat_count(lab):
        return "invalid", None

    occupied = await session.scalar(
        select(QueueEntry).where(
            QueueEntry.lab_id == lab_id,
            QueueEntry.queue_kind == kind,
            QueueEntry.place == place,
        )
    )
    existing = await session.scalar(
        select(QueueEntry).where(
            QueueEntry.lab_id == lab_id,
            QueueEntry.queue_kind == kind,
            QueueEntry.user_id == user_id,
        )
    )
    if occupied is not None and occupied.user_id != user_id:
        return "taken", None
    if existing is not None and existing.place == place:
        return "already", place
    if existing is not None:
        existing.place = place
        existing.signed_at = utcnow()
        await session.flush()
        return "moved", place

    session.add(QueueEntry(lab_id=lab_id, user_id=user_id, place=place, queue_kind=kind))
    await session.flush()
    return "ok", place


async def take_seat(
    session: AsyncSession,
    lab: Lab,
    user: User,
    place: int,
    kind: str = "main",
) -> tuple[str, int | None]:
    async with _lock_for(lab.id):
        try:
            status, seat = await _assign_seat(session, lab.id, user.id, place, kind)
            if status in {"ok", "moved"}:
                await session.commit()
            return status, seat
        except IntegrityError:
            await session.rollback()
            return "taken", None
