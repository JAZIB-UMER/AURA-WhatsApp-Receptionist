"""Voice notes: WaAPI webhook -> audio -> Whisper -> AURA reply. Offline (Groq, WaAPI, Gemini faked).

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


def payload(kind="ptt", body="", media=None):
    return {"event": "message", "instanceId": "108128",
            "data": {"message": {"type": kind, "body": body, "from": "923001234567@c.us", "fromMe": False,
                                 "id": {"_serialized": "false_923001234567@c.us_ABC"}},
                     **({"media": media} if media else {})}}


class FakeResponse:
    def __init__(self, body, status=200):
        self.status_code, self._body, self.text = status, body, str(body)

    def json(self):
        return self._body


print("Webhook parsing")
audio = b"OggS fake voice"
p = whatsapp.parse_incoming(payload(media={"mimetype": "audio/ogg; codecs=opus",
                                           "data": base64.b64encode(audio).decode()}))
check("voice note -> empty text + audio and mimetype", p and p.text == "" and p.voice["mimetype"].startswith("audio/ogg")
      and base64.b64decode(p.voice["data"]) == audio and p.message_id == "false_923001234567@c.us_ABC")
p = whatsapp.parse_incoming(payload("chat", "kal 3 baje haircut"))
check("text message unchanged, no voice", p and p.text == "kal 3 baje haircut" and p.voice is None)
check("images / stickers ignored", whatsapp.parse_incoming(payload("image")) is None)
check("forwarded audio file accepted too", whatsapp.parse_incoming(payload("audio")) is not None)

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
BUSINESS = {"id": 1, "name": "Glow Salon"}
sent, logged = [], []
with mock.patch.object(main, "find_business", return_value=BUSINESS), \
        mock.patch.object(main, "record_incoming", side_effect=lambda b, ph, t: logged.append(t)), \
        mock.patch.object(main, "send_to_customer", side_effect=lambda b, ph, t, **k: sent.append(t)), \
        mock.patch.object(main, "process_message", side_effect=lambda b, ph, t: f"REPLY to {t}"), \
        mock.patch.object(main, "transcribe", return_value="kal 3 baje haircut") as tr, \
        mock.patch.object(main, "download_media", return_value=(b"downloaded", "audio/ogg")) as dl:
    main.handle_message(Incoming("923001234567@c.us", "", "MSG1", "108128",
                                 {"data": base64.b64encode(audio).decode(), "mimetype": "audio/ogg"}))
    check("webhook audio transcribed (no download needed)", tr.call_args[0][0] == audio and not dl.called)
    check("backup says it was a voice note", logged[-1] == "🎤 Voice note: kal 3 baje haircut")
    check("reply shows what AURA heard, then answers it",
          sent[-1].startswith('🎤 _"kal 3 baje haircut"_') and sent[-1].endswith("REPLY to kal 3 baje haircut"))

    main.handle_message(Incoming("923001234567@c.us", "", "MSG2", "108128", {"data": None, "mimetype": "audio/ogg"}))
    check("audio missing from webhook -> downloaded from WaAPI",
          dl.call_args[0] == ("MSG2", "108128") and tr.call_args[0][0] == b"downloaded")

    tr.return_value = None
    main.handle_message(Incoming("923001234567@c.us", "", "MSG3", "108128", {"data": "x", "mimetype": "audio/ogg"}))
    check("unreadable voice note -> polite 'please type it'", sent[-1] == main.VOICE_FAILED
          and logged[-1] == "🎤 Voice note (could not be understood)")

    main.handle_message(Incoming("923001234567@c.us", "Salam", "MSG4", "108128"))
    check("text messages work as before", logged[-1] == "Salam" and sent[-1] == "REPLY to Salam")

print("Privacy IDs (@lid) and WhatsApp profile names")
lid_payload = payload("chat", "Hi")
lid_payload["data"]["message"].update({"from": "257139759173829@lid", "_data": {"notifyName": "Fatima​  K\n"}})
p = whatsapp.parse_incoming(lid_payload)
check("profile name read from the webhook (cleaned)", p and p.profile_name == "Fatima K" and p.chat_id.endswith("@lid"))

LID_ANSWER = {"data": {"status": "success",
                       "data": {"contacts": [{"lid": "257139759173829@lid", "pn": "923007654321@c.us"}]}}}
whatsapp._phone_cache.clear()
with mock.patch.object(whatsapp.requests, "post", return_value=FakeResponse(LID_ANSWER)) as post:
    check("privacy ID -> real number", whatsapp.resolve_chat_id("257139759173829@lid", "108215") == "923007654321@c.us")
    whatsapp.resolve_chat_id("257139759173829@lid", "108215")
    check("looked up once, then remembered", post.call_count == 1)
    check("normal numbers are not looked up",
          whatsapp.resolve_chat_id("923001234567@c.us", "108215") == "923001234567@c.us" and post.call_count == 1)
whatsapp._phone_cache.clear()
with mock.patch.object(whatsapp.requests, "post", side_effect=whatsapp.requests.RequestException("down")):
    check("lookup fails -> keeps the privacy ID (reply still attempted)",
          whatsapp.resolve_chat_id("257139759173829@lid", "108215") == "257139759173829@lid")

real = db.get_business(db.create_business("Fatima Salon", "fatima", "secret123", waapi_instance_id="108215"))
replies = []
whatsapp._phone_cache.clear()
with mock.patch.object(main, "find_business", return_value=real), \
        mock.patch.object(whatsapp.requests, "post", return_value=FakeResponse(LID_ANSWER)), \
        mock.patch.object(main, "process_message", side_effect=lambda b, ph, t: f"REPLY to {t}"), \
        mock.patch("app.services.messaging.send_message",
                   side_effect=lambda to, t, instance_id=None: replies.append(to) or True):
    main.handle_message(whatsapp.parse_incoming(lid_payload))
chats = db.list_conversations(real["id"])
check("reply goes to the real number (the trial accepts it)", replies == ["923007654321@c.us"])
check("dashboard shows the real number and the WhatsApp name",
      [(c["phone"], c["name"]) for c in chats] == [("923007654321", "Fatima K")])
db.create_booking(real["id"], "923007654321", "Fatima Azeem", "Haircut", "2030-01-01", "10:00", 30, "EV1")
check("the booking name wins over the WhatsApp name", db.list_conversations(real["id"])[0]["name"] == "Fatima Azeem")
check("the WhatsApp name is never used as a booking name", db.last_customer_name(real["id"], "923000000999") is None)

print(f"\n{sum(checks)}/{len(checks)} checks passed")
sys.exit(0 if all(checks) else 1)
