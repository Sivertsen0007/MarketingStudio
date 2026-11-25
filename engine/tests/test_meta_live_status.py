"""Honest Meta pushes and robust video upload (v0.3 step D). Run from the engine folder:
    python3 -m unittest tests.test_meta_live_status

- read-back after a push: live_status / review / link per item, one batched GET per level, a failed read -> "unknown"
- the push summary never says "Everything is live" unless every ad is ACTIVE and approved
- resumable chunked video upload: offsets, retry and resume of a failed chunk, chunked reads from disk, processing poll
- Prepare for Meta (POST /api/meta/media/prepare) runs as a background job, stores the video id, push reuses it
- GET /api/meta/status is read-only and behind the Host guard

Offline: a fake Graph API replaces meta_api._once and meta_ads._post_multipart; nothing is sent anywhere.
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("MSTUDIO_HOME", _tf.mkdtemp(prefix="mstudio-test-"))
_os.environ["MSTUDIO_KEYCHAIN"] = "0"

import builtins
import http.client
import json
import sys
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

from tests import test_meta_fast_push as fp  # noqa: E402  (sets core.STUDIO_DIR to a temp folder)
import core  # noqa: E402
import jobs  # noqa: E402
import meta_ads as meta  # noqa: E402
import meta_api  # noqa: E402
import server  # noqa: E402

ACT = fp.ACT
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 8


class Status:
    """Fake Graph for the read-back: GET /?ids=..&fields=effective_status,... answers from `rows`."""

    def __init__(self, rows, fail=False):
        self.rows, self.fail, self.calls = rows, fail, []

    def __call__(self, method, path, params=None, validate=False, token=None):
        params = dict(params or {})
        self.calls.append((method, path, params))
        if method != "GET":
            raise AssertionError("the read-back must never write")
        if self.fail:
            raise meta_api.MetaError({"message": "Some of the aliases you requested do not exist", "code": 100}, 400)
        ids = params["ids"].split(",")
        return {i: dict(self.rows[i], id=i) for i in ids if i in self.rows}


class Paced(unittest.TestCase):
    def setUp(self):
        meta_api._limiters.clear()
        for p in (mock.patch.object(meta, "act", lambda: ACT), mock.patch.object(meta_api, "PACE_RATE", 10000.0),
                  mock.patch.object(meta_api, "PACE_BURST", 10000)):
            p.start()
            self.addCleanup(p.stop)

    def fake(self, g):
        p = mock.patch.object(meta_api, "_once", g)
        p.start()
        self.addCleanup(p.stop)
        return g


class ReadBack(Paced):
    def test_mapping_review_and_links(self):
        g = self.fake(Status({
            "1": {"name": "Ad one", "effective_status": "DISAPPROVED", "configured_status": "ACTIVE",
                  "ad_review_feedback": {"global": {"Landing page": "The landing page does not work"}}},
            "2": {"name": "Ad two", "effective_status": "WITH_ISSUES", "configured_status": "ACTIVE",
                  "issues_info": [{"error_summary": "Payment method failed", "level": "AD"}]},
            "3": {"name": "Ad three", "effective_status": "PENDING_REVIEW", "configured_status": "PAUSED"},
            "9": {"name": "Camp", "effective_status": "PAUSED", "configured_status": "PAUSED"}}))
        got = meta.read_status([("ad", "1"), ("ad", "2"), ("ad", "3"), ("campaign", "9"), ("ad", "1")])
        self.assertEqual(got["1"]["live_status"], "DISAPPROVED")
        self.assertEqual(got["1"]["review"], "The landing page does not work")
        self.assertEqual(got["2"]["review"], "Payment method failed")
        self.assertEqual((got["3"]["live_status"], got["3"]["configured_status"], got["3"]["review"]), ("PENDING_REVIEW", "PAUSED", None))
        self.assertEqual(got["1"]["link"], "https://adsmanager.facebook.com/adsmanager/manage/ads?act=1&selected_ad_ids=1")
        self.assertEqual(got["9"]["link"], "https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=1&selected_campaign_ids=9")
        self.assertEqual(meta.ads_manager_link("adset", "5", "act_42"),
                         "https://adsmanager.facebook.com/adsmanager/manage/adsets?act=42&selected_adset_ids=5")
        # one GET per level; ad fields ask for the review, campaign fields do not (Meta refuses unknown fields)
        self.assertEqual(len(g.calls), 2)
        self.assertEqual(g.calls[0][2]["ids"], "1,2,3")
        self.assertIn("ad_review_feedback", g.calls[0][2]["fields"])
        self.assertNotIn("ad_review_feedback", g.calls[1][2]["fields"])

    def test_chunks_of_fifty(self):
        g = self.fake(Status({str(i): {"effective_status": "ACTIVE"} for i in range(120)}))
        got = meta.read_status([("ad", str(i)) for i in range(120)])
        self.assertEqual(len(g.calls), 3)
        self.assertEqual([len(c[2]["ids"].split(",")) for c in g.calls], [50, 50, 20])
        self.assertEqual({r["live_status"] for r in got.values()}, {"ACTIVE"})

    def test_failed_read_is_unknown(self):
        self.fake(Status({}, fail=True))
        got = meta.read_status([("ad", "1"), ("adset", "2")])
        self.assertEqual({k: v["live_status"] for k, v in got.items()}, {"1": "unknown", "2": "unknown"})
        self.assertIn("selected_adset_ids=2", got["2"]["link"])
        with mock.patch.object(meta, "get", mock.Mock(side_effect=RuntimeError("socket closed"))):
            self.assertEqual(meta.read_status([("ad", "1")])["1"]["live_status"], "unknown")

    def test_live_check_adds_fields_to_ok_items_only(self):
        self.fake(Status({"7": {"effective_status": "PENDING_REVIEW", "configured_status": "PAUSED"}}))
        entries = [{"id": "i1", "change": {"type": "meta_new_ad"}}, {"id": "i2", "change": {"type": "meta_status", "level": "ad"}},
                   {"id": "i3", "change": {"type": "meta_test_split"}}]
        results = [{"key": "i1", "status": "ok", "ids": {"ad_id": "7", "creative_id": "70"}},
                   {"key": "i2", "status": "failed", "ids": {}}, {"key": "i3", "status": "ok", "ids": {"study_id": "5"}}]
        s = meta._live_check(results, entries)
        self.assertEqual((results[0]["level"], results[0]["live_status"]), ("ad", "PENDING_REVIEW"))
        self.assertIsNone(results[1]["live_status"])
        self.assertIsNone(results[2]["link"])
        self.assertEqual(s, "1 ad created (Paused), 1 in review, 1 other change applied, 1 not pushed")


def res(key, level, live, conf=None, review=None, status="ok"):
    return {"key": key, "status": status, "level": level, "live_status": live, "configured_status": conf, "review": review}


class Summary(unittest.TestCase):
    ENT = [{"id": f"n{i}", "change": {"type": "meta_new_ad"}} for i in range(3)] + [
        {"id": "e1", "change": {"type": "meta_rename", "level": "campaign"}}, {"id": "e2", "change": {"type": "meta_copy", "level": "ad"}}]

    def test_example_wording(self):
        r = [res("n0", "ad", "PENDING_REVIEW", "PAUSED"), res("n1", "ad", "IN_PROCESS", "PAUSED"), res("n2", "ad", "PAUSED", "PAUSED"),
             res("e1", "campaign", "ACTIVE", "ACTIVE")]
        self.assertEqual(meta.summarise_push(r, self.ENT), "3 ads created (Paused), 2 in review, 1 edit applied")

    def test_everything_live_only_when_all_active_and_approved(self):
        r = [res("n0", "ad", "ACTIVE", "ACTIVE"), res("e1", "campaign", "ACTIVE", "ACTIVE")]
        self.assertEqual(meta.summarise_push(r, self.ENT), "Everything is live: 1 ad created (Active), 1 edit applied")
        for bad in (res("n1", "ad", "ACTIVE", "ACTIVE", review="Text in image"), res("n1", "ad", "PAUSED", "PAUSED"),
                    res("n1", "ad", "unknown"), res("n1", "ad", "DISAPPROVED", "ACTIVE", review="Policy"),
                    res("n1", "ad", None, status="failed")):
            with self.subTest(bad=bad):
                self.assertNotIn("Everything is live", meta.summarise_push(r + [bad], self.ENT))
        self.assertNotIn("live", meta.summarise_push([res("e1", "campaign", "ACTIVE", "ACTIVE")], self.ENT).lower())  # no ad

    def test_mixed_and_problem_counts(self):
        r = [res("n0", "ad", "ACTIVE", "ACTIVE"), res("n1", "ad", "PAUSED", "PAUSED"), res("n2", "ad", "unknown"),
             res("e2", "ad", "DISAPPROVED", "ACTIVE", review="Policy"), res("e1", "campaign", "WITH_ISSUES", "ACTIVE", review="Billing")]
        self.assertEqual(meta.summarise_push(r, self.ENT),
                         "3 ads created (1 Active, 1 Paused, 1 Unknown), 1 disapproved, 1 with issues, 2 edits applied, "
                         "1 with status unknown (Meta did not answer the check)")
        self.assertEqual(meta.summarise_push([res("n0", "ad", None, status="failed")], self.ENT), "Nothing was pushed to Meta")
        self.assertEqual(meta.summarise_push([res("n0", "ad", "unknown")], self.ENT),
                         "1 ad created (status unknown), 1 with status unknown (Meta did not answer the check)")


class LiveGraph(fp.FakeGraph):
    """The push fake, plus effective_status / configured_status on every object for the read-back."""
    review = {}
    fail_status = False

    def _handle(self, method, path, params, validate):
        if method == "GET" and path == "" and "effective_status" in params.get("fields", ""):
            self.calls.append(("STATUS", path, params.get("ids")))
            if self.fail_status:
                raise meta_api.MetaError({"message": "Some of the aliases you requested do not exist", "code": 100}, 400)
            out = {}
            for i in params["ids"].split(","):
                o = self.objs[i]
                st = o.get("status") or "ACTIVE"
                eff = "PENDING_REVIEW" if o.get("edge") == "ads" else st
                out[i] = {"id": i, "name": o.get("name"), "effective_status": eff, "configured_status": st,
                          **({"ad_review_feedback": self.review[i]} if i in self.review else {})}
            return out
        return super()._handle(method, path, params, validate)


class PushReadBack(fp.Base):
    def push(self, fail=False):
        g = LiveGraph()
        g.fail_status = fail
        p = mock.patch.object(meta_api, "_once", g)
        p.start()
        self.addCleanup(p.stop)
        cb = []
        with mock.patch.object(meta, "refresh_account", lambda: None):
            r = meta.push(accept_spend=True, progress=lambda *a: cb.append(a))
        return r, g, cb

    def test_push_reads_back_and_summarises(self):
        r, g, cb = self.push()
        self.assertTrue(r["ok"])
        self.assertEqual(r["summary"], "2 campaigns created (Paused), 2 ad sets created (Paused), 1 ad created (Paused), "
                                       "1 in review, 15 edits applied")
        self.assertEqual(r["message"], "Pushed to Meta: " + r["summary"] + ".")
        st = [c for c in g.calls if c[0] == "STATUS"]
        self.assertEqual(len(st), 3)  # one batched read per level (ads, ad sets, campaigns)
        new_ad = next(x for x in r["items"] if x["key"] == "i19")
        self.assertEqual((new_ad["level"], new_ad["live_status"]), ("ad", "PENDING_REVIEW"))
        self.assertIn("selected_ad_ids=" + new_ad["ids"]["ad_id"], new_ad["link"])
        camp = next(x for x in r["items"] if x["key"] == "i15")
        self.assertIn("/campaigns?act=1&selected_campaign_ids=", camp["link"])
        self.assertTrue(any(a[4] == "checking" and a[2] == "Checking what is live on Meta" for a in cb))
        self.assertNotIn("Everything is live", r["message"])

    def test_failed_read_back_never_fails_the_push(self):
        r, g, _ = self.push(fail=True)
        self.assertTrue(r["ok"])
        self.assertEqual({x["live_status"] for x in r["items"]}, {"unknown"})
        self.assertIn("20 with status unknown", r["summary"])
        self.assertEqual(g.made(), sorted(g.made()))  # objects were made as usual

    def test_jobs_keep_live_fields(self):
        out = jobs._items_from_result({"items": [{"key": "i1", "label": "x", "status": "done", "live_status": "PENDING_REVIEW",
                                                  "review": None, "level": "ad", "link": "L", "ids": {"ad_id": "1"}}]})
        self.assertEqual((out[0]["live_status"], out[0]["level"], out[0]["link"]), ("PENDING_REVIEW", "ad", "L"))


# ---------- resumable video upload ----------

class Uploads:
    """Fake Graph for advideos: start / transfer (via _post_multipart) / finish, video status polls, thumbnails."""

    def __init__(self, size, chunk=256, fail_chunks=(), resume_to=None, statuses=None, gone=False):
        self.size, self.chunk = size, chunk
        self.fail_chunks = list(fail_chunks)  # start offsets whose next transfer fails once
        self.resume_to = resume_to
        self.statuses = list(statuses or [{"video_status": "ready"}])
        self.gone = gone
        self.calls, self.got = [], bytearray()

    def once(self, method, path, params=None, validate=False, token=None):
        params = dict(params or {})
        self.calls.append((method, path, params.get("upload_phase") or params.get("fields")))
        if method == "POST" and path.endswith("/advideos"):
            if params["upload_phase"] == "start":
                assert int(params["file_size"]) == self.size
                return {"upload_session_id": "sess1", "video_id": "v1", "start_offset": "0", "end_offset": str(min(self.chunk, self.size))}
            if params["upload_phase"] == "finish":
                return {"success": True}
        if method == "GET" and path == "v1":
            if self.gone:
                raise meta_api.MetaError({"message": "Object v1 does not exist", "code": 100}, 400)
            st = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            return {"status": st}
        if method == "GET" and path == "v1/thumbnails":
            return {"data": [{"uri": "https://thumb/v1.jpg", "is_preferred": True}]}
        raise AssertionError((method, path, params))

    def transfer(self, edge, params, field, filename, chunk, timeout=None):
        start = int(params["start_offset"])
        self.calls.append(("POST", edge, "transfer", start, len(chunk)))
        assert params["upload_session_id"] == "sess1" and field == "video_file_chunk"
        if start in self.fail_chunks:
            self.fail_chunks.remove(start)
            if self.resume_to is not None:
                a, b = self.resume_to
                raise meta_api.MetaError({"message": "chunk out of order", "code": 6001, "error_subcode": 1363037,
                                          "error_data": json.dumps({"start_offset": a, "end_offset": b})}, 400)
            raise meta_api.MetaError({"message": "no answer from Meta (timed out)", "is_transient": True})
        assert len(self.got) == start, (len(self.got), start)
        self.got += chunk
        nxt = start + len(chunk)
        return {"start_offset": str(nxt), "end_offset": str(min(nxt + self.chunk, self.size))}


class ReadLog:
    """Wraps a file opened by meta_ads to record every read size."""

    def __init__(self, f, log):
        self.f, self.log = f, log

    def read(self, n=-1):
        self.log.append(n)
        return self.f.read(n)

    def __getattr__(self, k):
        return getattr(self.f, k)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.f.close()


class VideoBase(Paced):
    def setUp(self):
        super().setUp()
        for p in (mock.patch.object(meta.time, "sleep", lambda s: None), mock.patch.object(meta, "POLL_EVERY", 0)):
            p.start()
            self.addCleanup(p.stop)
        self.data = MP4 + bytes(range(256)) * 4 + b"tail-bytes"  # 1054 bytes
        self.mid = meta.store_media("clip.mp4", self.data)["media_id"]
        meta._media_save(lambda idx: idx[self.mid].pop("uploads", None) and None)
        self.reads = []
        real_open = builtins.open

        def tracking_open(path, mode="r", *a, **k):
            f = real_open(path, mode, *a, **k)
            return ReadLog(f, self.reads) if "b" in mode and str(path).endswith(".mp4") else f
        p = mock.patch.object(meta, "open", tracking_open, create=True)
        p.start()
        self.addCleanup(p.stop)

    def graph(self, **kw):
        u = Uploads(len(self.data), **kw)
        for p in (mock.patch.object(meta_api, "_once", u.once), mock.patch.object(meta, "_post_multipart", u.transfer)):
            p.start()
            self.addCleanup(p.stop)
        return u

    def transfers(self, u):
        return [(c[3], c[4]) for c in u.calls if c[2] == "transfer"]


class VideoUpload(VideoBase):
    def test_chunked_offsets_and_reads(self):
        u = self.graph(statuses=[{"video_status": "processing", "processing_progress": 30},
                                 {"video_status": "processing", "processing_progress": 70}, {"video_status": "ready"}])
        seen = []
        ref = meta.upload_media(self.mid, lambda label, status, pct=None: seen.append((label, status)))
        self.assertEqual(ref["video_id"], "v1")
        self.assertEqual(ref["image_url"], "https://thumb/v1.jpg")
        self.assertEqual(bytes(u.got), self.data)  # every byte once, in order
        self.assertEqual(self.transfers(u), [(0, 256), (256, 256), (512, 256), (768, 256), (1024, 30)])
        self.assertEqual([c[2] for c in u.calls if c[1].endswith("/advideos") and c[2] != "transfer"], ["start", "finish"])
        # the file is read in chunks, never whole
        self.assertTrue(self.reads)
        self.assertTrue(all(isinstance(n, int) and 0 < n <= 256 for n in self.reads), self.reads)
        self.assertIn(("Uploading video 0%", "uploading"), seen)
        self.assertIn(("Uploading video 48%", "uploading"), seen)
        self.assertIn(("Uploading video 100%", "uploading"), seen)
        self.assertEqual([x for x in seen if x[1] == "processing"],
                         [("Meta is processing the video 30%", "processing"), ("Meta is processing the video 70%", "processing")])
        stored = meta.media_info(self.mid)["uploads"][ACT]
        self.assertEqual((stored["video_id"], stored["image_url"]), ("v1", "https://thumb/v1.jpg"))

    def test_failed_chunk_is_sent_again(self):
        u = self.graph(fail_chunks=[512])
        meta.upload_media(self.mid)
        self.assertEqual(self.transfers(u), [(0, 256), (256, 256), (512, 256), (512, 256), (768, 256), (1024, 30)])
        self.assertEqual(bytes(u.got), self.data)

    def test_resumes_where_meta_says(self):
        u = self.graph(fail_chunks=[512], resume_to=(512, 700))
        meta.upload_media(self.mid)
        self.assertEqual(self.transfers(u), [(0, 256), (256, 256), (512, 256), (512, 188), (700, 256), (956, 98)])
        self.assertEqual(bytes(u.got), self.data)

    def test_gives_up_after_tries_and_keeps_nothing(self):
        u = self.graph(fail_chunks=[256] * meta.CHUNK_TRIES)
        with self.assertRaises(meta_api.MetaError) as cm:
            meta.upload_media(self.mid)
        self.assertIn("stopped at 24%", str(cm.exception))
        self.assertEqual(len([t for t in self.transfers(u) if t[0] == 256]), meta.CHUNK_TRIES)
        self.assertNotIn(ACT, meta.media_info(self.mid).get("uploads") or {})
        self.assertNotIn("finish", [c[2] for c in u.calls])

    def test_processing_error(self):
        self.graph(statuses=[{"video_status": "error", "processing_progress": 0}])
        with self.assertRaises(meta_api.MetaError):
            meta.upload_media(self.mid)
        self.assertEqual(meta.media_info(self.mid)["uploads"][ACT]["video_id"], "v1")  # kept: the next try reuses it

    def test_reused_video_checked_with_one_get(self):
        self.graph()
        meta.upload_media(self.mid)
        u = self.graph()
        ref = meta.upload_media(self.mid)
        self.assertEqual(ref["video_id"], "v1")
        self.assertEqual(u.calls, [("GET", "v1", "status")])  # no upload, one check

    def test_reused_video_still_processing_is_waited_for(self):
        self.graph(statuses=[{"video_status": "processing", "processing_progress": 10}, {"video_status": "ready"}])
        meta._remember_upload(self.mid, {"video_id": "v1"})  # an earlier upload whose processing timed out
        u = self.graph(statuses=[{"video_status": "processing", "processing_progress": 50}, {"video_status": "ready"}])
        ref = meta.upload_media(self.mid)
        self.assertEqual(ref["image_url"], "https://thumb/v1.jpg")
        self.assertFalse([c for c in u.calls if c[1].endswith("/advideos")])
        self.assertEqual([c for c in u.calls if c[1] == "v1"], [("GET", "v1", "status")] * 2)

    def test_reused_video_gone_is_uploaded_again(self):
        meta._remember_upload(self.mid, {"video_id": "v1", "image_url": "https://old"})
        u = self.graph(gone=True)
        with mock.patch.object(meta, "_wait_video", lambda vid, **k: "https://thumb/new.jpg"):
            ref = meta.upload_media(self.mid)
        self.assertEqual(ref["image_url"], "https://thumb/new.jpg")
        self.assertEqual(bytes(u.got), self.data)

    def test_push_step_reports_upload_progress(self):
        self.graph()
        cb = []
        meta.upload_media(self.mid, meta._step_report({"__report": lambda item, label, st: cb.append((item, label, st))},
                                                      {"item": "i7"}))
        self.assertIn(("i7", "Uploading video 100%", "uploading"), cb)


# ---------- Prepare for Meta + status route ----------

def wait_job(jid, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        j = jobs.get(jid)
        if j and j["state"] != "running":
            return j
        time.sleep(0.01)
    raise AssertionError("job still running")


class Routes(VideoBase):
    def setUp(self):
        super().setUp()
        jobs._reset()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), server.H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_address[1]
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        p = mock.patch.object(server.meta, "use", lambda key: key)
        p.start()
        self.addCleanup(p.stop)

    def call(self, method, path, body=None, headers=None, host=None, press=True):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": host or f"127.0.0.1:{self.port}"}
        data = None
        if body is not None:
            data = json.dumps(body)
            h["Content-Type"] = "application/json"
            if press:
                h["X-Studio-Press"] = server.PRESS
        h.update(headers or {})
        c.request(method, path, data, h)
        r = c.getresponse()
        out = (r.status, json.loads(r.read() or b"null"))
        c.close()
        return out

    def test_prepare_job_stores_and_push_reuses(self):
        u = self.graph(statuses=[{"video_status": "processing", "processing_progress": 40}, {"video_status": "ready"}])
        name = meta.media_info(self.mid)["file"]
        st, out = self.call("POST", "/api/meta/media/prepare", {"acct": "meta", "path": name})
        self.assertEqual(st, 202, out)
        self.assertEqual(out["media_id"], self.mid)
        j = wait_job(out["job"])
        self.assertEqual((j["kind"], j["source"], j["state"], j["ok"]), ("upload", "meta", "done", True), j)
        self.assertEqual(j["result"]["video_id"], "v1")
        self.assertEqual(j["items"][0]["status"], "done")
        self.assertEqual(bytes(u.got), self.data)
        self.assertEqual(meta.media_info(self.mid)["uploads"][ACT]["video_id"], "v1")
        # later, Push live: reuses the prepared video after one readiness check
        u2 = self.graph()
        ref = meta.upload_media(self.mid)
        self.assertEqual((ref["video_id"], ref["image_url"]), ("v1", "https://thumb/v1.jpg"))
        self.assertEqual(u2.calls, [("GET", "v1", "status")])

    def test_prepare_by_media_id_and_full_path(self):
        self.graph()
        for path in (self.mid, str(meta.MEDIA_DIR / meta.media_info(self.mid)["file"])):
            jobs._reset()
            st, out = self.call("POST", "/api/meta/media/prepare", {"acct": "meta", "path": path})
            self.assertEqual(st, 202, out)
            wait_job(out["job"])

    def test_prepare_guards(self):
        self.graph()
        name = meta.media_info(self.mid)["file"]
        body = {"acct": "meta", "path": name}
        self.assertEqual(self.call("POST", "/api/meta/media/prepare", body, press=False)[0], 403)
        self.assertEqual(self.call("POST", "/api/meta/media/prepare", body, headers={"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.call("POST", "/api/meta/media/prepare", body, headers={"Content-Type": "text/plain"})[0], 403)
        self.assertEqual(self.call("POST", "/api/meta/media/prepare", body, host="evil.example")[0], 403)
        for bad in ("/etc/passwd", "../index.json", "nope.mp4", ""):
            with self.subTest(path=bad):
                st, out = self.call("POST", "/api/meta/media/prepare", {"acct": "meta", "path": bad})
                self.assertEqual(st, 400, out)
        self.assertEqual(self.call("POST", "/api/meta/media/prepare", {"acct": "1234567890", "path": name})[0], 400)
        self.assertEqual(jobs.latest(), [])

    def test_second_prepare_of_same_file_is_refused(self):
        go = threading.Event()
        with mock.patch.object(meta, "prepare_media", lambda mid, progress=None: go.wait(5) and {"ok": True}):
            st, out = self.call("POST", "/api/meta/media/prepare", {"acct": "meta", "path": self.mid})
            st2, out2 = self.call("POST", "/api/meta/media/prepare", {"acct": "meta", "path": self.mid})
            go.set()
        self.assertEqual((st, st2), (202, 409))
        self.assertEqual(out2["job"], out["job"])
        wait_job(out["job"])

    def test_status_route_read_only_and_guarded(self):
        g = self.fake(Status({"11": {"name": "Ad", "effective_status": "PENDING_REVIEW", "configured_status": "ACTIVE"}}))
        st, out = self.call("GET", "/api/meta/status?acct=meta&ids=11,12")
        self.assertEqual(st, 200, out)
        self.assertEqual([(r["id"], r["live_status"]) for r in out["statuses"]], [("11", "PENDING_REVIEW"), ("12", "unknown")])
        self.assertIn("selected_ad_ids=11", out["statuses"][0]["link"])
        st, out = self.call("GET", "/api/meta/status?acct=meta&level=campaign&ids=11")
        self.assertIn("selected_campaign_ids=11", out["statuses"][0]["link"])
        self.assertEqual({c[0] for c in g.calls}, {"GET"})
        self.assertEqual(self.call("GET", "/api/meta/status?acct=meta&ids=11", host="evil.example:80")[0], 403)
        self.assertEqual(self.call("GET", "/api/meta/status?acct=meta&ids=")[0], 400)
        self.assertEqual(self.call("GET", "/api/meta/status?acct=meta&ids=1,act_2")[0], 400)
        self.assertEqual(self.call("GET", "/api/meta/status?acct=meta&ids=1&level=account")[0], 400)
        self.assertEqual(self.call("GET", "/api/meta/status?acct=1234567890&ids=1")[0], 400)
        self.assertEqual(self.call("POST", "/api/meta/status", {"acct": "meta", "ids": "11"})[0], 404)  # GET only


if __name__ == "__main__":
    unittest.main()
