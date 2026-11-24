"""Conversation state per (business, customer), kept in Redis for 2 hours."""
import json
import os

import redis
from dotenv import load_dotenv

load_dotenv()

# .env / Docker: REDIS_HOST=redis inside docker compose, localhost when running directly
r = redis.Redis(host=os.getenv("REDIS_HOST", "localhost"), port=int(os.getenv("REDIS_PORT", "6379")),
                db=0, decode_responses=True)

SESSION_TTL = 7200   # conversation is forgotten after 2 hours of silence


def _key(business_id: int, phone: str) -> str:
    return f"session:{business_id}:{phone}"


def new_state(phone: str) -> dict:
    return {
        "phone": phone,
        "intent": None,
        "collected": {"service": None, "date": None, "time": None, "name": None,
                      "email": None},  # email: None = not asked yet, "" = customer skipped it
        "answers": [],            # answers to the business's custom questions, in order
        "reschedule_of": None,    # booking id being moved, when rescheduling
        "pending": None,          # a question AURA is waiting on: pick a booking / confirm / what to change
        "last_cancelled": None,   # text of what was just cancelled ("konsi cancel ki?")
        "turn_count": 0,
        "failed_turns": 0,
        "confidence": None,
        "status": "active",
    }


def get_state(business_id: int, phone: str) -> dict:
    data = r.get(_key(business_id, phone))
    if data:
        state = json.loads(data)
        for k, v in new_state(phone).items():   # sessions saved by older versions
            state.setdefault(k, v)
        return state
    return new_state(phone)


def save_state(business_id: int, phone: str, state: dict):
    r.setex(_key(business_id, phone), SESSION_TTL, json.dumps(state, ensure_ascii=False))


def clear_state(business_id: int, phone: str):
    r.delete(_key(business_id, phone))


def get_missing_fields(state: dict, business: "dict | None" = None) -> list:
    """What AURA still needs to ask, in order: service, date, time, name, the owner's questions, then
    the customer's email (optional; only when the business sends booking emails)."""
    collected = state["collected"]
    missing = [f for f in ("service", "date", "time", "name") if not collected.get(f)]
    questions = (business or {}).get("custom_questions") or []
    answered = len(state.get("answers") or [])
    missing += [f"q{i}" for i in range(answered, len(questions))]
    if (business or {}).get("asks_email") and collected.get("email") is None:
        missing.append("email")
    return missing


def update_entities(state: dict, entities: dict) -> dict:
    for key in ["service", "date", "time", "name"]:
        if entities.get(key) and entities[key] not in [None, "null", "None"]:
            state["collected"][key] = entities[key]
    return state
