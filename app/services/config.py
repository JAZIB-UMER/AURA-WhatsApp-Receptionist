"""Platform settings: API keys and accounts AURA uses for every business.

Two kinds of configuration:
  .env (machine)      ports, hosts, file paths, first admin password. Set once when installing.
  this module (admin) Gemini / Groq / WaAPI keys, Google service account, email (SMTP), admin password.
                      Stored in the database, edited in the dashboard (Admin -> Platform settings),
                      and picked up by the running server within a few seconds, no restart.
Per-business values (WaAPI instance, calendar, owner phone/email) live on each business, not here.

First start: values found in the old .env / credentials.json are imported once, so nothing has to be
typed again. After that the dashboard is the only place to change them.

Forgot the admin password?  python -m app.services.config reset-admin
(then log in with ADMIN_PASSWORD from .env again).
"""
import json
import os
import secrets
import sys
import threading
import time
from pathlib import Path

from dotenv import load_dotenv

from app.services import db

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CACHE_SECONDS = 5           # dashboard changes reach the server within this time

DEFAULTS = {
    "gemini_model": "gemini-3.5-flash-lite",
    "whisper_model": "whisper-large-v3",
    "smtp_port": "587",
}
SECRETS = {"gemini_api_key", "groq_api_key", "waapi_token", "smtp_password", "google_service_account",
           "admin_password_hash"}
ENV_NAMES = {             # setting -> old .env name (imported once)
    "gemini_api_key": "GEMINI_API_KEY", "gemini_model": "GEMINI_MODEL",
    "groq_api_key": "GROQ_API_KEY", "whisper_model": "WHISPER_MODEL",
    "waapi_token": "WAAPI_TOKEN",
    "smtp_host": "SMTP_HOST", "smtp_port": "SMTP_PORT", "smtp_user": "SMTP_USER",
    "smtp_password": "SMTP_PASSWORD", "smtp_from": "SMTP_FROM",
}
IMPORTED_FLAG = "env_imported"

_lock = threading.Lock()
_cache: dict = {}
_loaded_at = 0.0


def _import_env_once(current: dict) -> dict:
    """Copy the old .env values (and credentials.json) into the database, the first time only."""
    if current.get(IMPORTED_FLAG):
        return current
    values = {k: os.getenv(env) for k, env in ENV_NAMES.items() if os.getenv(env) and k not in current}
    credentials = Path(os.getenv("GOOGLE_CREDENTIALS_FILE") or PROJECT_ROOT / "credentials.json")
    if "google_service_account" not in current and credentials.is_file():
        try:
            info = json.loads(credentials.read_text(encoding="utf-8"))
            if service_account_email(json.dumps(info)):
                values["google_service_account"] = json.dumps(info)
        except (OSError, ValueError):
            pass
    values[IMPORTED_FLAG] = "1"
    db.save_settings(values)
    if len(values) > 1:
        print(f"[Config] Imported {len(values) - 1} setting(s) from .env / credentials.json into the dashboard")
    return {**current, **values}


def _settings() -> dict:
    global _cache, _loaded_at
    if time.monotonic() - _loaded_at > CACHE_SECONDS:
        with _lock:
            if time.monotonic() - _loaded_at > CACHE_SECONDS:
                _cache = _import_env_once(db.get_settings())
                _loaded_at = time.monotonic()
    return _cache


def get(key: str) -> str:
    """The setting's value, its default, or ''."""
    return _settings().get(key) or DEFAULTS.get(key, "")


def save(values: dict):
    """Save settings (None deletes one) and make the change visible immediately in this process."""
    global _loaded_at
    db.save_settings(values)
    _loaded_at = 0.0


def is_set(key: str) -> bool:
    return bool(_settings().get(key))


def masked(key: str) -> str:
    """'saved, ends with …a1b2' for showing a secret without revealing it."""
    value = _settings().get(key) or ""
    if not value:
        return ""
    if key == "google_service_account":
        return service_account_email(value) or "saved"
    return f"saved, ends with …{value[-4:]}" if len(value) > 8 else "saved"


def service_account_email(raw: str) -> str:
    """client_email of a Google service-account key (JSON text), or '' if it isn't a valid one."""
    try:
        info = json.loads(raw)
    except (TypeError, ValueError):
        return ""
    if not isinstance(info, dict) or info.get("type") != "service_account":
        return ""
    if not (info.get("private_key") and info.get("client_email") and info.get("token_uri")):
        return ""
    return str(info["client_email"])


# ----------------------------------------------------------------- admin password

def check_admin_password(password: str) -> bool:
    """The password saved in the dashboard; until one is saved, ADMIN_PASSWORD from .env (first login)."""
    stored = _settings().get("admin_password_hash")
    if stored:
        return db.check_password(password, stored)
    first = os.getenv("ADMIN_PASSWORD", "")
    return bool(first) and secrets.compare_digest(password.encode(), first.encode())


def admin_login_available() -> bool:
    return bool(_settings().get("admin_password_hash") or os.getenv("ADMIN_PASSWORD"))


def set_admin_password(password: str):
    save({"admin_password_hash": db.hash_password(password)})


if __name__ == "__main__":
    if sys.argv[1:] == ["reset-admin"]:
        save({"admin_password_hash": None})
        print("Admin password reset. Log in with ADMIN_PASSWORD from the .env file, then set a new one.")
    else:
        print("Usage: python -m app.services.config reset-admin")
