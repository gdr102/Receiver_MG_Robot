from sqlalchemy import BigInteger, Column, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tg_id = Column(BigInteger, unique=True, nullable=False, index=True)
    username = Column(String(255), unique=True, nullable=False, index=True)

    messages = relationship("Message", back_populates="user_rel")

    def __repr__(self) -> str:
        return f"<User(id={self.id}, tg_id={self.tg_id}, username={self.username})>"


class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user = Column(
        String(255),
        ForeignKey("users.username", onupdate="CASCADE", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    date = Column(String(50), nullable=False)
    message = Column(Text, nullable=False)
    message_id = Column(BigInteger, unique=True, nullable=False, index=True)
    edit = Column(Integer, default=0, nullable=False)
    delete = Column(Integer, default=0, nullable=False)

    user_rel = relationship("User", back_populates="messages")

    def __repr__(self) -> str:
        return (
            f"<Message(id={self.id}, user='{self.user}', message_id={self.message_id}, "
            f"edit={self.edit}, delete={self.delete})>"
        )
