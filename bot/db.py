from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.config import Settings
from bot.models import Base

session_factory: async_sessionmaker[AsyncSession] | None = None


def _db_url(path: str) -> str:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite+aiosqlite:///{Path(path).as_posix()}"


async def init_db(settings: Settings) -> async_sessionmaker[AsyncSession]:
    global session_factory
    engine = create_async_engine(_db_url(settings.db_path), echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    return session_factory


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if session_factory is None:
        raise RuntimeError("База ещё не инициализирована")
    return session_factory


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
