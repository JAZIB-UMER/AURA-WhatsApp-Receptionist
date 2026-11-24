"""Reminders, confirmations and owner escalation for bookings.

Schedule (Pakistan time, never between 9 PM and 9 AM):
  booked more than 24 h ahead -> reminder 24 h before; follow-up 12 h before if no reply;
                                  owner alert 3 h before if the customer still hasn't confirmed
  booked less than 24 h ahead -> one reminder 2 h before (no follow-up / owner alert: they just booked)

Bookings live in SQLite (db.py). Jobs only store the booking id and re-check the
booking (and its Google Calendar event) when they run, so a cancelled, moved or
manually deleted appointment never gets a stale message. Jobs are rebuilt from the
database when the server starts.
"""
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from app.services import customer_mail, db
from app.services.calendar import cancel_appointment, event_exists
from app.services.datetime_parser import PKT, describe
from app.services.escalation import escalate
from app.services.state import r
from app.services.messaging import send_to_customer

QUIET_FROM, QUIET_UNTIL = 21, 9      # no messages from 9 PM to 9 AM
LATEST_SEND = (20, 30)               # a message that would fall in quiet hours moves to 8:30 PM before it

scheduler = BackgroundScheduler(timezone=PKT)
scheduler.start()

JOB_TYPES = ["reminder", "followup", "escalation"]
ACTIVE = ("booked", "confirmed")


def _send(business: dict, booking: dict, message: str, kind: str):
    send_to_customer(business, booking["phone"], message, kind=kind, booking_id=booking["id"])


def _remove_job(job_id: str):
    try:
        scheduler.remove_job(job_id)
    except Exception:
        pass  # job already ran or never existed


def booking_line(booking: dict) -> str:
    """'Massage — Friday 13 Nov, 11:00 AM (Hassan)'"""
    return f"{booking['service']} — {describe(booking['date'], booking['time'])} ({booking['customer_name']})"


def _appointment_time(booking: dict) -> datetime:
    return datetime.strptime(f"{booking['date']} {booking['time']}", "%Y-%m-%d %H:%M").replace(tzinfo=PKT)


def _awake(t: datetime) -> datetime:
    """Move a send time out of quiet hours, to 8:30 PM before it."""
    if t.hour >= QUIET_FROM:
        return t.replace(hour=LATEST_SEND[0], minute=LATEST_SEND[1], second=0, microsecond=0)
    if t.hour < QUIET_UNTIL:
        return (t - timedelta(days=1)).replace(hour=LATEST_SEND[0], minute=LATEST_SEND[1], second=0, microsecond=0)
    return t


def _load(booking_id: int):
    """(booking, business) if the booking is still active AND still on the calendar, else (None, None)."""
    booking = db.get_booking(booking_id)
    if not booking or booking["status"] not in ACTIVE:
        return None, None
    business = db.get_business(booking["business_id"])
    if booking.get("event_id") and event_exists(booking["event_id"], business.get("calendar_id")) is False:
        # The owner deleted the appointment directly in Google Calendar.
        db.set_booking_status(booking_id, "cancelled")
        cancel_reminders(booking_id)
        print(f"[Reminder] Booking {booking_id} was deleted from the calendar — marked cancelled")
        return None, None
    return booking, business


def send_reminder(booking_id: int):
    booking, business = _load(booking_id)
    if not booking or booking["status"] == "confirmed":
        return
    _send(business, booking, (
        f"⏰ *Reminder:* {business['name']} mein aap ki appointment hai.\n"
        f"• {booking_line(booking)}\n\n"
        f"Aa rahe hain? Reply *haan* ya *cancel*."
    ), "reminder")
    customer_mail.reminder(business, booking)
    r.setex(f"reminder_sent:{booking_id}", 86400 * 2, "1")   # "haan"/"cancel" now count as an answer
    r.setex(f"reminded:{booking_id}", 86400 * 7, "1")        # lets follow-up / escalation run
    print(f"[Reminder] Sent for booking {booking_id}")


def send_followup(booking_id: int):
    booking, business = _load(booking_id)
    if not booking or booking["status"] == "confirmed" or not r.get(f"reminded:{booking_id}"):
        return
    _send(business, booking, (
        f"*Aap ne abhi tak confirm nahi kiya:*\n• {booking_line(booking)}\n\n"
        f"Appointment rakhni hai? Reply *haan* ya *cancel*."
    ), "followup")
    r.setex(f"reminder_sent:{booking_id}", 86400 * 2, "1")
    print(f"[Followup] Sent for booking {booking_id}")


def escalate_unconfirmed(booking_id: int):
    booking, business = _load(booking_id)
    if booking and booking["status"] == "booked" and r.get(f"reminded:{booking_id}"):
        escalate(business, booking["phone"], "unconfirmed", "no reply to the reminder or follow-up", booking)


def _daytime(t: datetime, not_after: datetime) -> datetime:
    """Move t out of quiet hours: 9 AM the same morning if that is still <= not_after, else 8:30 PM before."""
    if t.hour < QUIET_UNTIL:
        morning = t.replace(hour=QUIET_UNTIL, minute=0, second=0, microsecond=0)
        if morning <= not_after:
            return morning
    return _awake(t)


def plan_reminders(appointment: datetime, now: datetime) -> list:
    """[(job_type, send_time)] for an appointment — pure, so it can be tested."""
    lead = appointment - now
    jobs = []
    if lead > timedelta(hours=24):
        # 24 h reminder -> 12 h follow-up -> owner alert 3 h before (each at least 2 h apart)
        reminder = _awake(appointment - timedelta(hours=24))
        escalation = _daytime(appointment - timedelta(hours=3), not_after=appointment - timedelta(hours=1))
        followup = _daytime(appointment - timedelta(hours=12), not_after=escalation - timedelta(hours=2))
        jobs.append(("reminder", reminder))
        if reminder + timedelta(hours=2) <= followup <= escalation - timedelta(hours=2):
            jobs.append(("followup", followup))
        if escalation >= reminder + timedelta(hours=2):
            jobs.append(("escalation", escalation))
    elif lead > timedelta(hours=3):
        wanted = appointment - timedelta(hours=2)
        send = _awake(wanted)
        if send <= now and wanted.hour < QUIET_UNTIL:
            # e.g. booked at 10 PM for 10 AM: the evening is gone, so remind at 9 AM instead
            morning = wanted.replace(hour=QUIET_UNTIL, minute=0, second=0, microsecond=0)
            if appointment - morning >= timedelta(hours=1):
                send = morning
        jobs.append(("reminder", send))
    return [(kind, t) for kind, t in jobs if now < t < appointment]


def schedule_reminders(booking_id: int):
    booking = db.get_booking(booking_id)
    if not booking or booking["status"] not in ACTIVE:
        return
    funcs = {"reminder": send_reminder, "followup": send_followup, "escalation": escalate_unconfirmed}
    for kind, run_at in plan_reminders(_appointment_time(booking), datetime.now(PKT)):
        scheduler.add_job(funcs[kind], 'date', run_date=run_at, args=[booking_id],
                          id=f"{kind}_{booking_id}", replace_existing=True, misfire_grace_time=3600)
        print(f"[{kind.title()}] Booking {booking_id} -> {run_at.strftime('%a %d %b %I:%M %p')}")


def restore_jobs():
    """Rebuild reminder jobs from the database (call on server start)."""
    bookings = db.all_upcoming_bookings()
    for b in bookings:
        schedule_reminders(b["id"])
    print(f"[Reminder] Restored reminders for {len(bookings)} upcoming booking(s)")


def cancel_reminders(booking_id: int):
    for name in JOB_TYPES:
        _remove_job(f"{name}_{booking_id}")
    r.delete(f"reminder_sent:{booking_id}", f"reminded:{booking_id}")


def cancel_booking_by_id(business: dict, booking_id: int) -> "dict | None":
    """Cancel one booking: free the calendar slot and stop its reminders.
    Returns the booking, or None if it wasn't active (already cancelled / moved)."""
    booking = db.get_booking(booking_id)
    if not booking or booking["status"] not in ACTIVE:
        return None
    if booking.get("event_id"):
        cancel_appointment(booking["event_id"], business.get("calendar_id"))
    db.set_booking_status(booking_id, "cancelled")
    cancel_reminders(booking_id)
    customer_mail.cancelled(business, booking)
    return booking


CONFIRM_WORDS = {"haan", "han", "haa", "ha", "yes", "ok", "okay", "theek", "thik", "confirm", "ji", "jee", "bilkul",
                 "zaroor", "inshallah", "aaunga", "aaungi", "aunga", "aungi"}
CANCEL_WORDS = {"cancel", "nahi", "nai", "nhi", "no", "band"}


def handle_confirmation(business: dict, phone: str, message: str):
    """Answer to a reminder ('haan' / 'cancel'); None if this message isn't one."""
    for booking in db.upcoming_bookings(business["id"], phone):
        # Only treat "haan" / "nahi" as an answer once a reminder has actually asked.
        if not r.get(f"reminder_sent:{booking['id']}"):
            continue
        words = set(message.lower().replace(",", " ").replace(".", " ").replace("!", " ").split())
        # Check cancel first so "nahi aa sakta" is never read as a yes.
        if words & CANCEL_WORDS:
            cancel_booking_by_id(business, booking["id"])
            return (f"*Appointment cancel kar di gayi:*\n• {booking_line(booking)}\n\n"
                    f"Dobara book karni ho toh batayein!")
        if words & CONFIRM_WORDS:
            db.set_booking_status(booking["id"], "confirmed")
            r.delete(f"reminder_sent:{booking['id']}")
            _remove_job(f"followup_{booking['id']}")
            _remove_job(f"escalation_{booking['id']}")
            return f"✅ *Shukriya! Appointment confirm ho gayi.*\n• {booking_line(booking)}\n\nMilte hain!"
        return None
    return None
