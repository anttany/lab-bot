from aiogram.types import User as TgUser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import AppState, User


async def upsert_user(session: AsyncSession, tg_user: TgUser) -> User:
    user = await session.scalar(select(User).where(User.tg_id == tg_user.id))
    if user is None:
        user = User(tg_id=tg_user.id)
        session.add(user)
    user.username = tg_user.username
    user.first_name = tg_user.first_name or ""
    user.last_name = tg_user.last_name or ""
    user.is_active = True
    await session.flush()
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
