"""Pretend to be WhatsApp: send AURA's server the same signed webhook WAHA sends for a customer message.

Tests the running server end to end (webhook -> voice note -> understanding -> booking -> dashboard)
without a phone. AURA's reply is printed in the server log and saved under Conversations; WhatsApp
delivers it only if the number is a real WhatsApp number.

Run where AURA's database is (Docker: docker compose exec aura python tests/send_test_webhook.py ...):
  python tests/send_test_webhook.py "kal 3 baje haircut book kar do"
  python tests/send_test_webhook.py --voice path/to/voice.ogg
  python tests/send_test_webhook.py "salam" --phone 923001234567 --business "Glow Salon"
"""
import argparse
import base64
import hashlib
import hmac
import json
import mimetypes
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services import config, db  # noqa: E402

parser = argparse.ArgumentParser(description="Send AURA a fake WAHA webhook (a customer message).")
parser.add_argument("text", nargs="?", default="", help="the customer's message")
parser.add_argument("--voice", help="audio file to send as a WhatsApp voice note (.ogg, .mp3, .m4a, .wav)")
parser.add_argument("--phone", default="923000000001", help="customer number, e.g. 923001234567 (default: a test number)")
parser.add_argument("--business", help="business name (default: the first business with WhatsApp linked)")
parser.add_argument("--url", default="http://localhost:8000/webhook", help="AURA's webhook URL")
args = parser.parse_args()

if not args.text and not args.voice:
    parser.error("give a message text or --voice FILE")

linked = [b for b in db.list_businesses(active_only=True) if b.get("whatsapp_session")]
if args.business:
    linked = [b for b in linked if b["name"].lower() == args.business.lower()]
if not linked:
    sys.exit("No active business with WhatsApp linked" + (f" named {args.business!r}" if args.business else "")
             + ". Connect one in the dashboard: Settings -> WhatsApp.")
business = linked[0]

message = {"id": f"false_{args.phone}@c.us_TEST{int(time.time() * 1000)}", "timestamp": int(time.time()),
           "from": f"{args.phone}@c.us", "fromMe": False, "body": args.text, "hasMedia": False}
if args.voice:
    audio = Path(args.voice)
    if not audio.is_file():
        sys.exit(f"No such file: {audio}")
    message.update(body="", hasMedia=True,
                   media={"url": None, "mimetype": mimetypes.guess_type(audio.name)[0] or "audio/ogg",
                          "filename": audio.name, "data": base64.b64encode(audio.read_bytes()).decode()})

raw = json.dumps({"event": "message", "session": business["whatsapp_session"], "payload": message}).encode()
signature = hmac.new(config.webhook_secret().encode(), raw, hashlib.sha512).hexdigest()
try:
    response = requests.post(args.url, data=raw, timeout=30, headers={
        "Content-Type": "application/json", "X-Webhook-Hmac": signature, "X-Webhook-Hmac-Algorithm": "sha512"})
except requests.RequestException as e:
    sys.exit(f"Could not reach {args.url}: {e}\nIs the server running?  uvicorn main:app --port 8000")
print(f"{business['name']}: {response.status_code} {response.text}")
if response.ok and '"received"' in response.text:
    print("Sent. AURA's reply appears in the server log and in the dashboard -> Conversations.")
