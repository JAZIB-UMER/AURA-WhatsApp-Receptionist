import base64
import json
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse

from app.services import db
from app.services.agent import process_message
from app.services.state import r as redis_client
from app.services.messaging import record_incoming, send_to_customer
from app.services.voice import transcribe
from app.services.whatsapp import (Incoming, download_media, mark_seen, parse_incoming, resolve_chat_id,
                                   signature_ok, start_background_sync)

@asynccontextmanager
async def lifespan(_app: FastAPI):
    print("[AURA] Starting up...")
    from app.services import entity_model, intent_model
    from app.services.reminder import restore_jobs
    intent_model.load()  # load both models once now, so the first customer doesn't wait
    entity_model.load()
    restore_jobs()       # reminders live in memory; rebuild them from the database
    start_background_sync()   # every business's WhatsApp session running and pointed at this server
    yield


app = FastAPI(title="AURA - Autonomous WhatsApp Receptionist", lifespan=lifespan)


@app.get("/")
def home():
    return {"message": "AURA is running", "status": "ok"}


def _first_time_seen(message_id: str) -> bool:
    """WAHA may deliver the same message twice (webhook retries); only answer it once."""
    if not message_id:
        return True
    return bool(redis_client.set(f"seen:{message_id}", "1", nx=True, ex=3600))


def find_business(session: str):
    """Which business's WhatsApp received this? (None: no active business uses this WAHA session)"""
    return db.get_business_by_session(session)


VOICE_FAILED = ("Maaf kijiye, aap ka voice note samajh nahi aa saka 🙏 "
                "Please apna message likh kar bhej dein.")


def voice_to_text(voice: dict):
    """Voice note -> text (Whisper). None if it couldn't be read."""
    audio, mimetype = None, voice.get("mimetype") or "audio/ogg"
    if voice.get("data"):                               # only the test sender includes the file itself
        try:
            audio = base64.b64decode(voice["data"])
        except (ValueError, TypeError):
            audio = None
    if not audio:                                       # WAHA saved the file; fetch it from WAHA
        downloaded = download_media(voice.get("url"))
        if downloaded:
            audio, mimetype = downloaded
    return transcribe(audio, mimetype) if audio else None


def handle_message(message: Incoming):
    chat_id, text, session, voice = message.chat_id, message.text, message.session, message.voice
    try:
        business = find_business(session)
        if not business:
            print(f"[Error] No active business has WhatsApp session {session!r}. "
                  f"Link the business's WhatsApp on the dashboard (Settings -> WhatsApp).")
            return
        mark_seen(message.chat_id, message.message_id, session)     # blue ticks, as a person reading it
        # Privacy IDs (…@lid) -> the real number, so replies, reminders and the dashboard use the phone
        chat_id = resolve_chat_id(chat_id, session, message.phone_hint)
        phone = chat_id.split("@")[0]   # conversation key, e.g. 923001234567
        db.save_profile_name(business["id"], phone, message.profile_name)
        if voice:
            text = voice_to_text(voice)
            if not text:
                record_incoming(business, phone, "🎤 Voice note (could not be understood)")
                send_to_customer(business, phone, VOICE_FAILED, chat_id=chat_id)
                return
            record_incoming(business, phone, f"🎤 Voice note: {text}")
        print(f"[Message] {business['name']} <- {phone}: {text}")
        if not voice:
            record_incoming(business, phone, text)      # backed up before anything else can fail
        response = process_message(business, phone, text)
        if voice:   # show what AURA heard, so a mishearing is obvious to the customer
            response = f'🎤 _"{text}"_\n\n{response}'
        print(f"[AURA] {business['name']} -> {phone}: {response}")
        # reply to the exact chat, from the business's own WhatsApp number (and back it up)
        send_to_customer(business, phone, response, chat_id=chat_id)
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"[Error] {e}")


@app.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    raw = await request.body()
    if not signature_ok(raw, request.headers.get("X-Webhook-Hmac")):     # only WAHA can send messages in
        return JSONResponse(content={"status": "invalid signature"}, status_code=401)
    try:
        payload = json.loads(raw)
    except ValueError:
        return JSONResponse(content={"status": "invalid json"}, status_code=400)

    incoming = parse_incoming(payload)
    if not incoming:
        return JSONResponse(content={"status": "ignored"})

    if not _first_time_seen(incoming.message_id):
        return JSONResponse(content={"status": "duplicate"})

    # Answer WAHA immediately; do the slow work (number lookup, voice, models, Gemini, calendar) after.
    background_tasks.add_task(handle_message, incoming)
    return JSONResponse(content={"status": "received"})


@app.get("/webhook")
async def verify_webhook():
    return JSONResponse(content={"status": "ok"})


