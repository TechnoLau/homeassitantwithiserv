"""Constants for the IServ integration."""

from __future__ import annotations

from typing import Final

# --- Integration metadata -------------------------------------------------
DOMAIN: Final = "iserv"
VERSION: Final = "1.0.2"

# Exactly one Home Assistant device holds every IServ entity.
DEVICE_NAME: Final = "IServ"
MANUFACTURER: Final = "IServ GmbH"
MODEL: Final = "Schulplattform"
ATTRIBUTION: Final = "Daten bereitgestellt von IServ"

# --- Config entry keys ----------------------------------------------------
CONF_HOST: Final = "host"
CONF_USERNAME: Final = "username"
CONF_PASSWORD: Final = "password"
CONF_SCAN_INTERVAL: Final = "scan_interval"

# --- Defaults -------------------------------------------------------------
DEFAULT_SCAN_INTERVAL_MINUTES: Final = 30
MIN_SCAN_INTERVAL_MINUTES: Final = 5
MAX_SCAN_INTERVAL_MINUTES: Final = 1440

# --- Timeouts (seconds) ---------------------------------------------------
CONNECTION_TIMEOUT: Final = 10
REQUEST_TIMEOUT: Final = 30

# Maximum number of meta-refresh redirects that are followed during login.
MAX_LOGIN_REDIRECTS: Final = 3

# How many days ahead the "next school start" sensor looks for lessons.
MAX_LOOKAHEAD_DAYS: Final = 21

# --- IServ endpoint paths -------------------------------------------------
# Pages that are tried in order while searching the login form. IServ ships
# several generations; some installations only answer on /iserv/login.
LOGIN_PATHS: Final = ("/iserv/app/login", "/iserv/login", "/iserv/")
# Newer "DieSchulApp" JSON API (returns timetable + substitutions).
CURRENT_TIMETABLE_PATH: Final = "/iserv/dieschulapp/api/1.0/current-timetable/"
# Older JSON API used as a fallback.
TIMETABLE_DATA_PATH: Final = "/iserv/timetable/data"
# Oldest IServ generation: the raw export of the plan module (a plain JSON
# lesson list with ``day``/``start_time``/``end_time``). Used as the last
# fallback when neither the DieSchulApp nor the timetable data API answers.
TIMETABLE_RAW_PATH: Final = "/iserv/plan/show/raw"

# --- Fetch window ---------------------------------------------------------
# Week offsets that are fetched on every update (0 = current week). The two
# following weeks make sure the "next school day" sensors still work on
# Friday/Saturday/Sunday and when the coming week is empty (holidays) or not
# published by the school yet.
WEEK_OFFSETS: Final = (0, 1, 2)

# --- Sensors (unique id suffix == translation key) ------------------------
SENSOR_SUBSTITUTIONS: Final = "substitutions"
SENSOR_NEXT_SCHOOL_START: Final = "next_school_start"
SENSOR_TIMETABLE: Final = "timetable"

# --- Lesson status markers (exposed in the sensor attributes) -------------
STATUS_REGULAR: Final = "regular"
STATUS_SUBSTITUTED: Final = "substituted"
STATUS_CANCELLED: Final = "cancelled"

# Where the start time of a lesson comes from.
TIME_SOURCE_ISERV: Final = "iserv"
TIME_SOURCE_FALLBACK: Final = "fallback"
TIME_SOURCE_UNKNOWN: Final = "unknown"

# --- Weekdays -------------------------------------------------------------
# Index 0 = Monday ... index 6 = Sunday (matches date.weekday()).
WEEKDAYS_DE: Final = (
    "Montag",
    "Dienstag",
    "Mittwoch",
    "Donnerstag",
    "Freitag",
    "Samstag",
    "Sonntag",
)

# --- Fallback period grid (1.-6. Stunde) ----------------------------------
# Used whenever IServ does not deliver concrete start/end times. Only the
# start times are part of the official raster; the end times are derived from
# the usual 45 minute lesson length.
FALLBACK_PERIOD_TIMES: Final[dict[int, tuple[str, str]]] = {
    1: ("08:10", "08:55"),
    2: ("09:00", "09:45"),
    3: ("10:00", "10:45"),
    4: ("10:45", "11:30"),
    5: ("11:45", "12:30"),
    6: ("12:35", "13:20"),
}

# --- Cancellation detection -----------------------------------------------
# Marks a cancelled lesson inside the IServ ``change_types`` list.
CANCEL_CHANGE_TYPE: Final = "0"
# Substitution type values that indicate a cancelled (entfallene) lesson.
CANCEL_SUBSTITUTION_TYPES: Final = frozenset(
    {"canceled", "cancelled", "entfall", "ausfall", "absage"}
)
