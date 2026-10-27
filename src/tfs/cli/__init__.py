"""The `tfs` command. Each non-underscore module in this package is one command."""

import argparse
import importlib
import pkgutil
from types import ModuleType

from tfs import __version__


def main(argv: list[str] | None = None, *, modules: list[ModuleType] | None = None) -> int:
    """Run the command named in argv and return its exit code."""
    parser = argparse.ArgumentParser(prog="tfs")
    parser.add_argument("--version", action="version", version=f"tfs {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)
    if modules is None:
        names = sorted(
            module.name
            for module in pkgutil.iter_modules(__path__)
            if not module.name.startswith("_")
        )
        modules = [importlib.import_module(f"{__name__}.{name}") for name in names]
    for module in modules:
        module.add_parser(subparsers)
    args = parser.parse_args(argv)
    return args.run(args)
