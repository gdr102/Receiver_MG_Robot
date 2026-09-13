import logging
import re
from datetime import datetime
from sqlalchemy import (
    event,
    func,
    insert,
    select,
    text,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config import DB_URL
from database.models import Base, User, get_daily_table

logger = logging.getLogger(__name__)

engine = create_async_engine(DB_URL, echo=False)


@event.listens_for(engine.sync_engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


async_session_maker = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)

# In-memory cache of already created/verified daily table names
_created_tables: set[str] = set()

DATE_TABLE_REGEX = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")


async def init_db() -> None:
    """Initializes the database, creating the global users table."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database initialized (base tables verified).")


async def ensure_daily_table(date_str: str) -> None:
    """
    Checks if a table for date_str (format 'dd.mm.yyyy') exists in DB,
    and creates it if it does not exist yet.
    """
    if date_str in _created_tables:
        return

    table = get_daily_table(date_str)
    async with engine.begin() as conn:
        await conn.run_sync(table.create, checkfirst=True)

    _created_tables.add(date_str)
    logger.info(f"Daily table verified/created: '{date_str}'")


async def get_all_date_table_names() -> list[str]:
    """
    Returns all date table names (format 'dd.mm.yyyy') present in SQLite,
    sorted in descending order by date (newest first).
    """
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table'")
        )
        names = [row[0] for row in result.fetchall()]

    date_tables = [name for name in names if DATE_TABLE_REGEX.match(name)]

    # Sort descending by date
    def parse_date(name: str):
        try:
            return datetime.strptime(name, "%d.%m.%Y")
        except ValueError:
            return datetime.min

    date_tables.sort(key=parse_date, reverse=True)
    return date_tables


async def upsert_user(session: AsyncSession, tg_id: int, username: str) -> User:
    """Finds or creates a user by tg_id. If username changed, updates it."""
    stmt = select(User).where(User.tg_id == tg_id)
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()

    if user is None:
        check_user = await session.execute(
            select(User).where(User.username == username)
        )
        if check_user.scalar_one_or_none():
            username = f"{username}_{tg_id}"
        user = User(tg_id=tg_id, username=username)
        session.add(user)
        await session.commit()
        await session.refresh(user)
    else:
        if user.username != username:
            check_user = await session.execute(
                select(User).where(
                    User.username == username, User.tg_id != tg_id
                )
            )
            if check_user.scalar_one_or_none() is None:
                user.username = username
                await session.commit()
                await session.refresh(user)
    return user


async def save_message(
    user_username: str,
    date_str: str,
    text_content: str,
    message_id: int,
    date_table_name: str,
) -> dict:
    """
    Ensures daily table exists and saves a new message into it.
    Returns the message dictionary.
    """
    await ensure_daily_table(date_table_name)
    table = get_daily_table(date_table_name)

    async with async_session_maker() as session:
        check_stmt = select(table).where(table.c.message_id == message_id)
        res = await session.execute(check_stmt)
        existing = res.mappings().one_or_none()

        if existing:
            return dict(existing)

        insert_stmt = (
            insert(table)
            .values(
                user=user_username,
                date=date_str,
                message=text_content,
                message_id=message_id,
                edit=0,
                delete=0,
            )
            .returning(
                table.c.id,
                table.c.user,
                table.c.date,
                table.c.message,
                table.c.message_id,
                table.c.edit,
                table.c.delete,
            )
        )
        result = await session.execute(insert_stmt)
        await session.commit()
        row = dict(result.mappings().one())
        logger.info(
            f"Saved message [id={row['id']}, message_id={message_id}] to table '{date_table_name}'"
        )
        return row


async def find_and_update_edited_message(
    message_id: int,
    new_text: str,
    preferred_date_table: str | None = None,
) -> dict | None:
    """
    Searches for message by message_id across daily tables (checking preferred table first).
    If found, updates message text and sets edit=1.
    Returns updated message dict.
    """
    tables_to_check = []
    if preferred_date_table:
        tables_to_check.append(preferred_date_table)

    all_tables = await get_all_date_table_names()
    for t in all_tables:
        if t not in tables_to_check:
            tables_to_check.append(t)

    async with async_session_maker() as session:
        for t_name in tables_to_check:
            table = get_daily_table(t_name)
            stmt = select(table).where(table.c.message_id == message_id)
            res = await session.execute(stmt)
            row = res.mappings().one_or_none()

            if row:
                upd = (
                    update(table)
                    .where(table.c.message_id == message_id)
                    .values(message=new_text, edit=1)
                    .returning(
                        table.c.id,
                        table.c.user,
                        table.c.date,
                        table.c.message,
                        table.c.message_id,
                        table.c.edit,
                        table.c.delete,
                    )
                )
                upd_res = await session.execute(upd)
                await session.commit()
                updated_row = dict(upd_res.mappings().one())
                logger.info(
                    f"Updated edited message {message_id} in table '{t_name}' (edit=1)"
                )
                return updated_row

    return None


async def mark_daily_message_deleted(
    table_name: str, message_id: int
) -> dict | None:
    """Sets delete=1 for message in specified daily table."""
    table = get_daily_table(table_name)
    async with async_session_maker() as session:
        stmt = (
            update(table)
            .where(table.c.message_id == message_id, table.c.delete == 0)
            .values(delete=1)
            .returning(
                table.c.id,
                table.c.user,
                table.c.date,
                table.c.message,
                table.c.message_id,
                table.c.edit,
                table.c.delete,
            )
        )
        res = await session.execute(stmt)
        await session.commit()
        row = res.mappings().one_or_none()
        if row:
            return dict(row)
    return None


async def get_all_active_messages() -> list[tuple[str, dict]]:
    """
    Returns list of (table_name, message_dict) for all messages
    with delete=0 across all daily tables.
    """
    date_tables = await get_all_date_table_names()
    active_messages = []

    async with async_session_maker() as session:
        for t_name in date_tables:
            table = get_daily_table(t_name)
            stmt = (
                select(table)
                .where(table.c.delete == 0)
                .order_by(table.c.id.desc())
            )
            res = await session.execute(stmt)
            for row in res.mappings().all():
                active_messages.append((t_name, dict(row)))

    return active_messages


async def get_recent_messages(limit: int = 15) -> list[dict]:
    """
    Returns the last `limit` messages across all daily tables
    in chronological order for INIT_HISTORY.
    """
    date_tables = await get_all_date_table_names()
    collected: list[dict] = []

    async with async_session_maker() as session:
        for t_name in date_tables:
            if len(collected) >= limit:
                break
            needed = limit - len(collected)
            table = get_daily_table(t_name)
            stmt = (
                select(table)
                .order_by(table.c.id.desc())
                .limit(needed)
            )
            res = await session.execute(stmt)
            rows = [dict(r) for r in res.mappings().all()]
            collected.extend(rows)

    return list(reversed(collected))


async def get_db_stats() -> dict:
    """Computes overall statistics across all tables."""
    date_tables = await get_all_date_table_names()
    total_msgs = 0
    total_edited = 0
    total_deleted = 0

    async with async_session_maker() as session:
        users_count = await session.scalar(select(func.count(User.id)))

        for t_name in date_tables:
            table = get_daily_table(t_name)
            cnt = await session.scalar(select(func.count(table.c.id)))
            edits = await session.scalar(
                select(func.count(table.c.id)).where(table.c.edit == 1)
            )
            deletes = await session.scalar(
                select(func.count(table.c.id)).where(table.c.delete == 1)
            )
            total_msgs += cnt or 0
            total_edited += edits or 0
            total_deleted += deletes or 0

    return {
        "users_count": users_count or 0,
        "msgs_count": total_msgs,
        "edited_count": total_edited,
        "deleted_count": total_deleted,
        "tables_count": len(date_tables),
        "tables": date_tables,
    }
