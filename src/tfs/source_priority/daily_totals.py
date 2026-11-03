"""Builds the daily facts: sums that count each moment once, and the latest reading of the day (HLD 7.3, 8)."""

import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, tzinfo

from tfs.connectors.core.contract import METRICS, parse_iso_time

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

_SUMMED = """
SELECT local_date, metric, connector, start_utc, end_utc,
       json_extract(values_json, '$.value'), coalesce(json_extract(values_json, '$.merged'), 0),
       connector || '/' || stream || '/' || coalesce(origin_app, '-') || '/'
         || coalesce(origin_device, '-')
FROM source_records
WHERE kind = 'measurement' AND deleted_at IS NULL
  AND metric IN (SELECT value FROM json_each(:metrics))
  AND local_date IN (SELECT value FROM json_each(:days))
"""


def _uncovered_seconds(start: float, end: float, covered: list[tuple[float, float]]) -> float:
    free, gap = start, 0.0
    for covered_start, covered_end in sorted(covered):
        if covered_end <= free:
            continue
        if covered_start >= end:
            break
        gap += max(0.0, covered_start - free)
        free = covered_end
    return gap + max(0.0, end - free)


def _counted(
    by_source: dict[str, list[tuple]], merge_by_time: bool
) -> tuple[list[tuple[str, float]], bool]:
    """Return the sources one connector counts for a day, with their values, and if the first is marked merged."""
    totals = {source: sum(record[2] for record in records) for source, records in by_source.items()}
    order = sorted(by_source, key=lambda source: (-totals[source], source))
    marked = [source for source in order if any(record[3] for record in by_source[source])]
    if marked:
        return [(marked[0], totals[marked[0]])], True
    if not merge_by_time or any(
        end is None or end <= start for records in by_source.values() for start, end, *_ in records
    ):
        return [(order[0], totals[order[0]])], False
    counted, covered = [], []
    for source in order:
        records = by_source[source]
        added = sum(
            value
            if not covered
            else value * _uncovered_seconds(start, end, covered) / (end - start)
            for start, end, value, _ in records
        )
        if added > 0 or not counted:
            counted.append((source, added))
        covered += [record[:2] for record in records]
    return counted, False


def rebuild(conn: sqlite3.Connection, dates: set[str], *, tz: tzinfo | None = None) -> int:
    """Rebuild the day facts of dates and return how many rows that wrote.

    Every metric with a daily rule is cleared first, so a day that lost its readings loses its
    row. A summed day is partial until its connector has received data past the end of that date
    in tz, or in the computer's zone when tz is None. The caller owns the transaction.
    """
    days = json.dumps(sorted(dates))
    daily = {name: metric for name, metric in METRICS.items() if metric.daily != "none"}
    conn.execute(
        "DELETE FROM day_facts WHERE local_date IN (SELECT value FROM json_each(?))"
        " AND metric IN (SELECT value FROM json_each(?))",
        (days, json.dumps(list(daily))),
    )
    written = 0
    summed = {name: metric for name, metric in daily.items() if metric.daily == "sum"}
    received = dict(conn.execute("SELECT connector, last_received_at FROM sources"))
    grouped: dict[tuple[str, str], dict[str, dict[str, list[tuple]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(list))
    )
    for date, name, connector, start, end, value, merged, source in conn.execute(
        _SUMMED, {"metrics": json.dumps(list(summed)), "days": days}
    ):
        span = (parse_iso_time(start).timestamp(), end and parse_iso_time(end).timestamp())
        grouped[date, name][connector][source].append((*span, value, merged))
    for (date, name), connectors in grouped.items():
        counted = {
            connector: _counted(by_source, name != "sleep_asleep")
            for connector, by_source in connectors.items()
        }
        *_, connector = min(
            (not marked, -sum(added for _, added in sources), connector)
            for connector, (sources, marked) in counted.items()
        )
        sources, _ = counted[connector]
        value = sum(added for _, added in sources)
        if summed[name].zero_is_missing and value == 0:
            continue
        day_end = (datetime.fromisoformat(date) + timedelta(days=1)).replace(tzinfo=tz)
        partial = (
            name != "sleep_asleep"
            and parse_iso_time(received[connector]).timestamp() < day_end.timestamp()
        )
        conn.execute(
            "INSERT INTO day_facts (local_date, metric, value, unit, source, coverage,"
            " formula_version) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                date,
                name,
                value,
                summed[name].unit,
                " + ".join(source for source, _ in sources),
                "partial" if partial else "full",
                FORMULA_VERSION,
            ),
        )
        written += 1
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
