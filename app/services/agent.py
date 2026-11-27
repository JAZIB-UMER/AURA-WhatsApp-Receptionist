"""AURA conversation agent.

Every message is handled for ONE business (its services, prices, hours, custom
questions, calendar and WhatsApp number come from the dashboard).

Design (the pattern used by production booking assistants):
  * Slot filling   — ask only for what is still missing; validate date/time as soon as known.
  * Confirm first  — show a summary and wait for "haan" before booking, cancelling or moving.
  * Disambiguate   — with several bookings, list them and let the customer pick
                     ("2", "Saturday wali", "haircut wali", a name).
  * Context        — AURA remembers what it is waiting for (state["pending"]).
  * Fallback       — after 3 messages it can't understand, the owner is alerted.
"""
import json
import re
from datetime import date, datetime, timedelta

from app.services import customer_mail, db, intent_model
from app.services.business import (
    DEFAULT_DURATION, duration_for, format_price, free_slots, free_windows, hours_text, is_open_day, match_service,
    nearest, overlaps, price_list_message, services_text, similar_services, suggest, suggestion_text,
    timings_line, within_hours,
)
from app.services.calendar import book_appointment, cancel_appointment, get_busy
from app.services.datetime_parser import PKT, WEEKDAYS, describe, parse_date, parse_time
from app.services.escalation import REPEAT_WINDOW_HOURS, escalate, wants_human
from app.services.nlu import (
    chat_reply, confirmation_meaning, extract_details, gemini_service, has_greeting_word, understand_message,
)
from app.services.questions import check_answer, prompt as question_prompt
from app.services.reminder import (
    booking_line, cancel_booking_by_id, cancel_reminders, handle_confirmation, schedule_reminders,
)
from app.services.state import (
    SESSION_TTL, clear_state, get_missing_fields, get_state, new_state, r as redis_client, save_state,
)

FLOW = ("book_appointment", "reschedule")
MAX_FAILED_TURNS = 3

# Phrases that mean "stop this booking" while AURA is asking questions.
STOP_PHRASES = ("cancel", "rehne do", "rehnay do", "rehny do", "nahi chahiye", "nai chahiye",
                "nhi chahiye", "chodo", "chhoro", "chor do", "stop", "kuch nahi")

YES_WORDS = {"haan", "han", "haa", "ha", "hn", "ji", "jee", "g", "yes", "yeah", "yep", "ok", "okay", "okk", "theek",
             "thik", "confirm", "confirmed", "bilkul", "sahi", "done", "kardo", "krdo", "karden", "krden", "kardein",
             "krdein", "kardain", "krdain", "kardijiye", "kardijye", "zaroor", "perfect", "sure", "chalega",
             "acha", "achha", "accha", "proceed"}
# "kar dein", "book kar do", "theek hai", "go ahead" (two-word ways of saying yes)
YES_RE = re.compile(r"\b(kar|kr|ker)\s*(do|dein|den|dain|dijiye|dijye|de|lo|lein|len)\b|\bbook\s*(kar|kr)|"
                    r"\bgo\s*ahead\b|\b(theek|thik)\s*(hai|he|h)\b|\bsounds\s*good\b", re.IGNORECASE)
NO_WORDS = {"nahi", "nai", "nhi", "na", "no", "nope", "mat", "galat", "ghalat"}


# ----------------------------------------------------------------- small helpers

def _words(message: str) -> set:
    return set(re.findall(r"[a-z0-9]+", message.lower()))


# Words that may sit around a "yes" without changing it ("haan g, book kar dein please").
YES_FILLER = {"hai", "he", "h", "hain", "do", "dein", "den", "dain", "de", "dijiye", "dijye", "kar", "kr", "ker",
              "karo", "kro", "lo", "lein", "len", "please", "plz", "pls", "bhai", "sir", "book", "booking", "ye",
              "yeh", "isay", "ise", "bas", "phir", "to", "toh", "it", "go", "ahead", "sounds", "good", "jee", "g",
              "appointment", "kardo", "krdo", "ji",
              "thanks", "thank", "you", "thx", "ty", "shukriya", "jazakallah"}   # "ok thanks" still confirms


def _is_yes(message: str, also: frozenset = frozenset()) -> bool:
    """A clear yes: a yes-word or phrase, and NOTHING else of substance. 'Kardein', 'theek hai kar dein' yes;
    'time thora late kar do' or 'service change kar do' no (they ask for a change). also: extra words allowed."""
    w = _words(message)
    if not w or w & NO_WORDS:
        return False
    return bool(w & YES_WORDS or YES_RE.search(message)) and w <= YES_WORDS | YES_FILLER | also


def _is_no(message: str) -> bool:
    return bool(_words(message) & NO_WORDS)


def _wants_to_stop(message: str) -> bool:
    text = message.lower()
    return any(p in text for p in STOP_PHRASES)


def _availability(business: dict, duration: int) -> str:
    """'🗓 *Aaj (Fri 27 Nov):* 1:00 PM – 5:00 PM' and the same for tomorrow, from the calendar.
    '' if neither day is open, or the calendar can't be read (then the plain question is asked)."""
    lines = []
    today = datetime.now(PKT).date()
    for offset, label in ((0, "Aaj"), (1, "Kal")):
        day = today + timedelta(days=offset)
        heading = f"🗓 *{label} ({day:%a %d %b}):*"
        if not is_open_day(business, day.isoformat()):
            lines.append(f"{heading} band")
            continue
        busy = get_busy(day.isoformat(), business.get("calendar_id"))
        if busy is None:
            return ""                              # calendar unreadable: don't show guesses
        windows = free_windows(business, day.isoformat(), duration, busy)
        lines.append(f"{heading} {', '.join(windows)}" if windows else f"{heading} koi waqt free nahi")
    if all(line.endswith((" band", "free nahi")) for line in lines):
        return ""
    return "\n".join(lines)


def _date_question(business: dict, services: list, state: "dict | None") -> str:
    duration = _duration(state, services) if state else DEFAULT_DURATION
    free = _availability(business, duration)
    if not free:
        return "Kis din aana chahenge?"
    return f"*Kab aana chahenge?*\n{free}\n\nInn mein se waqt batayein — ya kisi aur din aana chahenge?"


def _question(field: str, business: dict, services: list, state: "dict | None" = None) -> str:
    if field == "service":
        if services:
            return f"*Kaunsi service chahiye?*\n{services_text(services)}"
        return "Kaunsi service chahiye?"
    if field == "date":
        return _date_question(business, services, state)
    if field == "time":
        return "Kis waqt? (e.g. 3 baje, sham 6 baje, 10am)"
    if field == "name":
        return "Apna naam batayein please?"
    if field == "email":
        return question_prompt(customer_mail.EMAIL_QUESTION)
    return question_prompt(business["custom_questions"][int(field[1:])])   # owner's question "q0", "q1", ...


SALAM_RE = re.compile(r"sala+m|slam|aslam|assalam|alaik|alaykum|\baoa\b|a\.o\.a", re.IGNORECASE)
OPENING_SALAM_RE = re.compile(r"^\s*(assalam\w*|asalam\w*|salam\w*)(\s+(o|u|wa)?\s*(alaikum|alaykum|alekum))?[\s!.,]*",
                              re.IGNORECASE)


NO_SERVICES = ("Maaf kijiye, abhi {name} ki services online available nahi hain, is liye main booking nahi kar "
               "sakta. 🙏\nOwner jald services add kar denge — tab tak isi number par owner se rabta kar sakte hain.")

# "services kya hain?", "kya kya karte hain", "rate list" (a question about the list, not one service)
SERVICES_QUESTION = re.compile(r"\b(services?|servis|sarvis|rate\s*list|price\s*list|rates|prices)\b|"
                               r"kya\s*kya\s*(karte|hota|milta|hai|hain)", re.IGNORECASE)

MENU_OPTIONS = ("• Appointment booking\n"
                "• Services aur rates\n"
                "• Booking reschedule ya cancel\n"
                "• Timings\n\n"
                "_Kabhi bhi *menu* likh kar dobara shuru kar sakte hain._")
HELP_MENU = f"Main aap ki kya madad kar sakta hoon?\n{MENU_OPTIONS}"


def _with_menu(greeting: str) -> str:
    """Greeting, then the options. If the owner's greeting already asks how to help, it isn't asked twice."""
    if re.search(r"madad|khidmat|help", greeting, re.IGNORECASE):
        return f"{greeting}\n\n{MENU_OPTIONS}"
    return f"{greeting}\n\n{HELP_MENU}"


def _greeting(business: dict, message: str = "") -> str:
    """Business greeting; answers 'Assalam o alaikum' with 'Walaikum Assalam!'."""
    text = (business.get("greeting") or "").strip() or f"Assalamualaikum! {business['name']} mein khush aamdeed."
    if SALAM_RE.search(message):
        rest = OPENING_SALAM_RE.sub("", text, count=1)
        text = "Walaikum Assalam! " + rest if rest != text else "Walaikum Assalam! " + text
    return text


def _is_plain_greeting(message: str) -> bool:
    """A real greeting ("Salam", "AOA") that carries no booking details.
    A name like "Jazib Umer" is never a greeting, whatever the model says."""
    if not has_greeting_word(message):
        return False
    label, score = intent_model.predict(message)
    if label != "greeting" or score < 0.9:
        return False
    details = extract_details(message, use_gemini=False)
    return not (details["date"] or details["time"])


def _has_datetime(message: str) -> bool:
    details = extract_details(message, use_gemini=False)
    return bool(details["date"] or details["time"])


TIMINGS_RE = re.compile(
    r"\btimings?\b|\bhours\b|\bopen(ing)?\b|\bclos(e|ing)\b|kab\s+(tak\s+)?(khul|band)|\bkhul(t|a|e)|"
    r"band\s+(kab|hot)|office\s+time|time\s*table|schedule\s+kya",
    re.IGNORECASE,
)
ACTION_RE = re.compile(r"book|appointment|badal|change|reschedul|shift|cancel|postpone", re.IGNORECASE)
THANKS_RE = re.compile(r"shukri|shukria|thank|thnx|thx|\bty\b|jazak|meherbani|allah\s*hafiz|khuda\s*hafiz|"
                       r"\bbye\b|take care|\bfi\s*amanillah", re.IGNORECASE)
ACK_WORDS = {"ok", "okay", "okk", "k", "theek", "thik", "hai", "acha", "achha", "accha", "acchha", "good", "great",
             "nice", "done", "g", "ji", "hmm", "hm", "alright", "sahi", "perfect", "cool"}
LIST_WORDS = re.compile(r"\b(kab|kis\s+din|kya\s+time|konsi|kaunsi|kon\s*si|check|details?|bata|batao|btao|bta|"
                        r"dikha|dikhao|status|show|list|when|what|kitni|kitne)\b", re.IGNORECASE)
NOT_LISTING = re.compile(r"cancel|shift|reschedul|change|badal|karni|karna|krni|krna|karwa|chahiye|chahye", re.IGNORECASE)


def _asks_timings(message: str) -> bool:
    """A short question about opening hours: 'Timings?', 'kab tak khule ho?', 'opening hours'.
    Longer messages, or ones that also ask to book/change something, go to the model instead
    (e.g. 'Laptop repair ki timing badal dein' is a reschedule)."""
    if len(message.split()) > 6 or not TIMINGS_RE.search(message) or ACTION_RE.search(message):
        return False
    return not _has_datetime(message)


def _is_thanks(message: str) -> bool:
    return len(message.split()) <= 6 and bool(THANKS_RE.search(message)) and not ACTION_RE.search(message)


def _is_goodbye(message: str) -> bool:
    """'bye' / 'thank you' / 'Allah Hafiz' with no 'yes' in it ('ok thanks' at the confirm step still confirms)."""
    return _is_thanks(message) and not _is_yes(message)


GOODBYE_MID_BOOKING = "Koi baat nahi! Booking abhi nahi hui — jab chahein dobara message kar dein. Allah Hafiz!"


def _is_ack(message: str) -> bool:
    w = _words(message)
    return bool(w) and w <= ACK_WORDS


NOT_NAME_WORDS = YES_WORDS | NO_WORDS | ACK_WORDS | {
    "hi", "hello", "hey", "salam", "salaam", "aoa", "assalam", "walaikum", "thanks", "thank", "shukriya",
    "please", "plz", "pls", "kya", "what", "why", "kyun", "hmm", "test", "check", "karein", "kro", "karo",
    "ab", "abhi", "batao", "bataen", "sorry", "bye", "naam", "name", "mera", "my", "is", "hai"}


def _looks_like_name(text: str) -> bool:
    """'Ali Khan' yes; 'Ok', 'Hi', 'haan ji', 'Ab check karein', '12345' no (they'd be saved as the name)."""
    words = re.findall(r"[^\W\d_]+", text.lower())
    if not words or len(words) > 4 or re.search(r"\d", text):
        return False
    return not all(w in NOT_NAME_WORDS for w in words)   # (no greeting regex: "Aslam" is a real name)


SMALL_TALK_RE = re.compile(r"\b(kya|kia|kiya)\s*(haal|hal|hall)\b|\b(kaise|kese|kesay|kesy|kaisay)\s*(ho|hain|hn|hen)\b|"
                           r"\bhow\s*(are|r)\s*(you|u)\b|\bkhair(iyat|iat)\b|\bsab\s*(theek|thik|khair)\b|"
                           r"\bwhat'?s\s*up\b|\bkya\s*scene\b", re.IGNORECASE)

QUESTION_RE = re.compile(r"\?|\b(kya|kia|kahan|kahaan|kaise|kesay|kitn[aie]|kab|kaun|kon|what|where|when|how|why|"
                         r"do\s+you|can\s+i|is\s+there|are\s+there)\b", re.IGNORECASE)


def _answer_aside(state, business, services, phone, message, then: str):
    """A question in the middle of a booking ("parking hai?"): answer it naturally (Gemini, business facts
    only), then repeat what AURA is waiting for. None if Gemini can't answer it."""
    reply = chat_reply(message, business["name"], business.get("business_type") or "",
                       "; ".join(line.lstrip("• ") for line in services_text(services).splitlines()),
                       hours_text(business))
    if not reply:
        return None
    save_state(business["id"], phone, state)
    return f"{reply}\n\n{then}"


def _plain_name(text: str) -> bool:
    """A reply that is only a name: 1-4 words, no digits, none of them a common chat word ('Jazib Umer' yes;
    'mera naam Ali hai', 'ok', 'kal 3 baje' no)."""
    words = re.findall(r"[^\W\d_]+", text.lower())
    return (0 < len(words) <= 4 and not re.search(r"\d", text) and not QUESTION_RE.search(text)
            and not set(words) & NOT_NAME_WORDS and len(" ".join(words)) >= 2)


def _asks_my_bookings(message: str) -> bool:
    """'Meri booking kab hai?', 'my appointments', 'konsi booking hai meri?'"""
    t = message.lower()
    mentions = re.search(r"\b(meri|mere|my|apni)\b.*\b(booking|bookings|appointment|appointments)\b|"
                         r"\b(booking|appointment)s?\b.*\b(meri|mere|my)\b", t)
    return bool(mentions and LIST_WORDS.search(t) and not NOT_LISTING.search(t))


def _is_iso_date(value) -> bool:
    return bool(value) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(value)) is not None


def _is_hhmm(value) -> bool:
    return bool(value) and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(value)) is not None


def _fail(state: dict, business: dict, phone: str, message: str, reply: str) -> str:
    """Count a message AURA couldn't use; after 3 in a row, hand over to the owner."""
    state["failed_turns"] += 1
    state["last_failed_turn"] = state["turn_count"]
    if state["failed_turns"] >= MAX_FAILED_TURNS:
        return _escalate(business, phone, message)
    save_state(business["id"], phone, state)
    return reply


def _escalate(business: dict, phone: str, message: str) -> str:
    clear_state(business["id"], phone)
    escalate(business, phone, "not_understood", f"{MAX_FAILED_TURNS} messages in a row. Last: {message}")
    return "Mujhe samajh nahi aa raha. Main owner ko notify kar raha hoon, woh aap se jald rabta karenge."


def _hand_to_owner(state: dict, business: dict, phone: str) -> str:
    """Customer asked for a person: alert the owner once; any booking in progress is kept."""
    save_state(business["id"], phone, state)
    if db.recent_open_escalation(business["id"], phone, "human_request", hours=REPEAT_WINDOW_HOURS):
        return "Owner ko pehle hi bata diya gaya hai — woh jald aap se isi number par rabta karenge. 🙏"
    escalate(business, phone, "human_request")
    return (f"Zaroor! Main ne {business['name']} ke owner ko bata diya hai, woh jald aap se isi number par "
            f"rabta karenge. Tab tak booking, rates ya timings ke liye main yahan hoon.")


CALENDAR_DOWN = object()      # _slot_problem's answer when the business's calendar can't be read


def _calendar_down(state: dict, business: dict, phone: str) -> str:
    """The calendar can't be read or written (not shared with AURA, Google down, ...).
    Never trap the customer in a retry loop: pass the request to the owner and end the session."""
    c = state["collected"]
    when = describe(c["date"], c["time"]) if c.get("date") and c.get("time") else (c.get("date") or "day not chosen")
    parts = [x for x in (c.get("service"), when, c.get("name") and f"Naam: {c['name']}") if x]
    clear_state(business["id"], phone)
    if not db.recent_open_escalation(business["id"], phone, "calendar_unavailable", hours=REPEAT_WINDOW_HOURS):
        escalate(business, phone, "calendar_unavailable",
                 f"Requested: {', '.join(parts)}. Book it manually, and check in the dashboard (Settings → "
                 "Check calendar) that the calendar is shared with AURA's service account.")
    request = "\n".join(f"• {p}" for p in parts)
    return (f"Maaf kijiye, abhi hamara booking calendar check nahi ho pa raha.\n\n"
            f"*Aap ki request {business['name']} ke owner ko bhej di hai:*\n{request}\n\n"
            f"Woh jald isi number par confirm karenge. 🙏")


def _duration(state: dict, services: list) -> int:
    service = state["collected"].get("service")
    return duration_for(service, services) if service else DEFAULT_DURATION


def _numbered(bookings: list) -> str:
    return "\n".join(f"{i}) {booking_line(b)}" for i, b in enumerate(bookings, 1))


# ----------------------------------------------------------------- choosing one of several bookings

ORDINALS = {"pehli": 0, "pehla": 0, "pehle": 0, "first": 0, "1st": 0, "doosri": 1, "dusri": 1, "dosri": 1,
            "doosra": 1, "second": 1, "2nd": 1, "teesri": 2, "tesri": 2, "third": 2, "3rd": 2,
            "aakhri": -1, "akhri": -1, "last": -1}
ALL_WORDS = {"dono", "donon", "sab", "sari", "saari", "sare", "saare", "all", "both"}


def _pick(message: str, bookings: list, use_dates: bool = True):
    """Which booking does the customer mean? Returns a booking, "all", or None."""
    t = message.lower().strip()
    words = re.findall(r"[a-z0-9]+", t)
    if not bookings:
        return None
    if set(words) & ALL_WORDS:
        return "all"
    m = re.fullmatch(r"#?\s*([1-9])\s*(wali|wala|number|no)?\.?", t)
    if m and int(m.group(1)) <= len(bookings):
        return bookings[int(m.group(1)) - 1]
    for w in words:
        if w in ORDINALS and abs(ORDINALS[w]) <= len(bookings):
            return bookings[ORDINALS[w]]

    candidates = list(bookings)
    matched_something = False

    def narrow(test):
        nonlocal candidates, matched_something
        kept = [b for b in candidates if test(b)]
        if kept:
            candidates = kept
            matched_something = True

    if use_dates:
        weekdays = {WEEKDAYS[w] for w in words if w in WEEKDAYS}
        if weekdays:
            narrow(lambda b: date.fromisoformat(b["date"]).weekday() in weekdays)
        else:
            d = parse_date(message)
            if d:
                narrow(lambda b: b["date"] == d)
        tm = parse_time(message)
        if tm:
            narrow(lambda b: b["time"] == tm)
    narrow(lambda b: match_service(message, [{"name": b["service"]}]) is not None)
    narrow(lambda b: bool(b.get("customer_name")) and b["customer_name"].lower().split()[0] in words)
    # Only an answer if the message actually pointed at exactly one booking.
    return candidates[0] if matched_something and len(candidates) == 1 else None


# ----------------------------------------------------------------- slot checks

def _slot_problem(state: dict, business: dict, services: list):
    """None if the chosen date + time can be booked, otherwise a message that explains
    why not and offers the nearest free slots. Clears the time so AURA asks again."""
    c = state["collected"]
    duration = _duration(state, services)
    when = describe(c["date"], c["time"])
    start = datetime.strptime(f"{c['date']} {c['time']}", "%Y-%m-%d %H:%M").replace(tzinfo=PKT)
    calendar_id = business.get("calendar_id")
    busy = get_busy(c["date"], calendar_id)

    if busy is None:
        return CALENDAR_DOWN
    if state.get("reschedule_of"):
        # The customer's own current booking doesn't block its new time.
        old = db.get_booking(state["reschedule_of"])
        if old and old["date"] == c["date"]:
            o_start = datetime.strptime(f"{old['date']} {old['time']}", "%Y-%m-%d %H:%M").replace(tzinfo=PKT)
            busy = [(s, e) for s, e in busy if not (s == o_start and e == o_start + timedelta(minutes=old["duration_min"]))]
    if start <= datetime.now(PKT):
        problem = f"{when} ka waqt guzar chuka hai."
    elif not within_hours(business, c["date"], c["time"], duration):
        problem = f"{when} hamare timings se bahar hai.\n{timings_line(business)}"
    elif overlaps(start, start + timedelta(minutes=duration), busy):
        problem = f"Sorry, {when} available nahi hai."
    else:
        return None

    wanted_time = c["time"]
    c["time"] = None
    day, slots = suggest(business, c["date"], wanted_time, duration, lambda d: get_busy(d, calendar_id))
    if slots:
        c["date"] = day                       # suggestions may be on the next open day
        return f"{problem}\n\n{suggestion_text(day, slots)}\n\nKaunsa time chahiye?"
    return f"{problem}\n\nKoi aur din ya waqt batayein?"


# ----------------------------------------------------------------- collecting details

def _apply_entities(state: dict, entities: dict, message: str, services: list):
    """Put details from the first message (date, time, service, name) into the state."""
    c = state["collected"]
    for key in ("date", "time", "name"):
        if entities.get(key) and not c.get(key):
            c[key] = entities[key]
    if not c.get("service"):
        svc = match_service(message, services) or match_service(entities.get("service") or "", services)
        if not svc and services and entities.get("service"):
            # a service was mentioned in words the list doesn't use ("baal katwane") -> Gemini's term
            svc = match_service(gemini_service(message) or "", services)
        if svc:
            c["service"] = svc["name"]
        elif not services and entities.get("service"):
            c["service"] = entities["service"]      # business has no service list yet


def collect_field(state: dict, message: str, business: dict, services: list):
    """Fill missing details from a follow-up reply.
    Returns (state, got_it): whether the reply gave AURA anything useful."""
    missing = get_missing_fields(state, business)
    if not missing:
        return state, True
    asked = missing[0]
    c = state["collected"]
    before = dict(c)

    if asked == "email":                           # optional: an email address, or 'skip'
        answer, problem = check_answer(customer_mail.EMAIL_QUESTION, message)
        if problem:
            state["question_problem"] = problem
            return state, False
        c["email"] = answer                        # '' = skipped
        return state, True

    if asked.startswith("q"):                      # owner's question: check the answer against its type
        answer, problem = check_answer(business["custom_questions"][int(asked[1:])], message)
        if problem:
            state["question_problem"] = problem    # e.g. "Sahi email likhein, e.g. name@gmail.com. ..."
            return state, False
        state["answers"].append(answer)            # '' = optional question skipped
        return state, True

    text = message.strip()
    name_asked = asked == "name"
    details = extract_details(text, use_gemini=False, name_needs_cue=not name_asked)   # local, free
    if asked == "time" and not details["time"] and re.fullmatch(r"\d{1,2}([:.]\d{2})?", text):
        details["time"] = parse_time(text.replace(".", ":") + " baje")   # reply "6" to a slot list
    svc = match_service(text, services) or match_service(details.get("service") or "", services)
    words = len(text.split())
    plain_name = name_asked and _plain_name(text) and not (details["date"] or details["time"] or svc)
    needs_gemini = ((asked in ("date", "time") and not details[asked])
                    or (name_asked and words > 2 and not details["name"] and not plain_name)
                    or (asked == "service" and not svc and (words > 2 or details["service"])))
    if needs_gemini:
        gem = extract_details(text, name_needs_cue=not name_asked, force_gemini=True)
        details = {k: details.get(k) or gem.get(k) for k in ("date", "time", "service", "name")}
        svc = svc or match_service(details.get("service") or "", services)

    if not c["service"]:
        if svc:
            c["service"] = svc["name"]
        elif not services and asked == "service" and not (details["date"] or details["time"]):
            c["service"] = text[:60]

    if details["date"] and (asked in ("date", "time") or not c["date"]):
        c["date"] = details["date"]
    if details["time"] and (asked == "time" or not c["time"]):
        c["time"] = details["time"]

    if asked == "name" and not c["name"]:
        if plain_name:                             # "jazib umer" -> "Jazib Umer" (the model may tag one word)
            c["name"] = " ".join(text.split())[:60].title()
        elif details.get("name"):
            c["name"] = details["name"]
        elif not (details["date"] or details["time"]) and _looks_like_name(text) and not QUESTION_RE.search(text):
            c["name"] = text[:60].title()
        elif not (details["date"] or details["time"] or svc or QUESTION_RE.search(text)):
            state["question_problem"] = "Booking kis naam par karun? Apna naam likh dein, jaise: Ali Khan"

    # Useful even if it answered a different question (e.g. "11 baje" after a slot list)
    return state, bool(c.get(asked)) or c != before


def _apply_changes(state: dict, message: str, services: list) -> bool:
    """'Nahi, 5 baje kar do' / 'Friday rakh lo' / 'haircut kar do' / a new email while reviewing the summary."""
    c = state["collected"]
    if c.get("email") is not None:                 # only when AURA asked for an email in this booking
        found = re.search(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}", message)
        if found and found.group(0).strip(".,;").lower() != c["email"]:
            c["email"] = found.group(0).strip(".,;").lower()
            return True
    details = extract_details(message, use_gemini=False)
    if re.fullmatch(r"\s*\d{1,2}([:.]\d{2})?\s*", message):
        details["time"] = parse_time(message.strip().replace(".", ":") + " baje")
    svc = match_service(message, services)
    changed = False
    if details["date"] and details["date"] != c["date"]:
        c["date"], changed = details["date"], True
    if details["time"] and details["time"] != c["time"]:
        c["time"], changed = details["time"], True
    if svc and svc["name"] != c["service"]:
        c["service"], changed = svc["name"], True
    m = re.search(r"\b(?:naam|name)\s*(?:hai|is|:)?\s*([a-z][a-z ]{1,40}?)\s*(?:\b(?:hai|he|hy|h|hain|likh\s*do|kar\s*do)\b)?[.!]*$",
                  message.strip(), re.IGNORECASE)
    if m and m.group(1).strip():
        c["name"], changed = m.group(1).strip().title(), True
    elif details.get("name") and details["name"] != c["name"]:
        c["name"], changed = details["name"], True       # "main Usman hoon", "for Zainab"
    if changed:
        state["slot_checked"] = None
    return changed


CHANGE_WORDS = re.compile(r"\b(change|chnge|badal\w*|badl\w*|tabdeel|tabdil|dusr[ia]|doosr[ia]|another|different|"
                          r"alag|koi\s*aur)\b", re.IGNORECASE)
FIELD_WORDS = {
    "service": re.compile(r"\b(services?|servis|sarvis|treatment)\b", re.IGNORECASE),
    "time": re.compile(r"\b(time|timing|waqt|wakt|tym|samay)\b", re.IGNORECASE),
    "date": re.compile(r"\b(date|din|day|tareekh|tarikh)\b", re.IGNORECASE),
    "name": re.compile(r"\b(naam|name)\b", re.IGNORECASE),
    "email": re.compile(r"\be-?mail\b", re.IGNORECASE),
}


def _field_to_change(message: str, need_change_word: bool = True):
    """'service change karni hai' -> 'service'; 'time badalna hai' -> 'time'. After "kya change karna hai?",
    the field word alone ('service') is enough. None if no single part is named."""
    if need_change_word and not CHANGE_WORDS.search(message):
        return None
    fields = [f for f, pattern in FIELD_WORDS.items() if pattern.search(message)]
    return fields[0] if len(fields) == 1 else None


def _clock(hhmm: str) -> str:
    return datetime.strptime(hhmm, "%H:%M").strftime("%I:%M %p").lstrip("0")


def _ask_new_value(state, business, services, phone, field: str) -> str:
    """Clear one detail and ask for it again, saying what it is now. The summary follows once it's given."""
    c = state["collected"]
    state["pending"] = None
    state["slot_checked"] = None
    if field == "service" and services:
        question = f"Abhi service *{c['service']}* hai. Kaunsi service chahiye?\n{services_text(services)}"
        c["service"] = None
    elif field == "date" and c.get("date"):
        question = f"Abhi din *{describe(c['date'], '12:00').split(',')[0]}* hai. Kis din karna hai?"
        c["date"] = None
    elif field == "time" and c.get("time"):
        question = f"Abhi waqt *{_clock(c['time'])}* hai. Naya waqt batayein? (e.g. 3 baje, sham 6 baje)"
        c["time"] = None
    elif field == "name":
        question = f"Abhi naam *{c['name']}* hai. Booking kis naam par karun?"
        c["name"] = None
        state.pop("remembered_name", None)
    elif field == "email" and c.get("email") is not None:
        question = _question("email", business, services)
        c["email"] = None
    else:
        state["pending"] = {"type": "change"}
        question = "Kya change karna hai? Naya din, waqt ya service batayein — ya 'rehne do' likhein."
    save_state(business["id"], phone, state)
    return question


# ----------------------------------------------------------------- main entry

# ----------------------------------------------------------------- safety nets (never trap a customer)

# The whole message is a restart word: works at any point, whatever AURA was waiting for.
RESTART_RE = re.compile(r"^\s*(menu|main\s*menu|restart|reset|start\s*over|start\s*again|shuru\s*se|"
                        r"dobara\s*shuru(\s*(karo|karein|kren|krain))?|new)\s*[.!]*\s*$", re.IGNORECASE)
LOOP_LIMIT = 3                 # the same reply this many times in a row, mid-request = stuck
STUCK_REPLY = ("Maaf kijiye, lagta hai baat ek hi jagah atak gayi 🙏 Main ne conversation dobara shuru kar di "
               "hai aur owner ko bhi bata diya hai.")


def _reply_key(business_id: int, phone: str) -> str:
    return f"lastreply:{business_id}:{phone}"


def _loop_guard(business: dict, phone: str, message: str, reply: str, in_progress: bool) -> str:
    """If AURA keeps sending the same reply while a request is in progress (a bug, a broken calendar,
    a question the customer can't answer), break out instead of repeating it forever."""
    key = _reply_key(business["id"], phone)
    try:
        last = json.loads(redis_client.get(key) or "{}")
    except ValueError:
        last = {}
    repeats = last.get("n", 0) + 1 if in_progress and last.get("reply") == reply else 1
    if repeats >= LOOP_LIMIT:
        clear_state(business["id"], phone)
        redis_client.delete(key)
        if not db.recent_open_escalation(business["id"], phone, "not_understood", hours=REPEAT_WINDOW_HOURS):
            escalate(business, phone, "not_understood",
                     f"Conversation stuck: AURA gave the same reply {repeats} times. Last message: {message}")
        return f"{STUCK_REPLY}\n\n{HELP_MENU}"
    redis_client.setex(key, SESSION_TTL, json.dumps({"reply": reply, "n": repeats}))
    return reply


def process_message(business: dict, phone: str, message: str) -> str:
    """Answer one customer message. Wraps _respond() with the two safety nets: a restart word that
    always works, and a breaker for replies stuck in a loop."""
    bid = business["id"]
    if RESTART_RE.match(message or ""):
        clear_state(bid, phone)
        redis_client.delete(_reply_key(bid, phone))
        return f"Theek hai, nayi shuruaat! 🙂\n\n{HELP_MENU}"
    before = get_state(bid, phone)
    in_progress = bool(before.get("intent") or before.get("pending"))
    return _loop_guard(business, phone, message, _respond(business, phone, message), in_progress)


def _respond(business: dict, phone: str, message: str) -> str:
    services = db.list_services(business["id"])
    bid = business["id"]
    state = get_state(bid, phone)
    state["turn_count"] += 1
    if state.get("last_failed_turn") != state["turn_count"] - 1:
        state["failed_turns"] = 0          # the last message was understood: start counting again
    message = message.strip()

    # "owner se baat karni hai" / "call me" -> the owner takes over (checked first, even mid-booking)
    if wants_human(message):
        return _hand_to_owner(state, business, phone)

    if state.get("pending"):
        return _handle_pending(state, business, services, phone, message)

    # Reply to a reminder ("haan" / "cancel")
    if state["intent"] is None and len(message.split()) <= 4:
        reply = handle_confirmation(business, phone, message)
        if reply:
            return reply

    if state["intent"] in FLOW:
        return _continue_flow(state, business, services, phone, message)

    # Small talk and quick questions that need no model
    if state.get("last_cancelled") and len(message.split()) <= 6 \
            and re.search(r"\b(konsi|kaunsi|kon\s*si|which)\b.*\b(cancel|booking|appointment)", message, re.I):
        save_state(bid, phone, state)
        return f"*Ye booking cancel hui thi:*\n{state['last_cancelled']}"
    if _is_thanks(message):
        clear_state(bid, phone)
        return "Aap ka bhi shukriya! 😊 Kuch aur madad chahiye ho toh message kar dein."
    if _is_ack(message):
        save_state(bid, phone, state)
        return "👍 Kuch aur madad chahiye ho toh batayein — booking, rates ya timings."
    if _words(message) and _words(message) <= NO_WORDS | {"kuch", "bas", "thanks", "shukriya"} and len(message.split()) <= 3:
        clear_state(bid, phone)                          # "nahi" / "kuch nahi" / "bas" = nothing else needed
        return "Theek hai! 😊 Jab zarurat ho message kar dein."
    if _asks_timings(message):
        save_state(bid, phone, state)
        return f"{timings_line(business)}\n\nAppointment book karni ho toh din aur waqt batayein!"
    if _asks_my_bookings(message):
        return _list_my_bookings(state, business, phone)
    if SMALL_TALK_RE.search(message) and len(message.split()) <= 8:
        save_state(bid, phone, state)
        reply = chat_reply(message, business["name"], business.get("business_type") or "",
                           "; ".join(line.lstrip("• ") for line in services_text(services).splitlines()),
                           hours_text(business)) or "Alhamdulillah, hum theek hain! 😊"
        return f"{reply}\n\n{HELP_MENU}"
    if SERVICES_QUESTION.search(message) and not match_service(message, services) and len(message.split()) <= 8:
        save_state(bid, phone, state)
        if not services:
            return NO_SERVICES.format(name=business["name"])
        return price_list_message(services)

    nlu = understand_message(message)
    intent = nlu.get("intent", "unknown")
    entities = nlu.get("entities", {})
    state["confidence"] = nlu.get("confidence", "low")

    # A bare service name or day/time ("Massage", "Friday 11 bjay subha") means the customer wants to book.
    # (A date alone only counts in a short message: "weather kaisa hai aaj" is not a booking.)
    if intent in ("unknown", "other") and (match_service(message, services) or entities.get("time")
                                           or (entities.get("date") and len(message.split()) <= 3)):
        intent, state["confidence"] = "book_appointment", "medium"

    if state["confidence"] == "low" or intent in ("unknown", "other"):
        reply = chat_reply(message, business["name"], business.get("business_type") or "",
                           "; ".join(line.lstrip("• ") for line in services_text(services).splitlines()),
                           hours_text(business))
        if reply:
            save_state(bid, phone, state)
            return f"{reply}\n\n{HELP_MENU}"
        return _fail(state, business, phone, message,
                     f"Sorry, mujhe samajh nahi aaya. {HELP_MENU}")

    if intent == "greeting":
        save_state(bid, phone, state)
        return _with_menu(_greeting(business, message))

    if intent == "cancel_booking":
        return _start_cancel(state, business, phone, message)

    if intent == "price_query":
        save_state(bid, phone, state)
        svc = match_service(message, services) or match_service(entities.get("service") or "", services)
        if svc:
            return (f"*{svc['name']}*\n• Fees: {format_price(svc['price'])}\n• Waqt: {svc['duration_min']} min\n\n"
                    f"Booking karni ho toh batayein kab aana chahenge!")
        if services:
            return price_list_message(services)
        return NO_SERVICES.format(name=business["name"])

    if intent == "reschedule":
        return _start_reschedule(state, business, services, phone, message, entities)

    # book_appointment / check_availability
    return _start_booking(intent, entities, state, business, services, phone, message)


# ----------------------------------------------------------------- my bookings / cancel

def _list_my_bookings(state, business, phone) -> str:
    bookings = db.upcoming_bookings(business["id"], phone)
    save_state(business["id"], phone, state)
    if not bookings:
        return "Aap ki koi upcoming booking nahi hai. Book karni ho toh service aur din batayein!"
    if len(bookings) == 1:
        return f"*Aap ki booking:*\n• {booking_line(bookings[0])}\n\nCancel ya shift karni ho toh batayein."
    return f"*Aap ki {len(bookings)} bookings hain:*\n{_numbered(bookings)}\n\nCancel ya shift karni ho toh batayein."


def _start_cancel(state, business, phone, message) -> str:
    bookings = db.upcoming_bookings(business["id"], phone)
    if not bookings:
        save_state(business["id"], phone, state)
        return "Aap ki koi upcoming booking nahi mili. Nayi booking karni ho toh batayein!"
    chosen = bookings[0] if len(bookings) == 1 else _pick(message, bookings)
    if chosen is None:
        state["pending"] = {"type": "pick", "action": "cancel", "ids": [b["id"] for b in bookings]}
        save_state(business["id"], phone, state)
        return (f"*Aap ki {len(bookings)} bookings hain:*\n{_numbered(bookings)}\n\n"
                f"Kaunsi cancel karni hai? Number ya din batayein (ya 'dono').")
    return _ask_cancel_confirmation(state, business, phone, bookings if chosen == "all" else [chosen])


def _ask_cancel_confirmation(state, business, phone, bookings: list) -> str:
    state["pending"] = {"type": "confirm_cancel", "ids": [b["id"] for b in bookings]}
    save_state(business["id"], phone, state)
    lines = "\n".join(f"• {booking_line(b)}" for b in bookings)
    what = "Ye booking" if len(bookings) == 1 else "Ye sab bookings"
    return f"*{what} cancel kar doon?*\n{lines}\n\nReply *yes* ya *no*."


def _do_cancel(state, business, phone, ids: list) -> str:
    done = [b for b in (cancel_booking_by_id(business, i) for i in ids) if b]
    fresh = new_state(phone)
    if not done:
        save_state(business["id"], phone, fresh)
        return "Ye booking pehle hi cancel ho chuki thi."
    lines = "\n".join(f"• {booking_line(b)}" for b in done)
    fresh["last_cancelled"] = lines
    save_state(business["id"], phone, fresh)
    return f"*Booking cancel kar di gayi* ✅\n{lines}\n\nDobara book karni ho toh batayein!"


# ----------------------------------------------------------------- pending questions

def _handle_pending(state, business, services, phone, message) -> str:
    bid = business["id"]
    p = state["pending"]

    if _is_goodbye(message):                       # leaving before answering: change nothing
        clear_state(bid, phone)
        if p["type"] in ("confirm_booking", "change"):
            return GOODBYE_MID_BOOKING
        return "Theek hai, koi change nahi kiya — aap ki bookings barqarar hain ✅ Allah Hafiz!"

    if p["type"] == "pick":
        bookings = [b for b in (db.get_booking(i) for i in p["ids"]) if b and b["status"] in ("booked", "confirmed")]
        chosen = _pick(message, bookings)
        if chosen is None:
            if _wants_to_stop(message.replace("cancel", "")) or _is_no(message):
                state["pending"] = None
                clear_state(bid, phone)
                return "Theek hai, koi change nahi kiya. Aap ki bookings barqarar hain ✅"
            return _fail(state, business, phone, message,
                         f"*Kaunsi booking?* Number likhein:\n{_numbered(bookings)}")
        state["pending"] = None
        if p["action"] == "cancel":
            return _ask_cancel_confirmation(state, business, phone, bookings if chosen == "all" else [chosen])
        if chosen == "all":
            return _fail(state, business, phone, message,
                         f"Ek waqt mein ek booking shift ho sakti hai. Kaunsi?\n{_numbered(bookings)}")
        return _begin_reschedule(state, business, services, phone, chosen, p.get("entities") or {})

    if p["type"] == "confirm_cancel":
        if _is_yes(message, also=frozenset({"cancel", "cancell", "kardo", "dono", "sab", "both", "all"})):
            return _do_cancel(state, business, phone, p["ids"])
        if _is_no(message) or _wants_to_stop(message.replace("cancel", "")):
            clear_state(bid, phone)
            return "Theek hai, booking cancel nahi ki — aap ki appointment barqarar hai ✅"
        return _fail(state, business, phone, message, "Cancel kar doon? Please *yes* ya *no* likhein.")

    if p["type"] in ("confirm_booking", "change"):
        if _wants_to_stop(message):
            clear_state(bid, phone)
            return "Theek hai, booking nahi ki. Jab chahein dobara message karein!"
        if _apply_changes(state, message, services):
            state["pending"] = None
            return _next_step(state, business, services, phone, prefix="Theek hai, change kar diya. ")
        field = _field_to_change(message, need_change_word=p["type"] == "confirm_booking")
        if field:                                  # "service change karni hai" (no new value given yet)
            return _ask_new_value(state, business, services, phone, field)
        if p["type"] == "confirm_booking" and _is_yes(message):
            state["pending"] = None
            return _finalize(state, business, services, phone)
        if p["type"] == "change" and _is_yes(message):     # "ok, theek hai" = keep it as it is
            state["pending"] = {"type": "confirm_booking"}
            save_state(bid, phone, state)
            return _summary(state, business, services)
        what_to_change = "Kya change karna hai? Naya din, waqt ya service batayein — ya 'rehne do' likhein."
        if _is_no(message) and len(message.split()) <= 2:         # a plain "nahi"
            state["pending"] = {"type": "change"}
            save_state(bid, phone, state)
            return what_to_change
        if _is_plain_greeting(message):
            save_state(bid, phone, state)
            return "Walaikum Assalam!\n\n" + _summary(state, business, services)
        # The rules didn't settle it ("ye wala nahi, koi aur dikhao", "chalo set karo"): ask Gemini what it means.
        meaning = confirmation_meaning(message)
        if meaning.get("meaning") == "yes" and p["type"] == "confirm_booking":
            state["pending"] = None
            return _finalize(state, business, services, phone)
        if meaning.get("meaning") == "change":
            return _ask_new_value(state, business, services, phone, meaning.get("change") or "")
        if meaning.get("meaning") == "question" or (not meaning and QUESTION_RE.search(message)):
            then = ("Booking confirm karni ho toh *yes* likhein, ya batayein kya change karna hai."
                    if p["type"] == "confirm_booking" else what_to_change)
            answered = _answer_aside(state, business, services, phone, message, then)
            if answered:
                return answered
        if meaning.get("meaning") == "no" or _is_no(message):       # (also when Gemini is unavailable)
            state["pending"] = {"type": "change"}
            save_state(bid, phone, state)
            return what_to_change
        return _fail(state, business, phone, message,
                     "Maaf kijiye, samajh nahi aaya. Booking confirm karne ke liye *yes* likhein, ya batayein kya "
                     "change karna hai (service, din ya waqt).")

    state["pending"] = None                       # unknown pending type from an older version
    return _respond(business, phone, message)


# ----------------------------------------------------------------- booking / reschedule

def _start_booking(intent, entities, state, business, services, phone, message) -> str:
    bid = business["id"]
    if not services:                               # nothing to book yet: never ask "kaunsi service?"
        clear_state(bid, phone)
        return NO_SERVICES.format(name=business["name"])
    state["intent"] = "book_appointment"
    prefix = ""
    _apply_entities(state, entities, message, services)
    c = state["collected"]
    if not c["name"]:
        # Returning customer: reuse their last name, and say so in the summary so they can change it.
        c["name"] = db.last_customer_name(bid, phone)
        state["remembered_name"] = c["name"]
    if business.get("asks_email") and c.get("email") is None:
        c["email"] = db.last_customer_email(bid, phone)    # returning customer: not asked again

    if intent == "check_availability" and c["date"] and not c["time"]:
        return _list_free_slots(state, business, services, phone)
    if intent == "check_availability" and c["date"] and c["time"]:
        if is_open_day(business, c["date"]):
            problem = _slot_problem(state, business, services)
            if problem is CALENDAR_DOWN:
                return _calendar_down(state, business, phone)
            if problem:
                save_state(bid, phone, state)
                return problem
            state["slot_checked"] = f"{c['date']} {c['time']} {_duration(state, services)}"
            prefix = f"Haan, {describe(c['date'], c['time'])} free hai ✅ Booking ke liye: "

    return _next_step(state, business, services, phone, prefix)


def _start_reschedule(state, business, services, phone, message, entities) -> str:
    bookings = db.upcoming_bookings(business["id"], phone)
    if not bookings:
        state["intent"] = "book_appointment"
        _apply_entities(state, entities, message, services)
        return _next_step(state, business, services, phone,
                          prefix="Aap ki koi upcoming booking nahi mili — nayi booking kar dete hain. ")
    # The message usually contains the NEW date ("Monday 2 baje shift karo"), so pick by
    # number / service / name only — not by date.
    chosen = bookings[0] if len(bookings) == 1 else _pick(message, bookings, use_dates=False)
    if chosen in (None, "all"):
        state["pending"] = {"type": "pick", "action": "reschedule", "ids": [b["id"] for b in bookings],
                            "entities": {k: entities.get(k) for k in ("date", "time")}}
        save_state(business["id"], phone, state)
        return f"*Aap ki {len(bookings)} bookings hain:*\n{_numbered(bookings)}\n\nKaunsi shift karni hai? Number batayein."
    return _begin_reschedule(state, business, services, phone, chosen, entities)


def _begin_reschedule(state, business, services, phone, booking, entities) -> str:
    state["intent"] = "reschedule"
    state["reschedule_of"] = booking["id"]
    c = state["collected"]
    c["service"], c["name"] = booking["service"], booking["customer_name"]
    c["email"] = booking.get("customer_email") or ""    # moving a booking: never asks for the email again
    c["date"], c["time"] = entities.get("date"), entities.get("time")
    questions = business.get("custom_questions") or []
    state["answers"] = [booking["answers"].get(q["question"], "") for q in questions]
    prefix = f"Aap ki booking abhi {describe(booking['date'], booking['time'])} ({booking['service']}) ki hai. "
    if not c["date"]:
        save_state(business["id"], phone, state)
        return prefix.rstrip() + "\n\nKis din shift karni hai?"
    return _next_step(state, business, services, phone, prefix)


def _spread(slots: list, n: int) -> list:
    """n free times spread over the day (morning, afternoon, evening), not just the first n."""
    if len(slots) <= n:
        return slots
    step = (len(slots) - 1) / (n - 1)
    return [slots[round(i * step)] for i in range(n)]


def _list_free_slots(state, business, services, phone) -> str:
    """'Is Friday free?' -> list Friday's free times."""
    c = state["collected"]
    if not is_open_day(business, c["date"]):
        return _next_step(state, business, services, phone)       # explains the closed day
    busy = get_busy(c["date"], business.get("calendar_id"))
    if busy is None:
        return _calendar_down(state, business, phone)
    slots = free_slots(business, c["date"], _duration(state, services), busy)
    save_state(business["id"], phone, state)
    if slots:
        return f"{suggestion_text(c['date'], _spread(slots, 6))}\n\nKaunsa time chahiye?"
    day, next_slots = suggest(business, c["date"], None, _duration(state, services),
                              lambda d: get_busy(d, business.get("calendar_id")))
    if next_slots:
        c["date"] = day
        save_state(business["id"], phone, state)
        return f"Us din koi slot free nahi.\n\n{suggestion_text(day, next_slots)}\n\nKaunsa time chahiye?"
    return "Agle kuch din koi slot free nahi. Owner se rabta karein please."


def _continue_flow(state, business, services, phone, message) -> str:
    bid = business["id"]
    if _wants_to_stop(message):
        clear_state(bid, phone)
        return "Theek hai, booking ka process rok diya. Jab chahein dobara message karein!"
    if _is_goodbye(message):                       # "bye" / "thank you" while AURA is asking questions
        clear_state(bid, phone)
        return GOODBYE_MID_BOOKING

    missing = get_missing_fields(state, business)
    if missing and _is_plain_greeting(message):
        save_state(bid, phone, state)
        return "Walaikum Assalam!\n\n" + _question(missing[0], business, services, state)
    if missing and _asks_timings(message):
        save_state(bid, phone, state)
        return f"{timings_line(business)}\n\n" + _question(missing[0], business, services, state)

    state, got_it = collect_field(state, message, business, services)
    if not got_it and (QUESTION_RE.search(message) or SMALL_TALK_RE.search(message)) \
            and not state.get("question_problem"):
        missing = get_missing_fields(state, business)
        answered = _answer_aside(state, business, services, phone, message,
                                 _question(missing[0], business, services, state))
        if answered:
            return answered
    if not got_it:
        missing = get_missing_fields(state, business)
        if missing[0] == "service" and services:
            options = similar_services(message, services)
            if len(options) >= 2:                                   # "hair" -> Haircut ya Hair colour?
                reply = f"*Kaunsi wali?*\n{services_text(options)}"
            else:
                reply = f"Ye service hamari list mein nahi hai. *Inn mein se batayein:*\n{services_text(services)}"
        elif state.get("question_problem"):
            reply = state.pop("question_problem")
        else:
            reply = "Maaf kijiye, samajh nahi aaya.\n\n" + _question(missing[0], business, services, state)
        return _fail(state, business, phone, message, reply)

    return _next_step(state, business, services, phone)


def _next_step(state, business, services, phone, prefix: str = "") -> str:
    """Check what we have so far, then ask the next question or show the summary.
    prefix: a short lead-in ('Theek hai, change kar diya.'), put on its own line above the rest."""
    bid = business["id"]
    c = state["collected"]
    prefix = f"{prefix.rstrip()}\n\n" if prefix.strip() else ""

    if c["date"] and not is_open_day(business, c["date"]):
        day_name = describe(c["date"], "12:00").split(" ")[0]
        c["date"] = None
        save_state(bid, phone, state)
        return (f"{prefix}{day_name} ko hum band hote hain.\n{timings_line(business)}\n\n"
                + _question("date", business, services, state))

    if c["date"] and c["time"]:
        checked = f"{c['date']} {c['time']} {_duration(state, services)}"
        if state.get("slot_checked") != checked:
            problem = _slot_problem(state, business, services)
            if problem is CALENDAR_DOWN:
                return _calendar_down(state, business, phone)
            if problem:
                save_state(bid, phone, state)
                return prefix + problem
            state["slot_checked"] = checked

    missing = get_missing_fields(state, business)
    if missing:
        save_state(bid, phone, state)
        return prefix + _question(missing[0], business, services, state)

    if state.get("reschedule_of") and prefix.startswith("Aap ki booking abhi"):
        prefix = ""                     # the summary already shows the old time ("Purani: ...")
    state["pending"] = {"type": "confirm_booking"}
    save_state(bid, phone, state)
    return prefix + _summary(state, business, services)


def _summary(state, business, services) -> str:
    c = state["collected"]
    svc = next((s for s in services if s["name"] == c["service"]), None)
    price = f" ({format_price(svc['price'])})" if svc and svc.get("price") else ""
    name = c["name"]
    if state.get("remembered_name") and name == state["remembered_name"] and not state.get("reschedule_of"):
        name += " (pichli booking wala naam — alag ho toh likhein: naam Ali)"
    lines = [f"• Service: {c['service']}{price}", f"• Waqt: {describe(c['date'], c['time'])}", f"• Naam: {name}"]
    for q, a in zip(business.get("custom_questions") or [], state.get("answers") or []):
        lines.append(f"• {q['question']} {a or '—'}")
    if c.get("email"):
        lines.append(f"• Email: {c['email']}")
    if state.get("reschedule_of"):
        old = db.get_booking(state["reschedule_of"])
        head = (f"*Booking shift kar doon?*\nPurani: {describe(old['date'], old['time'])}\nNayi:" if old
                else "*Booking shift kar doon?*")
    else:
        head = "*Ye booking confirm kar doon?*"
    return f"{head}\n" + "\n".join(lines) + "\n\nReply *yes* — ya batayein kya change karna hai."


def _finalize(state, business, services, phone) -> str:
    bid = business["id"]
    c = state["collected"]

    # Values are normally already clean (YYYY-MM-DD / HH:MM); re-parse anything else.
    c["date"] = c["date"] if _is_iso_date(c["date"]) else parse_date(c["date"])
    c["time"] = c["time"] if _is_hhmm(c["time"]) else parse_time(c["time"])
    if not c["date"] or not c["time"]:
        return _next_step(state, business, services, phone)

    # Check again right before booking: someone else may have taken the slot meanwhile.
    problem = _slot_problem(state, business, services)
    if problem is CALENDAR_DOWN:
        return _calendar_down(state, business, phone)
    if problem:
        save_state(bid, phone, state)
        return problem

    duration = _duration(state, services)
    questions = business.get("custom_questions") or []
    answers = {q["question"]: a for q, a in zip(questions, state.get("answers") or [])}
    notes = "\n".join(f"{q} {a}" for q, a in answers.items() if a)

    result = book_appointment(name=c["name"], service=c["service"], date=c["date"], time=c["time"],
                              phone=phone, calendar_id=business.get("calendar_id"),
                              duration=duration, notes=notes)
    if not result["success"]:
        return _calendar_down(state, business, phone)     # owner gets the request; customer isn't stuck

    booking_id = db.create_booking(bid, phone, c["name"], c["service"], c["date"], c["time"],
                                   duration, result["event_id"], answers, customer_email=c.get("email"))

    moved_from = None
    old_id = state.get("reschedule_of")
    if old_id:
        old = db.get_booking(old_id)
        if old and old["status"] in ("booked", "confirmed"):
            if old.get("event_id"):
                cancel_appointment(old["event_id"], business.get("calendar_id"))
            db.set_booking_status(old_id, "rescheduled")
            cancel_reminders(old_id)
            moved_from = old

    schedule_reminders(booking_id)
    clear_state(bid, phone)
    booking = db.get_booking(booking_id)
    emailed = customer_mail.booking_confirmed(business, booking, moved_from=moved_from)
    title = "Booking shift ho gayi!" if moved_from else "Booking confirm ho gayi!"
    email_line = f"📧 Confirmation email: {customer_mail.customer_email(business, booking)}\n" if emailed else ""
    return (
        f"✅ *{title}*\n"
        f"🏪 {business['name']}\n\n"
        f"• Naam: {c['name']}\n"
        f"• Service: {c['service']}\n"
        f"• Waqt: {describe(c['date'], c['time'])}\n\n"
        f"{email_line}"
        f"Appointment se pehle reminder bhej diya jayega. Shukriya!"
    )
