import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tfs.connectors.core import runner
from tfs.connectors.core.contract import validate_record
from tfs.connectors.core.runner import (
    ConnectorUserError,
    RunnerTimeoutError,
    http_get,
    reader_version,
    run,
)

FAKE = "tests.fake_connector.connector"
FAKE_FOLDER = Path(__file__).parent / "fake_connector"


class RunTest(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def test_read_returns_the_result_and_writes_the_records_file(self) -> None:
        out_path = self.folder / "out.jsonl"
        payload = {"path": "a", "fmt": "b", "out_path": str(out_path)}
        result = run("fake_connector", "read", payload, timeout_s=10, module=FAKE)
        self.assertEqual(result, {"status": "ok", "count": 1, "message": None})
        lines = out_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        validate_record(json.loads(lines[0]))

    def test_a_stuck_connector_is_killed_and_reaped_at_the_deadline(self) -> None:
        children = []
        real_popen = subprocess.Popen

        def remember(*args: object, **kwargs: object) -> subprocess.Popen:
            children.append(real_popen(*args, **kwargs))
            return children[-1]

        with (
            mock.patch.object(runner.subprocess, "Popen", remember),
            self.assertRaises(RunnerTimeoutError),
        ):
            run("fake_connector", "send", {}, timeout_s=0.5, module=FAKE)
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].returncode)

    def test_a_user_error_hides_the_secret_and_a_missing_function_is_unsupported(self) -> None:
        payload = {"answers": {"api_key": "s3cret"}}
        with self.assertRaises(ConnectorUserError) as caught:
            run("fake_connector", "connect", payload, timeout_s=10, module=FAKE)
        self.assertEqual(str(caught.exception), "The key [secret] was refused.")
        self.assertEqual(
            run("fake_connector", "fetch", {}, timeout_s=10, module=FAKE), {"unsupported": True}
        )

    def test_a_bad_connector_name_or_operation_is_refused(self) -> None:
        for connector, op in (
            ("../x", "read"),
            ("core", "read"),
            ("testing", "read"),
            ("fake_connector", "delete"),
        ):
            with self.subTest(connector=connector, op=op), self.assertRaises(ValueError):
                run(connector, op, {}, timeout_s=1)


class ReaderVersionTest(unittest.TestCase):
    def test_changes_with_any_connector_file_or_the_contract(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        copy = Path(folder.name) / "fake_connector"
        shutil.copytree(FAKE_FOLDER, copy, ignore=shutil.ignore_patterns("__pycache__"))
        contract = Path(folder.name) / "contract.py"
        contract.write_text("a = 1\n", encoding="utf-8")
        versions = [reader_version(folder=copy)]
        self.assertRegex(versions[0], r"\A[0-9a-f]{16}\Z")
        with mock.patch.object(runner, "_CONTRACT_PATH", contract):
            versions.append(reader_version(folder=copy))
            contract.write_text("a = 2\n", encoding="utf-8")
            versions.append(reader_version(folder=copy))
            with (copy / "connector.py").open("a", encoding="utf-8") as handle:
                handle.write("# changed\n")
            versions.append(reader_version(folder=copy))
            with (copy / "manifest.json").open("a", encoding="utf-8") as handle:
                handle.write("\n")
            versions.append(reader_version(folder=copy))
        self.assertEqual(len(set(versions)), len(versions))


class HttpGetTest(unittest.TestCase):
    def test_refuses_a_url_that_is_not_https(self) -> None:
        for url in ("http://example.com/", "file:///etc/hostname"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                http_get(url, {})


if __name__ == "__main__":
    unittest.main()
