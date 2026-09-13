import logging
import pytz
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

from config import TARGET_CHAT_ID, TIMEZONE
from database import (
    async_session_maker,
    find_and_update_edited_message,
    get_db_stats,
    save_message,
    upsert_user,
)
from filters import contains_keywords
from server import manager
from services.checker import check_all_active_messages

logger = logging.getLogger(__name__)
router = Router(name="messages_router")


def get_msk_datetimes(date_obj) -> tuple[str, str]:
    """
    Returns (table_name, formatted_date_str) in Moscow timezone.
    table_name: 'dd.mm.yyyy' (e.g. '13.09.2026')
    formatted_date_str: 'dd.mm.yyyy_HH.MM' (e.g. '13.09.2026_18.05')
    """
    msk_tz = pytz.timezone(TIMEZONE)
    if date_obj.tzinfo is None:
        msk_date = pytz.utc.localize(date_obj).astimezone(msk_tz)
    else:
        msk_date = date_obj.astimezone(msk_tz)

    table_name = msk_date.strftime("%d.%m.%Y")
    formatted_date_str = msk_date.strftime("%d.%m.%Y_%H.%M")
    return table_name, formatted_date_str


@router.message(Command("start", "help"))
async def cmd_start(message: Message):
    text = (
        "🤖 **Бот Receiver MG Robot активен.**\n\n"
        f"Целевая группа: `{TARGET_CHAT_ID}`\n"
        "Отслеживаемые ключевые слова: *МГ, ОВЧ, Радиосеть, Ретранслятор, Алгоритм*.\n"
        "Каждый день сообщения сохраняются в отдельную таблицу даты (`dd.mm.yyyy`).\n\n"
        "Команды:\n"
        "/status — статистика базы данных\n"
        "/check_deleted — принудительная проверка удалённых сообщений"
    )
    await message.answer(text, parse_mode="Markdown")


@router.message(Command("status", "stats"))
async def cmd_status(message: Message):
    stats = await get_db_stats()
    tables_list = ", ".join(f"`{t}`" for t in stats["tables"][:5])
    if len(stats["tables"]) > 5:
        tables_list += f" и ещё {len(stats['tables']) - 5}"
    elif not tables_list:
        tables_list = "пока нет"

    response = (
        "📊 **Статистика базы данных:**\n"
        f"• Пользователей: `{stats['users_count']}`\n"
        f"• Таблиц дат (дней): `{stats['tables_count']}` ({tables_list})\n"
        f"• Сообщений сохранено: `{stats['msgs_count']}`\n"
        f"• Отредактированных (edit=1): `{stats['edited_count']}`\n"
        f"• Удалённых (delete=1): `{stats['deleted_count']}`\n"
        f"• Активных WebSocket-клиентов: `{len(manager.active_connections)}`"
    )
    await message.answer(response, parse_mode="Markdown")


@router.message(Command("check_deleted"))
async def cmd_check_deleted(message: Message):
    status_msg = await message.answer("🔄 Запуск проверки удалённых сообщений...")
    count = await check_all_active_messages(message.bot)
    await status_msg.edit_text(
        f"✅ Проверка завершена. Обнаружено и помечено удалёнными: `{count}` сообщений."
    )


@router.message(F.chat.id == TARGET_CHAT_ID)
async def handle_new_group_message(message: Message):
    """
    Handles new messages in the target supergroup.
    Checks keywords, ensures today's date table exists, saves message,
    and broadcasts NEW_MESSAGE to WebSocket clients.
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
    date_table_name, date_str = get_msk_datetimes(message.date)

    async with async_session_maker() as session:
        # Upsert user in global users table
        user = await upsert_user(session, tg_id=from_user.id, username=username)

    # Save message in the daily table corresponding to date_table_name
    saved_msg = await save_message(
        user_username=user.username,
        date_str=date_str,
        text_content=raw_text,
        message_id=message.message_id,
        date_table_name=date_table_name,
    )

    # Broadcast NEW_MESSAGE to WebSocket clients
    await manager.broadcast({
        "type": "NEW_MESSAGE",
        "data": saved_msg,
    })

    logger.info(
        f"Added message {message.message_id} from @{username} to table '{date_table_name}' and broadcasted."
    )


@router.edited_message(F.chat.id == TARGET_CHAT_ID)
async def handle_edited_group_message(message: Message):
    """
    Handles edited messages in the target supergroup.
    Finds the message in daily tables, updates text, sets edit=1,
    and broadcasts MESSAGE_EDITED to WebSocket clients.
    """
    raw_text = message.text or message.caption or ""
    message_id = message.message_id
    date_table_name, date_str = get_msk_datetimes(message.date)

    updated_msg = await find_and_update_edited_message(
        message_id=message_id,
        new_text=raw_text,
        preferred_date_table=date_table_name,
    )

    if updated_msg:
        await manager.broadcast({
            "type": "MESSAGE_EDITED",
            "data": updated_msg,
        })
        logger.info(
            f"Updated edited message {message_id} in DB (edit=1) and broadcasted."
        )
    else:
        # Message wasn't previously in DB, but edit now contains keywords
        if contains_keywords(raw_text):
            from_user = message.from_user
            username = from_user.username if from_user and from_user.username else f"user_{from_user.id if from_user else 'unknown'}"
            async with async_session_maker() as session:
                user = await upsert_user(session, tg_id=from_user.id, username=username)

            saved_msg = await save_message(
                user_username=user.username,
                date_str=date_str,
                text_content=raw_text,
                message_id=message_id,
                date_table_name=date_table_name,
            )
            # Update to set edit=1
            upd = await find_and_update_edited_message(
                message_id=message_id,
                new_text=raw_text,
                preferred_date_table=date_table_name,
            )
            target = upd or saved_msg
            await manager.broadcast({
                "type": "MESSAGE_EDITED",
                "data": target,
            })
            logger.info(
                f"Saved and broadcasted previously untracked message {message_id} after edit."
            )
