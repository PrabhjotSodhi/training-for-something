"""A command module used to test that `main` registers and runs commands."""

import argparse


def add_parser(subparsers) -> None:
    """Register the `hello` command."""
    parser = subparsers.add_parser("hello", help="say hello")
    parser.set_defaults(run=run)


def run(args: argparse.Namespace) -> int:
    """Return 7 so a test can see this command ran."""
    return 7
