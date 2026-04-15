# Fresh Bowl

Pet bowl cleaning reminder app with phone verification, SMS reminders, streak tracking, and a single-file frontend.

## Stack

- Backend: Flask + SQLAlchemy + APScheduler
- Database: PostgreSQL on Railway or SQLite locally
- Messaging: Twilio SMS with mocked local fallback
- Frontend: single `frontend/index.html` file with vanilla JS

## Local setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

The backend runs on `http://localhost:5002` by default and serves the single-page frontend at `/`.

## Environment variables

Copy `.env.example` and set:

- `DATABASE_URL`
- `PORT`
- `RUN_SCHEDULER`
- `EXPOSE_VERIFY_CODE`
- `TWILIO_ACCOUNT_SID`
- `TWILIO_AUTH_TOKEN`
- `TWILIO_PHONE_NUMBER`

When Twilio credentials are missing, outbound sends are mocked so local setup and tests still work.

## Twilio webhook

Point your Twilio number's inbound messaging webhook to:

```text
https://your-railway-app.up.railway.app/sms/inbound
```

Supported commands:

- `DONE`
- `SNOOZE`
- `STOP`
- `START`
- `HELP`

## Frontend artifact

The standalone frontend file is:

- `frontend/index.html`

If you host it separately on Vercel instead of through Flask, set `window.FRESH_BOWL_API_BASE` before the script runs so API requests point at the Railway backend.

## Tests

```bash
pytest
```

The test suite covers setup, phone verification, dashboard loading, cadence updates, snoozing, and `DONE` logging.

## Deployment note

`Procfile` uses a single Gunicorn worker:

```text
web: gunicorn -w 1 app:app
```

That is intentional so the in-process APScheduler reminder loop does not run multiple times.
