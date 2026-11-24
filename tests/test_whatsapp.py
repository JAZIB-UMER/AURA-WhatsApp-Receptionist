"""WhatsApp through WAHA: sending, signed webhooks, linking sessions, voice-note files. Offline (WAHA faked).

Run:  python tests/test_whatsapp.py
"""
import asyncio
import hashlib
import hmac
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["AURA_DB"] = os.path.join(tempfile.mkdtemp(), "whatsapp_test.db")   # never touch the real database

import main  # noqa: E402
from app.services import config, db, manage, whatsapp  # noqa: E402

checks = []


def check(name, cond, detail=""):
    checks.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + ("" if cond else f"  -> {detail}"))


class FakeResponse:
    def __init__(self, status=200, body=None, content=b"", headers=None):
        self.status_code, self._body, self.content = status, body, content
        self.text, self.headers, self.ok = json.dumps(body) if body is not None else "", headers or {}, status < 400

    def json(self):
        return self._body

    def raise_for_status(self):
        if not self.ok:
            raise whatsapp.requests.HTTPError(str(self.status_code))


class FakeWaha:
    """Answers AURA's WAHA calls from a dict of sessions, and records every call."""
    def __init__(self):
        self.sessions, self.calls, self.down = {}, [], False

    def __call__(self, method, url, headers=None, timeout=None, json=None, **kw):
        path = url.replace(whatsapp.WAHA_URL, "")
        self.calls.append((method, path, json, headers))
        if self.down:
            raise whatsapp.requests.ConnectionError("refused")
        if path == "/api/server/version":
            return FakeResponse(body={"version": "2026.10.1", "engine": "GOWS", "tier": "CORE"})
        if path == "/api/sessions" and method == "POST":
            self.sessions[json["name"]] = {"name": json["name"], "status": "SCAN_QR_CODE", "config": json["config"]}
            return FakeResponse(201, self.sessions[json["name"]])
        if path.startswith("/api/sessions/"):
            name, _, action = path[len("/api/sessions/"):].partition("/")
            if name not in self.sessions:
                return FakeResponse(404, {"error": "not found"})
            if method == "GET":
                return FakeResponse(body=self.sessions[name])
            if method == "PUT":
                self.sessions[name]["config"] = json["config"]
                return FakeResponse(body=self.sessions[name])
            if method == "DELETE":
                del self.sessions[name]
                return FakeResponse(body={})
            if action in ("start", "restart"):
                self.sessions[name]["status"] = "STARTING"
                return FakeResponse(201, self.sessions[name])
        if path.endswith("/auth/qr"):
            return FakeResponse(content=b"\x89PNG qr", headers={"content-type": "image/png"})
        if path == "/api/sendText":
            return FakeResponse(201, {"id": "true_x"})
        if path == "/api/sendSeen":
            return FakeResponse(200, {})
        if path.startswith("/api/files/"):
            return FakeResponse(content=b"OggS audio", headers={"content-type": "audio/ogg"})
        return FakeResponse(404, {"error": "unknown"})


waha = FakeWaha()
patch = mock.patch.object(whatsapp.requests, "request", side_effect=waha)
patch.start()

print("Sending")
check("reply sent through WAHA from the business's session",
      whatsapp.send_message("0300 1234567", "Salam", "aura_1_x") is True
      and waha.calls[-1][1:3] == ("/api/sendText", {"session": "aura_1_x", "chatId": "923001234567@c.us",
                                                       "text": "Salam"}), waha.calls[-1])
check("WAHA's API key sent with every call", waha.calls[-1][3]["X-Api-Key"] == os.getenv("WAHA_API_KEY", ""))
check("no WhatsApp linked -> not sent, no call", whatsapp.send_message("03001234567", "x", None) is False
      and waha.calls[-1][1] == "/api/sendText" and len(waha.calls) == 1)
waha.down = True
check("WAHA down -> False, no crash", whatsapp.send_message("03001234567", "x", "aura_1_x") is False)
waha.down = False
check("chat IDs: device numbers and WhatsApp IDs normalised",
      whatsapp.to_chat_id("923001234567:12@s.whatsapp.net") == "923001234567@c.us"
      and whatsapp.to_chat_id("257139759173829@lid") == "257139759173829@lid")

print("Linking a business's WhatsApp")
session = whatsapp.new_session_name(7)
check("session name is unique per business", session.startswith("aura_7_") and session != whatsapp.new_session_name(7))
ok, _ = whatsapp.start_session(session)
created = waha.sessions.get(session, {})
hook = (created.get("config") or {}).get("webhooks", [{}])[0]
check("new session created and started in WAHA", ok and created.get("status") == "SCAN_QR_CODE")
check("its webhook points at AURA, signed with AURA's secret, only for messages",
      hook.get("url") == whatsapp.WEBHOOK_URL and hook.get("events") == ["message"]
      and hook.get("hmac", {}).get("key") == config.webhook_secret(), hook)
check("the webhook secret stays the same", config.webhook_secret() == config.webhook_secret()
      and len(config.webhook_secret()) == 64)
check("QR code image while waiting for the phone", whatsapp.qr_image(session) == b"\x89PNG qr")

waha.sessions[session].update(status="WORKING", me={"id": "923001234567@c.us", "pushName": "Glow Salon"})
info = whatsapp.session_info(session)
check("connected session: status, number and name", (info["status"], info["phone"], info["name"])
      == ("WORKING", "923001234567", "Glow Salon"), info)
calls = len(waha.calls)
whatsapp.start_session(session)
check("starting an already linked, correct session changes nothing",
      all(c[0] == "GET" for c in waha.calls[calls:]) and waha.sessions[session]["status"] == "WORKING")

waha.sessions[session]["config"]["webhooks"][0]["url"] = "http://old-address/webhook"
whatsapp.start_session(session)
check("an old webhook address is corrected", waha.sessions[session]["config"]["webhooks"][0]["url"]
      == whatsapp.WEBHOOK_URL)
waha.sessions[session]["status"] = "FAILED"
whatsapp.start_session(session)
check("an expired / failed session is restarted (new QR code)", waha.sessions[session]["status"] == "STARTING")
del waha.sessions[session]
whatsapp.start_session(session)
check("a session WAHA lost (new volume) is created again", session in waha.sessions)
check("unknown session -> MISSING", whatsapp.session_info("aura_99_none")["status"] == whatsapp.MISSING)
waha.down = True
check("WAHA down -> UNREACHABLE, and starting reports it",
      whatsapp.session_info(session)["status"] == whatsapp.UNREACHABLE
      and not whatsapp.start_session(session)[0] and "not reachable" in whatsapp.start_session(session)[1])
check("server check explains WAHA is down", not whatsapp.server_status()[0])
waha.down = False
check("server check shows version and engine", whatsapp.server_status() ==
      (True, "WhatsApp server running: WAHA 2026.10.1 (GOWS engine)."))
check("disconnect deletes the session", whatsapp.delete_session(session)[0] and session not in waha.sessions)
check("disconnecting an already gone session is fine", whatsapp.delete_session(session)[0])

print("Dashboard check per business")
bid = db.create_business("Glow Salon", "glow", "secret123")
check("no WhatsApp linked -> says how to connect", "Connect" in manage.check_whatsapp(db.get_business(bid))[1])
db.update_business(bid, whatsapp_session=session)
whatsapp.start_session(session)
check("waiting for the QR scan -> says so", "QR code" in manage.check_whatsapp(db.get_business(bid))[1])
waha.sessions[session].update(status="WORKING", me={"id": "923001234567@c.us"})
check("connected -> shows the number", manage.check_whatsapp(db.get_business(bid))
      == (True, "WhatsApp connected: 0300 1234567."))
check("business found by its session", main.find_business(session)["id"] == bid)
db.update_business(bid, active=0)
check("inactive business is not answered", main.find_business(session) is None)
db.update_business(bid, active=1)
other = db.create_business("City Clinic", "clinic", "secret123")
try:
    db.update_business(other, whatsapp_session=session)
    check("one WhatsApp session can't belong to two businesses", False)
except db.sqlite3.IntegrityError:
    check("one WhatsApp session can't belong to two businesses", True)
db.update_business(other, whatsapp_session="")
check("several businesses without WhatsApp are fine", db.get_business(other)["whatsapp_session"] is None)

print("Server start: every linked business's session running")
waha.sessions[session]["status"] = "STOPPED"
waha.sessions[session]["config"]["webhooks"][0]["url"] = "http://old-address/webhook"
whatsapp.sync_sessions(attempts=1, wait=0)
check("stopped session started and webhook corrected", waha.sessions[session]["status"] == "STARTING"
      and waha.sessions[session]["config"]["webhooks"][0]["url"] == whatsapp.WEBHOOK_URL)
waha.down = True
with mock.patch.object(whatsapp.time, "sleep") as sleep:
    whatsapp.sync_sessions(attempts=3, wait=5)
check("WAHA still starting -> AURA waits and retries, no crash", sleep.call_count == 3)
waha.down = False

print("Voice-note files")
waha.calls.clear()
data = whatsapp.download_media("http://localhost:3000/api/files/aura_7_x/ABC.oga")
check("file fetched from WAHA with the API key", data == (b"OggS audio", "audio/ogg")
      and waha.calls[-1][1] == "/api/files/aura_7_x/ABC.oga")
whatsapp.download_media("http://evil.example.com/api/files/steal.oga?x=1")
check("whatever host a link names, AURA only asks its own WAHA (key never leaks)",
      waha.calls[-1][1] == "/api/files/steal.oga?x=1")
check("no link -> None", whatsapp.download_media(None) is None)

print("Webhook security")


class FakeRequest:
    def __init__(self, body: bytes, signature=None):
        self._body, self.headers = body, ({"X-Webhook-Hmac": signature} if signature else {})

    async def body(self):
        return self._body


def post(body: dict, signature="good"):
    raw = json.dumps(body).encode()
    if signature == "good":
        signature = hmac.new(config.webhook_secret().encode(), raw, hashlib.sha512).hexdigest()
    tasks = main.BackgroundTasks()
    response = asyncio.run(main.webhook(FakeRequest(raw, signature), tasks))
    return response.status_code, json.loads(response.body), tasks


message = {"event": "message", "session": session,
           "payload": {"id": "false_923001234567@c.us_M1", "from": "923001234567@c.us", "fromMe": False,
                       "body": "Salam", "hasMedia": False}}
with mock.patch.object(main, "redis_client") as redis:
    redis.set.return_value = True
    status, body, tasks = post(message)
    check("signed message accepted and handled in the background",
          status == 200 and body["status"] == "received" and len(tasks.tasks) == 1)
    check("unsigned message refused", post(message, signature=None)[0] == 401)
    check("wrong signature refused", post(message, signature="ab" * 64)[0] == 401)
    check("message signed with another secret refused",
          post(message, signature=hmac.new(b"other", json.dumps(message).encode(), hashlib.sha512).hexdigest())[0]
          == 401)
    check("own messages ignored", post({**message, "payload": {**message["payload"], "fromMe": True}})[1]["status"]
          == "ignored")
    check("group messages ignored", post({**message, "payload": {**message["payload"], "from": "1203@g.us"}})[1]
          ["status"] == "ignored")
    check("other events ignored", post({**message, "event": "session.status"})[1]["status"] == "ignored")
    redis.set.return_value = None
    check("a retried (duplicate) message is answered only once", post(message)[1]["status"] == "duplicate")

patch.stop()
print(f"\n{sum(checks)}/{len(checks)} checks passed")
sys.exit(0 if all(checks) else 1)
