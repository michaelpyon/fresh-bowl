from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler

from models import (
    Bowl,
    CleaningLog,
    Pet,
    UNKNOWN_LAST_CLEANED_HOURS,
    db,
    get_bowl_label,
)
from sms import build_follow_up_message, build_reminder_message, send_sms_message


logger = logging.getLogger(__name__)
scheduler: BackgroundScheduler | None = None
DEFAULT_REMINDER_HOUR = 9


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def get_timezone(pet: Pet) -> ZoneInfo:
    return ZoneInfo(pet.timezone or "America/New_York")


def get_pet_local_now(pet: Pet, base_utc: datetime | None = None) -> datetime:
    base = (base_utc or utcnow()).replace(tzinfo=timezone.utc)
    return base.astimezone(get_timezone(pet))


def round_up_minutes(base_utc: datetime, minutes: int = 5) -> datetime:
    remainder = base_utc.minute % minutes
    delta_minutes = 0 if remainder == 0 and base_utc.second == 0 and base_utc.microsecond == 0 else minutes - remainder
    rounded = base_utc + timedelta(minutes=delta_minutes)
    return rounded.replace(second=0, microsecond=0)


def compute_next_reminder_for_bowl(
    pet: Pet,
    bowl_type: str,
    cadence_days: int,
    *,
    last_cleaned: date | None,
    unknown_last_cleaned: bool = False,
    base_utc: datetime | None = None,
) -> datetime:
    now_utc = base_utc or utcnow()
    if unknown_last_cleaned or last_cleaned is None:
        offset_hours = UNKNOWN_LAST_CLEANED_HOURS.get(bowl_type, 12)
        return round_up_minutes(now_utc + timedelta(hours=offset_hours), 15)

    due_date = last_cleaned + timedelta(days=cadence_days)
    due_local = datetime.combine(due_date, time(hour=DEFAULT_REMINDER_HOUR), tzinfo=get_timezone(pet))
    due_utc = due_local.astimezone(timezone.utc).replace(tzinfo=None)
    if due_utc <= now_utc:
        return round_up_minutes(now_utc + timedelta(minutes=5), 5)
    return due_utc


def clear_pending_reminder_state(bowl: Bowl) -> None:
    bowl.last_reminder_sent_at = None
    bowl.last_follow_up_sent_at = None
    bowl.follow_up_due_at = None


def log_cleaning(
    bowl: Bowl,
    *,
    method: str,
    cleaned_at: datetime | None = None,
) -> CleaningLog:
    cleaned_utc = cleaned_at or utcnow()
    pet = bowl.pet
    local_date = cleaned_utc.replace(tzinfo=timezone.utc).astimezone(get_timezone(pet)).date()

    log = CleaningLog(bowl_id=bowl.id, cleaned_at=cleaned_utc, method=method)
    bowl.last_cleaned = local_date
    bowl.next_reminder = compute_next_reminder_for_bowl(
        pet,
        bowl.bowl_type,
        bowl.cadence_days,
        last_cleaned=local_date,
        base_utc=cleaned_utc,
    )
    clear_pending_reminder_state(bowl)
    db.session.add(log)
    return log


def format_local_label(dt_utc: datetime, timezone_name: str) -> str:
    tz = ZoneInfo(timezone_name or "America/New_York")
    local_dt = dt_utc.replace(tzinfo=timezone.utc).astimezone(tz)
    now_local = datetime.now(tz)
    if local_dt.date() == now_local.date():
        return f"today at {local_dt.strftime('%-I:%M %p')}"
    if local_dt.date() == now_local.date() + timedelta(days=1):
        return f"tomorrow at {local_dt.strftime('%-I:%M %p')}"
    return local_dt.strftime("%a, %b %-d at %-I:%M %p")


def find_most_recent_due_bowl(pet: Pet) -> Bowl | None:
    now = utcnow()
    return (
        Bowl.query.filter_by(pet_id=pet.id, active=True)
        .filter(
            (Bowl.last_reminder_sent_at.isnot(None)) | (Bowl.next_reminder <= now),
        )
        .order_by(Bowl.last_reminder_sent_at.desc(), Bowl.next_reminder.asc())
        .first()
    )


def send_due_reminders(app) -> dict[str, int]:
    with app.app_context():
        now = utcnow()
        primary_sent = 0
        follow_up_sent = 0

        due_bowls = (
            Bowl.query.join(Pet)
            .filter(
                Bowl.active.is_(True),
                Pet.active.is_(True),
                Pet.verified.is_(True),
                Bowl.next_reminder <= now,
            )
            .order_by(Bowl.next_reminder.asc(), Bowl.id.asc())
            .all()
        )

        for bowl in due_bowls:
            if bowl.last_reminder_sent_at is not None and bowl.last_reminder_sent_at >= bowl.next_reminder:
                continue

            pet = bowl.pet
            body = build_reminder_message(
                pet_name=pet.pet_name,
                bowl_type=bowl.bowl_type,
                seed=(bowl.id * 31) + int(bowl.next_reminder.timestamp()),
            )
            send_sms_message(pet.phone, body)
            bowl.last_reminder_sent_at = now
            bowl.last_follow_up_sent_at = None
            bowl.follow_up_due_at = now + timedelta(hours=24)
            primary_sent += 1

        follow_up_bowls = (
            Bowl.query.join(Pet)
            .filter(
                Bowl.active.is_(True),
                Pet.active.is_(True),
                Pet.verified.is_(True),
                Bowl.follow_up_due_at.isnot(None),
                Bowl.follow_up_due_at <= now,
                Bowl.last_follow_up_sent_at.is_(None),
            )
            .order_by(Bowl.follow_up_due_at.asc(), Bowl.id.asc())
            .all()
        )

        for bowl in follow_up_bowls:
            pet = bowl.pet
            body = build_follow_up_message(
                pet_name=pet.pet_name,
                bowl_type=bowl.bowl_type,
                seed=(bowl.id * 17) + now.day,
            )
            send_sms_message(pet.phone, body)
            bowl.last_follow_up_sent_at = now
            bowl.follow_up_due_at = None
            follow_up_sent += 1

        db.session.commit()
        return {"primary_sent": primary_sent, "follow_up_sent": follow_up_sent}


def init_scheduler(app) -> BackgroundScheduler:
    global scheduler
    if scheduler and scheduler.running:
        return scheduler

    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        send_due_reminders,
        "interval",
        minutes=5,
        id="send_due_reminders",
        args=[app],
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Fresh Bowl scheduler started.")
    return scheduler
