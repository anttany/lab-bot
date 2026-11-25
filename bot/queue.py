from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from bot.config import DEFAULT_LAB_SEATS
from bot.models import Lab, QueueEntry, SeatInvite, User

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


async def _pending_invites(session: AsyncSession, entry_id: int) -> list[SeatInvite]:
    rows = list(
        await session.scalars(
            select(SeatInvite).where(
                SeatInvite.entry_id == entry_id,
                SeatInvite.status == "pending",
            )
        )
    )
    for row in rows:
        row.status = "cancelled"
    return rows


async def _assign_seat(
    session: AsyncSession,
    lab_id: int,
    user_id: int,
    place: int,
    kind: str,
) -> tuple[str, int | None, list[SeatInvite]]:
    lab = await get_lab(session, lab_id)
    if lab is None or not signup_is_open(lab):
        return "closed", None, []
    if place < 1 or place > seat_count(lab):
        return "invalid", None, []

    occupied = await session.scalar(
        select(QueueEntry).where(
            QueueEntry.lab_id == lab_id,
            QueueEntry.queue_kind == kind,
            QueueEntry.place == place,
        )
    )
    mine = list(
        await session.scalars(
            select(QueueEntry).where(
                QueueEntry.lab_id == lab_id,
                QueueEntry.queue_kind == kind,
                QueueEntry.user_id == user_id,
            )
        )
    )
    if occupied is not None and occupied.user_id == user_id:
        cancelled = await _pending_invites(session, occupied.id)
        await session.delete(occupied)
        await session.flush()
        return "left", place, cancelled
    if occupied is not None:
        return "taken", None, []
    if len(mine) > 1:
        return "many", None, []
    if len(mine) == 1:
        cancelled = await _pending_invites(session, mine[0].id)
        mine[0].place = place
        mine[0].signed_at = utcnow()
        await session.flush()
        return "moved", place, cancelled

    session.add(QueueEntry(lab_id=lab_id, user_id=user_id, place=place, queue_kind=kind))
    await session.flush()
    return "ok", place, []


async def take_seat(
    session: AsyncSession,
    lab: Lab,
    user: User,
    place: int,
    kind: str = "main",
) -> tuple[str, int | None, list[SeatInvite]]:
    async with _lock_for(lab.id):
        try:
            status, seat, cancelled = await _assign_seat(session, lab.id, user.id, place, kind)
            if status in {"ok", "moved", "left"}:
                await session.commit()
            return status, seat, cancelled
        except IntegrityError:
            await session.rollback()
            return "taken", None, []


async def create_invite(
    session: AsyncSession,
    lab_id: int,
    from_user_id: int,
    to_user_id: int,
    place: int,
    kind: str,
) -> tuple[str, SeatInvite | None, list[SeatInvite]]:
    if from_user_id == to_user_id:
        return "self", None, []
    async with _lock_for(lab_id):
        lab = await get_lab(session, lab_id)
        if lab is None or not signup_is_open(lab):
            return "closed", None, []
        entry = await session.scalar(
            select(QueueEntry).where(
                QueueEntry.lab_id == lab_id,
                QueueEntry.queue_kind == kind,
                QueueEntry.place == place,
                QueueEntry.user_id == from_user_id,
            )
        )
        if entry is None:
            return "missing", None, []
        old = await _pending_invites(session, entry.id)
        invite = SeatInvite(
            lab_id=lab_id,
            queue_kind=kind,
            place=place,
            entry_id=entry.id,
            from_user_id=from_user_id,
            to_user_id=to_user_id,
            status="pending",
        )
        session.add(invite)
        await session.commit()
        return "ok", invite, old


async def _settle_invite(
    session: AsyncSession,
    invite_id: int,
    user_id: int,
    status: str,
) -> tuple[str, SeatInvite | None]:
    invite = await session.get(SeatInvite, invite_id)
    if invite is None or invite.to_user_id != user_id:
        return "stale", invite
    async with _lock_for(invite.lab_id):
        await session.refresh(invite)
        if invite.status != "pending":
            return "stale", invite
        if status == "declined":
            invite.status = "declined"
            await session.commit()
            return "declined", invite
        lab = await get_lab(session, invite.lab_id)
        entry = await session.get(QueueEntry, invite.entry_id) if invite.entry_id else None
        same_seat = (
            entry is not None
            and entry.user_id == invite.from_user_id
            and entry.place == invite.place
            and entry.queue_kind == invite.queue_kind
            and entry.lab_id == invite.lab_id
        )
        if lab is None or not signup_is_open(lab) or not same_seat or entry is None:
            invite.status = "cancelled"
            await session.commit()
            return "stale", invite
        entry.user_id = user_id
        entry.signed_at = utcnow()
        invite.status = "accepted"
        await session.commit()
        return "accepted", invite


async def accept_invite(
    session: AsyncSession,
    invite_id: int,
    user_id: int,
) -> tuple[str, SeatInvite | None]:
    return await _settle_invite(session, invite_id, user_id, "accepted")


async def decline_invite(
    session: AsyncSession,
    invite_id: int,
    user_id: int,
) -> tuple[str, SeatInvite | None]:
    return await _settle_invite(session, invite_id, user_id, "declined")


async def cancel_invite(session: AsyncSession, invite_id: int) -> None:
    invite = await session.get(SeatInvite, invite_id)
    if invite is None or invite.status != "pending":
        return
    async with _lock_for(invite.lab_id):
        await session.refresh(invite)
        if invite.status == "pending":
            invite.status = "cancelled"
            await session.commit()
