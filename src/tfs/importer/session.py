"""One import run: read a saved file, store its records and rebuild the dates it changed."""

import fnmatch
import json
import logging
import sqlite3
import tempfile
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import tzinfo
from importlib.util import find_spec
from pathlib import Path
from types import TracebackType
from typing import TextIO

from tfs.connectors.core import runner
from tfs.connectors.core.contract import load_manifests
from tfs.duplicates import workouts
from tfs.importer import records
from tfs.importer.raw_store import save_raw
from tfs.persistence.sqlite import transaction
from tfs.source_priority import daily_totals

logger = logging.getLogger(__name__)

_READER_STATUSES = ("ok", "partial", "unreadable")


@dataclass(frozen=True)
class ImportResult:
    """What reading one file did. skipped means the file was already read or another process holds it."""

    sha256: str
    connector: str
    status: str
    message: str | None
    inserted: int = 0
    replaced: int = 0
    unchanged: int = 0
    deleted: int = 0
    rejected: dict[str, int] = field(default_factory=dict)
    skipped: bool = False


class NoReaderError(Exception):
    """No connector reads a file with this name."""


class AmbiguousRouteError(Exception):
    """More than one connector reads a file with this name."""


class _Failure:
    """Swallow any error raised in the block and keep only its type name.

    An error's message or traceback can quote the file's contents, so neither is kept.
    """

    error_name: str | None = None

    def __enter__(self) -> "_Failure":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        if exc_type is None or not issubclass(exc_type, Exception):
            return False
        self.error_name = exc_type.__name__
        return True


def route_file(name: str, manifests: dict) -> tuple[str, str, str]:
    """Return the connector, format and extension for a file name, matching routes.files without regard to case."""
    folded = name.lower()
    matches = sorted(
        connector
        for connector, manifest in manifests.items()
        if any(
            fnmatch.fnmatchcase(folded, pattern.lower()) for pattern in manifest["routes"]["files"]
        )
    )
    if not matches:
        raise NoReaderError(f"No connector reads {name}.")
    if len(matches) > 1:
        raise AmbiguousRouteError(
            f"More than one connector reads {name}: {' and '.join(matches)}. "
            "Choose one with --connector."
        )
    return matches[0], "file", Path(folded).suffix.removeprefix(".")


def import_file(
    conn: sqlite3.Connection,
    data_dir: Path,
    path: Path,
    *,
    now: str,
    connector: str | None = None,
    tz: tzinfo | None = None,
) -> ImportResult:
    """Save one file under the connector that reads it, read it and rebuild the dates it changed."""
    path = path.resolve()
    if connector is None:
        connector, fmt, ext = route_file(path.name, load_manifests())
    else:
        manifest = load_manifests().get(connector)
        if manifest is None or not manifest["routes"]["files"]:
            raise NoReaderError(f"No connector named {connector} reads files.")
        fmt, ext = "file", path.suffix.lower().removeprefix(".")
    with path.open("rb") as source:
        saved = save_raw(conn, data_dir, connector, fmt, ext, source, now=now)
    result = read_file(conn, data_dir, saved.sha256, tz=tz)
    rebuild_pending(conn, tz=tz)
    return result


def read_file(
    conn: sqlite3.Connection,
    data_dir: Path,
    sha256: str,
    *,
    tz: tzinfo | None = None,
    timeout_s: float = 3600,
    module: str | None = None,
) -> ImportResult:
    """Read one registered file, store its records and set its status last."""
    row = _file_row(conn, sha256)
    # Only the process that moves the row to reading may read the file.
    with transaction(conn):
        claimed = conn.execute(
            "UPDATE files SET status = 'reading' WHERE sha256 = ? AND status IN ('new', 'failed')",
            (sha256,),
        ).rowcount
    if not claimed:
        current = _file_row(conn, sha256)
        return ImportResult(
            sha256, current["connector"], current["status"], current["message"], skipped=True
        )
    path = data_dir / "raw" / row["connector"] / row["format"] / f"{sha256}.{row['ext']}"
    with _Failure() as failure, tempfile.TemporaryDirectory() as workdir:
        version = _reader_version(row["connector"], module)
        out_path = Path(workdir) / "records.jsonl"
        payload = {"path": str(path), "fmt": row["format"], "out_path": str(out_path)}
        try:
            reply = runner.run(
                row["connector"], "read", payload, timeout_s=timeout_s, module=module
            )
        except (
            runner.RunnerTimeoutError,
            runner.RunnerFailedError,
            runner.ConnectorUserError,
        ) as error:
            return _finish(conn, row, "failed", f"Reader failed: {error}")
        status = reply.get("status")
        if status not in _READER_STATUSES:
            return _finish(conn, row, "failed", "Reader failed: it returned no usable status.")
        if status == "unreadable":
            return _finish(conn, row, "unreadable", reply.get("message"), version=version)
        with out_path.open(encoding="utf-8") as lines:
            stored = records.store(
                conn,
                connector=row["connector"],
                file_sha256=sha256,
                arrival_seq=row["arrival_seq"],
                received_at=row["received_at"],
                reader_version=version,
                records=_decode(lines),
                tz=tz,
            )
        messages = [reply.get("message"), _rejected_message(stored.rejected)]
        return _finish(
            conn,
            row,
            "partial" if status == "partial" or stored.rejected else "ok",
            " ".join(message for message in messages if message) or None,
            version=version,
            stored=stored,
        )
    logger.error("file processing failed %s %s", sha256, failure.error_name)
    return _finish(conn, row, "failed", f"Reader failed: {failure.error_name}")


def process_pending(
    conn: sqlite3.Connection,
    data_dir: Path,
    *,
    tz: tzinfo | None = None,
    retry_failed: bool = False,
    module: str | None = None,
) -> list[ImportResult]:
    """Read every new file in arrival order, then rebuild the dates they changed.

    A file that fails never stops the others. With retry_failed, failed files are read again.
    """
    pending = conn.execute(
        "SELECT sha256 FROM files WHERE status = 'new' OR (status = 'failed' AND ?)"
        " ORDER BY arrival_seq",
        (retry_failed,),
    ).fetchall()
    results = []
    for (sha256,) in pending:
        results.append(read_file(conn, data_dir, sha256, tz=tz, module=module))
    rebuild_pending(conn, tz=tz)
    return results


def rebuild_pending(conn: sqlite3.Connection, *, tz: tzinfo | None = None) -> int:
    """Rebuild the dates in rebuild_dates and those with partial day facts, one month per transaction, and return how many."""
    conn.execute(
        "INSERT OR IGNORE INTO rebuild_dates"
        " SELECT local_date FROM day_facts WHERE coverage = 'partial'"
    )
    months = conn.execute(
        "SELECT DISTINCT substr(local_date, 1, 7) FROM rebuild_dates ORDER BY 1"
    ).fetchall()
    rebuilt = 0
    for (month,) in months:
        with transaction(conn):
            dates = conn.execute(
                "SELECT local_date FROM rebuild_dates WHERE substr(local_date, 1, 7) = ?", (month,)
            ).fetchall()
            changed = {date for (date,) in dates}
            workouts.update(conn, changed)
            daily_totals.rebuild(conn, changed, tz=tz)
            rebuilt += conn.execute(
                "DELETE FROM rebuild_dates WHERE substr(local_date, 1, 7) = ?", (month,)
            ).rowcount
    return rebuilt


def _file_row(conn: sqlite3.Connection, sha256: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM files WHERE sha256 = ?", (sha256,)).fetchone()
    if row is None:
        raise ValueError(f"No file is registered under {sha256}.")
    return row


def _reader_version(connector: str, module: str | None) -> str:
    if module is None:
        return runner.reader_version(connector)
    spec = find_spec(module)
    if spec is None or spec.origin is None:
        raise ValueError(f"The connector module {module} cannot be found.")
    return runner.reader_version(folder=Path(spec.origin).parent)


def _decode(lines: TextIO) -> Iterator[dict]:
    for line in lines:
        if line.strip():
            yield json.loads(line)


def _rejected_message(rejected: Counter[str]) -> str | None:
    total = sum(rejected.values())
    if total == 0:
        return None
    noun = "record" if total == 1 else "records"
    return f"{total} {noun} rejected: " + "; ".join(reason for reason, _ in rejected.most_common())


def _finish(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    status: str,
    message: str | None,
    *,
    version: str | None = None,
    stored: records.StoreResult | None = None,
) -> ImportResult:
    with transaction(conn):
        conn.execute(
            "UPDATE files SET status = ?, message = ?, reader_version = ? WHERE sha256 = ?",
            (status, message, version, row["sha256"]),
        )
    counts = (
        {}
        if stored is None
        else {
            "inserted": stored.inserted,
            "replaced": stored.replaced,
            "unchanged": stored.unchanged,
            "deleted": stored.deleted,
            "rejected": dict(stored.rejected),
        }
    )
    return ImportResult(row["sha256"], row["connector"], status, message, **counts)
