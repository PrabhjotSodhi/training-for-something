"""Runs one import and stops the process at a named save point, as a power cut would.

Usage: python -m tests.crash_child <crash point or "none"> <data folder> [scenario]

A scenario is a list of files saved one after another. All but the last run to the end first,
so the crash comes during the last one.
"""

import functools
import io
import json
import os
import sys
from pathlib import Path

from tfs.duplicates import workouts
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


def _record(kind: str, stream: str, key: str, local_date: str, values: dict, **extra) -> dict:
    return {
        "revision_at": None,
        "start_utc": f"{local_date}T18:12:33Z",
        "end_utc": f"{local_date}T18:42:33Z",
        "utc_offset_min": 0,
        "local_date": local_date,
        "origin_app": None,
        "origin_device": None,
        "plan_ref": None,
        "kind": kind,
        "stream": stream,
        "key": key,
        "values": values,
        **extra,
    }


def _run(key: str, local_date: str) -> dict:
    return _record(
        "session",
        "workouts",
        key,
        local_date,
        {"duration_s": 1800.0},
        label="Invented run",
        family="run",
        metric=None,
        parts=[
            {"seq": 0, "part_kind": "warmup", "duration_s": 600.0},
            {"seq": 1, "part_kind": "main", "duration_s": 1200.0},
        ],
    )


def _steps(key: str, local_date: str) -> dict:
    values = {"value": 10.0, "unit": "count"}
    return _record(
        "measurement", "steps", key, local_date, values, label="Steps", family=None, metric="steps"
    )


def _delete(key: str) -> dict:
    return {"kind": "deleted", "stream": "workouts", "key": key}


# What the reader returns for each file. A file it does not know gets 600 steps and a run, which
# make two batches.
FILES = {
    b"moved first": [_run("run", "2026-10-01"), _steps("steps", "2026-10-01")],
    b"moved second": [_run("run", "2026-10-20"), _steps("steps", "2026-10-20")],
    b"deleted": [_delete("run")],
    b"two months": [
        _run("august", "2026-08-30"),
        _steps("august steps", "2026-08-30"),
        _run("september", "2026-09-02"),
        _steps("september steps", "2026-09-02"),
    ],
}
# The files of each scenario, with the time each one arrives.
SCENARIOS = {
    "one file": [(DATA, NOW)],
    "moved": [(b"moved first", NOW), (b"moved second", "2026-10-27T10:00:00Z")],
    "deleted": [(b"moved first", NOW), (b"deleted", "2026-10-27T10:00:00Z")],
    "two months": [(b"two months", NOW)],
}


def read(path: str, fmt: str, out_path: str) -> dict:
    """A reader that returns the records listed for the file's contents."""
    found = FILES.get(Path(path).read_bytes())
    if found is None:
        found = [_run("run", "2026-02-28")]
        found += [_steps(f"steps{number}", "2026-02-28") for number in range(RECORD_COUNT - 1)]
    lines = [json.dumps(record) for record in found]
    Path(out_path).write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    return {"status": "ok", "count": len(lines), "message": None}


def operate(data_dir: Path, upload: tuple[bytes, str] = SCENARIOS["one file"][0]) -> None:
    """Save one file, read it and rebuild the dates it changed, as an import does."""
    data, now = upload
    conn = connect(data_dir / "tfs.db")
    migrate(conn)
    saved = save_raw(
        conn,
        data_dir,
        CONNECTOR,
        "records",
        "json",
        io.BytesIO(data),
        now=now,
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


def _exit_at_second_call(original):
    calls = []

    def wrapper(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            os._exit(1)
        return original(*args, **kwargs)

    return wrapper


# Each point is the object and function to patch, and how to build the replacement.
CRASH_POINTS = {
    "before_rename": (Path, "replace", lambda original: _exit),
    "after_rename": (Path, "replace", _exit_after),
    "before_read": (session, "read_file", lambda original: _exit),
    "mid_read": (records, "store", _exit_at_second_batch),
    "before_rebuild": (session, "rebuild_pending", lambda original: _exit),
    "before_second_month": (workouts, "update", _exit_at_second_call),
}


if __name__ == "__main__":
    point, folder, *rest = sys.argv[1:]
    *settled, last = SCENARIOS[rest[0] if rest else "one file"]
    for upload in settled:
        operate(Path(folder), upload)
    if point != "none":
        target, name, build = CRASH_POINTS[point]
        setattr(target, name, build(getattr(target, name)))
    operate(Path(folder), last)
