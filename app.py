from __future__ import annotations

import os
import random
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import phonenumbers
from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from phonenumbers.phonenumberutil import NumberParseException
from twilio.twiml.messaging_response import MessagingResponse

from models import (
    Bowl,
    CleaningLog,
    DEFAULT_CADENCE_DAYS,
    Pet,
    recommended_cadence_for,
    recent_clean_dates_for_bowl,
    calculate_streak,
    db,
    editable_cadence_options_for,
    get_bowl_label,
)
from scheduler import (
    clear_pending_reminder_state,
    compute_next_reminder_for_bowl,
    find_most_recent_due_bowl,
    format_local_label,
    get_pet_local_now,
    init_scheduler,
    log_cleaning,
    utcnow,
)
from sms import (
    build_completion_message,
    build_help_message,
    build_snooze_message,
    build_start_message,
    build_stop_message,
    build_verification_message,
    build_welcome_message,
    send_sms_message,
)


load_dotenv()

PET_TYPES = {"dog", "cat", "other"}
BOWL_TYPES = set(DEFAULT_CADENCE_DAYS)


def normalize_database_url(url: str | None) -> str:
    if not url:
        return "sqlite:///fresh_bowl.db"
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql://", 1)
    return url


def normalize_phone(raw_phone: str) -> str:
    try:
        parsed = phonenumbers.parse(raw_phone, "US")
    except NumberParseException as exc:
        raise ValueError("Please enter a valid US phone number.") from exc
    if not phonenumbers.is_valid_number(parsed):
        raise ValueError("Please enter a valid US phone number.")
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def clean_text(value: Any, field_name: str, max_length: int = 100) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        raise ValueError(f"{field_name} is required.")
    return cleaned[:max_length]


def parse_timezone_name(value: Any) -> str:
    timezone_name = str(value or "America/New_York").strip() or "America/New_York"
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("Timezone is invalid.") from exc
    return timezone_name


def parse_date_value(value: Any, field_name: str) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a valid date.") from exc


def parse_bowl_payloads(raw_bowls: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_bowls, list) or not raw_bowls:
        raise ValueError("Pick at least one bowl to track.")

    normalized: list[dict[str, Any]] = []
    seen_types: set[str] = set()
    for item in raw_bowls:
        if not isinstance(item, dict):
            raise ValueError("Each bowl entry must be an object.")

        bowl_type = str(item.get("bowl_type", "")).strip()
        if bowl_type not in BOWL_TYPES:
            raise ValueError("One of the selected bowl types is invalid.")
        if bowl_type in seen_types:
            raise ValueError("Each bowl type can only be selected once.")

        unknown_last_cleaned = bool(item.get("unknown_last_cleaned", False))
        last_cleaned = None if unknown_last_cleaned else parse_date_value(
            item.get("last_cleaned"),
            f"Last cleaned date for {get_bowl_label(bowl_type)}",
        )
        cadence_days = int(item.get("cadence_days") or DEFAULT_CADENCE_DAYS[bowl_type])
        if cadence_days <= 0:
            raise ValueError("Cadence must be greater than zero.")

        normalized.append(
            {
                "bowl_type": bowl_type,
                "unknown_last_cleaned": unknown_last_cleaned,
                "last_cleaned": last_cleaned,
                "cadence_days": cadence_days,
            }
        )
        seen_types.add(bowl_type)

    return normalized


def format_cadence_label(days: int) -> str:
    if days == 1:
        return "Daily"
    if days == 7:
        return "Weekly"
    if days == 14:
        return "Every 2 weeks"
    return f"Every {days} days"


def format_last_cleaned_label(last_cleaned: date | None, local_today: date) -> str:
    if last_cleaned is None:
        return "Not logged yet"
    day_diff = (local_today - last_cleaned).days
    if day_diff <= 0:
        return "Today"
    if day_diff == 1:
        return "Yesterday"
    return f"{day_diff} days ago"


def build_week_dots(hit_dates: set[date], reference_date: date) -> list[dict[str, Any]]:
    start = reference_date - timedelta(days=6)
    dots = []
    for index in range(7):
        current_date = start + timedelta(days=index)
        dots.append(
            {
                "date": current_date.isoformat(),
                "label": current_date.strftime("%a"),
                "filled": current_date in hit_dates,
            }
        )
    return dots


def summarize_first_reminder(pet: Pet) -> dict[str, Any] | None:
    active_bowls = [bowl for bowl in pet.bowls if bowl.active]
    if not active_bowls:
        return None
    bowl = min(active_bowls, key=lambda item: item.next_reminder)
    return {
        "bowl_id": bowl.id,
        "bowl_type": bowl.bowl_type,
        "bowl_label": get_bowl_label(bowl.bowl_type),
        "next_reminder": bowl.next_reminder.isoformat(),
        "next_reminder_label": format_local_label(bowl.next_reminder, pet.timezone),
        "cadence_days": bowl.cadence_days,
        "cadence_label": format_cadence_label(bowl.cadence_days),
    }


def serialize_bowl(bowl: Bowl, pet: Pet, reference_date: date) -> dict[str, Any]:
    clean_dates = recent_clean_dates_for_bowl(bowl.id, pet.timezone, reference_date=reference_date)
    return {
        "id": bowl.id,
        "bowl_type": bowl.bowl_type,
        "bowl_label": get_bowl_label(bowl.bowl_type),
        "cadence_days": bowl.cadence_days,
        "cadence_label": format_cadence_label(bowl.cadence_days),
        "recommended_cadence": recommended_cadence_for(bowl.bowl_type),
        "editable_cadence_options": editable_cadence_options_for(bowl.bowl_type),
        "last_cleaned": bowl.last_cleaned.isoformat() if bowl.last_cleaned else None,
        "last_cleaned_label": format_last_cleaned_label(bowl.last_cleaned, reference_date),
        "next_reminder": bowl.next_reminder.isoformat(),
        "next_reminder_label": format_local_label(bowl.next_reminder, pet.timezone),
        "active": bowl.active,
        "streak_dots": build_week_dots(clean_dates, reference_date),
    }


def build_dashboard_payload(pet: Pet) -> dict[str, Any]:
    local_today = get_pet_local_now(pet).date()
    bowls = [
        serialize_bowl(bowl, pet, local_today)
        for bowl in sorted(
            [item for item in pet.bowls if item.active],
            key=lambda item: (item.next_reminder, item.id),
        )
    ]
    return {
        "phone": pet.phone,
        "pet_name": pet.pet_name,
        "pet_type": pet.pet_type,
        "timezone": pet.timezone,
        "streak": calculate_streak(pet.id, pet.timezone, local_today),
        "bowls": bowls,
        "first_reminder": summarize_first_reminder(pet),
    }


def upsert_pet_and_bowls(pet: Pet, payload: dict[str, Any]) -> None:
    pet.pet_name = clean_text(payload.get("pet_name"), "Pet name")
    pet.pet_type = str(payload.get("pet_type") or "other").strip().lower()
    if pet.pet_type not in PET_TYPES:
        raise ValueError("Pet type is invalid.")
    pet.timezone = parse_timezone_name(payload.get("timezone"))
    pet.active = True

    bowl_payloads = parse_bowl_payloads(payload.get("bowls"))
    existing_bowls = {bowl.bowl_type: bowl for bowl in pet.bowls}
    selected_types = {item["bowl_type"] for item in bowl_payloads}

    for bowl_type, bowl in existing_bowls.items():
        if bowl_type not in selected_types:
            bowl.active = False

    for item in bowl_payloads:
        bowl = existing_bowls.get(item["bowl_type"])
        if bowl is None:
            bowl = Bowl(
                pet=pet,
                bowl_type=item["bowl_type"],
                cadence_days=item["cadence_days"],
                next_reminder=utcnow(),
                active=True,
            )
            db.session.add(bowl)

        bowl.active = True
        bowl.cadence_days = item["cadence_days"]
        bowl.last_cleaned = item["last_cleaned"]
        clear_pending_reminder_state(bowl)
        bowl.next_reminder = compute_next_reminder_for_bowl(
            pet,
            bowl.bowl_type,
            bowl.cadence_days,
            last_cleaned=item["last_cleaned"],
            unknown_last_cleaned=item["unknown_last_cleaned"],
        )


def create_app(test_config: dict[str, Any] | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.getenv("SECRET_KEY", "fresh-bowl-dev-secret"),
        SQLALCHEMY_DATABASE_URI=normalize_database_url(os.getenv("DATABASE_URL")),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SQLALCHEMY_ENGINE_OPTIONS={"pool_pre_ping": True},
        JSON_SORT_KEYS=False,
        TESTING=False,
    )
    if test_config:
        app.config.update(test_config)

    CORS(app, resources={r"/api/*": {"origins": "*"}, r"/sms/*": {"origins": "*"}})
    db.init_app(app)

    with app.app_context():
        db.create_all()

    @app.get("/")
    def index():
        return send_from_directory(Path(app.root_path) / "frontend", "index.html")

    @app.get("/api/health")
    def health():
        return jsonify({"status": "ok"})

    @app.post("/api/setup")
    def setup():
        payload = request.get_json(silent=True) or {}
        try:
            phone = normalize_phone(str(payload.get("phone", "")))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        pet = Pet.query.filter_by(phone=phone).one_or_none()
        is_new_pet = pet is None
        if pet is None:
            pet = Pet(phone=phone)
            db.session.add(pet)

        try:
            pet.phone = phone
            upsert_pet_and_bowls(pet, payload)
            db.session.flush()
        except ValueError as exc:
            db.session.rollback()
            return jsonify({"error": str(exc)}), 400

        response = {
            "phone": pet.phone,
            "pet_name": pet.pet_name,
            "verification_required": not pet.verified,
            "first_reminder": summarize_first_reminder(pet),
        }

        if pet.verified:
            db.session.commit()
            response["dashboard"] = build_dashboard_payload(pet)
            return jsonify(response), 201 if is_new_pet else 200

        verify_code = f"{random.randint(0, 999999):06d}"
        pet.verify_code = verify_code
        pet.verified = False
        db.session.commit()
        send_sms_message(pet.phone, build_verification_message(verify_code))
        if os.getenv("EXPOSE_VERIFY_CODE", "0") == "1":
            response["debug_verify_code"] = verify_code
        return jsonify(response), 201 if is_new_pet else 200

    @app.post("/api/verify")
    def verify():
        payload = request.get_json(silent=True) or {}
        try:
            phone = normalize_phone(str(payload.get("phone", "")))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        pet = Pet.query.filter_by(phone=phone).one_or_none()
        if pet is None:
            return jsonify({"error": "No Fresh Bowl account found for that phone."}), 404

        code = str(payload.get("code", "")).strip()
        if pet.verify_code != code:
            return jsonify({"error": "That verification code does not match."}), 400

        pet.verified = True
        pet.active = True
        pet.verify_code = None
        for bowl in pet.bowls:
            if bowl.active:
                clear_pending_reminder_state(bowl)
        db.session.commit()

        first_reminder = summarize_first_reminder(pet)
        if first_reminder is not None:
            send_sms_message(
                pet.phone,
                build_welcome_message(pet.pet_name, first_reminder["next_reminder_label"]),
            )

        return jsonify(
            {
                "confirmed_message": (
                    f"You're set! First reminder for {pet.pet_name}'s "
                    f"{first_reminder['bowl_label']} arrives {first_reminder['next_reminder_label']}."
                    if first_reminder
                    else f"You're set, {pet.pet_name}."
                ),
                "first_reminder": first_reminder,
                "dashboard": build_dashboard_payload(pet),
            }
        )

    @app.get("/api/dashboard/<path:phone_token>")
    def dashboard(phone_token: str):
        try:
            phone = normalize_phone(unquote(phone_token))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        pet = Pet.query.filter_by(phone=phone, verified=True).one_or_none()
        if pet is None:
            return jsonify({"error": "No verified Fresh Bowl account found."}), 404
        return jsonify(build_dashboard_payload(pet))

    @app.post("/api/update-cadence")
    def update_cadence():
        payload = request.get_json(silent=True) or {}
        try:
            phone = normalize_phone(str(payload.get("phone", "")))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        bowl_id = payload.get("bowl_id")
        cadence_days = int(payload.get("cadence_days") or 0)
        if cadence_days <= 0:
            return jsonify({"error": "Cadence must be greater than zero."}), 400

        pet = Pet.query.filter_by(phone=phone, verified=True).one_or_none()
        if pet is None:
            return jsonify({"error": "No verified Fresh Bowl account found."}), 404

        bowl = Bowl.query.filter_by(id=bowl_id, pet_id=pet.id, active=True).one_or_none()
        if bowl is None:
            return jsonify({"error": "That bowl was not found."}), 404

        bowl.cadence_days = cadence_days
        bowl.next_reminder = compute_next_reminder_for_bowl(
            pet,
            bowl.bowl_type,
            cadence_days,
            last_cleaned=bowl.last_cleaned,
            unknown_last_cleaned=bowl.last_cleaned is None,
        )
        clear_pending_reminder_state(bowl)
        db.session.commit()
        return jsonify(build_dashboard_payload(pet))

    @app.post("/sms/inbound")
    def inbound_sms():
        response = MessagingResponse()
        command = request.form.get("Body", "").strip().upper()
        raw_phone = request.form.get("From", "")

        try:
            phone = normalize_phone(raw_phone)
        except ValueError:
            response.message(build_help_message())
            return str(response), 200, {"Content-Type": "application/xml"}

        pet = Pet.query.filter_by(phone=phone).one_or_none()
        if pet is None:
            response.message("You are not on Fresh Bowl yet. Sign up first, then reply here.")
            return str(response), 200, {"Content-Type": "application/xml"}

        if command == "HELP":
            response.message(build_help_message())
            return str(response), 200, {"Content-Type": "application/xml"}

        if command == "STOP":
            pet.active = False
            for bowl in pet.bowls:
                bowl.active = False
            db.session.commit()
            response.message(build_stop_message())
            return str(response), 200, {"Content-Type": "application/xml"}

        if command == "START":
            pet.active = True
            for bowl in pet.bowls:
                bowl.active = True
                if bowl.next_reminder <= utcnow():
                    bowl.next_reminder = utcnow() + timedelta(minutes=5)
            db.session.commit()
            response.message(build_start_message())
            return str(response), 200, {"Content-Type": "application/xml"}

        if command == "DONE":
            bowl = find_most_recent_due_bowl(pet)
            if bowl is None:
                response.message("No due bowl found right now. If you already replied, you're all caught up.")
                return str(response), 200, {"Content-Type": "application/xml"}

            log_cleaning(bowl, method="done_reply")
            db.session.commit()
            streak = calculate_streak(pet.id, pet.timezone, get_pet_local_now(pet).date())
            response.message(
                build_completion_message(
                    pet.pet_name,
                    bowl.bowl_type,
                    streak,
                    format_local_label(bowl.next_reminder, pet.timezone),
                )
            )
            return str(response), 200, {"Content-Type": "application/xml"}

        if command == "SNOOZE":
            bowl = find_most_recent_due_bowl(pet)
            if bowl is None:
                response.message("No due bowl found to snooze right now.")
                return str(response), 200, {"Content-Type": "application/xml"}

            bowl.next_reminder = utcnow() + timedelta(hours=4)
            clear_pending_reminder_state(bowl)
            db.session.commit()
            response.message(
                build_snooze_message(
                    pet.pet_name,
                    bowl.bowl_type,
                    format_local_label(bowl.next_reminder, pet.timezone),
                )
            )
            return str(response), 200, {"Content-Type": "application/xml"}

        response.message(build_help_message())
        return str(response), 200, {"Content-Type": "application/xml"}

    should_start_scheduler = (
        not app.config.get("TESTING")
        and os.getenv("RUN_SCHEDULER", "1") == "1"
        and (os.getenv("WERKZEUG_RUN_MAIN") == "true" or not app.debug)
    )
    if should_start_scheduler:
        init_scheduler(app)

    return app


app = create_app()


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "5002")),
        debug=True,
        use_reloader=False,
    )
