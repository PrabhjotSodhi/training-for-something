"""`tfs status`: show what came in, what is stale and what broke."""

import argparse
import sys
from contextlib import closing
from datetime import UTC, datetime

from tfs import config, diagnostics
from tfs.persistence import sqlite


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the `status` command."""
    parser = subparsers.add_parser("status", help="show what came in, what is stale and what broke")
    parser.set_defaults(run=run)


def run(args: argparse.Namespace) -> int:
    """Print the status report and return the exit code."""
    if not config.db_path().exists():
        print("TFS has no data yet. Run tfs connect NAME first.", file=sys.stderr)
        return 1
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    with closing(sqlite.connect(config.db_path(), "read_only")) as conn:
        print(diagnostics.render_text(diagnostics.report(conn, now=now)))
    return 0
