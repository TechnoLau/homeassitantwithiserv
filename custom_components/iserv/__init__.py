"""The IServ integration.

This module bundles the low level IServ HTTP client, the
:class:`DataUpdateCoordinator` and the config entry setup/unload handlers so
the integration stays compact while still separating responsibilities into
clearly delimited sections.
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
from urllib.parse import urljoin, urlparse, urlunparse

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
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
    DOMAIN,
    LOGIN_PATH,
    MAX_LOGIN_REDIRECTS,
    REQUEST_TIMEOUT,
    TIMETABLE_DATA_PATH,
    WEEKDAYS_DE,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]


# =========================================================================
# Exceptions
# =========================================================================
class IServError(Exception):
    """Base exception for the IServ integration."""


class IServAuthError(IServError):
    """Raised when IServ rejects the credentials or the session expired."""


class IServConnectionError(IServError):
    """Raised when the IServ server cannot be reached or answered oddly."""


class _EndpointUnavailable(IServError):
    """Raised when a specific IServ generation does not expose an endpoint."""


# =========================================================================
# URL / date / HTML helper functions
# =========================================================================
def normalize_host(host: str) -> str:
    """Return a clean ``https://<host>`` base URL without a trailing slash.

    Accepts inputs such as ``schule.iserv.de``, ``https://schule.iserv.de/`` or
    ``https://schule.iserv.de/iserv`` and normalises them to a usable base URL.
    """
    candidate = (host or "").strip().rstrip("/")
    if not candidate:
        raise ValueError("empty host")
    if not candidate.startswith(("http://", "https://")):
        candidate = f"https://{candidate}"

    parsed = urlparse(candidate)
    if not parsed.hostname or "." not in parsed.hostname:
        if parsed.hostname != "localhost":
            raise ValueError("invalid host")

    path = parsed.path.rstrip("/")
    # Users sometimes paste the full IServ path - strip it again.
    if path == "/iserv":
        path = ""
    return urlunparse(
        parsed._replace(path=path, params="", query="", fragment="")
    ).rstrip("/")


def _week_monday(week_offset: int = 0) -> date:
    """Return the Monday of the current week plus ``week_offset`` weeks."""
    today = date.today()
    return today - timedelta(days=today.weekday()) + timedelta(weeks=week_offset)


def _week_filter(monday: date) -> dict[str, Any]:
    """Build the filter payload expected by ``/iserv/timetable/data``."""
    return {
        "startDate": monday.strftime("%d.%m.%Y"),
        "endDate": (monday + timedelta(days=6)).strftime("%d.%m.%Y"),
        "changesUntil": None,
        "classes": ["%"],
        "teachers": ["%"],
        "rooms": ["%"],
    }


_HIDDEN_INPUT_RE = re.compile(r"<input\b[^>]*type=[\"']hidden[\"'][^>]*>", re.I)
_NAME_ATTR_RE = re.compile(r"name=[\"']([^\"']+)[\"']", re.I)
_VALUE_ATTR_RE = re.compile(r"value=[\"']([^\"']*)[\"']", re.I)
_META_REFRESH_RE = re.compile(
    r"<meta[^>]+http-equiv=[\"']?refresh[\"']?[^>]+content=[\"'][^;]+;\s*url=([^\"'>\s]+)",
    re.I,
)
_CLOCK_RE = re.compile(r"(?:T|\s|^)(\d{2}:\d{2})")


def _extract_hidden_inputs(html: str) -> dict[str, str]:
    """Return all hidden form fields (e.g. ``_csrf``) from a login page."""
    fields: dict[str, str] = {}
    for tag in _HIDDEN_INPUT_RE.findall(html):
        name_match = _NAME_ATTR_RE.search(tag)
        if name_match is None:
            continue
        value_match = _VALUE_ATTR_RE.search(tag)
        value = value_match.group(1) if value_match else ""
        fields[name_match.group(1)] = unescape(value)
    return fields


def _meta_refresh_url(html: str, base_url: str) -> str | None:
    """Extract and resolve a ``<meta http-equiv="refresh">`` target URL."""
    match = _META_REFRESH_RE.search(html)
    if match is None:
        return None
    return urljoin(base_url, unescape(match.group(1)).strip("\"' "))


def _is_login_form(html: str) -> bool:
    """Return whether the response body is the IServ login form."""
    body = html.casefold()
    markers = (
        'name="_username"',
        'name="_password"',
        'name="username"',
        'name="password"',
        'id="loginbutton"',
        "login-form",
    )
    return sum(marker in body for marker in markers) >= 2


def _clock_time(value: str) -> str:
    """Extract ``HH:MM`` from an IServ time or ISO date-time string."""
    match = _CLOCK_RE.search(value)
    return match.group(1) if match else value.strip()[:5]


def _first_string(mapping: dict[str, Any], *keys: str) -> str | None:
    """Return the first non-empty string among ``keys``."""
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _display_value(value: Any) -> str:
    """Extract a readable name or acronym from an IServ value object."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("name", "displayName", "acronym", "shortName", "label"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return ""


# =========================================================================
# Data model
# =========================================================================
@dataclass(slots=True)
class IServLesson:
    """A single lesson of the IServ timetable."""

    weekday: int  # 0 = Monday ... 6 = Sunday
    start_time: str  # "HH:MM"
    end_time: str  # "HH:MM"
    subject: str = ""
    room: str = ""
    teacher: str = ""
    canceled: bool = False
    substitution: bool = False
    note: str = ""

    @property
    def day(self) -> str:
        """Return the localised weekday name (Monday first)."""
        if 0 <= self.weekday < len(WEEKDAYS_DE):
            return WEEKDAYS_DE[self.weekday]
        return ""

    def as_dict(self) -> dict[str, Any]:
        """Return the lesson as a Home Assistant friendly dictionary."""
        return {
            "day": self.day,
            "weekday": self.weekday,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "subject": self.subject,
            "room": self.room,
            "teacher": self.teacher,
            "canceled": self.canceled,
            "substitution": self.substitution,
            "note": self.note,
        }


# =========================================================================
# Response parsers
# =========================================================================
def _loads(body: str) -> Any:
    """Parse JSON, raising a connection error on malformed data."""
    try:
        return json.loads(body)
    except (json.JSONDecodeError, TypeError) as err:
        raise IServConnectionError("IServ returned an invalid JSON response") from err


def _course_subject(value: Any) -> str:
    """Return the best display name for a course-subject object."""
    if isinstance(value, dict):
        return _display_value(value.get("subject")) or _display_value(
            value.get("acronym")
        )
    return _display_value(value)


def _substitution_note(substitution_type: Any) -> str:
    """Return a human readable note from a substitution type value."""
    if isinstance(substitution_type, str):
        return substitution_type.strip()
    if isinstance(substitution_type, dict):
        for key in ("name", "displayName", "label", "type"):
            candidate = substitution_type.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return _display_value(substitution_type)


def _is_canceled_substitution(substitution_type: Any) -> bool:
    """Return whether a substitution explicitly cancels a lesson."""
    if isinstance(substitution_type, str):
        values = [substitution_type]
    elif isinstance(substitution_type, dict):
        values = [str(item) for item in substitution_type.values()]
    else:
        return False
    return any(value.casefold() in CANCEL_SUBSTITUTION_TYPES for value in values)


def _change_is_canceled(change: Any) -> bool:
    """Return whether an IServ ``change`` object marks a cancellation."""
    if not isinstance(change, dict):
        return False
    change_types = change.get("change_types")
    if isinstance(change_types, dict):
        change_types = list(change_types.keys())
    if isinstance(change_types, list):
        return CANCEL_CHANGE_TYPE in {str(item) for item in change_types}
    return False


def _entry_weekday(entry: dict[str, Any]) -> int | None:
    """Determine the weekday index (0 = Monday) of a timetable entry."""
    weekday = entry.get("weekday")
    if isinstance(weekday, int) and 0 <= weekday <= 6:
        return weekday
    value = _first_string(entry, "date", "day", "lessonDate")
    if value:
        for date_format in ("%d.%m.%Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(value[:10], date_format).weekday()
            except ValueError:
                continue
    return None


def _period_times(payload: dict[str, Any]) -> dict[str, tuple[str, str]]:
    """Collect period start/end times from known timetable metadata shapes."""
    result: dict[str, tuple[str, str]] = {}
    for container in (payload.get("meta"), payload.get("data")):
        if not isinstance(container, dict):
            continue
        for key in ("lessonTimes", "periods", "times"):
            values = container.get(key)
            if isinstance(values, dict):
                values = [
                    dict(value, period=period)
                    for period, value in values.items()
                    if isinstance(value, dict)
                ]
            if not isinstance(values, list):
                continue
            for value in values:
                if not isinstance(value, dict):
                    continue
                period = value.get("period", value.get("number", value.get("id")))
                start = _first_string(value, "start_time", "startTime", "start")
                end = _first_string(value, "end_time", "endTime", "end")
                if period is not None and start and end:
                    result[str(period)] = (_clock_time(start), _clock_time(end))
    return result


def _parse_current_timetable(payload: Any) -> list[IServLesson]:
    """Parse the DieSchulApp ``/current-timetable`` JSON response."""
    if not isinstance(payload, dict):
        raise _EndpointUnavailable

    entries: list[Any] = []
    if not isinstance(payload.get("entries"), list) and not isinstance(
        payload.get("students"), list
    ):
        raise _EndpointUnavailable
    if isinstance(payload.get("entries"), list):
        entries.extend(payload["entries"])
    students = payload.get("students")
    if isinstance(students, list):
        for student in students:
            if isinstance(student, dict) and isinstance(student.get("entries"), list):
                entries.extend(student["entries"])

    lessons: list[IServLesson] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        slot = entry.get("timeTableSlot")
        if not isinstance(slot, dict):
            continue
        start = _first_string(slot, "start", "startTime", "start_time")
        end = _first_string(slot, "end", "endTime", "end_time")
        weekday = entry.get("weekday")
        if (
            not start
            or not end
            or not isinstance(weekday, int)
            or not 0 <= weekday <= 6
        ):
            continue

        change = entry.get("change")
        substitution_type = entry.get("substitutionType")
        subject = _course_subject(entry.get("courseSubject"))
        room = _display_value(entry.get("room"))
        if isinstance(change, dict):
            subject = _first_string(change, "substitutionSubject") or subject
            room = _first_string(change, "substitutionRoom") or room

        lessons.append(
            IServLesson(
                weekday=weekday,
                start_time=_clock_time(start),
                end_time=_clock_time(end),
                subject=subject,
                room=room,
                teacher=_display_value(entry.get("teacher"))
                or _display_value(entry.get("courseTeacher")),
                canceled=_is_canceled_substitution(substitution_type)
                or _change_is_canceled(change),
                substitution=substitution_type is not None
                or isinstance(change, dict),
                note=_substitution_note(substitution_type)
                or _first_string(entry, "note", "comment")
                or "",
            )
        )
    return lessons


def _parse_timetable_data(payload: Any) -> list[IServLesson]:
    """Parse the legacy ``/iserv/timetable/data`` JSON response."""
    data = payload.get("data") if isinstance(payload, dict) else None
    entries = data.get("timetable") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise _EndpointUnavailable

    period_times = _period_times(payload)
    lessons: list[IServLesson] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        start = _first_string(entry, "start_time", "startTime", "start")
        end = _first_string(entry, "end_time", "endTime", "end")
        if (not start or not end) and entry.get("period") is not None:
            start, end = period_times.get(str(entry["period"]), (start, end))
        weekday = _entry_weekday(entry)
        if not start or not end or weekday is None:
            continue

        change = entry.get("change")
        subject = _first_string(entry, "subject") or ""
        room = _display_value(entry.get("room"))
        if isinstance(change, dict):
            subject = _first_string(change, "substitutionSubject") or subject
            room = _first_string(change, "substitutionRoom") or room

        lessons.append(
            IServLesson(
                weekday=weekday,
                start_time=_clock_time(start),
                end_time=_clock_time(end),
                subject=subject,
                room=room,
                teacher=_display_value(entry.get("teacher")),
                canceled=_change_is_canceled(change),
                substitution=isinstance(change, dict),
                note=(
                    _first_string(change, "comment", "note")
                    if isinstance(change, dict)
                    else None
                )
                or "",
            )
        )
    return lessons


# =========================================================================
# API client
# =========================================================================
class IServApiClient:
    """Small asynchronous HTTP client for the IServ web interface."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        username: str,
        password: str,
    ) -> None:
        """Initialise the client (does not log in yet)."""
        self._session = session
        self._base_url = normalize_host(host)
        self._username = username
        self._password = password
        self._logged_in = False

    @property
    def base_url(self) -> str:
        """Return the normalised base URL of the IServ server."""
        return self._base_url

    @property
    def username(self) -> str:
        """Return the configured IServ username."""
        return self._username

    async def async_login(self) -> None:
        """Authenticate against IServ and keep the session cookie."""
        login_url, login_page = await self._async_discover_login_url()
        payload: dict[str, str] = {
            "_username": self._username,
            "_password": self._password,
        }
        # Only inject hidden fields (e.g. CSRF tokens) that are not credentials.
        for name, value in _extract_hidden_inputs(login_page).items():
            payload.setdefault(name, value)
        await self._async_submit_login(login_url, payload)

    async def _async_discover_login_url(self) -> tuple[str, str]:
        """Return the actual login URL and the HTML body of the login form."""
        fallback_url = f"{self._base_url}{LOGIN_PATH}"
        try:
            url = f"{self._base_url}{APP_LOGIN_PATH}"
            for _ in range(MAX_LOGIN_REDIRECTS):
                body, final_url = await self._async_get_page(
                    url, check_login=False, unavailable_statuses=()
                )
                if _is_login_form(body):
                    return final_url, body
                refresh_url = _meta_refresh_url(body, final_url)
                if refresh_url is None or refresh_url == url:
                    break
                url = refresh_url
        except IServError as err:
            _LOGGER.debug("IServ app login discovery failed: %s", err)

        # Legacy IServ installations only expose the plain login endpoint.
        try:
            body, final_url = await self._async_get_page(
                fallback_url, check_login=False, unavailable_statuses=()
            )
            if _is_login_form(body):
                return final_url, body
        except IServError as err:
            _LOGGER.debug("IServ default login page unavailable: %s", err)
        return fallback_url, ""

    async def _async_get_page(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        check_login: bool = True,
        unavailable_statuses: tuple[int, ...] = (404,),
    ) -> tuple[str, str]:
        """GET ``url`` and return ``(body, final_url)``."""
        timeout = aiohttp.ClientTimeout(
            total=REQUEST_TIMEOUT, connect=CONNECTION_TIMEOUT
        )
        try:
            async with self._session.get(
                url, params=params, timeout=timeout, allow_redirects=True
            ) as response:
                if response.status in unavailable_statuses:
                    raise _EndpointUnavailable(f"HTTP {response.status} for {url}")
                if response.status in (401, 403):
                    self._logged_in = False
                    raise IServAuthError(f"HTTP {response.status} for {url}")
                response.raise_for_status()
                body = await response.text()
                final_url = str(response.url)
        except (IServAuthError, _EndpointUnavailable):
            raise
        except asyncio.TimeoutError as err:
            raise IServConnectionError(f"Timeout while requesting {url}") from err
        except aiohttp.ClientError as err:
            raise IServConnectionError(f"Error requesting {url}: {err}") from err

        if check_login and _is_login_form(body):
            self._logged_in = False
            raise IServAuthError("IServ returned the login page")
        return body, final_url

    async def _async_submit_login(
        self, url: str, payload: dict[str, str]
    ) -> None:
        """POST the credentials and verify that a session was established."""
        timeout = aiohttp.ClientTimeout(
            total=REQUEST_TIMEOUT, connect=CONNECTION_TIMEOUT
        )
        try:
            async with self._session.post(
                url, data=payload, timeout=timeout, allow_redirects=True
            ) as response:
                if response.status in (401, 403):
                    raise IServAuthError("IServ rejected the credentials")
                response.raise_for_status()
                final_url = str(response.url)
                body = await response.text()
        except IServAuthError:
            raise
        except asyncio.TimeoutError as err:
            raise IServConnectionError(f"Timeout while logging in: {err}") from err
        except aiohttp.ClientError as err:
            raise IServConnectionError(f"Error while logging in: {err}") from err

        # Some IServ versions finish the login with a meta refresh redirect.
        for _ in range(MAX_LOGIN_REDIRECTS):
            refresh_url = _meta_refresh_url(body, final_url)
            if refresh_url is None or refresh_url == final_url:
                break
            body, final_url = await self._async_get_page(
                refresh_url, check_login=False, unavailable_statuses=()
            )

        if _is_login_form(body):
            self._logged_in = False
            raise IServAuthError("Invalid IServ credentials")
        self._logged_in = True

    async def async_get_lessons(self, week_offset: int = 0) -> list[IServLesson]:
        """Return all lessons of the requested week (0 = current week)."""
        if not self._logged_in:
            await self.async_login()
        try:
            return await self._async_fetch_lessons(week_offset)
        except IServAuthError:
            # The session expired - log in again and retry exactly once.
            self._logged_in = False
            await self.async_login()
            return await self._async_fetch_lessons(week_offset)

    async def _async_fetch_lessons(self, week_offset: int) -> list[IServLesson]:
        """Fetch lessons, preferring the modern API and falling back."""
        monday = _week_monday(week_offset)
        try:
            body, _ = await self._async_get_page(
                f"{self._base_url}{CURRENT_TIMETABLE_PATH}",
                params={
                    "date": monday.isoformat(),
                    "week": "true",
                    "substitutions": "true",
                },
                unavailable_statuses=(403, 404),
            )
            return _parse_current_timetable(_loads(body))
        except (_EndpointUnavailable, IServConnectionError) as err:
            _LOGGER.debug(
                "DieSchulApp timetable endpoint unusable (%s); using fallback", err
            )

        body, _ = await self._async_get_page(
            f"{self._base_url}{TIMETABLE_DATA_PATH}",
            params={
                "filter": json.dumps(_week_filter(monday), separators=(",", ":"))
            },
            unavailable_statuses=(404,),
        )
        return _parse_timetable_data(_loads(body))


# =========================================================================
# Coordinator
# =========================================================================
@dataclass(slots=True)
class IServData:
    """Data returned by a single coordinator refresh."""

    lessons: list[IServLesson] = field(default_factory=list)

    @property
    def substitutions(self) -> list[IServLesson]:
        """Return every lesson that differs from the regular timetable."""
        return [lesson for lesson in self.lessons if lesson.substitution]

    @property
    def canceled_lessons(self) -> list[IServLesson]:
        """Return every cancelled lesson of the fetched week."""
        return [lesson for lesson in self.lessons if lesson.canceled]

    @property
    def todays_lessons(self) -> list[IServLesson]:
        """Return the lessons of today (empty on weekends)."""
        weekday = date.today().weekday()
        return [lesson for lesson in self.lessons if lesson.weekday == weekday]


class IServDataUpdateCoordinator(DataUpdateCoordinator[IServData]):
    """Poll the IServ timetable and substitution plan."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: IServApiClient,
        scan_interval_minutes: int,
    ) -> None:
        """Initialise the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} ({client.username})",
            update_interval=timedelta(minutes=scan_interval_minutes),
        )
        self.client = client

    async def _async_update_data(self) -> IServData:
        """Fetch the timetable of the current week."""
        try:
            lessons = await self.client.async_get_lessons(0)
        except IServAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except IServConnectionError as err:
            raise UpdateFailed(str(err)) from err
        return IServData(lessons=lessons)


# =========================================================================
# Config entry setup / unload
# =========================================================================
# ``ConfigEntry`` became generic in Home Assistant 2024.6 - fall back gracefully
# so that the integration still imports (and runs) on older versions.
try:
    IServConfigEntry = ConfigEntry[IServDataUpdateCoordinator]
except TypeError:  # pragma: no cover - Home Assistant < 2024.6
    IServConfigEntry = ConfigEntry  # type: ignore[misc, assignment]


async def async_setup_entry(hass: HomeAssistant, entry: IServConfigEntry) -> bool:
    """Set up the IServ integration from a config entry."""
    client = IServApiClient(
        async_get_clientsession(hass),
        entry.data[CONF_HOST],
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
    )
    try:
        await client.async_login()
    except IServAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except IServConnectionError as err:
        raise ConfigEntryNotReady(str(err)) from err

    coordinator = IServDataUpdateCoordinator(
        hass,
        client,
        entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_MINUTES),
    )
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: IServConfigEntry) -> bool:
    """Unload an IServ config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry whenever its options change."""
    await hass.config_entries.async_reload(entry.entry_id)
