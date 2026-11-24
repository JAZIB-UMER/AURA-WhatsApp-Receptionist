from datetime import datetime, timedelta

import pandas as pd
import streamlit as st

from app import ui
from app.services import db
from app.services.business import hours_text
from app.services.datetime_parser import PKT, today_pkt
from app.services.email_alert import configured as email_configured

MAX_LIST = 12                                   # longer lists point to the Bookings page

business = ui.current_business()
bid = business["id"]
today = today_pkt()
now = datetime.now(PKT).replace(tzinfo=None)

st.caption(f"{business['name']} · {business.get('business_type') or 'Business'} · {hours_text(business)}")

# ----------------------------------------------------------------- setup checklist
services = db.list_services(bid)
missing = []
if not services:
    missing.append("add your **services and prices** (Services & prices) — AURA can't take bookings without them")
if not business.get("owner_phone"):
    missing.append("add the **owner's WhatsApp number** (Settings) — needed for alerts")
if not business.get("owner_email"):
    missing.append("add the **owner's email** (Settings) — escalations are emailed there")
elif not email_configured():
    missing.append("email alerts need the **email server** in Platform settings (admin)")
if not business.get("calendar_id"):
    missing.append("link a **Google Calendar** (admin)")
if not business.get("whatsapp_session"):
    missing.append("connect the business's **WhatsApp** (Settings → WhatsApp, scan the QR code)")
if not business.get("active"):
    missing.append("this business is **inactive** — AURA won't answer its customers")
if missing:
    st.warning("To finish setting up AURA:\n\n" + "\n".join(f"- {m}" for m in missing),
               title="Setup incomplete", icon=":material/construction:")

# ----------------------------------------------------------------- data
bookings = db.list_bookings(bid, from_date=(today - timedelta(days=30)).isoformat(),
                            to_date=(today + timedelta(days=30)).isoformat(), statuses=None)
df = ui.bookings_frame(bookings, ui.price_lookup(bid))
active = df[df["status_key"].isin(["booked", "confirmed"])]
week_end = today + timedelta(days=6)
upcoming = active[active["when"] >= now]
todays = active[active["date"] == today].reset_index(drop=True)
next7 = active[(active["date"] >= today) & (active["date"] <= week_end)]
awaiting = upcoming[upcoming["status_key"] == "booked"]
cancelled_30 = df[(df["status_key"] == "cancelled") & (df["date"] >= today - timedelta(days=30))]

# ----------------------------------------------------------------- KPIs
k1, k2, k3, k4 = st.columns(4)
k1.metric("Today", len(todays), border=True, height="stretch", icon=":material/today:",
          help="Active appointments today (booked or confirmed).")
k2.metric("Next 7 days", len(next7), border=True, height="stretch", icon=":material/date_range:",
          help="Active appointments from today until " + week_end.strftime("%a %d %b") + ".")
k3.metric("Awaiting reply", len(awaiting), border=True, height="stretch", icon=":material/hourglass_top:",
          help="Upcoming bookings where the customer hasn't confirmed the reminder yet.")
k4.metric("Expected revenue (7 days)", ui.money(next7["price"].sum()), border=True, height="stretch",
          icon=":material/payments:",
          help="Sum of current service prices for the next 7 days. Services that were renamed or removed count as 0.")

# ----------------------------------------------------------------- AURA's work (last 30 days)
month_ago = (datetime.now() - timedelta(days=30)).isoformat(timespec="seconds")
stats = db.message_stats(bid, since=month_ago)
open_cases = db.list_escalations(bid, status="open")
escalated = db.escalated_customers(bid, since=month_ago)
rate = f"{escalated / stats['customers']:.0%}" if stats["customers"] else "—"
m1, m2, m3, m4 = st.columns(4)
m1.metric("Customers (30 days)", stats["customers"], border=True, height="stretch", icon=":material/group:",
          help="Different people who messaged the business on WhatsApp and got answers from AURA.")
m2.metric("Bookings made (30 days)", db.bookings_made(bid, since=month_ago), border=True, height="stretch",
          icon=":material/event_available:", help="Bookings AURA created in the last 30 days (any status now).")
m3.metric("Escalation rate (30 days)", rate, border=True, height="stretch", icon=":material/support_agent:",
          help=f"{escalated} of {stats['customers']} customer(s) needed you (each customer counted once). "
               "Lower means AURA handled more on its own.")
m4.metric("Open escalations", len(open_cases), border=True, height="stretch",
          icon=":material/notification_important:", help="Cases waiting for you on the Escalations page.")

# ----------------------------------------------------------------- today + attention
left, right = st.columns([3, 2])
with left:
    with st.container(border=True, height="stretch"):
        st.subheader("Today's schedule", icon=":material/schedule:")
        if todays.empty:
            st.caption("No appointments today.")
        else:
            # A list (not a table) wraps on any screen width, so it never needs a scrollbar.
            badge = {"booked": ":orange-badge[Awaiting reply]", "confirmed": ":green-badge[Confirmed]"}
            for row in todays.head(MAX_LIST).itertuples():
                st.markdown(f"**{ui.clock(row.when)}** · {row.customer} — {row.service} {badge.get(row.status_key, '')}")
            if len(todays) > MAX_LIST:
                st.caption(f"…and {len(todays) - MAX_LIST} more on the Bookings page.")
with right:
    with st.container(border=True, height="stretch"):
        st.subheader("Needs attention", icon=":material/priority_high:")
        for case in open_cases[:3]:
            label, color = ui.ESCALATION_LABELS.get(case["kind"], (case["kind"], "gray"))
            st.markdown(f":{color}-badge[{label}] **{case['customer_name'] or 'Customer'}** · "
                        f"{datetime.fromisoformat(case['created_at']):%a} {ui.clock(datetime.fromisoformat(case['created_at']))}")
        if open_cases:
            st.page_link("app_pages/escalations.py", label=f"Open escalations ({len(open_cases)})",
                         icon=":material/arrow_forward:")
        soon = awaiting[awaiting["when"] <= now + timedelta(hours=24)]
        if soon.empty:
            if not open_cases:
                st.caption("Nothing right now — no escalations and no unconfirmed appointments in the next 24 hours.")
        else:
            st.caption("Not confirmed yet and due within 24 hours. Consider calling them.")
            for row in soon.head(MAX_LIST).itertuples():
                st.markdown(f":orange-badge[{row.when:%a} {ui.clock(row.when)}] **{row.customer}** — {row.service} · {row.phone}")
            if len(soon) > MAX_LIST:
                st.caption(f"…and {len(soon) - MAX_LIST} more on the Bookings page.")
        if len(cancelled_30):
            st.caption(f"{len(cancelled_30)} cancellation(s) in the last 30 days.")

# ----------------------------------------------------------------- charts
c1, c2 = st.columns([3, 2])
with c1:
    with st.container(border=True, height="stretch"):
        st.subheader("Bookings per day", icon=":material/bar_chart:")
        st.caption("Last 14 days and next 14 days")
        days = pd.date_range(today - timedelta(days=14), today + timedelta(days=14)).date
        window = df[df["date"].isin(days) & df["status_key"].isin(["booked", "confirmed", "cancelled"])]
        if window.empty:
            st.caption("No bookings in this period yet.")
        else:
            series = {"confirmed": "Confirmed", "booked": "Awaiting reply", "cancelled": "Cancelled"}
            counts = (window.groupby(["date", "status_key"]).size().unstack(fill_value=0)
                      .reindex(columns=list(series), fill_value=0).rename(columns=series).reset_index())
            counts["date"] = pd.to_datetime(counts["date"])
            st.bar_chart(counts, x="date", y=list(series.values()),
                         color=["#059669", "#D97706", "#DC2626"],          # same meaning as the status badges
                         x_label="", y_label="Bookings",
                         alt="Bookings per day for the last and next 14 days, by status")
with c2:
    with st.container(border=True, height="stretch"):
        st.subheader("Popular services", icon=":material/trending_up:")
        st.caption("Booked in the last 30 days and upcoming (not cancelled)")
        popular = df[df["status_key"].isin(["booked", "confirmed"])]
        if popular.empty:
            st.caption("No bookings yet.")
        else:
            top = popular.groupby("service").size().rename("Bookings").reset_index()
            st.bar_chart(top, x="service", y="Bookings", horizontal=True, sort="-Bookings",
                         x_label="", y_label="Bookings", alt="Number of bookings per service")
