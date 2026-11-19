from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from aiohttp import ClientSession
from aiohttp.hdrs import USER_AGENT
from aiohttp.http import SERVER_SOFTWARE
from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.__meta__ import __version__
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, TelegramObject, Update, User as TgUser
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import get_settings
from bot.db import get_session_factory, init_db, session_scope
from bot.handlers import admin_router, router
from bot.jobs import Runtime, rebuild_jobs, set_runtime
from bot.schedule_source import build_schedule_source
from bot.sync import sync_schedule
from bot.users import lock_for_user, set_state, upsert_user

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("labbot")


class DirectTelegramSession(AiohttpSession):
    """Сессия Telegram без прокси и без HTTP_PROXY/VPN из окружения."""

    def __init__(self) -> None:
        super().__init__(proxy=None)

    async def create_session(self) -> ClientSession:
        if self._should_reset_connector:
            await self.close()
        if self._session is None or self._session.closed:
            self._session = ClientSession(
                connector=self._connector_type(**self._connector_init),
                headers={USER_AGENT: f"{SERVER_SOFTWARE} aiogram/{__version__}"},
                trust_env=False,
            )
            self._should_reset_connector = False
        return self._session


class DbMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        from_user = _user_from_event(event) or data.get("event_from_user")
        factory = get_session_factory()
        async with lock_for_user(getattr(from_user, "id", None)):
            async with factory() as session:
                data["session"] = session
                try:
                    result = await handler(event, data)
                    await session.commit()
                    return result
                except Exception:
                    await session.rollback()
                    raise


def _user_from_event(event: TelegramObject) -> TgUser | None:
    if isinstance(event, Update):
        for source in (event.message, event.callback_query, event.my_chat_member, event.edited_message):
            if source is not None and getattr(source, "from_user", None) is not None:
                return source.from_user
        return None
    return getattr(event, "from_user", None)


class UserMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        session: AsyncSession = data["session"]
        from_user = _user_from_event(event) or data.get("event_from_user")
        if from_user is not None:
            data["db_user"] = await upsert_user(session, from_user)
        return await handler(event, data)


async def periodic_sync() -> None:
    settings = get_settings()
    try:
        async with session_scope() as session:
            created, updated = await sync_schedule(session, build_schedule_source(settings))
            logger.info("sync: created=%s updated=%s", created, updated)
        await rebuild_jobs()
    except Exception:
        logger.exception("Не удалось обновить расписание")


async def main() -> None:
    settings = get_settings()
    await init_db(settings)

    bot = Bot(
        token=settings.bot_token,
        session=DirectTelegramSession(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp["settings"] = settings
    dp.update.outer_middleware(DbMiddleware())
    dp.update.outer_middleware(UserMiddleware())
    dp.include_router(admin_router)
    dp.include_router(router)

    scheduler = AsyncIOScheduler(timezone=settings.tz)
    set_runtime(Runtime(bot=bot, settings=settings, scheduler=scheduler))

    if settings.group_chat_id is not None:
        async with session_scope() as session:
            await set_state(session, "group_chat_id", str(settings.group_chat_id))

    try:
        async with session_scope() as session:
            await sync_schedule(session, build_schedule_source(settings))
    except FileNotFoundError:
        logger.warning("Файл расписания не найден: %s", settings.schedule_path)
    except Exception:
        logger.exception("Первичная загрузка расписания не удалась")

    scheduler.add_job(periodic_sync, "interval", hours=1, id="schedule-sync", replace_existing=True)
    scheduler.start()
    await rebuild_jobs()

    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Начать работу"),
            BotCommand(command="labs", description="Ближайшие лабораторные"),
            BotCommand(command="my", description="Мои записи"),
            BotCommand(command="help", description="Как работает запись"),
            BotCommand(command="surname", description="Сменить фамилию"),
        ]
    )
    me = await bot.get_me()
    logger.info("Бот запущен как @%s", me.username)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
