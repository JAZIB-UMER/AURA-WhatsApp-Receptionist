import glob
import json
import os
import random
import re
from collections import defaultdict
from pathlib import Path
from typing import Literal
from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

# Load environment variables
root_dir = Path(__file__).resolve().parent.parent
load_dotenv(root_dir / ".env")

# ==============================================================================
# AGENT 1: INGESTION & SCHEMA NORMALIZER
# ==============================================================================
class IngestionAgent:
    def __init__(self, raw_output_path):
        self.raw_output_path = raw_output_path
        self.seen_texts = set()
        self.records = []

    def ingest_jsonl_files(self, search_dir):
        for filepath in glob.glob(os.path.join(search_dir, "*.jsonl")):
            if os.path.abspath(filepath) == os.path.abspath(self.raw_output_path):
                continue
            with open(filepath, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        text = data.get("text", "").strip()
                        intent = data.get("intent", "").strip()
                        entities = data.get("entities", [])
                        
                        if text and intent and text.lower() not in self.seen_texts:
                            self.seen_texts.add(text.lower())
                            self.records.append({
                                "text": text,
                                "intent": intent,
                                "entities": entities
                            })
                    except json.JSONDecodeError:
                        continue

    def run(self, raw_dir):
        self.ingest_jsonl_files(raw_dir)
        with open(self.raw_output_path, "w", encoding="utf-8") as f:
            for rec in self.records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"[Agent 1] Successfully pooled {len(self.records)} raw unique records.")
        return self.raw_output_path


# ==============================================================================
# AGENT 2: VERIFICATION & QUALITY AUDIT LLM
# ==============================================================================
VALID_INTENTS = Literal[
    "book_appointment",
    "cancel_booking",
    "check_availability",
    "price_query",
    "greeting",
    "reschedule"
]

VALID_ENTITIES = Literal["date", "time", "service", "name"]

class AuditedEntity(BaseModel):
    entity: VALID_ENTITIES
    value: str = Field(description="Must be an exact verbatim substring from text.")

class AuditedSample(BaseModel):
    text: str
    intent: VALID_INTENTS
    entities: list[AuditedEntity]
    is_valid: bool

class AuditBatch(BaseModel):
    results: list[AuditedSample]

class VerificationAgent:
    def __init__(self, client: genai.Client):
        self.client = client

    def audit_batch(self, batch):
        prompt = f"""
        You are the Quality Control LLM for an NLP task in Roman Urdu and Pakistani English.
        Audit this batch of WhatsApp messages.
        
        Tasks:
        1. Classify the message strictly into one of the 6 intents:
           - book_appointment, cancel_booking, check_availability, price_query, greeting, reschedule
        2. Extract valid entities: 'date', 'time', 'service', 'name'.
        3. CRITICAL: 'value' MUST be an exact verbatim substring inside 'text'. Never fix spelling in 'value'.
        4. Set is_valid=False if the message is gibberish, spam, or unintelligible.

        Input Batch:
        {json.dumps(batch, ensure_ascii=False)}
        """
        response = self.client.models.generate_content(
            model="gemini-3.8-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=AuditBatch,
                temperature=0.0
            )
        )
        return json.loads(response.text)["results"]

    def run(self, input_file, output_file, batch_size=12):
        with open(input_file, "r", encoding="utf-8") as f:
            records = [json.loads(line) for line in f if line.strip()]

        verified_records = []
        for i in range(0, len(records), batch_size):
            chunk = records[i:i + batch_size]
            try:
                audited = self.audit_batch(chunk)
                for item in audited:
                    if not item["is_valid"]:
                        continue
                    
                    # Strictly enforce substring verification in Python
                    clean_entities = [
                        ent for ent in item["entities"] if ent["value"] in item["text"]
                    ]
                    
                    verified_records.append({
                        "text": item["text"],
                        "intent": item["intent"],
                        "entities": clean_entities
                    })
            except Exception as e:
                print(f"[!] Error auditing batch at index {i}: {e}")

        with open(output_file, "w", encoding="utf-8") as f:
            for rec in verified_records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

        print(f"[Agent 2] Verification complete: {len(verified_records)} clean samples retained.")
        return output_file


# ==============================================================================
# AGENT 3: BIO SEQUENCE TAGGING ENGINE
# ==============================================================================
class BIOTaggingAgent:
    def __init__(self):
        self.entity_map = {
            "date": "DATE",
            "time": "TIME",
            "service": "SERVICE",
            "name": "NAME"
        }

    def tokenize_and_tag(self, text, entities):
        # Tokenize by alphanumeric blocks or distinct punctuation marks
        tokens = re.findall(r"\w+|[^\w\s]", text, re.UNICODE)
        tags = ["O"] * len(tokens)

        # Match longest entity values first to prevent nested substring conflicts
        sorted_entities = sorted(entities, key=lambda x: len(x["value"]), reverse=True)

        for ent in sorted_entities:
            ent_type = self.entity_map.get(ent["entity"].lower())
            if not ent_type:
                continue
            
            ent_tokens = re.findall(r"\w+|[^\w\s]", ent["value"], re.UNICODE)
            ent_len = len(ent_tokens)
            if ent_len == 0:
                continue

            for i in range(len(tokens) - ent_len + 1):
                slice_tokens = [t.lower() for t in tokens[i:i + ent_len]]
                target_tokens = [t.lower() for t in ent_tokens]

                if slice_tokens == target_tokens and all(tags[i + j] == "O" for j in range(ent_len)):
                    tags[i] = f"B-{ent_type}"
                    for j in range(1, ent_len):
                        tags[i + j] = f"I-{ent_type}"
                    break

        return tokens, tags

    def run(self, input_file, output_file):
        tagged_records = []
        with open(input_file, "r", encoding="utf-8") as f:
            for line in f:
                data = json.loads(line)
                tokens, tags = self.tokenize_and_tag(data["text"], data["entities"])
                tagged_records.append({
                    "text": data["text"],
                    "intent": data["intent"],
                    "tokens": tokens,
                    "ner_tags": tags
                })

        with open(output_file, "w", encoding="utf-8") as f:
            for rec in tagged_records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

        print(f"[Agent 3] Tagging complete: {len(tagged_records)} samples formatted with BIO labels.")
        return output_file


# ==============================================================================
# AGENT 4: STRATIFIED SPLIT ENGINE (70 / 15 / 15)
# ==============================================================================
class StratifiedSplitAgent:
    def __init__(self, train_ratio=0.70, val_ratio=0.15, test_ratio=0.15, seed=42):
        self.train_ratio = train_ratio
        self.val_ratio = val_ratio
        self.test_ratio = test_ratio
        self.seed = seed

    def run(self, input_file, output_dir):
        records_by_intent = defaultdict(list)
        with open(input_file, "r", encoding="utf-8") as f:
            for line in f:
                item = json.loads(line)
                records_by_intent[item["intent"]].append(item)

        random.seed(self.seed)
        train_data, val_data, test_data = [], [], []

        print("\n--- Intent Stratification Breakdown ---")
        for intent, items in records_by_intent.items():
            random.shuffle(items)
            total = len(items)
            n_train = int(total * self.train_ratio)
            n_val = int(total * self.val_ratio)

            train_split = items[:n_train]
            val_split = items[n_train:n_train + n_val]
            test_split = items[n_train + n_val:]

            train_data.extend(train_split)
            val_data.extend(val_split)
            test_data.extend(test_split)

            print(f"{intent:<20} Total: {total:<4} | Train: {len(train_split):<3} | Val: {len(val_split):<3} | Test: {len(test_split):<3}")

        splits = {
            os.path.join(output_dir, "train.json"): train_data,
            os.path.join(output_dir, "val.json"): val_data,
            os.path.join(output_dir, "test.json"): test_data
        }

        for filepath, dataset in splits.items():
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(dataset, f, ensure_ascii=False, indent=2)

        print(f"\n[Agent 4] Stratified export successful into {output_dir}:")
        print(f"  -> train.json: {len(train_data)} samples ({self.train_ratio * 100:.0f}%)")
        print(f"  -> val.json:   {len(val_data)} samples ({self.val_ratio * 100:.0f}%)")
        print(f"  -> test.json:  {len(test_data)} samples ({self.test_ratio * 100:.0f}%)")


# ==============================================================================
# PIPELINE EXECUTION
# ==============================================================================
if __name__ == "__main__":
    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

    data_dir = Path(__file__).resolve().parent
    raw_dir = Path("/home/pb/Desktop/AURA/dataset/raw")
    proc_dir = data_dir / "processed"
    proc_dir.mkdir(exist_ok=True, parents=True)

    raw_pool = proc_dir / "raw_pool.jsonl"
    bio_dataset = proc_dir / "bio_dataset.jsonl"

    raw_file = IngestionAgent(raw_output_path=str(raw_pool)).run(raw_dir=str(raw_dir))
    bio_file = BIOTaggingAgent().run(input_file=str(raw_file), output_file=str(bio_dataset))
    StratifiedSplitAgent().run(input_file=str(bio_file), output_dir=str(proc_dir))