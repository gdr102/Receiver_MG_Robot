import logging
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config import DB_URL
from database.models import Base, Message, User

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


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database initialized successfully.")


async def upsert_user(session: AsyncSession, tg_id: int, username: str) -> User:
    """Finds or creates a user by tg_id. If username changed, updates it."""
    stmt = select(User).where(User.tg_id == tg_id)
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()

    if user is None:
        # Check if username is already in use by another user
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
    session: AsyncSession,
    user_username: str,
    date_str: str,
    text: str,
    message_id: int,
) -> Message:
    """Saves a new message or returns existing one."""
    stmt = select(Message).where(Message.message_id == message_id)
    result = await session.execute(stmt)
    msg = result.scalar_one_or_none()

    if msg is None:
        msg = Message(
            user=user_username,
            date=date_str,
            message=text,
            message_id=message_id,
            edit=0,
            delete=0,
        )
        session.add(msg)
        await session.commit()
        await session.refresh(msg)
        logger.info(
            f"Saved new message [id={msg.id}, message_id={message_id}] from user '{user_username}'"
        )
    return msg


async def get_message_by_message_id(
    session: AsyncSession, message_id: int
) -> Message | None:
    stmt = select(Message).where(Message.message_id == message_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def update_edited_message(
    session: AsyncSession, message_id: int, new_text: str
) -> Message | None:
    """Updates message text and sets edit=1 if message exists in DB."""
    stmt = select(Message).where(Message.message_id == message_id)
    result = await session.execute(stmt)
    msg = result.scalar_one_or_none()

    if msg:
        msg.message = new_text
        msg.edit = 1
        await session.commit()
        await session.refresh(msg)
        logger.info(
            f"Updated edited message [id={msg.id}, message_id={message_id}], edit flag set to 1"
        )
        return msg
    return None


async def mark_message_deleted(
    session: AsyncSession, message_id: int
) -> Message | None:
    """Sets delete=1 for the message."""
    stmt = select(Message).where(Message.message_id == message_id)
    result = await session.execute(stmt)
    msg = result.scalar_one_or_none()

    if msg and msg.delete == 0:
        msg.delete = 1
        await session.commit()
        await session.refresh(msg)
        logger.info(
            f"Marked message [id={msg.id}, message_id={message_id}] as deleted (delete=1)"
        )
        return msg
    return None


async def get_active_messages(
    session: AsyncSession, limit: int = 200
) -> list[Message]:
    """Returns active (delete=0) messages ordered by most recent."""
    stmt = (
        select(Message)
        .where(Message.delete == 0)
        .order_by(Message.id.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_recent_messages(
    session: AsyncSession, limit: int = 15
) -> list[dict]:
    """Returns the last limit messages in chronological order for INIT_HISTORY."""
    stmt = (
        select(Message)
        .order_by(Message.id.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    messages = result.scalars().all()
    return [
        {
            "id": m.id,
            "user": m.user,
            "date": m.date,
            "message": m.message,
            "message_id": m.message_id,
            "edit": m.edit,
            "delete": m.delete,
        }
        for m in reversed(messages)
    ]
