"""Small extractor for scheduling utterances. Not a general language model."""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from scheduler.clinic import NOW

MONTHS = {name.lower(): index for index, name in enumerate(calendar.month_name) if name}
WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
KNOWN_NAMES = ("Maya Chen", "Luis Ortega", "Priya Nair", "Jonah Ellis")
RED_FLAGS = (
    "chest pain",
    "shortness of breath",
    "trouble breathing",
    "severe bleeding",
    "anaphylaxis",
    "suicidal",
    "stroke symptoms",
)
DOB_MONTH = re.compile(
    r"\b(january|february|march|april|may|june|july|august|september|october|november|december)"
    r"\s+(\d{1,2}),?\s+(\d{4})\b",
    re.IGNORECASE,
)
DOB_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
DOB_SLASH = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")
APPT_MONTH = re.compile(
    r"\b(january|february|march|april|may|june|july|august|september|october|november|december)"
    r"\s+(\d{1,2})\b(?!\s*,?\s*\d{4})",
    re.IGNORECASE,
)
HOUR_PATTERN = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", re.IGNORECASE)


@dataclass
class Parsed:
    name: str | None = None
    dob: str | None = None
    caller_is_patient: bool | None = None
    intent: str | None = None
    visit_type: str | None = None
    red_flags: list[str] = field(default_factory=list)
    advice: bool = False
    decline_scheduling: bool = False
    weekday: int | None = None
    hour: int | None = None
    on_date: date | None = None
    time_of_day: str | None = None
    affirmative: bool = False
    fee_ack: bool = False
    choice_index: int | None = None


def parse(text: str) -> Parsed:
    raw = " ".join(text.split())
    lowered = raw.lower()
    parsed = Parsed()
    parsed.name = _name(raw)
    parsed.dob = _dob(lowered)
    parsed.caller_is_patient = _caller(lowered)
    parsed.red_flags = [flag for flag in RED_FLAGS if flag in lowered]
    parsed.advice = any(
        cue in lowered
        for cue in ("antibiotic", "is it serious", "diagnose", "what medication")
    )
    parsed.decline_scheduling = bool(
        re.search(r"do not schedule|don't schedule|only want advice", lowered)
    )
    parsed.visit_type = _visit(lowered)
    parsed.intent = _intent(lowered, parsed)
    parsed.weekday = _weekday(lowered)
    parsed.hour = _hour(lowered)
    parsed.on_date = _appointment_date(lowered)
    if "afternoon" in lowered:
        parsed.time_of_day = "afternoon"
    elif "morning" in lowered:
        parsed.time_of_day = "morning"
    parsed.fee_ack = "fee" in lowered and any(
        word in lowered for word in ("understand", "acknowledge", "yes")
    )
    parsed.choice_index = _choice(lowered)
    parsed.affirmative = _affirmative(lowered)
    return parsed


def next_date_for_weekday(weekday: int, today: datetime = NOW) -> date:
    days = (weekday - today.weekday()) % 7
    return (today + timedelta(days=days)).date()


def _name(text: str) -> str | None:
    lowered = text.lower()
    for name in KNOWN_NAMES:
        if name.lower() in lowered:
            return name
    return None


def _dob(lowered: str) -> str | None:
    iso = DOB_ISO.search(lowered)
    if iso:
        return f"{int(iso.group(1)):04d}-{int(iso.group(2)):02d}-{int(iso.group(3)):02d}"
    spoken = DOB_MONTH.search(lowered)
    if spoken:
        month = MONTHS[spoken.group(1).lower()]
        return f"{int(spoken.group(3)):04d}-{month:02d}-{int(spoken.group(2)):02d}"
    slash = DOB_SLASH.search(lowered)
    if slash:
        # Assumption: US month/day/year.
        month, day, year = int(slash.group(1)), int(slash.group(2)), int(slash.group(3))
        return f"{year:04d}-{month:02d}-{day:02d}"
    return None


def _caller(lowered: str) -> bool | None:
    if re.search(
        r"\b(wife|husband|spouse|son|daughter|mother|father|family member|on behalf|calling for)\b",
        lowered,
    ):
        return False
    if re.search(r"\bi am the patient\b|\bi'm the patient\b|\bi am not the patient\b", lowered):
        if "not the patient" in lowered:
            return False
        return True
    return None


def _visit(lowered: str) -> str | None:
    if "annual physical" in lowered or re.search(r"\bphysical\b", lowered):
        return "annual_physical"
    if "dermatolog" in lowered:
        return "dermatology"
    if "follow-up" in lowered or "follow up" in lowered:
        return "follow_up"
    if "sick" in lowered:
        return "sick_visit"
    return None


def _intent(lowered: str, parsed: Parsed) -> str | None:
    if parsed.decline_scheduling or (
        parsed.advice and not re.search(r"\b(book|reschedule)\b", lowered)
    ):
        return "advice"
    if "cancel" in lowered:
        return "cancel"
    if "reschedule" in lowered or "move my" in lowered:
        return "reschedule"
    if re.search(r"when is|what time|next appointment", lowered):
        return "lookup"
    if re.search(r"\b(book|schedule|come in|appointment|physical|sick visit)\b", lowered):
        return "book"
    return None


def _weekday(lowered: str) -> int | None:
    for name, index in WEEKDAYS.items():
        if re.search(rf"\b{name}\b", lowered):
            return index
    return None


def _hour(lowered: str) -> int | None:
    match = HOUR_PATTERN.search(lowered)
    if not match:
        return None
    hour = int(match.group(1))
    suffix = match.group(3).lower()
    if suffix == "pm" and hour != 12:
        hour += 12
    if suffix == "am" and hour == 12:
        hour = 0
    return hour


def _appointment_date(lowered: str) -> date | None:
    match = APPT_MONTH.search(lowered)
    if not match:
        return None
    month = MONTHS[match.group(1).lower()]
    day = int(match.group(2))
    year = NOW.year
    try:
        found = date(year, month, day)
    except ValueError:
        return None
    if found < NOW.date():
        found = date(year + 1, month, day)
    return found


def _choice(lowered: str) -> int | None:
    stripped = lowered.strip(" .!")
    if stripped in {"1", "1."} or "the first one" in lowered:
        return 0
    if stripped in {"2", "2."} or "the second one" in lowered:
        return 1
    if stripped in {"3", "3."} or "the third one" in lowered:
        return 2
    return None


def _affirmative(lowered: str) -> bool:
    if re.search(r"\b(works|whenever|sometime)\b", lowered) and "yes" not in lowered:
        return False
    return bool(
        re.search(
            r"\b(yes|yeah|yep|go ahead|please book|please cancel|confirm|that's correct|that is correct)\b",
            lowered,
        )
    )
