"""Runs one import and stops the process at a named save point, as a power cut would.

Usage: python -m tests.crash_child <crash point or "none"> <data folder> [scenario]

A scenario is a list of files saved one after another. All but the last run to the end first,
so the crash comes during the last one. The scenarios "push" and "sync" run one phone push or
one fetch instead.
"""

import functools
import io
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from tfs import password_store
from tfs.duplicates import workouts
from tfs.importer import records, session
from tfs.importer.raw_store import save_raw
from tfs.persistence.migrations import migrate
from tfs.persistence.sqlite import connect
from tfs.web import push

CONNECTOR = "fake_connector"
MODULE = "tests.crash_child"
NOW = "2026-10-27T09:00:00Z"
DATA = b"invented crash test file"
CURSOR_JSON = '{"page": 2}'
PUSH_CONNECTOR = "health_auto_export"
PUSH_BODY = Path(__file__).parent / "fixtures" / "health_auto_export" / "metrics.json"
PUSH_NOW = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)
FETCH_CONNECTOR = "fake_fetch_connector"
FETCH_MODULE = "tests.fake_fetch_connector.connector"
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


# What the reader returns for each file. DATA gets a run and 599 steps, which make two batches.
FILES = {
    DATA: [_run("run", "2026-02-28")]
    + [_steps(f"steps{number}", "2026-02-28") for number in range(RECORD_COUNT - 1)],
    b"moved first": [_run("run", "2026-10-01"), _steps("steps", "2026-10-01")],
    b"moved second": [_run("run", "2026-10-20"), _steps("steps", "2026-10-20")],
    b"deleted": [{"kind": "deleted", "stream": "workouts", "key": "run"}],
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
    found = FILES[Path(path).read_bytes()]
    lines = [json.dumps(record) for record in found]
    Path(out_path).write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    return {"status": "ok", "count": len(lines), "message": None}


def operate(data_dir: Path, data: bytes = DATA, now: str = NOW) -> None:
    """Save one file, read it and rebuild the dates it changed, as an import does."""
    conn = connect(data_dir / "tfs.db", "import")
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


def operate_push(data_dir: Path) -> None:
    """Handle one phone push of PUSH_BODY, as the engine's push handler does."""
    conn = connect(data_dir / "tfs.db")
    migrate(conn)
    conn.close()
    handle = push.make_push_handler(
        data_dir=data_dir,
        conn_factory=lambda: connect(data_dir / "tfs.db", "save"),
        now_fn=lambda: PUSH_NOW,
        on_saved=lambda sha256: None,
    )
    headers = {"Authorization": f"Bearer {password_store.get('push_token')}"}
    with PUSH_BODY.open("rb") as body:
        handle(PUSH_CONNECTOR, body, headers)


def operate_sync(data_dir: Path) -> None:
    """Fetch from the fake fetch connector, save its pages and read them."""
    conn = connect(data_dir / "tfs.db")
    migrate(conn)
    session.sync(conn, data_dir, FETCH_CONNECTOR, now=NOW, module=FETCH_MODULE)
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
    "after_push_saved": (push, "save_raw", _exit_after),
    "after_first_page": (session, "save_raw", _exit_at_second_call),
    "before_sync_read": (session, "process_pending", lambda original: _exit),
}


if __name__ == "__main__":
    point, folder, *rest = sys.argv[1:]
    scenario = rest[0] if rest else "one file"
    settled, last = [], None
    if scenario in SCENARIOS:
        *settled, last = SCENARIOS[scenario]
    for upload in settled:
        operate(Path(folder), *upload)
    if point != "none":
        target, name, build = CRASH_POINTS[point]
        setattr(target, name, build(getattr(target, name)))
    if scenario == "push":
        operate_push(Path(folder))
    elif scenario == "sync":
        operate_sync(Path(folder))
    else:
        operate(Path(folder), *last)
