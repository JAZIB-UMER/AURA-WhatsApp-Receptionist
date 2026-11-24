"""Admin -> Platform settings: the API keys and accounts AURA uses for every business.

Secrets are never shown again after saving (only their last 4 characters). Leaving a key field empty
keeps the saved key. Each section has a check button that tests the saved value against the real service.
Per-business values (WhatsApp link, calendar, owner contacts) are on each business's Settings page.
"""
import smtplib

import streamlit as st

from app import ui
from app.services import config, validation
from app.services.email_alert import send_email
from app.services.nlu import check_gemini
from app.services.voice import check_groq
from app.services.whatsapp import server_status

if not ui.is_admin():                       # page is only registered for the admin; double-check anyway
    st.error("Admins only.")
    st.stop()

version = st.session_state.setdefault("platform_version", 0)   # new form keys after a save = empty secret fields


def saved_or_missing(key: str) -> str:
    return f":green[:material/check_circle: {config.masked(key)}]" if config.is_set(key) \
        else ":orange[:material/warning: not set]"


def secret_input(form, label: str, key: str, help_text: str):
    return form.text_input(label, type="password", max_chars=300, autocomplete="off", help=help_text,
                           placeholder="Leave empty to keep the saved one" if config.is_set(key) else "Paste it here")


def finish(values: dict, message: str):
    config.save(values)
    st.session_state.platform_version += 1
    ui.flash(message)
    st.rerun()


def show_errors(errors: list):
    for error in errors:
        st.error(error, icon=":material/error:")


def check_button(label: str, key: str, check):
    if st.button(label, key=key, icon=":material/network_check:"):
        with st.spinner("Checking…"):
            ok, message = check()
        (st.success if ok else st.error)(message, icon=":material/check_circle:" if ok else ":material/error:")


st.caption("Accounts AURA uses for **every** business. Each business's own WhatsApp number, calendar and "
           "owner contacts are on that business's Settings page. Changes apply within a few seconds, no restart.")

# ----------------------------------------------------------------- status at a glance
whatsapp_ok, whatsapp_message = server_status()
with st.container(horizontal=True, gap="small"):
    for label, ok in [("WhatsApp server", whatsapp_ok),
                      ("Gemini", config.is_set("gemini_api_key")),
                      ("Voice notes", config.is_set("groq_api_key")),
                      ("Google Calendar", config.is_set("google_service_account")),
                      ("Email alerts", all(config.is_set(k) for k in ("smtp_host", "smtp_user", "smtp_password")))]:
        st.badge(label, icon=":material/check:" if ok else ":material/close:", color="green" if ok else "orange")

left, right = st.columns(2, gap="medium")

# ----------------------------------------------------------------- WhatsApp
with left.container(border=True):
    st.subheader("WhatsApp server (WAHA)", icon=":material/chat:")
    (st.success if whatsapp_ok else st.error)(whatsapp_message,
                                              icon=":material/check_circle:" if whatsapp_ok else ":material/error:")
    st.caption("Free and self-hosted: it runs next to AURA (Docker container \"waha\"), so there's no account, "
               "token or monthly fee. Each business links its own WhatsApp number by scanning a QR code on "
               "its Settings page.")
    check_button("Check again", "pf_check_waha", server_status)

# ----------------------------------------------------------------- Gemini
with right.container(border=True):
    st.subheader("Gemini (understanding backup)", icon=":material/psychology:")
    st.markdown(f"API key: {saved_or_missing('gemini_api_key')}")
    with st.form(f"pf_gemini_{version}", border=False):
        key = secret_input(st, "API key", "gemini_api_key", "aistudio.google.com → Get API key.")
        model = st.text_input("Model", config.get("gemini_model"), max_chars=80, required=True,
                              help="Used when AURA's own models are unsure. Change it when Google retires a model.")
        save = st.form_submit_button("Save", type="primary", icon=":material/save:")
    if save:
        errors, values = [], {}
        name, error = validation.model_name(model, "Gemini model")
        errors += [error] if error else []
        if key.strip():
            value, error = validation.api_key(key, "Gemini API key")
            errors += [error] if error else []
            values["gemini_api_key"] = value
        if not errors:
            ok, message = check_gemini(values.get("gemini_api_key") or config.get("gemini_api_key") or None, name)
            if ok or not (values.get("gemini_api_key") or config.is_set("gemini_api_key")):
                finish({**values, "gemini_model": name}, "Gemini settings saved.")
            errors.append(f"Not saved: {message}")
        show_errors(errors)
    check_button("Check Gemini", "pf_check_gemini", check_gemini)

# ----------------------------------------------------------------- Groq / Whisper
with left.container(border=True):
    st.subheader("Voice notes (Groq Whisper)", icon=":material/mic:")
    st.markdown(f"API key: {saved_or_missing('groq_api_key')}")
    with st.form(f"pf_groq_{version}", border=False):
        key = secret_input(st, "API key", "groq_api_key", "console.groq.com → API Keys (free).")
        model = st.text_input("Whisper model", config.get("whisper_model"), max_chars=80, required=True,
                              help="whisper-large-v3 is recommended: it also supports the English fallback.")
        save = st.form_submit_button("Save", type="primary", icon=":material/save:")
    if save:
        errors, values = [], {}
        name, error = validation.model_name(model, "Whisper model")
        errors += [error] if error else []
        if key.strip():
            value, error = validation.api_key(key, "Groq API key")
            errors += [error] if error else []
            values["groq_api_key"] = value
        if not errors:
            ok, message = check_groq(values.get("groq_api_key") or None, name)
            if ok or not (values.get("groq_api_key") or config.is_set("groq_api_key")):
                finish({**values, "whisper_model": name}, "Voice note settings saved.")
            errors.append(f"Not saved: {message}")
        show_errors(errors)
    check_button("Check Groq", "pf_check_groq", check_groq)

# ----------------------------------------------------------------- Google Calendar
with right.container(border=True):
    st.subheader("Google Calendar", icon=":material/calendar_month:")
    robot = config.service_account_email(config.get("google_service_account"))
    if robot:
        st.markdown(f"Service account: :green[:material/check_circle:] `{robot}`")
        st.caption("Each business shares its Google Calendar with this email (permission: Make changes to events).")
    else:
        st.markdown("Service account: :orange[:material/warning: not set]")
        st.caption("Google Cloud → IAM → Service accounts → your account → Keys → Add key → JSON. "
                   "Enable the Google Calendar API for the project.")
    with st.form(f"pf_google_{version}", border=False):
        upload = st.file_uploader("Service account key (.json)", type=["json"], max_upload_size=1,
                                  help="Replaces the saved key. The file stays on this server.")
        save = st.form_submit_button("Save", type="primary", icon=":material/save:")
    if save:
        if upload is None:
            st.info("Choose the .json key file first.", icon=":material/info:")
        else:
            value, error = validation.service_account(upload.getvalue())
            if error:
                show_errors([error])
            else:
                finish({"google_service_account": value},
                       f"Service account saved: {config.service_account_email(value)}")

# ----------------------------------------------------------------- Email (SMTP)
with left.container(border=True):
    st.subheader("Email alerts (SMTP)", icon=":material/mail:")
    st.caption("Escalation alerts are emailed to each business owner from this account. "
               "Gmail: server smtp.gmail.com, port 587, password = an App Password "
               "(Google Account → Security → 2-Step Verification → App passwords).")
    st.markdown(f"Password: {saved_or_missing('smtp_password')}")
    with st.form(f"pf_smtp_{version}", border=False):
        c1, c2 = st.columns([3, 1])
        host = c1.text_input("Server", config.get("smtp_host"), max_chars=120, placeholder="smtp.gmail.com")
        port = c2.text_input("Port", config.get("smtp_port"), max_chars=5)
        user = st.text_input("Username (email)", config.get("smtp_user"), max_chars=120, type="email",
                             placeholder="aura.alerts@gmail.com")
        password = secret_input(st, "Password", "smtp_password", "For Gmail, a 16-letter App Password.")
        sender = st.text_input("Sender name (optional)", config.get("smtp_from"), max_chars=120,
                               placeholder="AURA Alerts <aura.alerts@gmail.com>",
                               help="Leave empty to send as 'AURA Alerts' from the username above.")
        save = st.form_submit_button("Save", type="primary", icon=":material/save:")
    if save:
        errors = []
        server, error = validation.smtp_server(host, port)
        errors += [error] if error else []
        email, error = validation.email(user, required=True)
        errors += [f"Username: {error}"] if error else []
        from_, error = validation.sender_name(sender)
        errors += [error] if error else []
        if not password.strip() and not config.is_set("smtp_password"):
            errors.append("Enter the password (for Gmail, an App Password).")
        if errors:
            show_errors(errors)
        else:
            values = {"smtp_host": server[0], "smtp_port": server[1], "smtp_user": email, "smtp_from": from_}
            if password.strip():
                values["smtp_password"] = "".join(password.split())    # Gmail shows App Passwords with spaces
            finish(values, "Email settings saved. Use 'Send test email' to try them.")
    if st.button("Send test email", key="pf_test_email", icon=":material/send:",
                 disabled=not config.is_set("smtp_password"),
                 help=f"Sends a test message to {config.get('smtp_user') or 'the username above'}."):
        with st.spinner("Sending…"):
            try:
                send_email(config.get("smtp_user"), "AURA test email",
                           "Email alerts from AURA are working.", raise_errors=True)
                st.success(f"Sent to {config.get('smtp_user')}. Check the inbox (and the spam folder once).",
                           icon=":material/check_circle:")
            except smtplib.SMTPAuthenticationError:
                st.error("The server rejected the username or password. For Gmail, use an App Password.",
                         icon=":material/error:")
            except Exception as e:
                st.error(f"Could not send: {str(e)[:150]}", icon=":material/error:")

# ----------------------------------------------------------------- Admin password
with right.container(border=True):
    st.subheader("Admin password", icon=":material/admin_panel_settings:")
    if not config.is_set("admin_password_hash"):
        st.warning("You're still using the first password from the .env file. Set your own here.",
                   icon=":material/warning:")
    with st.form(f"pf_admin_{version}", border=False):
        current = st.text_input("Current password", type="password", max_chars=128, autocomplete="current-password")
        new = st.text_input("New password", type="password", max_chars=128, autocomplete="new-password",
                            help=f"At least {validation.PASSWORD_MIN} characters, with a letter and a number.")
        again = st.text_input("Repeat new password", type="password", max_chars=128, autocomplete="new-password")
        save = st.form_submit_button("Change password", type="primary", icon=":material/key:")
    if save:
        value, error = validation.password(new, again)
        if not config.check_admin_password(current or ""):
            show_errors(["Current password is wrong."])
        elif error:
            show_errors([error])
        elif current == new:
            show_errors(["The new password must be different from the current one."])
        else:
            config.set_admin_password(value)
            st.session_state.platform_version += 1
            ui.flash("Admin password changed.")
            st.rerun()
