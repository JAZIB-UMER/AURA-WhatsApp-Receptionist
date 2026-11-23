import json
import os
from pathlib import Path
from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel
from typing import Literal

# Load environment variables from project root .env
root_dir = Path(__file__).resolve().parent.parent
load_dotenv(root_dir / ".env")

api_key = os.environ.get("GEMINI_API_KEY")
if not api_key:
    raise ValueError("GEMINI_API_KEY not found in environment or .env file.")

client = genai.Client(api_key=api_key)

VALID_ENTITIES = Literal["date", "time", "service", "name"]

class Entity(BaseModel):
    entity: VALID_ENTITIES
    value: str  # Must be an exact substring from text

class Sample(BaseModel):
    text: str
    intent: Literal[
        "check_availability",
        "book_appointment",
        "cancel_booking",
        "price_query",
        "greeting",
        "reschedule"
    ]
    entities: list[Entity]

class Batch(BaseModel):
    samples: list[Sample]

DOMAINS = "clinics, diagnostic centers, salons, bike/car repair workshops, and home tutors in Pakistan"

INTENT_CONFIGS = [
    {
        "intent": "check_availability",
        "instructions": (
            "User is strictly asking whether a slot, doctor, technician, or tutor is open/free. "
            "They are NOT confirming or booking yet. Examples: 'kal 4 bje CD 70 ka engine oil change krana tha slot khali h?', "
            "'dr sahab kis time available hotay hain?', 'kl sham space hogi?'"
        )
    },
    {
        "intent": "book_appointment",
        "instructions": (
            "User is actively requesting to confirm, reserve, or finalize an appointment. "
            "Examples: 'kal 5 bje haircut book kr dain', 'sunday sham 6 bje slot confirm kr dein', 'mera slot rakh lein'."
        )
    },
    {
        "intent": "cancel_booking",
        "instructions": (
            "User wants to cancel an existing appointment or notifies they cannot attend. "
            "Examples: 'kl ki booking cancel kr dein', 'mujhy kal nhi ana slot cancel kr do', 'appointment khatam kr dein'."
        )
    },
    {
        "intent": "price_query",
        "instructions": (
            "User is inquiring about rates, fees, or charges. "
            "Examples: 'haircut aur beard k kitny charges hain?', 'dr ki checkup fee kia h?', 'bike tuning kitny ki hogi?'"
        )
    },
    {
        "intent": "greeting",
        "instructions": (
            "Casual greetings or polite salutations with no booking actions. "
            "Examples: 'salam dr sb', 'aoa sir kaisy hain', 'hello bhai', 'hi'. Entities must be empty []."
        )
    },
    {
        "intent": "reschedule",
        "instructions": (
            "User wants to change an existing appointment to a different date or time. "
            "Examples: 'kal k bajaye parson kr dein', 'appointment aagy shift krna thi 5 bje'."
        )
    }
]

output_dir = Path(__file__).resolve().parent / "raw"
output_dir.mkdir(exist_ok=True, parents=True)

BATCHES_PER_INTENT = 6
SAMPLES_PER_BATCH = 20

def run_generation():
    print(f"[+] Generating raw dataset to: {output_dir}")
    
    for config in INTENT_CONFIGS:
        intent_name = config["intent"]
        file_path = output_dir / f"{intent_name}.jsonl"
        print(f"\n[>] Generating: {intent_name}")

        prompt = f"""
        You are generating training data exclusively for the intent '{intent_name}' for {DOMAINS}.
        Intent description: {config['instructions']}

        Language: Roman Urdu, Pakistani English, and informal WhatsApp code-switching.
        Typing style: Realistic WhatsApp chat (e.g., 'kl', 'bje', 'dr sb', 'tuning', 'CD 70', 'slot', 'bhai', 'space h?').

        Key Constraints:
        1. Every single message must strictly match '{intent_name}'.
        2. Entity 'value' MUST be an exact character-for-character substring present in 'text'.
        3. Extract only: 'date', 'time', 'service', 'name'. If none present, use [].
        4. Distribute realistic examples across clinics, diagnostic centers, salons, repair workshops, and tutors.

        Generate {SAMPLES_PER_BATCH} distinct WhatsApp messages now.
        """

        for batch_idx in range(1, BATCHES_PER_INTENT + 1):
            try:
                response = client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=Batch,
                        temperature=0.85,
                    ),
                )
                data = json.loads(response.text)
                with open(file_path, "a", encoding="utf-8") as f:
                    for item in data["samples"]:
                        f.write(json.dumps(item, ensure_ascii=False) + "\n")
                print(f"    Batch {batch_idx}/{BATCHES_PER_INTENT} saved ({len(data['samples'])} samples)")
            except Exception as e:
                print(f"    [!] Error on batch {batch_idx}: {e}")

if __name__ == "__main__":
    run_generation()