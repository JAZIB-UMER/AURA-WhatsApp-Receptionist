"""Evaluate an intent model folder on the v2 test sets.

Run:  python training/evaluate_model.py models/intent_model
      python training/evaluate_model.py models/intent_model_v2

Prints accuracy / macro-F1 on:
  test        the original 136 test messages (same as the Colab v1 result)
  test_noisy  the same messages spelled the way people type on WhatsApp
  test_other  out-of-scope messages (thanks, names, "ok", off-topic) — a model without an
              "other" class can never get these right; the column shows what it predicted instead
  test_short  short requests ("Booking karni hy", "rates?", "AOA") held out from training
"""
import json
import sys
from collections import Counter
from pathlib import Path

import torch
from transformers import AutoModelForSequenceClassification, AutoTokenizer

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "training" / "data_v2"


def accuracy_score(y_true, y_pred):
    return sum(t == p for t, p in zip(y_true, y_pred)) / len(y_true)


def f1_score(y_true, y_pred, average="macro", zero_division=0):
    """Macro-F1 over the classes present in y_true (no scikit-learn needed)."""
    scores = []
    for c in sorted(set(y_true)):
        tp = sum(t == c and p == c for t, p in zip(y_true, y_pred))
        fp = sum(t != c and p == c for t, p in zip(y_true, y_pred))
        fn = sum(t == c and p != c for t, p in zip(y_true, y_pred))
        prec = tp / (tp + fp) if tp + fp else zero_division
        rec = tp / (tp + fn) if tp + fn else zero_division
        scores.append(2 * prec * rec / (prec + rec) if prec + rec else 0.0)
    return sum(scores) / len(scores)


def main(model_dir: str):
    tok = AutoTokenizer.from_pretrained(model_dir)
    model = AutoModelForSequenceClassification.from_pretrained(model_dir).eval()
    labels = model.config.id2label

    def predict(texts):
        out = []
        with torch.no_grad():
            for i in range(0, len(texts), 32):
                enc = tok(texts[i:i + 32], return_tensors="pt", padding=True, truncation=True, max_length=96)
                probs = torch.softmax(model(**enc).logits, -1)
                conf, idx = probs.max(-1)
                out += [(labels[int(j)], float(c)) for j, c in zip(idx, conf)]
        return out

    print(f"Model: {model_dir}   classes: {sorted(labels.values())}\n")
    print(f"{'test set':12} {'n':>4} {'accuracy':>9} {'macro-F1':>9}  notes")
    for name in ["test", "test_noisy", "test_other", "test_short"]:
        rows = json.loads((DATA / f"{name}.json").read_text(encoding="utf-8"))
        preds = predict([r["text"] for r in rows])
        y_true = [r["intent"] for r in rows]
        y_pred = [p for p, _ in preds]
        acc = accuracy_score(y_true, y_pred)
        f1 = f1_score(y_true, y_pred, average="macro", zero_division=0) if name != "test_other" else float("nan")
        note = ""
        if name == "test_other":
            confident = sum(1 for p, c in preds if p != "other" and c >= 0.9)
            note = (f"predicted: {dict(Counter(y_pred).most_common(4))}; "
                    f"wrong with >=90% confidence: {confident}/{len(rows)}")
        print(f"{name:12} {len(rows):>4} {acc:>9.3f} {f1:>9.3f}  {note}")
        if name in ("test_noisy", "test_short"):
            wrong = [(r["text"], r["intent"], p, round(c, 2)) for r, (p, c) in zip(rows, preds) if p != r["intent"]]
            for text, true, pred, conf in wrong[:8]:
                print(f"    {true} -> {pred} @ {conf}: {text}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "models" / "intent_model"))
