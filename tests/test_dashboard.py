"""Dashboard UI tests with Streamlit's AppTest (headless, temporary database, nothing sent).

Run:  python tests/test_dashboard.py
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
os.environ["AURA_DB"] = os.path.join(tempfile.mkdtemp(), "dashboard_test.db")   # before importing db
os.environ["ADMIN_PASSWORD"] = "admin-test-123"

from streamlit.testing.v1 import AppTest  # noqa: E402

from app.services import db, manage  # noqa: E402
from app.services.datetime_parser import PKT  # noqa: E402

CHECKS = []
SENT = []


def check(name, condition, detail=""):
    CHECKS.append((name, bool(condition)))
    print(("[PASS] " if condition else "[FAIL] ") + name + ("" if condition else f"  -> {detail}"))


# ------------------------------------------------------------------ fake external calls
def fake_cancel(business, booking_id, notify):
    db.set_booking_status(booking_id, "cancelled")
    SENT.append((booking_id, notify))
    return {"error": None, "calendar": True, "notified": True if notify else None}


manage.cancel_booking = fake_cancel
CALENDAR_CHECK = {"ok": True}
manage.check_calendar = lambda business: ((True, "Calendar is reachable.") if CALENDAR_CHECK["ok"] else
                                          (False, f"Can't read calendar {business.get('calendar_id')}. Share it."))

# ------------------------------------------------------------------ seed data
bid = db.create_business("Noon Salon", "noon", "secret123", waapi_instance_id="111",
                         calendar_id="noon@gmail.com", owner_phone="03001234567", owner_email="owner@noon.pk",
                         open_days=[0, 1, 2, 3, 4, 5, 6], open_time="09:00", close_time="22:00")
other = db.create_business("Smile Dental", "smile", "secret123", waapi_instance_id="222")
db.replace_services(bid, [{"name": "Haircut", "price": 500, "duration_min": 30},
                          {"name": "Beard", "price": 300, "duration_min": 15}])
now = datetime.now(PKT)
later = now + timedelta(hours=3)
tomorrow = now + timedelta(days=1)
b1 = db.create_booking(bid, "923001112222", "Ali", "Haircut", later.strftime("%Y-%m-%d"), "21:30", 30, "EV1")
b2 = db.create_booking(bid, "923003334444", "Sara", "Beard", tomorrow.strftime("%Y-%m-%d"), "11:00", 15, "EV2")
b3 = db.create_booking(bid, "923005556666", "Old", "Haircut", (now - timedelta(days=3)).strftime("%Y-%m-%d"),
                       "12:00", 30, "EV3")
db.create_booking(other, "923009999999", "Dentist patient", "Checkup", tomorrow.strftime("%Y-%m-%d"), "10:00", 30, "EV4")


def app():
    return AppTest.from_file(os.path.join(ROOT, "Dashboard.py"), default_timeout=60)


def by_label(widgets, label):
    for w in widgets:
        if w.label == label:
            return w
    raise KeyError(label)


def texts(at):
    out = [e.value for e in at.error] + [e.value for e in at.success] + [m.value for m in at.markdown]
    return " | ".join(str(t) for t in out)


def login(at, username, password):
    by_label(at.text_input, "Username").set_value(username)
    by_label(at.text_input, "Password").set_value(password)
    at.form_submit_button[0].click().run()
    return at


# ------------------------------------------------------------------ login
at = app().run()
check("login page renders", not at.exception and at.text_input, at.exception)
login(at, "noon", "wrong-pass")
check("wrong password is refused", any("Wrong username or password" in e.value for e in at.error), texts(at))
check("login form is emptied after submitting",
      by_label(at.text_input, "Username").value == "" and by_label(at.text_input, "Password").value == "",
      [(w.label, w.value) for w in at.text_input])
login(at, "noon", "secret123")
check("owner can log in", at.session_state["user"]["role"] == "owner" and not at.exception, at.exception)

# ------------------------------------------------------------------ overview
check("overview has no errors", not at.exception, at.exception)
metrics = {m.label: m.value for m in at.metric}
check("overview: next 7 days counts only this business", metrics.get("Next 7 days") == "2", metrics)
check("overview: expected revenue from prices", metrics.get("Expected revenue (7 days)") == "Rs 800", metrics)

# ------------------------------------------------------------------ bookings
at.switch_page("app_pages/bookings.py").run()
check("bookings page has no errors", not at.exception, at.exception)
check("bookings shows the 2 upcoming bookings", at.dataframe and len(at.dataframe[0].value) == 2,
      at.dataframe[0].value if at.dataframe else "no table")
check("other business's bookings are hidden",
      at.dataframe and "Dentist patient" not in set(at.dataframe[0].value["customer"]))
by_label(at.text_input, "Search").set_value("sara").run()
check("search filters by name", at.dataframe and list(at.dataframe[0].value["customer"]) == ["Sara"],
      at.dataframe[0].value if at.dataframe else "")
by_label(at.text_input, "Search").set_value("0300 333").run()
check("search filters by phone", at.dataframe and list(at.dataframe[0].value["customer"]) == ["Sara"])
by_label(at.text_input, "Search").set_value("").run()

# Row actions. AppTest can't click a table row, so the selection is seeded in session state.
def select_booking(booking_id):
    table = at.dataframe[0].value
    row = int(table.index[table["id"] == booking_id][0])
    at.session_state["bk_table"] = {"selection": {"rows": [row], "columns": [], "cells": []}}
    at.run()


select_booking(b2)
by_label(at.button, "Mark as confirmed").click().run()
check("confirm booking from the table", db.get_booking(b2)["status"] == "confirmed" and not at.exception)
select_booking(b2)
check("confirm button disabled once confirmed", by_label(at.button, "Mark as confirmed").disabled)
check("confirm twice is refused (server side)", manage.confirm_booking(db.get_business(bid), b2))
check("confirm other business's booking is refused", manage.confirm_booking(db.get_business(other), b1))

by_label(at.button, "Cancel booking").click().run()
by_label(at.button, "Keep booking").click().run()
check("'Keep booking' closes the dialog without cancelling",
      db.get_booking(b2)["status"] == "confirmed" and "bk_cancel_id" not in at.session_state)
select_booking(b2)
by_label(at.button, "Cancel booking").click().run()
check("cancel dialog asks about notifying the customer",
      any(c.label.startswith("Send the customer") and c.value for c in at.checkbox))
by_label(at.button, "Yes, cancel it").click().run()
check("cancel booking from the dialog", db.get_booking(b2)["status"] == "cancelled" and SENT == [(b2, True)], SENT)
# ------------------------------------------------------------------ conversation backup + escalations
db.log_message(bid, "923003334444", "in", "kal 11 baje beard book kar do")
db.log_message(bid, "923003334444", "out", "Apna naam batayein please?")
db.log_message(bid, "923003334444", "in", "Sara")
db.log_message(bid, "923003334444", "out", "Reminder: aap ki appointment hai", kind="reminder", booking_id=b2,
               delivered=False)
db.log_message(other, "923003334444", "in", "message to the OTHER business")
case = db.create_escalation(bid, "923003334444", "human_request", "Customer wants to talk to a person",
                            customer_name="Sara", summary="...")
db.set_escalation_notified(case, True, False)

check("cancelled booking leaves the default list",
      at.dataframe and b2 not in set(at.dataframe[0].value["id"]) and not at.exception)

at.session_state["bk_status"] = ["Awaiting reply", "Confirmed", "Cancelled"]
at.run()
select_booking(b2)
by_label(at.button, "View conversation").click().run()
bubbles = [m.value for m in at.markdown]
check("booking conversation shows the chat in order",
      not at.exception and any("kal 11 baje beard" in b for b in bubbles) and any("Sara" == b for b in bubbles))
check("failed reminder is marked 'not delivered'", any("not delivered" in c.value for c in at.caption))
check("the other business's message is not shown", not any("OTHER business" in b for b in bubbles))
at.session_state["bk_status"] = ["Awaiting reply", "Confirmed"]
st_keys = [k for k in ("bk_chat_id", "bk_table") if k in at.session_state]
for k in st_keys:
    del at.session_state[k]
at.run()

at.switch_page("app_pages/escalations.py").run()
check("escalations page lists the open case", not at.exception and any("Wants a person" in m.value and "Sara" in m.value
                                                                       for m in at.markdown), texts(at))
check("alert channels shown", any("Owner alerted by WhatsApp." == c.value for c in at.caption))
by_label(at.button, "Mark resolved").click().run()
check("mark resolved", db.get_escalation(case)["status"] == "resolved" and not at.exception)
check("open list is empty after resolving", any("No open escalations" in c.value for c in at.caption))

at.switch_page("app_pages/conversations.py").run()
check("conversations page lists the customer", not at.exception and at.dataframe
      and list(at.dataframe[0].value["customer"]) == ["Sara"], at.dataframe[0].value if at.dataframe else at.exception)
at.session_state["conv_table"] = {"selection": {"rows": [0], "columns": [], "cells": []}}
at.run()
check("selecting a customer shows the chat", any("kal 11 baje beard" in m.value for m in at.markdown))

at.switch_page("app_pages/overview.py").run()
metrics = {m.label: m.value for m in at.metric}
check("overview shows messages handled and escalation rate",
      metrics.get("Messages (30 days)") == "2" and metrics.get("Escalation rate (30 days)") == "100%"
      and metrics.get("Open escalations") == "0", metrics)

# ------------------------------------------------------------------ services
at.switch_page("app_pages/services.py").run()
check("services page has no errors", not at.exception, at.exception)
check("save disabled without changes", by_label(at.button, "Save services").disabled)

# ------------------------------------------------------------------ settings
at.switch_page("app_pages/settings.py").run()
check("settings page has no errors", not at.exception, at.exception)
check("owner can't see admin connection fields", not any(w.label == "WaAPI instance ID" for w in at.text_input))
by_label(at.text_input, "Owner's WhatsApp number").set_value("12345")
at.form_submit_button[0].click().run()
check("invalid phone is refused", any("Pakistani mobile number" in e.value for e in at.error), texts(at))
check("invalid phone not saved", db.get_business(bid)["owner_phone"] == "03001234567")
# AppTest can't type into the question table; seed it and check that saving keeps it exactly.
QUESTIONS = [{"question": "Aap ki age kya hai?", "type": "number", "required": True},
             {"question": "Aap ki email?", "type": "email", "required": False}]
db.update_business(bid, custom_questions=QUESTIONS)
at.run()
check("settings page shows the questions table", not at.exception and "What AURA asks the customer"
      in [h.value for h in at.subheader], at.exception)
by_label(at.text_input, "Owner's WhatsApp number").set_value("+92 345 1234567")
at.form_submit_button[0].click().run()
saved = db.get_business(bid)
check("valid settings saved + phone normalised", saved["owner_phone"] == "03451234567", saved["owner_phone"])
check("saving settings keeps the questions with type and required",
      saved["custom_questions"] == QUESTIONS, saved["custom_questions"])
check("settings fields show the saved, cleaned phone after saving",
      by_label(at.text_input, "Owner's WhatsApp number").value == "03451234567",
      by_label(at.text_input, "Owner's WhatsApp number").value)
check("owner can't edit business name or type",
      by_label(at.text_input, "Business name").disabled and by_label(at.text_input, "Type of business").disabled)

# ------------------------------------------------------------------ account
at.switch_page("app_pages/account.py").run()
check("account page has no errors", not at.exception, at.exception)
by_label(at.text_input, "Current password").set_value("nope")
by_label(at.text_input, "New password").set_value("newpass123")
by_label(at.text_input, "Repeat new password").set_value("newpass123")
at.form_submit_button[0].click().run()
check("wrong current password refused", any("Current password is wrong" in e.value for e in at.error), texts(at))
by_label(at.text_input, "Current password").set_value("secret123")
by_label(at.text_input, "New password").set_value("short1")
by_label(at.text_input, "Repeat new password").set_value("short1")
at.form_submit_button[0].click().run()
check("weak new password refused", any("at least 8" in e.value for e in at.error), texts(at))
by_label(at.text_input, "Current password").set_value("secret123")
by_label(at.text_input, "New password").set_value("newpass123")
by_label(at.text_input, "Repeat new password").set_value("newpass123")
at.form_submit_button[0].click().run()
check("password changed", db.login("noon", "newpass123") is not None and db.login("noon", "secret123") is None)

# ------------------------------------------------------------------ lock-out
at = app().run()
for _ in range(5):
    login(at, "smile", "bad-password1")
login(at, "smile", "secret123")
check("6th attempt locked even with the right password", any("Too many wrong attempts" in e.value for e in at.error),
      texts(at))

# ------------------------------------------------------------------ admin
at = app().run()
login(at, "admin", "admin-test-123")
check("admin can log in", at.session_state["user"]["role"] == "admin" and not at.exception, at.exception)
check("admin sees business selector", any(s.label == "Managing" for s in at.selectbox))
at.switch_page("app_pages/businesses.py").run()
check("businesses page has no errors", not at.exception, at.exception)
check("businesses table lists both", at.dataframe and len(at.dataframe[0].value) == 2)


def add_business(name, username, password, instance="333", calendar="fixit@gmail.com", email="fix@it.pk"):
    by_label(at.text_input, "Business name").set_value(name)
    by_label(at.text_input, "Owner login username").set_value(username)
    by_label(at.text_input, "Owner's first password").set_value(password)
    by_label(at.text_input, "WaAPI instance ID").set_value(instance)
    by_label(at.text_input, "Google Calendar ID (email)").set_value(calendar)
    by_label(at.text_input, "Owner's email (for alerts)").set_value(email)
    at.form_submit_button[0].click().run()


add_business("Fix It", "fixit", "secret123", instance="", calendar="")
check("WaAPI instance and calendar are required",
      any("WaAPI instance ID is required" in e.value for e in at.error)
      and any("Calendar ID (email) is required" in e.value for e in at.error), texts(at))
check("form keeps typed values after an error", by_label(at.text_input, "Business name").value == "Fix It")
add_business("Fix It", "fixit", "secret123", email="")
check("owner email is required", any("Owner's email is required" in e.value for e in at.error), texts(at))
add_business("Fix It", "fixit", "secret123", calendar="not-an-email")
check("bad calendar email refused", any("Calendar ID looks like an email" in e.value for e in at.error), texts(at))
add_business("Fix It", "noon", "secret123")
check("duplicate username refused", any("already used" in e.value for e in at.error), texts(at))
add_business("Fix It", "fixit", "secret123", instance="111")
check("duplicate WaAPI instance refused", any("already linked" in e.value for e in at.error), texts(at))
add_business("Fix It", "Fix It!", "123")
check("bad username and weak password both reported", len(at.error) >= 2, texts(at))
add_business("Fix It", "fixit", "secret123")
check("valid business added", any(b["username"] == "fixit" and b["calendar_id"] == "fixit@gmail.com"
                                  and b["owner_email"] == "fix@it.pk" for b in db.list_businesses()))
check("new business becomes the managed one", at.session_state["business_id"] == db.list_businesses()[-1]["id"])
check("no errors after adding", not at.exception, at.exception)
at.switch_page("app_pages/businesses.py").run()
check("add form is empty after adding",
      all(by_label(at.text_input, label).value in ("", None) for label in
          ["Business name", "Owner login username", "WaAPI instance ID", "Google Calendar ID (email)"]),
      [(w.label, w.value) for w in at.text_input])

at.switch_page("app_pages/settings.py").run()
check("admin sees connection fields", any(w.label == "WaAPI instance ID" for w in at.text_input))
check("admin can edit the business name", not by_label(at.text_input, "Business name").disabled)
by_label(at.text_input, "Google Calendar ID").set_value("")
at.form_submit_button[0].click().run()
check("admin can't remove the calendar", db.get_business(at.session_state["business_id"])["calendar_id"]
      == "fixit@gmail.com" and any("required" in e.value for e in at.error), texts(at))
at.switch_page("app_pages/account.py").run()
check("admin reset needs no current password", not any(w.label == "Current password" for w in at.text_input))

# ------------------------------------------------------------------ owner can't reach admin page
at = app().run()
login(at, "fixit", "secret123")          # "smile" is locked out by the test above
try:
    at.switch_page("app_pages/businesses.py").run()
    blocked = False
except ValueError:
    blocked = True                                          # page isn't registered for owners
check("owner can't open the businesses page", blocked or any("Admins only" in e.value for e in at.error))

# ------------------------------------------------------------------ stay logged in after a refresh
from app import ui  # noqa: E402

at = app().run()
login(at, "fixit", "secret123")
token = at.session_state["session_token"]
check("login creates a stored session", db.get_session(token) == {"role": "owner", "business_id":
                                                                   db.login("fixit", "secret123")["id"]})

ui.cookie_token = lambda: token                              # what the browser sends after a refresh
refreshed = app().run()                                      # new session, empty session state
check("refresh keeps the owner logged in",
      refreshed.session_state["user"]["role"] == "owner" and not refreshed.exception,
      refreshed.session_state["user"] if "user" in refreshed.session_state else "logged out")
by_label(refreshed.button, "Log out").click().run()
check("log out removes the stored session", db.get_session(token) is None)
again = app().run()
check("after log out the old cookie no longer works", "user" not in again.session_state or not again.session_state["user"])

ui.cookie_token = lambda: "made-up-token"
check("a fake token is refused", "user" not in app().run().session_state)
ui.cookie_token = lambda: ""

# ------------------------------------------------------------------ platform settings (admin)
from app.services import config, nlu, voice, whatsapp  # noqa: E402

config.save({k: None for k in ("waapi_token", "gemini_api_key", "groq_api_key", "google_service_account",
                               "smtp_host", "smtp_user", "smtp_password", "smtp_from", "admin_password_hash")})
TOKEN_OK = {"value": True}
whatsapp.check_token = lambda token=None: (TOKEN_OK["value"], "Token works." if TOKEN_OK["value"] else "rejected")
nlu.check_gemini = lambda key=None, model=None: (True, "ok")
voice.check_groq = lambda key=None, model=None: (True, "ok")

at = login(app().run(), "admin", "admin-test-123")
at.switch_page("app_pages/platform.py").run()
check("platform settings page opens for the admin", not at.exception and "not set" in texts(at), at.exception)


def submit(at, form):
    """Click the save button of one platform form (pf_waapi, pf_smtp, ...): found by form, not position."""
    next(b for b in at.form_submit_button if b.proto.form_id.startswith(form)).click().run()


def fill(at, label, value, nth=0):
    [w for w in at.text_input if w.label == label][nth].set_value(value)


fill(at, "API token", "short")
submit(at, "pf_waapi")
check("an invalid WaAPI token is refused", "doesn't look like a valid key" in texts(at)
      and not config.is_set("waapi_token"), texts(at))

TOKEN_OK["value"] = False
fill(at, "API token", "waapi_live_ABCDEFGHIJKLMNOPQRSTUV")
submit(at, "pf_waapi")
check("a token WaAPI rejects is not saved", "Not saved" in texts(at) and not config.is_set("waapi_token"), texts(at))

TOKEN_OK["value"] = True
fill(at, "API token", "  waapi_live_ABCDEFGHIJKLMNOPQRSTUV \n")
at.form_submit_button[0].click().run()
check("a valid token is saved (spaces/line breaks from pasting removed)",
      config.get("waapi_token") == "waapi_live_ABCDEFGHIJKLMNOPQRSTUV", config.get("waapi_token"))
page_text = texts(at) + " ".join(str(w.value) for w in at.text_input)
check("the saved token is never shown, only its last 4 characters",
      "ABCDEFGHIJKLMNOPQRSTUV" not in page_text and "…STUV" in page_text, page_text[:300])
check("the token field is empty again after saving", all(not w.value for w in at.text_input if w.label == "API token"))
submit(at, "pf_waapi")
check("saving with an empty field keeps the saved token",
      config.get("waapi_token") == "waapi_live_ABCDEFGHIJKLMNOPQRSTUV")

fill(at, "Server", "smtp.gmail.com")
fill(at, "Port", "99999")
fill(at, "Username (email)", "aura.alerts@gmail.com")
fill(at, "Password", "abcd efgh ijkl mnop")
submit(at, "pf_smtp")
check("a wrong email port is refused", "between 1 and 65535" in texts(at) and not config.is_set("smtp_host"))
fill(at, "Port", "587")
submit(at, "pf_smtp")
check("email settings saved; Gmail app password stored without spaces",
      config.get("smtp_host") == "smtp.gmail.com" and config.get("smtp_password") == "abcdefghijklmnop"
      and config.get("smtp_user") == "aura.alerts@gmail.com", config.get("smtp_password"))

fill(at, "Current password", "wrong-one-1")
fill(at, "New password", "newAdmin2026")
fill(at, "Repeat new password", "newAdmin2026")
submit(at, "pf_admin")
check("admin password not changed with a wrong current password", not config.is_set("admin_password_hash"))
fill(at, "Current password", "admin-test-123")
fill(at, "New password", "newAdmin2026")
fill(at, "Repeat new password", "newAdmin2026")
submit(at, "pf_admin")
check("admin password changed in the dashboard", config.is_set("admin_password_hash") and not at.exception)
check("the old .env password no longer works", not config.check_admin_password("admin-test-123"))
check("the new password works", config.check_admin_password("newAdmin2026"))
config.save({"admin_password_hash": None})      # back to the .env password for anything after this

page = AppTest.from_file(os.path.join(ROOT, "app_pages", "platform.py"), default_timeout=30).run()
check("platform settings refuse non-admins", "Admins only." in [e.value for e in page.error])

# ------------------------------------------------------------------ delete a conversation
db.log_message(bid, "923007776666", "in", "test message to delete")
db.log_message(bid, "923007776666", "out", "AURA reply to delete")
db.save_profile_name(bid, "923007776666", "Delete Me")
at = login(app().run(), "noon", "newpass123")      # changed by the account test above
at.switch_page("app_pages/conversations.py").run()
check("WhatsApp profile name shown for a customer without a booking",
      "Delete Me" in list(at.dataframe[0].value["customer"]), list(at.dataframe[0].value["customer"]))
row = list(at.dataframe[0].value["customer"]).index("Delete Me")
at.session_state["conv_table"] = {"selection": {"rows": [row], "columns": [], "cells": []}}
at.run()
by_label(at.button, "Delete conversation").click().run()
check("delete asks for confirmation first", any("Delete the whole chat" in m.value for m in at.markdown)
      and len(db.list_messages(bid, "923007776666")) == 2)
by_label(at.button, "Keep it").click().run()
check("'Keep it' keeps the chat", len(db.list_messages(bid, "923007776666")) == 2 and not at.exception)
at.session_state["conv_table"] = {"selection": {"rows": [row], "columns": [], "cells": []}}
at.run()
by_label(at.button, "Delete conversation").click().run()
by_label(at.button, "Yes, delete it").click().run()
check("conversation deleted (messages and WhatsApp name)", not db.list_messages(bid, "923007776666")
      and db.customer_display_name(bid, "923007776666") is None and not at.exception, at.exception)
check("other customers' chats are untouched", db.list_messages(bid, "923003334444"))

# ------------------------------------------------------------------ calendar warning after saving
at.session_state[f"calendar_warning_{bid}"] = "Can't read calendar noon@gmail.com. Share it."
at.switch_page("app_pages/settings.py").run()
check("a calendar AURA can't use is shown as a warning on Settings",
      any("Google Calendar" in (w.proto.title or "") or "Share it" in w.value for w in at.warning), at.warning)

failed = [n for n, ok in CHECKS if not ok]
print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
sys.exit(1 if failed else 0)
