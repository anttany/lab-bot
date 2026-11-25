from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, TypeDecorator
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return value
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return value
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tg_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    first_name: Mapped[str] = mapped_column(String(128), default="")
    last_name: Mapped[str] = mapped_column(String(128), default="")
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow
    )

    queue_entries: Mapped[list[QueueEntry]] = relationship(back_populates="user")


class Subject(Base):
    __tablename__ = "subjects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)

    labs: Mapped[list[Lab]] = relationship(back_populates="subject")


class Lab(Base):
    __tablename__ = "labs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    external_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    subject_id: Mapped[int] = mapped_column(ForeignKey("subjects.id"))
    starts_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    classroom: Mapped[str | None] = mapped_column(String(64), nullable=True)
    teacher: Mapped[str | None] = mapped_column(String(128), nullable=True)
    max_seats: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notified_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    opened_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    posted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    cancelled: Mapped[bool] = mapped_column(default=False)

    subject: Mapped[Subject] = relationship(back_populates="labs")
    queue_entries: Mapped[list[QueueEntry]] = relationship(
        back_populates="lab",
        order_by="QueueEntry.place",
    )


class QueueEntry(Base):
    __tablename__ = "queue_entries"
    __table_args__ = (UniqueConstraint("lab_id", "queue_kind", "place"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lab_id: Mapped[int] = mapped_column(ForeignKey("labs.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    place: Mapped[int] = mapped_column(Integer)
    queue_kind: Mapped[str] = mapped_column(String(16), default="main")
    signed_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow
    )

    lab: Mapped[Lab] = relationship(back_populates="queue_entries")
    user: Mapped[User] = relationship(back_populates="queue_entries")


class SeatInvite(Base):
    __tablename__ = "seat_invites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lab_id: Mapped[int] = mapped_column(ForeignKey("labs.id"), index=True)
    queue_kind: Mapped[str] = mapped_column(String(16))
    place: Mapped[int] = mapped_column(Integer)
    entry_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    from_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    to_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class AppState(Base):
    __tablename__ = "app_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(256))
