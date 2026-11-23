import streamlit as st

from app import ui
from app.services import db, validation

business = ui.current_business()
bid = business["id"]
admin = ui.is_admin()

st.caption(f"Login for **{business['name']}** — username `{business['username']}`")

with st.container(border=True, width=520):
    st.subheader("Reset the owner's password" if admin else "Change password", icon=":material/key:")
    if admin:
        st.caption("Give the new password to the owner; they can change it after logging in. "
                   "(Your own admin password is in Admin → Platform settings.)")
    with st.form(f"password_{bid}", clear_on_submit=True, border=False):
        current = None if admin else st.text_input("Current password", type="password", max_chars=128,
                                                   autocomplete="current-password")
        new = st.text_input("New password", type="password", max_chars=128, autocomplete="new-password",
                            help=f"At least {validation.PASSWORD_MIN} characters, with a letter and a number.")
        again = st.text_input("Repeat new password", type="password", max_chars=128, autocomplete="new-password")
        submitted = st.form_submit_button("Save new password", type="primary", icon=":material/save:")

    if submitted:
        value, error = validation.password(new, again)
        if not admin and not db.verify_password(bid, current or ""):
            st.error("Current password is wrong.", icon=":material/error:")
        elif error:
            st.error(error, icon=":material/error:")
        elif not admin and current == new:
            st.error("The new password must be different from the current one.", icon=":material/error:")
        else:
            db.set_password(bid, value)
            if admin:
                db.delete_business_sessions(bid)       # the owner must log in with the new password
            st.success("Password changed." + (" The owner has been logged out everywhere." if admin else ""),
                       icon=":material/check_circle:")
