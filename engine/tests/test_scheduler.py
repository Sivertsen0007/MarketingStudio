"""Studio's background scheduler (scheduler.py). Run from the engine folder:
    python3 tests/test_scheduler.py

Offline and quick: jobs are small local functions, the clock is passed in (run_due(now=...)) where time matters, and
Studio's data folder is a temp folder.
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("MSTUDIO_HOME", _tf.mkdtemp(prefix="mstudio-test-"))
_os.environ["MSTUDIO_KEYCHAIN"] = "0"

import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
TMP = Path(tempfile.mkdtemp(prefix="scheduler-test-"))
os.environ["STUDIO_DATA_DIR"] = str(TMP)

import datapolicy  # noqa: E402
import scheduler as S  # noqa: E402

LONDON = ZoneInfo("Europe/London")


def at(y, mo, d, h, mi, tz=LONDON):
    return dt.datetime(y, mo, d, h, mi, tzinfo=tz).timestamp()


def wait_idle(name, limit=5.0):
    end = time.time() + limit
    while time.time() < end:
        j = S._jobs.get(name)
        if j and not (j.get("worker") and j["worker"].is_alive()):
            return
        time.sleep(0.01)
    raise AssertionError(f"{name} still running")


def set_policy(m):
    f = TMP / "settings.json"
    s = json.loads(f.read_text()) if f.exists() else {}
    s["data_policy"] = m
    f.write_text(json.dumps(s))


class Base(unittest.TestCase):
    def setUp(self):
        S._reset_for_tests()
        for f in TMP.glob("*.json"):
            f.unlink()
        set_policy("manual")
        self.real_now = S._now
        self.clock = [at(2026, 11, 17, 10, 0)]
        S._now = lambda: self.clock[0]

    def tearDown(self):
        S._now = self.real_now
        S._reset_for_tests()


class NotOnImport(unittest.TestCase):
    def test_no_thread_on_import(self):
        """Importing scheduler, server and the MCP server starts nothing (a fresh interpreter, so nothing else ran)."""
        code = ("import os,sys,threading; sys.path.insert(0, '.');"
                "import scheduler, server, mcp_server;"
                "names=[t.name for t in threading.enumerate()];"
                "assert 'studio-scheduler' not in names, names;"
                "assert not scheduler.running() and scheduler._loop['thread'] is None;"
                "print('ok')")
        env = {**os.environ, "STUDIO_DATA_DIR": tempfile.mkdtemp(prefix="sched-imp-"), "MSTUDIO_KEYCHAIN": "0",
               "MSTUDIO_HOME": tempfile.mkdtemp(prefix="sched-home-")}
        r = subprocess.run([sys.executable, "-c", code], cwd=str(HERE), env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        self.assertIn("ok", r.stdout)


class Interval(Base):
    def test_first_run_after_one_interval_then_every(self):
        runs = []
        S.register("tick", lambda: runs.append(1), every=60, enabled_default=True)
        self.assertEqual(S.run_due(), [])  # not at start
        self.clock[0] += 59
        self.assertEqual(S.run_due(), [])
        self.clock[0] += 1
        self.assertEqual(S.run_due(), ["tick"])
        wait_idle("tick")
        self.assertEqual(runs, [1])
        self.clock[0] += 30
        self.assertEqual(S.run_due(), [])
        self.clock[0] += 30
        self.assertEqual(S.run_due(), ["tick"])
        wait_idle("tick")
        row = S.status()["jobs"][0]
        self.assertEqual((row["last_status"], row["runs"], row["schedule"]), ("ok", 2, "every 1 minute"))

    def test_missed_runs_run_once(self):
        runs = []
        S.register("tick", lambda: runs.append(1), every=60, enabled_default=True)
        self.clock[0] += 3600  # asleep for an hour
        self.assertEqual(S.run_due(), ["tick"])
        wait_idle("tick")
        self.assertEqual(S.run_due(), [])
        self.assertEqual(len(runs), 1)
        self.assertEqual(S._jobs["tick"]["next_run"], self.clock[0] + 60)

    def test_bad_registrations(self):
        with self.assertRaises(ValueError):
            S.register("x", lambda: None)
        with self.assertRaises(ValueError):
            S.register("x", lambda: None, every=10, at="07:00")
        with self.assertRaises(ValueError):
            S.register("x", lambda: None, at="25:00")
        with self.assertRaises(ValueError):
            S.register("x", lambda: None, every=0)
        S.register("x", lambda: None, every=dt.timedelta(minutes=5))
        self.assertEqual(S._jobs["x"]["every"], 300)


class Daily(Base):
    def test_daily_at_in_time_zone(self):
        runs = []
        S.register("mail", lambda: runs.append(1), at="07:30", tz="Europe/London", enabled_default=True)
        self.assertEqual(S._jobs["mail"]["next_run"], at(2026, 11, 18, 7, 30))  # 10:00 now: tomorrow
        self.clock[0] = at(2026, 11, 18, 7, 29)
        self.assertEqual(S.run_due(), [])
        self.clock[0] = at(2026, 11, 18, 7, 30)
        self.assertEqual(S.run_due(), ["mail"])
        wait_idle("mail")
        self.assertEqual(S._jobs["mail"]["next_run"], at(2026, 11, 19, 7, 30))
        row = S.status()["jobs"][0]
        self.assertEqual(row["schedule"], "daily at 07:30 (Europe/London)")
        self.assertTrue(row["next_run"].startswith("2026-11-19T07:30:00"))

    def test_other_zone_and_dst(self):
        S.register("ny", lambda: None, at="09:00", tz=ZoneInfo("America/New_York"), enabled_default=True)
        self.assertEqual(S._jobs["ny"]["next_run"], at(2026, 11, 17, 9, 0, ZoneInfo("America/New_York")))  # 05:00 in NY now
        self.clock[0] = at(2027, 3, 27, 12, 0)  # the night the UK clocks go forward
        S.register("uk", lambda: None, at="09:00", tz="Europe/London", enabled_default=True)
        self.assertEqual(S._jobs["uk"]["next_run"], at(2027, 3, 28, 9, 0))
        self.assertEqual(S._jobs["uk"]["next_run"] - at(2027, 3, 27, 9, 0), 23 * 3600)

    def test_account_time_zone_from_cache(self):
        (TMP / "meta_cache_meta.json").write_text(json.dumps({"account": {"timezone_name": "Asia/Bangkok"}}))
        try:
            S.register("acct", lambda: None, at="08:00", enabled_default=True)  # tz=None: the account's zone
            self.assertEqual(S._jobs["acct"]["next_run"], at(2026, 11, 18, 8, 0, ZoneInfo("Asia/Bangkok")))
            self.assertIn("Asia/Bangkok", S.status()["jobs"][0]["schedule"])
        finally:
            (TMP / "meta_cache_meta.json").unlink()


class Safety(Base):
    def test_no_overlap(self):
        gate, inside, peak = threading.Event(), [0], [0]
        lock = threading.Lock()

        def slow():
            with lock:
                inside[0] += 1
                peak[0] = max(peak[0], inside[0])
            gate.wait(5)
            with lock:
                inside[0] -= 1
        S.register("slow", slow, every=1, enabled_default=True, timeout=60)
        self.clock[0] += 1
        self.assertEqual(S.run_due(), ["slow"])
        for _ in range(5):
            self.clock[0] += 1
            self.assertEqual(S.run_due(), [])  # still running: never a second one
            self.assertFalse(S.run_now("slow"))
        self.assertTrue(S.status()["jobs"][0]["running"])
        gate.set()
        wait_idle("slow")
        self.assertEqual(peak[0], 1)
        self.clock[0] += 1
        self.assertEqual(S.run_due(), ["slow"])
        wait_idle("slow")

    def test_exception_isolated(self):
        ok = []

        def bad():
            raise RuntimeError("vendor said no")
        S.register("bad", bad, every=10, enabled_default=True)
        S.register("good", lambda: ok.append(1), every=10, enabled_default=True)
        self.clock[0] += 10
        self.assertEqual(sorted(S.run_due()), ["bad", "good"])
        wait_idle("bad")
        wait_idle("good")
        rows = {r["name"]: r for r in S.status()["jobs"]}
        self.assertEqual(rows["bad"]["last_status"], "error")
        self.assertIn("vendor said no", rows["bad"]["last_error"])
        self.assertEqual(rows["bad"]["failures"], 1)
        self.assertEqual(rows["good"]["last_status"], "ok")
        self.clock[0] += 10
        self.assertEqual(sorted(S.run_due()), ["bad", "good"])  # the failing job runs again at its next time
        wait_idle("bad")
        wait_idle("good")
        self.assertEqual(ok, [1, 1])

    def test_timeout(self):
        gate = threading.Event()
        S.register("stuck", lambda: gate.wait(5), every=1, enabled_default=True, timeout=5)
        self.clock[0] += 1
        self.assertEqual(S.run_due(), ["stuck"])
        self.clock[0] += 6
        self.assertEqual(S.run_due(), [])
        row = S.status()["jobs"][0]
        self.assertEqual(row["last_status"], "timeout")
        self.assertTrue(row["running"])
        self.assertIn("still running after 5s", row["last_error"])
        self.clock[0] += 5
        self.assertEqual(S.run_due(), [])  # waits for the stuck run, no second copy
        gate.set()
        wait_idle("stuck")
        row = S.status()["jobs"][0]
        self.assertEqual(row["last_status"], "timeout")
        self.assertIn("timeout 5s", row["last_error"])
        self.clock[0] += 1
        self.assertEqual(S.run_due(), ["stuck"])
        wait_idle("stuck")
        self.assertEqual(S.status()["jobs"][0]["last_status"], "ok")

    def test_disabled_jobs(self):
        runs = []
        S.register("off", lambda: runs.append(1), every=10)  # enabled_default False
        self.clock[0] += 100
        self.assertEqual(S.run_due(), [])
        row = S.status()["jobs"][0]
        self.assertFalse(row["enabled"])
        self.assertIsNone(row["next_run"])
        S.set_enabled("off", True)
        self.assertEqual(S.run_due(), [])  # switched on: no instant catch-up
        self.clock[0] += 10
        self.assertEqual(S.run_due(), ["off"])
        wait_idle("off")
        S.set_enabled("off", False)
        self.clock[0] += 100
        self.assertEqual(S.run_due(), [])
        self.assertEqual(runs, [1])
        self.assertEqual(json.loads((TMP / "scheduler.json").read_text())["enabled"], {"off": False})
        S.register("pinned", lambda: None, every=10, enabled_default=True, can_disable=False)
        with self.assertRaises(ValueError):
            S.set_enabled("pinned", False)
        with self.assertRaises(ValueError):
            S.set_enabled("nope", True)

    def test_refresh_jobs_follow_data_policy(self):
        runs = []
        S.register("pull", lambda: runs.append(1), every=10, enabled_default=True, refresh=True)
        self.assertTrue(S.status()["jobs"][0]["held"])
        self.clock[0] += 10
        self.assertEqual(S.run_due(), [])  # manual: nothing is pulled
        self.assertEqual(S.status()["jobs"][0]["last_status"], "held")
        set_policy("auto")
        self.clock[0] += 10
        self.assertEqual(S.run_due(), ["pull"])
        wait_idle("pull")
        self.assertEqual(runs, [1])


class Persistence(Base):
    def test_survives_restart(self):
        S.register("p", lambda: None, every=60, enabled_default=True)
        S.register("d", lambda: None, at="07:00", tz="Europe/London", enabled_default=True)
        self.clock[0] += 60
        self.assertEqual(S.run_due(), ["p"])
        wait_idle("p")
        S.set_enabled("d", False)
        nxt = S._jobs["p"]["next_run"]
        # a new Studio process: memory gone, the file stays
        S._jobs.clear()
        S._disk.update(loaded=False, data=None)
        S.register("p", lambda: None, every=60, enabled_default=True)
        S.register("d", lambda: None, at="07:00", tz="Europe/London", enabled_default=True)
        rows = {r["name"]: r for r in S.status()["jobs"]}
        self.assertEqual(rows["p"]["runs"], 1)
        self.assertEqual(rows["p"]["last_status"], "ok")
        self.assertEqual(S._jobs["p"]["next_run"], nxt)
        self.assertFalse(rows["d"]["enabled"])  # the user's switch is kept
        # Studio closed past the next run: one catch-up run at start
        self.clock[0] = nxt + 3600
        S._jobs.clear()
        S._disk.update(loaded=False, data=None)
        S.register("p", lambda: None, every=60, enabled_default=True)
        self.assertEqual(S.run_due(), ["p"])
        wait_idle("p")
        # a changed schedule ignores the old next run
        S.register("p", lambda: None, every=600, enabled_default=True)
        self.assertEqual(S._jobs["p"]["next_run"], self.clock[0] + 600)

    def test_daily_already_ran_today(self):
        S.register("d", lambda: None, at="07:00", tz="Europe/London", enabled_default=True)
        self.clock[0] = at(2026, 11, 18, 7, 0)
        self.assertEqual(S.run_due(), ["d"])
        wait_idle("d")
        self.clock[0] = at(2026, 11, 18, 9, 0)  # restart later the same day
        S._jobs.clear()
        S._disk.update(loaded=False, data=None)
        S.register("d", lambda: None, at="07:00", tz="Europe/London", enabled_default=True)
        self.assertEqual(S.run_due(), [])
        self.assertEqual(S._jobs["d"]["next_run"], at(2026, 11, 19, 7, 0))


class Thread(unittest.TestCase):
    """The real thread with the real clock: start once, run, stop."""

    def setUp(self):
        S._reset_for_tests()
        for f in TMP.glob("*.json"):
            f.unlink()
        set_policy("manual")

    def tearDown(self):
        S._reset_for_tests()

    def test_thread_runs_jobs(self):
        runs = []
        S.register("fast", lambda: runs.append(1), every=0.2, enabled_default=True)
        self.assertTrue(S.start(tick=0.05))
        self.assertFalse(S.start(tick=0.05))
        self.assertTrue(S.status()["running"])
        end = time.time() + 5
        while len(runs) < 3 and time.time() < end:
            time.sleep(0.05)
        S.stop()
        self.assertGreaterEqual(len(runs), 3)
        self.assertFalse(S.running())

    def test_loop_survives_a_broken_job_list(self):
        S.register("ok", lambda: None, every=0.1, enabled_default=True)
        real = S.run_due
        n = [0]

        def flaky(now=None):
            n[0] += 1
            if n[0] == 1:
                raise RuntimeError("bad look")
            return real(now)
        S.run_due = flaky
        try:
            S.start(tick=0.05)
            end = time.time() + 5
            while (S._jobs["ok"].get("runs") or 0) < 1 and time.time() < end:
                time.sleep(0.05)
            self.assertTrue(S.running())
            self.assertGreaterEqual(S._jobs["ok"]["runs"], 1)
        finally:
            S.run_due = real
            S.stop()


if __name__ == "__main__":
    unittest.main(verbosity=1)
