"""Input validation for the dashboard (pure Python, no Streamlit, so it is easy to test).

Every function returns either a cleaned value or an error message the owner can act on.
The dashboard also validates in the browser, but browser checks can be bypassed,
so these checks run again on the server before anything is written to the database.
"""
import re

NAME_MAX = 80
GREETING_MAX = 300
QUESTION_MAX = 150
MAX_QUESTIONS = 5
SERVICE_NAME_MAX = 60
PRICE_MAX = 1_000_000
DURATION_MIN, DURATION_MAX = 5, 480
PASSWORD_MIN = 8

# Regexes shared with the browser-side checks (JavaScript-compatible).
USERNAME_RE = r"^[a-z0-9][a-z0-9_.]{2,29}$"
PHONE_RE = r"^(\+?92|0)?\s*3\d{2}[\s-]?\d{7}$"
CALENDAR_RE = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
EMAIL_RE = r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$"
RESERVED_USERNAMES = {"admin", "root", "aura"}


def _missing(value) -> bool:
    """None, NaN, pandas NA (empty cell in the services editor) or blank text."""
    if value is None:
        return True
    try:
        if value != value:                      # NaN
            return True
    except TypeError:                           # pandas NA can't be compared
        return True
    return str(value).strip() == ""


def clean_text(value) -> str:
    """Trim and collapse inner whitespace ('' for empty / missing values)."""
    return "" if _missing(value) else " ".join(str(value).split())


# ------------------------------------------------------------------ single fields

def business_name(value):
    name = clean_text(value)
    if len(name) < 2:
        return None, "Business name must be at least 2 characters."
    if len(name) > NAME_MAX:
        return None, f"Business name can be at most {NAME_MAX} characters."
    return name, None


def username(value, taken: bool = False):
    name = clean_text(value).lower()
    if not re.fullmatch(USERNAME_RE, name):
        return None, "Username: 3–30 characters, lowercase letters, numbers, '.' or '_' (start with a letter or number)."
    if name in RESERVED_USERNAMES:
        return None, f"'{name}' is reserved, pick another username."
    if taken:
        return None, f"Username '{name}' is already used by another business."
    return name, None


def password(value, repeat=None):
    value = value or ""
    if len(value) < PASSWORD_MIN:
        return None, f"Password must be at least {PASSWORD_MIN} characters."
    if not (re.search(r"[A-Za-z]", value) and re.search(r"\d", value)):
        return None, "Password must contain at least one letter and one number."
    if repeat is not None and value != repeat:
        return None, "The two passwords don't match."
    return value, None


def phone(value, required: bool = False):
    """Pakistani mobile number -> '03XXXXXXXXX'. Empty is allowed unless required."""
    raw = clean_text(value)
    if not raw:
        return ("", "WhatsApp number is required.") if required else ("", None)
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("92"):
        digits = "0" + digits[2:]
    elif digits.startswith("3"):
        digits = "0" + digits
    if not re.fullmatch(r"03\d{9}", digits):
        return None, "Enter a Pakistani mobile number, e.g. 0300 1234567 or +92 300 1234567."
    return digits, None


def calendar_id(value, required: bool = False):
    value = clean_text(value)
    if not value:
        return ("", "Google Calendar ID (email) is required.") if required else ("", None)
    if not re.fullmatch(CALENDAR_RE, value):
        return None, "Calendar ID looks like an email, e.g. yourname@gmail.com or …@group.calendar.google.com."
    return value, None


def email(value, required: bool = False):
    value = clean_text(value).lower()
    if not value:
        return ("", "Owner's email is required (AURA sends alerts there).") if required else ("", None)
    if len(value) > 120 or not re.fullmatch(EMAIL_RE, value):
        return None, "Enter a valid email address, e.g. owner@gmail.com."
    return value, None


def greeting(value):
    value = (value or "").strip()
    if len(value) > GREETING_MAX:
        return None, f"Greeting can be at most {GREETING_MAX} characters."
    return value, None


QUESTION_TYPES = {"Text": "text", "Email": "email", "Number": "number", "Phone number": "phone"}


def questions(rows):
    """Rows from the questions table ({"question", "type" label, "required"}) -> (clean list, error)."""
    items = []
    for n, row in enumerate(rows or [], 1):
        text = clean_text(row.get("question"))
        if not text:
            continue                                   # empty row left by the editor
        if len(text) < 5:
            return None, f"Question {n} ('{text}') is too short."
        if len(text) > QUESTION_MAX:
            return None, f"Question {n} can be at most {QUESTION_MAX} characters."
        if re.fullmatch(r"(aap\s*ka\s+|apka\s+|apna\s+|your\s+|customer('?s)?\s+)?(full\s+)?(naam|name)"
                        r"(\s+(kya\s+hai|batayein|bataen|likhein|please))?\s*\??", text, re.IGNORECASE):
            return None, f"Question {n}: AURA always asks the customer's name, no need to add it."
        kind = row.get("type")
        kind = QUESTION_TYPES.get(kind, kind if kind in QUESTION_TYPES.values() else None)
        if not kind:
            return None, f"Question {n}: pick an answer type (Text, Email, Number or Phone number)."
        required = row.get("required")
        items.append({"question": text, "type": kind, "required": True if _missing(required) else bool(required)})
    if len(items) > MAX_QUESTIONS:
        return None, f"At most {MAX_QUESTIONS} extra questions (each one makes booking longer for the customer)."
    texts = [str(q["question"]).lower() for q in items]
    if len(set(texts)) != len(texts):
        return None, "The same question is listed twice."
    return items, None


def hours(open_days, open_time, close_time):
    """open_days: list of 0-6; times: datetime.time. Returns error or None."""
    if not open_days:
        return "Pick at least one working day."
    if open_time is None or close_time is None:
        return "Set both opening and closing time."
    minutes = (close_time.hour * 60 + close_time.minute) - (open_time.hour * 60 + open_time.minute)
    if minutes <= 0:
        return "Closing time must be after opening time."
    if minutes < 60:
        return "Open at least 1 hour a day."
    return None


# ------------------------------------------------------------------ services table

def services(rows):
    """Rows from the services editor -> (clean list, errors). Blank rows are ignored."""
    clean, errors, seen = [], [], {}
    for n, row in enumerate(rows, 1):
        name = clean_text(row.get("name")).rstrip(" :;,.-–—")
        price, duration = row.get("price"), row.get("duration_min")
        if not name and _missing(price) and _missing(duration):
            continue                                    # empty row left by the editor
        label = f"Row {n}" + (f" ({name})" if name else "")
        if len(name) < 2 or not re.search(r"[A-Za-z]", name):
            errors.append(f"{label}: service name must have at least 2 characters and a letter.")
            continue
        if len(name) > SERVICE_NAME_MAX:
            errors.append(f"{label}: name can be at most {SERVICE_NAME_MAX} characters.")
            continue
        if name.lower() in seen:
            errors.append(f"{label}: '{name}' is listed twice (also row {seen[name.lower()]}).")
            continue
        seen[name.lower()] = n
        try:
            if _missing(price) or float(price) != int(float(price)):
                raise ValueError
            price = int(float(price))
        except (TypeError, ValueError, OverflowError):
            errors.append(f"{label}: enter a whole-rupee price (0 if free).")
            continue
        if not 0 <= price <= PRICE_MAX:
            errors.append(f"{label}: price must be between 0 and {PRICE_MAX:,}.")
            continue
        try:
            if _missing(duration) or float(duration) != int(float(duration)):
                raise ValueError
            duration = int(float(duration))
        except (TypeError, ValueError, OverflowError):
            errors.append(f"{label}: enter how many minutes it takes.")
            continue
        if not DURATION_MIN <= duration <= DURATION_MAX:
            errors.append(f"{label}: duration must be between {DURATION_MIN} and {DURATION_MAX} minutes.")
            continue
        clean.append({"name": name[0].upper() + name[1:], "price": price, "duration_min": duration})
    return clean, errors


# ------------------------------------------------------------------ platform settings (admin)

API_KEY_RE = r"^[A-Za-z0-9._\-]{20,300}$"
MODEL_RE = r"^[a-z0-9][a-z0-9.\-]{2,79}$"
HOST_RE = r"^[A-Za-z0-9]([A-Za-z0-9\-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)+$"


def api_key(value, label: str):
    """A pasted API key / token. Empty means 'keep the saved one' and is handled by the caller."""
    key = "".join(str(value or "").split())          # pasted keys often carry spaces or line breaks
    if not re.fullmatch(API_KEY_RE, key):
        return None, f"{label}: that doesn't look like a valid key (letters, numbers, '-', '_', '.'; 20+ characters)."
    return key, None


def model_name(value, label: str):
    name = clean_text(value).lower()
    if not re.fullmatch(MODEL_RE, name):
        return None, f"{label}: use the model's exact name, e.g. gemini-3.5-flash-lite."
    return name, None


def smtp_server(host, port):
    """(host, port) for the email server."""
    host = clean_text(host).lower()
    if not re.fullmatch(HOST_RE, host):
        return None, "Email server: enter a host name like smtp.gmail.com."
    try:
        number = int(str(port).strip())
    except (TypeError, ValueError):
        number = 0
    if not 1 <= number <= 65535:
        return None, "Email port must be a number between 1 and 65535 (Gmail: 587)."
    return (host, str(number)), None


def app_password(value):
    """An email App Password (Gmail shows it as 4 groups of 4 letters; the spaces are removed)."""
    text = "".join(str(value or "").split())
    if not 8 <= len(text) <= 100:
        return None, "That doesn't look like an App Password (Gmail's are 16 letters)."
    return text, None


def sender_name(value):
    """Optional 'From' display, e.g. 'AURA Alerts <alerts@gmail.com>'. Header injection is refused."""
    text = clean_text(value)
    if any(ch in str(value or "") for ch in "\r\n"):
        return None, "Sender: one line only."
    if len(text) > 120:
        return None, "Sender can be at most 120 characters."
    return text, None


def service_account(raw: bytes):
    """Uploaded Google service-account key file -> its JSON text (compact)."""
    import json
    if not raw or len(raw) > 20_000:
        return None, "Upload the service account's key file (.json, a few KB)."
    try:
        info = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None, "That file isn't valid JSON. Download the key again: Google Cloud → IAM → Service accounts → Keys."
    if not isinstance(info, dict) or info.get("type") != "service_account":
        return None, "That JSON isn't a service-account key (its \"type\" must be \"service_account\")."
    missing = [k for k in ("client_email", "private_key", "token_uri") if not info.get(k)]
    if missing:
        return None, f"The key file is incomplete (missing {', '.join(missing)}). Download a new key."
    return json.dumps(info, separators=(",", ":")), None
