"""WhatsApp messaging through WaAPI (https://waapi.app).

Docs: https://waapi.readme.io
  Send:     POST /instances/{id}/client/action/send-message  {"chatId": "923001234567@c.us", "message": "..."}
  Webhook:  WaAPI POSTs {"event": "message", "instanceId": ..., "data": {"message": {...}}} to AURA

The API token is a platform setting (dashboard -> Platform settings). Each business has its own
instance ID (its WhatsApp number), set on that business; there is no shared default.
"""
import os
import threading
from dataclasses import dataclass

import requests
from dotenv import load_dotenv

from app.services import config

load_dotenv()

WAAPI_API_URL = os.getenv("WAAPI_API_URL", "https://waapi.app/api/v1").rstrip("/")
TIMEOUT = 30


def _headers(token: "str | None" = None) -> dict:
    return {
        "Authorization": f"Bearer {token or config.get('waapi_token')}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "AURA/1.0",
    }


def _instance_url(path: str, instance_id) -> str:
    return f"{WAAPI_API_URL}/instances/{instance_id}/{path.lstrip('/')}"


def to_chat_id(phone: str) -> str:
    """'03001234567', '+92 300 1234567', '923001234567@s.whatsapp.net' -> '923001234567@c.us'.
    IDs that are already WaAPI chat IDs (@c.us, @lid, @g.us) are kept as they are."""
    phone = str(phone).strip()
    if phone.endswith(("@c.us", "@lid", "@g.us")):
        return phone
    digits = "".join(ch for ch in phone.split("@")[0] if ch.isdigit())
    if digits.startswith("0"):              # Pakistani local format 03XX -> 923XX
        digits = "92" + digits[1:]
    return f"{digits}@c.us"


def send_message(phone: str, message: str, instance_id) -> bool:
    """Send from the business's own WhatsApp instance."""
    if not config.get("waapi_token") or not instance_id:
        print("[WhatsApp] WaAPI token (Platform settings) or the business's instance ID missing — not sent")
        return False
    chat_id = to_chat_id(phone)
    try:
        response = requests.post(
            _instance_url("client/action/send-message", instance_id),
            json={"chatId": chat_id, "message": message},
            headers=_headers(),
            timeout=TIMEOUT,
        )
        body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
        # WaAPI answers HTTP 200 even when sending fails; data.status is the real result.
        ok = response.status_code == 200 and (body.get("data") or {}).get("status") == "success"
        if ok:
            print(f"[WhatsApp] Sent to {chat_id}")
        else:
            print(f"[WhatsApp] FAILED to {chat_id}: {response.status_code} - {response.text[:300]}")
        return ok
    except Exception as e:
        print(f"[WhatsApp] Error sending to {chat_id}: {e}")
        return False


VOICE_TYPES = ("ptt", "audio")              # ptt = WhatsApp voice note, audio = a forwarded audio file


@dataclass
class Incoming:
    """One customer message from the WaAPI webhook."""
    chat_id: str                 # 923001234567@c.us, or a privacy ID like 257139759173829@lid
    text: str                    # "" for a voice note
    message_id: str
    instance_id: str
    voice: "dict | None" = None  # {"data": base64 or None, "mimetype": ...} for voice notes
    profile_name: str = ""       # the name the customer set in WhatsApp (may be a nickname or emoji)


def _profile_name(message: dict) -> str:
    raw = (message.get("_data") or {}).get("notifyName") or message.get("notifyName") or ""
    return " ".join("".join(ch for ch in str(raw) if ch.isprintable()).split())[:60]


def parse_incoming(payload: dict) -> "Incoming | None":
    """Turn a WaAPI webhook into an Incoming message, or None if AURA should ignore it."""
    if not isinstance(payload, dict) or payload.get("event") != "message":
        return None
    data = payload.get("data") or {}
    message = data.get("message") or {}
    if message.get("fromMe"):
        return None
    chat_id = message.get("from") or ""
    if not chat_id or chat_id.endswith("@g.us") or chat_id == "status@broadcast":
        return None                          # ignore groups and status updates
    kind = message.get("type")
    voice = None
    if kind in VOICE_TYPES:
        media = data.get("media") or {}      # WaAPI includes the file (base64) in the webhook
        voice = {"data": media.get("data"), "mimetype": media.get("mimetype") or message.get("mimetype") or "audio/ogg"}
        text = ""
    elif kind in ("chat", None):
        text = (message.get("body") or "").strip()
        if not text:
            return None
    else:
        return None                          # images, stickers, documents, ... are not handled
    raw_id = message.get("id") or {}
    if isinstance(raw_id, dict):
        msg_id = raw_id.get("_serialized") or raw_id.get("id") or ""
    else:
        msg_id = str(raw_id)
    instance_id = str(payload.get("instanceId") or "")
    return Incoming(chat_id, text, msg_id, instance_id, voice, _profile_name(message))


_phone_cache: dict = {}          # privacy ID -> phone chat ID (they don't change)
_cache_lock = threading.Lock()


def resolve_chat_id(chat_id: str, instance_id) -> str:
    """WhatsApp often hides the sender's number behind a privacy ID ('257139759173829@lid').
    Ask WaAPI for the real number ('923001234567@c.us') so replies, reminders, the dashboard and
    owner alerts use the phone number. Falls back to the privacy ID if the lookup fails."""
    if not chat_id.endswith("@lid") or not instance_id:
        return chat_id
    with _cache_lock:
        if chat_id in _phone_cache:
            return _phone_cache[chat_id]
    try:
        response = requests.post(_instance_url("client/action/get-lid", instance_id),
                                 json={"contactIds": [chat_id]}, headers=_headers(), timeout=TIMEOUT)
        result = (response.json() or {}).get("data") or {}
        contacts = (result.get("data") or {}).get("contacts") or []
        phone = next((c.get("pn") for c in contacts if c.get("lid") == chat_id and c.get("pn")), None)
    except (requests.RequestException, ValueError, AttributeError) as e:
        print(f"[WhatsApp] Could not look up the number behind {chat_id}: {e}")
        return chat_id
    if not phone:
        print(f"[WhatsApp] WaAPI has no number for {chat_id}; using the privacy ID")
        return chat_id
    with _cache_lock:
        _phone_cache[chat_id] = phone
    return phone


def download_media(message_id: str, instance_id):
    """(bytes, mimetype) of a message's file, used when the webhook didn't include it. None on failure."""
    if not message_id or not instance_id or not config.get("waapi_token"):
        return None
    try:
        response = requests.post(_instance_url("client/action/download-media", instance_id),
                                 json={"messageId": message_id}, headers=_headers(), timeout=TIMEOUT)
        result = (response.json() or {}).get("data") or {}
        media = (result.get("data") or {}).get("media") or result.get("media") or {}
        if result.get("status") != "success" or not media.get("url"):
            print(f"[WhatsApp] No media for {message_id}: {response.text[:200]}")
            return None
        file = requests.get(media["url"], timeout=TIMEOUT)    # signed URL, valid 15 minutes
        file.raise_for_status()
        return file.content, media.get("mimetype") or "audio/ogg"
    except Exception as e:
        print(f"[WhatsApp] Media download failed for {message_id}: {e}")
        return None


def set_webhook(webhook_url: str, instance_id) -> bool:
    """Register AURA's /webhook URL with a WaAPI instance (replaces old AURA webhooks first)."""
    try:
        existing = requests.get(_instance_url("webhooks", instance_id), headers=_headers(),
                                timeout=TIMEOUT).json().get("data") or []
        for hook in existing:   # replace only AURA's own (…/webhook) subscriptions
            if hook.get("id") is not None and str(hook.get("url", "")).rstrip("/").endswith("/webhook"):
                requests.delete(_instance_url(f"webhooks/{hook['id']}", instance_id), headers=_headers(),
                                timeout=TIMEOUT)
        response = requests.post(
            _instance_url("webhooks", instance_id),
            json={"url": webhook_url, "events": ["message"]},
            headers=_headers(),
            timeout=TIMEOUT,
        )
        print(f"[WhatsApp] Webhook set: {response.status_code} - {response.text[:300]}")
        return response.status_code in (200, 201)
    except Exception as e:
        print(f"[WhatsApp] Webhook error: {e}")
        return False


def get_status(instance_id) -> dict:
    """Instance connection status (dashboard check and debugging)."""
    try:
        return requests.get(_instance_url("client/status", instance_id), headers=_headers(), timeout=TIMEOUT).json()
    except Exception as e:
        return {"error": str(e)}


def check_token(token: "str | None" = None):
    """(ok, message) — is this WaAPI API token valid? Lists the account's instances."""
    token = token or config.get("waapi_token")
    if not token:
        return False, "No WaAPI token saved."
    try:
        response = requests.get(f"{WAAPI_API_URL}/instances", headers=_headers(token), timeout=TIMEOUT)
    except requests.RequestException as e:
        return False, f"WaAPI unreachable: {str(e)[:120]}"
    if response.status_code in (401, 403):
        return False, "WaAPI rejected the token. Copy it again from waapi.app → User API Tokens."
    if response.status_code != 200:
        return False, f"WaAPI answered {response.status_code}: {response.text[:120]}"
    instances = (response.json() or {}).get("instances") or (response.json() or {}).get("data") or []
    ids = ", ".join(str(i.get("id")) for i in instances if isinstance(i, dict)) or "none yet"
    return True, f"Token works. Instances on this account: {ids}."


if __name__ == "__main__":
    # python -m app.services.whatsapp 108128  -> quick connection check of one instance
    import sys
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python -m app.services.whatsapp <instance id>")
    status = get_status(sys.argv[1]).get("clientStatus", {})
    print("Instance:", sys.argv[1], "| status:", status.get("instanceStatus"))
