import json
import sys
import tempfile
import unittest
from itertools import count
from pathlib import Path
from typing import ClassVar

from tfs.connectors.testing.contract_checks import ConnectorContract

FAKE_FOLDER = Path(__file__).parent / "fake_connector"
FIXTURE = ("fixture.bin", "fake")
_names = count()


class FakeConnectorPasses(ConnectorContract, unittest.TestCase):
    module = "tests.fake_connector.connector"
    fixtures: ClassVar = [FIXTURE]


class ContractFailuresTest(unittest.TestCase):
    def _case(self, source: str, manifest: dict | None = None, **settings: object):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        package = f"fake_contract_{next(_names)}"
        root = Path(folder.name)
        (root / package).mkdir()
        (root / package / "__init__.py").write_text("", encoding="utf-8")
        (root / package / "connector.py").write_text(source, encoding="utf-8")
        base = json.loads((FAKE_FOLDER / "manifest.json").read_text(encoding="utf-8"))
        (root / package / "manifest.json").write_text(
            json.dumps({**base, **(manifest or {})}), encoding="utf-8"
        )
        sys.path.insert(0, folder.name)
        self.addCleanup(sys.path.remove, folder.name)
        self.addCleanup(
            lambda: [
                sys.modules.pop(name) for name in list(sys.modules) if name.startswith(package)
            ]
        )
        return type(
            "Case",
            (ConnectorContract, unittest.TestCase),
            {"module": f"{package}.connector", "fixtures": [FIXTURE], **settings},
        )

    def test_manifest_says_fetch_but_code_has_none(self) -> None:
        case = self._case(
            "from tests.fake_connector.connector import read\n",
            {"routes": {"fetch": True, "push": False, "files": []}},
        )
        with self.assertRaises(AssertionError):
            case("test_manifest_matches_code").test_manifest_matches_code()

    def test_read_gives_different_keys_the_second_time(self) -> None:
        case = self._case(
            "import json\n"
            "from pathlib import Path\n"
            "from tests.fake_connector.connector import RECORD\n"
            "calls = []\n"
            "def read(path, fmt, out_path):\n"
            "    calls.append(1)\n"
            "    record = {**RECORD, 'key': f'k{len(calls)}'}\n"
            "    Path(out_path).write_text(json.dumps(record) + '\\n')\n"
            "    return {'status': 'ok', 'count': 1, 'message': None}\n"
        )
        with self.assertRaises(AssertionError):
            case("test_read_twice_same_keys").test_read_twice_same_keys()

    def test_fetch_ignores_a_401(self) -> None:
        case = self._case(
            "from tests.fake_connector.connector import read\n"
            "def fetch(cursor, secrets, out_dir, http, now):\n"
            "    http('https://example.com', {})\n"
            "    return {'files': [], 'cursor': cursor, 'auth_failed': False, 'wait_until': None}\n",
            {"routes": {"fetch": True, "push": False, "files": []}},
            fetch_args={"cursor": None, "secrets": {"api_key": "invented"}},
        )
        with self.assertRaises(AssertionError):
            case("test_auth_failed").test_auth_failed()

    def test_no_fixtures(self) -> None:
        case = self._case("from tests.fake_connector.connector import read\n", fixtures=[])
        with self.assertRaisesRegex(AssertionError, "fixtures is empty"):
            case("test_read_twice_same_keys").test_read_twice_same_keys()

    def test_second_read_gives_an_invalid_record(self) -> None:
        case = self._case(
            "import json\n"
            "from pathlib import Path\n"
            "from tests.fake_connector.connector import RECORD\n"
            "calls = []\n"
            "def read(path, fmt, out_path):\n"
            "    calls.append(1)\n"
            "    values = {'value': 5.0, 'unit': 'kcal' if len(calls) > 1 else 'count'}\n"
            "    Path(out_path).write_text(json.dumps({**RECORD, 'values': values}) + '\\n')\n"
            "    return {'status': 'ok', 'count': 1, 'message': None}\n"
        )
        with self.assertRaises(AssertionError):
            case("test_read_twice_same_keys").test_read_twice_same_keys()

    def test_delete_fixture_gives_a_deleted_record_with_no_key(self) -> None:
        case = self._case(
            "import json\n"
            "from pathlib import Path\n"
            "from tests.fake_connector.connector import read as fake_read\n"
            "def read(path, fmt, out_path):\n"
            "    if path != 'delete.bin':\n"
            "        return fake_read(path, fmt, out_path)\n"
            "    Path(out_path).write_text(json.dumps({'kind': 'deleted'}) + '\\n')\n"
            "    return {'status': 'ok', 'count': 1, 'message': None}\n",
            {"notices_deletes": True},
            delete_fixture=("delete.bin", "fake"),
        )
        with self.assertRaises(AssertionError):
            case("test_manifest_matches_code").test_manifest_matches_code()
