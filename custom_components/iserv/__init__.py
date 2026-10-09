"""The IServ integration.

Fetches the timetable (Stundenplan) and the substitution plan
(Vertretungsplan) of an IServ server and exposes them through one single Home
Assistant device called ``IServ``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from html import unescape
from typing import Any
from urllib.parse import urljoin, urlparse

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import (
    APP_LOGIN_PATH,
    CANCEL_CHANGE_TYPE,
    CANCEL_SUBSTITUTION_TYPES,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
    CONNECTION_TIMEOUT,
    CURRENT_TIMETABLE_PATH,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DEVICE_NAME,
    DOMAIN,
    FALLBACK_PERIOD_TIMES,
    MANUFACTURER,
    MAX_LOGIN_REDIRECTS,
    MAX_LOOKAHEAD_DAYS,
    MODEL,
    REQUEST_TIMEOUT,
    STATUS_CANCELLED,
    STATUS_REGULAR,
    STATUS_SUBSTITUTED,
    TIME_SOURCE_FALLBACK,
    TIME_SOURCE_ISERV,
    TIME_SOURCE_UNKNOWN,
    TIMETABLE_DATA_PATH,
    VERSION,
    WEEKDAYS_DE,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]

# Week offsets that are fetched so that "next school day" also works on
# Friday, Saturday and Sunday.
WEEK_OFFSETS: tuple[int, ...] = (0, 1)

# Recognised login form markers (IServ ships different login templates).
_LOGIN_MARKERS = ("iserv", "login", "anmeld", "passwort", "password", "benutzer")
# Hidden inputs whose values look like a session id instead of a CSRF token.
_SESSION_FIELD_MARKERS = ("session", "sid", "jsession", "phpsess", "authid")


class IServError(Exception):
    """Base exception for all IServ related errors."""


class IServAuthError(IServError):
    """Raised when IServ rejects the credentials or the session expired."""


class IServConnectionError(IServError):
    """Raised when the IServ server cannot be reached or answers unexpectedly."""


class _EndpointUnavailable(IServError):
    """Raised when a specific IServ generation does not expose an endpoint."""


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------
def normalize_host(host: str) -> str:
    """Return a clean base URL (``https://iserv.example.org``) for a host input."""
    value = (host or "").strip().rstrip("/")
    if not value:
        raise ValueError("empty host")
    if "://" in value:
        if not value.startswith(("http://", "https://")):
            raise ValueError(f"unsupported scheme in host: {host}")
    else:
        value = f"https://{value}"

    parts = urlparse(value)
    hostname = parts.hostname or ""
    if not parts.netloc or ("." not in hostname and hostname != "localhost"):
        raise ValueError(f"invalid host: {host}")
    try:
        port = parts.port
    except ValueError as err:
        raise ValueError(f"invalid port in host: {host}") from err

    netloc = hostname if port is None else f"{hostname}:{port}"
    return f"{parts.scheme}://{netloc}"


def _week_monday(week_offset: int = 0) -> date:
    """Return the Monday of the week that is ``week_offset`` weeks away."""
    today = date.today()
    return today - timedelta(days=today.weekday()) + timedelta(weeks=week_offset)


def _week_filter(monday: date) -> str:
    """Return the IServ table filter that selects a whole week."""
    sunday = monday + timedelta(days=6)
    return f"startDate={monday.strftime('%Y-%m-%d')}&endDate={sunday.strftime('%Y-%m-%d')}"


def _extract_hidden_inputs(html: str) -> dict[str, str]:
    """Extract ``<input type="hidden" ...>`` name/value pairs from a login form."""
    fields: dict[str, str] = {}
    for tag in re.findall(r"<input\b[^>]*>", html, flags=re.IGNORECASE):
        if "hidden" not in tag.lower():
            continue
        name_match = re.search(r"""name\s*=\s*["']([^"']+)["']""", tag, flags=re.I)
        if name_match is None:
            continue
        value_match = re.search(r"""value\s*=\s*["']([^"']*)["']""", tag, flags=re.I)
        fields[name_match.group(1)] = unescape(value_match.group(1)) if value_match else ""
    return fields


def _meta_refresh_url(html: str, current_url: str) -> str | None:
    """Return the target of the first ``<meta http-equiv="refresh">`` tag."""
    for tag in re.findall(r"<meta\b[^>]*>", html, flags=re.IGNORECASE):
        if "refresh" not in tag.lower():
            continue
        match = re.search(
            r"""content\s*=\s*["'][^"';]*;\s*url\s*=\s*([^"']+)["']""", tag, flags=re.I
        )
        if match is None:
            continue
        target = unescape(match.group(1).strip().strip("'\""))
        if target:
            return urljoin(current_url, target)
    return None


def _is_login_form(html: str) -> bool:
    """Return True if the HTML still contains an IServ login form."""
    lowered = html.lower()
    if "<form" not in lowered:
        return False
    return any(marker in lowered for marker in _LOGIN_MARKERS)


# ---------------------------------------------------------------------------
# Value helpers
# ---------------------------------------------------------------------------
def _clock_time(value: Any) -> str:
    """Normalise a time value to ``HH:MM`` (empty string if unknown)."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = str(int(value))
    if not isinstance(value, str):
        return ""

    text = value.strip()
    if not text or text.upper() in {"Z", "NULL", "NONE", "-", "--"}:
        return ""

    match = re.search(r"(\d{1,2})[:.](\d{2})", text)
    if match is not None:
        hour, minute = int(match.group(1)), int(match.group(2))
    elif re.fullmatch(r"\d{3,4}", text):
        hour, minute = int(text[:-2]), int(text[-2:])
    elif re.fullmatch(r"\d{1,2}", text):
        hour, minute = int(text), 0
    else:
        return ""
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return ""
    return f"{hour:02d}:{minute:02d}"


def _first_string(source: Any, *keys: str) -> str:
    """Return the first non-empty value of ``keys`` inside ``source``."""
    if not isinstance(source, dict):
        return ""
    for key in keys:
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
    return ""


def _display_value(value: Any) -> str:
    """Render an IServ teacher/room/subject object as a plain string."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("name", "displayName", "display_name", "acronym", "longName",
                    "shortName", "value", "id"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
            if isinstance(candidate, int) and not isinstance(candidate, bool):
                return str(candidate)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def _parse_date_string(value: str) -> date | None:
    """Parse the date formats that are used by IServ."""
    text = value.strip()
    if not text:
        return None
    for date_format in ("%d.%m.%Y", "%Y-%m-%d", "%d.%m.%y"):
        try:
            return datetime.strptime(text[:10], date_format).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text[:19]).date()
    except ValueError:
        return None


def _period_number(*values: Any) -> int | None:
    """Return the first usable lesson period ("Stunde") number."""
    for value in values:
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
        if isinstance(value, dict):
            nested = _period_number(
                value.get("number"), value.get("period"), value.get("id"), value.get("name")
            )
            if nested is not None:
                return nested
    return None


def _entry_date(entry: dict[str, Any], slot: dict[str, Any] | None, monday: date) -> date | None:
    """Determine the real date of a timetable entry."""
    explicit = _first_string(entry, "date", "lessonDate")
    if explicit:
        parsed = _parse_date_string(explicit)
        if parsed is not None:
            return parsed

    weekday = entry.get("weekday")
    if isinstance(weekday, int) and not isinstance(weekday, bool) and 0 <= weekday <= 6:
        return monday + timedelta(days=weekday)

    name = _first_string(entry, "weekdayName", "day")
    if name in WEEKDAYS_DE:
        return monday + timedelta(days=WEEKDAYS_DE.index(name))

    if isinstance(slot, dict):
        started = _first_string(slot, "start", "startTime", "start_time")
        if started:
            parsed = _parse_date_string(started)
            if parsed is not None:
                return parsed
    return None


def _lesson_times(period: int | None, start: str, end: str) -> tuple[str, str, str]:
    """Resolve start/end time of a lesson, falling back to the period grid.

    Returns ``(start, end, source)`` where source is ``iserv``, ``fallback``
    or ``unknown``.
    """
    start_hm = _clock_time(start)
    end_hm = _clock_time(end)
    grid = FALLBACK_PERIOD_TIMES.get(period) if period is not None else None
    if grid is None:
        return start_hm, end_hm, TIME_SOURCE_ISERV if start_hm else TIME_SOURCE_UNKNOWN
    # Times delivered by IServ always win, the grid only fills the gaps.
    source = TIME_SOURCE_ISERV if start_hm else TIME_SOURCE_FALLBACK
    return start_hm or grid[0], end_hm or grid[1], source


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class IServLesson:
    """One lesson of the IServ timetable (regular or changed)."""

    date: date
    period: int | None = None
    start_time: str = ""
    end_time: str = ""
    subject: str = ""
    room: str = ""
    teacher: str = ""
    substitute_subject: str = ""
    substitute_room: str = ""
    substitute_teacher: str = ""
    canceled: bool = False
    substitution: bool = False
    note: str = ""
    time_source: str = TIME_SOURCE_ISERV

    @property
    def weekday(self) -> int:
        """Weekday index (0 = Monday)."""
        return self.date.weekday()

    @property
    def day(self) -> str:
        """German weekday name."""
        return WEEKDAYS_DE[self.weekday]

    @property
    def status(self) -> str:
        """Status marker for Lovelace cards: regular/substituted/cancelled."""
        if self.canceled:
            return STATUS_CANCELLED
        if self.substitution:
            return STATUS_SUBSTITUTED
        return STATUS_REGULAR

    @property
    def changed(self) -> bool:
        """True if the lesson is cancelled or substituted."""
        return self.canceled or self.substitution

    @property
    def effective_subject(self) -> str:
        return self.substitute_subject or self.subject

    @property
    def effective_room(self) -> str:
        return self.substitute_room or self.room

    @property
    def effective_teacher(self) -> str:
        return self.substitute_teacher or self.teacher

    def as_dict(self) -> dict[str, Any]:
        """Return the lesson as a JSON serialisable dict for the attributes."""
        return {
            "date": self.date.isoformat(),
            "day": self.day,
            "weekday": self.weekday,
            "period": self.period,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "subject": self.effective_subject,
            "room": self.effective_room,
            "teacher": self.effective_teacher,
            "status": self.status,
            "cancelled": self.canceled,
            "substituted": self.substitution,
            "original_subject": self.subject,
            "original_room": self.room,
            "original_teacher": self.teacher,
            "substitute_subject": self.substitute_subject,
            "substitute_room": self.substitute_room,
            "substitute_teacher": self.substitute_teacher,
            "time_source": self.time_source,
            "note": self.note,
        }


@dataclass(slots=True)
class IServSchoolStart:
    """The beginning of the next school day."""

    date: date
    start_time: str
    period: int | None = None
    time_source: str = TIME_SOURCE_ISERV
    is_today: bool = False
    skipped: list[IServLesson] = field(default_factory=list)

    @property
    def day(self) -> str:
        """German weekday name."""
        return WEEKDAYS_DE[self.date.weekday()]

    @property
    def hour(self) -> int:
        return int(self.start_time[:2]) if len(self.start_time) >= 5 else 0

    @property
    def minute(self) -> int:
        return int(self.start_time[3:5]) if len(self.start_time) >= 5 else 0

    def as_dict(self) -> dict[str, Any]:
        """Return the school start as a JSON serialisable dict."""
        return {
            "date": self.date.isoformat(),
            "day": self.day,
            "weekday": self.date.weekday(),
            "time": self.start_time,
            "period": self.period,
            "time_source": self.time_source,
            "is_today": self.is_today,
            "skipped_lessons": [lesson.as_dict() for lesson in self.skipped],
        }


def _sort_key(lesson: IServLesson) -> tuple[str, str, int]:
    """Sort lessons by date, start time and period."""
    return (lesson.date.isoformat(), lesson.start_time or "99:99", lesson.period or 0)


@dataclass(slots=True)
class IServData:
    """Everything the coordinator knows about the IServ account."""

    lessons: list[IServLesson] = field(default_factory=list)

    def sorted_lessons(self) -> list[IServLesson]:
        """Return all lessons sorted by date and start time."""
        return sorted(self.lessons, key=_sort_key)

    def lessons_on(self, target: date) -> list[IServLesson]:
        """Return all (planned) lessons of a single day."""
        return sorted(
            (lesson for lesson in self.lessons if lesson.date == target),
            key=_sort_key,
        )

    def changes_on(self, target: date) -> list[IServLesson]:
        """Return cancelled/substituted lessons of a single day."""
        return [lesson for lesson in self.lessons_on(target) if lesson.changed]

    def cancellations_on(self, target: date) -> list[IServLesson]:
        """Return cancelled lessons of a single day."""
        return [lesson for lesson in self.lessons_on(target) if lesson.canceled]

    def substitutions_on(self, target: date) -> list[IServLesson]:
        """Return substituted lessons of a single day."""
        return [
            lesson
            for lesson in self.lessons_on(target)
            if lesson.substitution and not lesson.canceled
        ]

    @property
    def school_days(self) -> list[date]:
        """All days that have at least one lesson."""
        return sorted({lesson.date for lesson in self.lessons})

    def school_start(self, target: date) -> IServSchoolStart | None:
        """Return the first lesson that really takes place on ``target``.

        Cancelled lessons are skipped, so the school day starts later when
        period 1 (and maybe 2, 3, ...) is cancelled. If no lesson takes place
        at all (weekend, holiday, all lessons cancelled) ``None`` is returned.
        """
        lessons = self.lessons_on(target)
        if not lessons:
            return None

        taking_place = [
            lesson for lesson in lessons if not lesson.canceled and lesson.start_time
        ]
        if not taking_place:
            return None

        first = min(taking_place, key=_sort_key)
        skipped = [
            lesson
            for lesson in lessons
            if lesson.canceled and lesson.start_time and _sort_key(lesson) < _sort_key(first)
        ]
        return IServSchoolStart(
            date=target,
            start_time=first.start_time,
            period=first.period,
            time_source=first.time_source,
            skipped=skipped,
        )

    def next_school_start(self, now: datetime) -> IServSchoolStart | None:
        """Return the upcoming school start.

        Today is used as long as the first lesson of the day has not started
        yet, otherwise the next day with lessons is searched. This makes the
        sensor usable both in the evening (tomorrow) and in the early morning
        (today) for smart alarms.
        """
        today = now.date()
        now_hm = now.strftime("%H:%M")
        for offset in range(MAX_LOOKAHEAD_DAYS):
            target = today + timedelta(days=offset)
            start = self.school_start(target)
            if start is None:
                continue
            if offset == 0 and start.start_time <= now_hm:
                continue
            start.is_today = offset == 0
            return start
        return None


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------
def _change_types(entry: Any) -> list[str]:
    """Return the ``change_types`` of an entry as a list of strings."""
    if not isinstance(entry, dict):
        return []
    value = entry.get("change_types")
    if value is None:
        value = entry.get("changeTypes")
    collected: list[str] = []
    if isinstance(value, dict):
        collected.extend(str(key) for key in value)
        collected.extend(str(item) for item in value.values())
    elif isinstance(value, (list, tuple, set)):
        collected.extend(str(item) for item in value)
    elif value not in (None, "", False):
        collected.append(str(value))
    return [item.strip() for item in collected if item and item.strip()]


def _substitution_type_text(value: Any) -> str:
    """Return the plain text of a ``substitutionType`` object."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("name", "displayName", "display_name", "value", "key", "id"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return ""


def _is_canceled_substitution(value: Any) -> bool:
    """Return True when a substitution type text means "cancelled"."""
    text = _substitution_type_text(value).lower()
    if not text:
        return False
    if any(marker in text for marker in CANCEL_SUBSTITUTION_TYPES):
        return True
    return "cancel" in text or "entf" in text


def _change_is_canceled(
    substitution_type: Any, change: dict[str, Any] | None, change_types: list[str]
) -> bool:
    """Return True when the entry describes a cancelled lesson."""
    if _is_canceled_substitution(substitution_type):
        return True
    if CANCEL_CHANGE_TYPE in change_types:
        return True
    if isinstance(change, dict):
        if _is_canceled_substitution(change.get("substitutionType")):
            return True
        for key in ("canceled", "cancelled", "isCanceled", "is_canceled", "canceledLesson"):
            if change.get(key) is True:
                return True
    return False


def _course_subject(course: Any, fallback: str = "") -> str:
    """Return the plain subject name of a ``courseSubject`` object."""
    if isinstance(course, str):
        return course.strip() or fallback
    if isinstance(course, dict):
        for key in ("subject", "name", "displayName", "longName", "shortName",
                    "acronym", "label", "value"):
            value = course.get(key)
            if isinstance(value, dict):
                nested = _display_value(value)
                if nested:
                    return nested
            elif isinstance(value, str) and value.strip():
                return value.strip()
    return fallback


def _period_times(payload: Any) -> dict[str, tuple[str, str]]:
    """Return the ``period -> (start, end)`` raster of the legacy API."""
    mapping: dict[str, tuple[str, str]] = {}
    containers: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        meta = payload.get("meta")
        if isinstance(meta, dict):
            containers.append(meta)
        containers.append(payload)

    for container in containers:
        for key in ("lessonTimes", "periods", "times", "hours"):
            entries = container.get(key)
            if not isinstance(entries, list):
                continue
            for item in entries:
                if not isinstance(item, dict):
                    continue
                period = _period_number(
                    item.get("period"), item.get("number"), item.get("id"), item.get("name")
                )
                start = _clock_time(
                    _first_string(item, "start", "startTime", "start_time", "from")
                )
                end = _clock_time(_first_string(item, "end", "endTime", "end_time", "to"))
                if period is not None and start:
                    mapping[str(period)] = (start, end)
    return mapping


def _lesson_from_entry(
    entry: dict[str, Any],
    lesson_date: date,
    period: int | None,
    start: str,
    end: str,
) -> IServLesson | None:
    """Build an :class:`IServLesson` from a raw IServ entry."""
    start_hm, end_hm, source = _lesson_times(period, start, end)
    if not start_hm:
        return None

    change = entry.get("change")
    change = change if isinstance(change, dict) else None
    substitution_type = entry.get("substitutionType") or (change or {}).get("substitutionType")
    change_types = _change_types(change) or _change_types(entry)

    return IServLesson(
        date=lesson_date,
        period=period,
        start_time=start_hm,
        end_time=end_hm,
        subject=_course_subject(entry.get("courseSubject"), _first_string(entry, "subject")),
        room=_display_value(entry.get("room")) or _first_string(entry, "roomName", "room_name"),
        teacher=_display_value(entry.get("teacher")),
        substitute_subject=_first_string(change, "substitutionSubject", "substitution_subject"),
        substitute_room=_display_value((change or {}).get("substitutionRoom"))
        or _display_value((change or {}).get("substitution_room")),
        substitute_teacher=_display_value((change or {}).get("substitutionTeacher"))
        or _display_value((change or {}).get("substitution_teacher")),
        canceled=_change_is_canceled(substitution_type, change, change_types),
        substitution=change is not None or bool(_substitution_type_text(substitution_type)),
        note=_substitution_type_text(substitution_type)
        or _first_string(change, "comment", "note", "reason", "text"),
        time_source=source,
    )


def _parse_current_timetable(payload: Any, monday: date) -> list[IServLesson]:
    """Parse the ``/dieschulapp/api/1.0/current-timetable/`` response."""
    entries: list[Any] | None = None
    if isinstance(payload, list):
        entries = payload
    elif isinstance(payload, dict):
        for key in ("entries", "lessons", "timetable", "data"):
            candidate = payload.get(key)
            if isinstance(candidate, list):
                entries = candidate
                break
    if entries is None:
        raise _EndpointUnavailable("no timetable entries in the current-timetable response")

    lessons: list[IServLesson] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        slot = entry.get("timeTableSlot") or entry.get("slot") or entry.get("timeSlot")
        slot = slot if isinstance(slot, dict) else None
        lesson_date = _entry_date(entry, slot, monday)
        if lesson_date is None:
            continue
        period = _period_number(
            entry.get("period"),
            entry.get("lessonNumber"),
            entry.get("lesson"),
            (slot or {}).get("number"),
            (slot or {}).get("period"),
        )
        start = _first_string(slot, "start", "startTime", "start_time", "begin")
        end = _first_string(slot, "end", "endTime", "end_time")
        lesson = _lesson_from_entry(entry, lesson_date, period, start, end)
        if lesson is not None:
            lessons.append(lesson)
    return lessons


# ---------------------------------------------------------------------------
# Login helpers
# ---------------------------------------------------------------------------
def _form_action(html: str, current_url: str) -> str:
    """Return the action URL of the first form of a HTML page."""
    match = re.search(r"<form\b[^>]*>", html, flags=re.IGNORECASE)
    if match is None:
        return current_url
    action = re.search(r"""action\s*=\s*["']([^"']*)["']""", match.group(0), flags=re.I)
    if action is None or not action.group(1).strip():
        return current_url
    return urljoin(current_url, unescape(action.group(1).strip()))


def _form_field(html: str, keywords: tuple[str, ...], default: str) -> str:
    """Return the name of the first visible input matching one of the keywords."""
    for tag in re.findall(r"<input\b[^>]*>", html, flags=re.IGNORECASE):
        if "hidden" in tag.lower():
            continue
        match = re.search(r"""name\s*=\s*["']([^"']+)["']""", tag, flags=re.I)
        if match is None:
            continue
        name = match.group(1)
        if any(keyword in name.lower() for keyword in keywords):
            return name
    return default


def _login_hidden_fields(html: str) -> dict[str, str]:
    """Return the hidden fields that have to be posted back to the login form."""
    fields: dict[str, str] = {}
    for name, value in _extract_hidden_inputs(html).items():
        lowered = name.lower()
        if not value and any(marker in lowered for marker in _SESSION_FIELD_MARKERS):
            continue
        fields[name] = value
    return fields


def _parse_timetable_data(payload: Any, monday: date) -> list[IServLesson]:
    """Parse the legacy ``/iserv/timetable/data`` response."""
    if not isinstance(payload, dict):
        raise _EndpointUnavailable("invalid timetable data response")

    container = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    entries: list[Any] | None = None
    for key in ("timetable", "rows", "entries", "lessons", "items"):
        candidate = container.get(key)
        if isinstance(candidate, list):
            entries = candidate
            break
    if entries is None:
        raise _EndpointUnavailable("no timetable entries in the timetable data response")

    period_times = _period_times(payload)
    lessons: list[IServLesson] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        lesson_date = _entry_date(entry, None, monday)
        if lesson_date is None:
            continue
        period = _period_number(
            entry.get("period"),
            entry.get("lesson"),
            entry.get("lessonNumber"),
            entry.get("number"),
        )
        start = _first_string(entry, "start_time", "startTime", "start", "from")
        end = _first_string(entry, "end_time", "endTime", "end", "to")
        if period is not None and str(period) in period_times:
            raster_start, raster_end = period_times[str(period)]
            start = start or raster_start
            end = end or raster_end
        lesson = _lesson_from_entry(entry, lesson_date, period, start, end)
        if lesson is not None:
            lessons.append(lesson)
    return lessons


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------
class IServApiClient:
    """Small asynchronous client for the IServ JSON endpoints."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        username: str,
        password: str,
    ) -> None:
        self._session = session
        self._base_url = normalize_host(host)
        self._username = username.strip()
        self._password = password
        self._logged_in = False

    @property
    def base_url(self) -> str:
        """Base URL of the IServ server (without a trailing slash)."""
        return self._base_url

    @property
    def username(self) -> str:
        """Username that is used to log in."""
        return self._username

    def _url(self, path: str) -> str:
        """Build an absolute URL for an IServ path."""
        return f"{self._base_url}{path}"

    async def _async_text(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        allow_redirects: bool = True,
    ) -> tuple[str, str]:
        """Perform a request and return ``(body, final_url)``."""
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT, connect=CONNECTION_TIMEOUT)
        try:
            async with self._session.request(
                method,
                url,
                params=params,
                data=data,
                allow_redirects=allow_redirects,
                timeout=timeout,
            ) as response:
                text = await response.text()
                status = response.status
                final_url = str(response.url)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise IServConnectionError(f"{method} {url} failed: {err}") from err

        if status in (401, 403):
            raise IServAuthError(f"IServ denied {method} {final_url} with HTTP {status}")
        if status >= 400:
            raise IServConnectionError(f"IServ answered HTTP {status} for {method} {final_url}")
        return text, final_url

    def _loads(self, text: str, url: str) -> Any:
        """Parse a JSON body and map HTML/empty answers to usable errors."""
        if text.lstrip().startswith("<"):
            if _is_login_form(text):
                raise IServAuthError(f"IServ session expired ({url})")
            raise _EndpointUnavailable(f"{url} answered with HTML")
        try:
            return json.loads(text)
        except json.JSONDecodeError as err:
            raise _EndpointUnavailable(f"{url} did not return JSON") from err

    async def async_login(self) -> None:
        """Log in to IServ and keep the session cookie for later requests."""
        self._logged_in = False
        html, url = await self._async_text("GET", self._url(APP_LOGIN_PATH))
        for _ in range(MAX_LOGIN_REDIRECTS):
            target = _meta_refresh_url(html, url)
            if target is None or target == url:
                break
            html, url = await self._async_text("GET", target)

        if not _is_login_form(html):
            # Some installations answer with the dashboard right away.
            self._logged_in = True
            return

        payload = _login_hidden_fields(html)
        payload[_form_field(html, ("user", "login", "benutz"), "_username")] = self._username
        payload[_form_field(html, ("pass", "pwd", "kennwort"), "_password")] = self._password

        response_text, _ = await self._async_text("POST", _form_action(html, url), data=payload)
        if _is_login_form(response_text):
            raise IServAuthError("IServ rejected the username or the password")
        self._logged_in = True

    async def _async_fetch_current_timetable(self, week_offset: int) -> Any:
        """Fetch one week from the DieSchulApp JSON API (with substitutions)."""
        text, final_url = await self._async_text(
            "GET",
            self._url(CURRENT_TIMETABLE_PATH),
            params={
                "date": _week_monday(week_offset).isoformat(),
                "week": "true",
                "substitutions": "true",
            },
        )
        return self._loads(text, final_url)

    async def _async_fetch_timetable_data(self, week_offset: int) -> Any:
        """Fetch one week from the legacy JSON API."""
        monday = _week_monday(week_offset)
        url = f"{self._url(TIMETABLE_DATA_PATH)}?dataType=json&{_week_filter(monday)}"
        text, final_url = await self._async_text("GET", url)
        return self._loads(text, final_url)

    async def async_get_lessons(self, week_offset: int = 0) -> list[IServLesson]:
        """Return the lessons of a week (0 = current week)."""
        if not self._logged_in:
            await self.async_login()

        monday = _week_monday(week_offset)
        problems: list[str] = []
        for fetch, parse in (
            (self._async_fetch_current_timetable, _parse_current_timetable),
            (self._async_fetch_timetable_data, _parse_timetable_data),
        ):
            try:
                payload = await fetch(week_offset)
                return parse(payload, monday)
            except _EndpointUnavailable as err:
                _LOGGER.debug(
                    "IServ endpoint %s unusable for week %s: %s",
                    fetch.__name__,
                    week_offset,
                    err,
                )
                problems.append(str(err))
        raise IServConnectionError(
            f"No usable IServ endpoint for week {week_offset}: {'; '.join(problems)}"
        )


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------
class IServDataUpdateCoordinator(DataUpdateCoordinator[IServData]):
    """Fetch the IServ timetable and substitution plan in the background."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: IServApiClient,
        scan_interval_minutes: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=timedelta(minutes=scan_interval_minutes),
        )
        self.client = client
        self.entry = entry

    async def _async_update_data(self) -> IServData:
        """Fetch the current week and (best effort) the following week."""
        lessons: list[IServLesson] = []
        try:
            lessons.extend(await self.client.async_get_lessons(WEEK_OFFSETS[0]))
        except IServAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except IServConnectionError as err:
            raise UpdateFailed(str(err)) from err

        for week_offset in WEEK_OFFSETS[1:]:
            try:
                lessons.extend(await self.client.async_get_lessons(week_offset))
            except IServAuthError as err:
                raise ConfigEntryAuthFailed(str(err)) from err
            except IServError as err:
                # The next week is only needed for the "next school day"
                # sensor, a missing week must not break the integration.
                _LOGGER.debug("IServ week %s could not be fetched: %s", week_offset, err)

        return IServData(lessons=lessons)


try:  # Home Assistant >= 2024.6 supports typed config entries
    IServConfigEntry = ConfigEntry[IServDataUpdateCoordinator]  # type: ignore[misc]
except TypeError:  # pragma: no cover - fallback for older Home Assistant versions
    IServConfigEntry = ConfigEntry  # type: ignore[misc, assignment]


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
async def async_setup_entry(hass: HomeAssistant, entry: IServConfigEntry) -> bool:
    """Set up IServ from a config entry."""
    host = normalize_host(entry.data[CONF_HOST])
    client = IServApiClient(
        async_get_clientsession(hass),
        host,
        str(entry.data[CONF_USERNAME]),
        str(entry.data[CONF_PASSWORD]),
    )

    scan_interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_MINUTES)
    try:
        scan_interval = int(scan_interval)
    except (TypeError, ValueError):
        scan_interval = DEFAULT_SCAN_INTERVAL_MINUTES

    coordinator = IServDataUpdateCoordinator(hass, entry, client, scan_interval)
    # Raises ConfigEntryAuthFailed / ConfigEntryNotReady when the first fetch
    # fails, which Home Assistant handles (reauth / retry).
    await coordinator.async_config_entry_first_refresh()

    # Exactly one device "IServ" holds every entity of this account. Creating
    # it here keeps the device visible even if a platform fails to set up.
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name=DEVICE_NAME,
        manufacturer=MANUFACTURER,
        model=MODEL,
        configuration_url=host,
        sw_version=VERSION,
    )

    entry.runtime_data = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry after the options were changed."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
