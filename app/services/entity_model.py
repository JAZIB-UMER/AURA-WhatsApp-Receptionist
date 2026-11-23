"""Fine-tuned XLM-RoBERTa entity tagger (trained in training/AURA_Entity_Training.ipynb).

extract("kal 3 baje haircut book kar do, mera naam Ali hai")
  -> {"date": "kal", "time": "3 baje", "service": "haircut", "name": "Ali"}

Each value is the customer's own words. The date/time parser turns DATE/TIME text into real dates;
this model tells AURA *that* a date or time was mentioned, so Gemini is only asked when the parser
can't read it. Test F1 0.946 (DATE 0.99, NAME 0.97, SERVICE 0.92, TIME 0.91).

NAME in the training data also covers staff ("Dr Ayesha", "Sir Usman k sath"), so customer_name()
only accepts a name with a self-reference cue ("mera naam Ali", "Sana here", "for Bilal").
If the model folder or libraries are missing, extract() returns None and AURA uses Gemini as before.
"""
import os
import re
import threading
from typing import Any
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parents[2]
# .env: ENTITY_MODEL_DIR=models/entity_model   (relative paths are inside the project)
MODEL_DIR = Path(os.getenv("ENTITY_MODEL_DIR") or "models/entity_model")
if not MODEL_DIR.is_absolute():
    MODEL_DIR = PROJECT_ROOT / MODEL_DIR
MAX_LEN = 96

TITLES = {"dr", "doctor", "sir", "miss", "madam", "mam", "maam", "ma'am", "sahab", "sahib", "baji", "ustad"}
# words around a name that mean "this is me / the booking is for this person"
CUE_BEFORE = re.compile(r"(\b(naam|nam|name)\s*(is|hai|he|hy|:)?|\bi\s*am|\bi'm|\bthis\s+is|\bmain|\bmein|\bfor|-)\s*$", re.I)
CUE_AFTER = re.compile(r"^\s*(here|speaking|hoon|hun|hu|k\s+name|ke\s+naam|ke\s+name|k\s+naam|ka\s+naam|k\s+liye|ke\s+liye|ki\s+taraf)\b"
                       r"|^\s*[.!]*\s*$", re.I)

_lock = threading.Lock()
_model: Any = None
_tokenizer: Any = None
_load_failed = False


def load() -> bool:
    """Load the model into memory. Safe to call many times."""
    global _model, _tokenizer, _load_failed
    if _model is not None:
        return True
    if _load_failed:
        return False
    with _lock:
        if _model is not None:
            return True
        try:
            import torch
            from transformers import AutoModelForTokenClassification, AutoTokenizer

            if not (MODEL_DIR / "config.json").exists():
                raise FileNotFoundError(f"No model at {MODEL_DIR}")
            torch.set_grad_enabled(False)
            _tokenizer = AutoTokenizer.from_pretrained(MODEL_DIR)
            from app.services.intent_model import model_dtype      # same precision setting for both models
            _model = AutoModelForTokenClassification.from_pretrained(MODEL_DIR, dtype=model_dtype()).eval()
            print(f"[EntityModel] Loaded from {MODEL_DIR} ({str(model_dtype()).replace('torch.', '')})")
            return True
        except Exception as e:
            _load_failed = True
            print(f"[EntityModel] Not available ({e}). Falling back to Gemini for service / name.")
            return False


def words(text: str) -> list:
    """Split like the training data: punctuation is its own token ("2:30pm," -> 2 : 30pm ,)."""
    return re.findall(r"[\w']+|[^\w\s]", text)


def tag(text: str):
    """[(word, BIO tag), ...] or None if the model is unavailable."""
    tokens = words(text)
    if not tokens or not load():
        return None
    enc = _tokenizer(tokens, is_split_into_words=True, return_tensors="pt", truncation=True, max_length=MAX_LEN)
    ids = _model(**enc).logits.float().argmax(-1)[0].tolist()
    tags, seen = ["O"] * len(tokens), set()
    for pos, w in enumerate(enc.word_ids()):
        if w is not None and w not in seen:
            seen.add(w)
            tags[w] = _model.config.id2label[ids[pos]]
    return list(zip(tokens, tags))


def spans(tagged: list) -> list:
    """[(type, start, end)] from BIO tags; a stray I- starts a new entity."""
    found, kind, start = [], None, 0
    for i, (_, t) in enumerate(tagged + [("", "O")]):
        prefix, _, label = t.partition("-")
        if kind and (prefix != "I" or label != kind):
            found.append((kind, start, i))
            kind = None
        if prefix == "B" or (prefix == "I" and kind is None):
            kind, start = label, i
    return found


def _name_from(tagged, start, end, need_cue: bool):
    tokens = [w for w, _ in tagged[start:end] if w != "."]
    if tokens and tokens[0].lower().rstrip(".") in TITLES:
        return None                               # "Dr Ayesha", "Sir Usman": staff, not the customer
    if not tokens or not all(re.fullmatch(r"[A-Za-z']+", w) for w in tokens):
        return None
    if need_cue:
        before = " ".join(w for w, _ in tagged[max(0, start - 3):start])
        after = " ".join(w for w, _ in tagged[end:end + 2])
        if not (CUE_BEFORE.search(before) or CUE_AFTER.search(after)):
            return None                           # "Hamza k pass appointment", "Sara ka checkup": unclear who
    return " ".join(tokens).title()


def extract(text: str, name_needs_cue: bool = True):
    """{"date","time","service","name"} as the customer's own words (None if not mentioned).
    Returns None if the model is unavailable. name_needs_cue=False when AURA just asked for the name."""
    tagged = tag(text)
    if tagged is None:
        return None
    out = {"date": None, "time": None, "service": None, "name": None}
    found = spans(tagged)
    if any(kind in ("DATE", "TIME") for kind, *_ in found):
        name_needs_cue = True          # "agle hafte jumay ko" answers a date, it doesn't name "Jumay"
    for kind, start, end in found:
        key = kind.lower()
        if key == "name":
            out["name"] = out["name"] or _name_from(tagged, start, end, name_needs_cue)
        elif not out.get(key):
            out[key] = " ".join(w for w, _ in tagged[start:end])
    return out
