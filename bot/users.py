from __future__ import annotations

import asyncio
from contextlib import AbstractAsyncContextManager, nullcontext

from aiogram.types import User as TgUser
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import AppState, User

_user_locks: dict[int, asyncio.Lock] = {}


def lock_for_user(tg_id: int | None) -> AbstractAsyncContextManager[None]:
    if tg_id is None:
        return nullcontext()
    lock = _user_locks.get(tg_id)
    if lock is None:
        lock = asyncio.Lock()
        _user_locks[tg_id] = lock
    return lock


def has_surname(user: User) -> bool:
    return bool(user.last_name.strip())


async def upsert_user(session: AsyncSession, tg_user: TgUser) -> User:
    stmt = insert(User).values(
        tg_id=tg_user.id,
        username=tg_user.username,
        first_name=tg_user.first_name or "",
        last_name="",
        is_active=True,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[User.tg_id],
        set_={
            "username": stmt.excluded.username,
            "first_name": stmt.excluded.first_name,
            "is_active": True,
        },
    )
    await session.execute(stmt)
    await session.flush()
    user = await session.scalar(select(User).where(User.tg_id == tg_user.id))
    if user is None:
        raise RuntimeError(f"Не удалось сохранить пользователя {tg_user.id}")
    return user


async def get_state(session: AsyncSession, key: str) -> str | None:
    row = await session.get(AppState, key)
    return row.value if row else None


async def set_state(session: AsyncSession, key: str, value: str) -> None:
    row = await session.get(AppState, key)
    if row is None:
        session.add(AppState(key=key, value=value))
    else:
        row.value = value


async def resolve_group_chat_id(session: AsyncSession, fallback: int | None) -> int | None:
    stored = await get_state(session, "group_chat_id")
    if stored:
        return int(stored)
    return fallback
