import base64
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse

from app.services import db
from app.services.agent import process_message
from app.services.state import r as redis_client
from app.services.messaging import record_incoming, send_to_customer
from app.services.voice import transcribe
from app.services.whatsapp import Incoming, download_media, parse_incoming, resolve_chat_id

@asynccontextmanager
async def lifespan(_app: FastAPI):
    print("[AURA] Starting up...")
    from app.services import entity_model, intent_model, tunnel
    from app.services.reminder import restore_jobs
    intent_model.load()  # load both models once now, so the first customer doesn't wait
    entity_model.load()
    restore_jobs()       # reminders live in memory; rebuild them from the database
    tunnel.start()       # PUBLIC_TUNNEL=cloudflare: public address for WhatsApp, repaired when it breaks
    yield


app = FastAPI(title="AURA - Autonomous WhatsApp Receptionist", lifespan=lifespan)


@app.get("/")
def home():
    return {"message": "AURA is running", "status": "ok"}


def _first_time_seen(message_id: str) -> bool:
    """WaAPI may deliver the same message twice; only answer it once."""
    if not message_id:
        return True
    return bool(redis_client.set(f"seen:{message_id}", "1", nx=True, ex=3600))


def find_business(instance_id: str):
    """Which business does this WhatsApp number belong to? (None: no business uses this instance)"""
    return db.get_business_by_instance(instance_id) if instance_id else None


VOICE_FAILED = ("Maaf kijiye, aap ka voice note samajh nahi aa saka 🙏 "
                "Please apna message likh kar bhej dein.")


def voice_to_text(voice: dict, message_id: str, instance_id: str):
    """Voice note -> text (Whisper). None if it couldn't be read."""
    audio, mimetype = None, voice.get("mimetype") or "audio/ogg"
    if voice.get("data"):
        try:
            audio = base64.b64decode(voice["data"])
        except (ValueError, TypeError):
            audio = None
    if not audio:                                       # not in the webhook -> ask WaAPI for the file
        downloaded = download_media(message_id, instance_id)
        if downloaded:
            audio, mimetype = downloaded
    return transcribe(audio, mimetype) if audio else None


def handle_message(message: Incoming):
    chat_id, text, instance_id, voice = message.chat_id, message.text, message.instance_id, message.voice
    try:
        business = find_business(instance_id)
        if not business:
            print(f"[Error] No business is linked to WaAPI instance {instance_id!r}. "
                  f"Add it on the dashboard (Settings -> WaAPI instance ID).")
            return
        # Privacy IDs (…@lid) -> the real number, so replies, reminders and the dashboard use the phone
        chat_id = resolve_chat_id(chat_id, instance_id)
        phone = chat_id.split("@")[0]   # conversation key, e.g. 923001234567
        db.save_profile_name(business["id"], phone, message.profile_name)
        if voice:
            text = voice_to_text(voice, message.message_id, instance_id)
            if not text:
                record_incoming(business, phone, "🎤 Voice note (could not be understood)")
                send_to_customer(business, phone, VOICE_FAILED, chat_id=chat_id, instance_id=instance_id)
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
        send_to_customer(business, phone, response, chat_id=chat_id, instance_id=instance_id)
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"[Error] {e}")


@app.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(content={"status": "invalid json"}, status_code=400)

    incoming = parse_incoming(payload)
    if not incoming:
        return JSONResponse(content={"status": "ignored"})

    if not _first_time_seen(incoming.message_id):
        return JSONResponse(content={"status": "duplicate"})

    # Answer WaAPI immediately; do the slow work (number lookup, voice, models, Gemini, calendar) after.
    background_tasks.add_task(handle_message, incoming)
    return JSONResponse(content={"status": "received"})


@app.get("/webhook")
async def verify_webhook():
    return JSONResponse(content={"status": "ok"})


