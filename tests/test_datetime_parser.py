"""Tests for Roman Urdu / English date and time parsing.

Run:  python -m pytest tests/        (or)   python tests/test_datetime_parser.py
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.services.datetime_parser import parse_date, parse_time, describe  # noqa: E402

TODAY = date(2026, 11, 10)  # a Tuesday

DATE_CASES = [
    ("kal 3 baje haircut", "2026-11-11"), ("aaj sham", "2026-11-10"), ("parso", "2026-11-12"),
    ("day after tomorrow", "2026-11-12"), ("tomorrow at 4pm", "2026-11-11"),
    ("Friday ko", "2026-11-13"), ("coming friday", "2026-11-13"), ("Tuesday", "2026-11-17"),
    ("jumma ko aana hai", "2026-11-13"), ("itwar", "2026-11-15"), ("peer ko", "2026-11-16"),
    ("15 nov", "2026-11-15"), ("nov 20", "2026-11-20"), ("5 jan", "2027-01-05"),
    ("15th november", "2026-11-15"), ("1st jan", "2027-01-01"),
    ("14/11", "2026-11-14"), ("2/1", "2027-01-02"), ("25-12-2026", "2026-12-25"),
    ("2026-11-20", "2026-11-20"), ("20 tareekh ko", "2026-11-20"), ("5 tarikh", "2026-12-05"),
    # must NOT be read as dates
    ("sat baje", None), ("sun lo bhai", None), ("haircut chahiye", None), ("4.30 baje", None),
    ("1st time aa raha hoon", None),
]

TIME_CASES = [
    ("kal 3 baje", "15:00"), ("3 bje", "15:00"), ("10 baje", "10:00"), ("12 baje", "12:00"),
    ("9 baje", "09:00"), ("sham 6 baje", "18:00"), ("sham 6", "18:00"), ("raat 8", "20:00"),
    ("subah 10", "10:00"), ("subah 7 baje", "07:00"), ("dopahar 2", "14:00"),
    ("dopahar 12 baje", "12:00"), ("4pm", "16:00"), ("4:30 pm", "16:30"), ("10am", "10:00"),
    ("11:30", "11:30"), ("15:00", "15:00"), ("at 4", "16:00"), ("12 am", "00:00"),
    ("saadhe 4", "16:30"), ("sawa 3", "15:15"), ("paune 5", "16:45"), ("subah saadhe 9", "09:30"),
    ("teen baje", "15:00"), ("sat baje", "19:00"), ("sham saat baje", "19:00"),
    ("saadhe char baje", "16:30"), ("do baje", "14:00"), ("4.30 baje", "16:30"),
    ("Salam, can I visit today at 4:30 pm for teeth cleaning?", "16:30"),
    ("Friday at 12", "12:00"), ("Meri booking khatam kr dein aj 5 baje ki", "17:00"),
    # vague or not a time -> None, so AURA asks instead of guessing
    ("15 nov ko", None), ("14/11", None), ("haircut chahiye", None),
    ("mere 2 bachay hain", None), ("2026-11-20", None), ("sham ko", None), ("abhi", None),
]


def test_dates():
    for text, expected in DATE_CASES:
        assert parse_date(text, TODAY) == expected, f"{text!r}: {parse_date(text, TODAY)} != {expected}"


def test_times():
    for text, expected in TIME_CASES:
        assert parse_time(text) == expected, f"{text!r}: {parse_time(text)} != {expected}"


def test_clean_values_round_trip():
    """Already-clean values must never change when parsed again (07:00 must not become 19:00)."""
    for h in range(24):
        for m in (0, 15, 30, 45):
            hhmm = f"{h:02d}:{m:02d}"
            assert parse_time(hhmm) == hhmm, f"{hhmm} -> {parse_time(hhmm)}"
    for iso in ("2026-11-10", "2026-12-31", "2027-02-28"):
        assert parse_date(iso, TODAY) == iso


def test_describe():
    assert describe("2026-11-13", "16:30") == "Friday 13 Nov, 4:30 PM"


if __name__ == "__main__":
    test_dates()
    test_times()
    test_clean_values_round_trip()
    test_describe()
    print(f"All {len(DATE_CASES) + len(TIME_CASES) + 1} parser tests passed")
