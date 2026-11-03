"""Saves incoming files under their SHA-256 name and registers each one once."""

import hashlib
import os
import re
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from tfs.connectors.core.contract import to_utc_text
from tfs.persistence.sqlite import transaction

CHUNK_BYTES = 1024 * 1024
_NAME = re.compile(r"[a-z0-9_]+")
_EXTENSION = re.compile(r"[a-z0-9]+")
_RAW_FILE = re.compile(r"(?P<sha256>[0-9a-f]{64})\.(?P<ext>[a-z0-9]+)")


class FileConflictError(Exception):
    """The same bytes were already received from another connector or format."""


@dataclass(frozen=True)
class SavedFile:
    sha256: str
    path: Path
    arrival_seq: int
    is_new: bool


@dataclass(frozen=True)
class ScanResult:
    registered: list[str]
    missing: list[str]


def save_raw(
    conn: sqlite3.Connection,
    data_dir: Path,
    connector: str,
    fmt: str,
    ext: str,
    source: BinaryIO,
    *,
    now: str,
    cursor_json: str | None = None,
) -> SavedFile:
    """Save the bytes of source under raw/, register them once and mark the source as contacted."""
    for label, value, pattern in (
        ("connector", connector, _NAME),
        ("format", fmt, _NAME),
        ("extension", ext, _EXTENSION),
    ):
        if pattern.fullmatch(value) is None:
            raise ValueError(
                f"The {label} {value!r} is not allowed. Use only a-z, 0-9"
                f"{'' if pattern is _EXTENSION else ' and _'}."
            )
    folder = data_dir / "raw" / connector / fmt
    folder.mkdir(parents=True, exist_ok=True)
    # Every call gets its own temporary file, so two writers never touch each other's data.
    descriptor, name = tempfile.mkstemp(dir=folder, prefix=".tmp-")
    temporary = Path(name)
    try:
        digest = hashlib.sha256()
        size_bytes = 0
        with os.fdopen(descriptor, "wb") as handle:
            while chunk := source.read(CHUNK_BYTES):
                digest.update(chunk)
                size_bytes += len(chunk)
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        sha256 = digest.hexdigest()
        # The check, the rename and the row share one write lock. A second writer of the
        # same bytes waits here, then sees the first writer's row. The file is renamed
        # last, so a committed row always has its file.
        with transaction(conn):
            row = conn.execute(
                "SELECT arrival_seq, connector, format, ext FROM files WHERE sha256 = ?",
                (sha256,),
            ).fetchone()
            if row is not None and (row["connector"], row["format"]) != (connector, fmt):
                raise FileConflictError(
                    f"This file was already received as {row['connector']}/{row['format']}."
                )
            if row is None:
                arrival_seq = conn.execute(
                    "INSERT INTO files (sha256, arrival_seq, connector, format, ext, size_bytes,"
                    " received_at) SELECT ?, COALESCE(MAX(arrival_seq), 0) + 1, ?, ?, ?, ?, ?"
                    " FROM files RETURNING arrival_seq",
                    (sha256, connector, fmt, ext, size_bytes, now),
                ).fetchone()[0]
            else:
                arrival_seq, ext = row["arrival_seq"], row["ext"]
            conn.execute(
                "INSERT INTO sources (connector, cursor_json, last_contact_at, last_received_at)"
                " VALUES (?, ?, ?, ?) ON CONFLICT (connector) DO UPDATE SET"
                " cursor_json = COALESCE(excluded.cursor_json, cursor_json),"
                " last_contact_at = excluded.last_contact_at,"
                " last_received_at = excluded.last_received_at",
                (connector, cursor_json, now, now),
            )
            final = folder / f"{sha256}.{ext}"
            temporary.replace(final)
            _sync_folder(folder)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return SavedFile(sha256, final, arrival_seq, row is None)


def _sync_folder(folder: Path) -> None:
    """Make the rename durable. Windows cannot open a folder, so it skips this."""
    if os.name == "nt":
        return
    descriptor = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def scan_raw(conn: sqlite3.Connection, data_dir: Path) -> ScanResult:
    """Register raw files that have no row, mark rows whose file is gone and unstick rows."""
    raw = data_dir / "raw"
    # The scan holds the write lock, so it never sees a file that save_raw renamed but
    # has not committed yet.
    with transaction(conn):
        rows = conn.execute("SELECT * FROM files ORDER BY arrival_seq").fetchall()
        known = {row["sha256"] for row in rows}
        orphans = []
        for path in raw.glob("*/*/*"):
            match = _RAW_FILE.fullmatch(path.name)
            if match is not None and match["sha256"] not in known and path.is_file():
                orphans.append((path.stat(), path, match))
        registered = []
        for info, path, match in sorted(
            orphans, key=lambda orphan: (orphan[0].st_mtime_ns, orphan[1])
        ):
            if match["sha256"] in known:
                continue
            known.add(match["sha256"])
            conn.execute(
                "INSERT INTO files (sha256, arrival_seq, connector, format, ext, size_bytes,"
                " received_at) SELECT ?, COALESCE(MAX(arrival_seq), 0) + 1, ?, ?, ?, ?, ?"
                " FROM files",
                (
                    match["sha256"],
                    path.parent.parent.name,
                    path.parent.name,
                    match["ext"],
                    info.st_size,
                    to_utc_text(datetime.fromtimestamp(info.st_mtime, UTC)),
                ),
            )
            registered.append(match["sha256"])
        missing = []
        for row in rows:
            name = f"{row['sha256']}.{row['ext']}"
            if not (raw / row["connector"] / row["format"] / name).is_file():
                missing.append(row["sha256"])
                conn.execute(
                    "UPDATE files SET status = 'missing', message = ? WHERE sha256 = ?",
                    ("The raw file is missing.", row["sha256"]),
                )
            elif row["status"] in ("missing", "reading"):
                conn.execute(
                    "UPDATE files SET status = 'new', message = NULL WHERE sha256 = ?",
                    (row["sha256"],),
                )
    return ScanResult(registered, missing)
