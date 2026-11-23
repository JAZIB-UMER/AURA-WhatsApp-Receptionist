"""Fine-tuned XLM-RoBERTa intent classifier (trained in training/AURA_Intent_Training.ipynb).

predict("kal 3 baje haircut book kar do") -> ("book_appointment", 0.998)

The model loads once (about 5-10 s) and then answers in ~100 ms on CPU.
If the model folder or libraries are missing, predict() returns (None, 0.0)
and AURA falls back to Gemini, so the bot keeps working.
"""
import os
import threading
from typing import Any
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parents[2]
# .env: INTENT_MODEL_DIR=models/intent_model_v2   (relative paths are inside the project)
MODEL_DIR = Path(os.getenv("INTENT_MODEL_DIR") or "models/intent_model")
if not MODEL_DIR.is_absolute():
    MODEL_DIR = PROJECT_ROOT / MODEL_DIR
MAX_LEN = 96


def model_dtype():
    """MODEL_DTYPE=float16 (default) or float32. Measured on all test sets: float16 gives the same
    predictions as float32 (0 of 561 differ, no confidence-threshold crossings) from model files half
    the size, ~20 ms slower per message on CPU. float32 is the fallback for unusual CPUs."""
    import torch
    name = (os.getenv("MODEL_DTYPE") or "float16").strip().lower()
    return {"float16": torch.float16, "fp16": torch.float16, "float32": torch.float32,
            "fp32": torch.float32}.get(name, torch.float16)

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
            from transformers import AutoModelForSequenceClassification, AutoTokenizer

            if not (MODEL_DIR / "config.json").exists():
                raise FileNotFoundError(f"No model at {MODEL_DIR}")
            torch.set_grad_enabled(False)
            _tokenizer = AutoTokenizer.from_pretrained(MODEL_DIR)
            _model = AutoModelForSequenceClassification.from_pretrained(MODEL_DIR, dtype=model_dtype()).eval()
            print(f"[IntentModel] Loaded from {MODEL_DIR} ({str(model_dtype()).replace('torch.', '')})")
            return True
        except Exception as e:
            _load_failed = True
            print(f"[IntentModel] Not available ({e}). Falling back to Gemini for intents.")
            return False


def predict(text: str):
    """Return (intent, probability). (None, 0.0) if the model is unavailable."""
    if not text or not load():
        return None, 0.0
    import torch

    inputs = _tokenizer(text, return_tensors="pt", truncation=True, max_length=MAX_LEN)
    probs = torch.softmax(_model(**inputs).logits.float(), dim=-1)[0]   # probabilities in float32
    idx = int(probs.argmax())
    return _model.config.id2label[idx], round(float(probs[idx]), 4)
