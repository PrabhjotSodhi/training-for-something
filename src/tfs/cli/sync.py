"""`tfs sync [NAME]`: fetch from each connected app and store what it sends."""

import argparse
import sys
from contextlib import closing
from datetime import UTC, datetime

from tfs import config
from tfs.connectors.core.contract import load_manifests, to_utc_text
from tfs.importer.session import sync
from tfs.persistence import sqlite
from tfs.persistence.migrations import DatabaseTooNewError, migrate


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the `sync` command."""
    parser = subparsers.add_parser("sync", help="fetch new data from your connected apps")
    parser.add_argument("name", nargs="?", help="sync only this connector, for example hevy")
    parser.add_argument("--module", help=argparse.SUPPRESS)
    parser.set_defaults(run=run)


def run(args: argparse.Namespace) -> int:
    """Sync one connector or every enabled one that fetches, and return the exit code."""
    config.data_dir().mkdir(parents=True, exist_ok=True)
    exit_code = 0
    try:
        with closing(sqlite.connect(config.db_path())) as conn:
            migrate(conn)
            enabled = {
                row["connector"]
                for row in conn.execute("SELECT connector FROM sources WHERE enabled = 1")
            }
            names = (
                [args.name]
                if args.name
                else sorted(
                    name
                    for name, manifest in load_manifests().items()
                    if manifest["routes"]["fetch"] and name in enabled
                )
            )
            for name in names:
                now = to_utc_text(datetime.now(UTC))
                result = sync(conn, config.data_dir(), name, now=now, module=args.module)
                print(result.summary, file=sys.stderr if result.needs_you else sys.stdout)
                if result.needs_you:
                    exit_code = 1
    except (DatabaseTooNewError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1
    return exit_code
