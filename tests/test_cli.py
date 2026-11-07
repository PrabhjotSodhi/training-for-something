import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from importlib.metadata import requires
from pathlib import Path
from unittest import mock

import tfs
import tfs.cli
from tests import fake_cli_command
from tests.test_import_file import EXPORT
from tfs.cli import main


def run_tfs(*args: str) -> subprocess.CompletedProcess[str]:
    """Run `python -m tfs` with the given arguments and return the finished process."""
    return subprocess.run(
        [sys.executable, "-m", "tfs", *args], capture_output=True, text=True, check=False
    )


COMMAND_MODULE = """
def add_parser(subparsers):
    subparsers.add_parser("NAME").set_defaults(run=lambda args: 5)
"""


class CliTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(os.umask, os.umask(0o022))

    def test_version_constant(self):
        self.assertEqual(tfs.__version__, "0.1.0.dev0")

    def test_keyring_is_the_only_runtime_dependency(self):
        self.assertEqual(requires("training-for-something"), ["keyring>=25"])

    def test_version_flag_prints_version_and_exits_0(self):
        result = run_tfs("--version")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "tfs 0.1.0.dev0\n")

    def test_no_command_prints_usage_and_exits_2(self):
        result = run_tfs()
        self.assertEqual(result.returncode, 2)
        self.assertTrue(result.stderr.startswith("usage: tfs"))

    @unittest.skipIf(sys.platform == "win32", "Windows has no file modes")
    def test_files_and_folders_the_command_creates_are_private(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        export = Path(home.name, "export.xml")
        export.write_text(EXPORT, encoding="utf-8")
        data = Path(home.name, "data")
        with mock.patch.dict(os.environ, {"TFS_HOME": str(data), "TFS_PASSWORD_STORE": "file"}):
            result = run_tfs("import", str(export))
        self.assertEqual(result.returncode, 0, result.stderr)
        modes = {
            path.relative_to(data).as_posix(): path.stat().st_mode & 0o777
            for path in [data, data / "tfs.db", *(data / "raw").rglob("*")]
        }
        for name, mode in modes.items():
            self.assertEqual(mode, 0o700 if (data / name).is_dir() else 0o600, name)

    def test_main_runs_a_command_from_the_given_modules(self):
        self.assertEqual(main(["hello"], modules=[fake_cli_command]), 7)

    def test_main_finds_command_modules_in_the_cli_package(self):
        commands = tempfile.TemporaryDirectory()
        self.addCleanup(commands.cleanup)
        for name, command in (("greet", "greet"), ("_private", "secret")):
            Path(commands.name, f"{name}.py").write_text(
                COMMAND_MODULE.replace("NAME", command), encoding="utf-8"
            )
        self.addCleanup(sys.modules.pop, "tfs.cli.greet", None)
        self.addCleanup(sys.modules.pop, "tfs.cli._private", None)
        self.addCleanup(vars(tfs.cli).pop, "greet", None)
        with mock.patch.object(tfs.cli, "__path__", [commands.name]):
            self.assertEqual(main(["greet"]), 5)
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                main(["secret"])
        self.assertEqual(raised.exception.code, 2)

    def test_main_without_modules_rejects_an_unknown_command(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            main(["hello"])
        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
