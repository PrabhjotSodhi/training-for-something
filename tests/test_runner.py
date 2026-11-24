import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from tfs.connectors.core import runner
from tfs.connectors.core.contract import validate_record
from tfs.connectors.core.runner import (
    ConnectorUserError,
    RunnerFailedError,
    RunnerTimeoutError,
    http_get,
    http_request,
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
        self.real_popen = subprocess.Popen
        self.children = []

        def remember(*args: object, **kwargs: object) -> subprocess.Popen:
            self.children.append(self.real_popen(*args, **kwargs))
            return self.children[-1]

        patch = mock.patch.object(runner.subprocess, "Popen", remember)
        patch.start()
        self.addCleanup(patch.stop)

    def test_read_returns_the_result_and_writes_the_records_file(self) -> None:
        out_path = self.folder / "out.jsonl"
        payload = {"path": "a", "fmt": "b", "out_path": str(out_path)}
        result = run("fake_connector", "read", payload, timeout_s=10, module=FAKE)
        self.assertEqual(result, {"status": "ok", "count": 1, "message": None})
        lines = out_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        validate_record(json.loads(lines[0]))

    def test_a_stuck_connector_is_killed_and_reaped_at_the_deadline(self) -> None:
        with self.assertRaises(RunnerTimeoutError):
            run("fake_connector", "send", {}, timeout_s=0.5, module=FAKE)
        self.assertNotIn(self.children[0].returncode, (None, 0))

    def test_an_interrupted_wait_still_kills_and_reaps_the_child(self) -> None:
        def interrupted(child: subprocess.Popen, *args: object, **kwargs: object) -> None:
            child._sigint_wait_secs = 0  # what communicate leaves behind after a real Ctrl+C
            raise KeyboardInterrupt

        with (
            mock.patch.object(self.real_popen, "communicate", interrupted),
            self.assertRaises(KeyboardInterrupt),
        ):
            run("fake_connector", "send", {}, timeout_s=10, module=FAKE)
        self.assertNotIn(self.children[0].returncode, (None, 0))

    def test_a_user_error_or_crash_hides_the_secret_and_a_missing_function_is_unsupported(
        self,
    ) -> None:
        payload = {"answers": {"api_key": "s3cret"}}
        with self.assertRaises(ConnectorUserError) as caught:
            run("fake_connector", "connect", payload, timeout_s=10, module=FAKE)
        self.assertEqual(str(caught.exception), "The key [secret] was refused.")
        self.assertEqual(
            run("fake_connector", "fetch", {}, timeout_s=10, module=FAKE), {"unsupported": True}
        )
        for secret in (
            "ab\\cd99",
            "p\u00e4ssw\u00f6rd77",
            "\n".join(f"LINE{n}" for n in range(30)),
        ):
            payload = {"mode": "leak", "secrets": {"key": secret}}
            with self.subTest(secret=secret):
                with (
                    mock.patch.dict(os.environ, {"PYTHONIOENCODING": "cp1252"}),
                    self.assertRaises(RunnerFailedError) as caught,
                ):
                    run("fake_connector", "send", payload, timeout_s=10, module=FAKE)
                message = str(caught.exception)
                self.assertIn("[secret]", message)
                for form in (*secret.splitlines(), repr(secret)[1:-1]):
                    self.assertNotIn(form, message)

    def test_output_that_is_not_a_json_object_fails_with_the_stderr_tail(self) -> None:
        for mode in ("list", "deep"):
            with self.subTest(mode=mode):
                with self.assertRaises(RunnerFailedError) as caught:
                    run("fake_connector", "send", {"mode": mode}, timeout_s=10, module=FAKE)
                self.assertTrue(str(caught.exception).endswith("\nwhy it broke"))

    def test_send_receives_transport_in_the_real_helper(self) -> None:
        result = run("fake_connector", "send", {"mode": "transport"}, timeout_s=10, module=FAKE)
        self.assertEqual(
            result,
            {"transport": "http_request", "error": "Only https:// addresses can be fetched."},
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

    def test_a_connector_folder_that_does_not_exist_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            reader_version("no_such_connector")


class HttpGetTest(unittest.TestCase):
    def test_refuses_a_url_that_is_not_https(self) -> None:
        for url in ("http://example.com/", "file:///etc/hostname"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                http_get(url, {})

    def test_sends_a_post_with_a_body_and_refuses_other_methods(self) -> None:
        with mock.patch.object(runner.urllib.request, "build_opener") as build:
            reply = build.return_value.open.return_value.__enter__.return_value
            reply.status, reply.headers = 200, {}
            reply.read.return_value = b"token"
            result = http_get("https://example.invalid/", {}, method="POST", body=b"data")
            request = build.return_value.open.call_args.args[0]
            self.assertEqual((request.get_method(), request.data), ("POST", b"data"))
            self.assertEqual(result, (200, {}, b"token"))
            with self.assertRaises(ValueError):
                http_get("https://example.invalid/", {}, method="PUT")


class HttpRequestTest(unittest.TestCase):
    def test_exact_request_and_response(self) -> None:
        for method in ("GET", "POST", "PUT"):
            with (
                self.subTest(method=method),
                mock.patch.object(runner.urllib.request, "build_opener") as build,
            ):
                reply = build.return_value.open.return_value.__enter__.return_value
                reply.status, reply.headers = 200, {"Content-Type": "application/json"}
                reply.read.return_value = b'{"id":"routine-1"}'
                body = None if method == "GET" else '{"title":"café ✓"}'.encode()
                result = http_request(
                    method, "https://example.invalid/routines", {"Api-Key": "invented"}, body, 7
                )
                request = build.return_value.open.call_args.args[0]
                self.assertEqual(request.get_method(), method)
                self.assertEqual(request.full_url, "https://example.invalid/routines")
                self.assertEqual(request.data, body)
                self.assertEqual(request.get_header("Api-key"), "invented")
                self.assertEqual(build.return_value.open.call_args.kwargs, {"timeout": 7})
                self.assertEqual(result, (200, reply.headers, b'{"id":"routine-1"}'))
                self.assertEqual(build.return_value.open.call_count, 1)

    def test_http_failure_and_redirect_return_without_resend(self) -> None:
        for status in (401, 429, 500, 301, 302, 303, 307, 308):
            with (
                self.subTest(status=status),
                mock.patch.object(runner.urllib.request, "build_opener") as build,
            ):
                headers = {"Location": "https://other.invalid/", "Retry-After": "30"}
                error = urllib.error.HTTPError(
                    "https://example.invalid/", status, "response", headers, io.BytesIO(b"result")
                )
                build.return_value.open.side_effect = error
                self.assertEqual(
                    http_request("POST", "https://example.invalid/", {}, b"body"),
                    (status, headers, b"result"),
                )
                self.assertEqual(build.return_value.open.call_count, 1)
                handler = build.call_args.args[0]()
                request = urllib.request.Request("https://example.invalid/", data=b"body")
                if 300 <= status < 400:
                    self.assertIsNone(
                        handler.redirect_request(
                            request, None, status, "redirect", headers, headers["Location"]
                        )
                    )

    def test_transport_failure_propagates_once(self) -> None:
        with mock.patch.object(runner.urllib.request, "build_opener") as build:
            build.return_value.open.side_effect = TimeoutError("uncertain")
            with self.assertRaises(TimeoutError):
                http_request("PUT", "https://example.invalid/", {}, b"body")
            self.assertEqual(build.return_value.open.call_count, 1)

    def test_invalid_method_or_scheme_never_opens_a_request(self) -> None:
        with mock.patch.object(runner.urllib.request, "build_opener") as build:
            for method, url in (
                ("DELETE", "https://example.invalid/"),
                ("POST", "http://example.invalid/"),
                ("PUT", "file:///invented"),
            ):
                with self.subTest(method=method, url=url), self.assertRaises(ValueError):
                    http_request(method, url, {})
            build.assert_not_called()


if __name__ == "__main__":
    unittest.main()
