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
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from tests.test_engine import free_port
from tests.test_setup_page import Hrefs
from tfs import config, password_store, providers
from tfs.cli import _tailscale, main, setup, start
from tfs.connectors.core.contract import load_manifests
from tfs.persistence import sqlite
from tfs.persistence.migrations import migrate
from tfs.providers import mcp_tools
from tfs.web.overview import read_overview
from tfs.web.server import make_server

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
        self.confirm = mock.Mock(return_value=mock.Mock(ask=lambda: phone))

    @staticmethod
    def print(text, style=None):
        sys.stdout.write(f"{text}\n")

    def __getattr__(self, kind):
        def question(message, validate=lambda answer: True, **options):
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


class TfsHomeTestCase(unittest.TestCase):
    def setUp(self):
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
    def run_setup(self, questionary, commands, *, tty=True):
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch("sys.stdin", mock.Mock(isatty=lambda: tty)),
            mock.patch("tfs.cli.setup.questionary", questionary),
            mock.patch("tfs.providers.shutil.which", commands.which),
            mock.patch("tfs.providers.run_command", commands.run),
            mock.patch("tfs.cli.setup.webbrowser.open"),
            mock.patch("tfs.cli.setup.start.run", return_value=0) as started,
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
        _, out, _, started = self.run_setup(FakeQuestionary([]), commands)
        self.assertEqual(commands.calls, [["claude", "plugin", "list"]])
        self.assertIn("✔ Claude Code   TFS connected", out)
        footer = started.call_args.kwargs["footer"]
        self.assertIn("run `claude`", footer)
        self.assertIn(providers.FIRST_MESSAGE, footer)

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
        entry = {"command": "/opt/other-tfs", "args": ["serve"]}
        folder = self.cursor_config(json.dumps({"mcpServers": {"tfs": entry}}))
        before = self.snapshot(folder)
        _, out, _, _ = self.run_setup(FakeQuestionary([CURSOR], []), FakeCommands(found=set()))
        self.assertEqual(self.snapshot(folder), before)
        self.assertIn(entry["command"], out)

    def test_given_no_supported_tools_then_setup_names_them_and_continues(self):
        code, out, _, started = self.run_setup(FakeQuestionary([]), FakeCommands(found=set()))
        labels = [p.label for p in providers.PROVIDERS] + [t.label for t in mcp_tools.TOOLS]
        for label in labels:
            self.assertIn(label, out)
        self.assertEqual(code, 0)
        started.assert_called_once()
        footer = started.call_args.kwargs["footer"]
        self.assertIn("run tfs setup again", footer)
        self.assertNotIn(providers.FIRST_MESSAGE, footer)

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

    def test_given_goal_answers_when_setup_finishes_then_the_overview_has_a_plate(self):
        # GIVEN a name, a future date and a target
        answers = ("First powerlifting meet", "2099-03-14", "Squat 140 kg")
        # WHEN setup finishes
        self.run_goal_setup(*answers)
        # THEN the save path stored the goal and the Overview shows it on the plate
        plate = read_overview(config.db_path(), datetime(2030, 1, 1, tzinfo=UTC)).plate
        self.assertEqual(
            (plate.data.goal, plate.data.date), ("First powerlifting meet", "2099-03-14")
        )

    def test_given_an_empty_name_when_setup_finishes_then_no_goal_is_stored(self):
        # GIVEN the person presses Enter on the name
        # WHEN setup finishes
        self.run_goal_setup("")
        # THEN no goal is stored
        self.assertEqual(self.stored_goals(), [])

    def test_given_a_past_date_when_setup_asks_then_it_says_why_and_asks_again(self):
        # GIVEN a past date, then a future one
        answers = ("Hike the coast track", "2020-01-01", "2099-03-14", "")
        # WHEN setup asks for the date
        _, out, _, _ = self.run_goal_setup(*answers)
        # THEN it says why and saves the second date
        self.assertIn("Pick a date after today.", out)
        self.assertEqual(self.stored_goals(), [("Hike the coast track", "2099-03-14")])

    def test_given_a_saved_goal_when_setup_finishes_then_the_next_step_asks_for_a_first_plan(self):
        # GIVEN a past goal, so setup asks again and saves a new one
        config.data_dir().mkdir(parents=True, exist_ok=True)
        with contextlib.closing(sqlite.connect(config.db_path())) as conn:
            migrate(conn)
            conn.execute(
                "INSERT INTO goals (id, type, local_date, target_json)"
                " VALUES ('old', 'Hike the coast track', '2020-01-01', '{}')"
            )
        self.run_goal_setup("First powerlifting meet", "2099-03-14", "")
        # WHEN setup runs again
        _, out, _, started = self.run_goal_setup()
        # THEN it names the goal and the next step asks for a first training plan
        self.assertIn("Goal: First powerlifting meet on 2099-03-14", out)
        footer = started.call_args.kwargs["footer"]
        self.assertIn(providers.PLAN_MESSAGE, footer)
        self.assertNotIn(providers.FIRST_MESSAGE, footer)


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
            mock.patch.object(start, "Engine"),
            mock.patch.object(start, "sleep", side_effect=KeyboardInterrupt),
            mock.patch.object(start.update, "watch"),
            mock.patch.object(start.webbrowser, "open"),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(main([command]), 0)
        return output.getvalue()

    def test_phone_page_offers_each_automation_for_each_available_route(self):
        port = free_port()
        page = ""

        def read_page():
            nonlocal page
            with contextlib.closing(
                http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            ) as client:
                client.request("GET", "/setup/invented")
                page = client.getresponse().read().decode()

        for phone in (True, False):
            with self.subTest(phone=phone):
                # GIVEN a fresh phone setup with or without private access
                self.routes = self.route(self.target, port=8444)

                # WHEN the person opens the setup page
                with (
                    mock.patch.object(setup, "_BIND", "127.0.0.1"),
                    mock.patch.object(setup, "_host", return_value="127.0.0.1"),
                    mock.patch.object(config, "HOME_WIFI_PORT", port),
                    mock.patch.object(setup.secrets, "token_urlsafe", return_value="invented"),
                    mock.patch("builtins.input", read_page),
                ):
                    self.run_cli(phone=phone, apps=["health_auto_export"])
                # THEN each button installs a distinct automation with its route's address
                routes = {"Wi-Fi": f"http://127.0.0.1:{port}/"}
                if phone:
                    routes["Tailscale"] = f"https://{self.host}:8444/"
                parser = Hrefs()
                parser.feed(page)
                queries = [parse_qs(urlsplit(link).query) for link in parser.found]
                self.assertEqual(
                    [(q["name"][0], q["url"][0]) for q in queries],
                    [
                        (f"TFS {kind} ({route})", f"{base}push/health_auto_export")
                        for route, base in routes.items()
                        for kind in ("metrics", "workouts")
                    ],
                )
                self.assertEqual("Tailscale app is connected" in page, phone)

    def test_free_port_sets_up_once_and_start_shows_phone_and_push_links(self):
        # GIVEN a signed-in computer with no Serve config
        # WHEN setup runs twice and the engine starts again
        self.assertIn(f"https://{self.host}/", self.run_cli())
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
        with contextlib.redirect_stdout(io.StringIO()) as qr:
            setup.print_qr(url)
        self.assertIn(f"Dashboard (phone): {url}\n{qr.getvalue()}", banner)
        self.assertIn(f"https://{self.host}/push/health_auto_export", banner)

    def test_missing_or_signed_out_prints_install_steps_and_finishes(self):
        # GIVEN Tailscale is missing or needs login
        for state in (None, {"BackendState": "NeedsLogin"}):
            with self.subTest(state=state):
                self.state = state
                # WHEN setup runs
                output = self.run_cli()
                # THEN the person can finish setup and come back after signing in
                self.assertNotIn("█", self.run_cli("start"))
                for text in (
                    "tailscale.com/download",
                    "same account",
                    "tfs setup",
                    "TFS is running",
                ):
                    self.assertIn(text, output)
        self.assertTrue(
            all(
                call == mock.call("tailscale", "status", "--json")
                for call in self.commands.call_args_list
            )
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
        # WHEN setup starts the engine
        output = self.run_cli(phone=False)
        # THEN no Tailscale command runs
        self.assertIn("TFS is running", output)
        self.assertNotIn("█", output)
        self.commands.assert_not_called()

    def test_setup_waits_for_approval_and_retries_once(self):
        # GIVEN Serve needs approval
        self.approval = "https://login.tailscale.com/f/serve?node=invented"
        link = self.approval
        # WHEN the person approves it and presses Enter
        with mock.patch(
            "builtins.input", side_effect=lambda *_: setattr(self, "approval", "")
        ) as enter:
            output = self.run_cli()
        # THEN setup showed the approval link and the verified dashboard address
        enter.assert_called_once()
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

    def run_push_setup(self, picked, answers, port, on_enter=lambda: None):
        questionary = FakeQuestionary(picked, *answers)
        out, err = io.StringIO(), io.StringIO()

        def enter(*_):
            on_enter()
            return ""

        with (
            mock.patch("sys.stdin", mock.Mock(isatty=lambda: True)),
            mock.patch("tfs.cli.setup.questionary", questionary),
            mock.patch("tfs.providers.shutil.which", FakeCommands(found=set()).which),
            mock.patch("tfs.cli.setup.connect_source"),
            mock.patch("tfs.cli.setup.import_.run") as imported,
            mock.patch("tfs.cli.setup.start.run", return_value=0),
            mock.patch("tfs.cli.setup._host", return_value="127.0.0.1"),
            mock.patch("tfs.cli.setup._BIND", "127.0.0.1"),
            mock.patch("tfs.cli.setup.config.HOME_WIFI_PORT", port),
            mock.patch("tfs.cli.setup.secrets.token_urlsafe", return_value=self.CODE),
            mock.patch("builtins.input", enter),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = main(["setup"])
        return code, out.getvalue() + err.getvalue(), imported

    def free_port(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]

    def test_given_the_first_page_request_then_the_terminal_confirms_the_phone_and_the_token_stays_hidden(
        self,
    ):
        port = self.free_port()
        url = f"http://127.0.0.1:{port}/setup/{self.CODE}"
        _, out, _ = self.run_push_setup(
            ["health_auto_export"],
            [],
            port,
            lambda: urllib.request.urlopen(url).read(),  # noqa: S310
        )
        token = password_store.get("push_token")
        self.assertTrue(token)
        self.assertNotIn(token, out)
        self.assertIn("✔ Your phone reached this computer.", out)
        self.assertIn("Tap each button on the page, then press Enter.", out)

    def test_given_enter_before_any_request_then_no_phone_line_and_the_address_stops_answering(
        self,
    ):
        port = self.free_port()
        _, out, _ = self.run_push_setup(["health_auto_export"], [], port)
        self.assertNotIn("Your phone reached", out)
        with self.assertRaises(OSError):
            urllib.request.urlopen(f"http://127.0.0.1:{port}/setup/{self.CODE}", timeout=5)

    def test_given_the_port_is_in_use_then_setup_says_so_and_goes_on_to_the_next_app(self):
        with socket.socket() as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen()
            port = taken.getsockname()[1]
            _, out, imported = self.run_push_setup(
                ["health_auto_export", "apple_health"], [""], port
            )
        self.assertIn("Close TFS, then run tfs setup again.", out)
        self.assertIn("2 of 2", out)
        imported.assert_not_called()


class PushUrlHostTest(unittest.TestCase):
    def test_the_host_setup_prints_on_windows_passes_the_host_check(self):
        # GIVEN a Windows computer whose Wi-Fi address is 192.0.2.7
        with (
            mock.patch("tfs.cli.setup.sys.platform", "win32"),
            mock.patch("tfs.cli.setup.lan_address", return_value="192.0.2.7"),
            mock.patch("tfs.web.server.lan_address", return_value="192.0.2.7"),
            mock.patch("tfs.web.server.socket.getaddrinfo", side_effect=socket.gaierror),
        ):
            host = setup._host()
            server = make_server(
                config.SCREEN_HOST, 0, push_handler=lambda *_: (200, {}), push_only=True
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
            mock.patch("tfs.cli.start.Engine") as engine,
            mock.patch("tfs.cli.start.sleep", side_effect=KeyboardInterrupt),
            # The update check would reach GitHub.
            mock.patch("tfs.cli.update.watch"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            start.run(argparse.Namespace(home_wifi=False, open=False))
        return engine

    def test_given_a_push_source_is_on_then_the_home_wifi_listener_starts(self):
        self.enable("health_auto_export")
        self.assertTrue(self.start_engine().call_args.kwargs["home_wifi"])

    def test_given_no_push_source_then_the_home_wifi_listener_stays_off(self):
        self.enable("hevy")
        self.assertFalse(self.start_engine().call_args.kwargs["home_wifi"])


if __name__ == "__main__":
    unittest.main()
