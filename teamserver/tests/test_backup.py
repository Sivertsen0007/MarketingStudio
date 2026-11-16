"""Backups (teamserver/backup.py, `backup` / `restore` commands): an online copy while the server writes, and a restore.

Run:  python3 teamserver/tests/test_backup.py
"""
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

import test_server as base  # noqa: E402  (the shared server fixture; its tests are not re-run here)
from teamserver import backup  # noqa: E402
from teamserver.app import TeamServer  # noqa: E402


def cli(*args):
    return subprocess.run([sys.executable, "-m", "teamserver", *args], cwd=REPO, capture_output=True, text=True,
                          timeout=60)


class Backup(base.Base):
    def setUp(self):
        super().setUp()
        self.out_dir = tempfile.mkdtemp(prefix="teamserver-backup-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.out_dir, True))

    def item_ids(self, path):
        con = sqlite3.connect(path)
        try:
            return {r[0] for r in con.execute("SELECT id FROM items")}
        finally:
            con.close()

    def test_backup_while_the_server_writes(self):
        self.put(self.owner, "seo:changes", "before", 0, {"title": "before"})
        stop = threading.Event()
        written = []

        def writer():
            n = 0
            while not stop.is_set() and n < 400:
                self.put(self.owner, "seo:changes", "w%d" % n, 0, {"title": "x" * 200})
                written.append(n)
                n += 1

        t = threading.Thread(target=writer)
        t.start()
        try:
            while len(written) < 5:
                stop.wait(0.01)
            outs = []
            for i in range(3):  # several copies while the writes go on
                out = os.path.join(self.out_dir, "b%d.db" % i)
                info = backup.backup(self.dir, out)
                outs.append((out, info))
        finally:
            stop.set()
            t.join(30)
        for out, info in outs:
            self.assertEqual(backup.check(out), info)
            ids = self.item_ids(out)
            self.assertIn("before", ids)
            self.assertEqual(info["items"], len(ids))
            # consistent: the writes it holds are a prefix of the writes made (no gaps, nothing half written)
            nums = sorted(int(i[1:]) for i in ids if i.startswith("w"))
            self.assertEqual(nums, list(range(len(nums))))
            self.assertEqual(stat.S_IMODE(os.stat(out).st_mode), 0o600)
            self.assertFalse(os.path.exists(out + "-wal"))
            with open(out, "rb") as f:
                head = f.read(20)
            self.assertEqual(head[18:20], b"\x01\x01")  # rollback journal mode: one self-contained file
        self.assertEqual([f for f in os.listdir(self.out_dir) if ".part-" in f], [])
        self.assertEqual(self.ok("GET", "/api/v1/health")["ok"], True)  # the server kept serving

    def test_backup_refuses_to_overwrite_without_force(self):
        out = os.path.join(self.out_dir, "b.db")
        backup.backup(self.dir, out)
        with self.assertRaises(backup.BackupError) as e:
            backup.backup(self.dir, out)
        self.assertIn("--force", str(e.exception))
        self.put(self.owner, "seo:changes", "later", 0, {"title": "later"})
        backup.backup(self.dir, out, force=True)
        self.assertIn("later", self.item_ids(out))
        with self.assertRaises(backup.BackupError):
            backup.backup(self.dir, self.out_dir)  # a folder, not a file
        with self.assertRaises(backup.BackupError):
            backup.backup(os.path.join(self.out_dir, "nothing-here"), os.path.join(self.out_dir, "x.db"))

    def test_restore_refuses_while_the_server_runs(self):
        out = os.path.join(self.out_dir, "b.db")
        backup.backup(self.dir, out)
        with self.assertRaises(backup.BackupError) as e:
            backup.restore(self.dir, out)
        self.assertIn("--yes", str(e.exception))
        with self.assertRaises(backup.BackupError) as e:
            backup.restore(self.dir, out, yes=True)
        self.assertIn("stop the server", str(e.exception))
        self.assertEqual(self.ok("GET", "/api/v1/health")["ok"], True)
        self.assertEqual([f for f in os.listdir(self.dir) if "before-restore" in f], [])

    def test_restore_brings_the_backup_back(self):
        self.put(self.owner, "seo:changes", "kept", 0, {"title": "kept"})
        out = os.path.join(self.out_dir, "b.db")
        backup.backup(self.dir, out)
        self.put(self.owner, "seo:changes", "after", 0, {"title": "after the backup"})
        self.srv.stop()
        info = backup.restore(self.dir, out, yes=True, now=1_800_000_000)
        self.assertTrue(info["kept"].endswith(".before-restore-" + __import__("time").strftime(
            "%Y%m%d-%H%M%S", __import__("time").localtime(1_800_000_000))))
        self.assertIn("after", self.item_ids(info["kept"]))  # what it replaced is kept
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.dir, "team.db")).st_mode), 0o600)
        # the server starts on the restored data, and the owner's device still works
        self.srv = TeamServer(self.dir, host="127.0.0.1", port=0, clock=self.clock, ping_interval=0.3).start()
        self.port = self.srv.port
        items = self.ok("GET", "/api/v1/items?since=0", token=self.owner["token"])["items"]
        ids = {i["id"] for i in items}
        self.assertIn("kept", ids)
        self.assertNotIn("after", ids)
        self.put(self.owner, "seo:changes", "new", 0, {"title": "new"})

    def test_restore_rejects_files_that_are_not_team_backups(self):
        self.srv.stop()
        junk = os.path.join(self.out_dir, "junk.db")
        with open(junk, "wb") as f:
            f.write(b"not a database at all" * 10)
        other = os.path.join(self.out_dir, "other.db")
        con = sqlite3.connect(other)
        con.execute("CREATE TABLE notes (x)")
        con.commit()
        con.close()
        for bad in (junk, other, os.path.join(self.out_dir, "missing.db")):
            with self.assertRaises(backup.BackupError):
                backup.restore(self.dir, bad, yes=True)
        with self.assertRaises(backup.BackupError):
            backup.restore(self.dir, os.path.join(self.dir, "team.db"), yes=True)
        self.assertEqual([f for f in os.listdir(self.dir) if "before-restore" in f], [])

    def test_restore_into_an_empty_data_folder(self):
        out = os.path.join(self.out_dir, "b.db")
        backup.backup(self.dir, out)
        fresh = os.path.join(self.out_dir, "fresh")
        info = backup.restore(fresh, out)
        self.assertIsNone(info["kept"])
        self.assertEqual(info["members"], 1)
        self.assertEqual(info["team"], "Team")  # no name set: the same default the server shows

    def test_cli(self):
        self.put(self.owner, "seo:changes", "c1", 0, {"title": "c1"})
        out = os.path.join(self.out_dir, "cli.db")
        r = cli("backup", "--data", self.dir, "--out", out)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Backed up", r.stdout)
        r = cli("backup", "--data", self.dir, "--out", out)
        self.assertEqual(r.returncode, 1)
        self.assertIn("--force", r.stderr)
        r = cli("restore", "--data", self.dir, out, "--yes")
        self.assertEqual(r.returncode, 1)  # the server still runs
        self.srv.stop()
        r = cli("restore", "--data", self.dir, out, "--yes")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Restored", r.stdout)
        self.assertIn("before-restore", r.stdout)
        self.srv = TeamServer(self.dir, host="127.0.0.1", port=0, clock=self.clock, ping_interval=0.3).start()


if __name__ == "__main__":
    unittest.main()
