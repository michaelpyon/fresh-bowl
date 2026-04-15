import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

import pytest

os.environ["RUN_SCHEDULER"] = "0"
os.environ["EXPOSE_VERIFY_CODE"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import create_app
from models import Bowl, CleaningLog, Pet, db
from scheduler import send_due_reminders, utcnow


@pytest.fixture()
def app():
    db_fd, db_path = tempfile.mkstemp()
    test_app = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_path}",
        }
    )
    with test_app.app_context():
        db.drop_all()
        db.create_all()
        yield test_app
        db.session.remove()
        db.drop_all()
    os.close(db_fd)
    os.unlink(db_path)


@pytest.fixture()
def client(app):
    return app.test_client()


def setup_payload():
    return {
        "pet_name": "Mochi",
        "pet_type": "cat",
        "phone": "+13105551234",
        "timezone": "America/New_York",
        "bowls": [
            {
                "bowl_type": "water",
                "last_cleaned": "2026-03-23",
                "unknown_last_cleaned": False,
            },
            {
                "bowl_type": "food",
                "last_cleaned": None,
                "unknown_last_cleaned": True,
            },
        ],
    }


def verify_pet(client):
    setup_response = client.post("/api/setup", json=setup_payload())
    assert setup_response.status_code == 201
    setup_data = setup_response.get_json()

    verify_response = client.post(
        "/api/verify",
        json={
            "phone": setup_payload()["phone"],
            "code": setup_data["debug_verify_code"],
        },
    )
    assert verify_response.status_code == 200
    return verify_response.get_json()


def test_setup_verify_and_dashboard_flow(client):
    setup_response = client.post("/api/setup", json=setup_payload())
    assert setup_response.status_code == 201
    setup_data = setup_response.get_json()
    assert setup_data["verification_required"] is True
    assert setup_data["first_reminder"]["bowl_label"] in {"Water Bowl", "Food Bowl"}
    assert setup_data["debug_verify_code"]

    verify_response = client.post(
        "/api/verify",
        json={
            "phone": setup_payload()["phone"],
            "code": setup_data["debug_verify_code"],
        },
    )
    assert verify_response.status_code == 200
    verify_data = verify_response.get_json()
    assert verify_data["dashboard"]["pet_name"] == "Mochi"
    assert len(verify_data["dashboard"]["bowls"]) == 2

    dashboard_response = client.get("/api/dashboard/%2B13105551234")
    assert dashboard_response.status_code == 200
    assert dashboard_response.get_json()["pet_name"] == "Mochi"


def test_snooze_done_and_update_cadence_flow(app, client):
    verify_pet(client)

    with app.app_context():
        pet = Pet.query.filter_by(phone="+13105551234").one()
        bowl = Bowl.query.filter_by(pet_id=pet.id, bowl_type="water").one()
        bowl.next_reminder = utcnow() - timedelta(minutes=10)
        bowl.last_reminder_sent_at = None
        db.session.commit()

    send_due_reminders(app)

    snooze_response = client.post(
        "/sms/inbound",
        data={"From": "+13105551234", "Body": "SNOOZE"},
    )
    assert snooze_response.status_code == 200
    assert "snoozed" in snooze_response.get_data(as_text=True).lower()

    with app.app_context():
        pet = Pet.query.filter_by(phone="+13105551234").one()
        bowl = Bowl.query.filter_by(pet_id=pet.id, bowl_type="water").one()
        updated_dashboard = client.post(
            "/api/update-cadence",
            json={
                "phone": pet.phone,
                "bowl_id": bowl.id,
                "cadence_days": 3,
            },
        )
        assert updated_dashboard.status_code == 200
        assert updated_dashboard.get_json()["bowls"][0]["cadence_days"] in {1, 3}

        bowl.next_reminder = utcnow() - timedelta(minutes=5)
        bowl.last_reminder_sent_at = None
        db.session.commit()

    send_due_reminders(app)

    done_response = client.post(
        "/sms/inbound",
        data={"From": "+13105551234", "Body": "DONE"},
    )
    assert done_response.status_code == 200
    assert "logged clean" in done_response.get_data(as_text=True).lower()

    with app.app_context():
        pet = Pet.query.filter_by(phone="+13105551234").one()
        bowl = Bowl.query.filter_by(pet_id=pet.id, bowl_type="water").one()
        assert bowl.last_cleaned is not None
        assert bowl.next_reminder > utcnow()
        assert CleaningLog.query.join(Bowl).filter(Bowl.pet_id == pet.id).count() >= 1
