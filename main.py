import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import ErrorEvent, MenuButtonWebApp, WebAppInfo
from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import config
from database.db import init_db
from handlers import admin, user
from services.expiry_checker import check_expirations
from webapp.api import create_app

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def main() -> None:
    if not config.bot_token:
        raise RuntimeError("BOT_TOKEN не задан в .env")

    bot = Bot(token=config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(admin.router)
    dp.include_router(user.router)

    @dp.errors()
    async def on_error(event: ErrorEvent) -> bool:
        # Страховка: если в каком-то хендлере всплывёт необработанное исключение,
        # оно попадёт сюда с полным traceback в логах, а не потеряется молча
        # (пользователь при этом видит "зависшую" кнопку без ответа).
        logger.exception(
            "Unhandled exception while processing update %s", event.update, exc_info=event.exception
        )
        return True

    await init_db()

    if config.webapp_url:
        # Постоянная синяя кнопка меню рядом с полем ввода в Telegram-клиенте,
        # открывающая мини-приложение (личный кабинет).
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="Профиль", web_app=WebAppInfo(url=config.webapp_url))
        )
        logger.info("WebApp menu button set to %s", config.webapp_url)
    else:
        logger.info("WEBAPP_URL не задан — кнопка мини-приложения не будет показана в меню бота")

    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_expirations, "interval", hours=24, args=[bot])
    scheduler.start()

    # HTTP API для мини-приложения (webapp/api.py) — отдельный aiohttp-сервер,
    # работает параллельно с поллингом бота. Наружу должен быть проброшен через
    # reverse-proxy (nginx/caddy) с HTTPS на домене из API_CORS_ORIGIN/WEBAPP_URL.
    api_app = create_app(bot)
    runner = web.AppRunner(api_app)
    await runner.setup()
    site = web.TCPSite(runner, config.api_host, config.api_port)
    await site.start()
    logger.info("WebApp API listening on %s:%s", config.api_host, config.api_port)

    await bot.delete_webhook(drop_pending_updates=True)
    try:
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
