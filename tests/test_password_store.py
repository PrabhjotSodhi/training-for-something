import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import keyring
import keyring.backend
import keyring.backends.fail
import keyring.core
import keyring.errors

from tfs import password_store
from tfs.password_store import PasswordStoreError

LOCKED_MESSAGE = "The OS password store is locked or unavailable."


class MemoryKeyring(keyring.backend.KeyringBackend):
    """A password store that lives in a dict."""

    priority = 1

    def __init__(self) -> None:
        super().__init__()
        self.items: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self.items.get((service, username))

    def set_password(self, service, username, password):
        self.items[(service, username)] = password

    def delete_password(self, service, username):
        if (service, username) not in self.items:
            raise keyring.errors.PasswordDeleteError("not found")
        del self.items[(service, username)]


class LockedKeyring(MemoryKeyring):
    """A password store that refuses every call."""

    def get_password(self, service, username):
        raise keyring.errors.KeyringLocked("locked")

    def set_password(self, service, username, password):
        raise keyring.errors.KeyringLocked("locked")

    def delete_password(self, service, username):
        raise keyring.errors.KeyringLocked("locked")


class PasswordStoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name)
        self.secrets_file = self.home / "secrets.json"

        env = mock.patch.dict(os.environ, {"TFS_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("TFS_PASSWORD_STORE", None)

        # Reading keyring's current backend would probe the real password store.
        # Starting from the fail backend keeps every test away from it, and
        # patching the module setting restores it however a test changes it.
        backend = mock.patch.object(
            keyring.core, "_keyring_backend", keyring.backends.fail.Keyring()
        )
        backend.start()
        self.addCleanup(backend.stop)

    def use_file(self) -> None:
        os.environ["TFS_PASSWORD_STORE"] = "file"


class FileStoreTest(PasswordStoreTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.use_file()

    def test_set_then_get_returns_the_value(self):
        password_store.set("hevy:api_key", "abc")
        self.assertEqual(password_store.get("hevy:api_key"), "abc")

    @unittest.skipIf(sys.platform == "win32", "Windows has no POSIX file modes")
    def test_file_is_private_to_the_user(self):
        password_store.set("hevy:api_key", "abc")
        self.assertEqual(stat.S_IMODE(self.secrets_file.stat().st_mode), 0o600)

    @unittest.skipIf(sys.platform == "win32", "Windows has no POSIX file modes")
    def test_file_stays_private_after_a_second_write(self):
        password_store.set("hevy:api_key", "abc")
        password_store.set("hevy:api_key", "def")
        self.assertEqual(stat.S_IMODE(self.secrets_file.stat().st_mode), 0o600)

    def test_second_set_replaces_the_value(self):
        password_store.set("hevy:api_key", "abc")
        password_store.set("hevy:api_key", "def")
        self.assertEqual(password_store.get("hevy:api_key"), "def")

    def test_names_do_not_mix(self):
        password_store.set("hevy:api_key", "abc")
        password_store.set("other:token", "xyz")
        self.assertEqual(password_store.get("hevy:api_key"), "abc")
        self.assertEqual(password_store.get("other:token"), "xyz")

    def test_get_of_an_unknown_name_is_none(self):
        self.assertIsNone(password_store.get("hevy:api_key"))

    def test_delete_removes_the_value(self):
        password_store.set("hevy:api_key", "abc")
        password_store.delete("hevy:api_key")
        self.assertIsNone(password_store.get("hevy:api_key"))

    def test_delete_keeps_other_names(self):
        password_store.set("hevy:api_key", "abc")
        password_store.set("other:token", "xyz")
        password_store.delete("hevy:api_key")
        self.assertEqual(password_store.get("other:token"), "xyz")

    def test_delete_of_an_unknown_name_does_nothing(self):
        password_store.delete("hevy:api_key")
        self.assertIsNone(password_store.get("hevy:api_key"))

    def test_the_file_does_not_touch_the_os_password_store(self):
        locked = LockedKeyring()
        keyring.set_keyring(locked)
        password_store.set("hevy:api_key", "abc")
        self.assertEqual(password_store.get("hevy:api_key"), "abc")

    def test_no_temporary_file_is_left_behind(self):
        password_store.set("hevy:api_key", "abc")
        self.assertEqual([path.name for path in self.home.iterdir()], ["secrets.json"])

    def test_set_creates_a_missing_data_folder(self):
        os.environ["TFS_HOME"] = str(self.home / "new" / "folder")
        password_store.set("hevy:api_key", "abc")
        self.assertEqual(password_store.get("hevy:api_key"), "abc")

    def test_a_file_that_is_not_json_is_an_error_that_hides_its_content(self):
        self.secrets_file.write_text("hevy-secret-value", encoding="utf-8")
        with self.assertRaises(PasswordStoreError) as caught:
            password_store.get("hevy:api_key")
        self.assertNotIn("hevy-secret-value", str(caught.exception))
        self.assertIn("secrets.json", str(caught.exception))

    def test_a_file_that_is_not_text_is_an_error_that_carries_no_content(self):
        self.secrets_file.write_bytes(b"\xff")
        with self.assertRaises(PasswordStoreError) as caught:
            password_store.get("hevy:api_key")
        self.assertEqual(
            str(caught.exception),
            f"The password file {self.secrets_file} is damaged. "
            "Delete it and connect your apps again.",
        )
        self.assertIsNone(caught.exception.__context__)
        self.assertIsNone(caught.exception.__cause__)

    def test_a_file_that_is_not_a_json_object_is_an_error(self):
        self.secrets_file.write_text(json.dumps(["abc"]), encoding="utf-8")
        with self.assertRaises(PasswordStoreError):
            password_store.get("hevy:api_key")

    def test_a_file_nested_too_deep_to_read_is_a_damaged_file_error(self):
        self.secrets_file.write_text("[" * 100000, encoding="utf-8")
        with self.assertRaises(PasswordStoreError) as caught:
            password_store.get("hevy:api_key")
        self.assertEqual(
            str(caught.exception),
            f"The password file {self.secrets_file} is damaged. "
            "Delete it and connect your apps again.",
        )

    def test_a_file_with_a_value_that_is_not_text_is_a_damaged_file_error(self):
        self.secrets_file.write_text(json.dumps({"a": 1}), encoding="utf-8")
        with self.assertRaises(PasswordStoreError) as caught:
            password_store.get("a")
        self.assertEqual(
            str(caught.exception),
            f"The password file {self.secrets_file} is damaged. "
            "Delete it and connect your apps again.",
        )

    def test_a_failed_write_leaves_the_old_file_and_no_temporary_file(self):
        password_store.set("hevy:api_key", "abc")
        with (
            mock.patch.object(Path, "replace", side_effect=OSError("disk full")),
            self.assertRaises(OSError),
        ):
            password_store.set("hevy:api_key", "def")
        self.assertEqual(password_store.get("hevy:api_key"), "abc")
        self.assertEqual([path.name for path in self.home.iterdir()], ["secrets.json"])

    def test_a_write_that_finishes_inside_another_write_does_not_break_it(self):
        real_fsync = os.fsync
        calls = []

        def second_writer_runs_first(descriptor):
            calls.append(descriptor)
            if len(calls) == 1:
                password_store.set("other:token", "xyz")
            real_fsync(descriptor)

        with mock.patch.object(password_store.os, "fsync", second_writer_runs_first):
            password_store.set("hevy:api_key", "abc")
        self.assertEqual(password_store.get("hevy:api_key"), "abc")
        self.assertEqual([path.name for path in self.home.iterdir()], ["secrets.json"])

    def test_a_write_that_fails_inside_another_write_does_not_break_it(self):
        real_fsync = os.fsync
        calls = []

        def second_writer_fails_first(descriptor):
            calls.append(descriptor)
            if len(calls) == 1:
                with (
                    mock.patch.object(password_store.os, "fsync", side_effect=OSError("disk full")),
                    self.assertRaises(OSError),
                ):
                    password_store.set("other:token", "xyz")
            real_fsync(descriptor)

        with mock.patch.object(password_store.os, "fsync", second_writer_fails_first):
            password_store.set("hevy:api_key", "abc")
        self.assertEqual(
            json.loads(self.secrets_file.read_text(encoding="utf-8")), {"hevy:api_key": "abc"}
        )
        self.assertEqual([path.name for path in self.home.iterdir()], ["secrets.json"])

    def test_a_write_that_stops_half_way_leaves_the_old_file(self):
        password_store.set("hevy:api_key", "abc")
        with (
            mock.patch.object(password_store.json, "dump", side_effect=KeyboardInterrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            password_store.set("hevy:api_key", "def")
        self.assertEqual(password_store.get("hevy:api_key"), "abc")
        self.assertEqual([path.name for path in self.home.iterdir()], ["secrets.json"])

    @unittest.skipIf(sys.platform == "win32", "Windows has no POSIX file modes")
    def test_the_file_is_private_under_a_permissive_umask(self):
        old_mask = os.umask(0)
        self.addCleanup(os.umask, old_mask)
        password_store.set("hevy:api_key", "abc")
        self.assertEqual(stat.S_IMODE(self.secrets_file.stat().st_mode), 0o600)


class OsPasswordStoreTest(PasswordStoreTestCase):
    def test_values_go_to_the_backend_and_no_file_is_created(self):
        memory = MemoryKeyring()
        keyring.set_keyring(memory)
        password_store.set("hevy:api_key", "abc")
        self.assertEqual(password_store.get("hevy:api_key"), "abc")
        self.assertEqual(memory.items, {("training-for-something", "hevy:api_key"): "abc"})
        self.assertFalse(self.secrets_file.exists())

    def test_delete_removes_the_value_from_the_backend(self):
        memory = MemoryKeyring()
        keyring.set_keyring(memory)
        password_store.set("hevy:api_key", "abc")
        password_store.delete("hevy:api_key")
        self.assertIsNone(password_store.get("hevy:api_key"))
        self.assertEqual(memory.items, {})

    def test_delete_of_an_unknown_name_does_nothing(self):
        keyring.set_keyring(MemoryKeyring())
        password_store.delete("hevy:api_key")
        self.assertIsNone(password_store.get("hevy:api_key"))

    def test_file_setting_wins_over_the_backend(self):
        memory = MemoryKeyring()
        keyring.set_keyring(memory)
        self.use_file()
        password_store.set("hevy:api_key", "abc")
        self.assertEqual(memory.items, {})
        self.assertTrue(self.secrets_file.exists())


class FailBackendTest(PasswordStoreTestCase):
    def test_values_go_to_the_file(self):
        keyring.set_keyring(keyring.backends.fail.Keyring())
        with mock.patch.object(password_store.sys, "platform", "linux"):
            password_store.set("hevy:api_key", "abc")
            self.assertEqual(password_store.get("hevy:api_key"), "abc")
        self.assertEqual(
            json.loads(self.secrets_file.read_text(encoding="utf-8")), {"hevy:api_key": "abc"}
        )

    def test_delete_removes_the_value_from_the_file(self):
        keyring.set_keyring(keyring.backends.fail.Keyring())
        with mock.patch.object(password_store.sys, "platform", "linux"):
            password_store.set("hevy:api_key", "abc")
            password_store.delete("hevy:api_key")
            self.assertIsNone(password_store.get("hevy:api_key"))

    def test_windows_without_a_backend_is_an_error_and_writes_no_file(self):
        keyring.set_keyring(keyring.backends.fail.Keyring())
        with mock.patch.object(password_store.sys, "platform", "win32"):
            for call in (
                lambda: password_store.get("hevy:api_key"),
                lambda: password_store.set("hevy:api_key", "abc"),
                lambda: password_store.delete("hevy:api_key"),
            ):
                with self.assertRaises(PasswordStoreError) as caught:
                    call()
                self.assertEqual(
                    str(caught.exception), "Windows has no OS password store that TFS can use."
                )
        self.assertFalse(self.secrets_file.exists())

    def test_windows_with_the_file_setting_uses_the_file(self):
        self.use_file()
        with mock.patch.object(password_store.sys, "platform", "win32"):
            password_store.set("hevy:api_key", "abc")
            self.assertEqual(password_store.get("hevy:api_key"), "abc")


class LockedBackendTest(PasswordStoreTestCase):
    def setUp(self) -> None:
        super().setUp()
        keyring.set_keyring(LockedKeyring())

    def test_get_raises_and_does_not_fall_back_to_the_file(self):
        self.secrets_file.write_text(json.dumps({"hevy:api_key": "abc"}), encoding="utf-8")
        with self.assertRaises(PasswordStoreError) as caught:
            password_store.get("hevy:api_key")
        self.assertEqual(str(caught.exception), LOCKED_MESSAGE)

    def test_set_raises_and_writes_no_file(self):
        with self.assertRaises(PasswordStoreError) as caught:
            password_store.set("hevy:api_key", "abc")
        self.assertEqual(str(caught.exception), LOCKED_MESSAGE)
        self.assertFalse(self.secrets_file.exists())

    def test_delete_raises(self):
        with self.assertRaises(PasswordStoreError) as caught:
            password_store.delete("hevy:api_key")
        self.assertEqual(str(caught.exception), LOCKED_MESSAGE)

    def test_any_other_keyring_error_raises_the_same_error(self):
        class BrokenKeyring(MemoryKeyring):
            def set_password(self, service, username, password):
                raise keyring.errors.PasswordSetError("rejected abc")

        keyring.set_keyring(BrokenKeyring())
        with self.assertRaises(PasswordStoreError) as caught:
            password_store.set("hevy:api_key", "abc")
        self.assertEqual(str(caught.exception), LOCKED_MESSAGE)
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)
        self.assertFalse(self.secrets_file.exists())

    def test_a_read_error_that_holds_the_secret_bytes_raises_the_same_error(self):
        class UndecodableKeyring(MemoryKeyring):
            def get_password(self, service, username):
                return b"made-up-secret\xff".decode("utf-8")

        keyring.set_keyring(UndecodableKeyring())
        with self.assertRaises(PasswordStoreError) as caught:
            password_store.get("hevy:api_key")
        self.assertEqual(str(caught.exception), LOCKED_MESSAGE)
        self.assertIsNone(caught.exception.__context__)
        self.assertIsNone(caught.exception.__cause__)

    def test_a_write_error_that_is_not_a_keyring_error_raises_the_same_error(self):
        class OsErrorKeyring(MemoryKeyring):
            def set_password(self, service, username, password):
                raise OSError(1783, "The stub received bad data.")

        keyring.set_keyring(OsErrorKeyring())
        with self.assertRaises(PasswordStoreError) as caught:
            password_store.set("hevy:api_key", "abc")
        self.assertEqual(str(caught.exception), LOCKED_MESSAGE)
        self.assertIsNone(caught.exception.__context__)
        self.assertIsNone(caught.exception.__cause__)
        self.assertFalse(self.secrets_file.exists())


class BackendDiscoveryTest(PasswordStoreTestCase):
    def test_a_discovery_failure_raises_the_same_error_and_writes_no_file(self):
        failures = (
            keyring.errors.KeyringError("backend detail abc"),
            ImportError("backend detail abc"),
            ValueError("backend detail abc"),
        )
        for failure in failures:
            for call in (
                lambda: password_store.get("hevy:api_key"),
                lambda: password_store.set("hevy:api_key", "abc"),
                lambda: password_store.delete("hevy:api_key"),
            ):
                with (
                    self.subTest(failure=failure),
                    mock.patch.object(password_store.keyring, "get_keyring", side_effect=failure),
                    self.assertRaises(PasswordStoreError) as caught,
                ):
                    call()
                self.assertEqual(
                    str(caught.exception),
                    "The OS password store could not be set up. "
                    "Set TFS_PASSWORD_STORE=file to use a private file instead.",
                )
                self.assertIs(caught.exception.__cause__, failure)
        self.assertFalse(self.secrets_file.exists())

    def test_the_file_setting_skips_discovery(self):
        self.use_file()
        with mock.patch.object(
            password_store.keyring, "get_keyring", side_effect=keyring.errors.KeyringError("x")
        ):
            password_store.set("hevy:api_key", "abc")
            self.assertEqual(password_store.get("hevy:api_key"), "abc")


if __name__ == "__main__":
    unittest.main()
