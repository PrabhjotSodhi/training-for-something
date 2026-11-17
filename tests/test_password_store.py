import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tfs import password_store
from tfs.password_store import PasswordStoreError


class PasswordStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name)
        self.secrets_file = self.home / ".env"

        env = mock.patch.dict(os.environ, {"TFS_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)

    def test_set_writes_one_line_that_get_reads_back(self):
        self.assertIsNone(password_store.get("hevy:api_key"))
        password_store.set("hevy:api_key", "abc123")
        self.assertEqual(self.secrets_file.read_bytes(), b"HEVY_API_KEY=abc123\n")
        self.assertEqual(password_store.get("hevy:api_key"), "abc123")

    def test_delete_removes_only_the_given_name(self):
        self.secrets_file.write_bytes(b"HEVY_API_KEY=abc123\nPUSH_TOKEN=xyz789\n")
        password_store.delete("hevy:api_key")
        self.assertEqual(self.secrets_file.read_bytes(), b"PUSH_TOKEN=xyz789\n")
        password_store.delete("hevy:api_key")
        self.assertEqual(self.secrets_file.read_bytes(), b"PUSH_TOKEN=xyz789\n")

    def test_a_value_may_hold_an_equals_sign_but_not_a_line_break(self):
        password_store.set("hevy:api_key", "a=b=c")
        self.assertEqual(password_store.get("hevy:api_key"), "a=b=c")
        for value in ("abc\ndef", "abc\rdef"):
            with self.subTest(value=value), self.assertRaises(PasswordStoreError):
                password_store.set("push_token", value)
        self.assertEqual(self.secrets_file.read_bytes(), b"HEVY_API_KEY=a=b=c\n")

    def test_a_damaged_file_is_an_error_that_names_the_path_and_hides_the_content(self):
        for content in (b"HEVY_API_KEY=abc123\ninvented-secret-value\n", b"invented\xff"):
            self.secrets_file.write_bytes(content)
            with self.subTest(content=content), self.assertRaises(PasswordStoreError) as caught:
                password_store.get("hevy:api_key")
            self.assertEqual(
                str(caught.exception),
                f"The password file {self.secrets_file} is damaged. "
                "Delete it and connect your apps again.",
            )
            self.assertIsNone(caught.exception.__context__)

    @unittest.skipIf(sys.platform == "win32", "Windows has no POSIX file modes")
    def test_the_file_is_private_under_a_permissive_umask(self):
        self.addCleanup(os.umask, os.umask(0))
        password_store.set("hevy:api_key", "abc123")
        password_store.set("push_token", "xyz789")
        self.assertEqual(stat.S_IMODE(self.secrets_file.stat().st_mode), 0o600)

    def test_a_failed_write_leaves_the_old_file_and_no_temporary_file(self):
        password_store.set("hevy:api_key", "abc123")
        with (
            mock.patch.object(password_store.os, "fsync", side_effect=OSError("disk full")),
            self.assertRaises(OSError),
        ):
            password_store.set("hevy:api_key", "def456")
        self.assertEqual(password_store.get("hevy:api_key"), "abc123")
        self.assertEqual([path.name for path in self.home.iterdir()], [".env"])


if __name__ == "__main__":
    unittest.main()
