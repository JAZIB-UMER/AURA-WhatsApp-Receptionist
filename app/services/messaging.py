"""Every customer message in and out goes through here, so each conversation is backed up in SQLite.

Owner alerts are not customer messages; they are stored in the escalations table instead.
"""
from app.services import db
from app.services.whatsapp import send_message


def record_incoming(business: dict, phone: str, text: str):
    try:
        db.log_message(business["id"], phone, "in", text)
    except Exception as e:                       # a logging problem must never stop AURA replying
        print(f"[Log] Could not save incoming message: {e}")


def send_to_customer(business: dict, phone: str, text: str, kind: str = "chat", booking_id: "int | None" = None,
                     chat_id: "str | None" = None) -> bool:
    """Send a WhatsApp message to a customer from the business's own WhatsApp, and save it.
    chat_id: the exact chat to reply to (e.g. a privacy ID that couldn't be turned into a number)."""
    delivered = send_message(chat_id or phone, text, session=business.get("whatsapp_session"))
    try:
        db.log_message(business["id"], phone, "out", text, kind=kind, booking_id=booking_id, delivered=delivered)
    except Exception as e:
        print(f"[Log] Could not save outgoing message: {e}")
    return delivered
