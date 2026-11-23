"""Send alert emails to business owners (Python's built-in smtplib, no extra packages).

SMTP settings are platform settings (dashboard -> Platform settings). Gmail example:
    server smtp.gmail.com, port 587, username aura.alerts@gmail.com,
    password = a Gmail App Password (not the normal Gmail password)
"""
import smtplib
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from app.services import config

TIMEOUT = 20


def configured() -> bool:
    return all(config.get(k) for k in ("smtp_host", "smtp_user", "smtp_password"))


def send_email(to: str, subject: str, body: str, raise_errors: bool = False) -> bool:
    """True if the mail server accepted the message. Never raises unless raise_errors (dashboard test)."""
    if not to:
        return False
    if not configured():
        print("[Email] SMTP settings missing (dashboard → Platform settings) — email not sent")
        return False
    user = config.get("smtp_user")
    message = EmailMessage()
    # Complete, standard headers: a missing Date / Message-ID or a bare address raises spam scores.
    message["From"] = config.get("smtp_from") or formataddr(("AURA Alerts", user))
    message["To"] = to
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=user.split("@")[-1] or None)
    message.set_content(body)
    try:
        with smtplib.SMTP(config.get("smtp_host"), int(config.get("smtp_port") or 587), timeout=TIMEOUT) as smtp:
            smtp.starttls()
            smtp.login(user, config.get("smtp_password"))
            smtp.send_message(message)
        print(f"[Email] Sent to {to}: {subject}")
        return True
    except Exception as e:                       # wrong password, no internet, ...
        print(f"[Email] Could not send to {to}: {str(e)[:150]}")
        if raise_errors:
            raise
        return False
