"""WhatsApp through WAHA (https://waha.devlike.pro): a free WhatsApp server that runs next to AURA.

Each business links its own WhatsApp number by scanning a QR code on its dashboard Settings page. That
creates a WAHA "session", saved on the business as whatsapp_session. Then:
  WAHA -> AURA   every customer message:  POST {AURA_WEBHOOK_URL}  {"event": "message", "session", "payload"}
  AURA -> WAHA   every reply:             POST {WAHA_URL}/api/sendText  {"session", "chatId", "text"}

Machine settings (.env; docker compose sets them itself):
  WAHA_URL          where AURA reaches WAHA         Docker: http://waha:3000   otherwise http://localhost:3000
  WAHA_API_KEY      the API key WAHA runs with (the same value for WAHA, the server and the dashboard)
  AURA_WEBHOOK_URL  where WAHA reaches AURA         Docker: http://aura:8000/webhook

Webhooks are signed (HMAC-SHA512) with a secret AURA generates and keeps in its database
(config.webhook_secret), so only WAHA can hand AURA customer messages.
"""
import hashlib
import hmac
import os
import secrets
import threading
import time
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

import requests
from dotenv import load_dotenv

from app.services import config

load_dotenv()

WAHA_URL = os.getenv("WAHA_URL", "http://localhost:3000").rstrip("/")
WEBHOOK_URL = os.getenv("AURA_WEBHOOK_URL", "http://host.docker.internal:8000/webhook")
TIMEOUT = 30                 # sending / downloading
QUICK = 8                    # status checks shown in the dashboard

CONNECTED = "WORKING"                        # WAHA session statuses
WAITING = ("STARTING", "SCAN_QR_CODE")       # linking in progress
MISSING = "MISSING"                          # AURA's own: WAHA has no such session
UNREACHABLE = "UNREACHABLE"                  # AURA's own: WAHA didn't answer


def _headers() -> dict:
    return {"X-Api-Key": os.getenv("WAHA_API_KEY", ""), "Accept": "application/json", "User-Agent": "AURA/1.0"}


def _call(method: str, path: str, timeout: float = TIMEOUT, **kwargs) -> "requests.Response":
    return requests.request(method, f"{WAHA_URL}/{path.lstrip('/')}", headers={**_headers(), **kwargs.pop(
        "headers", {})}, timeout=timeout, **kwargs)


def _short(response: "requests.Response") -> str:
    return f"{response.status_code} {response.text[:200]}"


# ----------------------------------------------------------------- phone numbers and chat IDs

def to_chat_id(phone: str) -> str:
    """'03001234567', '+92 300 1234567', '923001234567:12@s.whatsapp.net' -> '923001234567@c.us'.
    Chat IDs WAHA already uses (@c.us, @lid, @g.us) are kept as they are."""
    phone = str(phone).strip()
    if phone.endswith(("@c.us", "@lid", "@g.us")):
        return phone
    digits = "".join(ch for ch in phone.split("@")[0].split(":")[0] if ch.isdigit())   # ':12' = device number
    if digits.startswith("0"):              # Pakistani local format 03XX -> 923XX
        digits = "92" + digits[1:]
    return f"{digits}@c.us"


def phone_of(chat_id: str) -> str:
    """'923001234567@c.us' -> '923001234567' (the conversation key AURA stores)."""
    return str(chat_id).split("@")[0]


# ----------------------------------------------------------------- sending

def send_message(phone: str, message: str, session: "str | None") -> bool:
    """Send from the business's own WhatsApp (its WAHA session)."""
    if not session:
        print("[WhatsApp] This business has no WhatsApp linked (Settings -> WhatsApp) — not sent")
        return False
    chat_id = to_chat_id(phone)
    try:
        response = _call("POST", "api/sendText", json={"session": session, "chatId": chat_id, "text": message})
    except requests.RequestException as e:
        print(f"[WhatsApp] Error sending to {chat_id}: {e}")
        return False
    if response.status_code in (200, 201):
        print(f"[WhatsApp] Sent to {chat_id}")
        return True
    print(f"[WhatsApp] FAILED to {chat_id}: {_short(response)}")
    return False


def mark_seen(chat_id: str, message_id: str, session: str):
    """Blue ticks on the customer's message, like a person reading it. Best effort."""
    try:
        _call("POST", "api/sendSeen", timeout=QUICK,
              json={"session": session, "chatId": chat_id, "messageIds": [message_id] if message_id else []})
    except requests.RequestException:
        pass


# ----------------------------------------------------------------- incoming messages

@dataclass
class Incoming:
    """One customer message from the WAHA webhook."""
    chat_id: str                 # 923001234567@c.us, or a privacy ID like 257139759173829@lid
    text: str                    # "" for a voice note
    message_id: str
    session: str                 # WAHA session = which business's WhatsApp received it
    voice: "dict | None" = None  # {"url": file on WAHA or None, "data": base64 (test sender only), "mimetype"}
    profile_name: str = ""       # the name the customer set in WhatsApp (may be a nickname or emoji)
    phone_hint: str = ""         # the sender's real number when WAHA already includes it (…@c.us)


VOICE_TYPES = ("ptt", "audio")   # ptt = WhatsApp voice note, audio = a forwarded audio file
IGNORED_CHATS = ("@g.us", "@broadcast", "@newsletter")


def _clean_name(raw) -> str:
    return " ".join("".join(ch for ch in str(raw or "") if ch.isprintable()).split())[:60]


def _engine_details(message: dict) -> dict:
    """Engine-specific extras (_data). GOWS puts them under Info / Message; WEBJS and NOWEB at the top."""
    data = message.get("_data") or {}
    info = data.get("Info") or {}
    content = data.get("Message") or data.get("message") or {}
    sender_alt = str(info.get("SenderAlt") or "")
    return {
        "name": info.get("PushName") or data.get("notifyName") or data.get("pushName") or "",
        "voice": data.get("type") in VOICE_TYPES or "audioMessage" in content,
        # the real number, when the sender appears as a privacy ID (GOWS)
        "phone": to_chat_id(sender_alt) if sender_alt.endswith(("@s.whatsapp.net", "@c.us")) else "",
    }


def parse_incoming(body: dict) -> "Incoming | None":
    """Turn a WAHA webhook into an Incoming message, or None if AURA should ignore it."""
    if not isinstance(body, dict) or body.get("event") != "message":
        return None
    message = body.get("payload") or {}
    if not isinstance(message, dict) or message.get("fromMe"):
        return None
    chat_id = str(message.get("from") or "")
    if not chat_id or chat_id.endswith(IGNORED_CHATS) or chat_id == "status@broadcast":
        return None                          # ignore groups, broadcasts, channels and status updates
    extra = _engine_details(message)
    media = message.get("media") or {}
    mimetype = str(media.get("mimetype") or "")
    voice = None
    if message.get("hasMedia") or media:
        if not (mimetype.startswith("audio/") or extra["voice"]):
            return None                      # images, stickers, videos, documents are not handled
        voice = {"url": media.get("url"), "data": media.get("data"), "mimetype": mimetype or "audio/ogg"}
        text = ""
    else:
        text = str(message.get("body") or "").strip()
        if not text:
            return None
    return Incoming(chat_id, text, str(message.get("id") or ""), str(body.get("session") or ""), voice,
                    _clean_name(extra["name"]), extra["phone"])


def signature_ok(raw_body: bytes, signature: "str | None") -> bool:
    """Was this webhook signed by WAHA with AURA's secret? (header X-Webhook-Hmac, HMAC-SHA512, hex)"""
    if not signature:
        return False
    expected = hmac.new(config.webhook_secret().encode(), raw_body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(expected, signature.strip().lower())


_phone_cache: dict = {}          # privacy ID -> phone chat ID (they don't change)
_cache_lock = threading.Lock()


def resolve_chat_id(chat_id: str, session: str, hint: str = "") -> str:
    """WhatsApp often hides the sender's number behind a privacy ID ('257139759173829@lid').
    Use the real number ('923001234567@c.us') so replies, reminders, the dashboard and owner alerts
    use the phone number. Falls back to the privacy ID if it can't be found."""
    if not chat_id.endswith("@lid"):
        return chat_id
    with _cache_lock:
        if chat_id in _phone_cache:
            return _phone_cache[chat_id]
    phone = hint if hint.endswith("@c.us") else ""
    if not phone and session:
        try:
            response = _call("GET", f"api/{quote(session)}/lids/{quote(chat_id)}", timeout=QUICK)
            found = response.json() if response.status_code == 200 else {}
            pn = str((found or {}).get("pn") or "") if isinstance(found, dict) else ""
            phone = to_chat_id(pn) if pn else ""
        except (requests.RequestException, ValueError) as e:
            print(f"[WhatsApp] Could not look up the number behind {chat_id}: {e}")
            return chat_id
    if not phone:
        print(f"[WhatsApp] No number known for {chat_id}; using the privacy ID")
        return chat_id
    with _cache_lock:
        _phone_cache[chat_id] = phone
    return phone


def download_media(url: "str | None"):
    """(bytes, mimetype) of a file WAHA saved (voice notes), or None. Only ever fetched from WAHA itself,
    whatever host the link names, so the API key never goes anywhere else."""
    if not url:
        return None
    parts = urlsplit(url)
    path = parts.path + (f"?{parts.query}" if parts.query else "")
    try:
        response = _call("GET", path)
        response.raise_for_status()
        return response.content, response.headers.get("content-type") or "audio/ogg"
    except requests.RequestException as e:
        print(f"[WhatsApp] Media download failed ({path}): {e}")
        return None


# ----------------------------------------------------------------- linking a business's WhatsApp (sessions)

def new_session_name(business_id: int) -> str:
    """Unique WAHA session name for a business, e.g. 'aura_3_9f2c1a'."""
    return f"aura_{int(business_id)}_{secrets.token_hex(3)}"


def _session_config() -> dict:
    return {"webhooks": [{
        "url": WEBHOOK_URL,
        "events": ["message"],
        "hmac": {"key": config.webhook_secret()},
        # AURA restarting (loading its models takes 1-2 minutes): WAHA keeps the message and tries again
        "retries": {"policy": "constant", "delaySeconds": 10, "attempts": 18},
    }]}


def _webhook_ok(current: dict, wanted: dict) -> bool:
    """Does the session already send messages to AURA, signed with AURA's secret? Only the fields AURA
    sets are compared (WAHA adds its own defaults), so a correct session is never restarted needlessly."""
    hooks = current.get("webhooks") or []
    return any(isinstance(h, dict) and h.get("url") == wanted["url"] and "message" in (h.get("events") or [])
               and (h.get("hmac") or {}).get("key") == wanted["hmac"]["key"] for h in hooks)


def session_info(session: str) -> dict:
    """{"status", "phone", "name"}. status: WORKING, STARTING, SCAN_QR_CODE, FAILED, STOPPED,
    MISSING (WAHA doesn't know it) or UNREACHABLE (with "error")."""
    try:
        response = _call("GET", f"api/sessions/{quote(session)}", timeout=QUICK)
    except requests.RequestException as e:
        return {"status": UNREACHABLE, "error": str(e)[:200]}
    if response.status_code == 404:
        return {"status": MISSING}
    if response.status_code != 200:
        return {"status": UNREACHABLE, "error": _short(response)}
    data = response.json() or {}
    me = data.get("me") or {}
    return {"status": str(data.get("status") or "UNKNOWN"), "phone": phone_of(me["id"]) if me.get("id") else "",
            "name": _clean_name(me.get("pushName")), "config": data.get("config") or {}}


def start_session(session: str) -> "tuple[bool, str]":
    """Create the session in WAHA if needed, make sure its webhook points at AURA, and start it.
    (ok, message). After this the dashboard shows the QR code (or 'connected' if already linked)."""
    info = session_info(session)
    status = info["status"]
    wanted = _session_config()
    try:
        if status == UNREACHABLE:
            return False, f"The WhatsApp server (WAHA) is not reachable: {info.get('error')}"
        if status == MISSING:
            response = _call("POST", "api/sessions", json={"name": session, "config": wanted, "start": True})
            return response.status_code in (200, 201), "" if response.ok else f"WAHA: {_short(response)}"
        if not _webhook_ok(info.get("config") or {}, wanted["webhooks"][0]):
            response = _call("PUT", f"api/sessions/{quote(session)}", json={"config": wanted})
            if not response.ok:
                return False, f"WAHA: {_short(response)}"
            status = session_info(session)["status"]
        if status in ("STOPPED", "FAILED"):
            action = "start" if status == "STOPPED" else "restart"
            response = _call("POST", f"api/sessions/{quote(session)}/{action}")
            if not response.ok:
                return False, f"WAHA: {_short(response)}"
        return True, ""
    except requests.RequestException as e:
        return False, f"The WhatsApp server (WAHA) is not reachable: {str(e)[:200]}"


def qr_image(session: str) -> "bytes | None":
    """PNG of the QR code to scan (only while the session is waiting for it)."""
    try:
        response = _call("GET", f"api/{quote(session)}/auth/qr", timeout=QUICK, headers={"Accept": "image/png"})
    except requests.RequestException:
        return None
    is_png = response.status_code == 200 and response.headers.get("content-type", "").startswith("image/")
    return response.content if is_png else None


def delete_session(session: str) -> "tuple[bool, str]":
    """Unlink: logs the business's WhatsApp out of AURA and removes the session from WAHA."""
    try:
        response = _call("DELETE", f"api/sessions/{quote(session)}")
    except requests.RequestException as e:
        return False, f"The WhatsApp server (WAHA) is not reachable: {str(e)[:200]}"
    if response.ok or response.status_code == 404:
        return True, ""
    return False, f"WAHA: {_short(response)}"


def server_status() -> "tuple[bool, str]":
    """(ok, message) — is the WhatsApp server running and accepting AURA's API key?"""
    try:
        response = _call("GET", "api/server/version", timeout=QUICK)
    except requests.RequestException:
        return False, f"The WhatsApp server (WAHA) is not reachable at {WAHA_URL}. Is its container running?"
    if response.status_code == 401:
        return False, "WAHA refused AURA's API key: WAHA_API_KEY must be the same for WAHA and AURA."
    if not response.ok:
        return False, f"WAHA answered {_short(response)}"
    data = response.json() or {}
    return True, f"WhatsApp server running: WAHA {data.get('version', '?')} ({data.get('engine', '?')} engine)."


def sync_sessions(attempts: int = 30, wait: float = 10):
    """At server start: make sure every active business's WhatsApp session is running and sends its
    messages to this server. Retries while WAHA is still starting up."""
    from app.services import db
    message = ""
    for _ in range(attempts):
        ok, message = server_status()
        if ok:
            break
        time.sleep(wait)
    else:
        print(f"[WhatsApp] Sessions not checked: {message}")
        return
    for business in db.list_businesses(active_only=True):
        session = business.get("whatsapp_session")
        if session:
            ok, message = start_session(session)
            print(f"[WhatsApp] {business['name']}: " + ("session running" if ok else f"PROBLEM - {message}"))


def start_background_sync():
    threading.Thread(target=sync_sessions, name="whatsapp-sync", daemon=True).start()


if __name__ == "__main__":
    # python -m app.services.whatsapp   -> is the WhatsApp server reachable, and how is each business linked?
    from app.services import db
    print(server_status()[1])
    for b in db.list_businesses():
        s = b.get("whatsapp_session")
        print(f"- {b['name']}: " + (f"{s} -> {session_info(s)['status']}" if s else "no WhatsApp linked"))
