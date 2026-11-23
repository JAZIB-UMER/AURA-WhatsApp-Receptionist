import json
import os
from google import genai
from google.genai import types
from pydantic import BaseModel
from typing import Literal

client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

class Entity(BaseModel):
    entity: Literal["date", "time", "service", "name"]
    value: str

class Sample(BaseModel):
    text: str
    intent: Literal["book_appointment"]
    entities: list[Entity]

class Batch(BaseModel):
    samples: list[Sample]

prompt = """
Generate 15 WhatsApp messages for intent 'book_appointment' for salons, clinics, repair shops and tutors in Pakistan.
User is actively booking or confirming a slot.
Language: Roman Urdu, Pakistani English, mixed.
Examples: 'kal 5 bje haircut book kr dain', 'sunday slot confirm kr dein', 'mera slot rakh lein'
Entity value must be exact substring from text.
Extract only: date, time, service, name. If none, use [].
"""

response = client.models.generate_content(
    model="gemini-3.8-flash",
    contents=prompt,
    config=types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=Batch,
        temperature=0.85,
    ),
)

data = json.loads(response.text)
with open("dataset/raw/book_appointment.jsonl", "a", encoding="utf-8") as f:
    for item in data["samples"]:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")
print(f"Added {len(data['samples'])} book_appointment samples.")
