"""The cards, trend metrics and plate rows the Overview can show."""

CARD_TYPES = ("week", "sleep", "trend", "sets_by_muscle", "lift")
TREND_METRICS = ("resting_hr", "hrv_sdnn", "weight", "vo2_max", "training_load")
TREND_TITLES = {
    "resting_hr": "Resting heart rate",
    "hrv_sdnn": "HRV",
    "weight": "Weight",
    "vo2_max": "VO2 max",
    "training_load": "Training load",
}
JOIN_DAYS = {"weight": 14, "vo2_max": 14}
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
LOAD_PARTS = {
    "cardio": ("Cardio", "load_cardio"),
    "strength": ("Strength", "load_strength"),
    "effort": ("Effort", "load_effort"),
}
LOAD_SCORES = tuple(score for _, score in LOAD_PARTS.values())
LOAD_BAND = (0.88, 1.1)
DEFAULT_CARDS = (
    {"type": "week"},
    {"type": "trend", "metric": "training_load"},
    {"type": "sleep"},
    {"type": "sets_by_muscle"},
    {"type": "lift"},
    {"type": "trend", "metric": "resting_hr"},
    {"type": "trend", "metric": "hrv_sdnn"},
)
