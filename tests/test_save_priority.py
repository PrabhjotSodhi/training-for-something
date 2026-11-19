import multiprocessing
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from tests.helpers import store_file
from tests.test_proposals import ADD, NOW, proposal
from tests.test_records import STEPS
from tfs.importer.session import rebuild_pending
from tfs.persistence.migrations import migrate
from tfs.persistence.sqlite import connect, save_intent, wait_for_saves
from tfs.proposals.apply import save


def hold_save(path, ready, release):
    with save_intent(path):
        ready.set()
        if not release.wait(15):
            raise TimeoutError("Save was not released")


def import_after_saves(path, ready, done):
    with closing(connect(path, "import")) as conn:
        ready.set()
        wait_for_saves(conn)
        conn.execute("INSERT INTO rebuild_dates VALUES ('2026-10-01')")
        done.set()


def import_batches(path, boundary, proceed, done, rebuild):
    with closing(connect(path, "import")) as conn:
        if rebuild:
            paused = False

            def trace(sql):
                nonlocal paused
                if not paused and sql.startswith("DELETE FROM rebuild_dates"):
                    paused = True
                    boundary.set()
                    proceed.wait(15)

            conn.set_trace_callback(trace)
            rebuild_pending(conn)
        else:
            batches = 0

            def before_batch():
                nonlocal batches
                batches += 1
                if batches == 2:
                    boundary.set()
                    if not proceed.wait(15):
                        raise TimeoutError("Import was not released")

            store_file(
                conn,
                [{**STEPS, "key": str(i)} for i in range(4)],
                1,
                "2026-10-01T08:00:00Z",
                batch_size=2,
                before_batch=before_batch,
            )
        done.set()


class SavePriorityTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "tfs.db"
        self.conn = connect(self.path)
        self.addCleanup(self.conn.close)
        migrate(self.conn)
        self.conn.execute(
            "INSERT INTO sources (connector, last_received_at)"
            " VALUES ('testapp', '2026-12-01T00:00:00Z')"
        )
        self.ctx = multiprocessing.get_context("spawn")

    def start(self, target, *args):
        process = self.ctx.Process(target=target, args=(self.path, *args))
        process.start()

        def cleanup():
            if process.is_alive():
                process.terminate()
            process.join(10)
            process.close()

        self.addCleanup(cleanup)
        return process

    def finished(self, process):
        process.join(10)
        self.assertEqual(process.exitcode, 0)

    def test_import_waits_for_both_saves_and_process_death_releases_intent(self):
        holders = []
        for _ in range(2):
            ready, release = self.ctx.Event(), self.ctx.Event()
            holders.append(self.start(hold_save, ready, release))
            self.assertTrue(ready.wait(10))
        ready, done = self.ctx.Event(), self.ctx.Event()
        importer = self.start(import_after_saves, ready, done)
        self.assertTrue(ready.wait(10))
        self.assertFalse(done.wait(0.1))
        holders[0].terminate()
        holders[0].join(10)
        self.assertFalse(done.wait(0.1))
        holders[1].terminate()
        holders[1].join(10)
        self.assertTrue(done.wait(10))
        self.finished(importer)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM rebuild_dates").fetchone()[0], 1)

    def test_record_batches_and_monthly_rebuilds_yield_then_finish(self):
        for rebuild in (False, True):
            with self.subTest(rebuild=rebuild):
                self.conn.execute("DELETE FROM rebuild_dates")
                if rebuild:
                    self.conn.executemany(
                        "INSERT INTO rebuild_dates VALUES (?)", [("2026-10-01",), ("2026-11-01",)]
                    )
                boundary, proceed, done = (self.ctx.Event() for _ in range(3))
                worker = self.start(import_batches, boundary, proceed, done, rebuild)
                self.assertTrue(boundary.wait(10))
                with save_intent(self.path):
                    proceed.set()
                    self.assertFalse(done.wait(0.1))
                    if not rebuild:
                        self.assertEqual(
                            self.conn.execute("SELECT count(*) FROM source_records").fetchone()[0],
                            2,
                        )
                    saved = save(
                        self.path,
                        proposal({**ADD, "item": str(rebuild)}, version=int(rebuild)),
                        request_id=str(rebuild),
                        approved_by="chat",
                        now=NOW,
                    )
                    self.assertEqual(saved["plan_version"], int(rebuild) + 1)
                self.assertTrue(done.wait(10))
                self.finished(worker)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM source_records").fetchone()[0], 4)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM rebuild_dates").fetchone()[0], 0)

    def test_save_and_failed_save_leave_imports_unblocked(self):
        save(self.path, proposal(ADD), request_id="first", approved_by="chat", now=NOW)
        with self.assertRaises(ValueError):
            save(self.path, proposal(ADD), request_id="stale", approved_by="chat", now=NOW)
        ready, done = self.ctx.Event(), self.ctx.Event()
        importer = self.start(import_after_saves, ready, done)
        self.assertTrue(done.wait(10))
        self.finished(importer)
        self.assertEqual(self.conn.execute("SELECT status FROM plan_items").fetchone()[0], "saved")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM journal").fetchone()[0], 1)
