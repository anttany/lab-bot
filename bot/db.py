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
        await conn.run_sync(_migrate_queue_kind)
        await conn.run_sync(_drop_one_seat_per_user)
        await conn.execute(text("PRAGMA journal_mode=WAL"))
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    return session_factory


def _migrate_queue_kind(connection) -> None:
    rows = connection.execute(text("PRAGMA table_info(queue_entries)")).fetchall()
    names = {row[1] for row in rows}
    if not names or "queue_kind" in names:
        return
    connection.execute(
        text(
            """
            CREATE TABLE queue_entries_new (
                id INTEGER PRIMARY KEY,
                lab_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                place INTEGER NOT NULL,
                queue_kind VARCHAR(16) NOT NULL DEFAULT 'main',
                signed_at DATETIME NOT NULL,
                FOREIGN KEY(lab_id) REFERENCES labs(id),
                FOREIGN KEY(user_id) REFERENCES users(id),
                UNIQUE(lab_id, user_id, queue_kind),
                UNIQUE(lab_id, queue_kind, place)
            )
            """
        )
    )
    connection.execute(
        text(
            """
            INSERT INTO queue_entries_new (id, lab_id, user_id, place, queue_kind, signed_at)
            SELECT id, lab_id, user_id, place, 'main', signed_at FROM queue_entries
            """
        )
    )
    connection.execute(text("DROP TABLE queue_entries"))
    connection.execute(text("ALTER TABLE queue_entries_new RENAME TO queue_entries"))
    connection.execute(text("CREATE INDEX ix_queue_entries_lab_id ON queue_entries(lab_id)"))
    connection.execute(text("CREATE INDEX ix_queue_entries_user_id ON queue_entries(user_id)"))


def _drop_one_seat_per_user(connection) -> None:
    if not _has_user_queue_unique(connection):
        return
    connection.execute(
        text(
            """
            CREATE TABLE queue_entries_multi (
                id INTEGER PRIMARY KEY,
                lab_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                place INTEGER NOT NULL,
                queue_kind VARCHAR(16) NOT NULL DEFAULT 'main',
                signed_at DATETIME NOT NULL,
                FOREIGN KEY(lab_id) REFERENCES labs(id),
                FOREIGN KEY(user_id) REFERENCES users(id),
                UNIQUE(lab_id, queue_kind, place)
            )
            """
        )
    )
    connection.execute(
        text(
            """
            INSERT INTO queue_entries_multi (id, lab_id, user_id, place, queue_kind, signed_at)
            SELECT id, lab_id, user_id, place, queue_kind, signed_at FROM queue_entries
            """
        )
    )
    connection.execute(text("DROP TABLE queue_entries"))
    connection.execute(text("ALTER TABLE queue_entries_multi RENAME TO queue_entries"))
    connection.execute(text("CREATE INDEX ix_queue_entries_lab_id ON queue_entries(lab_id)"))
    connection.execute(text("CREATE INDEX ix_queue_entries_user_id ON queue_entries(user_id)"))


def _has_user_queue_unique(connection) -> bool:
    sql = connection.execute(
        text("SELECT sql FROM sqlite_master WHERE type='table' AND name='queue_entries'")
    ).scalar()
    if not sql:
        return False
    compact = "".join(sql.split())
    if "UNIQUE(lab_id,user_id,queue_kind)" in compact:
        return True
    rows = connection.execute(text("PRAGMA index_list('queue_entries')")).fetchall()
    for row in rows:
        if not row[2]:
            continue
        cols = [
            info[2]
            for info in connection.execute(text(f"PRAGMA index_info('{row[1]}')"))
        ]
        if cols == ["lab_id", "user_id", "queue_kind"]:
            return True
    return False


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
