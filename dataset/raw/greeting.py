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
    intent: Literal["greeting"]
    entities: list[Entity]

class Batch(BaseModel):
    samples: list[Sample]

prompt = """
Generate 15 WhatsApp greeting messages. ONLY greetings, no booking actions.
Language: Roman Urdu, Pakistani English, mixed.
Examples: 'aoa', 'salam bhai', 'hello', 'assalamualaikum', 'hi sir'
Entities MUST always be empty list [].
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
with open("dataset/raw/greeting.jsonl", "a", encoding="utf-8") as f:
    for item in data["samples"]:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")
print(f"Added {len(data['samples'])} greeting samples.")
