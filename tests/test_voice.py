"""Voice notes: WAHA webhook -> audio -> Whisper -> AURA reply. Offline (Groq, WAHA, Gemini faked).

Run:  python tests/test_voice.py
"""
import base64
import os
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tempfile  # noqa: E402
os.environ["AURA_DB"] = os.path.join(tempfile.mkdtemp(), "voice_test.db")   # never touch the real database

import main  # noqa: E402
from app.services import config, db, voice, whatsapp  # noqa: E402
from app.services.whatsapp import Incoming  # noqa: E402

config.save({"groq_api_key": "gsk_test_key_0123456789"})

checks = []


def check(name, cond):
    checks.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")


def payload(body="", media=None, sender="923001234567@c.us", data=None):
    """A WAHA 'message' webhook as the GOWS engine sends it."""
    message = {"id": "false_923001234567@c.us_ABC", "from": sender, "fromMe": False, "body": body,
               "hasMedia": bool(media), "media": media, "_data": data or {}}
    return {"event": "message", "session": "aura_1_abc123", "payload": message}


class FakeResponse:
    def __init__(self, body, status=200, content=b"", headers=None):
        self.status_code, self._body, self.text = status, body, str(body)
        self.content, self.headers = content, headers or {}
        self.ok = status < 400

    def json(self):
        return self._body

    def raise_for_status(self):
        if not self.ok:
            raise whatsapp.requests.HTTPError(str(self.status_code))


print("Webhook parsing")
VOICE_MEDIA = {"url": "http://waha:3000/api/files/aura_1_abc123/ABC.oga", "mimetype": "audio/ogg; codecs=opus"}
p = whatsapp.parse_incoming(payload(media=VOICE_MEDIA))
check("voice note -> empty text + WAHA file link and mimetype", p and p.text == "" and p.voice["url"] == VOICE_MEDIA["url"]
      and p.voice["mimetype"].startswith("audio/ogg") and p.message_id == "false_923001234567@c.us_ABC"
      and p.session == "aura_1_abc123")
p = whatsapp.parse_incoming(payload("kal 3 baje haircut"))
check("text message unchanged, no voice", p and p.text == "kal 3 baje haircut" and p.voice is None)
check("images / stickers ignored",
      whatsapp.parse_incoming(payload(media={"url": "http://waha:3000/x.jpg", "mimetype": "image/jpeg"})) is None)
gows_voice = payload(media={"url": None, "mimetype": None}, data={"Message": {"audioMessage": {"PTT": True}}})
check("voice note recognised even when WAHA couldn't save the file",
      (whatsapp.parse_incoming(gows_voice) or Incoming("", "x", "", "")).voice is not None)

print("Whisper result handling")
speech = {"text": " کل تین بجے", "segments": [{"text": " کل تین بجے", "no_speech_prob": 0.01}]}
silence = {"text": " you", "segments": [{"text": " you", "no_speech_prob": 0.70}]}
with mock.patch.object(voice.requests, "post", return_value=FakeResponse(silence)):
    check("silence -> None (Whisper's invented 'you' is dropped)", voice.transcribe(b"x") is None)
with mock.patch.object(voice.requests, "post", return_value=FakeResponse(speech)), \
        mock.patch("app.services.nlu.to_roman_urdu", return_value="kal 3 baje"):
    check("Urdu script -> Roman Urdu", voice.transcribe(b"x") == "kal 3 baje")
english = {"text": "tomorrow at 3", "segments": [{"text": "tomorrow at 3", "no_speech_prob": 0.01}]}
with mock.patch.object(voice.requests, "post", side_effect=[FakeResponse(speech), FakeResponse(english)]), \
        mock.patch("app.services.nlu.to_roman_urdu", return_value=None):
    check("Gemini down -> English translation", voice.transcribe(b"x") == "tomorrow at 3")
with mock.patch.object(voice.requests, "post", return_value=FakeResponse({"error": "bad"}, 401)):
    check("Groq error -> None", voice.transcribe(b"x") is None)
check("too large -> None", voice.transcribe(b"x" * (voice.MAX_BYTES + 1)) is None)
config.save({"groq_api_key": None})
check("no API key -> None", voice.transcribe(b"x") is None)
config.save({"groq_api_key": "gsk_test_key_0123456789"})

print("Server flow")
audio = b"OggS fake voice"
BUSINESS = {"id": 1, "name": "Glow Salon", "whatsapp_session": "aura_1_abc123"}
sent, logged = [], []
with mock.patch.object(main, "find_business", return_value=BUSINESS), \
        mock.patch.object(main, "mark_seen"), \
        mock.patch.object(main, "record_incoming", side_effect=lambda b, ph, t: logged.append(t)), \
        mock.patch.object(main, "send_to_customer", side_effect=lambda b, ph, t, **k: sent.append(t)), \
        mock.patch.object(main, "process_message", side_effect=lambda b, ph, t: f"REPLY to {t}"), \
        mock.patch.object(main, "transcribe", return_value="kal 3 baje haircut") as tr, \
        mock.patch.object(main, "download_media", return_value=(b"downloaded", "audio/ogg")) as dl:
    main.handle_message(Incoming("923001234567@c.us", "", "MSG1", "aura_1_abc123",
                                 {"url": VOICE_MEDIA["url"], "mimetype": "audio/ogg"}))
    check("voice note fetched from WAHA and transcribed",
          dl.call_args[0] == (VOICE_MEDIA["url"],) and tr.call_args[0][0] == b"downloaded")
    check("backup says it was a voice note", logged[-1] == "🎤 Voice note: kal 3 baje haircut")
    check("reply shows what AURA heard, then answers it",
          sent[-1].startswith('🎤 _"kal 3 baje haircut"_') and sent[-1].endswith("REPLY to kal 3 baje haircut"))

    dl.reset_mock()
    main.handle_message(Incoming("923001234567@c.us", "", "MSG2", "aura_1_abc123",
                                 {"data": base64.b64encode(audio).decode(), "mimetype": "audio/ogg"}))
    check("audio inside the webhook (test sender) -> no download", tr.call_args[0][0] == audio and not dl.called)

    tr.return_value = None
    main.handle_message(Incoming("923001234567@c.us", "", "MSG3", "aura_1_abc123", {"url": None, "mimetype": "audio/ogg"}))
    check("unreadable voice note -> polite 'please type it'", sent[-1] == main.VOICE_FAILED
          and logged[-1] == "🎤 Voice note (could not be understood)")

    main.handle_message(Incoming("923001234567@c.us", "Salam", "MSG4", "aura_1_abc123"))
    check("text messages work as before", logged[-1] == "Salam" and sent[-1] == "REPLY to Salam")

print("Privacy IDs (@lid) and WhatsApp profile names")
lid_payload = payload("Hi", sender="257139759173829@lid", data={"Info": {"PushName": "Fatima​  K\n"}})
p = whatsapp.parse_incoming(lid_payload)
check("profile name read from the webhook (cleaned)", p and p.profile_name == "Fatima K" and p.chat_id.endswith("@lid"))
hinted = payload("Hi", sender="257139759173829@lid",
                 data={"Info": {"SenderAlt": "923007654321:5@s.whatsapp.net"}})
check("real number taken from the webhook when WAHA includes it (device suffix dropped)",
      whatsapp.parse_incoming(hinted).phone_hint == "923007654321@c.us")

LID_ANSWER = {"lid": "257139759173829@lid", "pn": "923007654321@c.us"}
whatsapp._phone_cache.clear()
with mock.patch.object(whatsapp.requests, "request", return_value=FakeResponse(LID_ANSWER)) as call:
    check("privacy ID -> real number", whatsapp.resolve_chat_id("257139759173829@lid", "aura_1_abc123")
          == "923007654321@c.us")
    whatsapp.resolve_chat_id("257139759173829@lid", "aura_1_abc123")
    check("looked up once, then remembered", call.call_count == 1)
    check("asked WAHA's lids API for that session", "/api/aura_1_abc123/lids/257139759173829%40lid"
          in call.call_args[0][1])
    check("normal numbers are not looked up",
          whatsapp.resolve_chat_id("923001234567@c.us", "aura_1_abc123") == "923001234567@c.us" and call.call_count == 1)
whatsapp._phone_cache.clear()
with mock.patch.object(whatsapp.requests, "request") as call:
    check("number from the webhook used without asking WAHA",
          whatsapp.resolve_chat_id("257139759173829@lid", "s", "923007654321@c.us") == "923007654321@c.us"
          and not call.called)
whatsapp._phone_cache.clear()
with mock.patch.object(whatsapp.requests, "request", side_effect=whatsapp.requests.RequestException("down")):
    check("lookup fails -> keeps the privacy ID (reply still attempted)",
          whatsapp.resolve_chat_id("257139759173829@lid", "aura_1_abc123") == "257139759173829@lid")

real = db.get_business(db.create_business("Fatima Salon", "fatima", "secret123", whatsapp_session="aura_1_abc123"))
replies = []
whatsapp._phone_cache.clear()
with mock.patch.object(whatsapp.requests, "request", return_value=FakeResponse(LID_ANSWER)), \
        mock.patch.object(main, "process_message", side_effect=lambda b, ph, t: f"REPLY to {t}"), \
        mock.patch("app.services.messaging.send_message",
                   side_effect=lambda to, t, session=None: replies.append((to, session)) or True):
    main.handle_message(whatsapp.parse_incoming(lid_payload))
chats = db.list_conversations(real["id"])
check("reply goes to the real number, from the business's own WhatsApp",
      replies == [("923007654321@c.us", "aura_1_abc123")])
check("dashboard shows the real number and the WhatsApp name",
      [(c["phone"], c["name"]) for c in chats] == [("923007654321", "Fatima K")])
db.create_booking(real["id"], "923007654321", "Fatima Azeem", "Haircut", "2030-01-01", "10:00", 30, "EV1")
check("the booking name wins over the WhatsApp name", db.list_conversations(real["id"])[0]["name"] == "Fatima Azeem")
check("the WhatsApp name is never used as a booking name", db.last_customer_name(real["id"], "923000000999") is None)

print(f"\n{sum(checks)}/{len(checks)} checks passed")
sys.exit(0 if all(checks) else 1)
