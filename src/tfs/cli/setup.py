"""`tfs setup`: connect the AI tools and the apps in one guided run, then start TFS."""

import argparse
import contextlib
import io
import itertools
import secrets
import sys
import threading
import webbrowser
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import quote

import questionary
from questionary import Separator, Style

from tfs import config, password_store, providers
from tfs.cli import _tailscale, import_, start
from tfs.cli.connect import connect_source
from tfs.cli.start import print_qr
from tfs.connectors.core import runner
from tfs.connectors.core.contract import load_manifests, to_utc_text
from tfs.importer.session import sync
from tfs.persistence import sqlite
from tfs.persistence.migrations import BackupFailedError, DatabaseTooNewError, migrate
from tfs.proposals import apply, journal
from tfs.providers import mcp_tools
from tfs.web import setup_page
from tfs.web.pairing import wifi_host

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
    BackupFailedError,
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
        first_tool = _connect_ai_tools()
        manifests = load_manifests()
        picked = _ask(
            _checklist(
                "Which apps hold your data?",
                [questionary.Choice(m["label"], value=n) for n, m in manifests.items()],
            )
        )
        phone = _ask(
            questionary.confirm(
                "View TFS on your phone? It uses Tailscale, a free private network for your own devices.",
                default=True,
            )
        )
        phone_url = _tailscale.set_up() if phone else None
        for number, name in enumerate(picked, 1):
            print(f"\n── {manifests[name]['label']} ({number} of {len(picked)})")
            _set_up_app(name, manifests[name], phone_url)
        has_goal = _set_goal()
    except KeyboardInterrupt:
        print("\nSetup stopped.", file=sys.stderr)
        return 1
    except _NO_CONSOLE:
        print("\nRun tfs setup in PowerShell, Windows Terminal or Command Prompt.", file=sys.stderr)
        return 1
    except (DatabaseTooNewError, BackupFailedError) as error:
        print(f"  {error}", file=sys.stderr)
        return 1
    footer = f"Keep this window open. TFS receives phone data only while it runs.\n{_next_step(first_tool, has_goal)}"
    return start.run(
        argparse.Namespace(home_wifi=False, open=True), footer=footer, check_phone=phone
    )


def fill_link(template: str, push_url: str, token: str, route: str = "Wi-Fi") -> str:
    """Fill a manifest link with its encoded push address, token and route."""
    return (
        template.replace("{push_url}", quote(push_url, safe=""))
        .replace("{token}", quote(token, safe=""))
        .replace("{route}", quote(route, safe=""))
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


def _connect_ai_tools() -> providers.Provider | mcp_tools.Tool | None:
    """Connect the tools the person picks. Return the first tool that ends up connected, or None."""
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
        return None
    choices = [_tool_choice(t, programs[t], t in connected) for t in programs]
    if not any(choice.disabled is None for choice in choices):
        for tool in connected:
            questionary.print(f"  ✔ {tool.label:<13} TFS connected", style=_GREEN)
        return next(tool for tool in programs if tool in connected)
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
            connected.add(tool)
            questionary.print(f"  ✔ {tool.label:<13} TFS connected", style=_GREEN)
    return next((tool for tool in programs if tool in connected), None)


def _set_goal() -> bool:
    """Ask for a goal unless one exists, and save it. Return True when a goal exists."""
    config.data_dir().mkdir(parents=True, exist_ok=True)
    today = datetime.now(UTC).astimezone().date()
    with contextlib.closing(sqlite.connect(config.db_path())) as conn:
        migrate(conn)
        goal = conn.execute(
            "SELECT type, local_date FROM goals WHERE local_date >= ? ORDER BY local_date",
            (today.isoformat(),),
        ).fetchone()
        version = journal.version(conn)
    if goal:
        print(f"\nGoal: {goal['type']} on {goal['local_date']}")
        return True
    print()
    name = _ask(
        questionary.text(
            'Name your goal, for example "First powerlifting meet". Press Enter to skip'
        )
    ).strip()
    if not name:
        return False
    day = _ask(questionary.text("What date are you aiming for? (YYYY-MM-DD)", validate=_future_day))
    target = _ask(questionary.text('Any target? Press Enter to skip, for example "Squat 140 kg"'))
    request_id = f"setup-{secrets.token_hex(8)}"
    goal_action = {
        "action": "set_goal",
        "item": request_id,
        "type": name,
        "date": date.fromisoformat(day.strip()).isoformat(),
        "target": {"description": target.strip() or name},
    }
    apply.save(
        config.db_path(),
        {
            "plan_version": version,
            "why": "Set in tfs setup.",
            "sources": [],
            "actions": [goal_action],
        },
        request_id=request_id,
        approved_by="screen",
        now=datetime.now(UTC).astimezone(),
    )
    return True


def _future_day(typed: str) -> bool | str:
    try:
        day = date.fromisoformat(typed.strip())
    except ValueError:
        return "Type the date as YYYY-MM-DD, for example 2027-04-12."
    return day > datetime.now(UTC).astimezone().date() or "Pick a date after today."


def _next_step(tool: providers.Provider | mcp_tools.Tool | None, has_goal: bool) -> str:
    if tool is None:
        return "To connect your AI tool, run tfs setup again."
    if tool.command:
        opening = f"Open a new terminal and run `{tool.command}`."
    else:
        opening = f"Open {tool.label}, or restart it if it is open. Then start a new chat."
    message = providers.PLAN_MESSAGE if has_goal else providers.FIRST_MESSAGE
    return f"{opening}\nPaste this: {message}"


def _tool_choice(
    tool: providers.Provider | mcp_tools.Tool, program: str | None, connected: bool
) -> questionary.Choice:
    if program is None:
        return questionary.Choice(tool.label, value=tool, disabled="not found")
    if connected:
        return questionary.Choice(tool.label, value=tool, disabled="TFS connected")
    tested = "tested" if isinstance(tool, providers.Provider) else "should work"
    return questionary.Choice(f"{tool.label:<22} found, {tested}", value=tool, checked=True)


def _set_up_app(name: str, manifest: dict, phone_url: str | None) -> None:
    setup = manifest.get("setup", {})
    for step in setup.get("steps", []):
        print(f"  {step}")
    try:
        if manifest["routes"]["files"]:
            _import_file(name)
        if manifest["routes"]["push"]:
            _show_links(name, manifest["label"], setup.get("links", []), phone_url)
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


def _show_links(name: str, label: str, links: list[dict], phone_url: str | None) -> None:
    connect_source(name, {})
    token = password_store.get("push_token")
    if not token:
        token = secrets.token_urlsafe(32)
        password_store.set("push_token", token)
    wifi_url = f"http://{wifi_host()}:{config.HOME_WIFI_PORT}/"
    routes = {"Wi-Fi": wifi_url}
    if phone_url:
        routes["Tailscale"] = phone_url
    buttons = [
        (
            f"Add {link['label']} ({route})",
            fill_link(link["url"], f"{url}push/{name}", token, route),
        )
        for route, url in routes.items()
        for link in links
    ]
    code = secrets.token_urlsafe(6)
    if sys.platform == "win32":
        questionary.print("  If Windows asks, allow TFS on private networks.", style=_DIM)
    try:
        server = setup_page.SetupServer(
            _BIND,
            config.HOME_WIFI_PORT,
            code,
            f"Connect {label}",
            buttons,
            _phone_reached,
            tailscale=bool(phone_url),
        )
    except OSError:
        questionary.print("  Close TFS, then run tfs setup again.", style=_RED)
        return
    server.start()
    try:
        print_qr(f"{wifi_url}setup/{code}")
        questionary.print("  Waiting for your phone... (Enter to skip)", style=_DIM)
        input()
    finally:
        server.stop()


def _phone_reached() -> None:
    questionary.print("  ✔ Your phone reached this computer.", style=_GREEN)
    questionary.print("  Tap each button on the page, then press Enter.", style=_DIM)


def _connect_with_asks(name: str, manifest: dict, opens: str | None) -> None:
    if opens:
        webbrowser.open(opens)
    answers = {}
    for ask in manifest["asks"]:
        prompt = questionary.password if ask["secret"] else questionary.text
        answers[ask["key"]] = _ask(prompt(f"{ask['label']}:"))
    with _spinner(f"Connecting {manifest['label']}"):
        connect_source(name, answers)
        arrived, summary = (
            _first_sync(name, manifest["label"]) if manifest["routes"]["fetch"] else (None, None)
        )
    if arrived:
        print(f"  ✔ {arrived}")
        return
    print(f"  Connected {manifest['label']}.")
    if summary:
        print(f"  {summary}")


def _first_sync(name: str, label: str) -> tuple[str | None, str]:
    """Fetch once so a rejected key shows now. A failed sync turns the source off and raises.

    Return what arrived, or None when the app holds no workouts yet, and the sync's own message.
    """
    with contextlib.closing(sqlite.connect(config.db_path())) as conn:
        result = sync(conn, config.data_dir(), name, now=to_utc_text(datetime.now(UTC)))
        if result.needs_you:
            with sqlite.transaction(conn):
                conn.execute("UPDATE sources SET enabled = 0 WHERE connector = ?", (name,))
            raise runner.ConnectorUserError(result.needs_you)
        count, latest = conn.execute(
            "SELECT COUNT(*), MAX(local_date) FROM source_records"
            " WHERE connector = ? AND kind = 'session' AND deleted_at IS NULL",
            (name,),
        ).fetchone()
    if not count:
        return None, result.summary
    day = date.fromisoformat(latest)
    noun = "workout" if count == 1 else "workouts"
    return f"{label}: {count} {noun}, most recent {day:%A} {day.day} {day:%b}", result.summary
