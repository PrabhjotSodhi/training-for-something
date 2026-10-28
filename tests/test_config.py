import os
import unittest
from pathlib import Path
from unittest import mock

from tfs import config


class DataDirTest(unittest.TestCase):
    def test_tfs_home_wins_on_every_platform(self):
        for platform in ("win32", "darwin", "linux"):
            with self.subTest(platform=platform):
                result = config.data_dir(
                    env={"TFS_HOME": "/tmp/tfs-test"}, platform=platform, home=Path("/home/ana")
                )
                self.assertEqual(result, Path("/tmp/tfs-test"))

    def test_windows_uses_localappdata(self):
        result = config.data_dir(
            env={"LOCALAPPDATA": "C:\\Users\\ana\\AppData\\Local"},
            platform="win32",
            home=Path("C:\\Users\\ana"),
        )
        self.assertEqual(result, Path("C:\\Users\\ana\\AppData\\Local") / "TFS")

    def test_windows_without_localappdata_uses_home(self):
        home = Path("C:\\Users\\ana")
        result = config.data_dir(env={}, platform="win32", home=home)
        self.assertEqual(result, home / "AppData" / "Local" / "TFS")

    def test_macos_uses_application_support(self):
        result = config.data_dir(env={}, platform="darwin", home=Path("/Users/ana"))
        self.assertEqual(result, Path("/Users/ana/Library/Application Support/TFS"))

    def test_linux_defaults_to_local_share(self):
        result = config.data_dir(env={}, platform="linux", home=Path("/home/ana"))
        self.assertEqual(result, Path("/home/ana/.local/share/tfs"))

    def test_linux_uses_xdg_data_home(self):
        result = config.data_dir(
            env={"XDG_DATA_HOME": "/data"}, platform="linux", home=Path("/home/ana")
        )
        self.assertEqual(result, Path("/data/tfs"))

    def test_missing_env_reads_the_process_environment(self):
        with mock.patch.dict(os.environ, {"TFS_HOME": "/tmp/tfs-from-os"}):
            self.assertEqual(
                config.data_dir(env=None, platform="linux", home=Path("/home/ana")),
                Path("/tmp/tfs-from-os"),
            )


class PathsTest(unittest.TestCase):
    def test_db_path_and_raw_dir_sit_in_the_data_folder(self):
        args = {"env": {}, "platform": "linux", "home": Path("/home/ana")}
        self.assertEqual(config.db_path(**args), Path("/home/ana/.local/share/tfs/tfs.db"))
        self.assertEqual(config.raw_dir(**args), Path("/home/ana/.local/share/tfs/raw"))

    def test_db_path_follows_tfs_home(self):
        self.assertEqual(
            config.db_path(
                env={"TFS_HOME": "/tmp/tfs-test"}, platform="linux", home=Path("/home/ana")
            ),
            Path("/tmp/tfs-test/tfs.db"),
        )


class ConstantsTest(unittest.TestCase):
    def test_ports_and_host(self):
        self.assertEqual(config.SCREEN_HOST, "127.0.0.1")
        self.assertEqual(config.SCREEN_PORT, 47823)
        self.assertEqual(config.HOME_WIFI_PORT, 47824)


if __name__ == "__main__":
    unittest.main()
