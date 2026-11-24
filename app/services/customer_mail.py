"""Booking emails to customers, sent from the business's own email address (its Settings page).

Sent only when the business has set up its email and the customer gave an email address (AURA asks for
it as an optional question, or the owner's own questions include an email question):
  booking confirmed / moved     when AURA books or reschedules on WhatsApp
  reminder                      together with the WhatsApp reminder
  cancelled                     by the customer on WhatsApp, or by the owner on the dashboard

Emails go out in the background, so a slow mail server never delays a WhatsApp reply.
"""
import threading
from datetime import datetime
from email.utils import formataddr

from app.services import db
from app.services.email_alert import send_email

EMAIL_QUESTION = {"question": "Confirmation email ke liye apna email likh dein",
                  "type": "email", "required": False}


def customer_email(business: dict, booking: dict) -> str:
    """The address to write to: the one AURA asked for, else an answer to the owner's email question."""
    if booking.get("customer_email"):
        return booking["customer_email"]
    email_questions = {q["question"] for q in business.get("custom_questions") or [] if q["type"] == "email"}
    return next((a for q, a in (booking.get("answers") or {}).items() if q in email_questions and a), "")


def _when(booking: dict) -> str:
    """'Tuesday, 26 November 2026 at 3:00 PM'"""
    moment = datetime.strptime(f"{booking['date']} {booking['time']}", "%Y-%m-%d %H:%M")
    return f"{moment:%A, %d %B %Y} at {moment:%I:%M %p}".replace(" 0", " ")


def _details(business: dict, booking: dict) -> str:
    price = next((s["price"] for s in db.list_services(business["id"])
                  if s["name"].lower() == (booking.get("service") or "").lower() and s.get("price")), None)
    lines = [f"  Service:   {booking['service']}", f"  When:      {_when(booking)}",
             f"  Duration:  {booking.get('duration_min') or 30} minutes"]
    if price:
        lines.append(f"  Price:     Rs {price:,}")
    lines.append(f"  Name:      {booking.get('customer_name') or '-'}")
    return "\n".join(lines)


def _body(business: dict, booking: dict, opening: str, closing: str) -> str:
    name = booking.get("customer_name") or "there"
    return (f"Assalam o Alaikum {name},\n\n{opening}\n\n{_details(business, booking)}\n\n{closing}\n\n"
            f"Thank you,\n{business['name']}\n")


def _send(business: dict, booking: dict, subject: str, body: str) -> bool:
    """Start sending in the background. True if an email is on its way."""
    to = customer_email(business, booking)
    account = db.mail_account(business["id"]) if to else None
    if not account:
        return False
    account["sender"] = formataddr((business["name"], account["user"]))
    threading.Thread(target=_deliver, args=(to, subject, body, account), name="customer-mail", daemon=True).start()
    return True


def _deliver(to: str, subject: str, body: str, account: dict):
    send_email(to, subject, body, account=account)


def booking_confirmed(business: dict, booking: dict, moved_from: "dict | None" = None) -> bool:
    if moved_from:
        subject = f"Appointment moved: {booking['service']}, {_when(booking)}"
        opening = (f"Your appointment at {business['name']} has been moved. It was on {_when(moved_from)}; "
                   f"the new time is below.")
    else:
        subject = f"Appointment confirmed: {booking['service']}, {_when(booking)}"
        opening = f"Your appointment at {business['name']} is confirmed."
    return _send(business, booking, subject, _body(
        business, booking, opening, "Need to change or cancel? Just message us on WhatsApp."))


def reminder(business: dict, booking: dict) -> bool:
    return _send(business, booking, f"Reminder: {booking['service']}, {_when(booking)}", _body(
        business, booking, f"This is a reminder of your appointment at {business['name']}.",
        "Please confirm on WhatsApp (reply \"haan\"), or reply \"cancel\" there if you can't come."))


def cancelled(business: dict, booking: dict) -> bool:
    return _send(business, booking, f"Appointment cancelled: {booking['service']}, {_when(booking)}", _body(
        business, booking, f"Your appointment at {business['name']} has been cancelled.",
        "To book again, just message us on WhatsApp."))


def send_test(business: dict) -> "tuple[bool, str]":
    """Dashboard 'Send test email': a short email from the business's address to itself."""
    account = db.mail_account(business["id"])
    if not account:
        return False, "Save the business email and its App Password first."
    account["sender"] = formataddr((business["name"], account["user"]))
    try:
        send_email(account["user"], "AURA test email",
                   f"Booking emails from {business['name']} to customers are working.", raise_errors=True,
                   account=account)
    except Exception as e:
        reason = str(e)
        if "535" in reason or "Username and Password not accepted" in reason:
            return False, ("The mail server rejected the email or password. For Gmail, use an App Password "
                           "(Google Account → Security → 2-Step Verification → App passwords).")
        return False, f"Could not send: {reason[:150]}"
    return True, f"Sent to {account['user']}. Check the inbox (and the spam folder once)."
