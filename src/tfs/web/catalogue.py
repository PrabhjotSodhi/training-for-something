"""The cards, trend metrics and plate rows the Overview can show."""

CARD_TYPES = ("week", "sleep", "trend")
TREND_METRICS = ("resting_hr", "hrv_sdnn", "weight", "vo2_max", "training_load")
PLATE_ROWS = (
    "readiness",
    "sleep_asleep",
    "resting_hr",
    "hrv_sdnn",
    "load_cardio",
    "load_strength",
    "load_effort",
)
SPORT_UNITS = {"run": "km", "ride": "km", "swim": "m", "strength": "sets"}
LOAD_SCORES = ("load_cardio", "load_strength", "load_effort")
DEFAULT_CARDS = (
    {"type": "week"},
    {"type": "trend", "metric": "training_load"},
    {"type": "sleep"},
    {"type": "trend", "metric": "resting_hr"},
    {"type": "trend", "metric": "hrv_sdnn"},
)
