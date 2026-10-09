"""Constants for the IServ integration."""

from __future__ import annotations

from typing import Final

# --- Integration metadata -------------------------------------------------
DOMAIN: Final = "iserv"
NAME: Final = "IServ"
MANUFACTURER: Final = "IServ GmbH"
MODEL: Final = "Schulplattform"
VERSION: Final = "1.0.0"
ATTRIBUTION: Final = "Daten bereitgestellt von IServ"

# --- Config entry keys ----------------------------------------------------
CONF_HOST: Final = "host"
CONF_USERNAME: Final = "username"
CONF_PASSWORD: Final = "password"
CONF_SCAN_INTERVAL: Final = "scan_interval"

# --- Defaults -------------------------------------------------------------
# The timetable / substitution plan rarely changes, so 30 minutes is plenty.
DEFAULT_SCAN_INTERVAL_MINUTES: Final = 30
MIN_SCAN_INTERVAL_MINUTES: Final = 5
MAX_SCAN_INTERVAL_MINUTES: Final = 1440

# --- Timeouts (seconds) ---------------------------------------------------
CONNECTION_TIMEOUT: Final = 10
REQUEST_TIMEOUT: Final = 30

# Maximum number of meta-refresh redirects that are followed during login.
MAX_LOGIN_REDIRECTS: Final = 3

# --- IServ endpoint paths -------------------------------------------------
# Login form discovery (returns the app specific login form via meta refresh).
APP_LOGIN_PATH: Final = "/iserv/app/login"
# Form POST target for the credentials.
LOGIN_PATH: Final = "/iserv/auth/login"
# Newer "DieSchulApp" JSON API (returns timetable + substitutions).
CURRENT_TIMETABLE_PATH: Final = "/iserv/dieschulapp/api/1.0/current-timetable/"
# Older JSON API used as a fallback.
TIMETABLE_DATA_PATH: Final = "/iserv/timetable/data"

# --- Sensor keys ----------------------------------------------------------
SENSOR_TIMETABLE: Final = "timetable"
SENSOR_SUBSTITUTIONS: Final = "substitutions"

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
WEEKDAY_ORDER: Final = {name: index for index, name in enumerate(WEEKDAYS_DE)}

# Marks a canceled lesson inside the IServ ``change_types`` list.
CANCEL_CHANGE_TYPE: Final = "0"
# Substitution type values that indicate a canceled (entfallene) lesson.
CANCEL_SUBSTITUTION_TYPES: Final = frozenset(
    {"canceled", "cancelled", "entfall", "ausfall"}
)
