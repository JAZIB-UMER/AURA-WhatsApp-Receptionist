"""Settings page panel that links a business's WhatsApp to AURA: Connect -> scan the QR code -> connected.

The QR code and status refresh by themselves every few seconds while the phone is being linked.
"""
import streamlit as st

from app.services import db, whatsapp
from app.services.manage import display_phone

SCAN_STEPS = ("On the business's phone open **WhatsApp → Settings → Linked devices → Link a device** and "
              "point the camera at this code. It changes every 20 seconds; this page follows by itself.")


def _connect(business: dict):
    """Start (or restart) the business's WhatsApp session; the page then shows its QR code."""
    session = business.get("whatsapp_session")
    if not session:                              # saved first, so a retry reuses the same session
        session = whatsapp.new_session_name(business["id"])
        db.update_business(business["id"], whatsapp_session=session)
    with st.spinner("Starting WhatsApp…"):
        ok, message = whatsapp.start_session(session)
    if ok:
        st.rerun()
    st.error(f"Could not start WhatsApp. {message}", icon=":material/error:")


def _close_dialog():
    st.session_state.pop("wa_disconnect", None)


@st.dialog("Disconnect WhatsApp", icon=":material/link_off:", on_dismiss=_close_dialog)
def _disconnect_dialog(business: dict):
    st.markdown(f"AURA will **stop answering** {business['name']}'s WhatsApp customers until the number is "
                "connected again. Chats, bookings and reminders already saved are kept.")
    with st.container(horizontal=True):
        confirm = st.button("Yes, disconnect", type="primary", icon=":material/link_off:", key="wa_disconnect_yes")
        keep = st.button("Keep it connected", key="wa_disconnect_no")
    if keep:
        _close_dialog()
        st.rerun()
    if confirm:
        ok, message = whatsapp.delete_session(business["whatsapp_session"])
        if ok:
            db.update_business(business["id"], whatsapp_session=None)
            _close_dialog()
            st.rerun()
        st.error(f"Could not disconnect. {message}", icon=":material/error:")


@st.fragment(run_every=3)
def _linking(session: str):
    """QR code while WhatsApp waits for the phone; reloads the page once linking ends."""
    info = whatsapp.session_info(session)
    if info["status"] not in whatsapp.WAITING:
        st.rerun()                               # connected, failed or expired: show that state
    qr = whatsapp.qr_image(session) if info["status"] == "SCAN_QR_CODE" else None
    if qr:
        st.image(qr, width=260, alt="WhatsApp QR code to link the business's number")
        st.caption(SCAN_STEPS)
    else:
        st.info("Starting WhatsApp… the QR code appears here in a few seconds.", icon=":material/hourglass_top:")


def show(business: dict):
    """The WhatsApp panel of a business's Settings page."""
    session = business.get("whatsapp_session")
    key = f"wa_{business['id']}_"
    with st.container(border=True):
        st.subheader("WhatsApp", icon=":material/chat:")
        if not session:
            st.caption("Link the business's WhatsApp number so AURA can answer its customers: click **Connect**, "
                       "then scan the QR code with that phone (like WhatsApp Web).")
            if st.button("Connect WhatsApp", type="primary", icon=":material/qr_code_2:", key=key + "connect"):
                _connect(business)
            return

        info = whatsapp.session_info(session)
        status = info["status"]
        if status == whatsapp.CONNECTED:
            number = display_phone(info.get("phone") or "")
            who = f" ({info['name']})" if info.get("name") else ""
            st.success(f"Connected: **{number}**{who}. AURA answers this number's customers.",
                       icon=":material/check_circle:")
        elif status in whatsapp.WAITING:
            _linking(session)
        elif status == whatsapp.UNREACHABLE:
            st.error("The WhatsApp server (WAHA) isn't answering, so AURA can't send or receive messages. "
                     "Check that its container is running (docker compose ps).", icon=":material/error:")
            st.button("Check again", icon=":material/refresh:", key=key + "retry")
            return
        else:                                    # FAILED, STOPPED, MISSING: QR expired or the phone unlinked AURA
            st.warning("WhatsApp is not connected: the QR code expired, or the phone removed AURA from its "
                       "linked devices. Show a new QR code and scan it again.", icon=":material/warning:")
            if st.button("Show a new QR code", type="primary", icon=":material/qr_code_2:", key=key + "reconnect"):
                _connect(business)
        if st.button("Disconnect", icon=":material/link_off:", key=key + "disconnect"):
            st.session_state.wa_disconnect = business["id"]

    # The confirmation stays open across reruns until the user confirms, keeps it, or closes it.
    if st.session_state.get("wa_disconnect") == business["id"]:
        _disconnect_dialog(business)
