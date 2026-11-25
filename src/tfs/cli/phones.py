"""Manage dashboard access for phones on the home Wi-Fi."""

import argparse
from contextlib import closing

from tfs import config
from tfs.persistence.migrations import migrate
from tfs.persistence.sqlite import connect


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the phones forget command."""
    commands = subparsers.add_parser("phones").add_subparsers(required=True)
    commands.add_parser("forget", help="remove access for all paired phones").set_defaults(
        run=forget
    )


def forget(args: argparse.Namespace) -> int:
    """Revoke phone tokens and unused pairing links."""
    if config.db_path().exists():
        with closing(connect(config.db_path())) as conn:
            migrate(conn)
            conn.execute("DELETE FROM phone_pairings")
    print("All phones forgotten.")
    return 0
