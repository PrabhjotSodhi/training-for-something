"""`tfs mcp`: run the ask TFS tool for an AI tool over stdio."""

import argparse


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the `mcp` command."""
    parser = subparsers.add_parser("mcp", help="run the ask TFS tool for your AI tool")
    parser.set_defaults(run=run)


def run(args: argparse.Namespace) -> int:
    """Serve MCP on stdin and stdout until the AI tool closes them."""
    # Imported here because the MCP SDK is slow to load and every other command skips it.
    from tfs.mcp import server

    server.mcp.run()
    return 0
