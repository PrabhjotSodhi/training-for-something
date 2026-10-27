import contextlib
import io
import os
import subprocess
import sys
import unittest
from importlib.metadata import requires

import tfs
from tests import fake_cli_command
from tfs.cli import main


def run_tfs(*args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": "src"}
    return subprocess.run(
        [sys.executable, "-m", "tfs", *args], capture_output=True, text=True, env=env, check=False
    )


class CliTest(unittest.TestCase):
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

    def test_main_runs_a_command_from_the_given_modules(self):
        self.assertEqual(main(["hello"], modules=[fake_cli_command]), 7)

    def test_main_without_modules_rejects_an_unknown_command(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            main(["hello"])
        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
