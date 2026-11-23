from datetime import datetime, timedelta

import streamlit as st

from app import ui
from app.services import db
from app.services.email_alert import configured as email_configured
from app.services.manage import display_phone

business = ui.current_business()
bid = business["id"]
MAX_SHOWN = 50

st.caption("Customers AURA handed over to you: they asked for a person, AURA couldn't understand them, "
           "or they didn't confirm a reminder. Contact them, then mark the case as resolved.")
if not business.get("owner_email") or not email_configured():
    st.warning("Email alerts are off: " + ("add the owner's email in Settings." if not business.get("owner_email")
                                           else "the email server isn't set up yet (Admin → Platform settings).") +
               " Alerts are still saved here and sent on WhatsApp.", icon=":material/mail:")

view = st.segmented_control("Show", ["Open", "Resolved", "All"], default="Open", key="esc_view",
                            label_visibility="collapsed")
status = {"Open": "open", "Resolved": "resolved"}.get(view or "Open")
cases = db.list_escalations(bid, status=status)

if not cases:
    st.caption("No open escalations — nothing needs your attention." if status == "open" else "Nothing here yet.")
    st.stop()

for case in cases[:MAX_SHOWN]:
    label, color = ui.ESCALATION_LABELS.get(case["kind"], (case["kind"], "gray"))
    when = datetime.fromisoformat(case["created_at"])
    customer = case["customer_name"] or "Customer"
    with st.container(border=True, key=f"esc_{case['id']}"):
        st.markdown(f":{color}-badge[{label}] **{customer}** · {display_phone(case['phone'])} · "
                    f"{when:%a %d %b}, {ui.clock(when)}"
                    + (" · :green-badge[Resolved]" if case["status"] == "resolved" else ""))
        st.caption(case["reason"])
        channels = [name for name, sent in (("WhatsApp", case["notified_whatsapp"]),
                                            ("email", case["notified_email"])) if sent]
        st.caption(f"Owner alerted by {' and '.join(channels)}." if channels
                   else ":red[Owner could not be alerted] — check the owner's number/email in Settings.")
        with st.expander("Conversation", icon=":material/forum:"):
            # from the day before the alert onwards, so the owner sees what led to it and what came after
            ui.show_conversation(bid, case["phone"], since=(when - timedelta(days=1)).isoformat(timespec="seconds"),
                                 name=case["customer_name"])
        if case["status"] == "open":
            with st.container(horizontal=True):
                if case["phone"].isdigit():
                    st.link_button("Open in WhatsApp", f"https://wa.me/{case['phone']}", icon=":material/chat:")
                if st.button("Mark resolved", icon=":material/task_alt:", key=f"esc_resolve_{case['id']}"):
                    db.resolve_escalation(bid, case["id"])
                    ui.flash("Marked as resolved.")
                    st.rerun()

if len(cases) > MAX_SHOWN:
    st.caption(f"Showing the latest {MAX_SHOWN} of {len(cases)}.")
