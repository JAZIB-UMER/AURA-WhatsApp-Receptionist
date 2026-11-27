from datetime import datetime, timedelta

import streamlit as st

from app import ui
from app.services import db, manage
from app.services.datetime_parser import PKT, describe, today_pkt

business = ui.current_business()
bid = business["id"]
today = today_pkt()
now = datetime.now(PKT).replace(tzinfo=None)

st.caption("Every booking AURA made for your business. Select a row to confirm or cancel it.")

# ----------------------------------------------------------------- filters
with st.container(horizontal=True, vertical_alignment="bottom"):
    period = st.date_input("Dates", value=(today, today + timedelta(days=30)), format="DD/MM/YYYY",
                           min_value=today - timedelta(days=365), max_value=today + timedelta(days=365),
                           key="bk_period", width=260)
    statuses = st.pills("Status", list(ui.STATUS_LABELS.values()), selection_mode="multi",
                        default=["Awaiting reply", "Confirmed"], key="bk_status")
    search = st.text_input("Search", placeholder="Name, phone or service", type="search", max_chars=60,
                           key="bk_search", icon=":material/search:", width=260)

if not isinstance(period, (tuple, list)) or len(period) != 2:
    st.info("Pick an end date to finish the date range.", icon=":material/date_range:")
    st.stop()
start, end = period
wanted = [k for k, label in ui.STATUS_LABELS.items() if label in (statuses or [])]
if not wanted:
    st.info("Pick at least one status.", icon=":material/filter_alt:")
    st.stop()

bookings = db.list_bookings(bid, from_date=start.isoformat(), to_date=end.isoformat(), statuses=wanted)
question_texts = [q["question"] for q in business.get("custom_questions") or []]
df = ui.bookings_frame(bookings, ui.price_lookup(bid), questions=question_texts)
term = (search or "").strip().lower()
if term:
    mask = (df["customer"].str.lower().str.contains(term, regex=False)
            | df["phone"].str.replace(" ", "").str.contains(term.replace(" ", ""), regex=False)
            | df["service"].str.lower().str.contains(term, regex=False))
    df = df[mask.fillna(False)].reset_index(drop=True)

# ----------------------------------------------------------------- summary + table
active = df[df["status_key"].isin(["booked", "confirmed"])]
with st.container(horizontal=True):
    st.metric("Bookings shown", len(df), border=True)
    st.metric("Customers", df["phone"].nunique(), border=True)
    st.metric("Value (active)", ui.money(active["price"].sum()), border=True,
              help="Current price of each booked service, cancelled bookings excluded.")

if df.empty:
    st.caption("No bookings match these filters.")
    st.stop()

columns = ["when", "customer", "phone", "service", "price", "status"]
answer_columns = {f"ans_{i}": q for i, q in enumerate(question_texts)}
columns += [c for c in [*answer_columns, "email"] if df[c].fillna("").str.len().gt(0).any()]   # only when used
event = st.dataframe(
    df, hide_index=True, key="bk_table", on_select="rerun", selection_mode="single-row",
    height="content",                                       # all rows shown: the page scrolls, not the table
    alt="Bookings matching the filters", column_order=columns,
    column_config={
        # Compact pixel widths so the table fits a laptop screen without a sideways scrollbar
        "when": st.column_config.DatetimeColumn("When", format="ddd D MMM, h:mm a", width=135),
        "customer": st.column_config.TextColumn("Customer", width=120),
        "phone": st.column_config.TextColumn("Phone", width=105),
        "service": st.column_config.TextColumn("Service", width=110),
        "price": st.column_config.NumberColumn("Rs", format="localized", width=60),
        "status": ui.status_column(),
        "email": st.column_config.TextColumn("Email", width=150),
        **{c: st.column_config.TextColumn(q[:24] + ("…" if len(q) > 24 else ""), width=130, help=q)
           for c, q in answer_columns.items()},
    },
)

export = (df.drop(columns=["status", "date", "details"]).rename(columns={"status_key": "status", **answer_columns}))
st.download_button("Download CSV", export.to_csv(index=False).encode("utf-8-sig"),
                   file_name=f"aura_bookings_{start}_{end}.csv", mime="text/csv",
                   icon=":material/download:", on_click="ignore")

# ----------------------------------------------------------------- actions on the selected booking
def close_cancel_dialog():
    st.session_state.pop("bk_cancel_id", None)


@st.dialog("Cancel booking", icon=":material/event_busy:", on_dismiss=close_cancel_dialog)
def cancel_dialog(booking: dict):
    st.markdown(f"**{booking['customer_name']}** — {booking['service']}, {describe(booking['date'], booking['time'])}")
    st.caption("The appointment is removed from Google Calendar and no reminders will be sent.")
    notify = st.checkbox("Send the customer a WhatsApp message about it", value=True, key="bk_notify")
    message = ""
    if notify:
        message = st.text_area("Message to the customer", manage.cancel_message(business, booking), max_chars=1000,
                               height=120, key=f"bk_cancel_msg_{booking['id']}",
                               help="Edit it if you like, e.g. to give the reason or offer another time.")
    with st.container(horizontal=True):
        confirm = st.button("Yes, cancel it", type="primary", icon=":material/delete:", key="danger_bk_cancel_yes")
        keep = st.button("Keep booking", key="bk_cancel_no")
    if keep:
        close_cancel_dialog()
        st.rerun()
    if confirm and notify and not message.strip():
        st.error("Write the message, or untick 'Send the customer a WhatsApp message'.", icon=":material/error:")
    elif confirm:
        result = manage.cancel_booking(business, booking["id"], notify, message=message)
        if result["error"]:
            st.error(result["error"])
            return
        note = "Booking cancelled."
        if not result["calendar"]:
            note += " Couldn't delete the calendar event — please remove it in Google Calendar."
        if result["notified"] is False:
            note += " WhatsApp message could not be sent."
        ui.flash(note, ":material/event_busy:")
        close_cancel_dialog()
        st.session_state.pop("bk_table", None)
        st.rerun()


rows = event.selection.rows if event else []
if rows and rows[0] < len(df):
    selected = df.iloc[rows[0]]
    booking = db.get_booking(int(selected["id"]))
    if booking and booking["business_id"] == bid:
        with st.container(border=True):
            st.markdown(f"**{booking['customer_name']}** · {booking['service']} · "
                        f"{describe(booking['date'], booking['time'])} · {selected['phone']}")
            details = [(q, a) for q, a in (booking.get("answers") or {}).items() if a]
            if booking.get("customer_email"):
                details.append(("Email", booking["customer_email"]))
            if details:
                st.table({"Question": [q for q, _ in details], "Answer": [a for _, a in details]},
                         alt="The customer's answers to the extra questions")
            is_future = selected["when"] >= now
            can_change = booking["status"] in ("booked", "confirmed") and is_future
            with st.container(horizontal=True):
                if st.button("View conversation", icon=":material/forum:", key="bk_chat", type="primary",
                             help="The WhatsApp chat in which this booking was made, plus its reminders."):
                    st.session_state.pop("bk_cancel_id", None)
                    st.session_state.bk_chat_id = booking["id"]
                if st.button("Mark as confirmed", icon=":material/check_circle:", key="ok_bk_confirm",
                             disabled=not (can_change and booking["status"] == "booked"),
                             help="Use this when the customer confirmed by phone or in person."):
                    error = manage.confirm_booking(business, booking["id"])
                    ui.flash(error or "Booking marked as confirmed.",
                             ":material/error:" if error else ":material/check_circle:")
                    st.rerun()
                if st.button("Cancel booking", icon=":material/event_busy:", key="danger_bk_cancel",
                             disabled=not can_change):
                    st.session_state.pop("bk_chat_id", None)
                    st.session_state.bk_cancel_id = booking["id"]
            if not can_change:
                st.caption("Past, cancelled or rescheduled bookings can't be changed.")

def close_chat_dialog():
    st.session_state.pop("bk_chat_id", None)


@st.dialog("Booking conversation", icon=":material/forum:", width="large", on_dismiss=close_chat_dialog)
def chat_dialog(booking: dict):
    made = datetime.fromisoformat(booking["created_at"])
    st.markdown(f"**{booking['customer_name']}** · {booking['service']} · {describe(booking['date'], booking['time'])}")
    full = st.toggle("Show the full history with this customer", key="bk_chat_full")
    # The booking chat starts before the booking was made; reminders come up to the appointment day.
    since = None if full else (made - timedelta(days=1)).isoformat(timespec="seconds")
    until = None if full else f"{booking['date']}T23:59:59"
    ui.show_conversation(bid, booking["phone"], since=since, until=until, name=booking["customer_name"],
                         mark_at=booking["created_at"])


if st.session_state.get("bk_chat_id"):
    chat_booking = db.get_booking(st.session_state.bk_chat_id)
    if chat_booking and chat_booking["business_id"] == bid:
        chat_dialog(chat_booking)
    else:
        close_chat_dialog()

# The dialog stays open across reruns until the owner confirms, keeps the booking, or closes it.
if st.session_state.get("bk_cancel_id"):
    pending = db.get_booking(st.session_state.bk_cancel_id)
    if pending and pending["business_id"] == bid and pending["status"] in ("booked", "confirmed"):
        cancel_dialog(pending)
    else:
        close_cancel_dialog()
