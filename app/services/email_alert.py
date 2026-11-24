"""Send emails (Python's built-in smtplib, no extra packages).

Two senders:
  AURA's own email (dashboard -> Platform settings)   alerts to business owners
  each business's own email (its Settings page)       booking emails to its customers (customer_mail.py)

Gmail example: server smtp.gmail.com, port 587, password = a Gmail App Password (not the normal password).
"""
import smtplib
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from app.services import config

TIMEOUT = 20


def configured() -> bool:
    """Is AURA's own email (Platform settings) set up?"""
    return all(config.get(k) for k in ("smtp_host", "smtp_user", "smtp_password"))


def _platform_account() -> dict:
    user = config.get("smtp_user")
    return {"host": config.get("smtp_host"), "port": int(config.get("smtp_port") or 587), "user": user,
            "password": config.get("smtp_password"),
            "sender": config.get("smtp_from") or formataddr(("AURA Alerts", user))}


def send_email(to: str, subject: str, body: str, raise_errors: bool = False, account: "dict | None" = None) -> bool:
    """True if the mail server accepted the message. Never raises unless raise_errors (dashboard tests).
    account: {"host", "port", "user", "password", "sender"}; default: AURA's own email (owner alerts)."""
    if not to:
        return False
    if account is None:
        if not configured():
            print("[Email] SMTP settings missing (dashboard → Platform settings) — email not sent")
            return False
        account = _platform_account()
    user = account["user"]
    message = EmailMessage()
    # Complete, standard headers: a missing Date / Message-ID or a bare address raises spam scores.
    message["From"] = account.get("sender") or user
    message["To"] = to
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=user.split("@")[-1] or None)
    message.set_content(body)
    try:
        with smtplib.SMTP(account["host"], int(account["port"]), timeout=TIMEOUT) as smtp:
            smtp.starttls()
            smtp.login(user, account["password"])
            smtp.send_message(message)
        print(f"[Email] Sent to {to}: {subject}")
        return True
    except Exception as e:                       # wrong password, no internet, ...
        print(f"[Email] Could not send to {to}: {str(e)[:150]}")
        if raise_errors:
            raise
        return False
