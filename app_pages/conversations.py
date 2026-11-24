from datetime import date, datetime, timedelta

import pandas as pd
import streamlit as st

from app import ui
from app.services import db, manage
from app.services.manage import display_phone

business = ui.current_business()
bid = business["id"]

st.caption("Every customer who messaged the business, once each. Select a customer to read their chats day by "
           "day, or to see all their bookings and cancellations.")

with st.container(horizontal=True, vertical_alignment="bottom"):
    period = st.segmented_control("Active in", ["7 days", "30 days", "All"], default="30 days", key="conv_period")
    search = st.text_input("Search", placeholder="Name or phone", type="search", max_chars=60,
                           key="conv_search", icon=":material/search:", width=260)

days = {"7 days": 7, "30 days": 30}.get(period or "30 days")
since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds") if days else None
chats = db.list_conversations(bid, since=since)
table = pd.DataFrame({
    "phone": pd.Series([c["phone"] for c in chats], dtype="string"),
    "customer": pd.Series([c["name"] or "—" for c in chats], dtype="string"),
    "number": pd.Series([display_phone(c["phone"]) for c in chats], dtype="string"),
    "last": pd.Series([datetime.fromisoformat(c["last_at"]) for c in chats], dtype="datetime64[ns]"),
    "bookings": pd.Series([c["bookings"] for c in chats], dtype="int64"),
})
term = (search or "").strip().lower()
if term:
    table = table[table["customer"].str.lower().str.contains(term, regex=False)
                  | table["number"].str.replace(" ", "").str.contains(term.replace(" ", ""), regex=False)
                  ].reset_index(drop=True)

if table.empty:
    st.caption("No customer messaged in this period." if not term else "No customer matches this search.")
    st.stop()

event = st.dataframe(
    table, hide_index=True, key="conv_table", on_select="rerun", selection_mode="single-row",
    height="content", alt="Customers who messaged the business",
    column_order=["customer", "number", "last", "bookings"],
    column_config={
        "customer": st.column_config.TextColumn("Customer", width=160),
        "number": st.column_config.TextColumn("Phone", width=120),
        "last": st.column_config.DatetimeColumn("Last message", format="ddd D MMM, h:mm a", width=160),
        "bookings": st.column_config.NumberColumn("Bookings", width=90, help="All bookings from this number."),
    },
)


def close_delete_dialog():
    st.session_state.pop("conv_delete", None)


@st.dialog("Delete conversation", icon=":material/delete:", on_dismiss=close_delete_dialog)
def delete_dialog(phone: str, label: str):
    count = len(db.list_messages(bid, phone, limit=100_000))
    st.markdown(f"Delete the whole chat with **{label}** ({count} message{'s' if count != 1 else ''})?")
    st.caption("This can't be undone. Their bookings and escalation records are kept. If they message again, "
               "AURA starts a fresh conversation.")
    with st.container(horizontal=True):
        confirm = st.button("Yes, delete it", type="primary", icon=":material/delete:", key="conv_delete_yes")
        keep = st.button("Keep it", key="conv_delete_no")
    if keep:
        close_delete_dialog()
        st.rerun()
    if confirm:
        deleted = manage.delete_conversation(business, phone)
        close_delete_dialog()
        st.session_state.pop("conv_table", None)            # nothing selected afterwards
        ui.flash(f"Conversation with {label} deleted ({deleted} messages).", ":material/delete:")
        st.rerun()


def chats_by_day(phone: str, name: "str | None"):
    """Pick one day; only that day's messages are shown."""
    chat_days = db.chat_days(bid, phone)
    if not chat_days:
        st.caption("No saved messages with this customer.")
        return
    counts = dict(chat_days)
    day = st.selectbox(
        "Day", list(counts), key=f"conv_day_{phone}",
        format_func=lambda d: f"{date.fromisoformat(d):%a %d %b %Y}  ·  {counts[d]} message{'s' if counts[d] != 1 else ''}",
        help="Days this customer chatted with AURA, newest first.")
    ui.show_conversation(bid, phone, since=f"{day}T00:00:00", until=f"{day}T23:59:59", name=name)


def bookings_record(phone: str, name: "str | None"):
    """All bookings of this number with a status filter; a selected booking shows the chat of the day it was made."""
    bookings = db.customer_bookings(bid, phone)
    if not bookings:
        st.caption("No bookings from this number yet.")
        return
    frame = ui.bookings_frame(bookings, ui.price_lookup(bid))
    frame["made"] = pd.Series([datetime.fromisoformat(b["created_at"]) for b in bookings], dtype="datetime64[ns]")
    totals = frame["status_key"].value_counts()
    st.caption(" · ".join(f"{label}: {totals.get(key, 0)}" for key, label in ui.STATUS_LABELS.items()))
    statuses = st.pills("Show", list(ui.STATUS_LABELS.values()), selection_mode="multi",
                        default=list(ui.STATUS_LABELS.values()), key=f"conv_status_{phone}")
    keys = [k for k, label in ui.STATUS_LABELS.items() if label in (statuses or [])]
    shown = frame[frame["status_key"].isin(keys)].reset_index(drop=True)
    if shown.empty:
        st.caption("No bookings with the chosen status.")
        return
    picked = st.dataframe(
        shown, hide_index=True, key=f"conv_bookings_{phone}", on_select="rerun", selection_mode="single-row",
        height="content", alt="All bookings of this customer",
        column_order=["when", "service", "status", "made", "details"],
        column_config={
            "when": st.column_config.DatetimeColumn("Appointment", format="ddd D MMM YYYY, h:mm a", width=170),
            "service": st.column_config.TextColumn("Service", width=120),
            "status": ui.status_column(),
            "made": st.column_config.DatetimeColumn("Booked on", format="D MMM, h:mm a", width=120),
            "details": st.column_config.TextColumn("Answers", width="medium"),
        },
    )
    rows = picked.selection.rows if picked else []
    if not rows or rows[0] >= len(shown):
        st.caption("Select a booking to read the chat from the day it was made.")
        return
    booking = db.get_booking(int(shown.iloc[rows[0]]["id"]))
    if not booking:
        return
    made = booking["created_at"][:10]
    st.markdown(f"**Chat on {date.fromisoformat(made):%A %d %b %Y}**, when this booking was made")
    ui.show_conversation(bid, phone, since=f"{made}T00:00:00", until=f"{made}T23:59:59", name=name,
                         mark_at=booking["created_at"])


rows = event.selection.rows if event else []
if rows and rows[0] < len(table):
    chosen = table.iloc[rows[0]]
    phone = str(chosen["phone"])
    name = None if chosen["customer"] == "—" else str(chosen["customer"])
    label = f"{chosen['customer']} · {chosen['number']}" if name else str(chosen["number"])
    with st.container(border=True):
        with st.container(horizontal=True, vertical_alignment="center"):
            st.subheader(label, icon=":material/person:", width="stretch")
            if st.button("Delete conversation", icon=":material/delete:", key="conv_delete_btn"):
                st.session_state.conv_delete = {"phone": phone, "label": label}
        view = st.segmented_control("View", ["Chats by day", "Bookings"], default="Chats by day", key="conv_view",
                                    label_visibility="collapsed")
        if view == "Bookings":
            bookings_record(phone, name)
        else:
            chats_by_day(phone, name)
else:
    st.caption("Select a customer above.")

# The dialog stays open across reruns until the owner confirms, keeps the chat, or closes it.
if st.session_state.get("conv_delete"):
    delete_dialog(st.session_state.conv_delete["phone"], st.session_state.conv_delete["label"])
