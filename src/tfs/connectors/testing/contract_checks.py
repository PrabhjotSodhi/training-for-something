"""The contract checks every connector's tests share (HLD section 6.1).

A connector's test class inherits `ConnectorContract` and `unittest.TestCase`, then sets:

`module`
    The connector module, like "tfs.connectors.hevy.connector". `manifest.json` sits next to it.
`fixtures`
    `(path, fmt)` pairs of invented raw files the connector can read.
`delete_fixture`
    A `(path, fmt)` pair whose read gives a `deleted` record, or None.
`fetch_args`
    `{"cursor": ..., "secrets": {...}}` for the auth check, when the connector has `fetch`.
"""

import importlib
import json
import tempfile
from pathlib import Path
from types import ModuleType

from tfs.connectors.core.contract import load_manifest, validate_record

_RESPONSES_THAT_MEAN_AUTH_FAILED = (401, 403)
_NOW = "2026-10-27T07:00:00Z"


class ConnectorContract:
    """Mixin for `unittest.TestCase` that adds the shared contract tests."""

    module: str
    fixtures: list[tuple[str, str]]
    delete_fixture: tuple[str, str] | None = None
    fetch_args: dict | None = None

    def test_read_twice_same_keys(self) -> None:
        """Reading each fixture twice gives the same non-empty keys, all valid records."""
        for path, fmt in self.fixtures:
            with self.subTest(fixture=path):
                first = self._read_records(path, fmt)
                second = self._read_records(path, fmt)
                self.assertTrue(first, "the fixture gave no records")
                self.assertEqual(_keys(first), _keys(second))
                for record in first:
                    validate_record(record)

    def test_manifest_matches_code(self) -> None:
        """The manifest passes its checks and agrees with the functions the module has."""
        module = self._module()
        manifest = load_manifest(Path(module.__file__).with_name("manifest.json"))
        self.assertEqual(manifest["routes"]["fetch"], hasattr(module, "fetch"))
        if manifest["sends"]:
            self.assertTrue(hasattr(module, "send"), "sends is not empty but there is no send")
        if manifest["notices_deletes"]:
            self.assertIsNotNone(
                self.delete_fixture, "notices_deletes is true but there is no delete_fixture"
            )
            path, fmt = self.delete_fixture
            kinds = {record["kind"] for record in self._read_records(path, fmt)}
            self.assertIn("deleted", kinds, "the delete fixture gave no deleted record")

    def test_auth_failed(self) -> None:
        """A 401 or 403 from the app gives auth_failed, no files and the same cursor."""
        module = self._module()
        if not hasattr(module, "fetch"):
            return
        cursor = self.fetch_args["cursor"]
        for status in _RESPONSES_THAT_MEAN_AUTH_FAILED:
            with self.subTest(status=status), tempfile.TemporaryDirectory() as out_dir:
                result = module.fetch(
                    cursor=cursor,
                    secrets=self.fetch_args["secrets"],
                    out_dir=out_dir,
                    http=lambda url, headers, timeout_s=30, status=status: (status, {}, b""),
                    now=_NOW,
                )
                self.assertTrue(result["auth_failed"])
                self.assertEqual(result["files"], [])
                self.assertEqual(result["cursor"], cursor)

    def _module(self) -> ModuleType:
        return importlib.import_module(self.module)

    def _read_records(self, path: str, fmt: str) -> list[dict]:
        with tempfile.TemporaryDirectory() as folder:
            out_path = Path(folder) / "records.jsonl"
            self._module().read(path, fmt, str(out_path))
            lines = out_path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines]


def _keys(records: list[dict]) -> set[tuple[str, str, str]]:
    return {(record["kind"], record["stream"], record["key"]) for record in records}
