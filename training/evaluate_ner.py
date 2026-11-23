"""Evaluate the downloaded entity model on the NER test sets.

Run:  python training/evaluate_ner.py models/entity_model

Entity-level scores, same rule as seqeval in the Colab notebook: an entity counts as correct only if
its type AND its exact words match. Shown overall and per type (DATE, TIME, SERVICE, NAME).
"""
import json
import sys
from collections import Counter
from pathlib import Path

import torch
from transformers import AutoModelForTokenClassification, AutoTokenizer

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "training" / "data_ner"
MAX_LEN = 96


def spans(tags):
    """BIO tags -> set of (type, start, end). A stray I- tag starts a new entity (seqeval default)."""
    found, kind, start = set(), None, None
    for i, tag in enumerate(list(tags) + ["O"]):
        prefix, _, label = tag.partition("-")
        if kind and (prefix != "I" or label != kind):
            found.add((kind, start, i))
            kind = None
        if prefix == "B" or (prefix == "I" and kind is None):
            kind, start = label, i
    return found


def score(true_rows, pred_rows):
    tp, fp, fn = Counter(), Counter(), Counter()
    for t, p in zip(true_rows, pred_rows):
        ts, ps = spans(t), spans(p)
        for k, *_ in ts & ps:
            tp[k] += 1
        for k, *_ in ps - ts:
            fp[k] += 1
        for k, *_ in ts - ps:
            fn[k] += 1

    def prf(a, b, c):
        precision = a / (a + b) if a + b else 0.0
        recall = a / (a + c) if a + c else 0.0
        return precision, recall, (2 * precision * recall / (precision + recall) if precision + recall else 0.0)

    per_type = {k: prf(tp[k], fp[k], fn[k]) + (tp[k] + fn[k],) for k in sorted(set(tp) | set(fn) | set(fp))}
    return prf(sum(tp.values()), sum(fp.values()), sum(fn.values())), per_type


def main(model_dir: str):
    tok = AutoTokenizer.from_pretrained(model_dir)
    model = AutoModelForTokenClassification.from_pretrained(model_dir).eval()
    id2label = model.config.id2label

    def predict(tokens):
        enc = tok(tokens, is_split_into_words=True, return_tensors="pt", truncation=True, max_length=MAX_LEN)
        with torch.no_grad():
            ids = model(**enc).logits.argmax(-1)[0].tolist()
        tags, seen = ["O"] * len(tokens), set()
        for pos, w in enumerate(enc.word_ids()):
            if w is not None and w not in seen:
                seen.add(w)
                tags[w] = id2label[ids[pos]]
        return tags

    print(f"Model: {model_dir}\n")
    for name in ["test", "test_noisy"]:
        rows = json.loads((DATA / f"{name}.json").read_text(encoding="utf-8"))
        preds = [predict(r["tokens"]) for r in rows]
        (p, r, f), per_type = score([r["ner_tags"] for r in rows], preds)
        print(f"{name:11} {len(rows):4} messages   F1 {f:.3f}   precision {p:.3f}   recall {r:.3f}")
        for kind, (tp_, tr_, tf_, support) in per_type.items():
            print(f"    {kind:8} F1 {tf_:.3f}  (precision {tp_:.3f}, recall {tr_:.3f}, {support} entities)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "models" / "entity_model"))
