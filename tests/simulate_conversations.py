"""Conversation-flow simulation with fake services (no Redis, Google, Gemini or model needed).

Run:  python tests/simulate_conversations.py

Uses a temporary SQLite database with two test businesses, a fake calendar and a
keyword-based stand-in for the model, then checks what AURA asks, when it books,
reschedules, cancels and escalates.
"""
import json
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
os.environ["AURA_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")   # never touch the real database


def mod(name, **attrs):
    m = types.ModuleType(name); m.__dict__.update(attrs); sys.modules[name] = m; return m


class FakeRedis(dict):
    def setex(s, k, t, v): s[k] = v
    def set(s, k, v, nx=False, ex=None):
        if nx and k in s: return None
        s[k] = v; return True
    def get(s, k): return dict.get(s, k)
    def delete(s, *ks): [s.pop(k, None) for k in ks]
    def lpush(s, k, v): s.setdefault(k, []).insert(0, v)


class FakeSched:
    def __init__(s, *a, **k): pass
    def start(s): pass
    def add_job(s, *a, **k): pass
    def remove_job(s, i): pass


DB = FakeRedis()
mod("redis", Redis=lambda **kw: DB)
mod("apscheduler"); mod("apscheduler.schedulers"); mod("apscheduler.schedulers.background", BackgroundScheduler=FakeSched)
mod("dotenv", load_dotenv=lambda *a, **k: None)
mod("requests")
mod("google"); mod("google.oauth2", service_account=None); mod("googleapiclient"); mod("googleapiclient.discovery", build=None)

from app.services import db, nlu, agent, reminder  # noqa: E402
from app.services.datetime_parser import parse_date  # noqa: E402

# ---------------------------------------------------------------- fakes


def fake_predict(text):
    t = text.lower()
    if "reschedul" in t or "shift" in t: return "reschedule", 0.99
    if "cancel" in t: return "cancel_booking", 0.99
    if "salam" in t or "hello" in t: return "greeting", 0.99
    if any(w in t for w in ("kitne", "fees", "price", "rates")): return "price_query", 0.99
    if any(w in t for w in ("free", "available", "khali")): return "check_availability", 0.99
    if "book" in t or "appointment" in t: return "book_appointment", 0.98
    return "book_appointment", 0.40


nlu.intent_model.predict = fake_predict
GEMINI = {}
GEMINI_CALLS = []
NER = {"on": False, "out": {}}   # entity model off by default: the scenarios test the Gemini fallback path
                                 # out: message -> what the tagger finds in it


def fake_gemini(text):
    GEMINI_CALLS.append(text)
    return dict(GEMINI)


def fake_extract(text, name_needs_cue=True):
    if not NER["on"]:
        return None
    return {"date": None, "time": None, "service": None, "name": None, **NER["out"].get(text, {})}


nlu._ask_gemini = fake_gemini
nlu.entity_model.extract = fake_extract

BUSY = {}                       # date -> list of (start, end)
EVENTS = {}                     # event_id -> details
SENT = []                       # (phone, message, WhatsApp session)


def fake_book(**kw):
    eid = f"EV{len(EVENTS) + 1}"; EVENTS[eid] = kw
    return {"success": True, "event_id": eid}


def fake_cancel(event_id, calendar_id=None):
    EVENTS.pop(event_id, None); return True


agent.get_busy = lambda d, cal=None: list(BUSY.get(d, []))
agent.book_appointment = fake_book
agent.cancel_appointment = fake_cancel
reminder.cancel_appointment = fake_cancel
EMAILS = []                     # (to, subject, body)


def fake_send(phone, message, session=None):
    SENT.append((phone, message, session))
    return True


from app.services import escalation, messaging  # noqa: E402

messaging.send_message = fake_send          # customer messages (replies, reminders)
escalation.send_message = fake_send         # owner WhatsApp alerts
escalation.send_email = lambda to, subject, body: (EMAILS.append((to, subject, body)), bool(to))[1]
reminder.event_exists = lambda event_id, calendar_id=None: event_id in EVENTS

# ---------------------------------------------------------------- test businesses

salon_id = db.create_business("Glow Salon", "glow", "secret1", business_type="Salon",
                              whatsapp_session="aura_1_test", calendar_id="glow@cal", owner_phone="03001111111",
                              open_days=[0, 1, 2, 3, 4, 5], open_time="10:00", close_time="20:00")
db.replace_services(salon_id, [{"name": "Haircut", "price": 800, "duration_min": 30},
                               {"name": "Nail polish", "price": 500, "duration_min": 45},
                               {"name": "Hair colour", "price": 3000, "duration_min": 90}])
clinic_id = db.create_business("City Clinic", "clinic", "secret2", whatsapp_session="aura_2_test",
                               owner_phone="03002222222", open_days=[0, 1, 2, 3, 4],
                               open_time="17:00", close_time="22:00",
                               custom_questions=["Aap ki age kya hai?", "Kya problem hai?"])
db.replace_services(clinic_id, [{"name": "Checkup", "price": 1500, "duration_min": 20}])
SALON, CLINIC = db.get_business(salon_id), db.get_business(clinic_id)


def chat(business, phone, *msgs, gemini=None):
    """Like main.handle_message: save the message, let AURA answer, send + save the reply."""
    out = None
    for m in msgs:
        GEMINI.clear(); GEMINI.update((gemini or {}).get(m, {}))
        fresh = db.get_business(business["id"])
        messaging.record_incoming(fresh, phone, m)
        out = agent.process_message(fresh, phone, m)
        messaging.send_to_customer(fresh, phone, out)
        print(f"  customer: {m}\n  AURA:     {out}".replace("\n", "\n            "))
    return out


checks = []


def check(name, cond):
    checks.append((name, bool(cond))); print(f"  [{'PASS' if cond else 'FAIL'}] {name}\n")


def session(business, phone):
    raw = DB.get(f"session:{business['id']}:{phone}")
    return json.loads(raw) if raw else None


# a weekday the salon is open, at least 2 days ahead, so nothing is "in the past"
from datetime import date as _date, datetime as _dt, timedelta as _td  # noqa: E402
from app.services.datetime_parser import PKT  # noqa: E402

OPEN_DAY = next(parse_date(w) for w in ("monday", "tuesday", "wednesday", "thursday", "friday")
                if parse_date(w) > parse_date("kal"))
OPEN_DAY_NAME = _date.fromisoformat(OPEN_DAY).strftime("%A")
SAT = parse_date("saturday")


def add_booking(business_id, phone, name, service, day, time, minutes=30):
    """Put a booking straight into the database (for multi-booking scenarios)."""
    eid = f"EV{len(EVENTS) + 1}"
    EVENTS[eid] = {"name": name}
    return db.create_booking(business_id, phone, name, service, day, time, minutes, eid, {})


# ---------------------------------------------------------------- scenarios

print("1) One message with service+date+time -> asks name -> shows summary -> books only after 'haan'")
out = chat(SALON, "p1", f"{OPEN_DAY_NAME} 3 baje nail polish book kar do", "Ayesha")
check("summary shown, nothing booked yet", "confirm kar doon" in out and "Nail polish (Rs 500)" in out
      and db.upcoming_booking(salon_id, "p1") is None)
out = chat(SALON, "p1", "haan")
b = db.upcoming_booking(salon_id, "p1")
check("booked after 'haan': right service, 15:00, 45 min", "confirm ho gayi" in out and b
      and b["service"] == "Nail polish" and b["time"] == "15:00" and b["duration_min"] == 45)

print("2) Service list, unknown service, ambiguous 'hair'")
out = chat(SALON, "p2", "appointment book karni hai")
check("question lists the business's services, one per line",
      "*Kaunsi service chahiye?*\n• Haircut — Rs 800\n" in out and "• Hair colour — Rs 3,000" in out)
out = chat(SALON, "p2", "massage")
check("'massage' not offered -> pick from list (one service per line)",
      "list mein nahi" in out and "\n• Nail polish — Rs 500" in out)
out = chat(SALON, "p2", "hair")
check("'hair' -> asks which, as a list", "*Kaunsi wali?*\n• Haircut — Rs 800\n• Hair colour — Rs 3,000" in out)
chat(SALON, "p2", "haircut", f"{OPEN_DAY_NAME} 4 baje", "Sara Khan", "ji")
check("booked haircut 16:00 for Sara Khan", (db.upcoming_booking(salon_id, "p2") or {}).get("time") == "16:00")

print("3) Slot busy -> nearest free slots -> customer replies just '6'")
BUSY[OPEN_DAY] = [(_dt.fromisoformat(f"{OPEN_DAY}T17:00").replace(tzinfo=PKT),
                   _dt.fromisoformat(f"{OPEN_DAY}T18:00").replace(tzinfo=PKT))]
out = chat(SALON, "p3", f"{OPEN_DAY_NAME} 5 baje haircut book karna hai")
check("says busy and suggests slots, one per line", "available nahi" in out and "*Free slots — " in out
      and "\n• " in out)
chat(SALON, "p3", "6", "Hina", "haan")
check("booked at 18:00", (db.upcoming_booking(salon_id, "p3") or {}).get("time") == "18:00")
BUSY.clear()

print("4) Closed day and 5) outside hours")
out = chat(SALON, "p4", "Sunday 3 baje haircut book karo")
check("closed on Sunday, shows hours", "band" in out and "Mon–Sat" in out)
chat(SALON, "p4", "rehne do")
out = chat(SALON, "p5", f"{OPEN_DAY_NAME} raat 9 baje haircut book karo")
check("9 PM is outside hours", "timings se bahar" in out)
chat(SALON, "p5", "rehne do")

print("6) Prices")
out = chat(SALON, "p6", "haircut ki fees kitni hai?")
check("haircut price", "Rs 800" in out)
out = chat(SALON, "p6", "rates kya hain?")
check("full price list", "Nail polish — Rs 500" in out and "Hair colour — Rs 3,000" in out)

print("7) 'Is <day> free?' -> free times -> booking continues")
out = chat(SALON, "p7", f"{OPEN_DAY_NAME} ko koi slot free hai?")
check("lists free times spread over the day (morning AND afternoon/evening)",
      "*Free slots — " in out and "AM" in out and "PM" in out)
chat(SALON, "p7", "11 baje", "haircut", "Zara", "haan")
check("booked 11:00", (db.upcoming_booking(salon_id, "p7") or {}).get("time") == "11:00")

print("8) Changing a detail at the summary")
out = chat(SALON, "p8", f"{OPEN_DAY_NAME} 1 baje haircut book karo", "Bilal", "nahi")
check("'nahi' -> asks what to change", "Kya change karna hai" in out)
out = chat(SALON, "p8", "2 baje")
check("new summary with 2:00 PM", "confirm kar doon" in out and "2:00 PM" in out)
out = chat(SALON, "p8", "nahi 3 baje kar do")
check("direct change in one message -> 3:00 PM", "3:00 PM" in out and "confirm kar doon" in out)
chat(SALON, "p8", "haan")
check("booked at 15:00", (db.upcoming_booking(salon_id, "p8") or {}).get("time") == "15:00")

print("9) Reschedule a single booking -> summary shows old and new -> 'haan'")
old = db.upcoming_booking(salon_id, "p7")
out = chat(SALON, "p7", f"meri appointment {OPEN_DAY_NAME} 2 baje shift kar dein")
check("summary shows old and new time, without repeating the old time", "shift kar doon" in out and "Purani" in out
      and "2:00 PM" in out and "Aap ki booking abhi" not in out)
out = chat(SALON, "p7", "haan")
new = db.upcoming_booking(salon_id, "p7")
check("moved: new 14:00, old marked rescheduled, old event deleted", "shift ho gayi" in out and new["time"] == "14:00"
      and db.get_booking(old["id"])["status"] == "rescheduled" and old["event_id"] not in EVENTS)

print("10) Cancel asks first and says WHICH booking; 'konsi cancel kardi?' is answered")
eid = db.upcoming_booking(salon_id, "p1")["event_id"]
out = chat(SALON, "p1", "booking cancel karni hy")
check("asks to confirm and shows the booking", "cancel kar doon" in out and "Nail polish" in out and "Ayesha" in out
      and eid in EVENTS)
out = chat(SALON, "p1", "haan")
check("cancelled with details", "cancel kar di gayi" in out and "Nail polish" in out
      and db.upcoming_booking(salon_id, "p1") is None and eid not in EVENTS)
out = chat(SALON, "p1", "konsi cancel kardi")
check("remembers what was cancelled", "Nail polish" in out and "cancel hui thi" in out)

print("11) Cancel -> 'nahi' keeps the booking")
chat(SALON, "p3", "meri booking cancel kar do")
out = chat(SALON, "p3", "nahi")
check("kept", "barqarar" in out and db.upcoming_booking(salon_id, "p3") is not None)

print("12) Two bookings: cancel asks which; 'Saturday wali' picks the right one")
mon_id = add_booking(salon_id, "p12", "Ali", "Haircut", OPEN_DAY, "12:00")
sat_id = add_booking(salon_id, "p12", "Ali", "Nail polish", SAT, "16:00", 45)
out = chat(SALON, "p12", "booking cancel karni hai")
check("lists both, numbered", "1)" in out and "2)" in out and "Kaunsi cancel" in out)
out = chat(SALON, "p12", "saturday wali")
check("confirms the Saturday one only", "cancel kar doon" in out and "Saturday" in out and "Haircut" not in out)
chat(SALON, "p12", "haan")
check("only Saturday cancelled", db.get_booking(sat_id)["status"] == "cancelled"
      and db.get_booking(mon_id)["status"] == "booked")

print("13) Pick by number, and 'dono'")
a = add_booking(salon_id, "p13", "Sana", "Haircut", OPEN_DAY, "10:00")
b2 = add_booking(salon_id, "p13", "Sana", "Hair colour", OPEN_DAY, "13:00", 90)
chat(SALON, "p13", "cancel karna hai booking", "2", "haan")
check("'2' cancelled the second", db.get_booking(b2)["status"] == "cancelled" and db.get_booking(a)["status"] == "booked")
c3 = add_booking(salon_id, "p13", "Sana", "Nail polish", SAT, "11:00", 45)
out = chat(SALON, "p13", "booking cancel karo", "dono")
check("'dono' asks to confirm both", "Ye sab bookings" in out)
chat(SALON, "p13", "haan")
check("both cancelled", db.get_booking(a)["status"] == "cancelled" and db.get_booking(c3)["status"] == "cancelled")

print("14) 'Meri bookings kab hain?' lists them")
add_booking(salon_id, "p14", "Usman", "Haircut", OPEN_DAY, "15:30")
out = chat(SALON, "p14", "meri booking kab hai?")
check("shows the booking", "Aap ki booking" in out and "Haircut" in out and "3:30 PM" in out)
out = chat(SALON, "p15", "meri bookings dikhao")
check("none -> says so", "koi upcoming booking nahi" in out)

print("15) Reschedule with two bookings -> asks which -> asks new day -> summary")
add_booking(salon_id, "p16", "Hira", "Haircut", OPEN_DAY, "10:30")
add_booking(salon_id, "p16", "Hira", "Nail polish", SAT, "12:00", 45)
out = chat(SALON, "p16", "appointment shift karni hai")
check("asks which booking", "Kaunsi shift karni hai" in out)
haircut_no = "2" if SAT < OPEN_DAY else "1"      # listed soonest first (on a Saturday, SAT is next week)
out = chat(SALON, "p16", haircut_no)
check(f"#{haircut_no} (listed soonest first) is the Haircut -> asks new day", "Haircut" in out and "Kis din shift" in out)
out = chat(SALON, "p16", f"{OPEN_DAY_NAME} 5 baje")
check("summary for the move", "shift kar doon" in out and "5:00 PM" in out)
chat(SALON, "p16", "haan")
moved = [b for b in db.upcoming_bookings(salon_id, "p16") if b["service"] == "Haircut"]
kept = [b for b in db.upcoming_bookings(salon_id, "p16") if b["service"] == "Nail polish"]
check("Haircut moved to 17:00, Nail polish untouched", moved and moved[0]["time"] == "17:00"
      and kept and kept[0]["time"] == "12:00")

print("15b) Returning customer isn't asked their name again; can change it at the summary")
out = chat(SALON, "p16", f"{OPEN_DAY_NAME} 12 baje haircut book karo")
check("name taken from last booking -> straight to summary", "confirm kar doon" in out and "Naam: Hira" in out)
out = chat(SALON, "p16", "naam Ayesha hai")
check("'naam Ayesha hai' changes the name (exactly Ayesha)", "Naam: Ayesha\n" in out and "confirm kar doon" in out)
chat(SALON, "p16", "rehne do")

print("16) Thanks and 'ok'")
out = chat(SALON, "p17", "Shukriya")
check("thanks -> polite close (not a greeting)", "shukriya" in out.lower() and "khush aamdeed" not in out)
out = chat(SALON, "p17", "ok")
check("'ok' -> short acknowledgement", out.startswith("👍"))

print("17) 'nahi' without a reminder must NOT cancel; after a reminder it does")
p2_booking = db.upcoming_booking(salon_id, "p2")
out = chat(SALON, "p2", "nahi")
check("booking still there; short friendly reply", db.get_booking(p2_booking["id"])["status"] == "booked"
      and out.startswith("Theek hai!"))
DB.setex(f"reminder_sent:{p2_booking['id']}", 1, "1")
out = chat(SALON, "p2", "nahi")
check("cancelled after reminder, says which", db.get_booking(p2_booking["id"])["status"] == "cancelled"
      and "Haircut" in out)

print("18) Clinic: custom questions asked, shown in the summary, saved")
clinic_day = next(parse_date(w) for w in ("monday", "tuesday", "wednesday", "thursday", "friday")
                  if parse_date(w) > parse_date("kal"))
clinic_day_name = _date.fromisoformat(clinic_day).strftime("%A")
chat(CLINIC, "p1", f"{clinic_day_name} sham 6 baje checkup ki appointment chahiye", "Bilal", "32")
out = chat(CLINIC, "p1", "bukhar hai")
check("summary includes the answers", "Aap ki age kya hai? 32" in out and "Kya problem hai? bukhar hai" in out)
chat(CLINIC, "p1", "haan")
cb = db.upcoming_booking(clinic_id, "p1")
check("answers saved with booking", cb and cb["answers"] == {"Aap ki age kya hai?": "32", "Kya problem hai?": "bukhar hai"})

print("19) Same phone, two businesses -> separate conversations")
chat(SALON, "p9", "appointment book karni hai")
out = chat(CLINIC, "p9", "Assalam o alaikum")
check("'Assalam o alaikum' answered with 'Walaikum Assalam! City Clinic...', salon flow untouched",
      out.startswith("Walaikum Assalam! City Clinic") and session(SALON, "p9")["intent"] == "book_appointment")

print("20) Three unclear answers -> the RIGHT owner is alerted")
SENT.clear()
chat(SALON, "p10", "appointment book karni hai", "haircut", "pata nahi", "hmm", "???")
check("salon owner alerted", any(p == "03001111111" for p, _, _ in SENT))
check("alert sent from the salon's own WhatsApp", all(s == "aura_1_test" for p, _, s in SENT if p == "03001111111"))

print("21) Salam mid-booking")
chat(SALON, "p11", "appointment book karni hai", "Assalam o alaikum")
s = session(SALON, "p11")
check("not counted as failure", s["failed_turns"] == 0 and s["intent"] == "book_appointment")

print("22) Reschedule rule in NLU")
nlu.intent_model.predict = lambda t: ("cancel_booking", 0.998)
r = nlu.understand_message("The appointment which i cancelled, can you please reschedule it on coming friday?")
check("intent = reschedule", r["intent"] == "reschedule")
nlu.intent_model.predict = fake_predict

print("23) Real chats from 12 Nov (model calls names, 'Timings', 'Shukriya' greetings)")
shop_id = db.create_business("NoonSyNai", "noon", "secret3", whatsapp_session="aura_3_test",
                             open_days=[0, 1, 2, 3, 4, 5], open_time="10:00", close_time="20:00")
db.replace_services(shop_id, [{"name": "Haircut", "price": 500, "duration_min": 30},
                              {"name": "Massage", "price": 200, "duration_min": 10},
                              {"name": "beard", "price": 300, "duration_min": 15}])
SHOP = db.get_business(shop_id)
REAL_MODEL_MISTAKES = {"jazib umer", "jazib", "timings", "hello", "hassan", "shukriya"}


def real_like_predict(text):
    if text.strip().lower() in REAL_MODEL_MISTAKES:
        return "greeting", 0.99
    if text.strip().lower() == "booking karni hy":
        return "cancel_booking", 0.63
    return fake_predict(text)


nlu.intent_model.predict = real_like_predict
agent.intent_model.predict = real_like_predict
out = chat(SHOP, "p23", "Timings")
check("'Timings' -> hours on their own line", out.startswith("🕒 *Timings:* Mon–Sat, 10:00 AM – 8:00 PM\n"))
out = chat(SHOP, "p23", "booking karni hy", gemini={"booking karni hy": {"intent": "book_appointment", "confidence": "high"}})
check("low-confidence 'cancel' sent to Gemini -> booking; 'Beard' capitalised", "• Beard — Rs 300" in out)
chat(SHOP, "p23", "beard", "sunday ko 11 bjay subah", "saturday 11 bjay subah")
out = chat(SHOP, "p23", "Jazib umer")
check("name accepted (no greeting loop), summary shown", "Naam: Jazib Umer" in out and "confirm kar doon" in out)
chat(SHOP, "p23", "haan")
sb = db.upcoming_booking(shop_id, "p23")
check("booked Saturday 11:00", sb and sb["date"] == SAT and sb["time"] == "11:00")
out = chat(SHOP, "p23", "Shukriya")
check("'Shukriya' -> thanks, not greeting", "Aap ka bhi shukriya" in out)
out = chat(SHOP, "p23", "booking cancel karni hy")
check("cancel asks first, names the booking", "cancel kar doon" in out and "Beard" in out and "Jazib Umer" in out)
chat(SHOP, "p23", "haan")
out = chat(SHOP, "p23", "konsi cancel kardi")
check("'konsi cancel kardi' answered", "Beard" in out and "cancel hui thi" in out)
nlu.intent_model.predict = fake_predict
agent.intent_model.predict = fake_predict

print("24) Reminder schedule: quiet hours, no duplicates, sensible for short notice")
from app.services.reminder import plan_reminders  # noqa: E402
cases = {
    "3 days ahead 4 PM": ("2026-11-12T14:00", "2026-11-15T16:00", ["reminder", "followup", "escalation"]),
    "next day 11 AM booked 2 PM": ("2026-11-12T14:12", "2026-11-13T11:00", ["reminder"]),
    "9:30 PM clinic slot": ("2026-11-12T14:00", "2026-11-15T21:30", ["reminder", "followup", "escalation"]),
    "10 AM, 2 days ahead": ("2026-11-12T14:00", "2026-11-14T10:00", ["reminder", "followup", "escalation"]),
    "booked 10 PM for 10 AM": ("2026-11-12T22:00", "2026-11-13T10:00", ["reminder"]),
    "2 hours ahead": ("2026-11-12T15:00", "2026-11-12T17:00", []),
}
ok = True
for label, (now, apt, kinds) in cases.items():
    plan = plan_reminders(_dt.fromisoformat(apt).replace(tzinfo=PKT), _dt.fromisoformat(now).replace(tzinfo=PKT))
    ok &= [k for k, _ in plan] == kinds and all(9 <= t.hour < 21 for _, t in plan) and len({t for _, t in plan}) == len(plan)
check("all reminder plans correct and between 9 AM - 9 PM", ok)


def times(now, apt):
    plan = plan_reminders(_dt.fromisoformat(apt).replace(tzinfo=PKT), _dt.fromisoformat(now).replace(tzinfo=PKT))
    return {k: t.strftime("%a %H:%M") for k, t in plan}


check("4 PM appointment: reminder 24 h before, follow-up 9 AM same day (12 h rule, after quiet hours), owner 1 PM",
      times("2026-11-12T14:00", "2026-11-15T16:00") == {"reminder": "Sat 16:00", "followup": "Sun 09:00",
                                                        "escalation": "Sun 13:00"})
check("8 PM appointment: follow-up exactly 12 h before (8 AM -> 9 AM), owner alert 5 PM",
      times("2026-11-12T14:00", "2026-11-15T20:00") == {"reminder": "Sat 20:00", "followup": "Sun 09:00",
                                                        "escalation": "Sun 17:00"})
check("10 AM appointment: follow-up the evening before, owner alert 9 AM",
      times("2026-11-12T14:00", "2026-11-14T10:00") == {"reminder": "Fri 10:00", "followup": "Fri 20:30",
                                                        "escalation": "Sat 09:00"})

print("25) Appointment deleted in Google Calendar -> no reminder, marked cancelled")
gone = add_booking(salon_id, "p24", "Asad", "Haircut", OPEN_DAY, "12:30")
EVENTS.pop(db.get_booking(gone)["event_id"])
SENT.clear()
reminder.send_reminder(gone)
check("no message sent, booking marked cancelled", not SENT and db.get_booking(gone)["status"] == "cancelled")

print("26) Customer asks for a person -> owner gets WhatsApp + email with the conversation")
db.update_business(salon_id, owner_email="owner@glow.pk")
SENT.clear(); EMAILS.clear()
chat(SALON, "p30", "appointment book karni hai", "haircut")
out = chat(SALON, "p30", "mujhe owner se baat karni hai")
check("customer told the owner will contact them", "owner ko bata diya" in out)
owner_msgs = [m for p, m, _ in SENT if p == "03001111111"]
check("owner WhatsApp alert names the reason and the business",
      len(owner_msgs) == 1 and "Customer wants to talk to a person" in owner_msgs[0] and "Glow Salon" in owner_msgs[0])
check("owner email sent with the recent conversation",
      len(EMAILS) == 1 and EMAILS[0][0] == "owner@glow.pk" and "haircut" in EMAILS[0][2]
      and "mujhe owner se baat karni hai" in EMAILS[0][2])
esc = db.list_escalations(salon_id, status="open")
check("escalation saved as open, both channels marked",
      esc and esc[0]["kind"] == "human_request" and esc[0]["notified_whatsapp"] and esc[0]["notified_email"])
check("booking in progress is kept", session(SALON, "p30")["intent"] == "book_appointment")
out = chat(SALON, "p30", "kisi insaan se baat karwao")
p30_cases = [e for e in db.list_escalations(salon_id) if e["phone"] == "p30"]
check("asking again doesn't alert the owner twice", "pehle hi bata diya" in out and len(EMAILS) == 1
      and len(p30_cases) == 1)
check("owner can resolve it", db.resolve_escalation(salon_id, esc[0]["id"])
      and db.get_escalation(esc[0]["id"])["status"] == "resolved")
check("another business can't resolve it", not db.resolve_escalation(clinic_id, esc[0]["id"]))

print("27) Every message is backed up, in order")
log = db.list_messages(salon_id, "p30")
check("customer messages and AURA replies saved in order",
      [m["direction"] for m in log[:4]] == ["in", "out", "in", "out"] and log[0]["text"] == "appointment book karni hai")

print("28) Unconfirmed reminder -> owner escalation with the booking")
EMAILS.clear()
late = add_booking(salon_id, "p31", "Kamran", "Haircut", OPEN_DAY, "15:00")
reminder.send_reminder(late)
check("reminder saved to the conversation with its booking",
      any(m["kind"] == "reminder" and m["booking_id"] == late for m in db.list_messages(salon_id, "p31")))
reminder.escalate_unconfirmed(late)
esc = [e for e in db.list_escalations(salon_id, status="open") if e["phone"] == "p31"]
check("unconfirmed escalation saved with booking and emailed",
      esc and esc[0]["kind"] == "unconfirmed" and esc[0]["booking_id"] == late and len(EMAILS) == 1
      and "Kamran" in EMAILS[0][1])

print("30) Owner's questions: required / optional, answer types checked")
db.update_business(salon_id, custom_questions=[
    {"question": "Aap ki email?", "type": "email", "required": False},
    {"question": "Aap ki age?", "type": "number", "required": True},
])
out = chat(SALON, "p50", "appointment book karni hai", "haircut", f"{OPEN_DAY_NAME} 4 baje", "Zara")
check("optional question asked with the skip hint", "Aap ki email?" in out and "skip" in out)
out = chat(SALON, "p50", "zara at gmail")
check("bad email asked again with a hint", "Sahi email" in out and session(SALON, "p50")["answers"] == [])
out = chat(SALON, "p50", "zara.k@gmail.com")
check("valid email accepted, next (required) question asked", "Aap ki age?" in out and "skip" not in out)
out = chat(SALON, "p50", "skip")
check("required question can't be skipped", "zaroori" in out)
out = chat(SALON, "p50", "22 saal")
check("summary shows the answers", "Aap ki email? zara.k@gmail.com" in out and "Aap ki age? 22" in out)
chat(SALON, "p50", "haan")
saved = db.upcoming_booking(salon_id, "p50")
check("answers saved with the booking", saved and saved["answers"] == {"Aap ki email?": "zara.k@gmail.com",
                                                                        "Aap ki age?": "22"})
out = chat(SALON, "p51", "appointment book karni hai", "haircut", f"{OPEN_DAY_NAME} 5 baje", "Omar", "skip", "30")
check("skipped optional question shown as '—' in the summary", "Aap ki email? —" in out)
chat(SALON, "p51", "haan")
saved = db.upcoming_booking(salon_id, "p51")
check("skipped answer saved empty", saved and saved["answers"] == {"Aap ki email?": "", "Aap ki age?": "30"})
db.update_business(salon_id, custom_questions=[])

print("31) Returning customer: name reused, but AURA says so and it can be changed")
add_booking(salon_id, "p60", "Hassan", "Haircut", OPEN_DAY, "11:00")
out = chat(SALON, "p60", f"{OPEN_DAY_NAME} 3 baje nail polish book kar do")
check("summary shows the remembered name with how to change it",
      "Naam: Hassan (pichli booking wala naam" in out and "naam Ali" in out)
out = chat(SALON, "p60", "naam Bilal hai")
check("customer changes the name; the note disappears", "Naam: Bilal" in out and "pichli booking" not in out)
chat(SALON, "p60", "haan")
check("booking saved under the new name",
      any(b["customer_name"] == "Bilal" for b in db.upcoming_bookings(salon_id, "p60")))
out = chat(SALON, "p61", f"{OPEN_DAY_NAME} 3 baje haircut book kar do")
check("new customer is asked for their name", "Apna naam batayein" in out)

print("29) Goodbye / thanks in the middle of a booking ends it politely")
out = chat(SALON, "p40", "appointment book karni hai", "haircut", "bye")
check("'bye' mid-booking ends the conversation politely",
      session(SALON, "p40") is None and "Booking abhi nahi hui" in out)
out = chat(SALON, "p41", "appointment book karni hai", "haircut", f"{OPEN_DAY_NAME} 4 baje", "thank you")
check("'thank you' at the name question is not taken as a name, nothing booked",
      session(SALON, "p41") is None and not db.upcoming_booking(salon_id, "p41"))
out = chat(SALON, "p42", "appointment book karni hai", "haircut", f"{OPEN_DAY_NAME} 4 baje", "Hamza", "shukriya")
check("'shukriya' at the confirm step ends without booking",
      session(SALON, "p42") is None and not db.upcoming_booking(salon_id, "p42") and "Booking abhi nahi hui" in out)
out = chat(SALON, "p43", "appointment book karni hai", "haircut", f"{OPEN_DAY_NAME} 5 baje", "Raza", "ok thanks")
check("'ok thanks' at the confirm step still confirms", db.upcoming_booking(salon_id, "p43") is not None)
add_booking(salon_id, "p44", "Nida", "Haircut", OPEN_DAY, "12:00")
add_booking(salon_id, "p44", "Nida", "Nail polish", OPEN_DAY, "14:00")
chat(SALON, "p44", "booking cancel karni hai")
out = chat(SALON, "p44", "Allah Hafiz")
check("'Allah Hafiz' while choosing which booking to cancel changes nothing",
      "barqarar" in out and len(db.upcoming_bookings(salon_id, "p44")) == 2)

print("33) Calendar can't be read -> owner gets the request, customer is never stuck in a loop")
agent.get_busy = lambda d, cal=None: None             # e.g. calendar not shared with AURA (Google 404)
EMAILS.clear()
out = chat(SALON, "p80", "appointment book karni hai", "haircut", f"{OPEN_DAY_NAME} 11 baje")
cases = db.list_escalations(salon_id, status="open")
check("customer told the owner will confirm, with the request details",
      "owner ko bhej di hai" in out and "Haircut" in out)
check("owner alerted with what was requested", any(c["phone"] == "p80" and c["kind"] == "calendar_unavailable"
                                                   and "Haircut" in c["reason"] for c in cases))
check("session ended (no retry loop)", session(SALON, "p80") is None)
out = chat(SALON, "p80", "Salam")
check("next message gets a normal answer, not the same calendar error", "calendar" not in out.lower()
      and "AURA" in out)
chat(SALON, "p80", "appointment book karni hai", "haircut", f"{OPEN_DAY_NAME} 12 baje")
check("asking again doesn't alert the owner twice",
      len([c for c in db.list_escalations(salon_id, status="open") if c["phone"] == "p80"]) == 1)
agent.get_busy = lambda d, cal=None: list(BUSY.get(d, []))

real_book = agent.book_appointment
agent.book_appointment = lambda **kw: {"success": False, "error": "403 Forbidden"}
out = chat(SALON, "p81", "appointment book karni hai", "haircut", f"{OPEN_DAY_NAME} 5 baje", "Sana", "haan")
check("Google refuses the booking at the end -> owner gets it, customer told",
      "owner ko bhej di hai" in out and session(SALON, "p81") is None
      and any(c["phone"] == "p81" for c in db.list_escalations(salon_id, status="open")))
agent.book_appointment = real_book

print("34) Words like 'ok' / 'hi' / 'haan' are not taken as the customer's name")
for n, reply in enumerate(("Ok", "haan ji", "Ab check karein", "12345")):
    customer = f"p82-{n}"                         # a fresh customer each: 3 unusable replies hand over to the owner
    chat(SALON, customer, "appointment book karni hai", "haircut", f"{OPEN_DAY_NAME} 1 baje")
    out = chat(SALON, customer, reply)
    check(f"'{reply}' is not a name -> asked again with an example",
          "Ali Khan" in out and not session(SALON, customer)["collected"]["name"])
out = chat(SALON, "p82-0", "Aslam")
check("then 'Aslam' (a real name) is accepted", "Naam: Aslam" in out)

print("35) Never stuck: 'menu' always restarts; the same reply 3 times mid-request breaks the loop")
out = chat(SALON, "p90", "appointment book karni hai", "haircut", "menu")
check("'menu' mid-booking restarts and shows the menu",
      "nayi shuruaat" in out and "• Appointment book karna" in out and session(SALON, "p90") is None)
chat(SALON, "p90", "appointment book karni hai", "haircut", f"{OPEN_DAY_NAME} 1 baje")
out = chat(SALON, "p90", "Start over")
check("'Start over' while AURA waits for the name restarts too", "nayi shuruaat" in out and session(SALON, "p90") is None)
out = chat(SALON, "p90", "menu wala haircut kitne ka hai?")
check("'menu' inside a longer message is not a restart", "nayi shuruaat" not in out)

chat(SALON, "p91", "appointment book karni hai")                # a request is now in progress
real_respond = agent._respond
agent._respond = lambda business, phone, message: "Maaf kijiye, abhi kuch masla hai."   # simulated bug: same reply
first, second = chat(SALON, "p91", "Hi"), chat(SALON, "p91", "Ok")
third = chat(SALON, "p91", "Hello?")
agent._respond = real_respond
check("same reply twice is still sent (could be legitimate)", first == second == "Maaf kijiye, abhi kuch masla hai.")
check("third identical reply -> loop broken, conversation restarted with the menu",
      "atak gayi" in third and "• Appointment book karna" in third and session(SALON, "p91") is None)
check("owner told the conversation got stuck", any(c["phone"] == "p91" and "stuck" in c["reason"]
                                                  for c in db.list_escalations(salon_id, status="open")))
out = chat(SALON, "p91", "rates?")
check("after the reset AURA answers normally", "Hamari services aur rates" in out)

replies = [chat(SALON, "p92", "Timings") for _ in range(4)]
check("asking the same thing outside a request isn't treated as a loop",
      all(r.startswith("🕒 *Timings:*") for r in replies))

print("32) Entity model: details read locally, Gemini only for what it couldn't read")
NER["on"] = True
first = f"{OPEN_DAY_NAME} 3 baje haircut book kar do, mera naam Ali hai"
NER["out"] = {first: {"date": OPEN_DAY_NAME, "time": "3 baje", "service": "haircut", "name": "Ali"}}
GEMINI_CALLS.clear()
out = chat(SALON, "p70", first)
check("all details from one message, straight to the summary, no Gemini call",
      "confirm kar doon" in out and "Naam: Ali" in out and "Haircut" in out and not GEMINI_CALLS)

msg = "baal katwane ki appointment chahiye"
NER["out"] = {msg: {"service": "baal katwane"}}
GEMINI_CALLS.clear()
chat(SALON, "p71", msg, gemini={msg: {"service": "haircut"}})
check("service in words the list doesn't use -> Gemini's term matched (Haircut), asked once",
      session(SALON, "p71")["collected"]["service"] == "Haircut" and len(GEMINI_CALLS) == 1)

msg = "15 Rabi ul Awal ko haircut ki appointment"
NER["out"] = {msg: {"date": "15 Rabi ul Awal", "service": "haircut"}}
GEMINI_CALLS.clear()
chat(SALON, "p72", msg)
check("a date the parser can't read -> Gemini is asked", len(GEMINI_CALLS) == 1)

msg = "mera naam Ali Khan hai"
NER["out"] = {msg: {"name": "Ali Khan"}}
chat(SALON, "p73", "appointment book karni hai", "haircut", f"{OPEN_DAY_NAME} 4 baje")
GEMINI_CALLS.clear()
out = chat(SALON, "p73", msg)
check("'mera naam Ali Khan hai' -> name Ali Khan without Gemini", "Naam: Ali Khan" in out and not GEMINI_CALLS)

msg = "main Usman hoon"
NER["out"] = {msg: {"name": "Usman"}}
out = chat(SALON, "p73", msg)
check("name changed at the summary from 'main Usman hoon'", "Naam: Usman" in out)
NER.update(on=False, out={})

print("36) Booking emails to the customer, from the business's own email")
from app.services import customer_mail  # noqa: E402

MAILS = []                      # (to, subject, body, account) -- nothing really sent


class _NowThread:               # run the background send at once, so the test can see it
    def __init__(self, target, args=(), **kw):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


customer_mail.threading = types.SimpleNamespace(Thread=_NowThread)
customer_mail.send_email = lambda to, subject, body, account=None, **kw: MAILS.append((to, subject, body, account)) or True
mail_id = db.create_business("Mail Salon", "mailsalon", "secret4", whatsapp_session="aura_4_test",
                             open_days=[0, 1, 2, 3, 4, 5], open_time="10:00", close_time="20:00",
                             mail_from="mail.salon@gmail.com", mail_password="abcdefghijklmnop")
db.replace_services(mail_id, [{"name": "Haircut", "price": 800, "duration_min": 30}])
MAIL = db.get_business(mail_id)
check("the App Password never leaves the database with the business",
      "mail_password" not in MAIL and MAIL["mail_ready"] and MAIL["asks_email"])
out = chat(MAIL, "m1", f"{OPEN_DAY_NAME} 3 baje haircut book kar do", "Bilal")
check("after the name, AURA asks for the email (optional)", "Confirmation email ke liye apna email" in out and "skip" in out)
out = chat(MAIL, "m1", "bilal at gmail")
check("not an email -> asks again with an example", "Sahi email likhein" in out)
out = chat(MAIL, "m1", "ye lo Bilal@Gmail.com")
check("email in the summary (cleaned)", "confirm kar doon" in out and "• Email: bilal@gmail.com" in out)
out = chat(MAIL, "m1", "haan")
booked = db.upcoming_booking(mail_id, "m1")
check("booked; the reply says the confirmation email went out",
      "confirm ho gayi" in out and "📧 Confirmation email: bilal@gmail.com" in out
      and booked["customer_email"] == "bilal@gmail.com")
check("confirmation email: to the customer, from the business's own address, with the details",
      len(MAILS) == 1 and MAILS[0][0] == "bilal@gmail.com" and MAILS[0][1].startswith("Appointment confirmed: Haircut")
      and MAILS[0][3]["user"] == "mail.salon@gmail.com" and "Mail Salon" in MAILS[0][3]["sender"]
      and "Rs 800" in MAILS[0][2] and "3:00 PM" in MAILS[0][2] and "Bilal" in MAILS[0][2])
MAILS.clear()
reminder.send_reminder(booked["id"])
check("reminder email together with the WhatsApp reminder", len(MAILS) == 1 and MAILS[0][1].startswith("Reminder: Haircut"))
MAILS.clear()
out = chat(MAIL, "m1", f"meri appointment {OPEN_DAY_NAME} 5 baje shift kar dein")
check("moving a booking never asks for the email again", "shift kar doon" in out and "Email: bilal@gmail.com" in out)
chat(MAIL, "m1", "haan")
check("'moved' email with the old and the new time", len(MAILS) == 1 and MAILS[0][1].startswith("Appointment moved")
      and "3:00 PM" in MAILS[0][2] and "5:00 PM" in MAILS[0][2])
MAILS.clear()
chat(MAIL, "m1", "booking cancel karni hai", "haan")
check("cancelling on WhatsApp -> cancellation email", len(MAILS) == 1
      and MAILS[0][1].startswith("Appointment cancelled") and db.upcoming_booking(mail_id, "m1") is None)
MAILS.clear()
out = chat(MAIL, "m1", f"{OPEN_DAY_NAME} 4 baje haircut book kar do")
check("returning customer: name and email remembered, not asked again",
      "confirm kar doon" in out and "Email: bilal@gmail.com" in out)
out = chat(MAIL, "m1", "nahi, email bilal.new@gmail.com kar do")
check("email can be changed at the summary", "Email: bilal.new@gmail.com" in out)
chat(MAIL, "m1", "haan")
check("confirmation goes to the new address", MAILS and MAILS[-1][0] == "bilal.new@gmail.com")
MAILS.clear()
from app.services import manage  # noqa: E402
manage.cancel_appointment = fake_cancel
result = manage.cancel_booking(MAIL, db.upcoming_booking(mail_id, "m1")["id"], notify_customer=False)
check("owner cancels on the dashboard -> the customer gets the cancellation email",
      result["error"] is None and len(MAILS) == 1 and MAILS[0][0] == "bilal.new@gmail.com"
      and MAILS[0][1].startswith("Appointment cancelled"))
MAILS.clear()
chat(MAIL, "m2", f"{OPEN_DAY_NAME} 6 baje haircut book kar do", "Zara")
out = chat(MAIL, "m2", "skip")
check("'skip' -> no email, straight to the summary", "confirm kar doon" in out and "Email" not in out)
out = chat(MAIL, "m2", "haan")
check("no email given -> booked, nothing emailed", "confirm ho gayi" in out and "📧" not in out and not MAILS)
db.update_business(mail_id, ask_email=0)
out = chat(db.get_business(mail_id), "m3", f"{OPEN_DAY_NAME} 7 baje haircut book kar do", "Hina")
check("business turned the question off -> not asked", "confirm kar doon" in out and "email" not in out.lower())
chat(MAIL, "m3", "nahi rehne do")
out = chat(SALON, "m4", f"{OPEN_DAY_NAME} 11 baje haircut book kar do", "Ali")
check("business without its own email set up -> never asks", "confirm kar doon" in out and "email" not in out.lower())

passed = sum(c for _, c in checks)
print(f"\n{passed}/{len(checks)} checks passed")
sys.exit(0 if passed == len(checks) else 1)
