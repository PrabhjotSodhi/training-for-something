"""`tfs import PATH`: save an export file, read it and say what was stored."""

import argparse
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from tfs import config
from tfs.connectors.core.contract import to_utc_text
from tfs.importer.raw_store import FileConflictError
from tfs.importer.session import AmbiguousRouteError, NoReaderError, import_file
from tfs.persistence import sqlite
from tfs.persistence.migrations import BackupFailedError, DatabaseTooNewError, migrate


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the `import` command."""
    parser = subparsers.add_parser("import", help="import an export file")
    parser.add_argument("path", type=Path, help="the file to import, for example export.zip")
    parser.add_argument("--connector", help="read the file with this connector")
    parser.set_defaults(run=run)


def run(args: argparse.Namespace) -> int:
    """Import the file and return the exit code."""
    config.data_dir().mkdir(parents=True, exist_ok=True)
    try:
        with closing(sqlite.connect(config.db_path())) as conn:
            migrate(conn)
            result = import_file(
                conn,
                config.data_dir(),
                args.path,
                now=to_utc_text(datetime.now(UTC)),
                connector=args.connector,
            )
    except (NoReaderError, AmbiguousRouteError) as error:
        print(error, file=sys.stderr)
        return 2
    except FileNotFoundError as error:
        print(f"{error.filename} does not exist.", file=sys.stderr)
        return 1
    except (FileConflictError, DatabaseTooNewError, BackupFailedError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1
    name = args.path.name
    if result.status not in ("ok", "partial"):
        print(result.message or f"{name} was not imported ({result.status}).", file=sys.stderr)
        return 1
    if result.skipped:
        print(f"{name} was already imported.")
        return 0
    stored = result.inserted + result.replaced + result.unchanged
    rejected = sum(result.rejected.values())
    print(
        f"Imported {name} with {result.connector}: {stored} records stored, "
        f"{rejected} rejected ({result.status})."
    )
    return 0
