"""Pretend to be WhatsApp: send AURA's server the same webhook WaAPI sends for a customer message.

Tests the running server end to end (webhook -> voice note -> understanding -> booking -> dashboard)
without a phone. AURA's reply is printed in the server terminal and saved under Conversations; it is
only sent to WhatsApp if WaAPI allows that number (trial accounts: only the trial test number).

Run (server must be running: uvicorn main:app --port 8000):
  python tests/send_test_webhook.py "kal 3 baje haircut book kar do"
  python tests/send_test_webhook.py --voice path/to/voice.ogg
  python tests/send_test_webhook.py "salam" --phone 923001234567 --instance 108128
  python tests/send_test_webhook.py "salam" --url https://xxxx.trycloudflare.com/webhook   (through the tunnel)
"""
import argparse
import base64
import mimetypes
import sys
import time
from pathlib import Path

import requests

parser = argparse.ArgumentParser(description="Send AURA a fake WaAPI webhook (a customer message).")
parser.add_argument("text", nargs="?", default="", help="the customer's message")
parser.add_argument("--voice", help="audio file to send as a WhatsApp voice note (.ogg, .mp3, .m4a, .wav)")
parser.add_argument("--phone", default="923000000001", help="customer number, e.g. 923001234567 (default: a test number)")
parser.add_argument("--instance", default="108128", help="WaAPI instance ID of the business (default: 108128)")
parser.add_argument("--url", default="http://localhost:8000/webhook", help="AURA's webhook URL")
args = parser.parse_args()

if not args.text and not args.voice:
    parser.error("give a message text or --voice FILE")

message = {"from": f"{args.phone}@c.us", "fromMe": False,
           "id": {"_serialized": f"false_{args.phone}@c.us_TEST{int(time.time() * 1000)}"}}
data = {"message": message}
if args.voice:
    audio = Path(args.voice)
    if not audio.is_file():
        sys.exit(f"No such file: {audio}")
    message.update(type="ptt", body="")
    data["media"] = {"mimetype": mimetypes.guess_type(audio.name)[0] or "audio/ogg",
                     "data": base64.b64encode(audio.read_bytes()).decode(), "filename": audio.name}
else:
    message.update(type="chat", body=args.text)

try:
    response = requests.post(args.url, json={"event": "message", "instanceId": args.instance, "data": data},
                             timeout=30)
except requests.RequestException as e:
    sys.exit(f"Could not reach {args.url}: {e}\nIs the server running?  uvicorn main:app --port 8000")
print(f"{response.status_code} {response.text}")
if response.ok and '"received"' in response.text:
    print("Sent. AURA's reply appears in the server terminal and in the dashboard -> Conversations.")
