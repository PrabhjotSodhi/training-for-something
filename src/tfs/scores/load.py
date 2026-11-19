"""Strength load: reps x weight over working sets per day, against the user's own normal (metrics spec)."""

import json
import sqlite3
import tomllib
from collections import defaultdict
from datetime import date, timedelta
from importlib import resources
from statistics import mean, median

SCORE = "load_strength"
_COEFFICIENTS = tomllib.loads(
    resources.files("tfs.scores").joinpath("coefficients.toml").read_text("utf-8")
)["load_strength"]
FORMULA_VERSION = _COEFFICIENTS["formula_version"]

# Each workout counts once: its parts come from the member with the most parts, as in coach_v1_workout_parts.
_WORKING_SETS = """
SELECT g.local_date, p.reps, p.weight_kg, json_extract(p.extras_json, '$.rpe')
FROM session_groups g
JOIN session_parts p ON p.record_id = (
  SELECT m.record_id FROM session_group_members m
  JOIN source_records live ON live.id = m.record_id AND live.deleted_at IS NULL
  WHERE m.group_id = g.id
  ORDER BY (SELECT count(*) FROM session_parts WHERE record_id = m.record_id) DESC, m.record_id
  LIMIT 1)
WHERE p.part_kind = 'set' AND coalesce(json_extract(p.extras_json, '$.set_type'), '') != 'warmup'
"""


def rebuild(conn: sqlite3.Connection) -> int:
    """Recompute every strength load row and return how many rows that wrote.

    The caller owns the transaction.
    """
    loads: dict[date, float] = defaultdict(float)
    sets: dict[date, list] = defaultdict(list)
    for local_date, reps, weight_kg, rpe in conn.execute(_WORKING_SETS):
        day = date.fromisoformat(local_date)
        loads[day] += (reps or 0) * (weight_kg or 0)
        sets[day].append((weight_kg, rpe))
    conn.execute("DELETE FROM scores WHERE score = ?", (SCORE,))
    for day in sorted(loads):
        previous = [
            loads[past]
            for offset in range(1, _COEFFICIENTS["baseline_days"] + 1)
            if (past := day - timedelta(days=offset)) in loads
        ]
        inputs: dict = {
            "working_sets": len(sets[day]),
            "normal": round(median(previous)) if previous else None,
            "normal_days": len(previous),
        }
        if rpes := [rpe for _, rpe in sets[day] if rpe is not None]:
            inputs["mean_rpe"] = round(mean(rpes), 1)
        if any(not weight_kg for weight_kg, _ in sets[day]):
            inputs["note"] = "body weight sets not counted"
        conn.execute(
            "INSERT INTO scores VALUES (?, ?, ?, NULL, NULL, ?, ?)",
            (day.isoformat(), SCORE, round(loads[day]), json.dumps(inputs), FORMULA_VERSION),
        )
    return len(loads)
