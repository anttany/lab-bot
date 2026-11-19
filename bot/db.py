from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from bot.config import Settings
from bot.models import Base

session_factory: async_sessionmaker[AsyncSession] | None = None


def _db_url(path: str) -> str:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite+aiosqlite:///{Path(path).as_posix()}"


def _configure_sqlite(sync_engine) -> None:
    @event.listens_for(sync_engine, "connect")
    def _on_connect(dbapi_connection, _record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


async def init_db(settings: Settings) -> async_sessionmaker[AsyncSession]:
    global session_factory
    engine = create_async_engine(
        _db_url(settings.db_path),
        echo=False,
        poolclass=NullPool,
        connect_args={"timeout": 30, "check_same_thread": False},
    )
    _configure_sqlite(engine.sync_engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("PRAGMA journal_mode=WAL"))
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
