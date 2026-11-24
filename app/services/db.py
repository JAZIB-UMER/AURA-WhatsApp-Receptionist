"""SQLite storage for businesses, their services, and bookings.

One file (storage/aura.db), no server to install. Redis is still used for
short-lived things (conversation state, duplicate-message checks).
"""
import hashlib
import json
import os
import secrets
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from app.services.datetime_parser import today_pkt
from app.services.questions import normalize as normalize_questions

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = Path(os.getenv("AURA_DB") or "storage/aura.db")       # relative paths are inside the project
if not DB_PATH.is_absolute():
    DB_PATH = PROJECT_ROOT / DB_PATH

WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS businesses (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT NOT NULL,
    business_type     TEXT DEFAULT '',
    greeting          TEXT DEFAULT '',
    username          TEXT UNIQUE NOT NULL,
    password_hash     TEXT NOT NULL,
    whatsapp_session  TEXT,                         -- WAHA session of the business's WhatsApp (QR-linked)
    calendar_id       TEXT DEFAULT '',
    owner_phone       TEXT DEFAULT '',
    open_days         TEXT DEFAULT '0,1,2,3,4,5',   -- 0 = Monday ... 6 = Sunday
    open_time         TEXT DEFAULT '10:00',
    close_time        TEXT DEFAULT '20:00',
    custom_questions  TEXT DEFAULT '[]',            -- JSON: [{"question", "type", "required"}]
    mail_from         TEXT DEFAULT '',              -- the business's own email: booking emails to customers
    mail_password     TEXT DEFAULT '',              --   its App Password (never shown again)
    mail_host         TEXT DEFAULT '',              --   mail server, '' = smtp.gmail.com
    mail_port         TEXT DEFAULT '',              --   '' = 587
    ask_email         INTEGER DEFAULT 1,            -- ask customers for their email (optional question)
    active            INTEGER DEFAULT 1,
    created_at        TEXT
);
CREATE TABLE IF NOT EXISTS services (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id  INTEGER NOT NULL REFERENCES businesses(id),
    name         TEXT NOT NULL,
    price        INTEGER DEFAULT 0,
    duration_min INTEGER DEFAULT 30
);
CREATE TABLE IF NOT EXISTS bookings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id   INTEGER NOT NULL REFERENCES businesses(id),
    phone         TEXT NOT NULL,
    customer_name TEXT,
    service       TEXT,
    date          TEXT NOT NULL,       -- YYYY-MM-DD
    time          TEXT NOT NULL,       -- HH:MM
    duration_min  INTEGER DEFAULT 30,
    event_id      TEXT,
    status        TEXT DEFAULT 'booked',   -- booked | confirmed | cancelled | rescheduled
    answers       TEXT DEFAULT '{}',       -- answers to custom questions (JSON)
    customer_email TEXT,                   -- for booking emails (optional, given by the customer)
    created_at    TEXT,
    updated_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_bookings_lookup ON bookings(business_id, phone, status, date);
CREATE TABLE IF NOT EXISTS dashboard_sessions (
    token_hash   TEXT PRIMARY KEY,         -- SHA-256 of the cookie value (the token itself is never stored)
    role         TEXT NOT NULL,            -- admin | owner
    business_id  INTEGER,
    expires_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (            -- backup of every WhatsApp message with a customer
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id  INTEGER NOT NULL REFERENCES businesses(id),
    phone        TEXT NOT NULL,
    direction    TEXT NOT NULL,                  -- in (customer) | out (AURA / business)
    kind         TEXT DEFAULT 'chat',            -- chat | reminder | followup | notice
    text         TEXT NOT NULL,
    booking_id   INTEGER,                        -- set for reminders and dashboard notices
    delivered    INTEGER,                        -- outgoing only: 1 sent, 0 failed
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(business_id, phone, created_at);
CREATE TABLE IF NOT EXISTS escalations (         -- cases handed to the owner
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id       INTEGER NOT NULL REFERENCES businesses(id),
    phone             TEXT NOT NULL,
    customer_name     TEXT,
    kind              TEXT NOT NULL,             -- human_request | not_understood | unconfirmed
    reason            TEXT NOT NULL,
    booking_id        INTEGER,
    summary           TEXT,                      -- recent conversation sent to the owner
    notified_whatsapp INTEGER DEFAULT 0,
    notified_email    INTEGER DEFAULT 0,
    status            TEXT DEFAULT 'open',       -- open | resolved
    created_at        TEXT NOT NULL,
    resolved_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_escalations_open ON escalations(business_id, status, created_at);
CREATE TABLE IF NOT EXISTS contacts (            -- the name each customer set in WhatsApp (display only)
    business_id   INTEGER NOT NULL REFERENCES businesses(id),
    phone         TEXT NOT NULL,
    profile_name  TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (business_id, phone)
);
CREATE TABLE IF NOT EXISTS settings (            -- platform settings the admin edits (API keys, email, ...)
    key          TEXT PRIMARY KEY,
    value        TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
"""

# Columns added after the first release; init_db() adds them to existing databases.
# (Databases from the WaAPI version keep their unused waapi_instance_id column.)
MIGRATIONS = {"businesses": {"owner_email": "TEXT DEFAULT ''", "whatsapp_session": "TEXT",
                              "mail_from": "TEXT DEFAULT ''", "mail_password": "TEXT DEFAULT ''",
                              "mail_host": "TEXT DEFAULT ''", "mail_port": "TEXT DEFAULT ''",
                              "ask_email": "INTEGER DEFAULT 1"},
              "bookings": {"customer_email": "TEXT"}}
# Indexes on migrated columns (SQLite can't add a UNIQUE column, so uniqueness is an index).
INDEXES = "CREATE UNIQUE INDEX IF NOT EXISTS idx_businesses_session ON businesses(whatsapp_session);"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with connect() as conn:
        conn.executescript(SCHEMA)
        for table, columns in MIGRATIONS.items():
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            for column, definition in columns.items():
                if column not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        conn.executescript(INDEXES)


# ------------------------------------------------------------ passwords

def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000).hex()
    return f"{salt}${digest}"


def check_password(password: str, stored: str) -> bool:
    try:
        salt, digest = stored.split("$", 1)
    except (ValueError, AttributeError):
        return False
    test = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000).hex()
    return secrets.compare_digest(test, digest)


# ------------------------------------------------------------ dashboard login sessions

def _token_hash(token: str) -> str:
    return hashlib.sha256((token or "").encode()).hexdigest()


def create_session(role: str, business_id: "int | None" = None, hours: int = 12) -> str:
    """New login session; returns the random token for the browser cookie."""
    token = secrets.token_urlsafe(32)
    expires = (datetime.now() + timedelta(hours=hours)).isoformat(timespec="seconds")
    with connect() as conn:
        conn.execute("DELETE FROM dashboard_sessions WHERE expires_at < ?", (_now(),))
        conn.execute("INSERT INTO dashboard_sessions (token_hash, role, business_id, expires_at) VALUES (?,?,?,?)",
                     (_token_hash(token), role, business_id, expires))
    return token


def get_session(token: str):
    """{"role", "business_id"} for a valid, unexpired token, else None."""
    if not token:
        return None
    with connect() as conn:
        row = conn.execute("SELECT role, business_id, expires_at FROM dashboard_sessions WHERE token_hash = ?",
                           (_token_hash(token),)).fetchone()
    if not row or row["expires_at"] < _now():
        return None
    return {"role": row["role"], "business_id": row["business_id"]}


def delete_session(token: str):
    if token:
        with connect() as conn:
            conn.execute("DELETE FROM dashboard_sessions WHERE token_hash = ?", (_token_hash(token),))


def delete_business_sessions(business_id: int):
    """Log an owner out everywhere (password changed / business deactivated)."""
    with connect() as conn:
        conn.execute("DELETE FROM dashboard_sessions WHERE business_id = ?", (business_id,))


# ------------------------------------------------------------ businesses

BUSINESS_FIELDS = {"name", "business_type", "greeting", "username", "whatsapp_session", "calendar_id",
                   "owner_phone", "owner_email", "open_days", "open_time", "close_time", "custom_questions",
                   "active", "mail_from", "mail_password", "mail_host", "mail_port", "ask_email"}


def _business(row):
    if row is None:
        return None
    b = dict(row)
    b["open_days"] = [int(d) for d in str(b["open_days"] or "").split(",") if d.strip() != ""]
    b["custom_questions"] = normalize_questions(json.loads(b["custom_questions"] or "[]"))
    b.pop("password_hash", None)
    # The App Password stays in the database (mail_account()); pages only learn whether one is saved.
    has_password = bool(b.pop("mail_password", None))
    b["mail_ready"] = bool(b.get("mail_from")) and has_password
    # AURA asks for the customer's email only when it can send them one, and only if the owner's own
    # questions don't already ask for an email.
    b["asks_email"] = bool(b["mail_ready"] and b.get("ask_email", 1)
                           and not any(q["type"] == "email" for q in b["custom_questions"]))
    return b


def mail_account(business_id: int) -> "dict | None":
    """The business's own email account for booking emails, or None if not set up."""
    with connect() as conn:
        row = conn.execute("SELECT name, mail_from, mail_password, mail_host, mail_port FROM businesses"
                           " WHERE id = ?", (business_id,)).fetchone()
    if not row or not (row["mail_from"] and row["mail_password"]):
        return None
    return {"host": row["mail_host"] or "smtp.gmail.com", "port": int(row["mail_port"] or 587),
            "user": row["mail_from"], "password": row["mail_password"], "name": row["name"]}


def _encode(fields: dict) -> dict:
    fields = {k: v for k, v in fields.items() if k in BUSINESS_FIELDS}
    if "open_days" in fields and not isinstance(fields["open_days"], str):
        fields["open_days"] = ",".join(str(int(d)) for d in sorted(fields["open_days"]))
    if "custom_questions" in fields and not isinstance(fields["custom_questions"], str):
        fields["custom_questions"] = json.dumps(normalize_questions(fields["custom_questions"]), ensure_ascii=False)
    if fields.get("whatsapp_session") == "":
        fields["whatsapp_session"] = None       # keep UNIQUE happy for businesses without WhatsApp yet
    return fields


def create_business(name: str, username: str, password: str, **fields) -> int:
    fields = _encode({**fields, "name": name, "username": username.strip().lower()})
    fields["password_hash"] = hash_password(password)
    fields["created_at"] = _now()
    cols = ", ".join(fields)
    with connect() as conn:
        cur = conn.execute(f"INSERT INTO businesses ({cols}) VALUES ({', '.join('?' * len(fields))})",
                           list(fields.values()))
        return cur.lastrowid


def update_business(business_id: int, **fields):
    fields = _encode(fields)
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE businesses SET {sets} WHERE id = ?", [*fields.values(), business_id])


def set_password(business_id: int, password: str):
    with connect() as conn:
        conn.execute("UPDATE businesses SET password_hash = ? WHERE id = ?", (hash_password(password), business_id))


def verify_password(business_id: int, password: str) -> bool:
    with connect() as conn:
        row = conn.execute("SELECT password_hash FROM businesses WHERE id = ?", (business_id,)).fetchone()
    return bool(row) and check_password(password or "", row["password_hash"])


def username_taken(username: str, except_id: int = None) -> bool:
    with connect() as conn:
        row = conn.execute("SELECT id FROM businesses WHERE username = ? AND id IS NOT ?",
                           ((username or "").strip().lower(), except_id)).fetchone()
    return row is not None


def get_business(business_id: int):
    with connect() as conn:
        return _business(conn.execute("SELECT * FROM businesses WHERE id = ?", (business_id,)).fetchone())


def get_business_by_session(session: "str | None"):
    """The active business whose WhatsApp is linked as this WAHA session (None if there is none)."""
    if not session:
        return None
    with connect() as conn:
        return _business(conn.execute("SELECT * FROM businesses WHERE whatsapp_session = ? AND active = 1",
                                      (str(session),)).fetchone())


def login(username: str, password: str):
    with connect() as conn:
        row = conn.execute("SELECT * FROM businesses WHERE username = ?",
                           ((username or "").strip().lower(),)).fetchone()
    if row and row["active"] and check_password(password, row["password_hash"]):
        return _business(row)
    return None


def list_businesses(active_only: bool = False) -> list:
    query = "SELECT * FROM businesses" + (" WHERE active = 1" if active_only else "") + " ORDER BY id"
    with connect() as conn:
        return [_business(r) for r in conn.execute(query).fetchall()]


# ------------------------------------------------------------ services

def list_services(business_id: int) -> list:
    with connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM services WHERE business_id = ? ORDER BY id", (business_id,)).fetchall()]
    for s in rows:                                   # services saved before capitalising was added
        s["name"] = s["name"][:1].upper() + s["name"][1:]
    return rows


def replace_services(business_id: int, services: list):
    """Save the full services list from the dashboard editor."""
    clean = []
    for s in services:
        name = " ".join(str(s.get("name") or "").split())
        if not name:
            continue
        name = name[0].upper() + name[1:]           # "beard" -> "Beard" (rest kept as the owner typed it)
        clean.append((business_id, name, int(s.get("price") or 0), max(5, int(s.get("duration_min") or 30))))
    with connect() as conn:
        conn.execute("DELETE FROM services WHERE business_id = ?", (business_id,))
        conn.executemany("INSERT INTO services (business_id, name, price, duration_min) VALUES (?, ?, ?, ?)", clean)


# ------------------------------------------------------------ bookings

def _booking(row):
    if row is None:
        return None
    b = dict(row)
    b["answers"] = json.loads(b["answers"] or "{}")
    return b


def create_booking(business_id: int, phone: str, customer_name: str, service: str, date: str, time: str,
                   duration_min: int, event_id: str, answers: dict = None, customer_email: "str | None" = None) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO bookings (business_id, phone, customer_name, service, date, time, duration_min,"
            " event_id, status, answers, customer_email, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,'booked',?,?,?,?)",
            (business_id, phone, customer_name, service, date, time, duration_min, event_id,
             json.dumps(answers or {}, ensure_ascii=False), customer_email or None, _now(), _now()))
        return cur.lastrowid


def get_booking(booking_id: int):
    with connect() as conn:
        return _booking(conn.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone())


def upcoming_booking(business_id: int, phone: str):
    """The customer's next active booking (today or later)."""
    today = today_pkt().isoformat()
    with connect() as conn:
        return _booking(conn.execute(
            "SELECT * FROM bookings WHERE business_id = ? AND phone = ? AND status IN ('booked','confirmed')"
            " AND date >= ? ORDER BY date, time LIMIT 1", (business_id, phone, today)).fetchone())


def upcoming_bookings(business_id: int, phone: str) -> list:
    """All of the customer's active bookings from today on, soonest first."""
    today = today_pkt().isoformat()
    with connect() as conn:
        return [_booking(r) for r in conn.execute(
            "SELECT * FROM bookings WHERE business_id = ? AND phone = ? AND status IN ('booked','confirmed')"
            " AND date >= ? ORDER BY date, time", (business_id, phone, today)).fetchall()]


def last_customer_name(business_id: int, phone: str):
    """Name the customer used on their most recent booking (returning customers aren't asked again)."""
    with connect() as conn:
        row = conn.execute("SELECT customer_name FROM bookings WHERE business_id = ? AND phone = ?"
                           " AND customer_name IS NOT NULL ORDER BY id DESC LIMIT 1", (business_id, phone)).fetchone()
    return row["customer_name"] if row else None


def last_customer_email(business_id: int, phone: str):
    """Email the customer gave on an earlier booking (returning customers aren't asked again)."""
    with connect() as conn:
        row = conn.execute("SELECT customer_email FROM bookings WHERE business_id = ? AND phone = ?"
                           " AND customer_email IS NOT NULL AND customer_email != '' ORDER BY id DESC LIMIT 1",
                           (business_id, phone)).fetchone()
    return row["customer_email"] if row else None


def all_upcoming_bookings() -> list:
    """Every active booking from today on (used to restore reminders after a restart)."""
    today = today_pkt().isoformat()
    with connect() as conn:
        return [_booking(r) for r in conn.execute(
            "SELECT * FROM bookings WHERE status IN ('booked','confirmed') AND date >= ? ORDER BY date, time",
            (today,)).fetchall()]


def set_booking_status(booking_id: int, status: str):
    with connect() as conn:
        conn.execute("UPDATE bookings SET status = ?, updated_at = ? WHERE id = ?", (status, _now(), booking_id))


def list_bookings(business_id: int, from_date: str = None, statuses=("booked", "confirmed"),
                  to_date: str = None) -> list:
    """Bookings of one business, soonest first. statuses=None returns every status."""
    query = "SELECT * FROM bookings WHERE business_id = ?"
    params = [business_id]
    if from_date:
        query += " AND date >= ?"
        params.append(from_date)
    if to_date:
        query += " AND date <= ?"
        params.append(to_date)
    if statuses:
        query += f" AND status IN ({', '.join('?' * len(statuses))})"
        params.extend(statuses)
    with connect() as conn:
        return [_booking(r) for r in conn.execute(query + " ORDER BY date, time", params).fetchall()]


def customer_bookings(business_id: int, phone: str) -> list:
    """Every booking of one customer (any status), newest appointment first."""
    with connect() as conn:
        return [_booking(r) for r in conn.execute(
            "SELECT * FROM bookings WHERE business_id = ? AND phone = ? ORDER BY date DESC, time DESC, id DESC",
            (business_id, phone)).fetchall()]


def chat_days(business_id: int, phone: str) -> list:
    """[(YYYY-MM-DD, messages that day)] of one customer's chat, newest day first."""
    with connect() as conn:
        return [(r["day"], r["n"]) for r in conn.execute(
            "SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS n FROM messages WHERE business_id = ? AND phone = ?"
            " GROUP BY day ORDER BY day DESC", (business_id, phone))]


def bookings_made(business_id: int, since: str) -> int:
    """Bookings created since a date-time (any status) — 'bookings completed by AURA'."""
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM bookings WHERE business_id = ? AND created_at >= ?",
                            (business_id, since)).fetchone()[0]


def booking_summary() -> dict:
    """{business_id: {"upcoming": n, "total": n}} for the admin overview."""
    today = today_pkt().isoformat()
    with connect() as conn:
        rows = conn.execute(
            "SELECT business_id, COUNT(*) AS total,"
            " SUM(status IN ('booked','confirmed') AND date >= ?) AS upcoming"
            " FROM bookings GROUP BY business_id", (today,)).fetchall()
    return {r["business_id"]: {"total": r["total"], "upcoming": r["upcoming"] or 0} for r in rows}


# ------------------------------------------------------------ conversation backup

def log_message(business_id: int, phone: str, direction: str, text: str, kind: str = "chat",
                booking_id: "int | None" = None, delivered: "bool | None" = None) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO messages (business_id, phone, direction, kind, text, booking_id, delivered, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (business_id, phone, direction, kind, text, booking_id,
             None if delivered is None else int(bool(delivered)), _now()))
        return cur.lastrowid


def list_messages(business_id: int, phone: str, since: "str | None" = None, until: "str | None" = None,
                  limit: int = 500) -> list:
    """One customer's messages, oldest first (since/until: ISO date-times, local time)."""
    query, params = "SELECT * FROM messages WHERE business_id = ? AND phone = ?", [business_id, phone]
    if since:
        query += " AND created_at >= ?"
        params.append(since)
    if until:
        query += " AND created_at <= ?"
        params.append(until)
    with connect() as conn:
        rows = conn.execute(query + " ORDER BY id DESC LIMIT ?", [*params, limit]).fetchall()
    return [dict(r) for r in reversed(rows)]


def delete_conversation(business_id: int, phone: str) -> int:
    """Delete the saved chat with one customer (messages and their WhatsApp name).
    Bookings and escalation records are kept. Returns how many messages were deleted."""
    with connect() as conn:
        deleted = conn.execute("DELETE FROM messages WHERE business_id = ? AND phone = ?",
                               (business_id, phone)).rowcount
        conn.execute("DELETE FROM contacts WHERE business_id = ? AND phone = ?", (business_id, phone))
    return deleted


def save_profile_name(business_id: int, phone: str, profile_name: str):
    if not profile_name:
        return
    with connect() as conn:
        conn.execute("INSERT INTO contacts (business_id, phone, profile_name, updated_at) VALUES (?, ?, ?, ?)"
                     " ON CONFLICT(business_id, phone) DO UPDATE SET profile_name = excluded.profile_name,"
                     " updated_at = excluded.updated_at", (business_id, phone, profile_name, _now()))


def customer_display_name(business_id: int, phone: str):
    """Name for showing the customer: their booking name, else their WhatsApp profile name, else None."""
    name = last_customer_name(business_id, phone)
    if name:
        return name
    with connect() as conn:
        row = conn.execute("SELECT profile_name FROM contacts WHERE business_id = ? AND phone = ?",
                           (business_id, phone)).fetchone()
    return row["profile_name"] if row else None


def list_conversations(business_id: int, since: "str | None" = None) -> list:
    """One row per customer: last message time, message counts, and a name (latest booking name,
    else the customer's WhatsApp profile name)."""
    query = ("SELECT m.phone, MAX(m.created_at) AS last_at, COUNT(*) AS messages,"
             " SUM(m.direction = 'in') AS from_customer,"
             " COALESCE((SELECT customer_name FROM bookings b WHERE b.business_id = m.business_id AND b.phone = m.phone"
             "  AND customer_name IS NOT NULL ORDER BY b.id DESC LIMIT 1),"
             " (SELECT profile_name FROM contacts c WHERE c.business_id = m.business_id AND c.phone = m.phone)) AS name,"
             " (SELECT COUNT(*) FROM bookings k WHERE k.business_id = m.business_id AND k.phone = m.phone) AS bookings"
             " FROM messages m WHERE m.business_id = ?")
    params = [business_id]
    if since:
        query += " AND m.created_at >= ?"
        params.append(since)
    with connect() as conn:
        return [dict(r) for r in conn.execute(query + " GROUP BY m.phone ORDER BY last_at DESC", params)]


def message_stats(business_id: int, since: str) -> dict:
    """Counts for the dashboard metrics."""
    with connect() as conn:
        row = conn.execute(
            "SELECT SUM(direction = 'in') AS received, SUM(direction = 'out') AS sent,"
            " COUNT(DISTINCT CASE WHEN direction = 'in' THEN phone END) AS customers"
            " FROM messages WHERE business_id = ? AND created_at >= ?", (business_id, since)).fetchone()
    return {k: row[k] or 0 for k in ("received", "sent", "customers")}


def escalated_customers(business_id: int, since: str) -> int:
    """How many of the customers who messaged since `since` were handed to the owner at least once
    (each customer counts once, however many alerts they caused), for the escalation rate."""
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(DISTINCT e.phone) AS n FROM escalations e WHERE e.business_id = ? AND e.created_at >= ?"
            " AND e.phone IN (SELECT phone FROM messages WHERE business_id = ? AND direction = 'in'"
            " AND created_at >= ?)", (business_id, since, business_id, since)).fetchone()
    return row["n"] or 0


# ------------------------------------------------------------ escalations

def create_escalation(business_id: int, phone: str, kind: str, reason: str, customer_name: "str | None" = None,
                      booking_id: "int | None" = None, summary: str = "") -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO escalations (business_id, phone, customer_name, kind, reason, booking_id, summary,"
            " status, created_at) VALUES (?,?,?,?,?,?,?,'open',?)",
            (business_id, phone, customer_name, kind, reason, booking_id, summary, _now()))
        return cur.lastrowid


def set_escalation_notified(escalation_id: int, whatsapp: bool, email: bool):
    with connect() as conn:
        conn.execute("UPDATE escalations SET notified_whatsapp = ?, notified_email = ? WHERE id = ?",
                     (int(bool(whatsapp)), int(bool(email)), escalation_id))


def recent_open_escalation(business_id: int, phone: str, kind: str, hours: int = 2):
    """An open escalation of this kind for this customer in the last `hours` (avoids repeat alerts)."""
    since = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
    with connect() as conn:
        row = conn.execute("SELECT * FROM escalations WHERE business_id = ? AND phone = ? AND kind = ?"
                           " AND status = 'open' AND created_at >= ? ORDER BY id DESC LIMIT 1",
                           (business_id, phone, kind, since)).fetchone()
    return dict(row) if row else None


def get_escalation(escalation_id: int):
    with connect() as conn:
        row = conn.execute("SELECT * FROM escalations WHERE id = ?", (escalation_id,)).fetchone()
    return dict(row) if row else None


def list_escalations(business_id: int, status: "str | None" = None, since: "str | None" = None) -> list:
    query, params = "SELECT * FROM escalations WHERE business_id = ?", [business_id]
    if status:
        query += " AND status = ?"
        params.append(status)
    if since:
        query += " AND created_at >= ?"
        params.append(since)
    with connect() as conn:
        return [dict(r) for r in conn.execute(query + " ORDER BY id DESC", params)]


def resolve_escalation(business_id: int, escalation_id: int) -> bool:
    with connect() as conn:
        cur = conn.execute("UPDATE escalations SET status = 'resolved', resolved_at = ?"
                           " WHERE id = ? AND business_id = ? AND status = 'open'",
                           (_now(), escalation_id, business_id))
        return cur.rowcount == 1


# ------------------------------------------------------------ platform settings (see config.py)

def get_settings() -> dict:
    with connect() as conn:
        return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}


def setting_once(key: str, value: str) -> str:
    """Save value under key unless the key already exists; return the stored value. Safe when two
    processes (server and dashboard) call it at the same moment: both get the same value."""
    with connect() as conn:
        conn.execute("INSERT OR IGNORE INTO settings (key, value, updated_at) VALUES (?, ?, ?)", (key, value, _now()))
        return conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()["value"]


def save_settings(values: dict):
    """Insert or update; a value of None deletes the setting."""
    with connect() as conn:
        for key, value in values.items():
            if value is None:
                conn.execute("DELETE FROM settings WHERE key = ?", (key,))
            else:
                conn.execute("INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)"
                             " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                             (key, str(value), _now()))


init_db()
