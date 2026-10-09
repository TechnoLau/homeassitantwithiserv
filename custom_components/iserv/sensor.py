"""Sensor platform for the IServ integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import IServConfigEntry, IServData, IServDataUpdateCoordinator, IServLesson
from .const import (
    ATTRIBUTION,
    DOMAIN,
    MANUFACTURER,
    MODEL,
    SENSOR_SUBSTITUTIONS,
    SENSOR_TIMETABLE,
)

SENSOR_DESCRIPTIONS: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(
        key=SENSOR_TIMETABLE,
        translation_key=SENSOR_TIMETABLE,
        icon="mdi:calendar-clock",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key=SENSOR_SUBSTITUTIONS,
        translation_key=SENSOR_SUBSTITUTIONS,
        icon="mdi:calendar-remove",
        state_class=SensorStateClass.MEASUREMENT,
    ),
)


def _next_lesson(data: IServData) -> IServLesson | None:
    """Return the next lesson of today that has not finished yet."""
    now = dt_util.now().strftime("%H:%M")
    upcoming = [
        lesson
        for lesson in data.todays_lessons
        if lesson.end_time and lesson.end_time >= now
    ]
    upcoming.sort(key=lambda lesson: lesson.start_time)
    return upcoming[0] if upcoming else None


class IServBaseSensor(CoordinatorEntity[IServDataUpdateCoordinator], SensorEntity):
    """Base class for the IServ sensors."""

    _attr_has_entity_name = True
    _attr_attribution = ATTRIBUTION

    def __init__(
        self,
        coordinator: IServDataUpdateCoordinator,
        entry: IServConfigEntry,
        description: SensorEntityDescription,
    ) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=f"IServ ({coordinator.client.username})",
            manufacturer=MANUFACTURER,
            model=MODEL,
            entry_type=DeviceEntryType.SERVICE,
            configuration_url=coordinator.client.base_url,
        )

    @property
    def _lessons(self) -> list[IServLesson]:
        """Return all lessons of the fetched week."""
        return self.coordinator.data.lessons


class IServTimetableSensor(IServBaseSensor):
    """Sensor with the number of lessons of the current day."""

    @property
    def native_value(self) -> int:
        """Return the number of lessons today."""
        return len(self.coordinator.data.todays_lessons)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the full timetable as attributes."""
        next_lesson = _next_lesson(self.coordinator.data)
        return {
            "lessons": [lesson.as_dict() for lesson in self._lessons],
            "today": [lesson.as_dict() for lesson in self.coordinator.data.todays_lessons],
            "next_lesson": next_lesson.as_dict() if next_lesson else None,
        }


class IServSubstitutionsSensor(IServBaseSensor):
    """Sensor with the number of cancellations and substitutions."""

    @property
    def _relevant(self) -> list[IServLesson]:
        """Return the lessons from today until the end of the fetched week."""
        today = dt_util.now().weekday()
        return [lesson for lesson in self._lessons if lesson.weekday >= today]

    @property
    def native_value(self) -> int:
        """Return the number of changed lessons from today onwards."""
        return len(
            [
                lesson
                for lesson in self._relevant
                if lesson.canceled or lesson.substitution
            ]
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the substitution plan details as attributes."""
        relevant = self._relevant
        cancellations = [lesson for lesson in relevant if lesson.canceled]
        substitutions = [
            lesson for lesson in relevant if lesson.substitution and not lesson.canceled
        ]
        today = dt_util.now().weekday()
        return {
            "cancellations": [lesson.as_dict() for lesson in cancellations],
            "substitutions": [lesson.as_dict() for lesson in substitutions],
            "today": [
                lesson.as_dict()
                for lesson in relevant
                if lesson.weekday == today and (lesson.canceled or lesson.substitution)
            ],
            "has_changes": bool(cancellations or substitutions),
        }


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IServConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the IServ sensors from a config entry."""
    coordinator = entry.runtime_data
    async_add_entities(
        [
            IServTimetableSensor(coordinator, entry, SENSOR_DESCRIPTIONS[0]),
            IServSubstitutionsSensor(coordinator, entry, SENSOR_DESCRIPTIONS[1]),
        ]
    )