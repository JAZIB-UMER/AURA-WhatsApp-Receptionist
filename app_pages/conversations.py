from datetime import datetime, timedelta

import pandas as pd
import streamlit as st

from app import ui
from app.services import db, manage
from app.services.manage import display_phone

business = ui.current_business()
bid = business["id"]

st.caption("Backup of every WhatsApp chat AURA had with your customers. Select a customer to read the chat.")

with st.container(horizontal=True, vertical_alignment="bottom"):
    period = st.segmented_control("Period", ["7 days", "30 days", "All"], default="30 days", key="conv_period")
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
    "messages": pd.Series([c["messages"] for c in chats], dtype="int64"),
})
term = (search or "").strip().lower()
if term:
    table = table[table["customer"].str.lower().str.contains(term, regex=False)
                  | table["number"].str.replace(" ", "").str.contains(term.replace(" ", ""), regex=False)
                  ].reset_index(drop=True)

if table.empty:
    st.caption("No conversations in this period." if not term else "No customer matches this search.")
    st.stop()

event = st.dataframe(
    table, hide_index=True, key="conv_table", on_select="rerun", selection_mode="single-row",
    height="content", alt="Customers who messaged the business",
    column_order=["customer", "number", "last", "messages"],
    column_config={
        "customer": st.column_config.TextColumn("Customer", width=160),
        "number": st.column_config.TextColumn("Phone", width=120),
        "last": st.column_config.DatetimeColumn("Last message", format="ddd D MMM, h:mm a", width=160),
        "messages": st.column_config.NumberColumn("Messages", width=90),
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


rows = event.selection.rows if event else []
if rows and rows[0] < len(table):
    chosen = table.iloc[rows[0]]
    label = f"{chosen['customer']} · {chosen['number']}" if chosen["customer"] != "—" else str(chosen["number"])
    with st.container(border=True):
        with st.container(horizontal=True, vertical_alignment="center"):
            st.subheader(f"{chosen['customer']} · {chosen['number']}", icon=":material/forum:", width="stretch")
            if st.button("Delete conversation", icon=":material/delete:", key="conv_delete_btn"):
                st.session_state.conv_delete = {"phone": str(chosen["phone"]), "label": label}
        ui.show_conversation(bid, str(chosen["phone"]), since=since,
                             name=None if chosen["customer"] == "—" else str(chosen["customer"]))
else:
    st.caption("Select a customer above to read the conversation.")

# The dialog stays open across reruns until the owner confirms, keeps the chat, or closes it.
if st.session_state.get("conv_delete"):
    delete_dialog(st.session_state.conv_delete["phone"], st.session_state.conv_delete["label"])
