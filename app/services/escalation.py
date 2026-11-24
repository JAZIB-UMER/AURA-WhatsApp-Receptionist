"""Hand a customer over to the business owner.

Used for: the customer asks for a human, AURA failed to understand 3 messages, or a customer
didn't confirm a reminder. Each case is saved (dashboard -> Escalations) and the owner gets a
WhatsApp message and an email with a short summary of the recent conversation.
"""
import re
from datetime import datetime, timedelta

from app.services import db
from app.services.datetime_parser import describe
from app.services.email_alert import send_email
from app.services.whatsapp import send_message

SUMMARY_MESSAGES = 10                                # last N messages of the last 24 h go into the alert
REPEAT_WINDOW_HOURS = 2                               # same customer asking again: don't alert twice

KINDS = {
    "human_request": "Customer wants to talk to a person",
    "not_understood": "AURA could not understand the customer",
    "unconfirmed": "Customer did not confirm the appointment",
    "calendar_unavailable": "Booking request — AURA could not use the Google Calendar",
}

# "owner se baat karni hai", "kisi insaan se baat karwao", "call me", "manager ka number do", ...
_PERSON = (r"insaan|insan|human|real person|actual person|person|banda|bande|owner|malik|maalik|manager|admin|"
           r"staff|receptionist|doctor|dr|sahab|sahib|kisi|someone|somebody")   # not "sir": too common in polite messages
_CONTACT = r"baat|bat|talk|speak|contact|rabta|rabita|call|connect|milwa\w*|number|num"
HUMAN_RE = re.compile(
    rf"\b({_PERSON})\b.*\b({_CONTACT})\b"                      # person ... contact
    rf"|\b(talk|speak|connect)\b.*\b({_PERSON})\b"             # talk to ... person
    r"|\bcall\s+(me|kar\w*|kij\w*|ki\w*|kro|karo)\b|\bmujhe\s+call\b"
    r"|\bphone\s+(pe|par|per)\s+baat\b|\bvoice\s+call\b"
    r"|\b(agent|representative|customer\s+(care|service|support))\b",
    re.IGNORECASE,
)


def wants_human(text: str) -> bool:
    return bool(HUMAN_RE.search(text or ""))


def display_phone(phone: str) -> str:
    digits = str(phone or "")
    if len(digits) == 12 and digits.startswith("923"):
        return f"0{digits[2:5]} {digits[5:]}"
    return digits


def conversation_summary(business_id: int, phone: str, name: "str | None" = None) -> str:
    since = (datetime.now() - timedelta(hours=24)).isoformat(timespec="seconds")
    messages = db.list_messages(business_id, phone, since=since, limit=SUMMARY_MESSAGES)
    if not messages:
        return "(no recent messages)"
    who = {"in": name or "Customer", "out": "AURA"}
    return "\n".join(f"[{m['created_at'][11:16]}] {who[m['direction']]}: {m['text']}" for m in messages)


def escalate(business: dict, phone: str, kind: str, detail: str = "", booking: "dict | None" = None) -> dict:
    """Save the case and alert the owner (WhatsApp + email). Returns the escalation row."""
    name = (booking or {}).get("customer_name") or db.customer_display_name(business["id"], phone)
    reason = KINDS.get(kind, kind) + (f" — {detail}" if detail else "")
    summary = conversation_summary(business["id"], phone, name)
    escalation_id = db.create_escalation(business["id"], phone, kind, reason, customer_name=name,
                                         booking_id=(booking or {}).get("id"), summary=summary)

    customer = f"{name} ({display_phone(phone)})" if name else display_phone(phone)
    lines = [f"AURA alert — {business['name']}", f"Customer: {customer}", f"Reason: {reason}"]
    if booking:
        lines.append(f"Booking: {booking['service']} — {describe(booking['date'], booking['time'])}")
    if phone.isdigit():
        lines.append(f"Chat with them: https://wa.me/{phone}")

    owner_phone = business.get("owner_phone")
    whatsapp_ok = bool(owner_phone) and send_message(owner_phone, "\n".join(lines),
                                                     session=business.get("whatsapp_session"))
    email_ok = send_email(
        business.get("owner_email") or "",
        f"AURA: {KINDS.get(kind, 'Attention needed')} — {customer}",
        "\n".join(lines) + "\n\nRecent conversation:\n" + summary +
        "\n\nOpen the AURA dashboard → Escalations to see the full chat and mark this as resolved.",
    )
    db.set_escalation_notified(escalation_id, whatsapp_ok, email_ok)
    if not (whatsapp_ok or email_ok):
        print(f"[Escalation] #{escalation_id} saved but the owner could not be notified "
              f"(owner WhatsApp/email: business Settings; email server: Admin → Platform settings)")
    else:
        print(f"[Escalation] #{escalation_id} {kind} for {phone}: WhatsApp={whatsapp_ok} email={email_ok}")
    return db.get_escalation(escalation_id)
