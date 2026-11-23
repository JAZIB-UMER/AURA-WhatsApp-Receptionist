"""Chat with AURA in the terminal — no WhatsApp needed.

Uses the real model, Gemini, Redis and Google Calendar, so bookings you make
here really appear in that business's calendar. The chat is saved like a WhatsApp
chat (dashboard -> Conversations), and an escalation really alerts the owner.

Run:  python chat_cli.py              (asks which business if there are several)
      python chat_cli.py 2            (chat as business id 2)

Commands while chatting:
  new    be a NEW customer (fresh fake number: AURA doesn't know your name yet)
  reset  start this conversation over (same customer)
  quit   exit
"""
import random
import sys

from app.services import db, entity_model, intent_model
from app.services.agent import process_message
from app.services.manage import display_phone
from app.services.messaging import record_incoming
from app.services.state import clear_state

TEST_PHONE = "920000000000"   # the regular test customer (AURA remembers their name from earlier bookings)


def new_test_phone(business_id: int) -> str:
    """A fake number with no bookings or messages here, so AURA treats it as a first-time customer."""
    while True:
        phone = "9200" + "".join(random.choices("0123456789", k=8))
        if not db.last_customer_name(business_id, phone) and not db.list_messages(business_id, phone, limit=1):
            return phone


def pick_business():
    businesses = db.list_businesses(active_only=True)
    if not businesses:
        raise SystemExit("No business yet. Open the dashboard (streamlit run Dashboard.py) and add one as admin.")
    if len(sys.argv) > 1:
        chosen = db.get_business(int(sys.argv[1]))
        if not chosen:
            raise SystemExit(f"No business with id {sys.argv[1]}")
        return chosen
    if len(businesses) == 1:
        return businesses[0]
    for b in businesses:
        print(f"  {b['id']}: {b['name']}")
    return db.get_business(int(input("Business id: ")))


def describe_customer(business: dict, phone: str) -> str:
    known = db.last_customer_name(business["id"], phone)
    return f"customer {display_phone(phone) or phone}" + (f" (returning: {known})" if known else " (new customer)")


if __name__ == "__main__":
    business = pick_business()
    intent_model.load()
    entity_model.load()
    phone = TEST_PHONE
    clear_state(business["id"], phone)
    print(f"\nChatting with AURA for '{business['name']}' as {describe_customer(business, phone)}")
    print("Commands: 'new' = new customer, 'reset' = start over, 'quit' = exit\n")
    while True:
        try:
            message = input("You:  ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        command = message.lower()
        if command in ("quit", "exit"):
            break
        if command == "reset":
            clear_state(business["id"], phone)
            print("(conversation reset)\n")
            continue
        if command == "new":
            phone = new_test_phone(business["id"])
            print(f"(now chatting as a {describe_customer(business, phone)})\n")
            continue
        if message:
            business = db.get_business(business["id"])   # pick up dashboard changes live
            record_incoming(business, phone, message)
            reply = process_message(business, phone, message)
            db.log_message(business["id"], phone, "out", reply)   # saved, not sent (no WhatsApp here)
            print("AURA:", reply.replace("\n", "\n      "), "\n")
