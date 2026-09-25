"""Constants for the Water Leak Detector integration."""

DOMAIN = "water_leak"

PLATFORMS = ["binary_sensor", "sensor", "switch"]

CONF_WATER_METER = "water_meter"
CONF_QUIET_MIN = "quiet_min"
CONF_LIMIT_MIN = "limit_min"
CONF_NOTIFY_SERVICE = "notify_service"

DEFAULT_QUIET_MIN = 45
DEFAULT_LIMIT_MIN = 120
DEFAULT_NOTIFY_SERVICE = "notify.telegram"

STORAGE_VERSION = 1
STORAGE_KEY = "water_leak"

QUIET_MIN_MIN = 5
QUIET_MIN_MAX = 240
LIMIT_MIN_MIN = 15
LIMIT_MIN_MAX = 1440

EVENT_LEAK_DETECTED = "water_leak_detected"
EVENT_LEAK_RESOLVED = "water_leak_resolved"