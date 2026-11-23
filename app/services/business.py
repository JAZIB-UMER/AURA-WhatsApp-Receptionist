"""Business rules: services, prices, opening hours and free-slot search.

Pure functions (no network, no database) so they are easy to test.
A "business" is the dict returned by db.get_business(); a "service" is a
dict with name / price / duration_min.
"""
import difflib
import re
from datetime import date, datetime, timedelta

from app.services.datetime_parser import PKT, describe

DEFAULT_DURATION = 30
SLOT_STEP_MIN = 30          # free slots are offered on a 30-minute grid
DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
SHORT_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# ------------------------------------------------------------- services

def _norm(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(text).lower()))


# Common Roman Urdu / English words that must never fuzzy-match a service name
# (e.g. "hai" looks like "hair").
FILLER_WORDS = {
    "hai", "hain", "ha", "ki", "ka", "ke", "ko", "se", "me", "mein", "main", "mai", "mujhe", "mujhy", "meri",
    "mera", "apni", "aap", "ap", "karni", "karna", "karwani", "karwana", "kar", "kr", "do", "dein", "den",
    "book", "booking", "appointment", "chahiye", "chahye", "please", "plz", "sir", "madam", "bhai", "kal",
    "aaj", "baje", "bje", "time", "slot", "want", "need", "the", "and", "for", "with", "kya", "kitni", "kitne",
    "fees", "price", "rate", "rates", "charges", "lagwani", "lagwana", "karwa", "wali", "wala",
}


def match_service(text: str, services: list):
    """Find which of the business's services the customer means, or None.

    'haircut chahiye' -> Haircut;  'hair cut' -> Haircut;  'nails' -> Nail polish
    """
    if not text or not services:
        return None
    said = _norm(text)
    said_compact = said.replace(" ", "")
    best, best_score, tied = None, 0.0, False
    for s in services:
        name = _norm(s["name"])
        compact = name.replace(" ", "")
        if not name:
            continue
        if name in said or compact in said_compact:
            score = 1.0                                   # full service name mentioned
        elif said and (said in name or said_compact in compact) and len(said_compact) >= 3:
            score = 0.9                                   # customer typed part of the name
        else:
            # fuzzy (typos like "haircutt", "nails", "color"): only real words of 4+ letters
            # that start with the same letter, never filler words like "hai"
            words = [w for w in said.split() if len(w) >= 4 and w not in FILLER_WORDS]
            score = 0.0
            for sw in name.split():
                if len(sw) < 4:
                    continue
                for w in words:
                    if w[0] == sw[0]:
                        score = max(score, difflib.SequenceMatcher(None, sw, w).ratio())
            score = score if score >= 0.8 else 0.0
        if score > best_score:
            best, best_score, tied = s, score, False
        elif score and score == best_score:
            tied = True
    # "hair" when the business has both Haircut and Hair colour: ask, don't guess
    if tied and best_score < 1.0:
        return None
    return best


def similar_services(text: str, services: list) -> list:
    """Services whose name contains a word the customer used ('hair' -> Haircut, Hair colour)."""
    words = [w for w in _norm(text).split() if len(w) >= 3 and w not in FILLER_WORDS]
    return [s for s in services if any(w in _norm(s["name"]).replace(" ", "") for w in words)]


def format_price(price) -> str:
    return f"Rs {int(price):,}" if price else "price on request"


# WhatsApp formatting used in every reply: *bold* headings, one "•" bullet per item, timings on their
# own line, and the question to the customer on its own line at the end.

def services_text(services: list) -> str:
    """One service per line:
    • Haircut — Rs 800
    • Nail polish — Rs 500"""
    return "\n".join(f"• {s['name']} — {format_price(s['price'])}" for s in services)


def price_list(services: list) -> str:
    """• Haircut — Rs 800 (30 min)"""
    return "\n".join(f"• {s['name']} — {format_price(s['price'])} ({s['duration_min']} min)" for s in services)


def price_list_message(services: list) -> str:
    """AURA's full reply to 'rates?' (also previewed on the dashboard's Services page)."""
    return f"*Hamari services aur rates:*\n{price_list(services)}\n\nBooking ke liye service aur din batayein."


def duration_for(service_name: str, services: list) -> int:
    for s in services:
        if s["name"] == service_name:
            return int(s.get("duration_min") or DEFAULT_DURATION)
    return DEFAULT_DURATION


# ------------------------------------------------------------- opening hours

def _hm(value: str):
    h, m = str(value).split(":")
    return int(h), int(m)


def _at(day: date, hhmm: str) -> datetime:
    h, m = _hm(hhmm)
    return datetime(day.year, day.month, day.day, h, m, tzinfo=PKT)


def _fmt_time(hhmm: str) -> str:
    return datetime.strptime(hhmm, "%H:%M").strftime("%I:%M %p").lstrip("0")


def hours_text(business: dict) -> str:
    """'Mon–Sat, 10:00 AM – 8:00 PM'"""
    days = sorted(business.get("open_days") or [])
    if not days:
        return "abhi koi din set nahi"
    if days == list(range(days[0], days[-1] + 1)) and len(days) > 2:
        day_part = f"{SHORT_DAYS[days[0]]}–{SHORT_DAYS[days[-1]]}"
    else:
        day_part = ", ".join(SHORT_DAYS[d] for d in days)
    return f"{day_part}, {_fmt_time(business['open_time'])} – {_fmt_time(business['close_time'])}"


def timings_line(business: dict) -> str:
    """'🕒 *Timings:* Mon–Sat, 10:00 AM – 8:00 PM' — always sent on its own line."""
    return f"🕒 *Timings:* {hours_text(business)}"


def is_open_day(business: dict, date_iso: str) -> bool:
    return date.fromisoformat(date_iso).weekday() in (business.get("open_days") or [])


def within_hours(business: dict, date_iso: str, time_hm: str, duration: int) -> bool:
    """Open that day, and the whole appointment fits between opening and closing time."""
    if not is_open_day(business, date_iso):
        return False
    day = date.fromisoformat(date_iso)
    start = _at(day, time_hm)
    return _at(day, business["open_time"]) <= start and start + timedelta(minutes=duration) <= _at(day, business["close_time"])


# ------------------------------------------------------------- free slots

def overlaps(start: datetime, end: datetime, busy: list) -> bool:
    return any(start < b_end and b_start < end for b_start, b_end in busy)


def free_slots(business: dict, date_iso: str, duration: int, busy: list, now: "datetime | None" = None) -> list:
    """All free start times ('HH:MM') on a day, inside opening hours, not in the past."""
    if not is_open_day(business, date_iso):
        return []
    now = now or datetime.now(PKT)
    day = date.fromisoformat(date_iso)
    t = _at(day, business["open_time"])
    close = _at(day, business["close_time"])
    slots = []
    while t + timedelta(minutes=duration) <= close:
        end = t + timedelta(minutes=duration)
        if t > now and not overlaps(t, end, busy):
            slots.append(t.strftime("%H:%M"))
        t += timedelta(minutes=SLOT_STEP_MIN)
    return slots


def nearest(slots: list, wanted_hm: "str | None", limit: int = 3) -> list:
    """The free slots closest to the time the customer asked for, in time order."""
    if not wanted_hm:
        return slots[:limit]
    wh, wm = _hm(wanted_hm)
    target = wh * 60 + wm
    closest = sorted(slots, key=lambda s: abs(_hm(s)[0] * 60 + _hm(s)[1] - target))[:limit]
    return sorted(closest)


def suggest(business: dict, date_iso: str, wanted_hm: "str | None", duration: int, get_busy, now: "datetime | None" = None,
            limit: int = 3, search_days: int = 7):
    """Nearest free slots on the requested day, or else on the next open days.

    get_busy(date_iso) -> list of (start, end) datetimes, or None if the calendar can't be read.
    Returns (date_iso, [HH:MM, ...]) or (None, []) if nothing is free.
    """
    day = date.fromisoformat(date_iso)
    for offset in range(search_days + 1):
        d = (day + timedelta(days=offset)).isoformat()
        if not is_open_day(business, d):
            continue
        busy = get_busy(d)
        if busy is None:                     # calendar unreadable: suggest nothing rather than guess
            return None, []
        slots = free_slots(business, d, duration, busy, now)
        if slots:
            return d, nearest(slots, wanted_hm if offset == 0 else None, limit)
    return None, []


def suggestion_text(date_iso: str, slots: list) -> str:
    """A bold heading with the day, then one free time per line:
    *Free slots — Friday 13 Nov:*
    • 4:30 PM
    • 5:30 PM"""
    if not date_iso or not slots:
        return ""
    day = describe(date_iso, slots[0]).split(",")[0]
    return f"*Free slots — {day}:*\n" + "\n".join(f"• {_fmt_time(s)}" for s in slots)
