"""Build the entity (NER) dataset: DATE, TIME, SERVICE, NAME in BIO format.

Run:  python training/build_ner_dataset.py
Writes training/data_ner/:
  train.json       original train + 1 re-spelled copy of each message (same tags)
  val.json         original val (unchanged)
  test.json        original test (unchanged)
  test_noisy.json  the test messages re-spelled the way people type on WhatsApp

Re-spelling works word by word ("kal" -> "kl", "baje" -> "bje"), so every token keeps its tag.
Source: Data/processed/{train,val,test}.json (tokens + ner_tags), same split as the intent model.
"""
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "training"))
from build_dataset_v2 import VARIANTS  # noqa: E402  (same spelling table as the intent model)

SRC = ROOT / "Data" / "processed"
OUT = ROOT / "training" / "data_ner"
LABELS = ["O", "B-DATE", "I-DATE", "B-TIME", "I-TIME", "B-SERVICE", "I-SERVICE", "B-NAME", "I-NAME"]


def load(name):
    rows = json.loads((SRC / f"{name}.json").read_text(encoding="utf-8"))
    out = []
    for r in rows:
        assert len(r["tokens"]) == len(r["ner_tags"]), r["text"]
        assert all(t in LABELS for t in r["ner_tags"]), r["ner_tags"]
        out.append({"tokens": r["tokens"], "ner_tags": r["ner_tags"]})
    return out


def respell(row, rng, p=0.6):
    """Same tokens and tags, with known words re-spelled; None if nothing changed."""
    tokens = []
    for tok in row["tokens"]:
        low = tok.lower()
        if low in VARIANTS and rng.random() < p:
            new = rng.choice(VARIANTS[low])
            if " " in new:                  # multi-word variants would break the 1 token = 1 tag rule
                new = tok
            tok = new.capitalize() if tok[:1].isupper() else new
        tokens.append(tok)
    if rng.random() < 0.4:
        tokens = [t.lower() for t in tokens]
    if [t.lower() for t in tokens] == [t.lower() for t in row["tokens"]]:
        return None
    return {"tokens": tokens, "ner_tags": list(row["ner_tags"])}


def main():
    train, val, test = load("train"), load("val"), load("test")
    rng = random.Random(42)
    augmented = [v for r in train if (v := respell(r, rng))]
    held_out = {" ".join(r["tokens"]).lower() for r in val + test}
    train_all = [r for r in train + augmented if " ".join(r["tokens"]).lower() not in held_out]
    rng.shuffle(train_all)

    noisy_rng = random.Random(7)
    test_noisy = [v for r in test if (v := respell(r, noisy_rng))]

    OUT.mkdir(parents=True, exist_ok=True)
    for name, rows in [("train", train_all), ("val", val), ("test", test), ("test_noisy", test_noisy)]:
        (OUT / f"{name}.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
        entities = sum(t.startswith("B-") for r in rows for t in r["ner_tags"])
        print(f"{name:11} {len(rows):5} messages  {entities:5} entities")
    (OUT / "labels.json").write_text(json.dumps(LABELS), encoding="utf-8")


if __name__ == "__main__":
    main()
