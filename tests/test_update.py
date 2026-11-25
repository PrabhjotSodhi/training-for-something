import contextlib
import hashlib
import io
import json
import shutil
import unittest
from unittest import mock

from tfs.cli import update

WHEEL = b"invented wheel bytes"
DIGEST = "sha256:" + hashlib.sha256(WHEEL).hexdigest()


def release(version: str, *, prerelease: bool, digest: str = DIGEST) -> dict:
    name = f"training_for_something-{version}-py3-none-any.whl"
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

        def install(wheel, restart: bool) -> None:
            self.events.append(("install", wheel.read_bytes(), restart))
            shutil.rmtree(wheel.parent)

        options = {"helpers": list, "stop": lambda: False, **fakes}
        out = io.StringIO()
        with (
            mock.patch.object(update, "POLL_SECONDS", 0),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(out),
        ):
            code = update.update(current, fetch=fetch, install=install, **options)
        return code, out.getvalue()

    def test_a_newer_release_installs_the_verified_wheel_and_starts_tfs_again(self) -> None:
        # GIVEN a pre-release install, a newer pre-release, and TFS running
        releases = [release("9.1.0a2", prerelease=True)]
        # WHEN
        code, _ = self.update("9.1.0a1", releases, stop=lambda: True)
        # THEN
        self.assertEqual(code, 0)
        self.assertEqual(self.events[-1], ("install", WHEEL, True))

    def test_a_wheel_whose_digest_does_not_match_installs_nothing(self) -> None:
        # GIVEN
        releases = [release("9.1.0", prerelease=False, digest="sha256:" + "0" * 64)]
        # WHEN
        code, _ = self.update("9.0.0", releases)
        # THEN
        self.assertEqual(code, 1)
        self.assertNotIn("install", [event[0] for event in self.events])

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

        def helpers() -> list[str]:
            self.events.append(("check",))
            return next(checks)

        # WHEN
        code, out = self.update("9.0.0", [release("9.1.0", prerelease=False)], helpers=helpers)
        # THEN
        self.assertEqual(code, 0)
        self.assertEqual(
            [event[0] for event in self.events],
            ["fetch", "fetch", "check", "check", "check", "install"],
        )
        self.assertEqual(out.count("Claude Code"), 1)

    def test_ctrl_c_while_a_helper_runs_installs_nothing(self) -> None:
        # GIVEN
        def helpers() -> list[str]:
            raise KeyboardInterrupt

        # WHEN
        code, _ = self.update("9.0.0", [release("9.1.0", prerelease=False)], helpers=helpers)
        # THEN
        self.assertEqual(code, 1)
        self.assertNotIn("install", [event[0] for event in self.events])


if __name__ == "__main__":
    unittest.main()
