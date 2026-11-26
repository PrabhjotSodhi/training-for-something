import argparse
import contextlib
import http.client
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import qrcode
import questionary
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from tests.test_setup_page import Hrefs
from tfs import config, password_store, providers
from tfs.cli import _tailscale, main, setup, start
from tfs.connectors.core.contract import load_manifests
from tfs.importer.raw_store import save_raw
from tfs.importer.session import read_file
from tfs.persistence import sqlite
from tfs.persistence.migrations import migrate
from tfs.providers import mcp_tools
from tfs.web.server import HomeWifi, make_server

REPO = "PrabhjotSodhi/training-for-something"
PLUGIN = "tfs@training-for-something"
CLAUDE, CODEX = providers.PROVIDERS
CURSOR = mcp_tools.TOOLS[0]
ANTIGRAVITY = next(t for t in mcp_tools.TOOLS if t.command == "agy")
PUSH_URL = "http://studio.local:47824/push/health_auto_export"


class FakeQuestionary:
    """Answers each question from a list, in order, then Enter on every text prompt."""

    Choice = mock.Mock(
        side_effect=lambda title, value, disabled=None, checked=False: mock.Mock(disabled=disabled)
    )

    def __init__(self, *answers, phone=False):
        self.answers = deque([*answers, ""])
        self.phone = iter(phone) if isinstance(phone, list) else None
        self.default_phone = phone

    def confirm(self, message, default=True):
        self.print(message)
        return mock.Mock(ask=lambda: next(self.phone) if self.phone else self.default_phone)

    @staticmethod
    def print(text, style=None):
        sys.stdout.write(f"{text}\n")

    def __getattr__(self, kind):
        def question(message, validate=lambda answer: True, **options):
            self.print(message)
            if options.get("instruction"):
                self.print(options["instruction"])
            answer = self.answers.popleft()
            while (problem := validate(answer)) is not True:
                sys.stdout.write(f"{problem}\n")
                answer = self.answers.popleft()
            return mock.Mock(ask=lambda: answer)

        return question


class FakeCommands:
    """Stands in for the claude and codex programs and records every command."""

    def __init__(self, found, plugin_list="", mcp_list=""):
        self.found = found
        self.plugin_list = plugin_list
        self.mcp_list = mcp_list
        self.calls = []

    def which(self, command):
        return command if command in self.found else None

    def run(self, argv):
        self.calls.append(list(argv))
        if argv[1:3] == ["plugin", "list"]:
            return True, self.plugin_list
        return True, self.mcp_list if argv[1:3] == ["mcp", "list"] else ""


@contextlib.contextmanager
def wifi_listener():
    wifi = HomeWifi(config.db_path())
    server = make_server("127.0.0.1", 0, push_handler=None, home_wifi=wifi)
    server.start()
    try:
        with mock.patch.object(config, "HOME_WIFI_PORT", server.sock.getsockname()[1]):
            yield wifi
    finally:
        server.stop()


def run_without_engine(args, *, finish_setup):
    with mock.patch.object(setup, "_phone_setup"):
        footer, _ = finish_setup(HomeWifi(config.db_path()))
        sys.stdout.write(footer)
    return 0


class TfsHomeTestCase(unittest.TestCase):
    def setUp(self):
        self.powershell = self.enterContext(
            mock.patch.object(start, "_powershell", return_value='{"allowed":true,"blocks":[]}')
        )
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        environment = mock.patch.dict(os.environ, {"TFS_HOME": home.name})
        environment.start()
        self.addCleanup(environment.stop)
        self.home = Path(home.name)
        fake_home = mock.patch("pathlib.Path.home", return_value=self.home)
        fake_home.start()
        self.addCleanup(fake_home.stop)


class SetupTest(TfsHomeTestCase):
    def test_python_block_skips_admin_and_pairing_and_offers_tailscale(self):
        # GIVEN the Python block wins even when the TFS rule is on
        self.powershell.return_value = '{"allowed":true,"blocks":["Example Python"]}'

        def finish(args, *, finish_setup):
            finish_setup(HomeWifi(config.db_path()))
            return 0

        with (
            mock.patch.object(sys, "platform", "win32"),
            mock.patch.object(start, "_elevate_firewall") as prompt,
            mock.patch.object(setup, "make_pairing_link", side_effect=AssertionError) as pairing,
            mock.patch.object(_tailscale, "_program", return_value=None),
            mock.patch.object(setup, "_wait_for_data"),
        ):
            # WHEN setup reaches phone viewing
            code, out, _, _ = self.run_setup(
                FakeQuestionary([]), FakeCommands(set()), finish=finish
            )
        # THEN setup offers Tailscale without asking for admin or making a pairing code
        self.assertIn(
            "Windows is blocking Python on home networks, so your phone cannot reach TFS over Wi-Fi.",
            out,
        )
        self.assertEqual(code, 0)
        self.assertIn("Tailscale works anywhere", out)
        prompt.assert_not_called()
        pairing.assert_not_called()

    def run_setup(self, questionary, commands, *, tty=True, finish=run_without_engine):
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch("sys.stdin", mock.Mock(isatty=lambda: tty)),
            mock.patch("tfs.cli.setup.questionary", questionary),
            mock.patch("tfs.providers.shutil.which", commands.which),
            mock.patch("tfs.providers.run_command", commands.run),
            mock.patch("tfs.cli.setup.webbrowser.open"),
            mock.patch("tfs.cli.setup.start.run", side_effect=finish) as started,
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = main(["setup"])
        return code, out.getvalue(), err.getvalue(), started

    def test_given_claude_not_connected_when_accepted_then_both_commands_run_in_order(self):
        commands = FakeCommands(found={"claude"})
        code, out, _, _ = self.run_setup(FakeQuestionary([CLAUDE], []), commands)
        self.assertEqual(code, 0)
        self.assertEqual(
            commands.calls,
            [
                ["claude", "plugin", "list"],
                ["claude", "plugin", "marketplace", "add", REPO],
                ["claude", "plugin", "install", PLUGIN],
            ],
        )
        self.assertIn("✔ Claude Code   TFS connected", out)

    def test_given_claude_already_connected_then_nothing_is_installed(self):
        commands = FakeCommands(found={"claude"}, plugin_list=f"  {PLUGIN}\n")
        _, out, _, _ = self.run_setup(FakeQuestionary([]), commands)
        self.assertEqual(commands.calls, [["claude", "plugin", "list"]])
        self.assertIn("✔ Claude Code   TFS connected", out)
        self.assertIn("run `claude`", out)
        self.assertIn(providers.FIRST_MESSAGE, out)

    def test_given_a_tool_left_unpicked_then_nothing_is_installed_for_it(self):
        commands = FakeCommands(found={"claude", "codex"})
        self.run_setup(FakeQuestionary([CODEX], []), commands)
        self.assertNotIn(["claude", "plugin", "install", PLUGIN], commands.calls)

    def test_given_codex_not_connected_then_codex_uses_plugin_add(self):
        commands = FakeCommands(found={"codex"})
        self.run_setup(FakeQuestionary([CODEX], []), commands)
        self.assertEqual(commands.calls[-1], ["codex", "plugin", "add", PLUGIN])

    def cursor_config(self, text):
        folder = self.home / ".cursor"
        folder.mkdir()
        (folder / "mcp.json").write_text(text, encoding="utf-8")
        return folder

    def snapshot(self, folder):
        return {p.name: p.read_bytes() for p in folder.iterdir()}

    def test_given_a_config_with_other_servers_when_setup_adds_tfs_then_they_stay_and_a_backup_exists(
        self,
    ):
        others = {"files": {"command": "npx", "args": ["files"]}}
        original = json.dumps({"theme": "dark", "mcpServers": others})
        folder = self.cursor_config(original)
        self.run_setup(FakeQuestionary([CURSOR], []), FakeCommands(found=set()))
        saved = json.loads((folder / "mcp.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["theme"], "dark")
        self.assertEqual({k: saved["mcpServers"][k] for k in others}, others)
        self.assertIn("tfs", saved["mcpServers"])
        backups = [p for p in folder.iterdir() if p.name != "mcp.json"]
        self.assertEqual([p.read_text(encoding="utf-8") for p in backups], [original])

    def test_given_setup_ran_once_when_it_runs_again_then_no_file_changes(self):
        folder = self.cursor_config("{}")
        self.run_setup(FakeQuestionary([CURSOR], []), FakeCommands(found=set()))
        after_first = self.snapshot(folder)
        commands = FakeCommands(found={"opencode"}, mcp_list="tfs  connected")
        self.run_setup(FakeQuestionary([]), commands)
        self.assertEqual(self.snapshot(folder), after_first)
        self.assertNotIn("add", [word for call in commands.calls for word in call])

    def test_given_a_tfs_entry_with_another_command_then_setup_reports_it_and_leaves_it(self):
        # GIVEN an existing entry with a private token
        entry = {"command": "/opt/other-tfs", "env": {"TOKEN": "invented-secret"}}
        folder = self.cursor_config(json.dumps({"mcpServers": {"tfs": entry}}))
        before = self.snapshot(folder)
        # WHEN setup finds the conflict
        _, out, err, _ = self.run_setup(FakeQuestionary([CURSOR], []), FakeCommands(found=set()))
        # THEN it identifies the entry without showing its contents
        self.assertEqual(self.snapshot(folder), before)
        self.assertNotIn(entry["env"]["TOKEN"], out + err)
        self.assertNotIn(entry["command"], out + err)
        self.assertIn(str(folder / "mcp.json"), out)
        self.assertIn("tfs entry", out)

    def test_given_no_supported_tools_then_setup_names_them_and_continues(self):
        code, out, _, started = self.run_setup(FakeQuestionary([]), FakeCommands(found=set()))
        labels = [p.label for p in providers.PROVIDERS] + [t.label for t in mcp_tools.TOOLS]
        for label in labels:
            self.assertIn(label, out)
        self.assertEqual(code, 0)
        started.assert_called_once()
        self.assertIn("run tfs setup again", out)
        self.assertNotIn(providers.FIRST_MESSAGE, out)

    def test_given_a_tool_with_a_command_and_a_file_when_setup_connects_it_then_the_file_holds_tfs(
        self,
    ):
        # GIVEN Antigravity's program is found and it has no config file
        commands = FakeCommands(found={"agy"})
        # WHEN setup connects it
        self.run_setup(FakeQuestionary([ANTIGRAVITY], []), commands)
        # THEN the config file holds the tfs entry and the program was not run
        saved = json.loads((self.home / ANTIGRAVITY.file).read_text(encoding="utf-8"))
        self.assertIn("tfs", saved["mcpServers"])
        self.assertEqual(commands.calls, [])

    def test_given_the_shipped_manifest_then_the_links_are_exact(self):
        links = load_manifests()["health_auto_export"]["setup"]["links"]
        filled = [setup.fill_link(link["url"], PUSH_URL, "tok-invented") for link in links]
        self.assertEqual(
            filled,
            [
                "com.HealthExport://automation?url=http%3A%2F%2Fstudio.local%3A47824"
                "%2Fpush%2Fhealth_auto_export&name=TFS%20metrics%20%28Wi-Fi%29&datatype=healthMetrics"
                "&format=json&exportversion=v2&period=lastsync&aggregatesleep=true"
                "&headers=Authorization,Bearer%20tok-invented",
                "com.HealthExport://automation?url=http%3A%2F%2Fstudio.local%3A47824"
                "%2Fpush%2Fhealth_auto_export&name=TFS%20workouts%20%28Wi-Fi%29&datatype=workouts"
                "&format=json&exportversion=v2&period=lastsync"
                "&headers=Authorization,Bearer%20tok-invented",
            ],
        )

    def test_given_a_quoted_path_then_the_import_receives_it_without_quotes(self):
        typed = '"C:\\Users\\ana\\Downloads\\export (2).zip"'
        questionary = FakeQuestionary(["apple_health"], typed)
        with mock.patch("tfs.cli.setup.import_.run", return_value=0) as imported:
            self.run_setup(questionary, FakeCommands(found=set()))
        namespace = imported.call_args.args[0]
        self.assertEqual(str(namespace.path), "C:\\Users\\ana\\Downloads\\export (2).zip")
        self.assertEqual(namespace.connector, "apple_health")

    def test_given_enter_on_the_file_question_then_nothing_is_imported(self):
        questionary = FakeQuestionary(["apple_health"], "")
        with mock.patch("tfs.cli.setup.import_.run") as imported:
            self.run_setup(questionary, FakeCommands(found=set()))
        imported.assert_not_called()

    def test_enter_keeps_connected_app_and_reticking_reopens_its_steps(self):
        # GIVEN an enabled app with a recent arrival
        name = "health_auto_export"
        arrived = datetime.now(UTC) - timedelta(minutes=2)
        with contextlib.closing(sqlite.connect(config.db_path())) as conn:
            migrate(conn)
            conn.execute(
                "INSERT INTO sources (connector, last_received_at) VALUES (?, ?)",
                (name, arrived.isoformat().replace("+00:00", "Z")),
            )
        prompts = FakeQuestionary("", "")
        prompts.Choice = questionary.Choice

        def checkbox(message, choices, **options):
            app = next(c for c in choices if c.value == name)
            self.assertIn("connected, last data 2 min ago", app.title)
            return questionary.checkbox(message, choices, initial_choice=app, **options)

        prompts.checkbox = checkbox
        for retick in (False, True):
            # WHEN the person accepts the defaults or unticks and reticks the app
            with (
                self.subTest(retick=retick),
                create_pipe_input() as keys,
                create_app_session(input=keys, output=DummyOutput()),
                mock.patch.object(setup, "_set_up_app") as steps,
            ):
                keys.send_text("  \r" if retick else "\r")
                _, out, _, _ = self.run_setup(prompts, FakeCommands(found=set()))
                # THEN only a fresh tick runs setup
                self.assertEqual(steps.call_count, int(retick))
                self.assertIn("✔ Health Auto Export", out)
                self.assertEqual("No new apps" in out, not retick)

    def test_given_a_terminal_that_is_not_a_windows_console_then_it_names_one_and_exits_1(self):
        class NoConsoleError(Exception):
            pass

        questionary = FakeQuestionary()
        questionary.checkbox = mock.Mock(side_effect=NoConsoleError)
        with mock.patch("tfs.cli.setup._NO_CONSOLE", (NoConsoleError,)):
            code, _, err, _ = self.run_setup(questionary, FakeCommands(found=set()))
        self.assertEqual(code, 1)
        self.assertIn("Run tfs setup in PowerShell, Windows Terminal or Command Prompt.", err)

    def test_given_no_terminal_then_it_says_so_and_exits_1(self):
        code, out, err, _ = self.run_setup(FakeQuestionary(), FakeCommands(found=set()), tty=False)
        self.assertEqual(code, 1)
        self.assertEqual(out + err, "Run tfs setup in a terminal.\n")

    def run_goal_setup(self, *goal_answers):
        commands = FakeCommands(found={"claude"}, plugin_list=f"  {PLUGIN}\n")
        return self.run_setup(FakeQuestionary([], *goal_answers), commands)

    def stored_goals(self):
        with contextlib.closing(sqlite.connect(config.db_path())) as conn:
            return [tuple(row) for row in conn.execute("SELECT type, local_date FROM goals")]

    def test_typed_goal_goes_in_the_paste_message_without_saving(self):
        # GIVEN one goal answer
        prompts = FakeQuestionary([])
        prompts.text = mock.Mock(return_value=mock.Mock(ask=lambda: "Hike the coast track"))
        # WHEN setup finishes
        _, out, _, _ = self.run_setup(prompts, FakeCommands({"claude"}, plugin_list=PLUGIN))
        # THEN setup asked once and passed the goal to the AI
        prompts.text.assert_called_once()
        self.assertEqual(self.stored_goals(), [])
        self.assertIn(
            "Paste this: Use TFS to set up my goal: Hike the coast track. Look up anything "
            "you need, then build my first training plan. Ask me one question at a time.",
            out,
        )

    def test_given_an_empty_name_when_setup_finishes_then_no_goal_is_stored(self):
        # GIVEN the person presses Enter on the name
        # WHEN setup finishes
        _, out, _, _ = self.run_goal_setup("")
        # THEN no goal is stored
        self.assertEqual(self.stored_goals(), [])
        self.assertIn(f"Paste this: {providers.FIRST_MESSAGE}", out)
        self.assertIn(
            "What are you training for? Press Enter to skip\n\n"
            "  Local powerlifting meet in June, squat 140 kg\n"
            "  Lose 10 kg in 6 months\n"
            "  Sub 90 minute Hyrox Sydney 2027\n"
            "  Sub 3:30 Auckland Marathon, October 2027\n"
            "  Olympic triathlon in February, under 2:45\n"
            "  Physique show on 14 March 2027\n"
            "  Run 5 km without stopping by summer\n",
            out,
        )

    def test_given_a_saved_goal_when_setup_finishes_then_the_next_step_asks_for_a_first_plan(self):
        # GIVEN a saved goal with a future date
        config.data_dir().mkdir(parents=True, exist_ok=True)
        with contextlib.closing(sqlite.connect(config.db_path())) as conn:
            migrate(conn)
            conn.execute(
                "INSERT INTO goals (id, type, local_date, target_json)"
                " VALUES ('walk', 'Hike the coast track', '2099-03-14', '{}')"
            )
        # WHEN setup runs again
        _, out, _, _ = self.run_goal_setup()
        # THEN it names the goal and the next step asks for a first training plan
        self.assertNotIn("What are you training for?", out)
        self.assertIn("Goal: Hike the coast track on 2099-03-14", out)
        self.assertIn(providers.PLAN_MESSAGE, out)
        self.assertNotIn(providers.FIRST_MESSAGE, out)


class FirstDataTest(TfsHomeTestCase):
    def test_wait_names_the_readings_committed_between_polls(self):
        # GIVEN a connected app with no readings
        source = "health_auto_export"
        with contextlib.closing(sqlite.connect(config.db_path())) as conn:
            migrate(conn)
            conn.execute("INSERT INTO sources (connector) VALUES (?)", (source,))

            def receive():
                fixtures = Path(__file__).parent / "fixtures" / source
                metrics = json.loads((fixtures / "metrics.json").read_text())["data"]["metrics"]
                data = json.loads((fixtures / "workouts.json").read_text())["data"]
                data["metrics"] = [next(m for m in metrics if m["name"] == "step_count")]
                raw = save_raw(
                    conn,
                    self.home,
                    source,
                    "rest_json",
                    "json",
                    io.BytesIO(json.dumps({"data": data}).encode()),
                    now="2030-01-01T00:00:00Z",
                )
                read_file(conn, self.home, raw.sha256)
                return False

            # WHEN Enter skips an empty wait, then a later wait receives a push
            out = io.StringIO()
            with (
                contextlib.redirect_stdout(out),
                mock.patch.object(setup, "_enter_within_second", side_effect=[True]),
            ):
                setup._wait_for_data(load_manifests())
            self.assertNotIn("arrived", out.getvalue())
            with (
                contextlib.redirect_stdout(out),
                mock.patch.object(setup, "_enter_within_second", side_effect=receive),
            ):
                setup._wait_for_data(load_manifests())
        # THEN setup names the two types of stored records
        self.assertIn("✔ Steps and workouts arrived from Health Auto Export.", out.getvalue())


class TailscaleRunnerTest(unittest.TestCase):
    def test_mac_command_runs_in_cli_mode(self):
        # GIVEN the bundled macOS program
        program = str(_tailscale._INSTALL_PATHS["darwin"])
        with (
            mock.patch.object(sys, "platform", "darwin"),
            mock.patch.object(
                subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b"{}", b"")
            ) as run,
        ):
            # WHEN the runner reads status
            self.assertEqual(_tailscale._run(program, "status", "--json"), "{}")
        # THEN the child runs in CLI mode
        self.assertEqual(run.call_args.kwargs["env"]["TAILSCALE_BE_CLI"], "1")

    def test_timeout_keeps_only_a_printed_approval_link(self):
        # GIVEN a fake command that hangs silently or after printing the approval link
        link = "https://login.tailscale.com/f/serve?node=invented"
        for output, error, expected in (
            (None, None, None),
            (None, link.encode(), link),
            (f"Approval needed\n  {link}\n".encode(), None, link),
        ):
            with (
                self.subTest(output=output, error=error),
                mock.patch.object(
                    subprocess, "run", side_effect=subprocess.TimeoutExpired([], 5, output, error)
                ) as run,
            ):
                # WHEN setup tries to serve the dashboard
                result = _tailscale._run("invented-cli", "serve", "--bg", "http://127.0.0.1:47823")
                # THEN it uses a timeout and returns only an available approval link
                self.assertEqual(result, expected)
                self.assertGreater(run.call_args.kwargs["timeout"], 0)


class TailscaleTest(TfsHomeTestCase):
    host = "laptop.example-tailnet.ts.net"
    target = "http://127.0.0.1:47823"

    def setUp(self):
        super().setUp()
        self.state = {"BackendState": "Running", "Self": {"DNSName": self.host + "."}}
        self.routes = {}
        self.approval = ""
        self.which = self.enterContext(mock.patch.object(shutil, "which", return_value="tailscale"))
        self.commands = self.enterContext(
            mock.patch("tfs.cli._tailscale._run", side_effect=self.run_command)
        )
        self.enter = self.enterContext(mock.patch("builtins.input", return_value=""))
        self.enterContext(mock.patch.object(setup, "_enter_within_second", return_value=True))
        self.browser = self.enterContext(mock.patch.object(start.webbrowser, "open"))

    def route(self, target, port=443):
        return {
            "TCP": {str(port): {"HTTPS": True}},
            "Web": {f"{self.host}:{port}": {"Handlers": {"/": {"Proxy": target}}}},
        }

    def run_command(self, program, *args):
        if args == ("status", "--json"):
            return json.dumps(self.state) if self.state else None
        if args == ("serve", "status", "--json"):
            return json.dumps(self.routes)
        self.assertEqual(args, ("serve", "--bg", self.target))
        if self.approval:
            return self.approval
        self.routes = self.route(self.target)
        return "Available within your tailnet"

    def run_cli(self, command="setup", phone=True, apps=()):
        output = io.StringIO()
        with (
            mock.patch("sys.stdin", mock.Mock(isatty=lambda: True)),
            mock.patch.object(setup, "_connect_ai_tools", return_value=None),
            mock.patch.object(setup, "questionary", FakeQuestionary(apps, phone=phone)),
            mock.patch.object(start, "Engine") as engine,
            mock.patch.object(start, "sleep", side_effect=KeyboardInterrupt),
            mock.patch.object(start.update, "watch"),
            contextlib.redirect_stdout(output),
        ):
            engine.return_value.home_wifi = getattr(self, "wifi", HomeWifi(config.db_path()))
            self.assertEqual(main([command]), 0)
        engine.assert_called_once()
        engine.return_value.start.assert_called_once_with()
        engine.return_value.stop.assert_called_once_with()
        return output.getvalue()

    def test_phone_page_offers_each_automation_for_each_available_route(self):
        self.wifi = self.enterContext(wifi_listener())
        port = config.HOME_WIFI_PORT
        pages = []

        def read_page(url):
            if "/setup/" not in url:
                return
            self.assertEqual(urlsplit(url).port, port)
            with contextlib.closing(
                http.client.HTTPConnection("127.0.0.1", urlsplit(url).port, timeout=5)
            ) as client:
                client.request("GET", urlsplit(url).path)
                pages.append(client.getresponse().read().decode())

        for phone in (True, False):
            with (
                self.subTest(phone=phone),
                tempfile.TemporaryDirectory() as home,
                mock.patch.dict(os.environ, TFS_HOME=home),
            ):
                # GIVEN a fresh phone setup with or without private access
                self.routes = self.route(self.target, port=8444)
                pages.clear()

                # WHEN the person opens the setup page
                with (
                    mock.patch.object(setup, "wifi_host", return_value="127.0.0.1"),
                    mock.patch.object(setup, "print_qr", side_effect=read_page),
                ):
                    out = self.run_cli(phone=phone, apps=["health_auto_export"])
                self.assertLess(
                    out.index("What are you training for?"), out.index("View TFS on your phone")
                )
                self.assertLess(
                    out.index("Health Auto Export (1 of 1)"),
                    out.index("What are you training for?"),
                )
                # THEN each button installs a distinct automation with its route's address
                routes = {"Wi-Fi": f"http://127.0.0.1:{port}/"}
                if phone:
                    routes["Tailscale"] = f"https://{self.host}:8444/"
                self.assertEqual(len(pages), len(routes))
                for (route, base), page in zip(routes.items(), pages, strict=True):
                    parser = Hrefs()
                    parser.feed(page)
                    queries = [parse_qs(urlsplit(link).query) for link in parser.found]
                    self.assertEqual(
                        [(q["name"][0], q["url"][0]) for q in queries],
                        [
                            (f"TFS {kind} ({route})", f"{base}push/health_auto_export")
                            for kind in ("metrics", "workouts")
                        ],
                    )
                    self.assertEqual("Tailscale app is connected" in page, route == "Tailscale")

    def test_blocked_wifi_offers_only_the_available_tailscale_automations(self):
        # GIVEN Windows blocks Python and the person accepts or declines Tailscale
        self.powershell.return_value = '{"allowed":true,"blocks":["Example Python"]}'
        for accept in (True, False):
            with (
                self.subTest(accept=accept),
                tempfile.TemporaryDirectory() as home,
                mock.patch.dict(os.environ, TFS_HOME=home),
                mock.patch.object(sys, "platform", "win32"),
                mock.patch.object(setup, "_wait_for_scan", side_effect=AssertionError),
                mock.patch.object(setup, "print_qr") as qr,
            ):
                # WHEN setup connects the push source
                out = self.run_cli(phone=accept, apps=["health_auto_export"])
                # THEN every automation code goes straight to the private route
                self.assertIn("Tailscale is already on this computer", out)
                urls = [call.args[0] for call in qr.call_args_list]
                self.assertFalse(any(url.startswith("http://") for url in urls))
                targets = [parse_qs(urlsplit(url).query)["url"][0] for url in urls if "?" in url]
                self.assertEqual(
                    targets, [f"https://{self.host}/push/health_auto_export"] * (2 if accept else 0)
                )

    def test_free_port_sets_up_once_and_start_shows_phone_and_push_links(self):
        # GIVEN a signed-in computer with no Serve config
        # WHEN setup runs twice and the engine starts again
        setup_output = self.run_cli()
        self.assertIn(
            "Tailscale is already on this computer. Use it to open TFS away from home too? "
            "It works anywhere and is more secure.",
            setup_output,
        )
        self.assertNotIn("Do you have a Tailscale account?", setup_output)
        self.assertIn(f"Push URL (Tailscale):     https://{self.host}/push/", setup_output)
        self.assertIn("already", self.run_cli().lower())
        banner = self.run_cli("start")
        # THEN only the first setup changed Serve and start shows both links
        self.assertEqual(
            self.commands.call_args_list.count(
                mock.call("tailscale", "serve", "--bg", self.target)
            ),
            1,
        )
        url = f"https://{self.host}/"
        code = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L, border=2)
        code.add_data(url)
        qr = io.StringIO()
        code.print_ascii(out=qr, invert=True)
        self.assertIn(f"Dashboard (phone): {url}\n{qr.getvalue()}", banner)
        self.assertIn(f"https://{self.host}/push/health_auto_export", banner)

    def test_cp1252_stdout_keeps_the_phone_link_and_omits_the_qr(self):
        # GIVEN a phone link and redirected Windows output
        url = f"https://{self.host}/"
        with io.TextIOWrapper(io.BytesIO(), encoding="cp1252") as stream:
            # WHEN start builds the banner for that stream
            with contextlib.redirect_stdout(stream):
                banner = start.render_banner("localhost", 5313, None, None, "invented", [], url)
            # THEN the link sits directly above the token and the whole banner prints
            self.assertIn(f"Dashboard (phone): {url}\nPush token:", banner)
            stream.write(banner)

    def test_signed_out_can_create_account_then_skip_after_recheck(self):
        # GIVEN Tailscale is missing or needs login
        for state in (None, {"BackendState": "NeedsLogin"}):
            with self.subTest(state=state):
                self.state = state
                # WHEN setup runs
                output = self.run_cli(phone=[True, False, False])
                # THEN the person can finish setup and come back after signing in
                self.assertNotIn("█", self.run_cli("start"))
                for text in (
                    "Create your account in the browser, then press Enter.",
                    "Open Tailscale on this computer and sign in, then press Enter.",
                    "Try again?",
                    "TFS is running",
                ):
                    self.assertIn(text, output)
                self.browser.assert_any_call("https://login.tailscale.com/start")
        self.assertTrue(
            all(
                call == mock.call("tailscale", "status", "--json")
                for call in self.commands.call_args_list
            )
        )

    def test_missing_program_can_be_installed_and_signed_in_on_retry(self):
        # GIVEN no program, then an installation that needs one more sign-in attempt
        self.state = {"BackendState": "NeedsLogin"}
        programs = iter([None, "tailscale", "tailscale"])

        def signed_in(*_):
            if self.commands.call_count:
                self.state = {"BackendState": "Running", "Self": {"DNSName": self.host}}

        # WHEN the person creates an account, installs the program and retries
        with (
            mock.patch.object(_tailscale, "_program", side_effect=lambda: next(programs)),
            mock.patch("builtins.input", side_effect=signed_in),
            mock.patch.object(setup, "print_qr") as qr,
        ):
            out = self.run_cli(phone=[True, False, True])
        # THEN the browser and QR codes lead to the requested destinations
        self.browser.assert_any_call("https://login.tailscale.com/start")
        self.browser.assert_any_call("https://tailscale.com/download")
        self.assertIn("Install Tailscale on this computer and sign in, then press Enter.", out)
        self.assertIn(
            "Install Tailscale on your phone and sign in with the same account, then press Enter.",
            out,
        )
        self.assertEqual(
            [call.args[0] for call in qr.call_args_list[1:]],
            ["https://tailscale.com/download/ios", f"https://{self.host}/"],
        )

    def test_busy_or_public_port_stays_untouched(self):
        # GIVEN another app or a public route on 443
        for routes in (
            self.route("http://127.0.0.1:5199"),
            {**self.route(self.target), "AllowFunnel": {f"{self.host}:443": True}},
        ):
            with self.subTest(routes=routes):
                self.routes = routes
                # WHEN setup checks the port
                output = self.run_cli()
                # THEN it suggests a private port without changing or advertising the existing route
                program = "& 'tailscale'" if sys.platform == "win32" else "tailscale"
                self.assertIn(f"{program} serve --bg --https=8443 {self.target}", output)
                self.assertNotIn(f"https://{self.host}/", output)
        self.assertNotIn(
            mock.call("tailscale", "serve", "--bg", self.target), self.commands.call_args_list
        )

    def test_busy_port_hint_uses_the_quoted_fallback_path(self):
        # GIVEN only a Windows installation outside PATH and an occupied port
        program = self.home / "Program Files" / "Tailscale" / "tailscale.exe"
        program.parent.mkdir(parents=True)
        program.touch()
        self.which.return_value = None
        self.routes = self.route("http://127.0.0.1:5199")
        # WHEN setup prints the alternate port command
        with (
            mock.patch.object(_tailscale.sys, "platform", "win32"),
            mock.patch.dict(_tailscale._INSTALL_PATHS, win32=program),
        ):
            output = self.run_cli()
        # THEN the hint runs in PowerShell with the same executable used by setup
        self.assertIn(f"& '{program}' serve --bg --https=8443 {self.target}", output)
        self.commands.assert_any_call(str(program), "serve", "status", "--json")

    def test_declining_phone_access_skips_all_commands(self):
        # GIVEN the person declines phone access
        self.which.return_value = None
        self.enterContext(
            mock.patch.dict(_tailscale._INSTALL_PATHS, {sys.platform: self.home / "missing"})
        )
        # WHEN setup starts the engine
        output = self.run_cli(phone=False)
        # THEN no Tailscale command runs
        self.assertIn("TFS is running", output)
        self.assertIn("Only phones that scan it can open TFS.", output)
        self.commands.assert_not_called()

    def test_setup_waits_for_approval_and_retries_once(self):
        # GIVEN Serve needs approval
        self.approval = "https://login.tailscale.com/f/serve?node=invented"
        link = self.approval
        # WHEN the person approves it and presses Enter
        with mock.patch(
            "builtins.input",
            side_effect=lambda *args: setattr(self, "approval", "")
            if args and "approval" in args[0]
            else "",
        ):
            output = self.run_cli()
        # THEN setup showed the approval link and the verified dashboard address
        self.browser.assert_any_call(link)
        self.assertIn(link, output)
        self.assertIn(f"https://{self.host}/", output)
        self.assertEqual(
            self.commands.call_args_list.count(
                mock.call("tailscale", "serve", "--bg", self.target)
            ),
            2,
        )


class PhonePageTest(TfsHomeTestCase):
    CODE = "k7q2abcd"

    def run_push_setup(self, picked, answers, on_enter=lambda url: None):
        wifi = self.enterContext(wifi_listener())

        def finish(args, *, finish_setup):
            with mock.patch.object(setup, "_phone_setup"):
                finish_setup(wifi)
            return 0

        def press_enter():
            on_enter(qr.call_args.args[0])
            return True

        questionary = FakeQuestionary(picked, *answers)
        out, err = io.StringIO(), io.StringIO()

        with (
            mock.patch("sys.stdin", mock.Mock(isatty=lambda: True)),
            mock.patch("tfs.cli.setup.questionary", questionary),
            mock.patch("tfs.providers.shutil.which", FakeCommands(found=set()).which),
            mock.patch("tfs.cli.setup.connect_source"),
            mock.patch("tfs.cli.setup.import_.run") as imported,
            mock.patch("tfs.cli.setup.start.run", side_effect=finish),
            mock.patch("tfs.cli.setup.wifi_host", return_value="127.0.0.1"),
            mock.patch.object(setup, "_enter_within_second", side_effect=press_enter),
            mock.patch("tfs.cli.setup.secrets.token_urlsafe", return_value=self.CODE),
            mock.patch.object(setup, "print_qr") as qr,
            mock.patch("builtins.input", return_value=""),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            self.assertEqual(main(["setup"]), 0)
        return qr, out.getvalue() + err.getvalue(), imported

    def test_given_the_first_page_request_then_the_terminal_confirms_the_phone_and_the_token_stays_hidden(
        self,
    ):
        _, out, _ = self.run_push_setup(
            ["health_auto_export"],
            [],
            lambda url: urllib.request.urlopen(url).read(),  # noqa: S310
        )
        token = password_store.get("push_token")
        self.assertTrue(token)
        self.assertNotIn(token, out)
        self.assertIn("✔ Your phone reached this computer.", out)
        self.assertIn("Tap each button on the page, then press Enter.", out)

    def test_given_enter_before_any_request_then_no_phone_line_and_the_code_stops_working(
        self,
    ):
        qr, out, _ = self.run_push_setup(["health_auto_export"], [])
        self.assertNotIn("Your phone reached", out)
        url = urlsplit(qr.call_args.args[0])
        with contextlib.closing(
            http.client.HTTPConnection("127.0.0.1", url.port, timeout=5)
        ) as phone:
            phone.request("GET", url.path)
            self.assertEqual(phone.getresponse().status, 404)

    def test_busy_home_port_reports_the_problem_before_showing_a_code(self):
        # GIVEN another listener on the requested Wi-Fi port
        from tfs.engine import Engine

        with (
            wifi_listener(),
            mock.patch("tfs.engine.HOME_WIFI_HOST", "127.0.0.1"),
            mock.patch.object(
                start,
                "Engine",
                side_effect=lambda data, **kw: Engine(
                    data, screen_port=0, home_wifi_port=config.HOME_WIFI_PORT, **kw
                ),
            ),
            contextlib.redirect_stderr(io.StringIO()) as err,
        ):
            finish = mock.Mock()
            # WHEN setup starts the engine
            result = start.run(argparse.Namespace(home_wifi=True, open=False), finish_setup=finish)
            # THEN setup reports the port and never shows a dead link
            self.assertEqual(result, 1)
            self.assertIn(str(config.HOME_WIFI_PORT), err.getvalue())
            finish.assert_not_called()

    def test_late_scan_still_reaches_the_page_during_the_backup_offer(self):
        # GIVEN a setup code whose first wait has elapsed
        pages = []

        def scan():
            with contextlib.closing(
                http.client.HTTPConnection("127.0.0.1", config.HOME_WIFI_PORT, timeout=5)
            ) as phone:
                phone.request("GET", urlsplit(self.url).path)
                pages.append(phone.getresponse().read())

        with (
            mock.patch.object(setup, "_SCAN_HINT_S", 0),
            mock.patch.object(setup, "questionary", FakeQuestionary()),
            mock.patch.object(setup, "_phone_reached") as reached,
            wifi_listener() as wifi,
            mock.patch.object(setup, "wifi_host", return_value="127.0.0.1"),
            mock.patch.object(setup, "print_qr", side_effect=lambda url: setattr(self, "url", url)),
            mock.patch("builtins.input", return_value=""),
            contextlib.redirect_stdout(io.StringIO()) as out,
        ):
            # WHEN the phone scans while Tailscale is offered
            setup._show_links("example", "Example", [], wifi, scan)
        # THEN setup reports the timeout and accepts the scan
        self.assertIn("Your phone has not reached TFS.", out.getvalue())
        self.assertIn(b"Connect Example", pages[0])
        reached.assert_called_once()


class PushUrlHostTest(unittest.TestCase):
    def test_the_host_setup_prints_on_windows_passes_the_host_check(self):
        # GIVEN a Windows computer whose Wi-Fi address is 192.0.2.7
        with (
            mock.patch("tfs.cli.setup.sys.platform", "win32"),
            mock.patch("tfs.web.pairing.lan_address", return_value="192.0.2.7"),
            mock.patch("tfs.web.server.lan_address", return_value="192.0.2.7"),
            mock.patch("tfs.web.server.socket.getaddrinfo", side_effect=socket.gaierror),
        ):
            host = setup.wifi_host()
            server = make_server(
                config.SCREEN_HOST,
                0,
                push_handler=lambda *_: (200, {}),
                home_wifi=HomeWifi(Path("unused.db")),
            )
        server.start()
        self.addCleanup(server.stop)
        # WHEN a phone pushes to the URL setup prints
        address, port = server.sock.getsockname()
        client = http.client.HTTPConnection(address, port, timeout=10)
        self.addCleanup(client.close)
        client.request("POST", "/push/x", b"hi", {"Host": f"{host}:{config.HOME_WIFI_PORT}"})
        # THEN the server accepts it
        self.assertEqual(client.getresponse().status, 200)


class StartHomeWifiTest(TfsHomeTestCase):
    def test_setup_reports_the_verified_result_after_one_prompt(self):
        # GIVEN a missing TFS rule and no block on Python
        for approved in (True, False):
            after = (approved, [])
            with (
                self.subTest(approved=approved),
                mock.patch.object(sys, "platform", "win32"),
                mock.patch.object(start, "_firewall_state", side_effect=[(False, []), after]),
                mock.patch.object(
                    start, "_elevate_firewall", side_effect=None if approved else OSError
                ) as prompt,
                contextlib.redirect_stdout(io.StringIO()) as out,
            ):
                # WHEN the user accepts or cancels the admin prompt
                start._check_firewall(setup=True)
                # THEN setup reports when the rule is still missing
                prompt.assert_called_once_with()
                self.assertIn("Windows needs your OK once", out.getvalue())
                self.assertEqual("run tfs setup again." in out.getvalue(), not approved)

    def test_start_checks_the_firewall_without_changing_rules(self):
        # GIVEN a Windows computer with a missing rule, a block, or working rules
        self.enable("health_auto_export")
        for allowed, blocks in ((False, []), (True, ["Example Python"]), (True, [])):
            with (
                self.subTest(allowed=allowed, blocks=blocks),
                mock.patch.object(sys, "platform", "win32"),
                mock.patch.object(start, "_elevate_firewall", side_effect=AssertionError),
            ):
                self.powershell.return_value = json.dumps({"allowed": allowed, "blocks": blocks})
                # WHEN the engine starts
                self.start_engine()
                # THEN only a broken route needs attention
                blocked = "Windows is blocking Python on home networks, so phones cannot reach TFS over Wi-Fi. Use Tailscale to view TFS on your phone."
                missing = "Your phone cannot reach TFS over Wi-Fi. Run tfs setup to fix it."
                self.assertEqual(blocked in self.output.splitlines(), bool(blocks))
                self.assertEqual(missing in self.output.splitlines(), not allowed and not blocks)
                script = self.powershell.call_args.args[0]
                self.assertIn("ActiveStore", script)
                self.assertNotIn("Disable-NetFirewallRule", script)
                self.assertNotIn("New-NetFirewallRule", script)

    def test_repair_script_limits_rules_to_the_home_network_and_base_python(self):
        # GIVEN a base interpreter with spaces and a quote
        with mock.patch.object(sys, "_base_executable", "C:/Pat's Tools/python.exe"):
            # WHEN setup builds the script
            script = start._firewall_script(repair=True)
        # THEN both branches apply the same settings to the exact TFS rule name
        self.assertIn("Pat''s Tools", script)
        self.assertIn("$name = 'TFS home Wi-Fi'", script)
        self.assertIn("if (Get-NetFirewallRule -Name $name ", script)
        update, create = script.split("} else {")
        self.assertIn("Set-NetFirewallRule -Name $name @settings", update)
        self.assertIn("New-NetFirewallRule -Name $name -DisplayName $name @settings", create)
        for setting in (
            "Direction = 'Inbound'",
            "Action = 'Allow'",
            "Protocol = 'TCP'",
            "LocalPort = 47824",
            "Profile = 'Private'",
            "RemoteAddress = 'LocalSubnet'",
            "Program = $python",
            "Enabled = 'True'",
        ):
            self.assertIn(setting, script)
        self.assertNotRegex(script, r"(?i)(Disable|Remove|Enable)-NetFirewall")

    def enable(self, connector):
        config.data_dir().mkdir(parents=True, exist_ok=True)
        conn = sqlite.connect(config.db_path())
        migrate(conn)
        with conn:
            conn.execute("INSERT INTO sources (connector, enabled) VALUES (?, 1)", (connector,))
        conn.close()

    def start_engine(self):
        with (
            mock.patch("tfs.cli._tailscale._run", return_value=None),
            # Tests that fake Windows would send shutil.which down its Windows path.
            mock.patch("tfs.cli._tailscale._program", return_value=None),
            mock.patch("tfs.cli.start.Engine") as engine,
            mock.patch("tfs.cli.start.sleep", side_effect=KeyboardInterrupt),
            # The update check would reach GitHub.
            mock.patch("tfs.cli.update.watch"),
            contextlib.redirect_stdout(io.StringIO()) as out,
        ):
            start.run(argparse.Namespace(home_wifi=False, open=False))
            self.output = out.getvalue()
        return engine

    def test_given_a_push_source_is_on_then_the_home_wifi_listener_starts(self):
        self.enable("health_auto_export")
        self.assertTrue(self.start_engine().call_args.kwargs["home_wifi"])

    def test_given_no_push_source_then_the_home_wifi_listener_stays_off(self):
        self.enable("hevy")
        self.assertFalse(self.start_engine().call_args.kwargs["home_wifi"])


if __name__ == "__main__":
    unittest.main()
