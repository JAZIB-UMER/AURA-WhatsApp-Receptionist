"""Google Calendar through one service account (the "robot" Google account AURA acts as).

The service-account key is a platform setting (dashboard -> Platform settings). Each business shares
its own calendar with the service account's email; the calendar ID is set on the business.
"""
import json
import time
from datetime import datetime, timedelta, timezone

from google.oauth2 import service_account
from googleapiclient.discovery import build

from app.services import config

SCOPES = ['https://www.googleapis.com/auth/calendar']

# Pakistan Standard Time is UTC+5 all year (no daylight saving).
PKT = timezone(timedelta(hours=5))

_creds = None
_creds_key = None


def get_calendar_service():
    """Calendar API client for the saved service account. A new client per call (they aren't
    thread-safe); the credentials are reused until the admin uploads a new key."""
    global _creds, _creds_key
    raw = config.get("google_service_account")
    if not raw:
        raise RuntimeError("No Google service account saved (dashboard → Platform settings).")
    if _creds is None or raw != _creds_key:
        _creds = service_account.Credentials.from_service_account_info(json.loads(raw), scopes=SCOPES)
        _creds_key = raw
    return build('calendar', 'v3', credentials=_creds, cache_discovery=False)


def _slot(date: str, time: str, duration: int = 60):
    """Return (start, end) as timezone-aware datetimes in Pakistan time."""
    start = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M").replace(tzinfo=PKT)
    return start, start + timedelta(minutes=duration)


def _event_times(event):
    """(start, end) of a calendar event; all-day events block the whole day."""
    start, end = event.get('start', {}), event.get('end', {})
    if 'dateTime' in start:
        return (datetime.fromisoformat(start['dateTime'].replace('Z', '+00:00')),
                datetime.fromisoformat(end['dateTime'].replace('Z', '+00:00')))
    day_start = datetime.fromisoformat(start['date']).replace(tzinfo=PKT)
    day_end = datetime.fromisoformat(end['date']).replace(tzinfo=PKT)
    return day_start, day_end


def get_busy(date: str, calendar_id: "str | None"):
    """Busy (start, end) periods on a day, or None if the calendar can't be read."""
    if not calendar_id:
        print("Calendar error: this business has no calendar ID")
        return None
    for attempt in range(2):          # one retry for a temporary Google / network problem
        try:
            service = get_calendar_service()
            day_start = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=PKT)
            events = service.events().list(
                calendarId=calendar_id,
                timeMin=day_start.isoformat(),
                timeMax=(day_start + timedelta(days=1)).isoformat(),
                singleEvents=True,
            ).execute().get('items', [])
            # Events marked "Free" in Google Calendar don't block bookings.
            return [_event_times(e) for e in events if e.get('transparency') != 'transparent']
        except Exception as e:
            status = getattr(getattr(e, 'resp', None), 'status', None)
            if status in (403, 404):  # not shared with AURA / wrong ID: retrying won't help
                print(f"Calendar error: {calendar_id} is not shared with AURA's service account "
                      f"or doesn't exist (HTTP {status})")
                return None
            if attempt == 0:
                time.sleep(1)
                continue
            print(f"Calendar error: {e}")
    return None


def check_availability(date: str, time: str, calendar_id: "str | None", duration: int = 60) -> bool:
    busy = get_busy(date, calendar_id)
    if busy is None:
        return False          # if we cannot read the calendar, never claim the slot is free
    start, end = _slot(date, time, duration)
    return not any(start < b_end and b_start < end for b_start, b_end in busy)


def book_appointment(
    name: str,
    service: str,
    date: str,
    time: str,
    phone: str,
    calendar_id: "str | None" = None,
    duration: int = 60,
    notes: str = "",
) -> dict:
    if not calendar_id:
        return {"success": False, "error": "This business has no calendar ID"}
    try:
        service_obj = get_calendar_service()
        start, end = _slot(date, time, duration)
        description = f"Customer: {name}\nPhone: {phone}\nService: {service}"
        if notes:
            description += f"\n{notes}"
        event = {
            'summary': f"{service} — {name}",
            'description': description + "\n\nBooked by AURA",
            'start': {'dateTime': start.isoformat(), 'timeZone': 'Asia/Karachi'},
            'end': {'dateTime': end.isoformat(), 'timeZone': 'Asia/Karachi'},
        }
        event = service_obj.events().insert(calendarId=calendar_id, body=event).execute()
        return {"success": True, "event_id": event.get('id')}
    except Exception as e:
        print(f"Booking error: {e}")
        return {"success": False, "error": str(e)}


def event_exists(event_id: str, calendar_id: "str | None"):
    """True if the event is still on the calendar, False if the owner deleted it,
    None if the calendar can't be reached (then we assume nothing changed)."""
    if not calendar_id:
        return None
    try:
        service = get_calendar_service()
        event = service.events().get(calendarId=calendar_id, eventId=event_id).execute()
        return event.get('status') != 'cancelled'
    except Exception as e:
        if getattr(getattr(e, 'resp', None), 'status', None) in (404, 410):
            return False
        print(f"Calendar check error: {e}")
        return None


def cancel_appointment(event_id: str, calendar_id: "str | None") -> bool:
    if not calendar_id:
        return False
    try:
        service = get_calendar_service()
        service.events().delete(calendarId=calendar_id, eventId=event_id).execute()
        return True
    except Exception as e:
        print(f"Cancel error: {e}")
        return False
