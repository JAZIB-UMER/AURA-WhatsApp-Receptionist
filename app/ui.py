"""Shared helpers for the dashboard pages (Dashboard.py + app_pages/)."""
import re
import time
from datetime import date, datetime

import pandas as pd
import streamlit as st

from app.services import config, db
from app.services.manage import display_phone


def check_calendar_after_save(business: dict):
    """Test the business's calendar now; a failure stays as a warning on its Settings page until a
    later check succeeds. Returns (ok, message)."""
    from app.services.manage import check_calendar
    ok, message = check_calendar(business)
    key = f"calendar_warning_{business['id']}"
    if ok:
        st.session_state.pop(key, None)
    else:
        st.session_state[key] = message
    return ok, message


def calendar_warning(business_id: int):
    return st.session_state.get(f"calendar_warning_{business_id}")


def calendar_share_help() -> str:
    """Help text for a calendar field: which email the business must share its calendar with."""
    robot = config.service_account_email(config.get("google_service_account"))
    if robot:
        return f"In Google Calendar, share this calendar with {robot} (permission: Make changes to events)."
    return "Upload the Google service account in Platform settings first, then share this calendar with its email."

STATUS_LABELS = {"booked": "Awaiting reply", "confirmed": "Confirmed", "cancelled": "Cancelled",
                 "rescheduled": "Rescheduled"}
STATUS_COLORS = ["orange", "green", "red", "gray"]          # same order as STATUS_LABELS

MAX_ATTEMPTS, LOCK_SECONDS = 5, 300


# ------------------------------------------------------------------ login throttling

@st.cache_resource
def _attempts() -> dict:
    """Failed logins per username, shared by all sessions of this dashboard process."""
    return {}


def login_locked(username: str) -> int:
    """Seconds left on a lock-out (0 if not locked)."""
    count, since = _attempts().get(username.lower(), (0, 0.0))
    left = int(since + LOCK_SECONDS - time.time())
    return left if count >= MAX_ATTEMPTS and left > 0 else 0


def record_login(username: str, ok: bool):
    attempts = _attempts()
    key = username.lower()
    if ok:
        attempts.pop(key, None)
        return
    count, since = attempts.get(key, (0, time.time()))
    if time.time() - since > LOCK_SECONDS:
        count, since = 0, time.time()
    attempts[key] = (count + 1, since)


# ------------------------------------------------------------------ remember the login across refreshes
# A refresh starts a new Streamlit session, so the login is kept in a cookie holding a random
# token. Only its SHA-256 is stored (db.dashboard_sessions); logging out deletes it server-side.

COOKIE = "aura_session"
SESSION_HOURS = 12


def cookie_token() -> str:
    """Token from the browser cookie sent with this page load ('' if none)."""
    try:
        value = st.context.cookies.get(COOKIE)
    except Exception:                       # no browser request (tests, bare mode)
        return ""
    return value if isinstance(value, str) else ""


def _cookie_script(value: str, max_age: int):
    # SameSite=Strict: the cookie is never sent from other sites. No HttpOnly: it is set from the page.
    st.html(f"<script>document.cookie = '{COOKIE}={value}; path=/; max-age={max_age}; SameSite=Strict';</script>",
            unsafe_allow_javascript=True, width="content")


def start_session(session: dict):
    """Log in: keep the user in session state and remember them in the browser."""
    token = db.create_session(session["role"], session.get("business_id"), hours=SESSION_HOURS)
    st.session_state.clear()
    st.session_state.user = session
    st.session_state.session_token = token
    st.session_state.write_cookie = True


def restore_session() -> bool:
    """After a refresh: rebuild the login from the cookie. True if logged in again."""
    token = cookie_token()
    saved = db.get_session(token)
    if not saved:
        return False
    if saved["role"] == "owner":
        business = db.get_business(saved["business_id"])
        if not business or not business.get("active"):
            db.delete_session(token)
            return False
        st.session_state.user = {"role": "owner", "business_id": business["id"], "name": business["name"]}
    elif saved["role"] == "admin":
        st.session_state.user = {"role": "admin"}
    else:
        return False
    st.session_state.session_token = token
    return True


def end_session():
    """Log out (button callback): forget the token everywhere and clear the cookie."""
    db.delete_session(st.session_state.get("session_token") or cookie_token())
    st.session_state.clear()
    st.session_state.clear_cookie = True


def sync_cookie():
    """Write or clear the browser cookie when a login/logout just happened."""
    if st.session_state.pop("write_cookie", False):
        _cookie_script(st.session_state.session_token, SESSION_HOURS * 3600)
    if st.session_state.pop("clear_cookie", False):
        _cookie_script("", 0)


# ------------------------------------------------------------------ session

def user() -> dict:
    return st.session_state.get("user") or {}


def is_admin() -> bool:
    return user().get("role") == "admin"


def flash(message: str, icon: str = ":material/check_circle:"):
    """Show a toast after the next rerun (a toast shown right before st.rerun() would be lost)."""
    st.session_state["_flash"] = (message, icon)


def show_flash():
    if "_flash" in st.session_state:
        message, icon = st.session_state.pop("_flash")
        st.toast(message, icon=icon)


def business_id():
    return st.session_state.get("business_id") if is_admin() else user().get("business_id")


def current_business() -> dict:
    """The business being managed, fresh from the database; stops the page if there is none."""
    bid = business_id()
    business = db.get_business(bid) if bid else None
    if not business:
        st.info("Add a business on the **Businesses** page first." if is_admin()
                else "Your business could not be found. Please log in again.",
                icon=":material/info:")
        st.stop()
    return business


# ------------------------------------------------------------------ data

def price_lookup(business_id: int) -> dict:
    return {s["name"].lower(): s["price"] for s in db.list_services(business_id)}


def bookings_frame(bookings: list, prices: dict) -> pd.DataFrame:
    """Bookings as a typed DataFrame (also correct when empty)."""
    columns = {
        "id": pd.Series(dtype="int64"), "when": pd.Series(dtype="datetime64[ns]"),
        "date": pd.Series(dtype="object"), "customer": pd.Series(dtype="string"),
        "phone": pd.Series(dtype="string"), "service": pd.Series(dtype="string"),
        "minutes": pd.Series(dtype="int64"), "price": pd.Series(dtype="float"),
        "status": pd.Series(dtype="object"), "status_key": pd.Series(dtype="string"),
        "details": pd.Series(dtype="string"),
    }
    if not bookings:
        return pd.DataFrame(columns)
    rows = []
    for b in bookings:
        rows.append({
            "id": b["id"],
            "when": datetime.strptime(f"{b['date']} {b['time']}", "%Y-%m-%d %H:%M"),
            "date": date.fromisoformat(b["date"]),
            "customer": b.get("customer_name") or "—",
            "phone": display_phone(b["phone"]),
            "service": b.get("service") or "—",
            "minutes": int(b.get("duration_min") or 0),
            "price": prices.get((b.get("service") or "").lower()),
            "status": [STATUS_LABELS.get(b["status"], b["status"])],
            "status_key": b["status"],
            "details": "; ".join(f"{k} {v}" for k, v in (b.get("answers") or {}).items() if v),
        })
    return pd.DataFrame(rows).astype({"price": "float"})


def status_column(label: str = "Status"):
    return st.column_config.MultiselectColumn(label, options=list(STATUS_LABELS.values()),
                                              color=STATUS_COLORS, width=120)


KIND_LABELS = {"reminder": "Reminder", "followup": "Follow-up", "notice": "Sent from dashboard"}
ESCALATION_LABELS = {"human_request": ("Wants a person", "red"), "not_understood": ("Not understood", "orange"),
                     "unconfirmed": ("Didn't confirm", "violet"),
                     "calendar_unavailable": ("Calendar problem", "blue")}


def show_conversation(business_id: int, phone: str, since: "str | None" = None, until: "str | None" = None,
                      name: "str | None" = None, mark_at: "str | None" = None):
    """The saved WhatsApp chat with one customer (customer left, AURA right), oldest first.

    mark_at: an ISO time (e.g. when a booking was made) shown as a divider in the chat.
    """
    messages = db.list_messages(business_id, phone, since=since, until=until)
    if not messages:
        st.caption("No saved messages for this period. (Messages are saved from the moment this feature went live.)")
        return
    day, marked = None, mark_at is None
    for m in messages:
        stamp = datetime.fromisoformat(m["created_at"])
        if mark_at and not marked and m["created_at"] >= mark_at:
            st.caption(f":material/event_available: Booking made · {clock(datetime.fromisoformat(mark_at))}")
            marked = True
        if stamp.date() != day:
            day = stamp.date()
            st.caption(f"**{stamp:%A %d %b %Y}**", text_alignment="center")
        is_customer = m["direction"] == "in"
        with st.chat_message("user" if is_customer else "assistant",
                             avatar=":material/person:" if is_customer else ":material/support_agent:"):
            st.markdown(whatsapp_markdown(m["text"]))
            meta = f"{name or 'Customer'} · " if is_customer else "AURA · "
            meta += clock(stamp)
            if KIND_LABELS.get(m["kind"]):
                meta += f" · {KIND_LABELS[m['kind']]}"
            if m["delivered"] == 0:
                meta += " · :red[not delivered]"
            st.caption(meta)


_WA_BOLD = re.compile(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])")
_WA_STRIKE = re.compile(r"(?<![\w~])~(?=\S)([^~\n]+?)(?<=\S)~(?![\w~])")


def whatsapp_markdown(text: str) -> str:
    """Show a WhatsApp message on the dashboard the way the customer sees it: *bold* -> **bold**,
    ~strike~ -> ~~strike~~, _italic_ stays, and every line break is kept."""
    text = _WA_BOLD.sub(r"**\1**", text or "")
    text = _WA_STRIKE.sub(r"~~\1~~", text)
    return text.replace("\n", "  \n")


def clock(moment) -> str:
    """'2:00 PM' (no leading zero)."""
    return moment.strftime("%I:%M %p").lstrip("0")


def money(value) -> str:
    return f"Rs {int(round(value or 0)):,}"
