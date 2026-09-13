import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode

from config import BOT_TOKEN, CHECK_DELETED_INTERVAL, PROXY_URL, TARGET_CHAT_ID
from database import init_db
from handlers import router
from server import create_ws_server
from services.checker import run_deleted_checker_loop

# Configure structured logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s:%(funcName)s:%(lineno)d - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("app")


def get_bot_session() -> AiohttpSession | None:
    """Returns an AiohttpSession with proxy if configured."""
    if PROXY_URL:
        logger.info(f"Configuring bot with proxy: {PROXY_URL}")
        return AiohttpSession(proxy=PROXY_URL)
    return None


async def main() -> None:
    """
    Main asynchronous entrypoint.
    Runs Telegram bot polling, WebSocket server, and background deletion checker
    concurrently in the same asyncio event loop without mutual blocking.
    """
    logger.info("Initializing database...")
    await init_db()

    session = get_bot_session()
    bot = Bot(
        token=BOT_TOKEN,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    dp = Dispatcher()
    dp.include_router(router)

    # Verify Telegram bot identity and connection
    try:
        bot_user = await bot.get_me()
        logger.info(
            f"Telegram Bot connected: @{bot_user.username} (ID: {bot_user.id})"
        )
    except Exception as e:
        logger.error(f"Failed to connect to Telegram API: {e}")
        if session:
            await bot.session.close()
        return

    logger.info(f"Target supergroup ID: {TARGET_CHAT_ID}")

    # 1. Create Uvicorn WebSocket server instance
    ws_server = create_ws_server()

    # 2. Launch WebSocket server and background checker as asyncio tasks in the same loop
    ws_server_task = asyncio.create_task(ws_server.serve())
    checker_task = asyncio.create_task(
        run_deleted_checker_loop(bot, interval=CHECK_DELETED_INTERVAL)
    )

    # 3. Start aiogram polling concurrently
    allowed_updates = dp.resolve_used_update_types()
    logger.info(f"Starting Telegram polling for updates: {allowed_updates}")

    try:
        await dp.start_polling(bot, allowed_updates=allowed_updates)
    finally:
        logger.info("Stopping all background services...")
        # Signal Uvicorn server to stop gracefully
        ws_server.should_exit = True
        # Cancel background deletion checker task
        checker_task.cancel()

        # Wait for all background tasks to complete cleanly
        await asyncio.gather(ws_server_task, checker_task, return_exceptions=True)
        await bot.session.close()
        logger.info("Bot and WebSocket server shutdown complete.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Terminated by user.")
