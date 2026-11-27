"""Actions the owner takes from the dashboard (no Streamlit code here).

Reminder jobs run inside the WhatsApp server, not the dashboard. They re-check the booking in
the database before sending, so a booking cancelled here never gets a reminder.
"""
from app.services import config, customer_mail, db, whatsapp
from app.services.calendar import cancel_appointment, get_busy
from app.services.datetime_parser import describe, today_pkt
from app.services.messaging import send_to_customer

ACTIVE = ("booked", "confirmed")


def display_phone(phone: str) -> str:
    """'923001234567' -> '0300 1234567'. Other IDs (WhatsApp privacy IDs) are shown as they are."""
    digits = str(phone or "")
    if len(digits) == 12 and digits.startswith("923"):
        return f"0{digits[2:5]} {digits[5:]}"
    return digits


def confirm_booking(business: dict, booking_id: int):
    """Mark a booking as confirmed (e.g. the customer called instead of replying). Returns error or None."""
    booking = db.get_booking(booking_id)
    if not booking or booking["business_id"] != business["id"]:
        return "Booking not found."
    if booking["status"] != "booked":
        return f"This booking is already {booking['status']}."
    db.set_booking_status(booking_id, "confirmed")
    return None


def cancel_message(business: dict, booking: dict) -> str:
    """The WhatsApp message the customer gets when the owner cancels (editable on the dashboard)."""
    return (f"Assalamualaikum {booking['customer_name'] or ''}! {business['name']} ki taraf se: aap ki "
            f"{booking['service']} ki booking ({describe(booking['date'], booking['time'])}) cancel kar di gayi hai. "
            f"Nayi booking ke liye isi number par message kar dein.").replace("  ", " ")


def cancel_booking(business: dict, booking_id: int, notify_customer: bool, message: "str | None" = None) -> dict:
    """Cancel from the dashboard: calendar event, database, optional WhatsApp message (message: the owner's
    own text; default cancel_message()).

    Returns {"error": str|None, "calendar": bool, "notified": bool|None}.
    """
    booking = db.get_booking(booking_id)
    if not booking or booking["business_id"] != business["id"]:
        return {"error": "Booking not found.", "calendar": False, "notified": None}
    if booking["status"] not in ACTIVE:
        return {"error": f"This booking is already {booking['status']}.", "calendar": False, "notified": None}

    calendar_ok = True
    if booking.get("event_id"):
        calendar_ok = cancel_appointment(booking["event_id"], business.get("calendar_id"))
    db.set_booking_status(booking_id, "cancelled")
    customer_mail.cancelled(business, booking)          # if they gave an email and the business sends emails

    notified = None
    if notify_customer:
        text = (message or "").strip() or cancel_message(business, booking)
        notified = send_to_customer(business, booking["phone"], text, kind="notice", booking_id=booking_id)
    return {"error": None, "calendar": calendar_ok, "notified": notified}


def delete_conversation(business: dict, phone: str) -> int:
    """Delete a customer's saved chat and forget any conversation AURA has in progress with them,
    so their next message starts fresh. Bookings and escalations are kept. Returns messages deleted."""
    deleted = db.delete_conversation(business["id"], phone)
    try:
        from app.services.state import clear_state, r
        clear_state(business["id"], phone)
        r.delete(f"lastreply:{business['id']}:{phone}")      # agent's loop guard starts fresh too
    except Exception as e:                       # Redis down: the chat is deleted anyway
        print(f"[Dashboard] Could not reset the conversation state: {e}")
    return deleted


def check_calendar(business: dict):
    """(ok, message) — can AURA read this business's Google Calendar?"""
    calendar = business.get("calendar_id")
    if not calendar:
        return False, "No calendar set for this business."
    robot = config.service_account_email(config.get("google_service_account"))
    if not robot:
        return False, "No Google service account saved yet (Admin → Platform settings)."
    busy = get_busy(today_pkt().isoformat(), calendar)
    if busy is None:
        return False, (f"Can't read calendar {calendar}. In Google Calendar, share it with {robot} "
                       "(Settings and sharing → Make changes to events).")
    return True, f"Calendar {calendar} is reachable ({len(busy)} busy period(s) today)."


def check_whatsapp(business: dict):
    """(ok, message) — is this business's WhatsApp linked and connected?"""
    session = business.get("whatsapp_session")
    if not session:
        return False, "No WhatsApp linked yet: Settings → WhatsApp → Connect, then scan the QR code."
    info = whatsapp.session_info(session)
    status = info["status"]
    if status == whatsapp.CONNECTED:
        number = display_phone(info.get("phone") or "")
        return True, "WhatsApp connected" + (f": {number}." if number else ".")
    if status == whatsapp.UNREACHABLE:
        return False, f"The WhatsApp server (WAHA) is not reachable: {info.get('error', '')[:150]}"
    if status in whatsapp.WAITING:
        return False, "WhatsApp is waiting for the QR code to be scanned (Settings → WhatsApp)."
    return False, f"WhatsApp is not connected ({status}). Settings → WhatsApp → Reconnect."
