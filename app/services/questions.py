"""The business owner's extra booking questions (dashboard -> Settings).

Each question: {"question": "Aap ki email?", "type": "email", "required": False}
Name is always asked by AURA and the phone number comes from WhatsApp, so they are not questions here.
Older businesses stored plain strings; normalize() turns those into required text questions.
"""
import re

from app.services.validation import EMAIL_RE, phone as clean_phone

TYPES = {"text": "Text", "email": "Email", "number": "Number", "phone": "Phone number"}
MAX_ANSWER = 200

# Replies that mean "I'd rather not answer" (only allowed for optional questions)
SKIP_RE = re.compile(r"^\s*(skip|pass|-+|n/?a|na|no|nahi|nhi|nai|nahin|nahi batana|nahi pata|pata nahi|"
                     r"maloom nahi|zaroori nahi|nothing|none)\s*[.!]*\s*$", re.IGNORECASE)

HINTS = {
    "email": "Sahi email likhein, e.g. name@gmail.com",
    "number": "Sirf number likhein, e.g. 25",
    "phone": "Mobile number likhein, e.g. 0300 1234567",
}


def normalize(raw) -> list:
    """Stored value (list of dicts or old list of strings) -> clean list of question dicts."""
    out = []
    for item in raw or []:
        if isinstance(item, str):
            item = {"question": item}
        if not isinstance(item, dict):
            continue
        text = " ".join(str(item.get("question") or "").split())
        if not text:
            continue
        kind = item.get("type") if item.get("type") in TYPES else "text"
        out.append({"question": text, "type": kind, "required": bool(item.get("required", True))})
    return out


def prompt(q: dict, example: bool = True) -> str:
    """What AURA asks the customer (no example when re-asking: the hint already has one)."""
    text = q["question"]
    if example and q["type"] == "email" and "@" not in text:
        text += " (e.g. name@gmail.com)"
    if not q["required"]:
        text += " (optional — 'skip' likhein agar nahi batana)"
    return text


def check_answer(q: dict, reply: str):
    """(answer, problem). answer '' means skipped. problem is a message to send back, else None."""
    reply = " ".join((reply or "").split())[:MAX_ANSWER]
    if not reply or SKIP_RE.match(reply):
        if q["required"]:
            return None, "Ye batana zaroori hai. " + prompt(q)
        return "", None
    if q["type"] == "email":
        found = re.search(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}", reply)
        value = found.group(0).strip(".,;").lower() if found else ""
        if value and re.fullmatch(EMAIL_RE, value):
            return value, None
        return None, f"{HINTS['email']}. " + prompt(q, example=False)
    if q["type"] == "number":
        found = re.search(r"\d+(?:[.,]\d+)?", reply)
        if found:
            return found.group(0).replace(",", "."), None
        return None, f"{HINTS['number']}. " + prompt(q, example=False)
    if q["type"] == "phone":
        found = re.search(r"(\+?92|0)?[\s-]?3\d{2}[\s-]?\d{7}", reply)
        value, error = clean_phone(found.group(0) if found else reply)
        if value and not error:
            return value, None
        return None, f"{HINTS['phone']}. " + prompt(q, example=False)
    return reply, None
