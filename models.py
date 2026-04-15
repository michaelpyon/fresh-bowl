from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.orm import Mapped, mapped_column, relationship


db = SQLAlchemy()


DEFAULT_CADENCE_DAYS = {
    "water": 2,
    "food": 1,
    "fountain": 14,
    "auto_feeder": 7,
}

BOWL_LABELS = {
    "water": "Water Bowl",
    "food": "Food Bowl",
    "fountain": "Fountain",
    "auto_feeder": "Auto Feeder",
}

RECOMMENDED_CADENCE_TEXT = {
    "water": "Every 1-2 days",
    "food": "Daily",
    "fountain": "Every 2 weeks",
    "auto_feeder": "Weekly",
}

UNKNOWN_LAST_CLEANED_HOURS = {
    "water": 8,
    "food": 6,
    "fountain": 24,
    "auto_feeder": 18,
}

EDITABLE_CADENCE_OPTIONS = {
    "water": [1, 2, 3],
    "food": [1, 2],
    "fountain": [7, 14, 21],
    "auto_feeder": [3, 7, 10],
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def get_bowl_label(bowl_type: str) -> str:
    return BOWL_LABELS.get(bowl_type, bowl_type.replace("_", " ").title())


def default_cadence_for(bowl_type: str) -> int:
    return DEFAULT_CADENCE_DAYS[bowl_type]


def recommended_cadence_for(bowl_type: str) -> str:
    return RECOMMENDED_CADENCE_TEXT[bowl_type]


def editable_cadence_options_for(bowl_type: str) -> list[int]:
    return EDITABLE_CADENCE_OPTIONS[bowl_type]


class Pet(db.Model):
    __tablename__ = "pets"

    id: Mapped[int] = mapped_column(primary_key=True)
    phone: Mapped[str] = mapped_column(db.String(20), unique=True, nullable=False, index=True)
    pet_name: Mapped[str] = mapped_column(db.String(100), nullable=False)
    pet_type: Mapped[str | None] = mapped_column(db.String(20))
    timezone: Mapped[str] = mapped_column(db.String(50), default="America/New_York", nullable=False)
    verified: Mapped[bool] = mapped_column(db.Boolean, default=False, nullable=False)
    verify_code: Mapped[str | None] = mapped_column(db.String(6))
    active: Mapped[bool] = mapped_column(db.Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(db.DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        db.DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )

    bowls: Mapped[list["Bowl"]] = relationship(
        back_populates="pet", cascade="all, delete-orphan", order_by="Bowl.id"
    )


class Bowl(db.Model):
    __tablename__ = "bowls"

    id: Mapped[int] = mapped_column(primary_key=True)
    pet_id: Mapped[int] = mapped_column(db.ForeignKey("pets.id"), nullable=False, index=True)
    bowl_type: Mapped[str] = mapped_column(db.String(30), nullable=False)
    cadence_days: Mapped[int] = mapped_column(db.Integer, nullable=False)
    last_cleaned: Mapped[date | None] = mapped_column(db.Date)
    next_reminder: Mapped[datetime] = mapped_column(db.DateTime, nullable=False, index=True)
    active: Mapped[bool] = mapped_column(db.Boolean, default=True, nullable=False)
    last_reminder_sent_at: Mapped[datetime | None] = mapped_column(db.DateTime)
    last_follow_up_sent_at: Mapped[datetime | None] = mapped_column(db.DateTime)
    follow_up_due_at: Mapped[datetime | None] = mapped_column(db.DateTime)
    created_at: Mapped[datetime] = mapped_column(db.DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        db.DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )

    pet: Mapped[Pet] = relationship(back_populates="bowls")
    cleaning_logs: Mapped[list["CleaningLog"]] = relationship(
        back_populates="bowl",
        cascade="all, delete-orphan",
        order_by="CleaningLog.cleaned_at.desc()",
    )


class CleaningLog(db.Model):
    __tablename__ = "cleaning_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    bowl_id: Mapped[int] = mapped_column(db.ForeignKey("bowls.id"), nullable=False, index=True)
    cleaned_at: Mapped[datetime] = mapped_column(db.DateTime, default=utcnow, nullable=False)
    method: Mapped[str | None] = mapped_column(db.String(20))

    bowl: Mapped[Bowl] = relationship(back_populates="cleaning_logs")


def calculate_streak(
    pet_id: int,
    timezone_name: str,
    reference_date: date | None = None,
) -> int:
    tz = ZoneInfo(timezone_name or "America/New_York")
    local_today = reference_date or datetime.now(tz).date()

    rows = (
        db.session.query(CleaningLog.cleaned_at)
        .join(Bowl, Bowl.id == CleaningLog.bowl_id)
        .filter(Bowl.pet_id == pet_id)
        .all()
    )

    hit_days = {
        cleaned_at.replace(tzinfo=timezone.utc).astimezone(tz).date()
        for (cleaned_at,) in rows
        if cleaned_at is not None
    }

    streak = 0
    cursor = local_today
    while cursor in hit_days:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


def recent_clean_dates_for_pet(
    pet_id: int,
    timezone_name: str,
    *,
    days: int = 7,
    reference_date: date | None = None,
) -> set[date]:
    tz = ZoneInfo(timezone_name or "America/New_York")
    local_today = reference_date or datetime.now(tz).date()
    start_date = local_today - timedelta(days=days - 1)

    rows = (
        db.session.query(CleaningLog.cleaned_at)
        .join(Bowl, Bowl.id == CleaningLog.bowl_id)
        .filter(Bowl.pet_id == pet_id)
        .all()
    )

    return {
        local_date
        for (cleaned_at,) in rows
        if cleaned_at is not None
        for local_date in [cleaned_at.replace(tzinfo=timezone.utc).astimezone(tz).date()]
        if start_date <= local_date <= local_today
    }


def recent_clean_dates_for_bowl(
    bowl_id: int,
    timezone_name: str,
    *,
    days: int = 7,
    reference_date: date | None = None,
) -> set[date]:
    tz = ZoneInfo(timezone_name or "America/New_York")
    local_today = reference_date or datetime.now(tz).date()
    start_date = local_today - timedelta(days=days - 1)

    rows = db.session.query(CleaningLog.cleaned_at).filter(CleaningLog.bowl_id == bowl_id).all()
    return {
        local_date
        for (cleaned_at,) in rows
        if cleaned_at is not None
        for local_date in [cleaned_at.replace(tzinfo=timezone.utc).astimezone(tz).date()]
        if start_date <= local_date <= local_today
    }
