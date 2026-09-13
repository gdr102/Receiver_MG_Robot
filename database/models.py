from sqlalchemy import BigInteger, Column, ForeignKey, Integer, String, Table, Text
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tg_id = Column(BigInteger, unique=True, nullable=False, index=True)
    username = Column(String(255), unique=True, nullable=False, index=True)

    def __repr__(self) -> str:
        return f"<User(id={self.id}, tg_id={self.tg_id}, username={self.username})>"


_daily_tables: dict[str, Table] = {}


def get_daily_table(date_str: str) -> Table:
    """
    Returns or dynamically creates a SQLAlchemy Table definition for a specific date (e.g. '13.09.2026').
    """
    if date_str in _daily_tables:
        return _daily_tables[date_str]

    table = Table(
        date_str,
        Base.metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column(
            "user",
            String(255),
            ForeignKey("users.username", onupdate="CASCADE", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        Column("date", String(50), nullable=False),
        Column("message", Text, nullable=False),
        Column("message_id", BigInteger, unique=True, nullable=False, index=True),
        Column("edit", Integer, default=0, nullable=False),
        Column("delete", Integer, default=0, nullable=False),
        extend_existing=True,
    )
    _daily_tables[date_str] = table
    return table
