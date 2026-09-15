import logging
import re
import pytz
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, Message

from config import AUTHORIZED_USER_ID, TARGET_CHAT_ID, TIMEZONE
from database import (
    async_session_maker,
    find_and_update_edited_message,
    get_db_stats,
    get_messages_by_period,
    save_message,
    upsert_user,
)
from filters import contains_keywords
from server import manager
from services.checker import check_all_active_messages
from services.stats import (
    create_stats_report,
    generate_stats_docx,
    parse_period,
)

logger = logging.getLogger(__name__)
router = Router(name="messages_router")

HELP_PROMPT = 'Чтобы получить статистику напишите период и время в формате "дд.мм.гггг чч.мм - дд.мм.гггг чч.мм".'


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


@router.message(Command("start"))
async def cmd_start(message: Message):
    """Replies to /start with the prompt to request period statistics."""
    await message.answer(HELP_PROMPT)


@router.message(Command("help"))
async def cmd_help(message: Message):
    text = (
        "🤖 <b>Бот Receiver MG Robot активен.</b>\n\n"
        f"Целевая группа: <code>{TARGET_CHAT_ID}</code>\n"
        "Отслеживаемые ключевые слова: <i>МГ, ОВЧ, Радиосеть, Ретранслятор, Алгоритм</i>.\n"
        "Каждый день сообщения сохраняются в отдельную таблицу даты (<code>dd.mm.yyyy</code>).\n\n"
        f"{HELP_PROMPT}"
    )
    await message.answer(text, parse_mode="HTML")


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


# ---------------------------------------------------------------------------
# Private Chat Messages (User 6373347786 Period Statistics)
# ---------------------------------------------------------------------------


@router.message(F.chat.type == "private")
async def handle_private_message(message: Message):
    """
    Handles user messages in private chat.
    Validates user authorization (ID: 6373347786).
    Parses 'dd.mm.yyyy HH.MM - dd.mm.yyyy HH.MM' and returns a table of radio network statistics.
    Digits are in <code> tags for instant copy-on-click in Telegram.
    No buttons are added to the message.
    """
    from_user = message.from_user
    if not from_user:
        return

    # Check authorized user
    if AUTHORIZED_USER_ID and from_user.id != AUTHORIZED_USER_ID:
        await message.answer("⛔ Доступ ограничен. Вы не авторизованы для работы с данным ботом.")
        return

    raw_text = (message.text or "").strip()

    # Parse period
    period = parse_period(raw_text)
    if not period:
        await message.answer(HELP_PROMPT)
        return

    start_dt, end_dt = period

    status_wait = await message.answer("⏳ Выполняется подсчёт радиограмм за указанный период...")

    # Query non-deleted messages in timeframe across all daily tables
    messages = await get_messages_by_period(start_dt, end_dt)

    if not messages:
        await status_wait.edit_text(
            f"Итого за период {raw_text}\n\n"
            "Радиограмм за указанный период не найдено.\n\n"
            f"{HELP_PROMPT}"
        )
        return

    report_rich = create_stats_report(
        period_str=raw_text,
        messages=messages,
    )
    docx_stream = generate_stats_docx(
        period_str=raw_text,
        messages=messages,
    )

    clean_period = re.sub(r"[^\w\.\-]+", "_", raw_text).strip("_")
    doc_file = BufferedInputFile(
        file=docx_stream.getvalue(),
        filename=f"Статистика_{clean_period}.docx",
    )

    await status_wait.delete()
    # 1. Sent as Rich Message with bordered native table using sendRichMessage
    await message.answer_rich(rich_message=report_rich)
    # 2. Sent Word (.docx) document file
    await message.answer_document(document=doc_file)


# ---------------------------------------------------------------------------
# Group Message Handlers (Target Supergroup: -1004290775156)
# ---------------------------------------------------------------------------


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
            username = (
                from_user.username
                if from_user and from_user.username
                else f"user_{from_user.id if from_user else 'unknown'}"
            )
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
