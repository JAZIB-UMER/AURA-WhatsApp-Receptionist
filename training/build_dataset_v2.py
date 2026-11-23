"""Build the v2 intent dataset: spelling-variant augmentation + an "other" (out-of-scope) class.

Run:  python training/build_dataset_v2.py
Writes training/data_v2/:
  train.json      original train + short requests + 2 spelling variants each + "other" examples
  val.json        original val + "other" val examples            (no augmentation)
  test.json       original test, untouched                       (comparable with the v1 result)
  test_noisy.json the test messages re-spelled the way people type (robustness test)
  test_other.json held-out "other" messages                      (out-of-scope test)
  test_short.json held-out short requests ("Booking karni hy")    (short-message test)

Why: the v1 model was trained only on the 6 booking intents, so short messages that are none
of them ("Shukriya", "ok", a name like "Jazib Umer") were forced into one — usually "greeting"
at 99% confidence — and spelling variants ("Booking karni hy") could flip it to the wrong intent.

The "other" examples are synthetic (written by the project team) and labelled as such in the report.
"""
import json
import random
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "Data" / "processed"
OUT = ROOT / "training" / "data_v2"

# ------------------------------------------------------------------ spelling variants
# How people actually type these words on WhatsApp (canonical -> variants).
VARIANTS = {
    "hai": ["hy", "he", "h", "ha"], "hain": ["hy", "hen", "hn"], "hy": ["hai", "he"],
    "karni": ["krni", "karna", "krna"], "karna": ["krna", "karni", "krni"], "krni": ["karni", "krna"],
    "kar": ["kr"], "kr": ["kar"], "kardo": ["krdo", "kar do", "kr do"], "kardein": ["krden", "kar dein", "kr dein"],
    "karwani": ["krwani", "karwana", "krwana"], "mujhe": ["mujhy", "mjhe", "mujhay", "mje", "mujy"],
    "mujhy": ["mujhe", "mjhe"], "chahiye": ["chahye", "chaiye", "chahie", "chahiay", "chaye"],
    "appointment": ["apointment", "appointmnt", "appt", "appoinment", "appointmet"],
    "booking": ["bkng", "boking", "bookng", "bukking"], "cancel": ["cancle", "cansel", "cncl", "cancell"],
    "kal": ["kl", "kall"], "baje": ["bje", "bajay", "bjy", "bjay"], "bje": ["baje", "bajay"],
    "sham": ["shaam", "shm"], "subah": ["subha", "suba", "sbh"], "aap": ["ap", "app", "apko"],
    "kya": ["kia", "kiya", "ky"], "kia": ["kya"], "please": ["plz", "pls", "plzz", "plx"],
    "theek": ["thik", "theak", "thk"], "nahi": ["nai", "nhi", "nahin"], "nhi": ["nahi", "nai"],
    "kitne": ["kitny", "ktne", "kitnay"], "kitna": ["ktna", "kitnaa"], "dein": ["den", "dain", "dijiye", "do"],
    "salam": ["slam", "salaam"], "assalam": ["asalam", "aslam"], "time": ["tym", "tm", "taim"],
    "reschedule": ["reschdule", "reshedule", "re schedule"], "available": ["availble", "avlbl", "avail"],
    "slot": ["slott", "slots"], "hoga": ["hga", "hogaa"], "kab": ["kb"], "aaj": ["aj", "ajj"],
    "parso": ["parson", "prso"], "service": ["servic", "srvc"], "charges": ["charjes", "chrgs"],
    "price": ["prise", "prce"], "fees": ["fee", "fess"], "shift": ["shft"], "bhai": ["bhae", "bhaii"],
}
WORD_RE = re.compile(r"[A-Za-z]+|[^A-Za-z]+")


def respell(text: str, rng: random.Random, p: float = 0.6) -> str:
    """Replace known words with common spellings; sometimes lowercase / drop punctuation."""
    out = []
    for tok in WORD_RE.findall(text):
        low = tok.lower()
        if low in VARIANTS and rng.random() < p:
            new = rng.choice(VARIANTS[low])
            tok = new.capitalize() if tok[:1].isupper() else new
        out.append(tok)
    text = "".join(out)
    if rng.random() < 0.4:
        text = text.lower()
    if rng.random() < 0.3:
        text = re.sub(r"[.,!?]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def variants(text: str, rng: random.Random, n: int) -> list:
    seen, result = {text.lower()}, []
    for _ in range(n * 4):
        v = respell(text, rng)
        if v.lower() not in seen:
            seen.add(v.lower())
            result.append(v)
        if len(result) == n:
            break
    return result


# ------------------------------------------------------------------ out-of-scope ("other")
# Synthetic, hand-written. Covers what the v1 model mislabelled in real chats.
OTHER = {
    "thanks / goodbye": [
        "Shukriya", "shukria", "Thank you", "thanks", "thnx", "thank u so much", "JazakAllah", "Jazak Allah khair",
        "bohat shukriya", "Allah Hafiz", "Khuda hafiz", "bye", "ok bye", "take care", "theek hai shukriya",
        "meherbani", "thanks a lot", "shukriya bhai", "ok thanks", "thank you sir", "chalo theek hai shukriya",
        "fi amanillah", "bohot meherbani", "thx", "thanks madam", "bas shukriya",
    ],
    "acknowledgements": [
        "ok", "okay", "okk", "theek hai", "thik hai", "acha", "achha", "accha theek hai", "hmm", "hmmm", "g",
        "ji", "ji theek", "done", "great", "nice", "perfect", "sahi", "alright", "cool", "👍", "👌", "🙏",
        "acha ji", "ok ji", "theek", "got it", "samajh gaya", "samajh gayi", "noted",
    ],
    "names / bare answers": [
        "Jazib Umer", "Jazib", "Hassan", "Ali Khan", "Sara", "Fatima Azeem", "Ruqqaya", "Maryam", "Ahmed Raza",
        "Usman", "Hira", "Bilal Ahmed", "Ayesha", "Zainab", "Hamza", "Sana Malik", "Mr Imran", "Dr Kashif",
        "Umer", "Noor", "Asad Ali", "Rabia", "Talha", "Mahnoor", "32", "25 saal", "0300-1234567",
        "03001234567", "house 12 street 4", "Abbottabad", "Islamabad", "same", "wahi", "pata nahi",
    ],
    "off-topic questions": [
        "aap kaun ho?", "tum robot ho?", "weather kaisa hai aaj", "mujhe job chahiye", "kya aap delivery karte hain?",
        "location kahan hai?", "address bhej dein", "parking hai?", "owner ka number de dein", "payment kaise karni hai?",
        "easypaisa chalta hai?", "card accept karte ho?", "WiFi password kya hai", "koi discount hai?",
        "complaint karni hai", "pichli dafa service achi nahi thi", "manager se baat karwao", "kya ye real insaan hai?",
        "mujhe sales ke liye contact karna hai", "aap ka instagram page hai?", "franchise milti hai?",
        "hiring kar rahe ho?", "cricket score kya hai", "biryani kahan achi milti hai", "joke sunao",
        "mera order kahan hai", "refund chahiye", "bill bhej dein", "invoice chahiye", "google map pin bhejo",
        "kya ladies staff hai?", "bachon ke liye bhi hai?", "kitne log kaam karte hain", "naya branch kab khulega",
        "aap ka email kya hai", "website ka link do", "phone pe baat ho sakti hai?", "voice call karo",
    ],
    "noise": [
        "?", "??", "...", "test", "testing", "hello?", "asdf", "kjhkjh", "na", "haan", "nahi", "no", "yes",
        "kya", "kyun", "acha?", "sun", "suno", "bolo", "ji?", "jee", "hm?", "lol", "😂", "😊",
    ],
}


# Short requests (1-5 words). The real data is mostly full sentences while "other" is mostly short,
# so without these the model learns "short message = other" ("Booking karni hy" -> other @ 0.99).
# Synthetic, written by the project team; 30% is held out as test_short.
SHORT = {
    "book_appointment": [
        "Booking karni hai", "booking chahiye", "appointment chahiye", "appointment leni hai", "appt chahiye",
        "book kar do", "slot book karo", "booking please", "I want to book", "book an appointment",
        "need an appointment", "appointment book karni hai", "mujhe booking karni", "booking krwani hai",
        "time lena hai", "appointment milegi?", "haircut book karo", "booking ho sakti hai?", "nayi booking",
        "aana hai kal", "doctor se milna hai", "facial karwana hai",
    ],
    "cancel_booking": [
        "cancel karni hai", "booking cancel", "cancel kar do", "appointment cancel karo", "cancel my booking",
        "cancel please", "nahi aa sakta, cancel", "meri booking cancel", "cancel it", "appointment cancel",
        "cancel krdo", "booking hata do", "nahi aana, cancel karo", "cancel booking please", "kal wali cancel",
        "cancel appt", "I want to cancel", "remove my booking", "booking cancel krni", "booking khatam kar do",
    ],
    "check_availability": [
        "slot available hai?", "kal free ho?", "koi slot khali?", "aaj jagah hai?", "available ho?",
        "kal time hai?", "is time free?", "slot hai?", "kab free ho?", "aaj available?", "any slots today?",
        "are you free tomorrow?", "kal ka slot?", "khali time hai?", "sham ko free ho?", "kab aa sakta hoon?",
        "abhi aa jaun?", "doctor available hain?", "kal open ho?", "free slot hai?",
    ],
    "greeting": [
        "Hi", "Hello", "AOA", "Salam", "Assalam o alaikum", "hey", "hi there", "Salaam bhai", "aoa sir",
        "hello ji", "Good morning", "salam ji", "hy", "helo", "Asalam o alaikum", "hii", "slam", "Wsalam",
        "hey there", "good evening",
    ],
    "price_query": [
        "rates?", "price?", "fees kitni hai?", "kitne ka hai?", "charges?", "rate list", "price list bhejo",
        "haircut kitne ka?", "fees?", "kitna lagega?", "rates batao", "cost kya hai?", "how much?",
        "price kya hai", "kitne paise?", "facial ka rate?", "charges kya hain", "kya rate hai",
        "massage price?", "rate kya hai",
    ],
    "reschedule": [
        "time change karna hai", "reschedule karna hai", "date change karo", "booking shift karo",
        "appointment aage kar do", "reschedule please", "time badal do", "kal ki jagah parso kar do",
        "shift kar do", "postpone karna hai", "change my time", "reschedule my booking", "din badal do",
        "appt shift", "time change please", "booking ka time badlo", "aage kar dein", "can I reschedule?",
        "date change please", "kisi aur din kar do",
    ],
}


def split_short(rng: random.Random):
    """70% train / 30% test_short inside every intent."""
    train, test = [], []
    for intent, items in SHORT.items():
        items = list(dict.fromkeys(items))
        rng.shuffle(items)
        n_test = round(len(items) * 0.3)
        test += [{"text": t, "intent": intent} for t in items[:n_test]]
        train += [{"text": t, "intent": intent} for t in items[n_test:]]
    return train, test


def split_other(rng: random.Random):
    """70 / 15 / 15 split inside every group, so all kinds appear in train, val and test."""
    train, val, test = [], [], []
    for group in OTHER.values():
        items = list(dict.fromkeys(group))
        rng.shuffle(items)
        n = len(items)
        n_val = max(1, round(n * 0.15))
        n_test = max(1, round(n * 0.15))
        test += items[:n_test]
        val += items[n_test:n_test + n_val]
        train += items[n_test + n_val:]
    return train, val, test


def main():
    rng = random.Random(42)
    load = lambda name: [{"text": r["text"], "intent": r["intent"]}
                         for r in json.loads((SRC / f"{name}.json").read_text(encoding="utf-8"))]
    train, val, test = load("train"), load("val"), load("test")

    other_train, other_val, other_test = split_other(rng)
    # Light augmentation for "other" too (lowercase / punctuation), 1 variant each.
    other_train_rows = [{"text": t, "intent": "other"} for t in other_train]
    other_train_rows += [{"text": v, "intent": "other"} for t in other_train for v in variants(t, rng, 1)]

    short_train, short_test = split_short(random.Random(11))
    train = train + short_train
    augmented = [{"text": v, "intent": r["intent"]} for r in train for v in variants(r["text"], rng, 2)]
    train_v2 = train + augmented + other_train_rows
    rng.shuffle(train_v2)
    val_v2 = val + [{"text": t, "intent": "other"} for t in other_val]

    noisy_rng = random.Random(7)                       # different randomness from training
    test_noisy = [{"text": v, "intent": r["intent"]} for r in test for v in variants(r["text"], noisy_rng, 1)]
    test_other = [{"text": t, "intent": "other"} for t in other_test]

    # No evaluation message may appear in training: drop any training row that matches one
    # (the test sets themselves stay complete, so the original test is comparable with v1).
    held_out = {r["text"].lower() for r in val_v2 + test + test_noisy + test_other + short_test}
    leaks = [r["text"] for r in train_v2 if r["text"].lower() in held_out]
    train_v2 = [r for r in train_v2 if r["text"].lower() not in held_out]

    OUT.mkdir(parents=True, exist_ok=True)
    for name, rows in [("train", train_v2), ("val", val_v2), ("test", test),
                       ("test_noisy", test_noisy), ("test_other", test_other), ("test_short", short_test)]:
        (OUT / f"{name}.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
        counts = {}
        for r in rows:
            counts[r["intent"]] = counts.get(r["intent"], 0) + 1
        print(f"{name:11} {len(rows):5} rows  {dict(sorted(counts.items()))}")
    print(f"training rows identical to an evaluation message (removed from training): {len(leaks)}")


if __name__ == "__main__":
    main()
