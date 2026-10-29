"""Numbered SQL migrations. Every pending file applies together or not at all."""

import re
import sqlite3
from collections.abc import Callable
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path

_FILE_NAME = re.compile(r"([0-9]{4})_.+\.sql")


class DatabaseTooNewError(Exception):
    """The database was made by a newer version of TFS than this one."""


def migrate(conn: sqlite3.Connection, migrations_dir: Path | None = None) -> int:
    """Apply every migration above user_version in one transaction and return the schema number."""
    files = _numbered_files(_folder(migrations_dir))
    newest = len(files)
    while True:
        current = _user_version(conn)
        if current > newest:
            raise DatabaseTooNewError(
                f"This database was made by a newer version of TFS (schema {current}). "
                "Install that version, or restore a backup."
            )
        if current == newest:
            return newest
        scripts = [_read(files[number]) for number in range(current + 1, newest + 1)]
        if conn.in_transaction:
            raise sqlite3.ProgrammingError(
                "Finish or roll back the open transaction before migrating the database."
            )
        try:
            _apply(conn, scripts, newest)
        except sqlite3.Error:
            # Another process may have migrated between our read of user_version and our lock.
            # Then our files fail on tables that now exist, so look again before giving up.
            if _user_version(conn) == current:
                raise
        else:
            return newest


def _folder(migrations_dir: Path | None) -> Traversable:
    """Return the given folder, or the migrations shipped inside TFS."""
    if migrations_dir is not None:
        return migrations_dir
    try:
        return resources.files("tfs.persistence.sql")
    except ModuleNotFoundError as error:
        raise ValueError("The TFS migration files are missing. Reinstall TFS.") from error


def _numbered_files(folder: Traversable) -> dict[int, Traversable]:
    """Map each migration number to its file, and require the numbers to run 1, 2, 3 with no gaps."""
    try:
        names = [entry for entry in folder.iterdir() if entry.name.endswith(".sql")]
    except OSError as error:
        raise ValueError(f"Cannot read the migrations folder {folder}: {error}") from error
    files: dict[int, Traversable] = {}
    for entry in names:
        number = _number_of(entry.name)
        if number in files:
            raise ValueError(
                f"Two migration files are numbered {number:04d}: {files[number].name} "
                f"and {entry.name}. Each number can be used once."
            )
        files[number] = entry
    for expected in range(1, len(files) + 1):
        if expected not in files:
            raise ValueError(
                f"Migration {expected:04d} is missing. "
                "File numbers must run 1, 2, 3 with no gaps."
            )
    return files


def _number_of(name: str) -> int:
    """Return the number in a name like 0001_init.sql."""
    match = _FILE_NAME.fullmatch(name)
    if match is None or int(match[1]) == 0:
        raise ValueError(f"Migration file {name!r} must be named like 0001_name.sql, from 0001 up.")
    return int(match[1])


def _read(entry: Traversable) -> str:
    """Return the text of one migration file."""
    try:
        text = entry.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ValueError(f"Cannot read migration file {entry.name}: {error}") from error
    if "\0" in text:
        raise ValueError(f"Migration file {entry.name} contains a null character.")
    return text


def _user_version(conn: sqlite3.Connection) -> int:
    """Return the schema number stored in the database file."""
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _apply(conn: sqlite3.Connection, scripts: list[str], newest: int) -> None:
    """Run the scripts and the version change inside one transaction, or change nothing."""
    # A lone ";" after each file stops a missing semicolon or a trailing comment
    # from joining one file to the next.
    body = "".join(f"{script}\n;\n" for script in scripts)
    script = f"BEGIN IMMEDIATE;\n{body}PRAGMA user_version = {newest};\nCOMMIT;"
    try:
        conn.set_authorizer(_allow_only_our_transaction(newest))
        try:
            conn.executescript(script)
        finally:
            conn.set_authorizer(None)
        if conn.in_transaction:
            # An unclosed /* comment in a file hides the rest of the script, COMMIT included.
            raise sqlite3.OperationalError(
                "A migration file did not finish. Check it for a /* comment that is never closed."
            )
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _allow_only_our_transaction(newest: int) -> Callable[..., int]:
    """Build an authorizer that refuses any BEGIN, COMMIT or ROLLBACK a migration file adds.

    A file that ends the transaction early would leave the earlier work saved
    when a later file fails.
    """
    begun = False
    version_set = False

    def authorize(action: int, arg1: str | None, arg2: str | None, *_: str | None) -> int:
        nonlocal begun, version_set
        if action == sqlite3.SQLITE_PRAGMA and arg1 == "user_version" and arg2 == str(newest):
            version_set = True
        if action != sqlite3.SQLITE_TRANSACTION:
            return sqlite3.SQLITE_OK
        if arg1 == "BEGIN" and not begun:
            begun = True
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_OK if arg1 == "COMMIT" and version_set else sqlite3.SQLITE_DENY

    return authorize
