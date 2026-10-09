"""Sensor platform for the IServ integration.

All sensors belong to exactly one device called ``IServ``:

* ``sensor.iserv_vertretungen``      - cancellations/substitutions of today
* ``sensor.iserv_schulbeginn``       - start time of the next school day
* ``sensor.iserv_stundenplan``       - weekly timetable incl. ``status`` markers
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import IServData, IServDataUpdateCoordinator, IServLesson, IServSchoolStart
from .const import (
    ATTRIBUTION,
    DEVICE_NAME,
    DOMAIN,
    MANUFACTURER,
    MODEL,
    SENSOR_NEXT_SCHOOL_START,
    SENSOR_SUBSTITUTIONS,
    SENSOR_TIMETABLE,
    VERSION,
    WEEKDAYS_DE,
)


def _device_info(entry: ConfigEntry, coordinator: IServDataUpdateCoordinator) -> DeviceInfo:
    """Return the DeviceInfo of the single IServ device.

    Every entity uses the same identifiers, so Home Assistant merges all of
    them into one device named "IServ".
    """
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=DEVICE_NAME,
        manufacturer=MANUFACTURER,
        model=MODEL,
        entry_type=DeviceEntryType.SERVICE,
        configuration_url=coordinator.client.base_url,
        sw_version=VERSION,
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the IServ sensors."""
    coordinator: IServDataUpdateCoordinator = entry.runtime_data
    async_add_entities(
        [
            IServSubstitutionsSensor(coordinator, entry),
            IServNextSchoolStartSensor(coordinator, entry),
            IServTimetableSensor(coordinator, entry),
        ]
    )


class IServBaseSensor(CoordinatorEntity[IServDataUpdateCoordinator], SensorEntity):
    """Common behaviour of all IServ sensors."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_attribution = ATTRIBUTION

    def __init__(
        self,
        coordinator: IServDataUpdateCoordinator,
        entry: ConfigEntry,
        key: str,
    ) -> None:
        """Initialise the sensor with the shared IServ device."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = _device_info(entry, coordinator)
        self._account = coordinator.client.username

    @property
    def data(self) -> IServData:
        """Return the last fetched IServ data."""
        return self.coordinator.data or IServData()

    @property
    def today(self) -> date:
        """Return today's date in the local time zone."""
        return dt_util.now().date()

    def _base_attributes(self) -> dict[str, Any]:
        """Return the attributes that every IServ sensor exposes."""
        return {"account": self._account, "attribution": ATTRIBUTION}


class IServSubstitutionsSensor(IServBaseSensor):
    """Number of cancellations and substitutions of the current day.

    The state counts all changed lessons of today, the attributes contain the
    detailed lists for today and tomorrow (subject, teacher, room, status).
    """

    _attr_icon = "mdi:calendar-alert"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: IServDataUpdateCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, SENSOR_SUBSTITUTIONS)

    @property
    def native_value(self) -> int:
        """Return the number of changes (cancellations + substitutions) today."""
        return len(self.data.changes_on(self.today))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return today's and tomorrow's changes with all details."""
        data = self.data
        today = self.today
        tomorrow = today + timedelta(days=1)
        today_changes = data.changes_on(today)
        tomorrow_changes = data.changes_on(tomorrow)

        attributes: dict[str, Any] = {
            "date": today.isoformat(),
            "day": WEEKDAYS_DE[today.weekday()],
            "count_today": len(today_changes),
            "count_tomorrow": len(tomorrow_changes),
            "cancellations": [
                lesson.as_dict() for lesson in today_changes if lesson.canceled
            ],
            "substitutions": [
                lesson.as_dict() for lesson in today_changes if not lesson.canceled
            ],
            "changes_tomorrow": [lesson.as_dict() for lesson in tomorrow_changes],
            "lessons_today": len(data.lessons_on(today)),
            "has_changes": bool(today_changes),
            "has_changes_tomorrow": bool(tomorrow_changes),
        }
        attributes.update(self._base_attributes())
        return attributes


class IServNextSchoolStartSensor(IServBaseSensor):
    """Time when the first lesson of the next school day begins.

    The state is a plain ``HH:MM`` string (e.g. ``08:10``) and can therefore be
    used directly in automations. Cancelled lessons are skipped: if period 1 is
    cancelled the state becomes ``09:00``, if period 1 and 2 are cancelled it
    becomes ``10:00`` and so on. Whenever IServ does not deliver the real times
    the fallback raster (08:10, 09:00, 10:00, 10:45, 11:45, 12:35) is used.
    """

    _attr_icon = "mdi:alarm"

    def __init__(self, coordinator: IServDataUpdateCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, SENSOR_NEXT_SCHOOL_START)

    @property
    def school_start(self) -> IServSchoolStart | None:
        """Return the upcoming school start."""
        return self.data.next_school_start(dt_util.now())

    @property
    def native_value(self) -> str | None:
        """Return ``HH:MM`` of the first (not cancelled) lesson."""
        start = self.school_start
        return start.start_time if start is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return date, timestamp and the skipped lessons."""
        start = self.school_start
        attributes: dict[str, Any] = {}
        if start is not None:
            moment = dt_util.start_of_local_day(start.date).replace(
                hour=start.hour, minute=start.minute
            )
            attributes.update(
                {
                    "timestamp": moment.isoformat(),
                    "date": start.date.isoformat(),
                    "day": start.day,
                    "weekday": start.date.weekday(),
                    "period": start.period,
                    "time_source": start.time_source,
                    "is_today": start.is_today,
                    "skipped_lessons": [lesson.as_dict() for lesson in start.skipped],
                }
            )
        attributes.update(self._base_attributes())
        return attributes


class IServTimetableSensor(IServBaseSensor):
    """Weekly timetable with cancellations and substitutions.

    The state counts the lessons that really take place on the day the sensor
    currently points to: today while the school day has lessons, otherwise the
    next school day. That way the sensor keeps showing a useful value at the
    weekend or during the holidays. The attributes contain the complete fetched
    timetable (current + two following weeks) grouped by day. Every lesson
    carries a ``status`` field (``regular`` / ``substituted`` / ``cancelled``)
    so that Lovelace cards can mark cancelled lessons in red or strike them
    through.
    """

    _attr_icon = "mdi:timetable"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _unrecorded_attributes = frozenset(
        {"days", "today", "tomorrow", "lessons", "cancelled", "substituted"}
    )

    def __init__(self, coordinator: IServDataUpdateCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, SENSOR_TIMETABLE)

    @property
    def display_day(self) -> date | None:
        """Return the day the state refers to (today, else the next school day)."""
        today = self.today
        if self.data.lessons_on(today):
            return today
        next_start = self.data.next_school_start(dt_util.now())
        return next_start.date if next_start is not None else None

    @property
    def next_lesson(self) -> IServLesson | None:
        """Return the next lesson that has not started yet."""
        return self.data.next_lesson(dt_util.now())

    @property
    def native_value(self) -> int:
        """Return the number of lessons that take place on the displayed day."""
        day = self.display_day
        if day is None:
            return 0
        return len([lesson for lesson in self.data.lessons_on(day) if not lesson.canceled])

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the whole timetable grouped by day."""
        data = self.data
        today = self.today
        day = self.display_day
        next_lesson = self.next_lesson
        days: dict[str, Any] = {}
        for lesson in data.sorted_lessons():
            key = lesson.date.isoformat()
            bucket = days.setdefault(
                key,
                {
                    "date": key,
                    "day": lesson.day,
                    "weekday": lesson.weekday,
                    "lessons": [],
                },
            )
            bucket["lessons"].append(lesson.as_dict())

        next_start = data.next_school_start(dt_util.now())
        attributes: dict[str, Any] = {
            "days": days,
            "school_days": [school_day.isoformat() for school_day in data.school_days],
            "today": [lesson.as_dict() for lesson in data.lessons_on(today)],
            "tomorrow": [
                lesson.as_dict() for lesson in data.lessons_on(today + timedelta(days=1))
            ],
            "date": day.isoformat() if day else None,
            "day": WEEKDAYS_DE[day.weekday()] if day else None,
            "weekday": day.weekday() if day else None,
            "is_today": day == today,
            "lessons": [lesson.as_dict() for lesson in data.lessons_on(day)] if day else [],
            "next_lesson": next_lesson.as_dict() if next_lesson is not None else None,
            "cancelled": [
                lesson.as_dict() for lesson in data.sorted_lessons() if lesson.canceled
            ],
            "substituted": [
                lesson.as_dict()
                for lesson in data.sorted_lessons()
                if lesson.substitution and not lesson.canceled
            ],
            "next_school_day": next_start.date.isoformat() if next_start else None,
            "next_school_start": next_start.start_time if next_start else None,
            "lessons_total": len(data.lessons),
        }
        attributes.update(self._base_attributes())
        return attributes

