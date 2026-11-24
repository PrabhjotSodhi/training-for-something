import copy
import json
import tempfile
import unittest
from pathlib import Path

from tfs.connectors.core.contract import ManifestError, load_manifest, load_manifests

HEVY = {
    "label": "Hevy",
    "about": "Reads your workouts with your Hevy API key. Hevy can change its API at any time.",
    "routes": {"fetch": True, "push": False, "files": []},
    "asks": [{"key": "api_key", "label": "API key", "secret": True}],
    "stale_after_hours": 168,
    "app_ids": ["com.hevyapp.hevy"],
    "notices_deletes": True,
    "sends": ["strength"],
}


class ManifestTestCase(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def write_manifest(self, manifest: dict, name: str = "alpha") -> Path:
        path = self.folder / name / "manifest.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(manifest), encoding="utf-8")
        return path

    def assert_refused(self, manifest: dict, field: str) -> None:
        path = self.write_manifest(manifest)
        with self.assertRaises(ManifestError) as caught:
            load_manifest(path)
        self.assertIn(field, str(caught.exception))


class LoadManifestTest(ManifestTestCase):
    def test_accepts_the_example_from_the_design(self) -> None:
        path = self.write_manifest(HEVY)
        self.assertEqual(load_manifest(path), HEVY)

    def test_missing_field_is_named(self) -> None:
        manifest = copy.deepcopy(HEVY)
        del manifest["sends"]
        self.assert_refused(manifest, "sends")

    def test_extra_field_is_named(self) -> None:
        self.assert_refused({**HEVY, "colour": "red"}, "colour")

    def test_stale_after_hours_must_be_above_zero(self) -> None:
        self.assert_refused({**HEVY, "stale_after_hours": 0}, "stale_after_hours")

    def test_stale_after_hours_must_be_a_whole_number(self) -> None:
        self.assert_refused({**HEVY, "stale_after_hours": True}, "stale_after_hours")
        self.assert_refused({**HEVY, "stale_after_hours": 1.5}, "stale_after_hours")

    def test_sends_must_hold_known_families(self) -> None:
        self.assert_refused({**HEVY, "sends": ["running"]}, "sends")

    def test_text_fields_must_be_text(self) -> None:
        self.assert_refused({**HEVY, "label": 7}, "label")
        self.assert_refused({**HEVY, "about": None}, "about")

    def test_notices_deletes_must_be_true_or_false(self) -> None:
        self.assert_refused({**HEVY, "notices_deletes": "yes"}, "notices_deletes")

    def test_app_ids_must_be_a_list_of_text(self) -> None:
        self.assert_refused({**HEVY, "app_ids": "com.hevyapp.hevy"}, "app_ids")
        self.assert_refused({**HEVY, "app_ids": [3]}, "app_ids")

    def test_routes_fields_are_checked(self) -> None:
        routes = HEVY["routes"]
        self.assert_refused({**HEVY, "routes": {"fetch": True, "push": False}}, "routes.files")
        self.assert_refused({**HEVY, "routes": {**routes, "fetch": 1}}, "routes.fetch")
        self.assert_refused({**HEVY, "routes": {**routes, "files": [1]}}, "routes.files")
        self.assert_refused({**HEVY, "routes": {**routes, "extra": 1}}, "routes.extra")
        self.assert_refused({**HEVY, "routes": []}, "routes")

    def test_asks_fields_are_checked(self) -> None:
        ask = HEVY["asks"][0]
        self.assert_refused({**HEVY, "asks": ask}, "asks")
        self.assert_refused(
            {**HEVY, "asks": [{"key": "api_key", "label": "API key"}]}, "asks[0].secret"
        )
        self.assert_refused({**HEVY, "asks": [{**ask, "secret": "no"}]}, "asks[0].secret")
        self.assert_refused({**HEVY, "asks": [{**ask, "hint": "x"}]}, "asks[0].hint")

    def test_an_empty_asks_list_is_allowed(self) -> None:
        path = self.write_manifest({**HEVY, "asks": []})
        self.assertEqual(load_manifest(path)["asks"], [])

    def test_setup_is_optional_and_its_fields_are_checked(self) -> None:
        setup = {
            "steps": ["Needs Pro."],
            "links": [{"label": "one", "url": "app://x?u={push_url}"}],
            "opens": "https://example.com/key",
        }
        path = self.write_manifest({**HEVY, "setup": setup})
        self.assertEqual(load_manifest(path)["setup"], setup)
        self.assert_refused({**HEVY, "setup": {"steps": "Needs Pro."}}, "setup.steps")
        self.assert_refused({**HEVY, "setup": {"links": [{"label": "one"}]}}, "setup.links[0].url")
        self.assert_refused({**HEVY, "setup": {"colour": "red"}}, "setup.colour")

    def test_file_that_is_not_json_is_refused(self) -> None:
        path = self.folder / "manifest.json"
        path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ManifestError) as caught:
            load_manifest(path)
        self.assertIn(str(path), str(caught.exception))

    def test_manifest_that_is_not_an_object_is_refused(self) -> None:
        path = self.folder / "manifest.json"
        path.write_text("[]", encoding="utf-8")
        with self.assertRaises(ManifestError):
            load_manifest(path)

    def test_missing_file_is_refused(self) -> None:
        with self.assertRaises(ManifestError) as caught:
            load_manifest(self.folder / "nowhere" / "manifest.json")
        self.assertIn("nowhere", str(caught.exception))


class LoadManifestsTest(ManifestTestCase):
    def test_reads_connector_folders_and_skips_core_and_testing(self) -> None:
        self.write_manifest(HEVY, "alpha")
        self.write_manifest(HEVY, "core")
        self.write_manifest(HEVY, "testing")
        self.assertEqual(load_manifests(root=self.folder), {"alpha": HEVY})

    def test_skips_folders_without_a_manifest(self) -> None:
        self.write_manifest(HEVY, "alpha")
        (self.folder / "__pycache__").mkdir()
        self.assertEqual(list(load_manifests(root=self.folder)), ["alpha"])

    def test_a_bad_manifest_names_its_connector(self) -> None:
        self.write_manifest({**HEVY, "colour": "red"}, "alpha")
        with self.assertRaises(ManifestError) as caught:
            load_manifests(root=self.folder)
        self.assertIn("alpha", str(caught.exception))
        self.assertIn("colour", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
