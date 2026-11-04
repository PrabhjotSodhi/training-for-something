"""Runs one import and stops the process at a named save point, as a power cut would.

Usage: python -m tests.crash_child <crash point or "none"> <data folder>
"""

import functools
import io
import json
import os
import sys
from pathlib import Path

from tfs.importer import records, session
from tfs.importer.raw_store import save_raw
from tfs.persistence.migrations import migrate
from tfs.persistence.sqlite import connect

CONNECTOR = "fake_connector"
MODULE = "tests.crash_child"
NOW = "2026-10-27T09:00:00Z"
DATA = b"invented crash test file"
CURSOR_JSON = '{"page": 2}'
BATCH_SIZE = 500
RECORD_COUNT = BATCH_SIZE + 100


def read(path: str, fmt: str, out_path: str) -> dict:
    """A reader that returns one run with two parts, then step counts. 600 records make two batches."""
    base = {
        "revision_at": None,
        "start_utc": "2026-02-28T18:12:33Z",
        "end_utc": "2026-02-28T18:42:33Z",
        "utc_offset_min": 0,
        "local_date": "2026-02-28",
        "origin_app": None,
        "origin_device": None,
        "plan_ref": None,
    }
    run = {
        **base,
        "kind": "session",
        "stream": "workouts",
        "key": "run",
        "label": "Invented run",
        "family": "run",
        "metric": None,
        "values": {"duration_s": 1800.0},
        "parts": [
            {"seq": 0, "part_kind": "warmup", "duration_s": 600.0},
            {"seq": 1, "part_kind": "main", "duration_s": 1200.0},
        ],
    }
    steps = [
        {
            **base,
            "kind": "measurement",
            "stream": "steps",
            "key": f"steps{number}",
            "label": "Steps",
            "family": None,
            "metric": "steps",
            "values": {"value": 10.0, "unit": "count"},
        }
        for number in range(RECORD_COUNT - 1)
    ]
    lines = [json.dumps(record) for record in (run, *steps)]
    Path(out_path).write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    return {"status": "ok", "count": len(lines), "message": None}


def operate(data_dir: Path) -> None:
    """Save the file, read it and rebuild the dates it changed, as an import does."""
    conn = connect(data_dir / "tfs.db")
    migrate(conn)
    saved = save_raw(
        conn,
        data_dir,
        CONNECTOR,
        "records",
        "json",
        io.BytesIO(DATA),
        now=NOW,
        cursor_json=CURSOR_JSON,
    )
    session.read_file(conn, data_dir, saved.sha256, module=MODULE)
    session.rebuild_pending(conn)
    conn.close()


def _exit(*args: object, **kwargs: object) -> None:
    os._exit(1)


def _exit_after(original):
    def wrapper(*args, **kwargs):
        original(*args, **kwargs)
        os._exit(1)

    return wrapper


def _exit_at_second_batch(original):
    batches = []

    def before_batch():
        batches.append(1)
        if len(batches) == 2:
            os._exit(1)

    return functools.partial(original, before_batch=before_batch)


# Each point is the object and function to patch, and how to build the replacement.
CRASH_POINTS = {
    "before_rename": (Path, "replace", lambda original: _exit),
    "after_rename": (Path, "replace", _exit_after),
    "before_read": (session, "read_file", lambda original: _exit),
    "mid_read": (records, "store", _exit_at_second_batch),
}


if __name__ == "__main__":
    point, folder = sys.argv[1:]
    if point != "none":
        target, name, build = CRASH_POINTS[point]
        setattr(target, name, build(getattr(target, name)))
    operate(Path(folder))
