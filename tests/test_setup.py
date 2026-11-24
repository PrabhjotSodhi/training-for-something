import argparse
import contextlib
import http.client
import io
import os
import socket
import sys
import tempfile
import unittest
import urllib.request
from collections import deque
from unittest import mock

from tfs import config, password_store, providers
from tfs.cli import main, setup, start
from tfs.connectors.core.contract import load_manifests
from tfs.persistence import sqlite
from tfs.persistence.migrations import migrate
from tfs.web.server import make_server

REPO = "PrabhjotSodhi/training-for-something"
PLUGIN = "tfs@training-for-something"
CLAUDE, CODEX = providers.PROVIDERS
PUSH_URL = "http://studio.local:47824/push/health_auto_export"


class FakeQuestionary:
    """Answers each question from a list, in order."""

    Choice = mock.Mock(
        side_effect=lambda title, value, disabled=None, checked=False: mock.Mock(disabled=disabled)
    )

    def __init__(self, *answers):
        self.answers = deque(answers)

    @staticmethod
    def print(text, style=None):
        sys.stdout.write(f"{text}\n")

    def __getattr__(self, kind):
        def question(message, **options):
            answer = self.answers.popleft()
            return mock.Mock(ask=lambda: answer)

        return question


class FakeCommands:
    """Stands in for the claude and codex programs and records every command."""

    def __init__(self, found, plugin_list=""):
        self.found = found
        self.plugin_list = plugin_list
        self.calls = []

    def which(self, command):
        return command if command in self.found else None

    def run(self, argv):
        self.calls.append(list(argv))
        return True, self.plugin_list if argv[1:3] == ["plugin", "list"] else ""


class TfsHomeTestCase(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        environment = mock.patch.dict(os.environ, {"TFS_HOME": home.name})
        environment.start()
        self.addCleanup(environment.stop)


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
        _, out, _, _ = self.run_setup(FakeQuestionary([]), commands)
        self.assertEqual(commands.calls, [["claude", "plugin", "list"]])
        self.assertIn("✔ Claude Code   TFS connected", out)

    def test_given_a_tool_left_unpicked_then_nothing_is_installed_for_it(self):
        commands = FakeCommands(found={"claude", "codex"})
        self.run_setup(FakeQuestionary([CODEX], []), commands)
        self.assertNotIn(["claude", "plugin", "install", PLUGIN], commands.calls)

    def test_given_codex_not_connected_then_codex_uses_plugin_add(self):
        commands = FakeCommands(found={"codex"})
        self.run_setup(FakeQuestionary([CODEX], []), commands)
        self.assertEqual(commands.calls[-1], ["codex", "plugin", "add", PLUGIN])

    def test_given_the_shipped_manifest_then_the_links_are_exact(self):
        links = load_manifests()["health_auto_export"]["setup"]["links"]
        filled = [setup.fill_link(link["url"], PUSH_URL, "tok-invented") for link in links]
        self.assertEqual(
            filled,
            [
                "com.HealthExport://automation?url=http%3A%2F%2Fstudio.local%3A47824"
                "%2Fpush%2Fhealth_auto_export&name=TFS%20metrics&datatype=healthMetrics"
                "&format=json&exportversion=v2&period=lastsync&aggregatesleep=true"
                "&headers=Authorization,Bearer%20tok-invented",
                "com.HealthExport://automation?url=http%3A%2F%2Fstudio.local%3A47824"
                "%2Fpush%2Fhealth_auto_export&name=TFS%20workouts&datatype=workouts"
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
        self.assertIn("Tap both buttons on the page, then press Enter.", out)

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
            server = make_server(config.SCREEN_HOST, 0, push_handler=lambda *_: (200, {}))
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
            mock.patch("tfs.cli.start.Engine") as engine,
            mock.patch("tfs.cli.start.sleep", side_effect=KeyboardInterrupt),
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
