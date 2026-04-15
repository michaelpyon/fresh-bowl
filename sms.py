from __future__ import annotations

import logging
import os
from datetime import datetime

from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client

from models import get_bowl_label


logger = logging.getLogger(__name__)


HEALTH_FACTS = [
    "Bacteria can double in a pet's water bowl every 8 hours.",
    "Biofilm can build up long before a bowl looks dirty.",
    "That slimy layer can irritate a cat's chin and skin.",
    "Stainless steel bowls usually hold onto less bacteria than plastic.",
    "Rinsing is not the same as cleaning with soap and hot water.",
    "Plastic scratches create tiny places for bacteria to hide.",
    "Fresh water tends to mean better hydration for cats.",
    "A clean bowl can make picky pets more willing to drink.",
    "Dirty food bowls can leave residue that attracts bacteria fast.",
    "A cleaner water source can help lower irritation around the mouth.",
    "Water fountains need regular scrubbing, not just top-offs.",
    "Fountain filters help, but they do not replace a real wash.",
    "Food residue can go sour faster than most people expect.",
    "Pets smell stale bowls before humans do.",
    "A quick scrub now can prevent a much grosser cleanup later.",
    "Warm apartments speed up bacterial growth in bowls.",
    "Soap, hot water, and a full dry beat a quick rinse every time.",
    "A cleaner bowl can make refill time feel fresher to your pet.",
    "Daily refills do not stop biofilm from forming on the sides.",
    "Standing water gets funky faster than it looks.",
    "Even healthy pets benefit from cleaner feeding gear.",
    "Fountains grow grime in seams and pumps, not just the basin.",
    "Food bowls collect oil and saliva even after a light snack.",
    "A regular cleaning cadence removes guesswork and guilt.",
    "A clean bowl is one of the easiest hygiene wins in pet care.",
    "Old food residue can change how appealing the next meal smells.",
    "Fresh surfaces help keep bowls from developing stubborn odors.",
    "Cleaning the bowl helps the whole refill routine stay sanitary.",
    "Pet bowls sit low, which means they pick up dust and floor grime fast.",
    "The cleanest-looking bowl is not always the cleanest bowl.",
    "A weekly feeder wipe-down is better than waiting for buildup.",
    "Consistent bowl cleaning is a tiny habit with outsized payoff.",
]


REMINDER_TEMPLATES = [
    "{pet_name}'s {bowl_label} needs a clean. {health_fact} Reply DONE when done.",
    "Fresh Bowl check: time to scrub {pet_name}'s {bowl_label}. {health_fact} Reply DONE when done.",
    "{pet_name}'s {bowl_label} is due for a wash. {health_fact} Reply DONE when done.",
    "Quick pet-care reset: clean {pet_name}'s {bowl_label}. {health_fact} Reply DONE when done.",
    "Small task, big difference: {pet_name}'s {bowl_label} needs attention. {health_fact} Reply DONE when done.",
    "Time to freshen up {pet_name}'s {bowl_label}. {health_fact} Reply DONE when done.",
    "{pet_name}'s {bowl_label} would love a clean start. {health_fact} Reply DONE when done.",
    "Friendly nudge: wash {pet_name}'s {bowl_label}. {health_fact} Reply DONE when done.",
    "Fresh Bowl here. {pet_name}'s {bowl_label} is ready for a scrub. {health_fact} Reply DONE when done.",
    "Two-minute cleanup time for {pet_name}'s {bowl_label}. {health_fact} Reply DONE when done.",
    "{pet_name}'s {bowl_label} is on the clock. {health_fact} Reply DONE when done.",
    "Pet hygiene ping: clean {pet_name}'s {bowl_label}. {health_fact} Reply DONE when done.",
    "Fresh water and clean surfaces go together. Wash {pet_name}'s {bowl_label}. {health_fact} Reply DONE when done.",
    "A quick bowl scrub for {pet_name} is due. {health_fact} Reply DONE when done.",
    "{pet_name}'s {bowl_label} has hit its clean-by window. {health_fact} Reply DONE when done.",
    "Keep it fresh for {pet_name}: clean the {bowl_label}. {health_fact} Reply DONE when done.",
    "That bowl is due. Give {pet_name}'s {bowl_label} a proper clean. {health_fact} Reply DONE when done.",
    "Care gap closed in two minutes: wash {pet_name}'s {bowl_label}. {health_fact} Reply DONE when done.",
    "Fresh Bowl reminder: {pet_name}'s {bowl_label} needs a reset. {health_fact} Reply DONE when done.",
    "A cleaner {bowl_label} is the move right now for {pet_name}. {health_fact} Reply DONE when done.",
]


FOLLOW_UP_TEMPLATES = [
    "Still pending: {pet_name}'s {bowl_label} could use a clean. {health_fact} Reply DONE when finished.",
    "Gentle follow-up for {pet_name}: the {bowl_label} is still due. {health_fact} Reply DONE when finished.",
    "Quick check-in: have you cleaned {pet_name}'s {bowl_label} yet? {health_fact} Reply DONE when finished.",
]


def build_verification_message(code: str) -> str:
    return f"Fresh Bowl verification code: {code}. Reply STOP any time to pause reminders."


def build_help_message() -> str:
    return "Fresh Bowl commands: DONE logs a clean, SNOOZE delays 4 hours, STOP pauses, START resumes."


def build_stop_message() -> str:
    return "Fresh Bowl paused. You will not get more reminders until you reply START."


def build_start_message() -> str:
    return "Fresh Bowl is back on. We'll text you the next time one of your bowls is due."


def build_completion_message(
    pet_name: str,
    bowl_type: str,
    streak: int,
    next_label: str,
) -> str:
    bowl_label = get_bowl_label(bowl_type).lower()
    streak_line = f" {streak}-day streak!" if streak > 0 else ""
    return f"{pet_name}'s {bowl_label} is logged clean.{streak_line} Next reminder: {next_label}."


def build_snooze_message(pet_name: str, bowl_type: str, next_label: str) -> str:
    bowl_label = get_bowl_label(bowl_type).lower()
    return f"Okay, {pet_name}'s {bowl_label} is snoozed. I'll check back {next_label}."


def build_welcome_message(pet_name: str, first_label: str) -> str:
    return f"Fresh Bowl is live for {pet_name}. Your first reminder is {first_label}. Reply DONE when you've cleaned the bowl."


def choose_message(seed: int, templates: list[str]) -> str:
    return templates[seed % len(templates)]


def choose_health_fact(seed: int) -> str:
    return HEALTH_FACTS[seed % len(HEALTH_FACTS)]


def build_reminder_message(
    *,
    pet_name: str,
    bowl_type: str,
    seed: int,
) -> str:
    template = choose_message(seed, REMINDER_TEMPLATES)
    return template.format(
        pet_name=pet_name,
        bowl_label=get_bowl_label(bowl_type).lower(),
        health_fact=choose_health_fact(seed * 3 + len(bowl_type)),
    )


def build_follow_up_message(
    *,
    pet_name: str,
    bowl_type: str,
    seed: int,
) -> str:
    template = choose_message(seed, FOLLOW_UP_TEMPLATES)
    return template.format(
        pet_name=pet_name,
        bowl_label=get_bowl_label(bowl_type).lower(),
        health_fact=choose_health_fact(seed * 5 + datetime.utcnow().day),
    )


def send_sms_message(to_number: str, body: str) -> str:
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    from_number = os.getenv("TWILIO_PHONE_NUMBER")

    if not all([account_sid, auth_token, from_number]):
        logger.info("Twilio credentials missing. Mocking send to %s: %s", to_number, body)
        return "mock-message-sid"

    client = Client(account_sid, auth_token)
    try:
        message = client.messages.create(body=body, from_=from_number, to=to_number)
        return message.sid
    except TwilioRestException as exc:  # pragma: no cover - third-party failure
        logger.exception("Twilio send failed: %s", exc)
        raise
