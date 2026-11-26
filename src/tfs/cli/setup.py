"""`tfs setup`: connect the AI tools and the apps in one guided run, then start TFS."""

import argparse
import contextlib
import io
import itertools
import secrets
import sys
import threading
import time
import webbrowser
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime
from functools import cache
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
from tfs.providers import mcp_tools
from tfs.web import setup_page
from tfs.web.pairing import make_pairing_link, wifi_host
from tfs.web.server import HomeWifi

# prompt_toolkit raises this on Windows when the terminal is not a real console, such as Git Bash.
# Its module refuses to import on other systems, so the tuple stays empty there.
_NO_CONSOLE: tuple[type[Exception], ...] = ()
if sys.platform == "win32":
    from prompt_toolkit.output.win32 import NoConsoleScreenBufferError

    _NO_CONSOLE = (NoConsoleScreenBufferError,)

_SCAN_HINT_S = 30
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
        picked = _pick_apps(manifests)

        @cache
        def offer_tailscale() -> str | None:
            return _tailscale.set_up(
                lambda text, default: _ask(questionary.confirm(text, default=default)), print_qr
            )

        def finish_setup(wifi: HomeWifi) -> tuple[str, str | None]:
            for number, name in enumerate(picked, 1):
                print(f"\n── {manifests[name]['label']} ({number} of {len(picked)})")
                _set_up_app(name, manifests[name], wifi, offer_tailscale)
            message = _goal_message()
            phone_url = _phone_setup(manifests, wifi, offer_tailscale)
            footer = f"Keep this window open. TFS receives phone data only while it runs.\n{_next_step(first_tool, message)}"
            return footer, phone_url

        return start.run(argparse.Namespace(home_wifi=True, open=True), finish_setup=finish_setup)
    except KeyboardInterrupt:
        print("\nSetup stopped.", file=sys.stderr)
        return 1
    except _NO_CONSOLE:
        print("\nRun tfs setup in PowerShell, Windows Terminal or Command Prompt.", file=sys.stderr)
        return 1
    except (DatabaseTooNewError, BackupFailedError) as error:
        print(f"  {error}", file=sys.stderr)
        return 1


def fill_link(template: str, push_url: str, token: str, route: str = "Wi-Fi") -> str:
    """Fill a manifest link with its encoded push address, token and route."""
    return (
        template.replace("{push_url}", quote(push_url, safe=""))
        .replace("{token}", quote(token, safe=""))
        .replace("{route}", quote(route, safe=""))
    )


def _checklist(message: str, choices: list, **options) -> questionary.Question:
    return questionary.checkbox(
        message,
        choices=[*choices, _CHECKLIST_HINT],
        instruction="",
        style=_CHECKLIST_STYLE,
        **options,
    )


def _pick_apps(manifests: dict) -> list[str]:
    connected = {}
    if config.db_path().is_file():
        with contextlib.closing(sqlite.connect(config.db_path(), "read_only")) as conn:
            connected = dict(
                conn.execute("SELECT connector, last_received_at FROM sources WHERE enabled = 1")
            )
    kept = set(connected)

    def track_ticks(selected: list[str]) -> bool:
        # The checklist validates every toggle. Only untouched ticks can skip setup.
        kept.intersection_update(selected)
        return True

    choices = []
    for name, manifest in manifests.items():
        if name in connected:
            received = connected[name]
            detail = (
                f"connected, last data {_ago(received)}" if received else "connected, no data yet"
            )
        else:
            needs = [
                step.rstrip(".")
                for step in manifest.get("setup", {}).get("steps", [])
                if step.startswith("Needs ")
            ]
            needs.extend(ask["label"] for ask in manifest["asks"])
            if manifest["routes"]["files"]:
                needs.append("Export file from your phone")
            detail = ", ".join(needs)
        choices.append(
            questionary.Choice(
                f"{manifest['label']:<22} {detail}", value=name, checked=name in connected
            )
        )
    picked = _ask(
        _checklist(
            "Which apps hold your data?", choices, validate=track_ticks, erase_when_done=True
        )
    )
    for name in picked:
        questionary.print(f"  ✔ {manifests[name]['label']}", style=_GREEN)
    fresh = [name for name in picked if name not in kept]
    if not fresh:
        questionary.print("  No new apps", style=_DIM)
    return fresh


def _ago(received: str) -> str:
    minutes = max(
        0, int((datetime.now(UTC) - datetime.fromisoformat(received)).total_seconds() // 60)
    )
    if minutes < 60:
        return f"{minutes} min ago"
    if minutes < 1440:
        return f"{minutes // 60} hr ago"
    return f"{minutes // 1440} d ago"


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


def _goal_message() -> str:
    """Choose the first chat message from a saved goal or one typed during setup."""
    config.data_dir().mkdir(parents=True, exist_ok=True)
    today = datetime.now(UTC).astimezone().date()
    with contextlib.closing(sqlite.connect(config.db_path())) as conn:
        migrate(conn)
        goal = conn.execute(
            "SELECT type, local_date FROM goals WHERE local_date >= ? ORDER BY local_date",
            (today.isoformat(),),
        ).fetchone()
    if goal:
        print(f"\nGoal: {goal['type']} on {goal['local_date']}")
        return providers.PLAN_MESSAGE
    print()
    name = _ask(
        questionary.text(
            "What are you training for? Press Enter to skip",
            instruction=(
                "\n  Local powerlifting meet in June, squat 140 kg"
                "\n  Lose 10 kg in 6 months"
                "\n  Sub 90 minute Hyrox Sydney 2027"
                "\n  Sub 3:30 Auckland Marathon, October 2027"
                "\n  Olympic triathlon in February, under 2:45"
                "\n  Physique show on 14 March 2027"
                "\n  Run 5 km without stopping by summer\n"
            ),
            style=Style([("instruction", _DIM)]),
        )
    ).strip()
    if not name:
        return providers.FIRST_MESSAGE
    return (
        f"Use TFS to set up my goal: {name}. Look up anything you need, "
        "then build my first training plan. Ask me one question at a time."
    )


def _next_step(tool: providers.Provider | mcp_tools.Tool | None, message: str) -> str:
    if tool is None:
        opening = "To connect your AI tool, run tfs setup again."
        if message in (providers.FIRST_MESSAGE, providers.PLAN_MESSAGE):
            return opening
    elif tool.command:
        opening = f"Open a new terminal and run `{tool.command}`."
    else:
        opening = f"Open {tool.label}, or restart it if it is open. Then start a new chat."
    return f"{opening}\nPaste this: {message}"


def _enter_within_second() -> bool:
    if sys.platform == "win32":
        import msvcrt

        time.sleep(1)
        while msvcrt.kbhit():
            if msvcrt.getwch() == "\r":
                return True
        return False
    import select

    if select.select([sys.stdin], [], [], 1)[0]:
        sys.stdin.readline()
        return True
    return False


def _wait_for_data(manifests: dict) -> None:
    with contextlib.closing(sqlite.connect(config.db_path(), "read_only")) as conn:
        if not conn.execute("SELECT 1 FROM sources WHERE enabled = 1 LIMIT 1").fetchone():
            return
        print("Waiting for your first data... (Enter to skip)", flush=True)
        while True:
            records = conn.execute(
                "SELECT DISTINCT connector, COALESCE(metric, kind) AS reading FROM source_records"
                " WHERE deleted_at IS NULL AND connector = ("
                " SELECT connector FROM source_records JOIN sources USING (connector)"
                " WHERE enabled = 1 AND deleted_at IS NULL ORDER BY arrival_seq LIMIT 1)"
            ).fetchall()
            if records:
                names = {
                    "session": "workouts",
                    "event": "events",
                    "document": "documents",
                    "resting_hr": "resting heart rate",
                    "hrv_sdnn": "heart rate variability",
                    "hrv_rmssd": "heart rate variability",
                }
                readings = {
                    "sleep" if row["reading"].startswith("sleep_") else row["reading"]
                    for row in records
                }
                labels = sorted({names.get(name, name.replace("_", " ")) for name in readings})
                arrived = (
                    " and ".join([", ".join(labels[:-1]), labels[-1]])
                    if len(labels) > 1
                    else labels[0]
                )
                source = records[0]["connector"]
                label = manifests.get(source, {}).get("label", source)
                print(f"  ✔ {arrived.capitalize()} arrived from {label}.")
                return
            if _enter_within_second():
                return


def _wait_for_scan(reached: threading.Event, offer_tailscale: Callable[[], str | None]) -> bool:
    deadline = time.monotonic() + _SCAN_HINT_S
    hinted = False
    while not reached.is_set():
        if not hinted and time.monotonic() >= deadline:
            print("Your phone has not reached TFS. Check both are on the same Wi-Fi.", flush=True)
            offer_tailscale()
            if not reached.is_set():
                print("Waiting for your phone... (Enter to skip)", flush=True)
            hinted = True
            continue
        if _enter_within_second():
            return False
    return True


def _phone_setup(
    manifests: dict, wifi: HomeWifi, offer_tailscale: Callable[[], str | None]
) -> str | None:
    _wait_for_data(manifests)
    print(
        "\nView TFS on your phone\n  Scan this code with your phone on this Wi-Fi."
        "\n  Only phones that scan it can open TFS."
    )
    with contextlib.closing(sqlite.connect(config.db_path(), "read_only")) as conn:
        link = make_pairing_link()
        reached = threading.Event()
        wifi.pairing_scan = (link.rsplit("/", 1)[1], reached)
        try:
            print_qr(link)
            print("Waiting for your phone... (Enter to skip)", flush=True)
            _wait_for_scan(reached, offer_tailscale)
        finally:
            wifi.pairing_scan = None
        phone_url = offer_tailscale()
        if not phone_url:
            return None
        print("Scan to open TFS anywhere. Add it to your home screen.")
        print_qr(phone_url)
        for (name,) in conn.execute("SELECT connector FROM sources WHERE enabled = 1"):
            manifest = manifests.get(name, {})
            links = manifest.get("setup", {}).get("links")
            if links:
                label = manifest["label"]
                print(
                    f"You use {label}. Add its Tailscale automations so your data also arrives away from home."
                )
                _show_links(name, label, links, wifi, offer_tailscale, phone_url)
        return phone_url


def _tool_choice(
    tool: providers.Provider | mcp_tools.Tool, program: str | None, connected: bool
) -> questionary.Choice:
    if program is None:
        return questionary.Choice(tool.label, value=tool, disabled="not found")
    if connected:
        return questionary.Choice(tool.label, value=tool, disabled="TFS connected")
    tested = "tested" if isinstance(tool, providers.Provider) else "should work"
    return questionary.Choice(f"{tool.label:<22} found, {tested}", value=tool, checked=True)


def _set_up_app(
    name: str, manifest: dict, wifi: HomeWifi, offer_tailscale: Callable[[], str | None]
) -> None:
    setup = manifest.get("setup", {})
    for step in setup.get("steps", []):
        print(f"  {step}")
    try:
        if manifest["routes"]["files"]:
            _import_file(name)
        if manifest["routes"]["push"]:
            connect_source(name, {})
            _show_links(name, manifest["label"], setup.get("links", []), wifi, offer_tailscale)
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


def _show_links(
    name: str,
    label: str,
    links: list[dict],
    wifi: HomeWifi,
    offer_tailscale: Callable[[], str | None],
    phone_url: str | None = None,
) -> None:
    token = password_store.get("push_token")
    if not token:
        token = secrets.token_urlsafe(32)
        password_store.set("push_token", token)
    wifi_url = f"http://{wifi_host()}:{config.HOME_WIFI_PORT}/"
    route = "Tailscale" if phone_url else "Wi-Fi"
    buttons = [
        (
            f"Add {link['label']} ({route})",
            fill_link(link["url"], f"{phone_url or wifi_url}push/{name}", token, route),
        )
        for link in links
    ]
    code = secrets.token_urlsafe(6)
    page = setup_page.render_page(f"Connect {label}", buttons, tailscale=bool(phone_url))
    with setup_page.show(wifi, code, page, _phone_reached) as reached:
        print_qr(f"{wifi_url}setup/{code}")
        questionary.print("  Waiting for your phone... (Enter to skip)", style=_DIM)
        if _wait_for_scan(reached, offer_tailscale):
            input()


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
