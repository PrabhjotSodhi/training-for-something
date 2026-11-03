import io
import json
from pathlib import Path

from tests.test_read_file import NOW, SessionTest
from tfs.connectors.core.runner import reader_version
from tfs.importer.raw_store import save_raw
from tfs.importer.session import process_pending, rebuild_pending

THIS = "tests.test_reader_replay"
STOPPED: set[bytes] = set()


def read(path: str, fmt: str, out_path: str) -> dict:
    """A reader that emits one record per file: "key@label" is the key k with that label."""
    content = Path(path).read_bytes()
    lines = []
    if content not in STOPPED:
        key, _, label = content.decode().partition("@")
        lines.append(
            json.dumps(
                {
                    "kind": "session" if key.startswith("run") else "event",
                    "stream": "records",
                    "key": key,
                    "revision_at": None,
                    "start_utc": "2026-02-28T18:12:33Z",
                    "end_utc": "2026-02-28T18:42:33Z",
                    "utc_offset_min": 0,
                    "local_date": "2026-02-28",
                    "label": label,
                    "family": "run" if key.startswith("run") else None,
                    "metric": None,
                    "values": {},
                    "origin_app": None,
                    "origin_device": None,
                    "plan_ref": None,
                }
            )
        )
    Path(out_path).write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    return {"status": "ok", "count": len(lines), "message": None}


class ReaderReplayTest(SessionTest):
    def save_for(self, connector: str, data: bytes) -> str:
        return save_raw(
            self.conn, self.data_dir, connector, "records", "json", io.BytesIO(data), now=NOW
        ).sha256

    def labels(self) -> list[tuple[str, str]]:
        rows = self.conn.execute(
            "SELECT connector, label FROM source_records ORDER BY connector, key"
        ).fetchall()
        return [tuple(row) for row in rows]

    def read_all_then_update_reader(self, stopped: set[bytes]) -> None:
        process_pending(self.conn, self.data_dir, module=THIS)
        rebuild_pending(self.conn)
        self.conn.execute(
            "UPDATE files SET reader_version = 'older' WHERE connector = 'fake_connector'"
        )
        STOPPED.update(stopped)
        self.addCleanup(STOPPED.clear)

    def test_a_new_reader_version_rereads_the_files_in_arrival_order(self) -> None:
        self.save_for("fake_connector", b"k@first")
        self.save_for("fake_connector", b"k@second")
        self.read_all_then_update_reader({b"k@second"})
        self.assertEqual(self.labels(), [("fake_connector", "second")])

        process_pending(self.conn, self.data_dir, module=THIS)

        self.assertEqual(self.labels(), [("fake_connector", "first")])

    def test_a_group_left_with_no_members_is_removed(self) -> None:
        self.save_for("fake_connector", b"run1@morning")
        self.read_all_then_update_reader({b"run1@morning"})
        self.assertEqual(self.conn.execute("SELECT count(*) FROM session_groups").fetchone()[0], 1)

        process_pending(self.conn, self.data_dir, module=THIS)

        self.assertEqual(self.conn.execute("SELECT count(*) FROM session_groups").fetchone()[0], 0)
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM session_group_members").fetchone()[0], 0
        )

    def test_files_from_other_connectors_are_not_reread(self) -> None:
        self.save_for("fake_connector", b"k@first")
        other = self.save_for("other_app", b"k@theirs")
        self.read_all_then_update_reader({b"k@first", b"k@theirs"})
        self.assertEqual(
            reader_version(folder=Path(__file__).parent), self.row(other)["reader_version"]
        )

        process_pending(self.conn, self.data_dir, module=THIS)

        self.assertEqual(self.labels(), [("other_app", "theirs")])
        self.assertEqual(self.row(other)["status"], "ok")
