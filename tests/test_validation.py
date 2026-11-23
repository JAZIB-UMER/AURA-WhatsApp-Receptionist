"""Dashboard input validation.  Run:  python tests/test_validation.py"""
import os
import sys
from datetime import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services import validation as v  # noqa: E402

CHECKS = []


def check(name, condition, detail=""):
    CHECKS.append((name if condition else f"{name}  -> {detail}", bool(condition)))


# phone
check("phone 03 format", v.phone("0300 1234567") == ("03001234567", None))
check("phone +92 format", v.phone("+92 300-1234567") == ("03001234567", None))
check("phone 92 format", v.phone("923001234567") == ("03001234567", None))
check("phone without 0", v.phone("3001234567") == ("03001234567", None))
check("phone empty allowed", v.phone("") == ("", None))
check("phone empty required", v.phone("", required=True)[1])
check("phone landline rejected", v.phone("051 1234567")[1])
check("phone too short", v.phone("0300 12345")[1])
check("phone letters", v.phone("abc")[1])

# username / password
check("username ok", v.username("Noon.Salon_1") == ("noon.salon_1", None))
check("username too short", v.username("ab")[1])
check("username spaces", v.username("noon salon")[1])
check("username reserved", v.username("admin")[1])
check("username taken", v.username("noon", taken=True)[1])
check("password ok", v.password("secret12", "secret12") == ("secret12", None))
check("password short", v.password("abc12")[1])
check("password no digit", v.password("abcdefgh")[1])
check("password mismatch", v.password("secret12", "secret13")[1])

# connection fields
check("instance ok", v.instance_id(" 108128 ") == ("108128", None))
check("instance letters", v.instance_id("abc")[1])
check("instance taken", v.instance_id("1", taken=True)[1])
check("calendar ok", v.calendar_id("a@gmail.com") == ("a@gmail.com", None))
check("calendar bad", v.calendar_id("not a calendar")[1])

# email
check("email ok + lowercased", v.email(" Owner@Gmail.com ") == ("owner@gmail.com", None))
check("email bad", v.email("owner@gmail")[1])
check("email required", v.email("", required=True)[1])
check("email optional", v.email("") == ("", None))

# talk to a human
from app.services.escalation import wants_human  # noqa: E402

for text in ["mujhe owner se baat karni hai", "kisi insaan se baat karwao", "Can I talk to a real person?",
             "please call me", "manager se baat karwain", "phone pe baat ho sakti hai?"]:
    check(f"human request: {text}", wants_human(text))
for text in ["kal 3 baje haircut", "hello sir, booking karni hai, mera number 03001234567", "kya ye real insaan hai?",
             "kisi aur din kar do", "Dr sahab kal afternoon mein baithenge", "booking for 2 person kal"]:
    check(f"not a human request: {text}", not wants_human(text))

# text fields
check("name ok", v.business_name("  Noon   Salon ") == ("Noon Salon", None))
check("name too short", v.business_name("A")[1])
check("greeting too long", v.greeting("x" * 301)[1])
rows = [{"question": " Aap ki age? ", "type": "Number", "required": True},
        {"question": None, "type": None, "required": None},                      # empty editor row
        {"question": "Aap ki email?", "type": "Email", "required": False},
        {"question": "Kya problem hai?", "type": "Text", "required": float("nan")}]  # new row: default required
check("questions ok", v.questions(rows) == ([
    {"question": "Aap ki age?", "type": "number", "required": True},
    {"question": "Aap ki email?", "type": "email", "required": False},
    {"question": "Kya problem hai?", "type": "text", "required": True}], None), v.questions(rows))
check("questions too many", v.questions([{"question": f"Question {i}?", "type": "Text", "required": True}
                                          for i in range(6)])[1])
check("questions duplicate", v.questions([{"question": "Aap ki age?", "type": "Text", "required": True},
                                          {"question": "aap ki age?", "type": "Text", "required": True}])[1])
check("questions too short", v.questions([{"question": "Age", "type": "Text", "required": True}])[1])
check("questions need a type", v.questions([{"question": "Aap ki age?", "type": None, "required": True}])[1])
check("name question refused (AURA always asks it)",
      v.questions([{"question": "Aap ka naam?", "type": "Text", "required": True}])[1])
check("other names are fine", v.questions([{"question": "Aap ke walid ka naam?", "type": "Text",
                                             "required": False}])[1] is None)

# answers to the owner's questions
from app.services import questions as qs  # noqa: E402

old = qs.normalize(["Aap ki age kya hai?"])
check("old plain-text questions become required text questions",
      old == [{"question": "Aap ki age kya hai?", "type": "text", "required": True}])
email_q = {"question": "Aap ki email?", "type": "email", "required": False}
number_q = {"question": "Aap ki age?", "type": "number", "required": True}
phone_q = {"question": "Ghar ka number?", "type": "phone", "required": True}
check("optional question shows how to skip", "skip" in qs.prompt(email_q) and "skip" not in qs.prompt(number_q))
check("email found inside a sentence", qs.check_answer(email_q, "meri email Ali.Khan@Gmail.com hai") ==
      ("ali.khan@gmail.com", None))
check("bad email asked again with a hint", qs.check_answer(email_q, "ali at gmail")[1].startswith("Sahi email"))
check("optional question can be skipped", qs.check_answer(email_q, "skip") == ("", None)
      and qs.check_answer(email_q, "nahi batana") == ("", None))
check("required question can't be skipped", qs.check_answer(number_q, "skip")[1].startswith("Ye batana zaroori"))
check("number found in '25 saal'", qs.check_answer(number_q, "25 saal") == ("25", None))
check("number missing asked again", qs.check_answer(number_q, "pata nahi yaar")[1])
check("phone answer normalised", qs.check_answer(phone_q, "+92 345 1234567") == ("03451234567", None))
check("bad phone asked again", qs.check_answer(phone_q, "12345")[1])

# hours
check("hours ok", v.hours([0, 1], time(10), time(20)) is None)
check("hours no days", v.hours([], time(10), time(20)))
check("hours close before open", v.hours([0], time(20), time(10)))
check("hours under an hour", v.hours([0], time(10), time(10, 30)))

# services table
nan = float("nan")
clean, errors = v.services([
    {"name": " haircut ", "price": 500, "duration_min": 30},
    {"name": "Beard", "price": 300.0, "duration_min": 15},
    {"name": None, "price": nan, "duration_min": nan},          # blank row from the editor
])
check("services clean", clean == [{"name": "Haircut", "price": 500, "duration_min": 30},
                                  {"name": "Beard", "price": 300, "duration_min": 15}] and not errors)
check("services duplicate", v.services([{"name": "Haircut", "price": 1, "duration_min": 30},
                                        {"name": "haircut", "price": 2, "duration_min": 30}])[1])
check("services missing price", v.services([{"name": "Facial", "price": nan, "duration_min": 30}])[1])
check("services negative price", v.services([{"name": "Facial", "price": -5, "duration_min": 30}])[1])
check("services long duration", v.services([{"name": "Facial", "price": 5, "duration_min": 600}])[1])
check("services number name", v.services([{"name": "12", "price": 5, "duration_min": 30}])[1])

check("services decimal price", v.services([{"name": "Facial", "price": 99.5, "duration_min": 30}])[1])

# exactly what st.data_editor returns: typed columns with pandas NA in empty cells
import pandas as pd  # noqa: E402

frame = pd.DataFrame({
    "name": pd.Series(["Haircut", pd.NA, "Facial", pd.NA], dtype="string"),
    "price": pd.Series([500, pd.NA, pd.NA, 200], dtype="Int64"),
    "duration_min": pd.Series([30, pd.NA, 45, 30], dtype="Int64"),
})
try:
    clean, errors = v.services(frame.to_dict("records"))
    check("editor rows with pandas NA don't crash", True)
    check("editor: blank row ignored, missing price + missing name reported",
          clean == [{"name": "Haircut", "price": 500, "duration_min": 30}] and len(errors) == 2, (clean, errors))
except Exception as e:                                       # noqa: BLE001
    check(f"editor rows with pandas NA don't crash ({e!r})", False)
check("clean_text of pandas NA", v.clean_text(pd.NA) == "")

# platform settings (admin)
import json  # noqa: E402

check("api key: pasted spaces/line breaks removed", v.api_key(" gsk_ABCDEFGHIJ\n0123456789xyz ", "Groq")
      == ("gsk_ABCDEFGHIJ0123456789xyz", None))
check("api key: too short refused", v.api_key("abc123", "Groq")[1])
check("api key: odd characters refused", v.api_key("gsk_ABCDEFGHIJ0123456789<script>", "Groq")[1])
check("model name normalised", v.model_name(" Gemini-3.5-Flash-Lite ", "Gemini") == ("gemini-3.5-flash-lite", None))
check("model name with spaces refused", v.model_name("gemini flash", "Gemini")[1])
check("smtp ok", v.smtp_server(" SMTP.gmail.com ", "587") == (("smtp.gmail.com", "587"), None))
check("smtp host without a dot refused", v.smtp_server("localhost", "587")[1])
check("smtp port out of range refused", v.smtp_server("smtp.gmail.com", "70000")[1])
check("smtp port not a number refused", v.smtp_server("smtp.gmail.com", "abc")[1])
check("sender name ok", v.sender_name("AURA Alerts <a@b.pk>") == ("AURA Alerts <a@b.pk>", None))
check("sender with a line break refused (header injection)", v.sender_name("x\nBcc: evil@x.com")[1])
key = {"type": "service_account", "client_email": "aura@proj.iam.gserviceaccount.com",
       "private_key": "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\n",
       "token_uri": "https://oauth2.googleapis.com/token"}
value, error = v.service_account(json.dumps(key).encode())
check("service account key accepted", error is None and json.loads(value)["client_email"] == key["client_email"])
check("service account: not JSON", v.service_account(b"hello")[1])
check("service account: other JSON refused", v.service_account(json.dumps({"type": "authorized_user"}).encode())[1])
check("service account: incomplete key refused",
      v.service_account(json.dumps({**key, "private_key": ""}).encode())[1])
check("service account: empty upload refused", v.service_account(b"")[1])

failed = [name for name, ok in CHECKS if not ok]
for name, ok in CHECKS:
    print(("[PASS] " if ok else "[FAIL] ") + name)
print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
sys.exit(1 if failed else 0)
