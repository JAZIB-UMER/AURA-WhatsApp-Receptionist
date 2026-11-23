"""Hybrid NLU for AURA.

1. Intent   -> fine-tuned XLM-RoBERTa classifier (fast, free, runs locally)
2. Entities -> fine-tuned XLM-RoBERTa tagger (entity_model.py): service, customer name, and
               *whether* a date / time was mentioned
3. Rules    -> fix the intent model's known confusions; date & time text -> real dates (datetime_parser.py)
4. Gemini   -> backup only: when the intent model is unsure, or a detail was mentioned that the
               local steps couldn't read (e.g. a date the parser doesn't know). If the entity model
               is missing, Gemini extracts service / name as before.

understand_message() returns:
{
  "intent": "book_appointment" | ... | "unknown",
  "entities": {"date": "YYYY-MM-DD"|None, "time": "HH:MM"|None, "service": str|None, "name": str|None},
  "confidence": "high" | "medium" | "low",
  "score": 0.0-1.0,          # model probability for its top intent
  "source": "model" | "model+rule" | "model+gemini" | "gemini" | "none",
}
"""
import json
import os
import re
import time
from datetime import date

from dotenv import load_dotenv

from app.services import config, entity_model, intent_model
from app.services.datetime_parser import parse_date, parse_time, today_pkt

load_dotenv()

INTENTS = ["book_appointment", "cancel_booking", "check_availability",
           "greeting", "price_query", "reschedule"]
# The v2 model also predicts "other" (thanks, names, "ok", off-topic). AURA then answers with
# what it can help with instead of guessing an intent. v1 models simply never predict it.
OUT_OF_SCOPE = "other"

# Below this probability the model's answer is checked with Gemini.
INTENT_THRESHOLD = float(os.getenv("INTENT_THRESHOLD", "0.70"))
# Gemini API key and model name are platform settings (dashboard -> Platform settings).

# Intents that need service / date / time / name details.
NEEDS_DETAILS = {"book_appointment", "check_availability", "reschedule", "price_query"}

# The model sometimes reads "I cancelled it, please reschedule" as cancel.
# In the dataset, messages with these words are reschedule requests (81 of 84;
# the other 3 are mislabelled reschedule requests).
RESCHEDULE_HINTS = (
    "reschedul", "re-schedul", "re schedul", "postpone", "prepone", "shift",
    "change the time", "change the date", "time change", "date change",
    "time badal", "din badal", "aage kar", "agay kar", "aagay kar", "aage kr", "agay kr",
)

# The model is over-confident on short text it never saw (it labelled the name
# "Jazib umer" a greeting at 99%). So "greeting" is only accepted when the message
# really contains a greeting word. Covers 83 of 84 greetings in the dataset
# (the other is the mislabelled "Didn't get it").
GREETING_RE = re.compile(
    r"\b(h+i+|h+e+y+|hy|hel+o+|hlo|aoa|adaa?b|greetings|good (morning|afternoon|evening))\b"
    r"|\b(a?s+ala+m|sla+m|aslam|w[ao]?s+ala+m)\w*|alaik|alaykum",       # \b: "Islamabad" is not a salam
    re.IGNORECASE,
)


def has_greeting_word(text: str) -> bool:
    return bool(GREETING_RE.search(text.replace(".", "")))


# Clear intent words, checked in this order ("booking cancel karni" is a cancel, "slot khali hai?"
# is an availability question). Used when the model says "other" for a short message that obviously
# isn't ("Booking karni hy" -> other @ 0.99, because most "other" training examples are short),
# and as a last resort when the model is unsure and Gemini is down.
KEYWORD_INTENTS = [
    ("reschedule", re.compile("|".join(re.escape(h) for h in RESCHEDULE_HINTS) + r"|\bshft\b", re.I)),
    ("cancel_booking", re.compile(r"\b(cancel\w*|cancle|cansel|cncl|cnsl)\b", re.I)),
    ("price_query", re.compile(r"\b(price|prise|rates?|fees?|fess|charges|charjes|cost|kitne\s+ka|kitne\s+ki|kitna|kitne\s+paise)\b", re.I)),
    ("check_availability", re.compile(r"\b(available|availble|avlbl|avail|khali|khaali|free)\b", re.I)),
    ("book_appointment", re.compile(r"\b(book\w*|bkng|boking|bukking|appointment\w*|apointment|appoinment|appointmnt|appt|slott?s?)\b", re.I)),
]


def keyword_intent(text: str):
    for intent, pattern in KEYWORD_INTENTS:
        if pattern.search(text):
            return intent
    return None


_client = None
_client_key = None


def _gemini(key: "str | None" = None):
    """Gemini client for the saved API key (None if there is none). Rebuilt when the admin changes the key."""
    global _client, _client_key
    key = key or config.get("gemini_api_key")
    if not key:
        return None
    from google import genai
    from google.genai import types
    # 20 s cap: a stuck request once took 52 s; better to fall back than keep the customer waiting
    options = types.HttpOptions(timeout=20_000)
    if key != config.get("gemini_api_key"):
        return genai.Client(api_key=key, http_options=options)      # trying a key before saving it
    if _client is None or key != _client_key:
        _client, _client_key = genai.Client(api_key=key, http_options=options), key
    return _client


def check_gemini(key: "str | None" = None, model: "str | None" = None):
    """(ok, message) — does this key work with this model? One tiny request."""
    client = _gemini(key)
    if client is None:
        return False, "No Gemini API key saved."
    model = model or config.get("gemini_model")
    try:
        client.models.generate_content(model=model, contents="Reply with OK.")
        return True, f"Gemini works with model {model}."
    except Exception as e:
        text = str(e)
        if "API_KEY_INVALID" in text or "PERMISSION_DENIED" in text or "401" in text or "403" in text:
            return False, "Google rejected the API key. Create one at aistudio.google.com → Get API key."
        if "NOT_FOUND" in text or "404" in text:
            return False, f"Model '{model}' not found. Check the name at ai.google.dev/gemini-api/docs/models."
        return False, f"Gemini check failed: {text[:150]}"


PROMPT = """You help a WhatsApp receptionist for a small business in Pakistan (clinic, salon, tutor, repair shop).
Messages may be Roman Urdu, English, or mixed.
Today is __TODAY__ (Pakistan time).

Customer message: "__TEXT__"

Return ONLY a JSON object:
{
  "intent": one of __INTENTS__ or "unknown",
  "confidence": "high", "medium" or "low",
  "date": "YYYY-MM-DD" or null,
  "time": "HH:MM" (24-hour) or null,
  "service": short service name (e.g. "haircut", "dental checkup") or null,
  "name": the customer's own name or null
}

Rules:
- kal = tomorrow, parso = day after tomorrow, baje = o'clock, subah = morning, dopahar = afternoon, sham = evening, raat = night.
- Fill date or time ONLY if the customer clearly said one. Vague words like "sham ko" or "abhi" -> null.
- name is the customer's own name only, never a doctor's or staff member's name.
- Never invent values. Use null when unsure."""


def _ask_gemini(text: str) -> dict:
    client = _gemini()
    if client is None:
        return {}
    today = today_pkt()
    prompt = (PROMPT.replace("__TODAY__", today.strftime("%A, %Y-%m-%d"))
                    .replace("__INTENTS__", json.dumps(INTENTS))
                    .replace("__TEXT__", text.replace('"', "'")))
    from google.genai import types
    for attempt in range(2):   # one retry: Gemini sometimes returns a brief 503 "high demand"
        try:
            response = client.models.generate_content(
                model=config.get("gemini_model"),
                contents=prompt,
                config=types.GenerateContentConfig(temperature=0, response_mime_type="application/json"),
            )
            raw = (response.text or "").strip()
            raw = re.sub(r"^```(?:json)?|```$", "", raw).strip()
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except Exception as e:
            temporary = any(code in str(e) for code in ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED"))
            if attempt == 0 and temporary:
                time.sleep(1.5)
                continue
            print(f"[NLU] Gemini unavailable: {str(e)[:120]}")
            return {}
    return {}


ROMAN_PROMPT = """Rewrite this WhatsApp voice-note transcript in Roman Urdu, the way Pakistanis type on WhatsApp.
Only change the script: do NOT translate, summarise or add anything. Keep English words in English,
numbers as digits, and names as they are. Return only the rewritten text.

Transcript: __TEXT__"""


def to_roman_urdu(text: str):
    """'کل 3 بجے haircut کی appointment' -> 'kal 3 baje haircut ki appointment'. None if Gemini is unavailable."""
    client = _gemini()
    if client is None:
        return None
    from google.genai import types
    for attempt in range(2):
        try:
            response = client.models.generate_content(
                model=config.get("gemini_model"), contents=ROMAN_PROMPT.replace("__TEXT__", text),
                config=types.GenerateContentConfig(temperature=0))
            return (response.text or "").strip().strip('"') or None
        except Exception as e:
            if attempt == 0 and any(c in str(e) for c in ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED")):
                time.sleep(1.5)
                continue
            print(f"[NLU] Gemini unavailable for Roman Urdu: {str(e)[:120]}")
            return None
    return None


def _clean(value):
    if value is None:
        return None
    value = str(value).strip()
    return None if value.lower() in ("", "null", "none", "unknown", "n/a") else value


def _valid_date(value):
    value = _clean(value)
    try:
        d = date.fromisoformat(value) if value else None
    except ValueError:
        return None
    return d.isoformat() if d and d >= today_pkt() else None


def _valid_time(value):
    value = _clean(value)
    if value and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value):
        return value
    return None


def _local_entities(text: str, name_needs_cue: bool = True):
    """Entities without Gemini, plus whether Gemini could still add something.
    Returns (entities, needs_gemini). Without the entity model, needs_gemini is always True."""
    ner = entity_model.extract(text, name_needs_cue)
    found = {"date": parse_date(text), "time": parse_time(text),
             "service": ner["service"] if ner else None, "name": ner["name"] if ner else None}
    if ner is None:
        return found, True
    # The tagger saw a date / time the parser couldn't read ("15 Rabi ul Awal", "dhai baje"). Bare
    # "sham" / "subah" is tagged TIME too but is too vague to book, so a time needs a number.
    unread = ((ner["date"] and not found["date"])
              or (ner["time"] and not found["time"] and re.search(r"\d", ner["time"])))
    return found, bool(unread)


def _merge_entities(local: dict, gemini: dict) -> dict:
    """Local (model + rules) first, predictable and free; Gemini only fills the gaps.
    Service is the exception: if Gemini was asked, its normalised name ('haircut' for 'baal katwane')
    matches the business's list better, and the agent matches the customer's own words first anyway."""
    return {
        "date": local["date"] or _valid_date(gemini.get("date")),
        "time": local["time"] or _valid_time(gemini.get("time")),
        "service": _clean(gemini.get("service")) or local["service"],
        "name": local["name"] or _clean(gemini.get("name")),
    }


def extract_details(text: str, use_gemini: bool = True, name_needs_cue: bool = True,
                    force_gemini: bool = False) -> dict:
    """Entities only (used for follow-up answers like 'Friday 4 baje' or 'mera naam Ali hai').
    use_gemini=True asks Gemini only if the local steps may have missed something;
    force_gemini=True always asks (e.g. the local result is there but didn't match anything).
    name_needs_cue=False when AURA has just asked for the name (a bare 'Ali Khan' is the name)."""
    local, needs_gemini = _local_entities(text, name_needs_cue)
    ask = force_gemini or (use_gemini and needs_gemini)
    return _merge_entities(local, _ask_gemini(text) if ask else {})


def gemini_service(text: str):
    """Gemini's short service name ('baal katwane hain' -> 'haircut'), for matching against
    the business's list when the customer's own words didn't match any service."""
    return _clean(_ask_gemini(text).get("service"))


def understand_message(text: str) -> dict:
    label, score = intent_model.predict(text)
    intent, confidence, source = None, "low", "none"

    if label == "greeting" and not has_greeting_word(text):
        label, score = None, 0.0            # not a real greeting -> let Gemini decide

    if label == OUT_OF_SCOPE:
        if keyword_intent(text):
            label, score = None, 0.0        # "Booking karni hy" is not out of scope -> let Gemini decide
        elif has_greeting_word(text) and len(text.split()) <= 4:
            intent, confidence, source = "greeting", "high", "rule"     # "Hi", "AOA"

    if intent is None and label and score >= INTENT_THRESHOLD:
        intent, confidence, source = label, "high", "model"

    local, details_unread = _local_entities(text)
    gemini = {}
    if intent is None or (intent in NEEDS_DETAILS and details_unread):
        gemini = _ask_gemini(text)

    if intent is None:
        g_intent, g_conf = gemini.get("intent"), gemini.get("confidence")
        if g_intent in INTENTS and label == g_intent:
            intent, confidence, source = g_intent, "medium", "model+gemini"   # both agree
        elif g_intent in INTENTS and g_conf in ("high", "medium"):
            intent, confidence, source = g_intent, g_conf, "gemini"
        elif keyword_intent(text):
            intent, confidence, source = keyword_intent(text), "medium", "rule"   # Gemini down or unsure
        else:
            intent, confidence = "unknown", "low"

    if intent in ("cancel_booking", "book_appointment") and any(h in text.lower() for h in RESCHEDULE_HINTS):
        intent = "reschedule"
        source = source + "+rule"

    result = {
        "intent": intent,
        "entities": _merge_entities(local, gemini),
        "confidence": confidence,
        "score": score,
        "source": source,
    }
    print(f"[NLU] {text!r} -> {intent} ({confidence}, model={label}@{score}, via {source})")
    return result
