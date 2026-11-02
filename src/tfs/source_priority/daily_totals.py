"""Builds the daily facts: the latest reading of the day for resting heart rate and weight (HLD 7.3, 8)."""

import json
import sqlite3

from tfs.connectors.core.contract import METRICS

FORMULA_VERSION = "day-facts-1"

# A source is connector/stream/origin_app/origin_device. Ties on the instant go to the smaller
# source and then to the smaller record id, so the result never depends on insert order.
_LATEST = """
INSERT INTO day_facts (local_date, metric, value, unit, source, coverage, formula_version)
SELECT local_date, :metric, value, :unit, source, 'full', :formula_version FROM (
  SELECT local_date, value, source,
         row_number() OVER (PARTITION BY local_date ORDER BY start_utc DESC, source, id) AS position
  FROM (
    SELECT id, local_date, start_utc,
           json_extract(values_json, '$.value') AS value,
           connector || '/' || stream || '/' || coalesce(origin_app, '-') || '/'
             || coalesce(origin_device, '-') AS source
    FROM source_records
    WHERE kind = 'measurement' AND metric = :metric AND deleted_at IS NULL
      AND local_date IN (SELECT value FROM json_each(:days))
  )
  WHERE NOT (:zero_is_missing AND value = 0)
) WHERE position = 1
"""


def rebuild(conn: sqlite3.Connection, dates: set[str]) -> int:
    """Rebuild the day facts of dates and return how many rows that wrote.

    Every metric with a daily rule is cleared first, so a day that lost its readings loses its
    row. Summed metrics are cleared and not rebuilt yet. The caller owns the transaction.
    """
    days = json.dumps(sorted(dates))
    daily = {name: metric for name, metric in METRICS.items() if metric.daily != "none"}
    conn.execute(
        "DELETE FROM day_facts WHERE local_date IN (SELECT value FROM json_each(?))"
        " AND metric IN (SELECT value FROM json_each(?))",
        (days, json.dumps(list(daily))),
    )
    written = 0
    for name, metric in daily.items():
        if metric.daily == "latest":
            written += conn.execute(
                _LATEST,
                {
                    "metric": name,
                    "unit": metric.unit,
                    "zero_is_missing": metric.zero_is_missing,
                    "formula_version": FORMULA_VERSION,
                    "days": days,
                },
            ).rowcount
    return written
