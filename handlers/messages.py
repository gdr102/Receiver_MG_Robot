import logging
import pytz
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import func, select

from config import TARGET_CHAT_ID, TIMEZONE
from database import (
    async_session_maker,
    get_message_by_message_id,
    save_message,
    update_edited_message,
    upsert_user,
)
from database.models import Message as DBMessage, User as DBUser
from filters import contains_keywords
from server import manager
from services.checker import check_all_active_messages

logger = logging.getLogger(__name__)
router = Router(name="messages_router")


def format_msk_date(date_obj) -> str:
    """Formats datetime to 'dd.mm.yyyy_HH.MM' in Moscow timezone."""
    msk_tz = pytz.timezone(TIMEZONE)
    if date_obj.tzinfo is None:
        msk_date = pytz.utc.localize(date_obj).astimezone(msk_tz)
    else:
        msk_date = date_obj.astimezone(msk_tz)
    return msk_date.strftime("%d.%m.%Y_%H.%M")


@router.message(Command("start", "help"))
async def cmd_start(message: Message):
    text = (
        "🤖 **Бот Receiver MG Robot активен.**\n\n"
        f"Целевая группа: {TARGET_CHAT_ID}\n"
        "Отслеживаемые ключевые слова: *МГ, ОВЧ, Радиосеть, Ретранслятор, Алгоритм*.\n\n"
        "Команды:\n"
        "/status — статистика базы данных\n"
        "/check_deleted — принудительная проверка удалённых сообщений"
    )
    await message.answer(text, parse_mode="Markdown")


@router.message(Command("status", "stats"))
async def cmd_status(message: Message):
    async with async_session_maker() as session:
        users_count = await session.scalar(select(func.count(DBUser.id)))
        msgs_count = await session.scalar(select(func.count(DBMessage.id)))
        edited_count = await session.scalar(
            select(func.count(DBMessage.id)).where(DBMessage.edit == 1)
        )
        deleted_count = await session.scalar(
            select(func.count(DBMessage.id)).where(DBMessage.delete == 1)
        )

    response = (
        "📊 **Статистика базы данных:**\n"
        f"• Пользователей: {users_count}\n"
        f"• Сообщений сохранено: {msgs_count}\n"
        f"• Отредактированных (edit=1): {edited_count}\n"
        f"• Удалённых (delete=1): {deleted_count}\n"
        f"• Активных WebSocket-клиентов: {len(manager.active_connections)}"
    )
    await message.answer(response, parse_mode="Markdown")


@router.message(Command("check_deleted"))
async def cmd_check_deleted(message: Message):
    status_msg = await message.answer("🔄 Запуск проверки удалённых сообщений...")
    count = await check_all_active_messages(message.bot)
    await status_msg.edit_text(
        f"✅ Проверка завершена. Обнаружено и помечено удалёнными: {count} сообщений."
    )


@router.message(F.chat.id == TARGET_CHAT_ID)
async def handle_new_group_message(message: Message):
    """
    Handles new messages in the target supergroup.
    Checks keywords before saving to database and broadcasting via WebSocket.
    """
    raw_text = message.text or message.caption
    if not raw_text:
        return

    # Check keyword presence
    if not contains_keywords(raw_text):
        logger.debug(
            f"Message {message.message_id} from {message.from_user.id} does not contain keywords. Skipped."
        )
        return

    from_user = message.from_user
    if not from_user:
        return

    username = from_user.username or f"user_{from_user.id}"
    date_str = format_msk_date(message.date)

    async with async_session_maker() as session:
        # Upsert user
        user = await upsert_user(session, tg_id=from_user.id, username=username)

        # Save message
        saved_msg = await save_message(
            session=session,
            user_username=user.username,
            date_str=date_str,
            text=raw_text,
            message_id=message.message_id,
        )

    # Broadcast NEW_MESSAGE to WebSocket clients
    await manager.broadcast({
        "type": "NEW_MESSAGE",
        "data": {
            "id": saved_msg.id,
            "user": saved_msg.user,
            "date": saved_msg.date,
            "message": saved_msg.message,
            "message_id": saved_msg.message_id,
            "edit": saved_msg.edit,
            "delete": saved_msg.delete,
        },
    })

    logger.info(
        f"Added message {message.message_id} from @{username} to DB and broadcasted to WebSocket."
    )


@router.edited_message(F.chat.id == TARGET_CHAT_ID)
async def handle_edited_group_message(message: Message):
    """
    Handles edited messages in the target supergroup.
    If the message was previously stored in DB, updates text, sets edit=1,
    and broadcasts MESSAGE_EDITED to WebSocket clients.
    """
    raw_text = message.text or message.caption or ""
    message_id = message.message_id

    async with async_session_maker() as session:
        existing = await get_message_by_message_id(session, message_id)

        if existing:
            updated_msg = await update_edited_message(session, message_id, raw_text)
            if updated_msg:
                # Broadcast MESSAGE_EDITED
                await manager.broadcast({
                    "type": "MESSAGE_EDITED",
                    "data": {
                        "id": updated_msg.id,
                        "user": updated_msg.user,
                        "date": updated_msg.date,
                        "message": updated_msg.message,
                        "message_id": updated_msg.message_id,
                        "edit": updated_msg.edit,
                        "delete": updated_msg.delete,
                    },
                })
            logger.info(
                f"Updated edited message {message_id} in DB (edit=1) and broadcasted."
            )
        else:
            # Message wasn't previously in DB, but edit now contains keywords
            if contains_keywords(raw_text):
                from_user = message.from_user
                username = from_user.username if from_user and from_user.username else f"user_{from_user.id if from_user else 'unknown'}"
                user = await upsert_user(session, tg_id=from_user.id, username=username)
                date_str = format_msk_date(message.date)
                saved = await save_message(
                    session=session,
                    user_username=user.username,
                    date_str=date_str,
                    text=raw_text,
                    message_id=message_id,
                )
                updated_msg = await update_edited_message(session, message_id, raw_text)
                target = updated_msg or saved
                await manager.broadcast({
                    "type": "MESSAGE_EDITED",
                    "data": {
                        "id": target.id,
                        "user": target.user,
                        "date": target.date,
                        "message": target.message,
                        "message_id": target.message_id,
                        "edit": target.edit,
                        "delete": target.delete,
                    },
                })
                logger.info(
                    f"Saved and broadcasted previously untracked message {message_id} after edit."
                )
