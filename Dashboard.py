"""AURA management dashboard.

Run:  streamlit run Dashboard.py

Admin    username "admin". First login with ADMIN_PASSWORD from .env, then change it in Platform settings.
         Adds businesses, manages any of them, and sets the platform settings (API keys, email server).
Owners   log in with the username/password the admin created; they see only their own business.
Pages live in app_pages/; shared helpers in app/ui.py; validation in app/services/validation.py.
"""
from typing import NoReturn

import streamlit as st

from app import ui
from app.services import config, db

st.set_page_config(page_title="AURA dashboard", page_icon=":material/support_agent:", layout="wide")


def _login_failed(message: str) -> NoReturn:
    """Show the error on a fresh, empty form (a new form key clears both fields)."""
    st.session_state.login_error = message
    st.session_state.login_version = st.session_state.get("login_version", 0) + 1
    st.rerun()


def login_page():
    left, middle, right = st.columns([1, 1.2, 1])
    with middle:
        st.space("large")
        st.title("AURA", icon=":material/support_agent:")
        st.caption("WhatsApp receptionist — management dashboard")
        with st.form(f"login_{st.session_state.get('login_version', 0)}", border=True):
            username = st.text_input("Username", max_chars=30, autocomplete="username",
                                     icon=":material/person:")
            password = st.text_input("Password", type="password", max_chars=128,
                                     autocomplete="current-password", icon=":material/lock:")
            submitted = st.form_submit_button("Log in", type="primary", width="stretch",
                                              icon=":material/login:")
        if "login_error" in st.session_state:
            st.error(st.session_state.pop("login_error"), icon=":material/error:")
        if not config.admin_login_available():
            st.caption("Admin login is off until ADMIN_PASSWORD is set in the .env file.")
        if not submitted:
            return

        username = (username or "").strip().lower()
        if not username or not password:
            _login_failed("Enter your username and password.")
        wait = ui.login_locked(username)
        if wait:
            _login_failed(f"Too many wrong attempts. Try again in {wait // 60 + 1} minute(s).")

        if username == "admin":
            session = {"role": "admin"} if config.check_admin_password(password) else None
        else:
            business = db.login(username, password)
            session = {"role": "owner", "business_id": business["id"], "name": business["name"]} if business else None
        ui.record_login(username, bool(session))
        if not session:
            _login_failed("Wrong username or password.")
        ui.start_session(session)
        st.rerun()


def business_pages(business_id: int) -> list:
    """Pages for one business; the Escalations title shows how many cases are open."""
    open_cases = len(db.list_escalations(business_id, status="open"))
    return [
        st.Page("app_pages/overview.py", title="Overview", icon=":material/dashboard:", default=True,
                help="Today's schedule, the next 7 days at a glance, and what needs your attention."),
        st.Page("app_pages/bookings.py", title="Bookings", icon=":material/event:",
                help="Search and filter bookings, see each booking's conversation, confirm or cancel, export CSV."),
        st.Page("app_pages/escalations.py", title=f"Escalations ({open_cases})" if open_cases else "Escalations",
                icon=":material/notification_important:",
                help="Customers handed over to you, with the conversation. Mark each as resolved when done."),
        st.Page("app_pages/conversations.py", title="Conversations", icon=":material/forum:",
                help="Backup of every WhatsApp chat AURA had with your customers."),
        st.Page("app_pages/services.py", title="Services & prices", icon=":material/sell:",
                help="The services customers can book, with the price AURA quotes and how long each takes."),
        st.Page("app_pages/settings.py", title="Settings", icon=":material/tune:",
                help="Business details, working hours, greeting, extra questions and alert contacts."),
        st.Page("app_pages/account.py", title="Account", icon=":material/key:",
                help="Change the login password."),
    ]


if not ui.user():
    ui.restore_session()                     # a page refresh starts a new session: log back in from the cookie
user = ui.user()
ui.sync_cookie()
if not user:
    page = st.navigation([st.Page(login_page, title="Log in", icon=":material/login:")], position="hidden")
    page.run()
    st.stop()

# ----------------------------------------------------------------- logged in
if ui.is_admin():
    businesses = db.list_businesses()
    admin_page = st.Page("app_pages/businesses.py", title="Businesses", icon=":material/storefront:",
                         default=not businesses,
                         help="All businesses on AURA: add a new one and pick which one to manage.")
    platform_page = st.Page("app_pages/platform.py", title="Platform settings", icon=":material/settings:",
                            help="API keys and accounts AURA uses for every business, and your admin password.")
    admin_pages = [admin_page, platform_page]
    if businesses:
        ids = [b["id"] for b in businesses]
        names = {b["id"]: b["name"] + ("" if b.get("active") else " (inactive)") for b in businesses}
        if st.session_state.get("select_business") in ids:
            st.session_state.business_id = st.session_state.pop("select_business")
        if st.session_state.get("business_id") not in ids:
            st.session_state.business_id = ids[0]
        with st.sidebar:
            st.selectbox("Managing", ids, format_func=lambda i: names[i], key="business_id")
        page = st.navigation({"Admin": admin_pages,
                              names[st.session_state.business_id]: business_pages(st.session_state.business_id)})
    else:
        page = st.navigation({"Admin": admin_pages})
else:
    business = db.get_business(user["business_id"])
    if not business or not business.get("active"):
        ui.end_session()
        st.rerun()
    page = st.navigation(business_pages(business["id"]))

with st.sidebar:
    who = "Administrator" if ui.is_admin() else user.get("name", "")
    st.caption(f":material/account_circle: Logged in as **{who}**")
    st.button("Log out", icon=":material/logout:", on_click=ui.end_session, width="stretch")

ui.show_flash()
st.title(page.title, icon=page.icon)
page.run()
