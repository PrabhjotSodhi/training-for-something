"""`tfs mcp`: run the ask TFS tool for an AI tool over stdio."""

import argparse
import sys

from tfs import config
from tfs.mcp import server


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the `mcp` command."""
    parser = subparsers.add_parser("mcp", help="run the ask TFS tool for your AI tool")
    parser.set_defaults(run=run)


def run(args: argparse.Namespace) -> int:
    """Serve MCP on stdin and stdout until the AI tool closes them."""
    # Windows pipes default to cp1252, which breaks non-ASCII text.
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    server.serve(sys.stdin, sys.stdout, config.db_path())
    return 0
