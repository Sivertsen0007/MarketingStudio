"""Meta push robustness (v0.2.3 step S2b, T09 option B). Run from the engine folder:
    python3 -m unittest tests.test_meta_s2b

- M15 a reused video gets a fresh thumbnail URL at push (Meta's URLs expire)
- M16 no answer from Meta is one connection banner, never "these changes can't go live"
- M23 image / video size, aspect and length read from the file headers, warnings per placement; raw byte uploads
- M24 landing pages read once per URL (cached, side by side), never inside the draft lock
- M25 undo reads each object first and leaves fields changed since the push as they are

Offline: fake Graph APIs replace meta_api._once and the landing page reader; nothing is sent anywhere.
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("MSTUDIO_HOME", _tf.mkdtemp(prefix="mstudio-test-"))
_os.environ["MSTUDIO_KEYCHAIN"] = "0"

import contextlib
import http.client
import io
import json
import struct
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
import meta_ads as meta  # noqa: E402
import meta_api  # noqa: E402

ACT = fp.ACT


# ---------- small media files built in memory ----------

def png(w, h):
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", w, h) + b"\x08\x02\x00\x00\x00" + b"\x00" * 4 + b"rest"


def jpeg(w, h):
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 17, 8, h, w, 3) + b"\x00" * 9
    return b"\xff\xd8" + app0 + sof + b"\xff\xd9"


def box(typ, payload):
    return struct.pack(">I", 8 + len(payload)) + typ + payload


def mp4(w, h, seconds, rotate=False, moov_last=True):
    mvhd = b"\x00\x00\x00\x00" + struct.pack(">IIII", 0, 0, 1000, int(seconds * 1000)) + b"\x00" * 80
    matrix = ([0, 0x10000, 0, -0x10000, 0, 0, 0, 0, 0x40000000] if rotate else [0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000])
    tkhd = (b"\x00\x00\x00\x07" + struct.pack(">IIIII", 0, 0, 1, 0, int(seconds * 1000)) + b"\x00" * 8 + b"\x00" * 8
            + struct.pack(">9i", *matrix) + struct.pack(">II", w << 16, h << 16))
    audio = (b"\x00\x00\x00\x07" + struct.pack(">IIIII", 0, 0, 2, 0, 0) + b"\x00" * 16 + struct.pack(">9i", *matrix) + b"\x00" * 8)
    moov = box(b"moov", box(b"mvhd", mvhd) + box(b"trak", box(b"tkhd", audio)) + box(b"trak", box(b"tkhd", tkhd)))
    head = box(b"ftyp", b"mp42\x00\x00\x00\x00mp42")
    mdat = box(b"mdat", b"\x11" * 300)
    return head + (mdat + moov if moov_last else moov + mdat)


IG_STORY = {"publisher_platforms": ["instagram"], "instagram_positions": ["story"]}
FB_FEED = {"publisher_platforms": ["facebook"], "facebook_positions": ["feed"]}


class MediaFacts(unittest.TestCase):
    def store(self, name, data):
        return meta.store_media(name, data)

    def test_png_jpeg_and_video_headers(self):
        self.assertEqual(meta._png_size(png(1080, 1920)), (1080, 1920))
        self.assertEqual(meta._jpeg_size(jpeg(1200, 628)), (1200, 628))
        r = self.store("clip.mp4", mp4(1920, 1080, 42.5))
        self.assertEqual((r["width"], r["height"], r["duration"]), (1920, 1080, 42.5))
        r = self.store("turned.mov", mp4(1920, 1080, 9, rotate=True, moov_last=False))
        self.assertEqual((r["width"], r["height"]), (1080, 1920))  # filmed on a phone held upright
        r = self.store("pic.jpg", jpeg(1080, 1080))
        self.assertEqual((r["width"], r["height"]), (1080, 1080))
        self.assertNotIn("duration", r)

    def test_warnings_per_placement(self):
        wide = self.store("wide.png", png(1920, 1080))["media_id"]
        self.assertEqual(meta.media_warnings(wide, FB_FEED), [])  # 16:9 is fine in Feed
        w = meta.media_warnings(wide, IG_STORY)
        self.assertEqual(len(w), 1)
        self.assertIn("Stories", w[0])
        self.assertIn("9:16", w[0])
        auto = meta.media_warnings(wide, {})
        self.assertTrue(any("if Meta shows it there" in x for x in auto), auto)  # Advantage+ placements: Meta picks
        tall = self.store("tall.png", png(1080, 1920))["media_id"]
        self.assertEqual(meta.media_warnings(tall, IG_STORY), [])
        self.assertTrue(any("Feed" in x and "cropped" in x for x in meta.media_warnings(tall, FB_FEED)))
        small = self.store("small.png", png(400, 400))["media_id"]
        self.assertTrue(any("400x400" in x and "blurry" in x for x in meta.media_warnings(small, FB_FEED)))

    def test_video_length_per_placement(self):
        long_ = self.store("long.mp4", mp4(1920, 1080, 200))["media_id"]
        self.assertTrue(any("Stories" in x and "200 s" in x for x in meta.media_warnings(long_, IG_STORY)))
        self.assertEqual(meta.media_warnings(long_, FB_FEED), [])
        mid = self.store("mid.mp4", mp4(1080, 1920, 90))["media_id"]
        self.assertTrue(any("shown in parts" in x for x in meta.media_warnings(mid, IG_STORY)))
        unread = meta.store_media("odd.mp4", b"\x00\x00\x00\x08ftyp" + b"\x00" * 50)["media_id"]
        self.assertTrue(any("couldn't read" in x for x in meta.media_warnings(unread, FB_FEED)))

    def test_old_library_entry_gets_its_facts_once(self):
        mid = self.store("old.png", png(800, 800))["media_id"]
        meta._media_save(lambda idx: idx[mid].pop("facts"))
        self.assertEqual(meta.media_facts(mid), {"width": 800, "height": 800})
        self.assertEqual(meta.media_info(mid)["facts"], {"width": 800, "height": 800})

    def test_check_warns_for_the_ad_sets_placements(self):
        mid = self.store("wide2.png", png(1920, 1080) + b"x")["media_id"]
        d = {"account": {"currency": "EUR"}, "campaigns": [{"id": "c1", "name": "C", "objective": "OUTCOME_TRAFFIC"}],
             "adsets": [{"id": "s1", "name": "S", "campaign_id": "c1", "targeting": IG_STORY}], "ads": [], "audiences": []}
        c = meta._normalise({"type": "meta_new_ad", "adset_id": "s1", "name": "New", "media_id": mid, "primary_text": "Hi\n\nhttps://example.com/",
                             "headline": "H", "link": "https://example.com/"})
        with mock.patch.object(meta, "link_problem", lambda u: ""), mock.patch.object(meta, "page_check", lambda t, u: []):
            probs, warns = meta.check(c, d, {})
        self.assertEqual(probs, [])
        self.assertTrue(any("Stories" in w for w in warns), warns)


class RawUpload(unittest.TestCase):
    def test_stream_is_the_same_file_as_base64(self):
        data = png(1080, 1080) + b"stream"
        a = meta.store_media_stream("s.png", io.BytesIO(data), len(data))
        b = meta.store_media("s.png", data)
        self.assertEqual(a["media_id"], b["media_id"])
        self.assertEqual((a["width"], a["height"]), (1080, 1080))
        self.assertEqual(list(meta.MEDIA_DIR.glob("in-*.part")), [])

    def test_stream_is_read_in_pieces(self):
        data = mp4(1080, 1920, 5) + b"\x00" * 5000
        reads = []

        class S(io.BytesIO):
            def read(self, n=-1):
                reads.append(n)
                return super().read(n)
        with mock.patch.object(meta, "STREAM_PIECE", 1024):
            r = meta.store_media_stream("v.mp4", S(data), len(data))
        self.assertEqual(r["size"], len(data))
        self.assertTrue(all(0 < n <= 1024 for n in reads), reads)
        self.assertEqual(meta.MEDIA_DIR.joinpath(meta.media_info(r["media_id"])["file"]).read_bytes(), data)

    def test_stream_refusals_leave_nothing(self):
        data = png(10, 10)
        for name, body, n, why in (("x.gif", data, len(data), "only"), ("x.png", data, 0, "empty"),
                                   ("x.png", data[:10], len(data), "stopped"), ("x.jpg", data, len(data), "does not look like")):
            with self.assertRaises(ValueError) as cm:
                meta.store_media_stream(name, io.BytesIO(body), n)
            self.assertIn(why, str(cm.exception))
        with mock.patch.dict(meta.MEDIA_MAX, {"image": 5}), self.assertRaises(ValueError):
            meta.store_media_stream("x.png", io.BytesIO(data), len(data))
        self.assertEqual(list(meta.MEDIA_DIR.glob("in-*.part")), [])

    def test_route_takes_raw_bytes_behind_the_guards(self):
        import server  # noqa: PLC0415
        srv = ThreadingHTTPServer(("127.0.0.1", 0), server.H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        port = srv.server_address[1]
        data = png(1080, 1350) + b"route"

        def call(headers):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            c.request("POST", "/api/meta/media/raw?acct=meta&name=r.png", data, {"Host": f"127.0.0.1:{port}", **headers})
            r = c.getresponse()
            try:
                out = (r.status, json.loads(r.read() or b"null"))
            except ConnectionResetError:  # a refused upload closes the connection without reading the body
                out = (r.status, None)
            c.close()
            return out
        st, out = call({"Content-Type": "application/octet-stream", "X-Studio-Press": server.PRESS})
        self.assertEqual(st, 200, out)
        self.assertEqual((out["kind"], out["width"], out["height"]), ("image", 1080, 1350))
        self.assertEqual(call({"Content-Type": "application/octet-stream"})[0], 403)  # no press secret
        full = OSError(28, "No space left on device")
        with mock.patch.object(server.meta, "store_media_stream", mock.Mock(side_effect=full)):
            st, out = call({"Content-Type": "application/octet-stream", "X-Studio-Press": server.PRESS})
        self.assertEqual(st, 507)
        self.assertIn("No space left", (out or {}).get("error", ""))
        self.assertEqual(call({"Content-Type": "application/json", "X-Studio-Press": server.PRESS})[0], 403)


class ThumbnailFresh(unittest.TestCase):
    def test_reused_video_without_thumbnail_is_an_error(self):
        mid = meta.store_media("t.mp4", mp4(1080, 1920, 3) + b"t")["media_id"]
        meta._cur.act = ACT
        self.addCleanup(lambda: setattr(meta._cur, "act", None))
        meta._remember_upload(mid, {"video_id": "v9", "image_url": "https://expired"})

        def once(method, path, params=None, validate=False, token=None):
            if path == "v9":
                return {"status": {"video_status": "ready"}}
            if path == "v9/thumbnails":
                return {"data": []}
            raise AssertionError(path)
        with mock.patch.object(meta_api, "_once", once), self.assertRaises(meta.MetaError):
            meta.upload_media(mid)


# ---------- M16: no answer from Meta ----------

class NoAnswer(fp.FakeGraph):
    """FakeGraph where some calls get no answer at all (timeout / connection reset)."""

    def __init__(self, dead_checks=(), dead_reads=(), **kw):
        super().__init__(**kw)
        self.dead_checks, self.dead_reads = set(dead_checks), set(dead_reads)

    def _handle(self, method, path, params, validate):
        if validate and (path in self.dead_checks or "*" in self.dead_checks):
            raise meta_api._NetworkError("no answer from Meta (timed out)")
        if method == "GET" and ("*" in self.dead_reads or path in self.dead_reads or
                                any(i in self.dead_reads for i in (params.get("ids") or "").split(","))):
            raise meta_api._NetworkError("no answer from Meta (connection reset)")
        return super()._handle(method, path, params, validate)


class Connection(fp.Base):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(meta_api.time, "sleep", lambda s: None)
        p.start()
        self.addCleanup(p.stop)

    def net(self, **kw):
        g = NoAnswer(**kw)
        p = mock.patch.object(meta_api, "_once", g)
        p.start()
        self.addCleanup(p.stop)
        return g

    def test_no_answer_is_one_banner_not_a_problem(self):
        self.net(dead_checks={"*"})
        p = meta.plan(workers=4)
        self.assertFalse(p["ok"])
        self.assertIn("Couldn't reach Meta, nothing was checked", p["connection"])
        self.assertEqual([e["problems"] for e in p["items"] if e["problems"]], [])
        self.assertTrue(all(e["unchecked"] for e in p["items"]))

    def test_dead_live_read_is_a_connection_problem(self):
        self.net(dead_reads={"*"})
        p = meta.plan(workers=4)
        self.assertFalse(p["ok"])
        self.assertTrue(p["connection"])
        self.assertEqual([e["problems"] for e in p["items"] if e["problems"]], [])

    def test_real_refusal_stays_a_problem_next_to_the_banner(self):
        self.net(dead_checks={"a2"}, refuse_check={"a3"})
        p = meta.plan(workers=4)
        bad = {e["id"]: e["problems"] for e in p["items"] if e["problems"]}
        self.assertEqual(bad, {"i2": ["Meta: refused a3"]})
        self.assertTrue(p["connection"])
        self.assertEqual([e["id"] for e in p["items"] if e["unchecked"]], ["i1"])

    def test_push_sends_nothing_and_says_why(self):
        g = self.net(dead_checks={"a1"})
        with mock.patch.object(meta, "refresh_account", lambda: None):
            r = meta.push(accept_spend=True)
        self.assertFalse(r["ok"])
        self.assertIn("Couldn't reach Meta", r["message"])
        self.assertTrue(r["connection"])
        self.assertEqual(g.count("POST"), 0)

    def test_classification(self):
        self.assertTrue(meta._no_answer(meta.MetaError({"message": "no answer from Meta (timed out)"})))
        self.assertTrue(meta._no_answer(meta.MetaError({"message": "Service unavailable"}, status=503)))
        self.assertTrue(meta._no_answer(meta.MetaError({"message": "x", "is_transient": True}, status=400)))
        self.assertFalse(meta._no_answer(meta.MetaError({"message": "User request limit reached", "code": 17}, status=400)))
        self.assertFalse(meta._no_answer(meta.MetaError({"message": "Invalid parameter", "code": 100}, status=400)))
        self.assertFalse(meta._no_answer(ValueError("bad")))


# ---------- M24: landing pages ----------

class Links(unittest.TestCase):
    def setUp(self):
        meta.forget_links()
        self.addCleanup(meta.forget_links)
        self.calls = []
        self.lock = threading.Lock()
        self.mid = meta.store_media("link.png", png(1080, 1080) + b"links")["media_id"]

    def fetch(self, delay=0.0, code=None, text="Price $1155 here"):
        def f(url):
            with self.lock:
                self.calls.append(url)
            time.sleep(delay)
            return {"code": code, "text": None if code else text, "error": f"HTTP {code}" if code else ""}
        return mock.patch.object(meta, "_fetch_link", f)

    def test_one_read_per_url(self):
        with self.fetch():
            self.assertEqual(meta.link_problem("https://example.com/a"), "")
            self.assertEqual(meta.page_check("from $1155", "https://example.com/a"), [])
            self.assertEqual(meta.page_check("from $999", "https://example.com/a"), ['"$999" is not on the linked page - check it is still true'])
        self.assertEqual(self.calls, ["https://example.com/a"])

    def test_dead_link_still_found(self):
        with self.fetch(code=404):
            self.assertIn("answers 404", meta.link_problem("https://example.com/gone"))
            self.assertEqual(meta.page_check("from $5", "https://example.com/gone"), [])

    def test_network_trouble_is_retried_sooner(self):
        def f(url):
            self.calls.append(url)
            return {"code": None, "text": None, "error": "timed out"}
        with mock.patch.object(meta, "_fetch_link", f):
            self.assertEqual(meta.link_problem("https://example.com/n"), "")
            self.assertEqual(meta.link_problem("https://example.com/n"), "")
            self.assertEqual(len(self.calls), 1)
            self.assertTrue(meta.page_check("from $5", "https://example.com/n")[0].startswith("couldn't open the linked page"))
            with mock.patch.object(meta, "LINK_TTL_ERR", 0):
                meta.link_problem("https://example.com/n")
        self.assertEqual(len(self.calls), 2)

    def test_same_url_at_once_shares_one_read(self):
        with self.fetch(delay=0.2):
            ts = [threading.Thread(target=meta.link_problem, args=("https://example.com/same",)) for _ in range(5)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
        self.assertEqual(len(self.calls), 1)

    def test_add_checks_items_side_by_side(self):
        meta._cur.act = ACT
        self.addCleanup(lambda: setattr(meta._cur, "act", None))
        core.clear(meta.acct())
        d = {"account": {"currency": "EUR"}, "campaigns": [], "ads": [], "audiences": [],
             "adsets": [{"id": "s1", "name": "S", "campaign_id": "c1", "targeting": {}}]}
        changes = [{"type": "meta_new_ad", "adset_id": "s1", "name": f"Ad {n}", "media_id": self.mid, "primary_text": "Hi",
                    "headline": "H", "link": f"https://example.com/p{n}"} for n in range(4)]
        with self.fetch(delay=0.3), mock.patch.object(meta, "load", lambda *a, **k: d):
            t0 = time.monotonic()
            got = meta.add(changes)
            wall = time.monotonic() - t0
        self.assertEqual(len(got), 4)
        self.assertEqual(len(self.calls), 4)
        self.assertLess(wall, 0.9, "four landing pages of 0.3 s each were read one after the other")

    def test_validate_reads_pages_outside_the_draft_lock(self):
        meta._cur.act = ACT
        self.addCleanup(lambda: setattr(meta._cur, "act", None))
        core.clear(meta.acct())
        d = {"account": {"currency": "EUR"}, "campaigns": [], "ads": [], "audiences": [],
             "adsets": [{"id": "s1", "name": "S", "campaign_id": "c1", "targeting": {}}]}
        inside = []
        real = core._locked

        @contextlib.contextmanager
        def watched(*a, **k):
            inside.append(True)
            try:
                with real(*a, **k) as x:
                    yield x
            finally:
                inside.pop()
        seen = []

        def f(url):
            seen.append(bool(inside))
            return {"code": None, "text": "", "error": ""}
        with mock.patch.object(meta, "load", lambda *a, **k: d), mock.patch.object(meta, "_fetch_link", f):
            meta.add([{"type": "meta_new_ad", "adset_id": "s1", "name": "Ad", "media_id": self.mid, "primary_text": "Hi",
                       "headline": "H", "link": "https://example.com/v"}])
            meta.forget_links()
            with mock.patch.object(core, "_locked", watched):
                res = meta.validate()
        self.assertEqual(len(res), 1)
        self.assertEqual(seen, [False, False])
        core.clear(meta.acct())


# ---------- M25: undo reads first ----------

def push_items(*changes):
    return [{"id": f"u{n}", "change": meta._normalise(c), "by": "you", "summary": f"undo test {n}", "problems": []}
            for n, c in enumerate(changes)]


class Undo(fp.Base):
    def setUp(self):
        super().setUp()
        self.items[:] = push_items({"type": "meta_budget", "level": "adset", "id": "s1", "budget": 6},
                                   {"type": "meta_status", "level": "ad", "id": "a1", "status": "paused"},
                                   {"type": "meta_rename", "level": "campaign", "id": "c1", "new_name": "Renamed"},
                                   {"type": "meta_copy", "level": "ad", "id": "a7", "primary_text": "New text"})
        for p in (mock.patch.object(meta, "refresh_account", lambda: None), mock.patch.object(meta_api.time, "sleep", lambda s: None)):
            p.start()
            self.addCleanup(p.stop)

    def pushed(self):
        g = self.graph()
        r = meta.push(accept_spend=True)
        self.assertTrue(r["ok"], r)
        return g, r["push_id"]

    def test_fields_changed_since_are_left_alone(self):
        g, pid = self.pushed()
        g.objs["s1"]["daily_budget"] = "900"  # changed in Ads Manager after the push
        g.objs["a7"]["creative"] = {"id": "cr-other"}  # pointed at another creative in Ads Manager
        n = len(g.calls)
        u = meta.undo(pid, accept_spend=True)
        self.assertTrue(u["ok"], u)
        self.assertEqual(len(u["skipped"]), 2, u)
        self.assertTrue(any("daily_budget was changed after the push" in x for x in u["skipped"]))
        self.assertTrue(any("another creative" in x for x in u["skipped"]))
        self.assertIn("Left as they are", u["message"])
        posts = [c[1] for c in g.calls[n:] if c[0] == "POST"]
        self.assertEqual(sorted(posts), ["a1", "c1"])
        self.assertEqual(g.objs["s1"]["daily_budget"], "900")
        self.assertEqual(g.objs["a1"]["status"], "ACTIVE")
        self.assertEqual(g.objs["c1"]["name"], "Camp 1")
        self.assertEqual(g.objs["a7"]["creative"], {"id": "cr-other"})

    def test_untouched_push_is_undone_in_full(self):
        g, pid = self.pushed()
        u = meta.undo(pid, accept_spend=True)
        self.assertTrue(u["ok"], u)
        self.assertEqual(u["skipped"], [])
        self.assertEqual(g.objs["s1"]["daily_budget"], 1000)
        self.assertEqual(g.objs["a7"]["creative"], {"creative_id": "cr7"})

    def test_unreadable_object_is_not_written(self):
        g, pid = self.pushed()
        g.objs.pop("c1")  # gone (or unreadable): never written back blindly
        u = meta.undo(pid, accept_spend=True)
        self.assertFalse(u["ok"])
        self.assertTrue(any("couldn't read it first" in x for x in u["not_undone"]), u)
        self.assertEqual(g.objs["a1"]["status"], "ACTIVE")

    def test_old_log_without_pushed_values_is_written_as_before(self):
        g, pid = self.pushed()
        log = meta._log()
        recs = [json.loads(x) for x in log.read_text().splitlines() if x.strip()]
        for r in recs:
            for o in r.get("ops") or []:
                o.pop("pushed", None)
        log.write_text("".join(json.dumps(r) + "\n" for r in recs))
        g.objs["s1"]["daily_budget"] = "900"
        u = meta.undo(pid, accept_spend=True)
        self.assertTrue(u["ok"], u)
        self.assertEqual(g.objs["s1"]["daily_budget"], 1000)

    def test_same_values(self):
        self.assertTrue(meta._same("600", 600))
        self.assertTrue(meta._same("2026-12-01T23:59:00+0000", "2026-12-01T23:59:00+00:00"))
        self.assertTrue(meta._same(None, 0))  # no end date
        self.assertFalse(meta._same("2026-12-02T23:59:00+0000", "2026-12-01T23:59:00+0000"))
        tg = {"geo_locations": {"countries": ["GB", "DE"]}, "age_min": 25, "custom_audiences": [{"id": "w1"}]}
        live = {"geo_locations": {"countries": ["DE", "GB"], "location_types": ["home"]}, "age_min": 25,
                "custom_audiences": [{"id": "w1", "name": "Web"}], "targeting_automation": {"advantage_audience": 0}}
        self.assertTrue(meta._same(live, tg))
        self.assertFalse(meta._same({**live, "age_min": 30}, tg))
        self.assertFalse(meta._same({**live, "custom_audiences": []}, tg))


class VerifierFixes(unittest.TestCase):
    """Second round (independent verifier): broken headers, keys added in Ads Manager, undo counts, re-check."""

    def test_empty_or_cut_tkhd_is_size_unknown(self):
        self.assertEqual(meta._moov_facts(b"\x00\x00\x00\x10trak" + b"\x00\x00\x00\x08tkhd"), {})
        self.assertEqual(meta._moov_facts(b"\x00\x00\x00\x14trak" + b"\x00\x00\x00\x0ctkhd\x00\x00\x00\x00"), {})
        bad = b"\x00\x00\x00\x08ftyp" + b"\x00\x00\x00\x18moov" + b"\x00\x00\x00\x10trak" + b"\x00\x00\x00\x08tkhd"
        r = meta.store_media("cut.mp4", bad)
        self.assertNotIn("width", r)
        self.assertIn(r["media_id"], meta._media_index())  # stored and indexed, not left behind

    def test_facts_reader_never_raises(self):
        with mock.patch.object(meta, "_video_facts", mock.Mock(side_effect=IndexError("boom"))):
            r = meta.store_media("boom.mp4", mp4(1080, 1920, 3) + b"boom")
        self.assertIn(r["media_id"], meta._media_index())
        with mock.patch.object(meta, "_jpeg_size", mock.Mock(side_effect=IndexError("boom"))):
            self.assertEqual(meta._facts_of(meta.MEDIA_DIR / meta.media_info(r["media_id"])["file"], "image"), {})

    def test_old_entry_with_broken_header_does_not_break_the_draft(self):
        meta._cur.act = ACT
        self.addCleanup(lambda: setattr(meta._cur, "act", None))
        core.clear(meta.acct())
        bad = b"\x00\x00\x00\x08ftyp" + b"\x00\x00\x00\x18moov" + b"\x00\x00\x00\x10trak" + b"\x00\x00\x00\x08tkhd" + b"old"
        mid = meta.store_media("old.mp4", bad)["media_id"]
        meta._media_save(lambda idx: idx[mid].pop("facts"))  # stored before facts were read
        d = {"account": {"currency": "EUR"}, "campaigns": [], "ads": [], "audiences": [],
             "adsets": [{"id": "s1", "name": "S", "campaign_id": "c1", "targeting": IG_STORY}]}
        with mock.patch.object(meta, "load", lambda *a, **k: d), mock.patch.object(meta, "link_problem", lambda u: ""), \
                mock.patch.object(meta, "page_check", lambda t, u: []):
            got = meta.add([{"type": "meta_new_ad", "adset_id": "s1", "name": "Ad", "media_id": mid, "primary_text": "Hi",
                             "headline": "H", "link": "https://example.com/o"}])
            self.assertEqual(got[0]["problems"], [])
            self.assertTrue(any("couldn't read" in w for w in got[0]["warnings"]), got[0]["warnings"])
            self.assertEqual(len(meta.validate()), 1)
        core.clear(meta.acct())

    def test_keys_added_in_ads_manager_count_as_changed(self):
        pushed = {"geo_locations": {"countries": ["GB"]}, "age_min": 25, "custom_audiences": [{"id": "w1"}]}
        meta_added = {"geo_locations": {"countries": ["GB"], "location_types": ["home", "recent"]}, "age_min": 25,
                      "custom_audiences": [{"id": "w1", "name": "Web"}], "targeting_automation": {"advantage_audience": 0},
                      "brand_safety_content_filter_levels": ["FACEBOOK_STANDARD"]}
        self.assertTrue(meta._same(meta_added, pushed))
        city = json.loads(json.dumps(meta_added))
        city["geo_locations"]["cities"] = [{"key": "2421836", "radius": 10, "distance_unit": "mile"}]
        self.assertFalse(meta._same(city, pushed))
        excl = {**meta_added, "excluded_custom_audiences": [{"id": "w9", "name": "Buyers"}]}
        self.assertFalse(meta._same(excl, pushed))
        self.assertTrue(meta._same({**meta_added, "excluded_custom_audiences": []}, pushed))

    def test_recheck_and_refresh_read_pages_again(self):
        calls = []
        with mock.patch.object(meta, "_fetch_link", lambda u: calls.append(u) or {"code": None, "text": "", "error": ""}):
            meta.forget_links()
            meta.link_problem("https://example.com/r")
            meta.link_problem("https://example.com/r")
            self.assertEqual(len(calls), 1)
            meta._cur.act = ACT
            self.addCleanup(lambda: setattr(meta._cur, "act", None))
            core.clear(meta.acct())
            with mock.patch.object(meta, "load", lambda *a, **k: {"campaigns": [], "adsets": [], "ads": [], "audiences": []}):
                meta.validate()
            meta.link_problem("https://example.com/r")
        self.assertEqual(len(calls), 2)


class UndoCounts(Undo):
    def test_wholly_skipped_change_is_not_counted_as_reversed(self):
        g, pid = self.pushed()
        g.objs["s1"]["daily_budget"] = "900"
        u = meta.undo(pid, accept_spend=True)
        self.assertTrue(u["ok"], u)
        self.assertTrue(u["message"].startswith("Undone: 3 change(s) reversed."), u["message"])

    def test_city_added_in_ads_manager_is_kept(self):
        self.items[:] = push_items({"type": "meta_targeting", "level": "adset", "id": "s2", "countries": ["GB", "DE"]})
        d = {"account": {"currency": "EUR"}, "audiences": [], "ads": [],
             "campaigns": [{"id": "c1", "name": "Camp 1", "objective": "OUTCOME_TRAFFIC"}],
             "adsets": [{"id": "s2", "name": "Set 2", "campaign_id": "c1", "targeting": {"geo_locations": {"countries": ["GB"]}}}]}
        p = mock.patch.object(meta, "load", lambda *a, **k: d)
        p.start()
        self.addCleanup(p.stop)
        g, pid = self.pushed()
        g.objs["s2"]["targeting"]["geo_locations"]["cities"] = [{"key": "2421836"}]
        g.objs["s2"]["targeting"]["excluded_custom_audiences"] = [{"id": "w9"}]
        u = meta.undo(pid, accept_spend=True)
        self.assertTrue(any("targeting was changed after the push" in x for x in u["skipped"]), u)
        self.assertEqual(g.objs["s2"]["targeting"]["geo_locations"]["cities"], [{"key": "2421836"}])
        self.assertEqual(g.objs["s2"]["targeting"]["geo_locations"]["countries"], ["GB", "DE"])

    def test_preview_of_uploaded_video_gets_a_fresh_thumbnail(self):
        mid = meta.store_media("pv.mp4", mp4(1080, 1920, 4) + b"pv")["media_id"]
        meta._remember_upload(mid, {"video_id": "v5", "image_url": "https://expired"})
        self.items[:] = push_items({"type": "meta_new_ad", "adset_id": "s1", "name": "Vid", "media_id": mid, "primary_text": "Hi",
                                    "headline": "H", "link": "https://example.com/"})
        g = self.graph()
        g.objs["v5/thumbnails"] = {"data": [{"uri": "https://fresh/v5.jpg", "is_preferred": True}]}
        p = meta.plan()
        step = next(s for s in p["steps"] if s.get("kind") == "new_ad")
        self.assertEqual(step["creative"]["object_story_spec"]["video_data"]["image_url"], "https://fresh/v5.jpg")
        with mock.patch.object(meta, "_thumb_url", mock.Mock(side_effect=meta.MetaError({"message": "no answer from Meta"}))):
            p = meta.plan()
        step = next(s for s in p["steps"] if s.get("kind") == "new_ad")
        self.assertEqual(step["creative"]["object_story_spec"]["video_data"]["image_url"], "https://expired")


if __name__ == "__main__":
    unittest.main()
