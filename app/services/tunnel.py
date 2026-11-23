"""Keep WhatsApp connected through a free Cloudflare quick tunnel, and repair it when it breaks.

A quick tunnel gives AURA a public address (https://<random>.trycloudflare.com) so WaAPI can deliver
WhatsApp messages to this computer. Quick tunnels are temporary: if the connection drops for a while
(Wi-Fi change, laptop sleep), Cloudflare deletes the tunnel and cloudflared keeps retrying a tunnel
that no longer exists ("Tunnel not found"). So AURA runs cloudflared itself and watches it:

  every 30 s   ask cloudflared whether it has a live connection (/ready)
  healthy      make sure every active business's WaAPI instance points to <address>/webhook
               (new businesses are picked up within 30 s)
  broken 90 s  restart cloudflared -> new address -> all webhooks re-registered automatically

.env / docker compose:  PUBLIC_TUNNEL=cloudflare   (off when unset; needs the cloudflared program,
                        which the Docker image includes)
"""
import os
import shutil
import subprocess
import threading
import time

import requests

from app.services import config, db
from app.services.whatsapp import set_webhook

CHECK_EVERY = 30            # seconds between checks
BROKEN_AFTER = 90           # seconds without a live connection before the tunnel is replaced
METRICS = "127.0.0.1:20241"
LOCAL_URL = f"http://127.0.0.1:{os.getenv('AURA_INTERNAL_PORT', '8000')}"

_registered: set = set()    # (instance_id, public_url) pairs done with the current address


def enabled() -> bool:
    return os.getenv("PUBLIC_TUNNEL", "").lower() == "cloudflare"


def public_url():
    """https://<name>.trycloudflare.com of the running tunnel, or None."""
    try:
        hostname = requests.get(f"http://{METRICS}/quicktunnel", timeout=5).json().get("hostname")
    except (requests.RequestException, ValueError):
        return None
    return f"https://{hostname}" if hostname else None


def connected() -> bool:
    """True when cloudflared has at least one live connection to Cloudflare."""
    try:
        return requests.get(f"http://{METRICS}/ready", timeout=5).status_code == 200
    except requests.RequestException:
        return False


def register(url: str):
    """Point every active business's WaAPI instance at url/webhook (each once per address)."""
    if config.get("public_url") != url:
        config.save({"public_url": url})          # shown on the dashboard (Platform settings)
        print(f"[Tunnel] Public address: {url}")
    if not config.get("waapi_token"):
        return                                    # nothing can be registered before the token is saved
    for business in db.list_businesses(active_only=True):
        instance = business.get("waapi_instance_id")
        if instance and (instance, url) not in _registered:
            if set_webhook(f"{url}/webhook", instance):
                _registered.add((instance, url))
                print(f"[Tunnel] WhatsApp webhook set for {business['name']} (instance {instance})")


def _start_cloudflared():
    print("[Tunnel] Starting a Cloudflare quick tunnel...")
    return subprocess.Popen(
        ["cloudflared", "tunnel", "--no-autoupdate", "--metrics", METRICS, "--url", LOCAL_URL],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)   # its retry chatter would flood the logs


def _stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(10)
        except subprocess.TimeoutExpired:
            process.kill()


def check(state: dict, now: float) -> str:
    """One watchdog step. state: {"process", "last_ok"}. Returns "ok", "waiting" or "replaced"."""
    process = state["process"]
    if process.poll() is None and connected():
        state["last_ok"] = now
        url = public_url()
        if url:
            register(url)
        return "ok"
    down_for = now - state["last_ok"]
    if process.poll() is None and down_for <= BROKEN_AFTER:
        return "waiting"                          # starting up, or a short network blip
    reason = "stopped" if process.poll() is not None else f"no connection for {down_for:.0f} s"
    print(f"[Tunnel] Tunnel lost ({reason}) - starting a new one; webhooks will be re-registered")
    _stop(process)
    state.update(process=_start_cloudflared(), last_ok=now)
    return "replaced"


def _run():
    state = {"process": _start_cloudflared(), "last_ok": time.monotonic()}
    while True:
        time.sleep(CHECK_EVERY)
        try:
            check(state, time.monotonic())
        except Exception as e:                    # never let this thread die
            print(f"[Tunnel] Check failed: {e}")


def start():
    """Run and watch the tunnel in the background (only when PUBLIC_TUNNEL=cloudflare)."""
    if not enabled():
        return
    if not shutil.which("cloudflared"):
        print("[Tunnel] PUBLIC_TUNNEL=cloudflare but the cloudflared program isn't installed - WhatsApp "
              "webhooks won't be set automatically")
        return
    threading.Thread(target=_run, name="tunnel", daemon=True).start()
