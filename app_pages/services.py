import pandas as pd
import streamlit as st

from app import ui
from app.services import db, validation
from app.services.business import price_list_message
from app.services.datetime_parser import today_pkt

business = ui.current_business()
bid = business["id"]

st.caption("Customers can only book these services. AURA quotes these prices and blocks the duration in your "
           "calendar. Add a service in the empty row at the bottom; tick **Delete** to remove one, then save.")

services = db.list_services(bid)
frame = pd.DataFrame({
    "name": pd.Series([s["name"] for s in services], dtype="string"),
    "price": pd.Series([s["price"] for s in services], dtype="Int64"),
    "duration_min": pd.Series([s["duration_min"] for s in services], dtype="Int64"),
    "remove": pd.Series([False] * len(services), dtype="bool"),
})

# A new key after each save resets the editor to what is now in the database.
version = st.session_state.get("svc_version", 0)
with st.container(border=True):
    st.subheader("Your services", icon=":material/sell:")
    edited = st.data_editor(
        frame, num_rows="dynamic", hide_index=True, key=f"svc_editor_{version}", height="content",
        alt="Services, prices and durations",
        column_config={
            "name": st.column_config.TextColumn("Service", required=True, max_chars=validation.SERVICE_NAME_MAX,
                                                width=320, help="e.g. Haircut, Dental checkup"),
            "price": st.column_config.NumberColumn("Price (Rs)", required=True, min_value=0,
                                                   max_value=validation.PRICE_MAX, step=50, format="localized"),
            "duration_min": st.column_config.NumberColumn("Duration (min)", required=True,
                                                          min_value=validation.DURATION_MIN,
                                                          max_value=validation.DURATION_MAX, step=5,
                                                          default=30),
            "remove": st.column_config.CheckboxColumn("Delete", default=False, width="small",
                                                      help="Tick to remove this service when you save."),
        },
    )
    kept = [r for r in edited.to_dict("records") if not r.get("remove")]
    clean, errors = validation.services(kept)
    changed = clean != [{"name": s["name"], "price": s["price"], "duration_min": s["duration_min"]}
                        for s in services]
    with st.container(horizontal=True, vertical_alignment="center"):
        save = st.button("Save services", type="primary", icon=":material/save:",
                         disabled=not changed or bool(errors))
        if changed and not errors:
            st.caption("You have unsaved changes.")
    for e in errors:
        st.error(e, icon=":material/error:")

    if save:
        clean, errors = validation.services(kept)                            # check again before writing
        if not errors:
            db.replace_services(bid, clean)
            st.session_state.svc_version = version + 1
            ui.flash(f"Saved {len(clean)} service(s).")
            st.rerun()

with st.container(border=True):
    st.subheader("What customers see", icon=":material/chat:")
    st.caption("AURA's reply when someone asks for your rates:")
    if clean:
        with st.chat_message("assistant", avatar=":material/support_agent:"):
            st.markdown(ui.whatsapp_markdown(price_list_message(clean)))
    else:
        st.caption("No services yet — AURA can't take bookings until you add at least one.")
    removed = {s["name"].lower() for s in services} - {c["name"].lower() for c in clean}
    upcoming = db.list_bookings(bid, from_date=today_pkt().isoformat()) if removed else []
    affected = [b for b in upcoming if (b["service"] or "").lower() in removed]
    if affected:
        st.warning(f"{len(affected)} upcoming booking(s) use a service you are removing. "
                   "They stay booked; only new bookings are affected.", icon=":material/warning:")
