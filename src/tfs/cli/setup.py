"""`tfs setup`: connect the AI tools and the apps in one guided run, then start TFS."""

import argparse
import secrets
import socket
import sys
import webbrowser
from pathlib import Path
from urllib.parse import quote

import qrcode
import questionary

from tfs import config, password_store, providers
from tfs.cli import import_, start
from tfs.cli.connect import connect_source
from tfs.connectors.core import runner
from tfs.connectors.core.contract import load_manifests
from tfs.persistence.migrations import DatabaseTooNewError

# prompt_toolkit raises this on Windows when the terminal is not a real console, such as Git Bash.
# Its module refuses to import on other systems, so the tuple stays empty there.
_NO_CONSOLE: tuple[type[Exception], ...] = ()
if sys.platform == "win32":
    from prompt_toolkit.output.win32 import NoConsoleScreenBufferError

    _NO_CONSOLE = (NoConsoleScreenBufferError,)

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
            questionary.checkbox(
                "Which apps hold your data? (space to pick)",
                choices=[questionary.Choice(m["label"], value=n) for n, m in manifests.items()],
            )
        )
        for number, name in enumerate(picked, 1):
            print(f"\n── {manifests[name]['label']} ({number} of {len(picked)})")
            _set_up_app(name, manifests[name])
        if not _ask(questionary.confirm("Start TFS and open the dashboard?", default=True)):
            return 0
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


def _ask(question: questionary.Question):
    answer = question.ask()
    if answer is None:
        raise KeyboardInterrupt
    return answer


def _connect_ai_tools() -> None:
    print("AI tools")
    found = 0
    for provider in providers.PROVIDERS:
        program = providers.find(provider)
        if program is None:
            print(f"  ✘ {provider.label:<13} not found")
            continue
        found += 1
        connected = providers.is_connected(program) or _offer_connect(program, provider)
        status = "found, TFS connected" if connected else "found, TFS not connected"
        print(f"  {'✔' if connected else '✘'} {provider.label:<13} {status}")
    if not found:
        print("TFS needs Claude Code or Codex. Install one, then run tfs setup again.")


def _offer_connect(program: str, provider: providers.Provider) -> bool:
    if not _ask(questionary.confirm(f"Connect TFS to {provider.label}?", default=True)):
        return False
    problem = providers.connect(program, provider)
    if problem:
        print(f"  Could not connect {provider.label}: {problem}")
    return problem is None


def _set_up_app(name: str, manifest: dict) -> None:
    setup = manifest.get("setup", {})
    for step in setup.get("steps", []):
        print(f"  {step}")
    try:
        if manifest["routes"]["files"]:
            _import_file(name)
        if manifest["routes"]["push"]:
            _show_links(name, setup.get("links", []))
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
    if path:
        import_.run(argparse.Namespace(path=Path(path), connector=name))


def _show_links(name: str, links: list[dict]) -> None:
    connect_source(name, {})
    token = password_store.get("push_token")
    if not token:
        token = secrets.token_urlsafe(32)
        password_store.set("push_token", token)
    push_url = f"http://{_host()}:{config.HOME_WIFI_PORT}/push/{name}"
    for number, link in enumerate(links, 1):
        url = fill_link(link["url"], push_url, token)
        print(f"\n  {link['label']}")
        code = qrcode.QRCode()
        code.add_data(url)
        code.print_ascii(invert=True)
        print(url)
        if number < len(links):
            input("Press Enter, then scan the next one.")


def _host() -> str:
    """The address a phone on the same Wi-Fi reaches this computer at."""
    if sys.platform == "darwin":
        return f"{socket.gethostname()}.local"
    # A UDP connect sends nothing. It only makes the OS pick the Wi-Fi address.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect(("192.0.2.1", 9))
        except OSError:
            return socket.gethostbyname(socket.gethostname())
        return probe.getsockname()[0]


def _connect_with_asks(name: str, manifest: dict, opens: str | None) -> None:
    if opens:
        webbrowser.open(opens)
    answers = {}
    for ask in manifest["asks"]:
        prompt = questionary.password if ask["secret"] else questionary.text
        answers[ask["key"]] = _ask(prompt(f"{ask['label']}:"))
    connect_source(name, answers)
    print(f"  Connected {manifest['label']}.")
