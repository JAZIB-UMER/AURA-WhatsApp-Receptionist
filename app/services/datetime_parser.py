"""Rule-based date/time understanding for Roman Urdu + English messages.

parse_date("Friday ko")        -> "2026-11-13"
parse_time("sham 6 baje")      -> "18:00"
parse_time("saadhe 4")         -> "16:30"

Deterministic on purpose: the same message always gives the same slot,
so AURA never books a time the customer did not say.
"""
import re
from datetime import date, datetime, timedelta, timezone

PKT = timezone(timedelta(hours=5))  # Pakistan time, no daylight saving


def today_pkt() -> date:
    return datetime.now(PKT).date()


def _words(text: str) -> list:
    return re.findall(r"[a-z]+|\d+", text.lower())


# ----------------------------------------------------------------- dates

WEEKDAYS = {
    # English
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3, "friday": 4, "fri": 4,
    "saturday": 5, "sunday": 6,   # no "sat"/"sun": clash with Urdu "saat" (7) and "sun" (listen)
    # Roman Urdu ("hafta" is left out: it also means "week")
    "peer": 0, "pir": 0, "somwar": 0, "mangal": 1, "mangalwar": 1, "budh": 2, "budhwar": 2,
    "jumerat": 3, "jumeraat": 3, "jumeraaat": 3, "juma": 4, "jumma": 4, "jummah": 4, "jumah": 4,
    "itwar": 6, "itwaar": 6, "itvar": 6,
}

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3, "apr": 4, "april": 4,
    "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9, "oct": 10, "october": 10,
    "nov": 11, "november": 11, "dec": 12, "december": 12,
}

TODAY_WORDS = {"aaj", "aj", "aja", "today", "tday"}
TOMORROW_WORDS = {"kal", "kl", "kall", "tomorrow", "tmrw", "tomorow", "tommorow", "tmr"}
DAY_AFTER_WORDS = {"parso", "parson", "parsu", "parsoo", "parsun"}


def _safe_date(y: int, m: int, d: int):
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _future(d: date, today: date, roll) -> date:
    """If d is already past, move it forward with roll(d)."""
    return roll(d) if d and d < today else d


def parse_date(text: str, today: date = None):
    """Return 'YYYY-MM-DD' for the date mentioned in text, or None."""
    if not text:
        return None
    today = today or today_pkt()
    t = re.sub(r"(\d+)(?:st|nd|rd|th)\b", r"\1", str(text).lower())  # 15th -> 15
    words = _words(t)

    # 1. Already ISO: 2026-11-14
    m = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", t)
    if m:
        d = _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return d.isoformat() if d else None

    # 2. Relative words (check "day after tomorrow" before "tomorrow")
    if "day after tomorrow" in t or DAY_AFTER_WORDS & set(words):
        return (today + timedelta(days=2)).isoformat()
    if TOMORROW_WORDS & set(words):
        return (today + timedelta(days=1)).isoformat()
    if TODAY_WORDS & set(words):
        return today.isoformat()

    # 3. Numeric: 14/11, 14-11-2026, 14.11
    m = re.search(r"\b(\d{1,2})[/\-.](\d{1,2})(?:[/\-.](\d{2,4}))?\b", t)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        year = int(m.group(3)) if m.group(3) else today.year
        year = year + 2000 if year < 100 else year
        d = _safe_date(year, month, day)
        if d and not m.group(3):
            d = _future(d, today, lambda x: _safe_date(x.year + 1, x.month, x.day))
        if d:
            return d.isoformat()

    # 4. Day + month name: "14 nov", "nov 14", "14th november"
    for i, w in enumerate(words):
        if w in MONTHS:
            day = None
            if i > 0 and words[i - 1].isdigit():
                day = int(words[i - 1])
            elif i + 1 < len(words) and words[i + 1].isdigit():
                day = int(words[i + 1])
            if day:
                d = _safe_date(today.year, MONTHS[w], day)
                d = _future(d, today, lambda x: _safe_date(x.year + 1, x.month, x.day))
                if d:
                    return d.isoformat()

    # 5. Weekday: next occurrence ("Monday" said on a Monday means next week)
    for w in words:
        if w in WEEKDAYS:
            ahead = (WEEKDAYS[w] - today.weekday()) % 7 or 7
            return (today + timedelta(days=ahead)).isoformat()

    # 6. "15 tareekh" = the 15th of this month (or next month if passed)
    m = re.search(r"\b(\d{1,2})\s*(?:st|nd|rd|th)?\s*(?:tareekh|tarikh|tareek|tarik|date)\b", t)
    if m:
        day = int(m.group(1))
        d = _safe_date(today.year, today.month, day)
        if d and d < today:
            nxt = today.replace(day=1) + timedelta(days=32)
            d = _safe_date(nxt.year, nxt.month, day)
        if d:
            return d.isoformat()

    return None


# ----------------------------------------------------------------- times

MORNING = {"subah", "subha", "suba", "morning", "savere", "sawere"}
AFTERNOON = {"dopahar", "dopehar", "dupahar", "dophar", "dopher", "dopair", "afternoon", "noon"}
EVENING = {"sham", "shaam", "evening", "raat", "rat", "night"}

TIME_MARKERS = r"(?:baje|bje|bajay|bajey|bjay|bjey|bjy|bj|baj|bajy|o\s?clock|oclock)"
TIME_RE = re.compile(
    r"(?<![\d/\-.])(\d{1,2})(?:[:.](\d{2}))?\s*(a\.?m\.?|p\.?m\.?|" + TIME_MARKERS + r")?(?![\d/\-])",
    re.IGNORECASE,
)
NUMBER_WORDS = {
    "ek": 1, "aik": 1, "one": 1, "do": 2, "two": 2, "teen": 3, "tin": 3, "three": 3,
    "char": 4, "chaar": 4, "four": 4, "panch": 5, "paanch": 5, "five": 5,
    "chay": 6, "chhe": 6, "che": 6, "cheh": 6, "chah": 6, "six": 6, "saat": 7, "sat": 7, "seven": 7,
    "aath": 8, "aat": 8, "eight": 8, "nau": 9, "nine": 9, "das": 10, "dus": 10, "ten": 10,
    "gyarah": 11, "gyara": 11, "giyarah": 11, "eleven": 11,
    "barah": 12, "bara": 12, "baara": 12, "baarah": 12, "twelve": 12,
}
_NUM_ALT = "|".join(sorted(NUMBER_WORDS, key=len, reverse=True))
FRACTION_WORDS = r"(?:saadhe|saade|sadhe|sade|saarhe|sarhe|sare|saray|sawa|sava|swa|paune|pone|pauney|poney)"


def _numbers_to_digits(t: str) -> str:
    """'teen baje' -> '3 baje', 'saadhe char' -> 'saadhe 4' (only next to time words)."""
    def to_digit(m):
        return str(NUMBER_WORDS[m.group(1)])
    t = re.sub(r"\b(" + _NUM_ALT + r")\b(?=\s*" + TIME_MARKERS + ")", to_digit, t)
    t = re.sub(r"\b(" + FRACTION_WORDS + r")\s+(" + _NUM_ALT + r")\b",
               lambda m: m.group(1) + " " + str(NUMBER_WORDS[m.group(2)]), t)
    return t


# saadhe 4 = 4:30, sawa 4 = 4:15, paune 4 = 3:45
FRACTIONS = [
    (r"(?:saadhe|saade|sadhe|sade|saarhe|sarhe|sare|saray)\s+(\d{1,2})", 30, 0),
    (r"(?:sawa|sava|swa)\s+(\d{1,2})", 15, 0),
    (r"(?:paune|pone|pauney|poney)\s+(\d{1,2})", 45, -1),
]


def _to_24h(hour: int, minute: int, ampm: str, period: str):
    if ampm == "am":
        hour = 0 if hour == 12 else hour
    elif ampm == "pm":
        hour = hour if hour == 12 else hour + 12
    elif period == "morning":
        hour = 0 if hour == 12 else hour
    elif period in ("afternoon", "evening"):
        hour = hour if hour == 12 else hour + 12
    else:
        # No am/pm and no "subah/sham": assume normal business hours.
        # 1-7 -> afternoon/evening, 8-11 -> morning, 12 -> noon.
        if 1 <= hour <= 7:
            hour += 12
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return f"{hour:02d}:{minute:02d}"
    return None


def parse_time(text: str):
    """Return 'HH:MM' (24-hour) for the time mentioned in text, or None."""
    if not text:
        return None
    t = _numbers_to_digits(str(text).lower())
    words = set(_words(t))

    period = None
    if words & MORNING:
        period = "morning"
    elif words & AFTERNOON:
        period = "afternoon"
    elif words & EVENING:
        period = "evening"

    if "noon" in words and not re.search(r"\d", t):
        return "12:00"

    # Roman Urdu fractions: saadhe 4, sawa 3, paune 5
    for pattern, minute, hour_shift in FRACTIONS:
        m = re.search(pattern, t)
        if m:
            hour = int(m.group(1)) + hour_shift
            if hour < 1:
                hour = 12
            return _to_24h(hour, minute, None, period)

    for m in TIME_RE.finditer(t):
        hour, minute = int(m.group(1)), int(m.group(2) or 0)
        suffix = (m.group(3) or "").replace(".", "").replace(" ", "")
        ampm = "am" if suffix.startswith("a") else "pm" if suffix.startswith("p") else None
        has_marker = bool(suffix) or m.group(2) is not None
        before = t[max(0, m.start() - 4):m.start()]
        # A bare number only counts as a time next to a time word: "sham 6", "at 4"
        if not has_marker and not period and not re.search(r"\bat\s*$", before):
            continue
        if hour > 23 or (ampm and not 1 <= hour <= 12):
            continue
        if m.group(1).startswith("0") and m.group(2) is not None and not ampm:
            # "07:00" / "01:30" written with a leading zero is already 24-hour time
            return f"{hour:02d}:{minute:02d}"
        result = _to_24h(hour, minute, ampm, period)
        if result:
            return result
    return None


def describe(date_iso: str, time_hm: str) -> str:
    """Friendly text for confirmations: 'Friday 13 Nov, 4:30 PM'."""
    try:
        dt = datetime.strptime(f"{date_iso} {time_hm}", "%Y-%m-%d %H:%M")
        return dt.strftime("%A %d %b, %I:%M %p").replace(" 0", " ")
    except (ValueError, TypeError):
        return f"{date_iso} {time_hm}"
