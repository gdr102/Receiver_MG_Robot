import asyncio
import logging
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

from config import CHECK_DELETED_INTERVAL, TARGET_CHAT_ID
from database import (
    get_all_active_messages,
    mark_daily_message_deleted,
)
from server import manager

logger = logging.getLogger(__name__)


async def is_message_deleted(bot: Bot, chat_id: int | str, message_id: int) -> bool:
    """
    Checks if a message is deleted in Telegram.
    Returns True if deleted, False if message still exists.
    """
    try:
        await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id)
        return False
    except TelegramBadRequest as e:
        err_msg = str(e.message).lower()
        if "message to edit not found" in err_msg or "message not found" in err_msg:
            return True
        elif "message can't be edited" in err_msg:
            return False
        logger.warning(f"Unexpected TelegramBadRequest for message {message_id}: {e.message}")
        return False
    except TelegramRetryAfter as e:
        logger.warning(f"Rate limited during deletion check, sleeping for {e.retry_after}s")
        await asyncio.sleep(e.retry_after)
        return False
    except Exception as e:
        logger.error(f"Error checking message {message_id}: {e}")
        return False


async def check_all_active_messages(bot: Bot) -> int:
    """
    Iterates over all active messages (delete=0) across all daily tables.
    Marks deleted messages with delete=1 and broadcasts MESSAGE_DELETED to WebSocket clients.
    """
    active_messages = await get_all_active_messages()
    if not active_messages:
        return 0

    deleted_count = 0
    for table_name, msg in active_messages:
        deleted = await is_message_deleted(bot, TARGET_CHAT_ID, msg["message_id"])
        if deleted:
            deleted_msg = await mark_daily_message_deleted(
                table_name=table_name, message_id=msg["message_id"]
            )
            if deleted_msg:
                await manager.broadcast({
                    "type": "MESSAGE_DELETED",
                    "data": {
                        "id": deleted_msg["id"],
                        "user": deleted_msg["user"],
                        "message_id": deleted_msg["message_id"],
                        "edit": deleted_msg["edit"],
                        "delete": deleted_msg["delete"],
                    },
                })
                deleted_count += 1
                logger.info(
                    f"Message {msg['message_id']} in table '{table_name}' was deleted in Telegram. Broadcasted."
                )
        await asyncio.sleep(0.1)

    return deleted_count


async def run_deleted_checker_loop(bot: Bot, interval: int = CHECK_DELETED_INTERVAL) -> None:
    """
    Background worker loop that periodically checks for deleted messages across daily tables.
    """
    logger.info(f"Background deleted messages checker started (interval={interval}s)")
    while True:
        try:
            await asyncio.sleep(interval)
            count = await check_all_active_messages(bot)
            if count > 0:
                logger.info(f"Checker cycle finished: marked {count} messages as deleted.")
        except asyncio.CancelledError:
            logger.info("Background deleted messages checker stopped.")
            break
        except Exception as e:
            logger.exception(f"Exception in deleted messages checker loop: {e}")
