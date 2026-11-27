import sqlite3
from datetime import datetime

import pandas as pd
import streamlit as st

from app import ui, whatsapp_panel
from app.services import customer_mail, db, validation
from app.services.business import SHORT_DAYS, hours_text
from app.services.questions import TYPES as QUESTION_TYPES

business = ui.current_business()
bid = business["id"]
admin = ui.is_admin()
k = f"set_{bid}_"                               # widget keys per business (admin can switch businesses)


def _time(hhmm: str):
    return datetime.strptime(hhmm, "%H:%M").time()


if ui.calendar_warning(bid):
    st.warning(ui.calendar_warning(bid), title="AURA can't use this business's Google Calendar yet",
               icon=":material/calendar_month:")

with st.form(f"settings_{bid}", border=False):
    with st.container(border=True):
        st.subheader("Business", icon=":material/storefront:")
        c1, c2 = st.columns(2)
        locked = None if admin else "Only the AURA admin can change the business name and type."
        name = c1.text_input("Business name", business["name"], max_chars=validation.NAME_MAX, required=True,
                             key=k + "name", disabled=not admin, help=locked)
        business_type = c2.text_input("Type of business", business.get("business_type") or "", max_chars=40,
                                      placeholder="Salon, Dental clinic, Tutor, Repair shop…", key=k + "type",
                                      disabled=not admin, help=locked)
        greeting = st.text_area("Greeting message", business.get("greeting") or "", max_chars=validation.GREETING_MAX,
                                placeholder=f"Assalamualaikum! {business['name']} mein khush aamdeed.",
                                help="AURA's first line when a customer says hi. Leave empty for the default.",
                                key=k + "greeting")

    with st.container(border=True):
        st.subheader("Working hours", icon=":material/schedule:")
        days = st.pills("Open on", SHORT_DAYS, selection_mode="multi",
                        default=[SHORT_DAYS[d] for d in business["open_days"]], key=k + "days")
        c1, c2 = st.columns(2)
        open_time = c1.time_input("Opens at", _time(business["open_time"]), step=1800, format="12h", key=k + "open")
        close_time = c2.time_input("Closes at", _time(business["close_time"]), step=1800, format="12h",
                                   key=k + "close")
        st.caption("AURA only offers slots inside these hours and never books on closed days.")

    with st.container(border=True):
        st.subheader("What AURA asks the customer", icon=":material/quiz:")
        st.caption("AURA always asks the customer's **name** (required) and takes their **phone number** from "
                   f"WhatsApp automatically. Add up to {validation.MAX_QUESTIONS} more questions below. Tick "
                   "**Required** if the customer must answer; optional ones can be skipped by replying 'skip'. "
                   "AURA checks Email, Number and Phone number answers and asks again if one is wrong. "
                   "Tick **Delete** to remove a question, then save. The answers appear with each booking.")
        saved_questions = business.get("custom_questions") or []
        questions_table = pd.DataFrame({
            "question": pd.Series([q["question"] for q in saved_questions], dtype="string"),
            "type": pd.Series([QUESTION_TYPES[q["type"]] for q in saved_questions], dtype="object"),
            "required": pd.Series([q["required"] for q in saved_questions], dtype="bool"),
            "remove": pd.Series([False] * len(saved_questions), dtype="bool"),
        })
        questions = st.data_editor(
            questions_table, num_rows="dynamic", hide_index=True, key=k + "questions", height="content",
            alt="Extra questions AURA asks for every booking",
            column_config={
                "question": st.column_config.TextColumn("Question", required=True, width="large",
                                                        max_chars=validation.QUESTION_MAX,
                                                        help="Written the way AURA should ask it, e.g. Aap ki age kya hai?"),
                "type": st.column_config.SelectboxColumn("Answer type", options=list(QUESTION_TYPES.values()),
                                                         required=True, default="Text", width="small"),
                "required": st.column_config.CheckboxColumn("Required", default=True, width="small"),
                "remove": st.column_config.CheckboxColumn("Delete", default=False, width="small",
                                                          help="Tick to remove this question when you save."),
            },
        )

    with st.container(border=True):
        st.subheader("Alerts", icon=":material/notifications:")
        owner_phone = st.text_input("Owner's WhatsApp number", business.get("owner_phone") or "", max_chars=20,
                                    placeholder="0300 1234567", type="phone", key=k + "phone",
                                    validate=(validation.PHONE_RE, "Enter a Pakistani mobile number, e.g. 0300 1234567."),
                                    help="AURA alerts this number when a customer needs a human or doesn't confirm. "
                                         "Use a different number from the business's AURA WhatsApp.")
        owner_email = st.text_input("Owner's email (for alerts)", business.get("owner_email") or "", max_chars=120,
                                    type="email", required=True, placeholder="owner@gmail.com", key=k + "email",
                                    validate=(validation.EMAIL_RE, "Enter a valid email address."),
                                    help="Escalations are emailed here with the recent conversation.")

    if admin:
        with st.container(border=True):
            st.subheader("Connection (admin only)", icon=":material/link:")
            c1, c2 = st.columns(2)
            calendar = c1.text_input("Google Calendar ID", business.get("calendar_id") or "", max_chars=200,
                                     required=True, validate=(validation.CALENDAR_RE, "Looks like an email address."),
                                     help=ui.calendar_share_help(),
                                     key=k + "calendar")
            username = c2.text_input("Owner login username", business["username"], max_chars=30, required=True,
                                     validate=(validation.USERNAME_RE, "3–30 lowercase letters, numbers, '.' or '_'."),
                                     key=k + "username")
            active = c1.toggle("Active (AURA answers this business's customers)", bool(business.get("active")),
                               key=k + "active")

    submitted = st.form_submit_button("Save settings", type="primary", icon=":material/save:")

if submitted:
    errors, fields = [], {}

    def take(field, result):
        value, error = result
        if error:
            errors.append(error)
        else:
            fields[field] = value

    if admin:                                    # owners can't change the name or type (checked here too)
        take("name", validation.business_name(name))
        fields["business_type"] = validation.clean_text(business_type)[:40]
    take("greeting", validation.greeting(greeting))
    take("custom_questions", validation.questions([q for q in questions.to_dict("records") if not q.get("remove")]))
    take("owner_phone", validation.phone(owner_phone))
    take("owner_email", validation.email(owner_email, required=True))
    open_days = sorted(SHORT_DAYS.index(d) for d in (days or []) if d in SHORT_DAYS)
    error = validation.hours(open_days, open_time, close_time)
    if error:
        errors.append(error)
    else:
        fields.update(open_days=open_days, open_time=open_time.strftime("%H:%M"),
                      close_time=close_time.strftime("%H:%M"))

    if admin:
        take("calendar_id", validation.calendar_id(calendar, required=True))
        take("username", validation.username(username, taken=db.username_taken(username, bid)))
        fields["active"] = int(bool(active))

    if errors:
        for e in errors:
            st.error(e, icon=":material/error:")
    else:
        try:
            db.update_business(bid, **fields)
            if admin and not fields.get("active", 1):
                db.delete_business_sessions(bid)        # deactivated: log the owner out
            for key in [key for key in st.session_state if str(key).startswith(k)]:
                del st.session_state[key]               # fields reload with the saved, cleaned values
            if admin and fields.get("calendar_id") != business.get("calendar_id"):
                ui.check_calendar_after_save(db.get_business(bid))   # new calendar: can AURA use it?
            ui.flash("Settings saved.")
            st.rerun()
        except sqlite3.IntegrityError:
            st.error("That username is already used by another business.")

st.caption(f"Saved hours, as AURA tells customers: {hours_text(business)}")

whatsapp_panel.show(business)

# ----------------------------------------------------------------- booking emails to customers
with st.container(border=True):
    st.subheader("Booking emails to customers", icon=":material/mail:")
    st.caption("AURA emails customers **from the business's own address**: the booking confirmation, the reminder, "
               "and a note when a booking is moved or cancelled. During booking it asks for their email (optional). "
               "Gmail: use an **App Password** (Google Account → Security → 2-Step Verification → App passwords), "
               "not the normal password.")
    if business.get("mail_ready"):
        st.markdown(f"Sending from :green[:material/check_circle: {business['mail_from']}]")
    else:
        st.markdown(":orange[:material/warning: Not set up]: customers only get WhatsApp messages.")
    with st.form(f"mail_{bid}", border=False):
        c1, c2 = st.columns(2)
        mail_from = c1.text_input("Business email", business.get("mail_from") or "", max_chars=120, type="email",
                                  placeholder="glowsalon@gmail.com", key=k + "mail_from",
                                  help="Customers see this address as the sender. Leave empty to turn emails off.")
        mail_password = c2.text_input("App password", type="password", max_chars=100, autocomplete="new-password",
                                      key=k + "mail_password",
                                      placeholder="Leave empty to keep the saved one" if business.get("mail_ready")
                                      else "16 letters from Google")
        c3, c4 = st.columns([3, 1])
        mail_host = c3.text_input("Mail server", business.get("mail_host") or "smtp.gmail.com", max_chars=120,
                                  key=k + "mail_host")
        mail_port = c4.text_input("Port", business.get("mail_port") or "587", max_chars=5, key=k + "mail_port")
        ask_email = st.toggle("Ask customers for their email during booking (they can skip it)",
                              bool(business.get("ask_email", 1)), key=k + "ask_email",
                              help="Off: only customers who answer one of your own email questions get emails.")
        save_mail = st.form_submit_button("Save email settings", icon=":material/save:")

    if save_mail:
        errors: list = []
        mail_fields: dict = {"ask_email": int(bool(ask_email))}
        address, error = validation.email(mail_from)
        errors += [error] if error else []
        if address:
            server, error = validation.smtp_server(mail_host, mail_port)
            errors += [error] if error else []
            if server:
                mail_fields.update(mail_from=address, mail_host=server[0], mail_port=server[1])
            if mail_password.strip():
                value, error = validation.app_password(mail_password)
                errors += [error] if error else []
                mail_fields["mail_password"] = value
            elif not business.get("mail_ready") or address != business.get("mail_from"):
                errors.append("Enter the App Password for this email address.")
        elif not errors:                                 # address emptied: booking emails off, password forgotten
            mail_fields.update(mail_from="", mail_password="", mail_host="", mail_port="")
        if errors:
            for e in errors:
                st.error(e, icon=":material/error:")
        else:
            db.update_business(bid, **mail_fields)
            for key in [key for key in st.session_state if str(key).startswith(k + "mail_")]:
                del st.session_state[key]                # the password field empties; the rest reloads
            ui.flash("Email settings saved. Use 'Send test email' to try them." if address
                     else "Booking emails turned off.")
            st.rerun()

    if st.button("Send test email", icon=":material/send:", key=k + "mail_test",
                 disabled=not business.get("mail_ready"),
                 help="Sends a short test email from the business's address to itself."):
        with st.spinner("Sending…"):
            ok, message = customer_mail.send_test(business)
        (st.success if ok else st.error)(message, icon=":material/check_circle:" if ok else ":material/error:")

if admin:
    with st.container(border=True):
        st.subheader("Check Google Calendar", icon=":material/network_check:")
        st.caption("Uses the saved calendar above. Read-only: nothing is booked.")
        if st.button("Check Google Calendar", icon=":material/calendar_month:", key=k + "chk_cal"):
            ok, message = ui.check_calendar_after_save(business)
            (st.success if ok else st.error)(message)
