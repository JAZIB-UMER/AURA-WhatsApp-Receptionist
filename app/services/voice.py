"""Voice notes -> text with Whisper (hosted by Groq, free tier).

WhatsApp voice notes are OGG/Opus clips. AURA turns them into text and then handles them exactly
like a typed message (intent model, entity model, booking flow).

Whisper writes Urdu speech in Urdu (sometimes Hindi) script: "کل 3 بجے haircut کی appointment book کر دیں".
That transcript is accurate, but AURA's models read Roman Urdu and English. So:
  1. transcribe (Whisper)
  2. Urdu/Hindi script -> Roman Urdu with Gemini, script only: "kal 3 baje haircut ki appointment book kar dein"
  3. Gemini down -> Whisper's translate mode (speech -> English). Last resort only: tested on Urdu
     clips it garbles details ("facial" -> "official", "haircut" -> "hair kit").

Groq API key and Whisper model are platform settings (dashboard -> Platform settings).
whisper-large-v3 is the default because it also supports the translate fallback.
"""
import re

import requests

from app.services import config

GROQ_URL = "https://api.groq.com/openai/v1/audio"
MAX_BYTES = 10 * 1024 * 1024        # ~10 min of WhatsApp voice; Groq's free limit is 25 MB
TIMEOUT = 60
URDU_SCRIPT = re.compile(r"[؀-ۿݐ-ݿऀ-ॿ]")     # Urdu/Arabic and Hindi letters
# No style prompt on purpose: with one, Whisper "hears" prompt words in silence ('Shukriya' at
# no_speech 0.14); without it, silence scores ~0.70 and real speech ~0.01, so NO_SPEECH separates them.


def configured() -> bool:
    return bool(config.get("groq_api_key"))


def check_groq(key: "str | None" = None, model: "str | None" = None):
    """(ok, message) — does this Groq key work, and does it offer this Whisper model?"""
    key, model = key or config.get("groq_api_key"), model or config.get("whisper_model")
    if not key:
        return False, "No Groq API key saved."
    try:
        response = requests.get("https://api.groq.com/openai/v1/models",
                                headers={"Authorization": f"Bearer {key}"}, timeout=TIMEOUT)
    except requests.RequestException as e:
        return False, f"Groq unreachable: {str(e)[:120]}"
    if response.status_code in (401, 403):
        return False, "Groq rejected the API key. Create one at console.groq.com → API Keys."
    if response.status_code != 200:
        return False, f"Groq answered {response.status_code}: {response.text[:120]}"
    models = {m.get("id") for m in response.json().get("data", [])}
    if model not in models:
        return False, f"The key works, but Groq has no model '{model}'. Try whisper-large-v3."
    return True, f"Groq works with model {model}."


NO_SPEECH = 0.6      # Whisper's own "this segment is silence/noise" probability


def _call(endpoint: str, audio: bytes, filename: str):
    """Whisper text, '' for silence / noise (Whisper then invents words like 'you'), None on error."""
    data = {"model": config.get("whisper_model"), "temperature": "0", "response_format": "verbose_json"}
    response = requests.post(
        f"{GROQ_URL}/{endpoint}",
        headers={"Authorization": f"Bearer {config.get('groq_api_key')}"},
        files={"file": (filename, audio)},
        data=data,
        timeout=TIMEOUT,
    )
    if response.status_code != 200:
        print(f"[Voice] Groq {endpoint} failed: {response.status_code} - {response.text[:200]}")
        return None
    body = response.json()
    segments = body.get("segments") or []
    speech = [s.get("text", "") for s in segments if (s.get("no_speech_prob") or 0) < NO_SPEECH]
    if segments and not speech:
        print(f"[Voice] No speech detected (Whisper guessed {body.get('text', '')[:40]!r})")
        return ""
    return (" ".join(speech) if segments else body.get("text") or "").strip()


def _filename(mimetype: str) -> str:
    """Groq picks the decoder from the file extension."""
    mimetype = (mimetype or "").lower()
    for key, ext in (("ogg", "ogg"), ("opus", "ogg"), ("mpeg", "mp3"), ("mp3", "mp3"), ("mp4", "m4a"),
                     ("m4a", "m4a"), ("aac", "m4a"), ("wav", "wav"), ("webm", "webm"), ("flac", "flac")):
        if key in mimetype:
            return f"voice.{ext}"
    return "voice.ogg"                   # WhatsApp voice notes


def transcribe(audio: bytes, mimetype: str = "audio/ogg") -> str | None:
    """Voice note -> text AURA can read (Roman Urdu or English). None if it couldn't be done."""
    if not configured():
        print("[Voice] No Groq API key (Platform settings) - voice notes can't be read")
        return None
    if not audio or len(audio) > MAX_BYTES:
        print(f"[Voice] Audio empty or too large ({len(audio or b'')} bytes)")
        return None
    name = _filename(mimetype)
    try:
        text = _call("transcriptions", audio, name)
        if text and URDU_SCRIPT.search(text):
            from app.services.nlu import to_roman_urdu
            print(f"[Voice] Heard: {text!r}")
            roman = to_roman_urdu(text)
            if roman and not URDU_SCRIPT.search(roman):
                text = roman
            else:
                print("[Voice] Roman Urdu rewrite unavailable -> English translation instead")
                text = _call("translations", audio, name)
    except requests.RequestException as e:
        print(f"[Voice] Groq unreachable: {e}")
        return None
    if not text or URDU_SCRIPT.search(text):
        return None                      # silence, noise, or nothing readable
    print(f"[Voice] -> {text!r}")
    return text
