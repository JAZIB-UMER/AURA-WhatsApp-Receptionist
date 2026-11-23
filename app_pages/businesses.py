import sqlite3

import pandas as pd
import streamlit as st

from app import ui
from app.services import db, validation

if not ui.is_admin():                       # page is only registered for the admin; double-check anyway
    st.error("Admins only.")
    st.stop()

businesses = db.list_businesses()
summary = db.booking_summary()

with st.container(horizontal=True):
    st.metric("Businesses", len(businesses), border=True)
    st.metric("Active", sum(1 for b in businesses if b.get("active")), border=True)
    st.metric("Upcoming bookings", sum(s["upcoming"] for s in summary.values()), border=True)
    st.metric("All-time bookings", sum(s["total"] for s in summary.values()), border=True)

with st.container(border=True):
    st.subheader("All businesses", icon=":material/storefront:")
    if not businesses:
        st.caption("No businesses yet — add the first one below.")
    else:
        table = pd.DataFrame([{
            "Business": b["name"], "Type": b.get("business_type") or "—", "Owner login": b["username"],
            "WaAPI": b.get("waapi_instance_id") or "—", "Calendar": b.get("calendar_id") or "—",
            "Services": len(db.list_services(b["id"])),
            "Upcoming": summary.get(b["id"], {}).get("upcoming", 0),
            "Active": bool(b.get("active")),
        } for b in businesses])
        st.dataframe(table, hide_index=True, height="content", alt="All businesses on AURA",
                     column_config={
                         "Business": st.column_config.TextColumn(width="medium"),
                         "Calendar": st.column_config.TextColumn(width="medium"),
                         "WaAPI": st.column_config.TextColumn(width="small"),
                         "Services": st.column_config.NumberColumn(width="small"),
                         "Upcoming": st.column_config.NumberColumn(width="small"),
                         "Active": st.column_config.CheckboxColumn(width="small"),
                     })
        st.caption("Pick a business under **Managing** in the sidebar to change its services, settings or password.")

with st.container(border=True):
    st.subheader("Add a business", icon=":material/add_business:")
    # A new form key after a successful add empties every field; after an error the typed values stay.
    form_version = st.session_state.get("add_business_version", 0)
    with st.form(f"add_business_{form_version}", border=False):
        c1, c2 = st.columns(2)
        name = c1.text_input("Business name", max_chars=validation.NAME_MAX, required=True)
        business_type = c2.text_input("Type", max_chars=40, placeholder="Salon, Clinic, Tutor…")
        username = c1.text_input("Owner login username", max_chars=30, required=True,
                                 validate=(validation.USERNAME_RE, "3–30 lowercase letters, numbers, '.' or '_'."))
        password = c2.text_input("Owner's first password", type="password", max_chars=128, required=True,
                                 autocomplete="new-password",
                                 help=f"At least {validation.PASSWORD_MIN} characters, with a letter and a number.")
        instance = c1.text_input("WaAPI instance ID", max_chars=12, required=True, placeholder="108128",
                                 validate=(validation.INSTANCE_RE, "Numbers only, e.g. 108128."),
                                 help="From the WaAPI dashboard: the instance linked to this business's WhatsApp number.")
        calendar = c2.text_input("Google Calendar ID (email)", max_chars=200, required=True,
                                 placeholder="business@gmail.com", type="email",
                                 validate=(validation.CALENDAR_RE, "Enter the calendar's email address."),
                                 help=ui.calendar_share_help())
        owner_phone = c1.text_input("Owner's WhatsApp number (optional)", max_chars=20, type="phone",
                                    placeholder="0300 1234567",
                                    validate=(validation.PHONE_RE, "Enter a Pakistani mobile number."))
        owner_email = c2.text_input("Owner's email (for alerts)", max_chars=120, type="email", required=True,
                                    placeholder="owner@gmail.com",
                                    validate=(validation.EMAIL_RE, "Enter a valid email address."))
        submitted = st.form_submit_button("Add business", type="primary", icon=":material/add:")

    if submitted:
        errors = []
        clean = {}
        for field, (value, error) in {
            "name": validation.business_name(name),
            "username": validation.username(username, taken=db.username_taken(username)),
            "password": validation.password(password),
            "waapi_instance_id": validation.instance_id(instance, required=True,
                                                        taken=db.instance_taken(validation.clean_text(instance))),
            "calendar_id": validation.calendar_id(calendar, required=True),
            "owner_phone": validation.phone(owner_phone),
            "owner_email": validation.email(owner_email, required=True),
        }.items():
            if error:
                errors.append(error)
            else:
                clean[field] = value
        if errors:
            for e in errors:
                st.error(e, icon=":material/error:")
        else:
            try:
                new_id = db.create_business(
                    clean["name"], clean["username"], clean["password"],
                    business_type=validation.clean_text(business_type)[:40],
                    waapi_instance_id=clean["waapi_instance_id"], calendar_id=clean["calendar_id"],
                    owner_phone=clean["owner_phone"], owner_email=clean["owner_email"])
            except sqlite3.IntegrityError:
                st.error("That username or WaAPI instance ID is already used by another business.")
            else:
                st.session_state.select_business = new_id      # applied before the sidebar selector is drawn
                st.session_state.add_business_version = form_version + 1
                calendar_ok, _ = ui.check_calendar_after_save(db.get_business(new_id))
                ui.flash(f"Added {clean['name']}. Next: add its services."
                         + ("" if calendar_ok else " Its Google Calendar isn't usable yet — see its Settings page."),
                         icon=":material/check_circle:" if calendar_ok else ":material/warning:")
                st.rerun()
