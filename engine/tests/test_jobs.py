"""Background live preview / push jobs (jobs.py + server.py routes). Run from the engine folder:
    python3 -m unittest tests.test_jobs

Offline: the Google and Meta engines are replaced with fakes; nothing is sent anywhere.
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("MSTUDIO_HOME", _tf.mkdtemp(prefix="mstudio-test-"))
_os.environ["MSTUDIO_KEYCHAIN"] = "0"

import http.client
import json
import sys
import threading
import time
import types
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

import jobs  # noqa: E402
import progress  # noqa: E402
import server  # noqa: E402


class _NoThread:
    def __init__(self, *a, **k):
        pass

    def start(self):
        raise RuntimeError("can't start new thread")


NO_THREADS = types.SimpleNamespace(Thread=_NoThread)  # jobs.py's threading, as when the process is out of threads


def wait(pred, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(0.01)
    raise AssertionError("timed out")


def done(jid):
    return wait(lambda: (lambda j: j if j and j["state"] != "running" else None)(jobs.get(jid)))


class JobsUnit(unittest.TestCase):
    def setUp(self):
        jobs._reset()

    def test_lifecycle_and_progress(self):
        go = threading.Event()

        def fn(report):
            report(0, 2, "Checking", "i1", "checking")
            report(1, 2, "Sending", "i1", "done")
            go.wait(5)
            return {"ok": True, "message": "Live", "items": [{"key": "i1", "label": "One", "status": "done", "ids": ["9"]},
                                                              {"key": "i2", "label": "Two", "status": "failed", "error": "no"}]}
        jid = jobs.start("push", "google", "123", fn)
        j = wait(lambda: (lambda j: j if j["progress"]["done"] == 1 else None)(jobs.get(jid)))
        self.assertEqual(j["state"], "running")
        self.assertEqual(j["progress"], {"done": 1, "total": 2, "label": "Sending"})
        self.assertEqual(j["items"][0]["key"], "i1")
        self.assertEqual(j["items"][0]["status"], "done")
        go.set()
        j = done(jid)
        self.assertEqual(j["state"], "done")
        self.assertIsNotNone(j["finished"])
        self.assertEqual(j["result"]["message"], "Live")
        self.assertEqual([(i["key"], i["status"], i["detail"]) for i in j["items"]], [("i1", "done", None), ("i2", "error", "no")])
        self.assertEqual(j["items"][0]["ids"], ["9"])
        self.assertEqual(j["progress"]["done"], j["progress"]["total"])
        for k in ("id", "kind", "source", "acct", "state", "started", "finished", "progress", "items", "result", "error"):
            self.assertIn(k, j)

    def test_error_captured(self):
        def fn(report):
            raise ValueError("Google said no")
        j = done(jobs.start("preview", "google", "123", fn))
        self.assertEqual(j["state"], "error")
        self.assertEqual(j["error"], "Google said no")
        self.assertIsNone(j["result"])

    def test_one_push_per_account(self):
        go = threading.Event()
        a = jobs.start("push", "meta", "meta", lambda r: go.wait(5) and {"ok": True})
        with self.assertRaises(jobs.Busy) as cm:
            jobs.start("push", "meta", "meta", lambda r: {"ok": True})
        self.assertEqual(cm.exception.job_id, a)
        b = jobs.start("push", "meta", "meta_2", lambda r: {"ok": True})  # another account is fine
        p = jobs.start("preview", "meta", "meta", lambda r: {"ok": True})  # and so is a preview
        go.set()
        for i in (a, b, p):
            self.assertEqual(done(i)["state"], "done")
        done(jobs.start("push", "meta", "meta", lambda r: {"ok": True}))  # free again

    def test_keeps_last_50(self):
        ids = [jobs.start("preview", "google", "1", lambda r: {"ok": True}) for _ in range(60)]
        for i in ids:
            wait(lambda i=i: jobs.get(i) is None or jobs.get(i)["state"] != "running")
        jobs.start("preview", "google", "1", lambda r: {"ok": True})
        self.assertLessEqual(len(jobs.latest(limit=500)), jobs.KEEP)
        self.assertIsNone(jobs.get(ids[0]))

    def test_progress_py_fallback(self):
        go = threading.Event()

        def fn(report):
            report.use_progress_py("acc9")
            progress.start("acc9", "push", [("x1", "First", 1), ("x2", "Second", 1)], "sending", "Sending")
            progress.sent("acc9", "x1")
            go.wait(5)
            progress.sent("acc9", "x2")
            progress.finish("acc9", True, "Done")
            return {"ok": True}
        jid = jobs.start("push", "google", "acc9", fn)
        j = wait(lambda: (lambda j: j if j["items"] else None)(jobs.get(jid)))
        self.assertEqual(j["progress"]["total"], 2)
        self.assertEqual(j["progress"]["done"], 1)
        self.assertEqual([i["status"] for i in j["items"]], ["done", "waiting"])
        go.set()
        j = done(jid)
        self.assertEqual([i["status"] for i in j["items"]], ["done", "done"])

    def test_refused_push_is_failed_not_done(self):
        j = done(jobs.start("push", "google", "1", lambda r: {"ok": False, "message": "Not pushed - tick the box"}))
        self.assertEqual((j["state"], j["ok"], j["error"]), ("failed", False, None))
        self.assertEqual(j["progress"]["label"], "Not pushed - tick the box")
        j = done(jobs.start("push", "google", "1", lambda r: {"ok": False, "needs_approval": True}))
        self.assertEqual((j["state"], j["ok"], j["progress"]["label"]), ("failed", False, "Not pushed"))
        j = done(jobs.start("push", "google", "1", lambda r: {"ok": True}))
        self.assertEqual((j["state"], j["ok"]), ("done", True))
        j = done(jobs.start("preview", "google", "1", lambda r: {"ok": False, "items": []}))  # a preview with problems ran fine
        self.assertEqual((j["state"], j["ok"]), ("done", False))
        j = done(jobs.start("push", "google", "1", lambda r: 1 / 0))
        self.assertEqual((j["state"], j["ok"]), ("error", False))
        go = threading.Event()
        jid = jobs.start("push", "google", "2", lambda r: go.wait(5) and {"ok": True})
        self.assertIsNone(jobs.get(jid)["ok"])
        go.set()
        done(jid)

    def test_thread_start_failure_leaves_no_job(self):
        with mock.patch.object(jobs, "threading", NO_THREADS):
            with self.assertRaises(RuntimeError):
                jobs.start("push", "google", "1", lambda r: {"ok": True})
        self.assertEqual(jobs.latest(), [])
        self.assertIsNone(jobs.running_push("1"))

    def test_accepts(self):
        self.assertTrue(jobs.accepts(lambda a, progress=None: 0, "progress"))
        self.assertTrue(jobs.accepts(lambda a, **kw: 0, "progress"))
        self.assertFalse(jobs.accepts(lambda a, only=None: 0, "progress"))


class JobsHTTP(unittest.TestCase):
    def setUp(self):
        jobs._reset()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), server.H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_address[1]
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.posted = []
        for p in (mock.patch.object(server.team_reviews, "joined", lambda: False),
                  mock.patch.object(server.team_live, "after_post", lambda path, body, res: self.posted.append((path, res))),
                  mock.patch.object(server.gapi, "refresh", lambda *a, **k: {"ok": True}),
                  mock.patch.object(server.meta, "use", lambda key: key),
                  mock.patch.object(server.meta, "acct", lambda: "meta")):
            p.start()
            self.addCleanup(p.stop)

    def call(self, method, path, body=None, headers=None, host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": host or f"127.0.0.1:{self.port}"}
        data = None
        if body is not None:
            data = json.dumps(body)
            h.update({"Content-Type": "application/json", "X-Studio-Press": server.PRESS})
        h.update(headers or {})
        c.request(method, path, data, h)
        r = c.getresponse()
        out = (r.status, json.loads(r.read() or b"null"))
        c.close()
        return out

    def test_sync_route_unchanged(self):
        with mock.patch.object(server.live, "push", lambda acct, en, sp, perm, only: {"ok": True, "push_id": "p1", "acct": acct,
                                                                                       "spend": sp, "permanent": perm}):
            st, res = self.call("POST", "/api/live/push", {"acct": "1234567890", "accept_spend": True})
        self.assertEqual(st, 200)
        self.assertEqual(res, {"ok": True, "push_id": "p1", "acct": "1234567890", "spend": True, "permanent": False})
        self.assertEqual(self.posted, [("/api/live/push", res)])
        self.assertFalse(server.PUSH_LOCK.locked())
        with mock.patch.object(server.live, "preview", lambda acct, en=False, only=None: {"ok": True, "items": []}):
            st, res = self.call("POST", "/api/live/preview", {"acct": "1234567890"})
        self.assertEqual((st, res), (200, {"ok": True, "items": []}))

    def test_background_push_google(self):
        go = threading.Event()
        seen = {}

        def push(acct, en, sp, perm, only, progress=None):
            seen.update(acct=acct, sp=sp, perm=perm)
            progress(0, 1, "Sending", "d1", "sending")
            go.wait(5)
            progress(1, 1, "Sent", "d1", "done")
            return {"ok": True, "push_id": "p2", "message": "Live in Google Ads: 1 operation(s)."}
        with mock.patch.object(server.live, "push", push):
            st, res = self.call("POST", "/api/live/push", {"acct": "1234567890", "background": True, "accept_permanent": True})
            self.assertEqual(st, 202)
            jid = res["job"]
            st, j = self.call("GET", f"/api/jobs/{jid}")
            self.assertEqual(st, 200)
            self.assertEqual((j["kind"], j["source"], j["acct"]), ("push", "google", "1234567890"))
            wait(lambda: self.call("GET", f"/api/jobs/{jid}")[1]["items"])
            st, j = self.call("GET", f"/api/jobs/{jid}")
            self.assertEqual(j["state"], "running")
            self.assertEqual(j["items"][0]["status"], "running")
            self.assertTrue(server.PUSH_LOCK.locked())
            st, again = self.call("POST", "/api/live/push", {"acct": "1234567890", "background": True})
            self.assertEqual((st, again["job"]), (409, jid))
            st, sync = self.call("POST", "/api/live/push", {"acct": "1234567890"})  # the old route still refuses
            self.assertEqual(st, 400)
            self.assertEqual(self.posted, [])  # nothing reported to the team while it runs
            go.set()
            j = done(jid)
        self.assertEqual(j["state"], "done")
        self.assertEqual(j["result"]["push_id"], "p2")
        self.assertEqual(seen, {"acct": "1234567890", "sp": False, "perm": True})
        wait(lambda: self.posted)
        self.assertEqual(self.posted, [("/api/live/push", j["result"])])
        self.assertFalse(server.PUSH_LOCK.locked())
        st, lst = self.call("GET", "/api/jobs?acct=1234567890")
        self.assertEqual([x["id"] for x in lst["jobs"]], [jid])
        self.assertEqual(self.call("GET", "/api/jobs?acct=999")[1]["jobs"], [])

    def test_background_preview_and_error(self):
        with mock.patch.object(server.live, "preview", lambda acct, en=False, only=None: {
                "ok": False, "items": [{"id": "d1", "summary": "Pause X", "problems": ["bad"]}]}):
            st, res = self.call("POST", "/api/live/preview", {"acct": "1234567890", "background": True})
            self.assertEqual(st, 202)
            j = done(res["job"])
        self.assertEqual(j["kind"], "preview")
        self.assertEqual(j["items"], [{"key": "d1", "label": "Pause X", "status": "error", "detail": "bad", "link": None}])

        def boom(*a, **k):
            raise ValueError("Google refused")
        with mock.patch.object(server.live, "push", boom):
            st, res = self.call("POST", "/api/live/push", {"acct": "1234567890", "background": True})
            j = done(res["job"])
        self.assertEqual((j["state"], j["error"]), ("error", "Google refused"))
        wait(lambda: not server.PUSH_LOCK.locked())
        self.assertEqual(self.posted, [])

    def test_background_meta_push_refresh(self):
        refreshed = threading.Event()
        calls = {}

        def push(enable_new=False, accept_spend=False, accept_permanent=False, only=None, progress=None, refresh=True):
            calls.update(refresh=refresh, spend=accept_spend)
            progress(1, 1, "Sent", "m1", "done")
            return {"ok": True, "push_id": "mp"}

        def refresh_account():
            refreshed.wait(5)
            return {"fetched_at": "now"}
        with mock.patch.object(server.meta, "push", push), mock.patch.object(server.meta, "refresh_account", refresh_account):
            st, res = self.call("POST", "/api/live/push", {"acct": "meta", "background": True, "accept_spend": True})
            self.assertEqual(st, 202)
            j = done(res["job"])
            self.assertEqual(calls, {"refresh": False, "spend": True})
            self.assertEqual(j["source"], "meta")
            self.assertTrue(j["result"]["refreshing"])
            self.assertEqual(j["items"][0]["key"], "m1")
            refreshed.set()
            wait(lambda: jobs.get(res["job"])["result"]["refreshing"] is False)

    def test_meta_push_without_refresh_keyword(self):
        with mock.patch.object(server.meta, "push", lambda en=False, sp=False, perm=False, only=None: {"ok": True}):
            st, res = self.call("POST", "/api/live/push", {"acct": "meta", "background": True})
            j = done(res["job"])
        self.assertEqual(j["result"], {"ok": True, "refreshing": False})

    def test_background_push_refused_is_failed(self):
        with mock.patch.object(server.live, "push", lambda acct, en, sp, perm, only: {
                "ok": False, "message": "Not pushed - this push can raise spend; tick the box to accept."}):
            st, res = self.call("POST", "/api/live/push", {"acct": "1234567890", "background": True})
            self.assertEqual(st, 202)
            j = done(res["job"])
        self.assertEqual((j["state"], j["ok"]), ("failed", False))
        self.assertIn("tick the box", j["progress"]["label"])
        wait(lambda: not server.PUSH_LOCK.locked())
        time.sleep(0.05)
        self.assertEqual(self.posted, [])  # never reported to the team as a push

    def test_job_start_failure_releases_lock(self):
        with mock.patch.object(server.jobs, "threading", NO_THREADS):
            st, res = self.call("POST", "/api/live/push", {"acct": "1234567890", "background": True})
        self.assertEqual(st, 500)
        self.assertIn("can't start new thread", res["error"])
        self.assertFalse(server.PUSH_LOCK.locked())
        self.assertEqual(jobs.latest(), [])
        with mock.patch.object(server.live, "push", lambda acct, en, sp, perm, only: {"ok": True}):
            st, res = self.call("POST", "/api/live/push", {"acct": "1234567890", "background": True})
            self.assertEqual(st, 202)
            self.assertEqual(done(res["job"])["state"], "done")

    def test_guards(self):
        st, _ = self.call("POST", "/api/live/push", {"acct": "1234567890", "background": True}, {"X-Studio-Press": "wrong"})
        self.assertEqual(st, 403)
        st, _ = self.call("POST", "/api/live/push", {"acct": "1234567890", "background": True}, {"Origin": "http://evil.example"})
        self.assertEqual(st, 403)
        st, _ = self.call("GET", "/api/jobs", host="evil.example")
        self.assertEqual(st, 403)
        st, _ = self.call("GET", "/api/jobs/nope")
        self.assertEqual(st, 404)
        self.assertEqual(jobs.latest(), [])


if __name__ == "__main__":
    unittest.main()
