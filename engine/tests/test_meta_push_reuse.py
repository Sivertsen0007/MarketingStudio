"""Meta push speed, safety unchanged (v0.3 step H). Run from the engine folder:
    python3 -m unittest tests.test_meta_push_reuse

- a push reuses a preview's validate_only pass ONLY for the same ad account, a preview finished < REUSE_MAX_AGE s ago,
  the exact same step and the same freshly re-read live state of every object the step touches; anything else is
  checked again, a problem is never reused, the memory is bounded and emptied after every push; REUSE_CHECKS is off by default (tests switch it on)
- the per-level batched live reads (plan) and the read-back levels (after a push) run side by side
- video upload: a chunk larger than CHUNK_READ_MAX is streamed from disk, a saved video Meta could not process is
  uploaded again, and Prepare for Meta answers JSON for any error while resolving the file

Offline: fake Graph APIs replace meta_api._once and meta_ads._post_multipart (or urlopen); nothing is sent anywhere.
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("MSTUDIO_HOME", _tf.mkdtemp(prefix="mstudio-test-"))
_os.environ["MSTUDIO_KEYCHAIN"] = "0"

import io
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests import test_meta_fast_push as fp  # noqa: E402  (sets core.STUDIO_DIR to a temp folder)
from tests import test_meta_live_status as ls  # noqa: E402
import meta_ads as meta  # noqa: E402
import meta_api  # noqa: E402
import server  # noqa: E402

ACT = fp.ACT
_DEFAULT_REUSE = meta.REUSE_CHECKS  # as shipped, before any test patches it


def meta_default_reuse():
    return _DEFAULT_REUSE
ALL_CHECKS = 21  # validate_only calls for fp.items(): one per step, a copied ad makes two
ALL_STEPS = 20  # steps (what a pass is remembered for)


def checks(g, since=0):
    return [c[1] for c in g.calls[since:] if c[0] == "VALIDATE"]


class Reuse(fp.Base):
    def setUp(self):
        super().setUp()
        meta.forget_checks()
        self.addCleanup(meta.forget_checks)
        self.acct = [ACT]
        for p in (mock.patch.object(meta, "act", lambda: self.acct[0]), mock.patch.object(meta, "REUSE_CHECKS", True),
                  mock.patch.object(meta_api, "TOKEN", "token-one")):
            p.start()
            self.addCleanup(p.stop)

    def push(self, **kw):
        with mock.patch.object(meta, "refresh_account", lambda: None):
            return meta.push(accept_spend=True, **kw)

    def preview_then(self, g, between=None, **kw):
        """preview, then (optionally) change something, then push. -> (preview result, push result, push's checks)."""
        pv = meta.preview()
        if between:
            between()
        n = len(g.calls)
        res = self.push(**kw)
        return pv, res, checks(g, n)

    # ----- the push reuses only under the exact conditions -----

    def test_unchanged_preview_then_push_reuses_every_pass(self):
        g = self.graph()
        pv, res, again = self.preview_then(g)
        self.assertTrue(pv["ok"])
        self.assertEqual(pv["checks_reused"], 0)
        self.assertEqual(len(checks(g)), ALL_CHECKS)  # the preview's own checks
        self.assertTrue(res["ok"], res.get("message"))
        self.assertEqual(again, [])  # every pass reused
        self.assertEqual(res["checks_reused"], ALL_STEPS)
        # the push still read every object live again (batched) before sending
        reads = [c for c in g.calls if c[0] == "GET" and c[1] == ""]
        self.assertGreaterEqual(len(reads), 6)

    def test_off_by_default(self):
        self.assertIs(meta_default_reuse(), False)  # shipped off
        g = self.graph()
        with mock.patch.object(meta, "REUSE_CHECKS", meta_default_reuse()):
            _pv, res, again = self.preview_then(g)
        self.assertEqual(len(again), ALL_CHECKS)
        self.assertEqual(res["checks_reused"], 0)

    def test_other_token_checks_again(self):
        g = self.graph()
        _pv, res, again = self.preview_then(g, between=lambda: setattr(meta_api, "TOKEN", "token-two"))
        self.assertEqual(len(again), ALL_CHECKS)
        self.assertEqual(res["checks_reused"], 0)

    def test_token_never_stored(self):
        self.graph()
        meta.preview()
        self.assertTrue(meta._reuse)
        self.assertNotIn("token-one", repr(list(meta._reuse.items())))

    def test_other_account_checks_again(self):
        g = self.graph()
        _pv, res, again = self.preview_then(g, between=lambda: self.acct.__setitem__(0, "act_2"))
        self.assertEqual(len(again), ALL_CHECKS)

    def test_old_preview_checks_again(self):
        g = self.graph()

        def age():
            with meta._reuse_lock:
                for k, (a, t) in list(meta._reuse.items()):
                    meta._reuse[k] = (a, t - meta.REUSE_MAX_AGE - 1)
        _pv, _res, again = self.preview_then(g, between=age)
        self.assertEqual(len(again), ALL_CHECKS)

    def test_just_under_the_age_limit_reuses(self):
        g = self.graph()

        def age():
            with meta._reuse_lock:
                for k, (a, t) in list(meta._reuse.items()):
                    meta._reuse[k] = (a, t - meta.REUSE_MAX_AGE + 5)
        _pv, _res, again = self.preview_then(g, between=age)
        self.assertEqual(again, [])

    def test_changed_params_check_that_step_again(self):
        g = self.graph()

        def edit():
            it = next(i for i in self.items if i["change"].get("type") == "meta_budget" and i["change"]["id"] == "s2")
            it["change"] = meta._normalise({"type": "meta_budget", "level": "adset", "id": "s2", "budget": 99})
        _pv, res, again = self.preview_then(g, between=edit)
        self.assertTrue(res["ok"], res.get("message"))
        self.assertEqual(again, ["s2"])

    def test_enable_new_changes_the_step(self):
        g = self.graph()
        meta.preview()
        n = len(g.calls)
        self.push(enable_new=True)
        self.assertEqual(sorted(checks(g, n)), sorted([ACT + "/adcreatives", ACT + "/ads"]))  # the new ad: now Active

    def test_changed_live_object_checks_its_step_again(self):
        g = self.graph()

        def change():
            with g.lock:
                g.objs["a2"]["adset_id"] = "s2"  # moved in Ads Manager: not in the step's params, only in its live state
        _pv, _res, again = self.preview_then(g, between=change)
        self.assertEqual(again, ["a2"])

    def test_changed_parent_checks_every_step_touching_it(self):
        g = self.graph()

        def change():
            with g.lock:
                g.objs["s1"]["daily_budget"] = "4444"  # the ad set a new ad is copied into, also paused and re-budgeted
        _pv, _res, again = self.preview_then(g, between=change)
        self.assertEqual(sorted(again), sorted(["s1", "s1", ACT + "/adcreatives", ACT + "/ads"]))

    def test_changed_campaign_list_checks_new_adset_in_new_campaign(self):
        g = self.graph()

        def change():
            with g.lock:
                g.objs["c9"]["name"] = "Twin renamed"  # the stand-in campaign for "Set in new"
        _pv, _res, again = self.preview_then(g, between=change)
        self.assertIn(ACT + "/adsets", again)
        self.assertNotIn("a1", again)

    def test_switched_off(self):
        g = self.graph()
        with mock.patch.object(meta, "REUSE_CHECKS", False):
            pv, _res, again = self.preview_then(g)
        self.assertEqual(len(again), ALL_CHECKS)
        self.assertEqual(len(meta._reuse), 0)

    def test_plain_plan_remembers_nothing(self):
        g = self.graph()
        meta.plan()
        self.assertEqual(len(meta._reuse), 0)
        n = len(g.calls)
        self.push()
        self.assertEqual(len(checks(g, n)), ALL_CHECKS)

    def test_unknown_change_types_are_never_reused(self):
        self.assertIsNone(meta._touches({"type": "meta_boost_post", "adset_id": "s1"}))
        self.assertIsNone(meta._touches({"type": "meta_audience"}))
        self.assertEqual(meta._touches({"type": "meta_new_ad", "adset_id": "s1", "from_ad": "a1"}), [("adset", "s1"), ("ad", "a1")])

    # ----- a problem is never reused as a pass -----

    def test_refused_check_is_never_reused(self):
        g = self.graph(refuse_check={"a3"})
        pv = meta.preview()
        self.assertFalse(pv["ok"])
        self.assertEqual(len(meta._reuse), ALL_STEPS - 1)  # only the passes
        g.refuse_check.clear()  # Meta would now accept it: the push must ask, not reuse
        n = len(g.calls)
        res = self.push()
        self.assertTrue(res["ok"], res.get("message"))
        self.assertEqual(checks(g, n), ["a3"])

    def test_refused_check_still_refused_blocks_the_push(self):
        g = self.graph(refuse_check={"a3"})
        meta.preview()
        res = self.push()
        self.assertFalse(res["ok"])
        self.assertEqual([c for c in g.calls if c[0] == "POST"], [])

    def test_rate_limited_check_is_not_remembered(self):
        g = self.graph(busy={"a4": -1})
        with mock.patch.object(meta, "CHECK_BUSY_WAIT", 0):
            pv = meta.preview()
        self.assertFalse(pv["ok"])
        self.assertEqual(len(meta._reuse), ALL_STEPS - 1)
        g.busy.clear()
        n = len(g.calls)
        self.push()
        self.assertEqual(checks(g, n), ["a4"])

    # ----- accounts, memory bounds, clearing -----

    def test_never_across_accounts(self):
        self.graph()
        meta.preview()
        key = next(iter(meta._reuse))
        self.assertTrue(meta._reused(key))
        self.acct[0] = "act_2"
        self.assertFalse(meta._reused(key))  # the same key, asked from another account
        self.assertFalse(meta._reused(("act_2",) + key[1:]))
        self.acct[0] = ACT
        self.assertFalse(meta._reused(key[:1] + ("other-token",) + key[2:]))

    def test_cleared_after_every_push(self):
        g = self.graph()
        meta.preview()
        self.assertTrue(meta._reuse)
        self.push()
        self.assertEqual(len(meta._reuse), 0)
        g = self.graph()  # the same world again, no preview in between: the push checks everything
        self.push()
        self.assertEqual(len(checks(g)), ALL_CHECKS)

    def test_cleared_after_a_push_that_was_not_sent(self):
        self.graph()
        meta.preview()
        with mock.patch.object(meta, "refresh_account", lambda: None):
            res = meta.push(accept_spend=False)
        self.assertFalse(res["ok"])
        self.assertEqual(len(meta._reuse), 0)

    def test_cleared_after_a_push_that_crashed(self):
        self.graph()
        meta.preview()
        with mock.patch.object(meta, "_push", mock.Mock(side_effect=RuntimeError("boom"))):
            with self.assertRaises(RuntimeError):
                meta.push()
        self.assertEqual(len(meta._reuse), 0)

    def test_bounded(self):
        self.graph()
        with mock.patch.object(meta, "REUSE_MAX", 5):
            meta.preview()
            self.assertEqual(len(meta._reuse), 5)

    def test_thread_safe(self):
        errs = []

        def worker(k):
            try:
                for i in range(300):
                    key = (ACT, f"s{k}-{i}", "l")
                    meta._remember([key])
                    meta._reused(key)
                    if i % 50 == 0:
                        meta.forget_checks()
            except Exception as ex:  # noqa: BLE001
                errs.append(ex)
        with mock.patch.object(meta, "REUSE_MAX", 100):
            ts = [threading.Thread(target=worker, args=(k,)) for k in range(8)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            self.assertEqual(errs, [])
            self.assertLessEqual(len(meta._reuse), 100)


class ParallelReads(fp.Base):
    def test_prefetch_levels_side_by_side(self):
        g = self.graph(delay=0.2)
        meta._plan_tls.live = {}
        try:
            t0 = time.monotonic()
            n = meta._prefetch([it["change"] for it in self.items], workers=8)
            wall = time.monotonic() - t0
            cached = set(meta._plan_tls.live)
        finally:
            meta._plan_tls.live = None
        self.assertEqual(n, 3)  # campaign, ad set, ad
        self.assertEqual(g.peak, 3)
        self.assertLess(wall, 0.5)  # one after the other: 0.6 s
        self.assertIn(("ad", "a1"), cached)
        self.assertIn(("adset", "s1"), cached)
        self.assertIn(("campaign", "c1"), cached)

    def test_prefetch_refused_level_still_falls_back(self):
        self.items.append({"id": "i99", "change": meta._normalise({"type": "meta_status", "level": "ad", "id": "a404",
                                                                     "status": "paused"}), "by": "you", "summary": "gone",
                           "problems": []})
        self.graph()
        meta._plan_tls.live = {}
        try:
            meta._prefetch([it["change"] for it in self.items])
            cached = set(meta._plan_tls.live)
        finally:
            meta._plan_tls.live = None
        self.assertNotIn(("ad", "a1"), cached)  # the whole ad read was refused: each ad reads on its own later
        self.assertIn(("adset", "s1"), cached)

    def test_read_back_levels_side_by_side(self):
        g = self.graph(delay=0.2)
        t0 = time.monotonic()
        got = meta.read_status([("ad", "a1"), ("ad", "a2"), ("adset", "s1"), ("campaign", "c1")])
        wall = time.monotonic() - t0
        self.assertEqual(len([c for c in g.calls if c[0] == "GET"]), 3)
        self.assertEqual(g.peak, 3)
        self.assertLess(wall, 0.5)
        self.assertEqual(set(got), {"a1", "a2", "s1", "c1"})


# ---------- video upload fixes ----------

class StreamingUploads(ls.Uploads):
    """Like ls.Uploads, but a chunk may be a _FileSlice: read it the way http.client does, in pieces."""

    def transfer(self, edge, params, field, filename, chunk, timeout=None):
        if not isinstance(chunk, (bytes, bytearray)):
            self.streamed = getattr(self, "streamed", 0) + 1
            chunk = b"".join(iter(lambda: chunk.read(8192), b""))
        return super().transfer(edge, params, field, filename, chunk, timeout)


class Video(ls.VideoBase):
    def graph(self, cls=ls.Uploads, **kw):
        u = cls(len(self.data), **kw)
        for p in (mock.patch.object(meta_api, "_once", u.once), mock.patch.object(meta, "_post_multipart", u.transfer)):
            p.start()
            self.addCleanup(p.stop)
        return u

    def test_big_chunk_is_streamed_never_read_whole(self):
        with mock.patch.object(meta, "CHUNK_READ_MAX", 100):
            u = self.graph(StreamingUploads)
            meta.upload_media(self.mid)
        self.assertEqual(bytes(u.got), self.data)
        self.assertEqual(u.streamed, 4)  # the four 256 byte chunks are above the 100 byte cap; the last (30) is read
        self.assertEqual(self.transfers(u), [(0, 256), (256, 256), (512, 256), (768, 256), (1024, 30)])
        self.assertTrue(self.reads)
        self.assertTrue(all(isinstance(n, int) and 0 < n <= 100 for n in self.reads), self.reads)

    def test_small_chunks_unchanged(self):
        u = self.graph()
        meta.upload_media(self.mid)
        self.assertEqual(bytes(u.got), self.data)
        self.assertTrue(all(0 < n <= 256 for n in self.reads), self.reads)

    def test_multipart_streams_the_file_slice(self):
        path = Path(_tf.mkdtemp()) / "v.bin"
        payload = bytes(range(256)) * 40  # 10240 bytes
        path.write_bytes(payload)
        sent = {}

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def urlopen(req, timeout=None):
            body, reads = b"", []
            while True:
                b = req.data.read(8192)
                reads.append(len(b))
                if not b:
                    break
                body += b
            sent.update(body=body, length=req.get_header("Content-length"), reads=reads)
            return Resp(b'{"start_offset": "0", "end_offset": "0"}')
        fileread = []
        with mock.patch.object(meta, "CHUNK_READ_MAX", 1000), mock.patch.object(meta.urllib.request, "urlopen", urlopen), \
                open(path, "rb") as f:
            real = f.read

            def tracked(n=-1):
                fileread.append(n)
                return real(n)
            f.read = tracked
            out = meta._post_multipart(ACT + "/advideos", {"upload_phase": "transfer", "start_offset": 100}, "video_file_chunk",
                                       "v.bin", meta._FileSlice(f, 100, 9000))
        self.assertEqual(out, {"start_offset": "0", "end_offset": "0"})
        self.assertEqual(int(sent["length"]), len(sent["body"]))
        self.assertIn(payload[100:9100], sent["body"])
        self.assertNotIn(payload[100:9101], sent["body"])  # exactly the bytes asked for
        self.assertTrue(sent["body"].endswith(b"--\r\n"))
        self.assertTrue(all(0 < n <= 1000 for n in fileread), fileread)

    def test_saved_video_in_error_is_uploaded_again(self):
        meta._remember_upload(self.mid, {"video_id": "v1", "image_url": "https://old"})
        u = self.graph(statuses=[{"video_status": "error", "processing_progress": 0}, {"video_status": "ready"}])
        ref = meta.upload_media(self.mid)
        self.assertEqual(bytes(u.got), self.data)  # uploaded again
        self.assertEqual(ref["image_url"], "https://thumb/v1.jpg")
        self.assertEqual([c[2] for c in u.calls if c[1].endswith("/advideos") and c[2] != "transfer"], ["start", "finish"])

    def test_saved_video_ready_is_still_reused(self):
        meta._remember_upload(self.mid, {"video_id": "v1", "image_url": "https://old"})
        u = self.graph()
        ref = meta.upload_media(self.mid)
        self.assertEqual(ref["image_url"], "https://old")
        self.assertEqual(u.calls, [("GET", "v1", "status")])


class PrepareErrors(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(server.meta, "use", lambda key: key)
        p.start()
        self.addCleanup(p.stop)

    def test_unexpected_error_is_a_json_500(self):
        with mock.patch.object(server.meta, "resolve_media", mock.Mock(side_effect=RuntimeError("index unreadable"))), \
                mock.patch.object(server.traceback, "print_exc", lambda: None):
            st, out = server.prepare_job("meta", {"path": "x.mp4"})
        self.assertEqual(st, 500)
        self.assertIn("index unreadable", out["error"])

    def test_bad_input_is_a_json_400(self):
        for ex in (KeyError("file"), TypeError("bad path"), ValueError("not a file")):
            with self.subTest(ex=ex), mock.patch.object(server.meta, "resolve_media", mock.Mock(side_effect=ex)):
                st, out = server.prepare_job("meta", {"path": "x.mp4"})
                self.assertEqual(st, 400)
                self.assertTrue(out["error"])

    def test_use_failing_is_answered_too(self):
        with mock.patch.object(server.meta, "use", mock.Mock(side_effect=OSError("settings unreadable"))), \
                mock.patch.object(server.traceback, "print_exc", lambda: None):
            st, out = server.prepare_job("meta", {"path": "x.mp4"})
        self.assertEqual(st, 500)


if __name__ == "__main__":
    unittest.main()
