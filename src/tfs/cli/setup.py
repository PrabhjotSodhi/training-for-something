"""`tfs setup`: connect the AI tools and the apps in one guided run, then start TFS."""

import argparse
import contextlib
import io
import itertools
import secrets
import socket
import sys
import threading
import webbrowser
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import qrcode
import questionary
from questionary import Separator, Style

from tfs import config, password_store, providers
from tfs.cli import import_, start
from tfs.cli.connect import connect_source
from tfs.connectors.core import runner
from tfs.connectors.core.contract import load_manifests, to_utc_text
from tfs.importer.session import sync
from tfs.persistence import sqlite
from tfs.persistence.migrations import DatabaseTooNewError
from tfs.providers import mcp_tools
from tfs.web import setup_page
from tfs.web.server import lan_address

# prompt_toolkit raises this on Windows when the terminal is not a real console, such as Git Bash.
# Its module refuses to import on other systems, so the tuple stays empty there.
_NO_CONSOLE: tuple[type[Exception], ...] = ()
if sys.platform == "win32":
    from prompt_toolkit.output.win32 import NoConsoleScreenBufferError

    _NO_CONSOLE = (NoConsoleScreenBufferError,)

_BIND = "0.0.0.0"  # noqa: S104 (a phone on the Wi-Fi must reach it)
_BOLD, _GREEN, _RED, _DIM = "bold", "fg:ansigreen", "fg:ansired", "fg:ansibrightblack"
# prompt_toolkit draws ticked items reversed (white background) unless the style says otherwise.
_CHECKLIST_STYLE = Style(
    [("selected", f"noreverse {_GREEN}"), ("pointer", "fg:ansicyan bold"), ("separator", _DIM)]
)
_CHECKLIST_HINT = Separator("  ↑↓ move · space pick · enter continue")

_CONNECT_ERRORS = (
    runner.ConnectorUserError,
    runner.RunnerTimeoutError,
    runner.RunnerFailedError,
    password_store.PasswordStoreError,
    DatabaseTooNewError,
)


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the `setup` command."""
    parser = subparsers.add_parser("setup", help="connect AI tools and apps, then start TFS")
    parser.set_defaults(run=run)


def run(args: argparse.Namespace) -> int:
    """Walk the person through setup and return the exit code."""
    if not sys.stdin.isatty():
        print("Run tfs setup in a terminal.", file=sys.stderr)
        return 1
    try:
        _connect_ai_tools()
        manifests = load_manifests()
        picked = _ask(
            _checklist(
                "Which apps hold your data?",
                [questionary.Choice(m["label"], value=n) for n, m in manifests.items()],
            )
        )
        for number, name in enumerate(picked, 1):
            print(f"\n── {manifests[name]['label']} ({number} of {len(picked)})")
            _set_up_app(name, manifests[name])
    except KeyboardInterrupt:
        print("\nSetup stopped.", file=sys.stderr)
        return 1
    except _NO_CONSOLE:
        print("\nRun tfs setup in PowerShell, Windows Terminal or Command Prompt.", file=sys.stderr)
        return 1
    print("Keep this window open. TFS receives phone data only while it runs.")
    return start.run(argparse.Namespace(home_wifi=False, open=True))


def fill_link(template: str, push_url: str, token: str) -> str:
    """Put the URL-encoded push address and token into a manifest link."""
    return template.replace("{push_url}", quote(push_url, safe="")).replace(
        "{token}", quote(token, safe="")
    )


def _checklist(message: str, choices: list) -> questionary.Question:
    return questionary.checkbox(
        message,
        choices=[*choices, _CHECKLIST_HINT],
        instruction="",
        style=_CHECKLIST_STYLE,
    )


@contextlib.contextmanager
def _spinner(text: str) -> Iterator[Callable[[str], None]]:
    """Animate a spinner beside text until the block ends. Yields a function that changes the text."""
    stream, label, done = sys.stdout, [text], threading.Event()

    def spin() -> None:
        width = 0
        for frame in itertools.cycle("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"):
            line = f"  {frame} {label[0]}"
            stream.write("\r" + line.ljust(width))
            stream.flush()
            width = max(width, len(line))
            if done.wait(0.08):
                break
        stream.write("\r" + " " * width + "\r")
        stream.flush()

    thread = threading.Thread(target=spin, daemon=True)
    if stream.isatty():
        thread.start()
    try:
        yield lambda new_text: label.__setitem__(0, new_text)
    finally:
        done.set()
        if thread.is_alive():
            thread.join()


def _ask(question: questionary.Question):
    answer = question.ask()
    if answer is None:
        raise KeyboardInterrupt
    return answer


def _connect_ai_tools() -> None:
    home = Path.home()
    with _spinner("Looking for your AI tools"):
        programs = {tool: providers.find(tool) for tool in providers.PROVIDERS}
        programs |= {tool: mcp_tools.find(tool, home) for tool in mcp_tools.TOOLS}
        connected = {
            p for p in providers.PROVIDERS if programs[p] and providers.is_connected(programs[p])
        } | {
            t
            for t in mcp_tools.TOOLS
            if programs[t] and mcp_tools.is_connected(t, programs[t], home, providers.run_command)
        }
    if not any(programs.values()):
        names = ", ".join(tool.label for tool in (*providers.PROVIDERS, *mcp_tools.TOOLS))
        questionary.print(
            f"TFS works with {names}. Install one, then run tfs setup again.", style=_RED
        )
        return
    choices = [_tool_choice(t, programs[t], t in connected) for t in programs]
    if not any(choice.disabled is None for choice in choices):
        for tool in connected:
            questionary.print(f"  ✔ {tool.label:<13} TFS connected", style=_GREEN)
        return
    picked = _ask(_checklist("Connect TFS to your AI tools", choices))
    for tool in picked:
        with _spinner(f"Connecting {tool.label}") as say:
            if isinstance(tool, providers.Provider):
                problem = providers.connect(programs[tool], tool, say)
            else:
                problem = mcp_tools.add(tool, programs[tool], home, providers.run_command)
        if problem:
            questionary.print(f"  ✘ Could not connect {tool.label}: {problem}", style=_RED)
        else:
            questionary.print(f"  ✔ {tool.label:<13} TFS connected", style=_GREEN)


def _tool_choice(
    tool: providers.Provider | mcp_tools.Tool, program: str | None, connected: bool
) -> questionary.Choice:
    if program is None:
        return questionary.Choice(tool.label, value=tool, disabled="not found")
    if connected:
        return questionary.Choice(tool.label, value=tool, disabled="TFS connected")
    tested = "tested" if isinstance(tool, providers.Provider) else "should work"
    return questionary.Choice(f"{tool.label:<22} found, {tested}", value=tool, checked=True)


def _set_up_app(name: str, manifest: dict) -> None:
    setup = manifest.get("setup", {})
    for step in setup.get("steps", []):
        print(f"  {step}")
    try:
        if manifest["routes"]["files"]:
            _import_file(name)
        if manifest["routes"]["push"]:
            _show_links(name, manifest["label"], setup.get("links", []))
        if manifest["asks"]:
            _connect_with_asks(name, manifest, setup.get("opens"))
    except _CONNECT_ERRORS as error:
        print(f"  {error}", file=sys.stderr)


def _import_file(name: str) -> None:
    typed = _ask(
        questionary.text("Drag the file onto this window, or type where it is (Enter to skip)")
    )
    # Dragging a file onto a Windows terminal wraps its path in quotes.
    path = typed.strip().strip("\"'")
    if not path:
        return
    output = io.StringIO()
    try:
        with (
            _spinner(f"Importing {Path(path).name}"),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(output),
        ):
            import_.run(argparse.Namespace(path=Path(path), connector=name))
    finally:
        print(output.getvalue(), end="")


def _show_links(name: str, label: str, links: list[dict]) -> None:
    connect_source(name, {})
    token = password_store.get("push_token")
    if not token:
        token = secrets.token_urlsafe(32)
        password_store.set("push_token", token)
    push_url = f"http://{_host()}:{config.HOME_WIFI_PORT}/push/{name}"
    buttons = [(f"Add {link['label']}", fill_link(link["url"], push_url, token)) for link in links]
    code = secrets.token_urlsafe(6)
    if sys.platform == "win32":
        questionary.print("  If Windows asks, allow TFS on private networks.", style=_DIM)
    try:
        server = setup_page.SetupServer(
            _BIND, config.HOME_WIFI_PORT, code, f"Connect {label}", buttons, _phone_reached
        )
    except OSError:
        questionary.print("  Close TFS, then run tfs setup again.", style=_RED)
        return
    server.start()
    try:
        _print_qr(f"http://{_host()}:{config.HOME_WIFI_PORT}/setup/{code}")
        questionary.print("  Waiting for your phone... (Enter to skip)", style=_DIM)
        input()
    finally:
        server.stop()


def _phone_reached() -> None:
    questionary.print("  ✔ Your phone reached this computer.", style=_GREEN)
    questionary.print("  Tap both buttons on the page, then press Enter.", style=_DIM)


def _print_qr(url: str) -> None:
    code = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L, border=2)
    code.add_data(url)
    code.print_ascii(invert=True)


def _host() -> str:
    """The address a phone on the same Wi-Fi reaches this computer at."""
    if sys.platform == "darwin":
        return f"{socket.gethostname()}.local"
    return lan_address()


def _connect_with_asks(name: str, manifest: dict, opens: str | None) -> None:
    if opens:
        webbrowser.open(opens)
    answers = {}
    for ask in manifest["asks"]:
        prompt = questionary.password if ask["secret"] else questionary.text
        answers[ask["key"]] = _ask(prompt(f"{ask['label']}:"))
    with _spinner(f"Connecting {manifest['label']}"):
        connect_source(name, answers)
        summary = _first_sync(name) if manifest["routes"]["fetch"] else None
    print(f"  Connected {manifest['label']}.")
    if summary:
        print(f"  {summary}")


def _first_sync(name: str) -> str:
    """Fetch once so a rejected key shows now. A failed sync turns the source off and raises."""
    with contextlib.closing(sqlite.connect(config.db_path())) as conn:
        result = sync(conn, config.data_dir(), name, now=to_utc_text(datetime.now(UTC)))
        if result.needs_you:
            with sqlite.transaction(conn):
                conn.execute("UPDATE sources SET enabled = 0 WHERE connector = ?", (name,))
            raise runner.ConnectorUserError(result.needs_you)
    return result.summary
