"""Automatic WhatsApp webhook through the Cloudflare tunnel, and repairing a dead tunnel.
Offline: cloudflared and WaAPI are faked.

Run:  python tests/test_tunnel.py
"""
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["AURA_DB"] = os.path.join(tempfile.mkdtemp(), "tunnel_test.db")   # never touch the real database

from app.services import config, db, tunnel  # noqa: E402

checks = []


def check(name, cond):
    checks.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")


class FakeProcess:
    """A running (or stopped) cloudflared."""
    def __init__(self, name):
        self.name, self.code, self.terminated = name, None, False

    def poll(self):
        return self.code

    def terminate(self):
        self.terminated, self.code = True, 0

    def wait(self, timeout=None):
        return self.code

    def kill(self):
        self.code = -9


a = db.create_business("Glow Salon", "glow", "secret123", waapi_instance_id="111")
db.create_business("City Clinic", "clinic", "secret123", waapi_instance_id="222")
config.save({"waapi_token": "waapi_test_token_0123456789"})

# ----------------------------------------------------------------- registering webhooks
print("Registering webhooks")
hooks = []
with mock.patch.object(tunnel, "set_webhook", side_effect=lambda url, inst: hooks.append((url, inst)) or True):
    tunnel.register("https://blue-sky.trycloudflare.com")
    check("every business's instance gets the webhook",
          sorted(hooks) == [("https://blue-sky.trycloudflare.com/webhook", "111"),
                            ("https://blue-sky.trycloudflare.com/webhook", "222")])
    check("public address saved for the dashboard", config.get("public_url") == "https://blue-sky.trycloudflare.com")
    hooks.clear()
    tunnel.register("https://blue-sky.trycloudflare.com")
    check("not registered again on every check", not hooks)
    db.create_business("Fix It", "fixit", "secret123", waapi_instance_id="333")
    tunnel.register("https://blue-sky.trycloudflare.com")
    check("a business added later is picked up", hooks == [("https://blue-sky.trycloudflare.com/webhook", "333")])
    hooks.clear()
    tunnel.register("https://new-name.trycloudflare.com")
    check("new address -> all re-registered", len(hooks) == 3
          and all(url == "https://new-name.trycloudflare.com/webhook" for url, _ in hooks))

hooks.clear()
db.update_business(a, active=0)
with mock.patch.object(tunnel, "set_webhook", side_effect=lambda url, inst: hooks.append((url, inst)) or False):
    tunnel.register("https://third.trycloudflare.com")
    check("inactive businesses are skipped", "111" not in [inst for _, inst in hooks])
    hooks.clear()
    tunnel.register("https://third.trycloudflare.com")
    check("a failed registration is retried next time", len(hooks) == 2)

config.save({"waapi_token": None})
hooks.clear()
with mock.patch.object(tunnel, "set_webhook", side_effect=lambda url, inst: hooks.append((url, inst)) or True):
    tunnel.register("https://fourth.trycloudflare.com")
    check("no WaAPI token yet -> waits instead of failing", not hooks)
config.save({"waapi_token": "waapi_test_token_0123456789"})

# ----------------------------------------------------------------- watchdog
print("Watchdog (dead tunnel is replaced)")
started = []


def fake_start():
    started.append(FakeProcess(f"cloudflared-{len(started) + 1}"))
    return started[-1]


first = FakeProcess("cloudflared-0")
state = {"process": first, "last_ok": 0.0}
with mock.patch.object(tunnel, "_start_cloudflared", side_effect=fake_start), \
        mock.patch.object(tunnel, "register") as reg, \
        mock.patch.object(tunnel, "public_url", return_value="https://live.trycloudflare.com"):
    with mock.patch.object(tunnel, "connected", return_value=True):
        check("healthy tunnel -> webhooks kept in sync", tunnel.check(state, 30) == "ok"
              and reg.call_args[0][0] == "https://live.trycloudflare.com")
    with mock.patch.object(tunnel, "connected", return_value=False):
        check("short drop (60 s) -> waits, nothing restarted", tunnel.check(state, 90) == "waiting" and not started)
        result = tunnel.check(state, 30 + tunnel.BROKEN_AFTER + 1)
        check("no connection for over 90 s ('Tunnel not found') -> replaced with a new tunnel",
              result == "replaced" and first.terminated and len(started) == 1)
        check("the new tunnel gets its own start-up time", tunnel.check(state, 30 + tunnel.BROKEN_AFTER + 31)
              == "waiting" and len(started) == 1)
    state["process"].code = 1
    with mock.patch.object(tunnel, "connected", return_value=False):
        check("cloudflared crashed -> restarted at once", tunnel.check(state, 200) == "replaced" and len(started) == 2)

with mock.patch.dict(os.environ, {"PUBLIC_TUNNEL": ""}), mock.patch.object(tunnel.threading, "Thread") as thread:
    tunnel.start()
    check("off unless PUBLIC_TUNNEL=cloudflare", not thread.called)
with mock.patch.dict(os.environ, {"PUBLIC_TUNNEL": "cloudflare"}), mock.patch.object(tunnel.shutil, "which",
                                                                                    return_value=None), \
        mock.patch.object(tunnel.threading, "Thread") as thread:
    tunnel.start()
    check("cloudflared not installed -> a clear message, no crash", not thread.called)

print(f"\n{sum(checks)}/{len(checks)} checks passed")
sys.exit(0 if all(checks) else 1)
