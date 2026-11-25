import contextlib
import hashlib
import io
import itertools
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from tfs.cli import update

WHEEL = b"invented wheel bytes"
DIGEST = "sha256:" + hashlib.sha256(WHEEL).hexdigest()


def release(
    version: str, *, prerelease: bool, digest: str = DIGEST, wheel_version: str = ""
) -> dict:
    name = f"training_for_something-{wheel_version or version}-py3-none-any.whl"
    return {
        "tag_name": f"v{version}",
        "draft": False,
        "prerelease": prerelease,
        "assets": [
            {"name": name, "browser_download_url": f"https://wheels/{name}", "digest": digest}
        ],
    }


class UpdateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.events: list = []

    def update(self, current: str, releases: list[dict], **fakes) -> tuple[int, str]:
        """Run tfs update against a fake GitHub that serves the releases and WHEEL."""

        def fetch(url: str) -> bytes:
            self.events.append(("fetch", url))
            return json.dumps(releases).encode() if url == update.RELEASES_URL else WHEEL

        def install(uv: str, wheel, restart: bool) -> int:
            self.events.append(("install", wheel.read_bytes(), restart))
            shutil.rmtree(wheel.parent)
            return 0

        options = {"uv": "uv", "helpers": list, "stop": lambda: False, "install": install, **fakes}
        out = io.StringIO()
        with (
            mock.patch.object(update, "POLL_SECONDS", 0),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(out),
        ):
            code = update.update(current, fetch=fetch, **options)
        return code, out.getvalue()

    def installed(self) -> bool:
        return "install" in [event[0] for event in self.events]

    def test_a_newer_release_installs_the_verified_wheel_and_starts_tfs_again(self) -> None:
        # GIVEN a pre-release install, a newer pre-release, and TFS running
        releases = [release("9.1.0a2", prerelease=True)]
        # WHEN
        code, _ = self.update("9.1.0a1", releases, stop=lambda: True)
        # THEN
        self.assertEqual(code, 0)
        self.assertEqual(self.events[-1], ("install", WHEEL, True))

    def test_the_highest_release_with_a_matching_wheel_wins_in_any_list_order(self) -> None:
        # GIVEN an rc listed first, the final after it, and a newer tag whose wheel is misnamed
        releases = [
            release("9.1.0rc2", prerelease=True),
            release("9.1.0", prerelease=False),
            release("9.2.0", prerelease=False, wheel_version="9.1.9"),
        ]
        # WHEN
        code, _ = self.update("9.1.0rc1", releases)
        # THEN the final 9.1.0 sorts above its rc and is the one downloaded
        self.assertEqual(code, 0)
        self.assertIn(("fetch", releases[1]["assets"][0]["browser_download_url"]), self.events)
        self.assertTrue(self.installed())

    def test_a_wheel_whose_digest_does_not_match_installs_nothing(self) -> None:
        # GIVEN
        releases = [release("9.1.0", prerelease=False, digest="sha256:" + "0" * 64)]
        # WHEN
        code, _ = self.update("9.0.0", releases)
        # THEN
        self.assertEqual(code, 1)
        self.assertFalse(self.installed())

    def test_no_newer_release_downloads_and_installs_nothing(self) -> None:
        # GIVEN a stable install, the same stable release, and a newer pre-release it ignores
        releases = [release("9.2.0a1", prerelease=True), release("9.1.0", prerelease=False)]
        # WHEN
        code, out = self.update("9.1.0", releases)
        # THEN
        self.assertEqual(code, 0)
        self.assertEqual(self.events, [("fetch", update.RELEASES_URL)])
        self.assertIn("9.1.0", out)

    def test_a_running_helper_holds_the_install_until_it_exits(self) -> None:
        # GIVEN a helper that runs for two checks
        checks = iter([["Claude Code"], ["Claude Code"], []])
        active = [True]

        def helpers() -> list[str]:
            names = next(checks)
            active[0] = bool(names)
            return names

        def install(uv: str, wheel, restart: bool) -> int:
            self.assertFalse(active[0], "installed while a helper runs")
            self.events.append(("install",))
            shutil.rmtree(wheel.parent)
            return 0

        # WHEN
        code, out = self.update(
            "9.0.0", [release("9.1.0", prerelease=False)], helpers=helpers, install=install
        )
        # THEN
        self.assertEqual(code, 0)
        self.assertIn("Claude Code", out)
        self.assertTrue(self.installed())

    def test_ctrl_c_while_a_helper_runs_installs_nothing(self) -> None:
        # GIVEN
        def helpers() -> list[str]:
            raise KeyboardInterrupt

        # WHEN
        code, _ = self.update("9.0.0", [release("9.1.0", prerelease=False)], helpers=helpers)
        # THEN
        self.assertEqual(code, 1)
        self.assertFalse(self.installed())

    def test_a_failed_process_check_installs_nothing(self) -> None:
        # GIVEN
        def helpers() -> list[str]:
            raise subprocess.TimeoutExpired("powershell", 30)

        # WHEN
        code, _ = self.update("9.0.0", [release("9.1.0", prerelease=False)], helpers=helpers)
        # THEN
        self.assertEqual(code, 1)
        self.assertFalse(self.installed())

    def test_tfs_that_does_not_stop_installs_nothing(self) -> None:
        # GIVEN
        def stop() -> bool:
            raise TimeoutError

        # WHEN
        code, _ = self.update("9.0.0", [release("9.1.0", prerelease=False)], stop=stop)
        # THEN
        self.assertEqual(code, 1)
        self.assertFalse(self.installed())

    def test_no_uv_leaves_tfs_running(self) -> None:
        # GIVEN
        stopped = []
        # WHEN
        code, _ = self.update(
            "9.0.0", [release("9.1.0", prerelease=False)], uv=None, stop=lambda: stopped.append(1)
        )
        # THEN
        self.assertEqual(code, 1)
        self.assertEqual(stopped, [])
        self.assertFalse(self.installed())

    def test_a_download_it_cannot_save_leaves_tfs_running(self) -> None:
        # GIVEN a full disk
        stopped = []
        # WHEN
        with mock.patch.object(update.tempfile, "mkdtemp", side_effect=OSError("disk full")):
            code, _ = self.update(
                "9.0.0", [release("9.1.0", prerelease=False)], stop=lambda: stopped.append(1)
            )
        # THEN
        self.assertEqual(code, 1)
        self.assertEqual(stopped, [])


class StopTest(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        environment = mock.patch.dict(os.environ, {"TFS_HOME": folder.name})
        environment.start()
        self.addCleanup(environment.stop)
        self.running = Path(folder.name) / update.RUNNING_FILE

    def start_tfs(self, seconds: float) -> subprocess.Popen:
        """Stand in for tfs start with a process that ends after some seconds."""
        process = subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({seconds})"])
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        # Reap it once it ends, so POSIX does not keep it alive as a zombie.
        threading.Thread(target=process.wait, daemon=True).start()
        self.running.write_text(str(process.pid))
        return process

    def stop(self) -> bool:
        # The screen port answers once, then is closed while the process still runs.
        pings = itertools.chain([True], itertools.repeat(False))
        with (
            mock.patch.object(update, "tfs_answers_ping", side_effect=pings),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            return update._stop()

    def test_stop_waits_for_the_tfs_process_to_end(self) -> None:
        # GIVEN
        process = self.start_tfs(0.5)
        # WHEN
        self.stop()
        # THEN
        self.assertFalse(self.running.exists())
        self.assertIsNotNone(process.poll())

    def test_stop_gives_up_when_tfs_does_not_end_in_time(self) -> None:
        # GIVEN
        self.start_tfs(30)
        # WHEN / THEN
        with (
            mock.patch.object(update, "STOP_TIMEOUT_SECONDS", 0.2),
            self.assertRaises(TimeoutError),
        ):
            self.stop()


class HelpersTest(unittest.TestCase):
    def test_a_helper_with_a_quoted_mcp_argument_is_named_after_its_ai_tool(self) -> None:
        # GIVEN an AI tool that runs tfs.exe, which runs this install's python with "mcp"
        python = "C:\\tools\\tfs\\Scripts\\python.exe"
        listing = [
            {"ProcessId": 1, "ParentProcessId": 0, "Name": "claude.exe", "CommandLine": "claude"},
            {"ProcessId": 2, "ParentProcessId": 1, "Name": "tfs.exe", "CommandLine": '"tfs" "mcp"'},
            {
                "ProcessId": 3,
                "ParentProcessId": 2,
                "Name": "python.exe",
                "CommandLine": f'"{python}" "C:\\bin\\tfs.exe" "mcp"',
            },
        ]
        done = subprocess.CompletedProcess([], 0, stdout=json.dumps(listing))
        # WHEN
        with (
            mock.patch.object(update.subprocess, "run", return_value=done),
            mock.patch.object(update.sys, "executable", python),
        ):
            names = update._helpers()
        # THEN
        self.assertEqual(names, ["Claude Code"])


class UpdateCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.data_dir = Path(folder.name)
        self.requests: list[str] = []

    def github(self, url: str) -> bytes:
        self.requests.append(url)
        return json.dumps([release("9.1.0", prerelease=False)]).encode()

    def check(self, fetch=None, turned_off: str = "") -> str | None:
        """Run the check that tfs start makes, as TFS 9.0.0."""
        with (
            mock.patch.dict(os.environ, {"TFS_NO_UPDATE_CHECK": turned_off}),
            mock.patch.object(update, "__version__", "9.0.0"),
        ):
            return update.check(self.data_dir, fetch=fetch or self.github)

    def test_a_newer_release_shows_the_notice_once_for_that_version(self) -> None:
        # GIVEN a newer release
        # WHEN tfs start checks at start and a day later
        notices = [self.check(), self.check()]
        # THEN the terminal tells once, and the dashboard can show the version
        self.assertEqual(notices[0], "TFS 9.1.0 is ready. Run tfs update.")
        self.assertIsNone(notices[1])
        with mock.patch.object(update, "__version__", "9.0.0"):
            self.assertEqual(update.available(self.data_dir), "9.1.0")

    def test_no_network_is_silent(self) -> None:
        # GIVEN
        def offline(url: str) -> bytes:
            raise OSError("no network")

        # WHEN
        notice = self.check(fetch=offline)
        # THEN
        self.assertIsNone(notice)

    def test_a_saved_answer_that_is_not_an_object_counts_as_none(self) -> None:
        # GIVEN
        (self.data_dir / update.CHECK_FILE).write_text("[]", encoding="utf-8")
        # WHEN
        notice = self.check()
        # THEN
        self.assertEqual(notice, "TFS 9.1.0 is ready. Run tfs update.")

    def test_a_folder_it_cannot_write_still_gives_the_notice(self) -> None:
        # GIVEN a data folder that is gone
        self.data_dir = self.data_dir / "gone"
        # WHEN
        with self.assertLogs(update.logger):
            notice = self.check()
        # THEN
        self.assertEqual(notice, "TFS 9.1.0 is ready. Run tfs update.")

    def test_turned_off_makes_no_request(self) -> None:
        # WHEN
        notice = self.check(turned_off="1")
        # THEN
        self.assertIsNone(notice)
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
