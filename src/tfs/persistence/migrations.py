"""Numbered SQL migrations. Every pending file applies together or not at all."""

import re
import sqlite3
from collections.abc import Callable
from contextlib import closing, suppress
from datetime import UTC, datetime
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path

_FILE_NAME = re.compile(r"([0-9]{4})_.+\.sql")


class DatabaseTooNewError(Exception):
    """The database was made by a newer version of TFS than this one."""


class BackupFailedError(Exception):
    """The copy of the database that comes before a migration could not be saved."""


def migrate(
    conn: sqlite3.Connection,
    migrations_dir: Path | None = None,
    on_backup: Callable[[Path], None] | None = None,
) -> int:
    """Apply every migration above user_version in one transaction and return the schema number.

    An existing database is copied to backups/ in its folder first. on_backup gets the copy's path.
    """
    files = _numbered_files(_folder(migrations_dir))
    newest = len(files)
    current = _schema_number(conn, newest)
    if current == newest:
        return newest
    if conn.in_transaction:
        raise sqlite3.ProgrammingError(
            "Finish or roll back the open transaction before migrating the database."
        )
    database_file = conn.execute("PRAGMA database_list").fetchone()[2]
    if current > 0 and database_file:
        backup = _back_up(conn, Path(database_file).parent / "backups")
        if on_backup is not None:
            on_backup(backup)
    _apply(conn, files)
    return newest


def _folder(migrations_dir: Path | None) -> Traversable:
    """Return the given folder, or the migrations shipped inside TFS."""
    if migrations_dir is not None:
        return migrations_dir
    return resources.files("tfs.persistence.sql")


def _numbered_files(folder: Traversable) -> dict[int, Traversable]:
    """Map each migration number to its file."""
    try:
        names = [entry for entry in folder.iterdir() if entry.name.endswith(".sql")]
    except OSError as error:
        raise ValueError(f"Cannot read the migrations folder {folder}: {error}") from error
    files: dict[int, Traversable] = {}
    for entry in names:
        number = _number_of(entry.name)
        files[number] = entry
    return files


def _number_of(name: str) -> int:
    """Return the number in a name like 0001_init.sql."""
    match = _FILE_NAME.fullmatch(name)
    if match is None:
        raise ValueError(f"Migration file {name!r} must be named like 0001_name.sql, from 0001 up.")
    return int(match[1])


def _read(entry: Traversable) -> str:
    """Return the text of one migration file."""
    return entry.read_text(encoding="utf-8")


def _schema_number(conn: sqlite3.Connection, newest: int) -> int:
    """Return the schema number stored in the database file, or raise if TFS cannot use it."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current < 0:
        raise sqlite3.DatabaseError(
            f"This database has an invalid schema number ({current}). Restore a backup."
        )
    if current > newest:
        raise DatabaseTooNewError(
            f"This database was made by a newer version of TFS (schema {current}). "
            "Install that version, or restore a backup."
        )
    return current


_BACKUPS_KEPT = 2


def _back_up(conn: sqlite3.Connection, folder: Path) -> Path:
    """Copy the database into folder, keep the newest backups and return the new copy's path."""
    day = f"{datetime.now(UTC):%Y-%m-%d}"
    partial = folder / f"tfs-{day}.tmp"
    try:
        folder.mkdir(exist_ok=True)
        with closing(sqlite3.connect(partial)) as copy:
            conn.backup(copy)
            # Named from the copy itself: another process may have migrated since this one checked.
            schema_number = copy.execute("PRAGMA user_version").fetchone()[0]
        target = folder / f"tfs-v{schema_number}-{day}.db"
        partial.replace(target)
    except (OSError, sqlite3.Error) as error:
        with suppress(OSError):
            partial.unlink()
        raise BackupFailedError(
            f"TFS could not save a backup in {folder} ({error}), so it did not update your "
            "database. Free some disk space or fix the folder, then try again."
        ) from error
    others = [path for path in folder.glob("tfs-v*.db") if path != target]
    others.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    for path in others[_BACKUPS_KEPT - 1 :]:
        with suppress(OSError):
            path.unlink()
    return target


def _apply(conn: sqlite3.Connection, files: dict[int, Traversable]) -> None:
    """Take the write lock, then read the schema number and apply what is missing, or change nothing."""
    newest = len(files)
    # In the default mode, executescript commits an open transaction before it runs.
    legacy_mode = conn.autocommit
    conn.autocommit = True
    try:
        conn.execute("BEGIN IMMEDIATE")
        current = _schema_number(conn, newest)
        # A lone ";" after each file stops a missing semicolon or a trailing comment
        # from joining one file to the next.
        body = "".join(f"{_read(files[number])}\n;\n" for number in range(current + 1, newest + 1))
        conn.executescript(f"{body}PRAGMA user_version = {newest};\nCOMMIT;")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.autocommit = legacy_mode
