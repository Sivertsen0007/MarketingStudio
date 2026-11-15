"""SoMe area tests. Plain unittest, from the studio folder:  python3 -m unittest tests/test_social.py
Nothing is published, scheduled or deleted anywhere: every publisher here is a FakePublisher or a patched fake.
SOCIAL_REAL=1 also runs the read-only Meta tests (page posts + IG media read, and Meta's validate_only check of a
boost creative, which creates nothing).
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("MSTUDIO_HOME", _tf.mkdtemp(prefix="mstudio-test-"))
_os.environ["MSTUDIO_KEYCHAIN"] = "0"

import datetime as dt
import io
import json
import os
import struct
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
TMP = tempfile.mkdtemp(prefix="social-test-")
os.environ["SOCIAL_DIR"] = TMP
DP_DIR = tempfile.mkdtemp(prefix="social-dp-")
os.environ["STUDIO_DATA_DIR"] = DP_DIR  # datapolicy's settings + status file: never the real data folder
os.environ["SOCIAL_FAKE_PUBLISH"] = "1"

from areas import _social_checks as checks  # noqa: E402
from areas import _social_publish as pub  # noqa: E402
from areas import _social_store as store  # noqa: E402
from areas import social  # noqa: E402
import social_publisher  # noqa: E402
import _legacy_copy_rules  # noqa: E402
import team_settings  # noqa: E402
team_settings.local_set("copy_rules", {"rules": _legacy_copy_rules.RULES})  # the rules these tests check (a new install has none)

REAL = os.environ.get("SOCIAL_REAL") == "1"


def png(w, h):
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", w, h) + b"\x08\x02\x00\x00\x00" + b"\x00" * 20


def jpg(w, h):
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 17, 8, h, w, 3) + b"\x00" * 9
    return b"\xff\xd8" + b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9 + sof + b"\xff\xd9"


def mp4(seconds, scale=1000):
    mvhd = b"mvhd" + b"\x00\x00\x00\x00" + b"\x00" * 8 + struct.pack(">II", scale, int(seconds * scale))
    return b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 8 + struct.pack(">I", len(mvhd) + 4) + mvhd


def write(name, data):
    p = Path(TMP) / name
    p.write_bytes(data)
    return p


def fresh():
    for f in ("social_draft.json", "social_queue.json", "social_settings.json"):
        (Path(TMP) / f).unlink(missing_ok=True)


class Checks(unittest.TestCase):
    def post(self, **kw):
        return {**store.blank(), "channels": ["facebook"], "caption": "Seven nights in the Red Sea.", **kw}

    def run_check(self, p, facts=None, links=None):
        return checks.check_post(p, store.caption_for, facts, links)

    def test_copy_rules_block(self):
        for bad, why in (("Dive with us \U0001F42C", "emoji"), ("Calm seas \u2014 great dives", "em dash"),
                         ("Like the Bahamas, only better", "Bahamas"), ("Mantas guaranteed every trip", "guarantee"),
                         ("You will see whale sharks", "you will see")):
            r = self.run_check(self.post(caption=bad))
            self.assertFalse(r["ok"], why)

    def test_copy_rules_warn(self):
        r = self.run_check(self.post(caption="A luxury yacht. From $1155 per cabin."))
        self.assertTrue(r["ok"])
        self.assertTrue(any("vessel" in w for w in r["warnings"]))
        self.assertTrue(any("spaces" in w for w in r["warnings"]))

    def test_first_comment_and_alt_checked(self):
        r = self.run_check(self.post(channels=["instagram"], media=[{"media_id": "x", "kind": "image"}], first_comment="Bahamas next",
                                     alt_text="A diver \u2014 smiling"), facts=[{"kind": "image", "ext": "jpg", "size": 1000, "width": 1080, "height": 1350}])
        self.assertEqual(len([p for p in r["problems"] if "Bahamas" in p or "em dash" in p]), 2)

    def test_limits(self):
        r = self.run_check(self.post(channels=["instagram", "facebook"], caption="a" * 2300, media=[{"media_id": "x", "kind": "image"}]),
                           facts=[{"kind": "image", "ext": "jpg", "size": 1000, "width": 1080, "height": 1080}])
        self.assertTrue(any("Instagram: caption is 2300" in p for p in r["problems"]))
        self.assertFalse(any(p.startswith("Facebook: caption") for p in r["problems"]))
        tags = " ".join(f"#t{i}" for i in range(31))
        r = self.run_check(self.post(channels=["instagram"], hashtags=tags, media=[{"media_id": "x", "kind": "image"}]),
                           facts=[{"kind": "image", "ext": "jpg", "size": 1000, "width": 1080, "height": 1080}])
        self.assertTrue(any("31 hashtags" in p for p in r["problems"]))

    def test_instagram_needs_media_and_rules(self):
        r = self.run_check(self.post(channels=["instagram"]), facts=[])
        self.assertTrue(any("Instagram has no text-only posts" in p for p in r["problems"]))
        f = [{"kind": "image", "ext": "png", "size": 9 * 1048576, "width": 1080, "height": 2000}]
        p, w = checks.media_problems("instagram", "image", f)
        self.assertTrue(any("JPEG" in x for x in p))
        self.assertTrue(any("8 MB" in x for x in p))
        self.assertTrue(any("4:5 to 1.91:1" in x for x in p))
        self.assertEqual(checks.media_problems("instagram", "image", [{"kind": "image", "ext": "jpg", "size": 1, "width": 1080, "height": 1350}])[0], [])
        many = [{"kind": "image", "ext": "jpg", "size": 1, "width": 1080, "height": 1080}] * 11
        self.assertTrue(any("2 to 10" in x for x in checks.media_problems("facebook", "carousel", many)[0]))
        mix = [{"kind": "image", "ext": "jpg", "size": 1, "width": 1, "height": 1}, {"kind": "video", "ext": "mp4", "size": 1, "seconds": 10}]
        self.assertTrue(any("all be images" in x for x in checks.media_problems("facebook", "carousel", mix)[0]))
        self.assertTrue(any("3 s to 15 min" in x for x in checks.media_problems("instagram", "reel", [{"kind": "video", "ext": "mp4", "size": 1, "seconds": 2}])[0]))

    def test_links_and_consent(self):
        r = self.run_check(self.post(link="https://example.com/dead", guest_photos=True), links=lambda u: f"link {u} answers 404")
        self.assertTrue(any("404" in p for p in r["problems"]))
        self.assertTrue(any("consent" in p for p in r["problems"]))
        r = self.run_check(self.post(guest_photos=True, consent_confirmed=True), links=lambda u: "")
        self.assertTrue(r["ok"])

    def test_media_facts(self):
        self.assertEqual(checks.image_size(png(1080, 1350)), (1080, 1350))
        self.assertEqual(checks.image_size(jpg(1440, 900)), (1440, 900))
        self.assertEqual(checks.video_seconds(mp4(12.5)), 12.5)
        f = checks.media_facts(write("a.jpg", jpg(1080, 1080)), "image", "jpg")
        self.assertEqual((f["width"], f["height"]), (1080, 1080))

    def test_schedule_route(self):
        now = store.now()
        self.assertEqual(checks.schedule_route("facebook", now + dt.timedelta(hours=2), now, False), ("meta", []))
        self.assertIsNone(checks.schedule_route("facebook", now + dt.timedelta(minutes=5), now, False)[0])
        self.assertIsNone(checks.schedule_route("facebook", now + dt.timedelta(days=40), now, False)[0])
        self.assertEqual(checks.schedule_route("facebook", now + dt.timedelta(days=40), now, True)[0], "queue")
        r, p = checks.schedule_route("instagram", now + dt.timedelta(hours=2), now, False)
        self.assertIsNone(r)
        self.assertIn("Studio's scheduler", p[0])
        self.assertEqual(checks.schedule_route("linkedin", now + dt.timedelta(hours=2), now, True)[0], "queue")
        self.assertIsNone(checks.schedule_route("instagram", now - dt.timedelta(hours=1), now, True)[0])


class Store(unittest.TestCase):
    def setUp(self):
        fresh()

    def test_save_status_caption(self):
        p = store.save({"caption": "Hello", "hashtags": "liveaboard, #diving", "channels": ["instagram", "bogus"]})
        self.assertEqual(p["channels"], ["instagram"])
        self.assertEqual(store.caption_for(p, "instagram"), "Hello\n\n#liveaboard #diving")
        self.assertEqual(store.status(p), "draft")
        p = store.update(p["id"], lambda x: x["results"].__setitem__("instagram", {"status": "scheduled"}))
        self.assertEqual(store.status(p), "scheduled")
        with self.assertRaises(ValueError):
            store.remove(p["id"])

    def test_queue_due(self):
        now = store.now()
        items = [{"id": "a", "status": "scheduled", "due": store.iso(now - dt.timedelta(minutes=5))},
                 {"id": "b", "status": "scheduled", "due": store.iso(now + dt.timedelta(minutes=5))},
                 {"id": "c", "status": "scheduled", "due": store.iso(now - dt.timedelta(hours=5))},
                 {"id": "d", "status": "cancelled", "due": store.iso(now - dt.timedelta(minutes=5))},
                 {"id": "e", "status": "published", "due": store.iso(now - dt.timedelta(minutes=5))}]
        ready, missed = store.due(items, now, 120)
        self.assertEqual([x["id"] for x in ready], ["a"])
        self.assertEqual([x["id"] for x in missed], ["c"])

    def test_queue_add_replaces_and_cancel(self):
        a = store.queue_add({"post_id": "p", "channel": "instagram", "due": "2030-01-01T09:00:00+00:00", "payload": {}})
        b = store.queue_add({"post_id": "p", "channel": "instagram", "due": "2030-01-02T09:00:00+00:00", "payload": {}})
        self.assertEqual([x["id"] for x in store.queue()], [b["id"]])
        self.assertEqual(store.queue_cancel("p", "instagram"), {"cancelled": 1, "busy": []})
        self.assertEqual(store.queue()[0]["status"], "cancelled")
        self.assertNotEqual(a["id"], b["id"])


class PublisherJob(unittest.TestCase):
    def setUp(self):
        fresh()
        now = store.now()
        self.due = store.queue_add({"post_id": "p1", "channel": "instagram", "due": store.iso(now - dt.timedelta(minutes=1)),
                                    "payload": {"channel": "instagram", "text": "x", "media": []}})
        self.later = store.queue_add({"post_id": "p2", "channel": "linkedin", "due": store.iso(now + dt.timedelta(hours=1)),
                                      "payload": {"channel": "linkedin", "text": "y", "media": []}})
        self.late = store.queue_add({"post_id": "p3", "channel": "facebook", "due": store.iso(now - dt.timedelta(hours=6)),
                                     "payload": {"channel": "facebook", "text": "z", "media": []}})
        self.fakes = {c: pub.FakePublisher(c) for c in store.CHANNELS}

    def st(self, it):
        return next(x for x in store.queue() if x["id"] == it["id"])

    def test_dry_run_changes_nothing(self):
        r = social_publisher.run_once(store, pub, self.fakes, live=False)
        self.assertEqual(r["would"], [self.due["id"]])
        self.assertEqual(self.st(self.due)["status"], "scheduled")
        self.assertEqual(self.st(self.late)["status"], "scheduled")
        self.assertFalse(any(f.calls for f in self.fakes.values()))

    def test_live_with_fakes(self):
        r = social_publisher.run_once(store, pub, self.fakes, live=True)
        self.assertEqual(r["published"], [self.due["id"]])
        self.assertEqual(self.st(self.due)["status"], "published")
        self.assertTrue(self.st(self.due)["result"]["post_id"].startswith("fake-instagram"))
        self.assertEqual(self.st(self.later)["status"], "scheduled")
        self.assertEqual(self.st(self.late)["status"], "missed")
        self.assertEqual(self.fakes["facebook"].calls, [])  # missed is never posted late
        r = social_publisher.run_once(store, pub, self.fakes, live=True)  # second run: nothing again
        self.assertEqual(r["published"], [])

    def test_failure_marked(self):
        self.fakes["instagram"].fail = "publish"
        r = social_publisher.run_once(store, pub, self.fakes, live=True)
        self.assertEqual(r["failed"], [self.due["id"]])
        self.assertEqual(self.st(self.due)["status"], "failed")

    def test_lock(self):
        import fcntl
        with open(Path(TMP) / "social_publisher.lock", "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            done = {}
            t = threading.Thread(target=lambda: done.update(social_publisher.run_once(store, pub, self.fakes, live=True)))
            t.start()
            t.join(5)
            fcntl.flock(lk, fcntl.LOCK_UN)
        self.assertTrue(done["busy"])
        self.assertEqual(self.st(self.due)["status"], "scheduled")

    def test_cli_add_cancel_status(self):
        item = {"id": "zz1", "post_id": "p9", "channel": "facebook", "due": "2030-01-01T09:00:00+00:00", "payload": {"text": "t"}}
        with mock.patch("sys.stdin", io.StringIO(json.dumps(item))):
            self.assertEqual(social_publisher.main(["--data", TMP, "--add", "-"]), 0)
        self.assertEqual(self.st(item)["status"], "scheduled")
        social_publisher.main(["--data", TMP, "--cancel", "zz1"])
        self.assertEqual(self.st(item)["status"], "cancelled")
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            social_publisher.main(["--data", TMP, "--status"])
        self.assertNotIn("payload", out.getvalue().splitlines()[-1])
        self.assertEqual(social_publisher.main(["--data", TMP]), 0)  # default dry-run
        self.assertEqual(self.st(self.due)["status"], "scheduled")


class Calendar(unittest.TestCase):
    def test_events(self):
        t0 = store.parse_when("2026-11-12T09:00:00+00:00")
        posts = [{**store.blank(), "id": "a", "channels": ["facebook", "instagram"], "when": store.iso(t0), "caption": "A",
                  "results": {"facebook": {"status": "published", "post_id": "fb_1", "url": "u"}}},
                 {**store.blank(), "id": "b", "channels": ["facebook"], "when": store.iso(t0), "caption": "B",
                  "move": {"when": "2026-11-14T10:00:00+00:00"}},
                 {**store.blank(), "id": "c", "channels": ["instagram"], "when": "", "caption": "C"}]
        live = [{"id": "fb_1", "channel": "facebook", "when": "2026-11-12T09:00:05+00:00", "text": "A"},
                {"id": "ig_9", "channel": "instagram", "when": "2026-11-13T08:00:00+00:00", "text": "Old"}]
        sched = [{"id": "fb_s", "channel": "facebook", "when": "2026-11-20T08:00:00+00:00", "text": "S"}]
        s, e = store.parse_when("2026-11-01T00:00:00+00:00"), store.parse_when("2026-12-01T00:00:00+00:00")
        out = social.calendar_events(posts, live, sched, s, e)
        keys = {x["key"]: x for x in out["events"]}
        self.assertEqual(keys["a:facebook"]["status"], "published")
        self.assertEqual(keys["a:instagram"]["status"], "draft")
        self.assertNotIn("live:fb_1", keys)  # the Studio post already shows it
        self.assertIn("live:ig_9", keys)
        self.assertEqual(keys["live:fb_s"]["status"], "scheduled")
        self.assertTrue(keys["b:facebook"]["moved"])
        self.assertTrue(keys["b:facebook"]["when"].startswith("2026-11-14T10:00"))
        self.assertEqual([u["id"] for u in out["unplanned"]], ["c"])


class Buttons(unittest.TestCase):
    """Post now / Schedule / Cancel / Delete through social.py with FakePublishers (SOCIAL_FAKE_PUBLISH=1)."""

    def setUp(self):
        fresh()
        social._fakes.clear()
        pub.FakePublisher.live.clear()
        social._fake_host.put_calls.clear()
        social._shipper = None
        self.img = write("pic.jpg", jpg(1080, 1350))
        self.ml = mock.patch.object(social, "media_lookup", lambda mid: (self.img, "jpg", "image"))
        self.ml.start()
        self.lc = mock.patch.object(social, "link_check", lambda u: "")
        self.lc.start()

    def tearDown(self):
        self.ml.stop()
        self.lc.stop()
        social._shipper = None

    def new(self, **kw):
        return store.save({"caption": "Seven nights on the Red Sea reefs.", "channels": ["facebook", "instagram"],
                           "media": [{"media_id": "m1", "kind": "image"}], "alt_text": "Reef", **kw})

    def test_needs_press(self):
        p = self.new()
        with self.assertRaises(PermissionError):
            social.post_now({"id": p["id"]})

    def test_post_now(self):
        p = self.new()
        r = social.post_now({"id": p["id"], "pressed": "user"})
        self.assertTrue(r["ok"], r)
        self.assertEqual(set(r["results"]), {"facebook", "instagram"})
        self.assertEqual(r["post"]["status"], "published")
        self.assertEqual(social._fake_host.put_calls[0][1], "social/m1.jpg")  # IG needs a public URL, made at the press
        import progress
        pr = progress.get("social")
        self.assertEqual([x["state"] for x in pr["items"]], ["done", "done"])
        with self.assertRaises(ValueError):  # nothing left to post
            social.post_now({"id": p["id"], "pressed": "user"})

    def test_post_now_blocked_by_checks(self):
        p = self.new(caption="Guaranteed mantas")
        r = social.post_now({"id": p["id"], "pressed": "user"})
        self.assertFalse(r["ok"])
        self.assertFalse(social._fakes)  # no publisher touched

    def test_one_channel_fails(self):
        p = self.new()
        social.publishers()["instagram"].fail = "publish"
        r = social.post_now({"id": p["id"], "pressed": "user"})
        self.assertFalse(r["ok"])
        self.assertEqual(r["results"]["facebook"]["status"], "published")
        self.assertEqual(r["results"]["instagram"]["status"], "failed")
        self.assertEqual(r["post"]["status"], "failed")

    def test_retry_finds_live_post_and_does_not_repost(self):
        p = self.new(channels=["facebook"])
        social.publishers()["facebook"].fail = "publish_timeout"  # it went out, but the answer was lost
        r = social.post_now({"id": p["id"], "pressed": "user"})
        self.assertEqual(r["results"]["facebook"]["status"], "failed")
        fb = social._fakes["facebook"]
        fb.fail = None
        n = len([c for c in fb.calls if c[0] == "publish"])
        r = social.post_now({"id": p["id"], "pressed": "user"})
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["results"]["facebook"]["via"], "found")
        self.assertEqual(len([c for c in fb.calls if c[0] == "publish"]), n)  # looked up, not posted again
        self.assertEqual(store.get(p["id"])["results"]["facebook"]["status"], "published")

    def test_retry_unsure_needs_confirm(self):
        p = self.new(channels=["facebook"], caption="A different caption for the unsure case.")
        store.update(p["id"], lambda x: x["results"].__setitem__("facebook", {"status": "failed", "at": store.iso(store.now()),
                     "error": "no answer from Meta (timed out); the change may or may not have been applied"}))
        fb = social.publishers()["facebook"]
        r = social.post_now({"id": p["id"], "pressed": "user"})  # lookup finds nothing, but the error says it may be live
        self.assertFalse(r["ok"])
        self.assertEqual(r["needs_confirm"], ["facebook"])
        self.assertFalse([c for c in fb.calls if c[0] == "publish"])
        fb.fail = "find"  # lookup itself fails: also unsure
        store.update(p["id"], lambda x: x["results"]["facebook"].__setitem__("error", "Meta: some other refusal"))
        self.assertEqual(social.post_now({"id": p["id"], "pressed": "user"})["needs_confirm"], ["facebook"])
        fb.fail = None
        r = social.post_now({"id": p["id"], "pressed": "user", "confirm_repost": True})
        self.assertTrue(r["ok"], r)
        self.assertEqual(len([c for c in fb.calls if c[0] == "publish"]), 1)

    def test_retry_after_plain_refusal_posts(self):
        p = self.new(channels=["facebook"], caption="Third caption, refused before it went out.")
        social.publishers()["facebook"].fail = "publish"  # refused: never went out
        social.post_now({"id": p["id"], "pressed": "user"})
        social._fakes["facebook"].fail = None
        r = social.post_now({"id": p["id"], "pressed": "user"})  # lookup finds nothing and the error was a clear no
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["results"]["facebook"]["status"], "published")

    def test_media_marked_social(self):
        import meta_ads
        d = Path(TMP) / "meta_media"
        with mock.patch.object(meta_ads, "MEDIA_DIR", d):
            import base64
            r = social.media_upload({"filename": "x.jpg", "data": base64.b64encode(jpg(1080, 1080)).decode()})
            self.assertEqual(meta_ads._media_index()[r["media_id"]]["source"], "social")
            self.assertEqual(meta_ads.list_media(), [])  # the Ads media list leaves social files out
            self.assertEqual([m["media_id"] for m in social.get_media({})], [r["media_id"]])

    def test_schedule_fb_meta_ig_blocked(self):
        when = store.iso(store.now() + dt.timedelta(hours=3))
        p = self.new(when=when)
        r = social.schedule({"id": p["id"], "pressed": "user"})
        self.assertFalse(r["ok"])
        self.assertTrue(r["can_fb_only"])
        self.assertTrue(any("Studio's scheduler" in x for x in r["problems"]))
        r = social.schedule({"id": p["id"], "pressed": "user", "channels": ["facebook"]})
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["results"]["facebook"]["via"], "meta")
        self.assertEqual(social._fakes["facebook"].calls[0][0], "schedule")
        self.assertEqual(store.queue(), [])  # nothing for the VPS
        r = social.cancel({"id": p["id"], "pressed": "user"})
        self.assertTrue(r["ok"])
        self.assertEqual(store.get(p["id"])["results"]["facebook"]["status"], "cancelled")

    def test_vps_runner_parked(self):
        with self.assertRaises(ValueError) as e:
            social.save_settings({"vps_job": True})
        self.assertIn("parked", str(e.exception))
        social._shipper = object()  # even a shipper hook and a hand-edited settings file can't bring it back
        with store._locked("social_settings.json", {}) as st:
            st.update(vps_job=True, vps_host="aihub@vps", vps_dir="/x")
        self.assertIsNone(social.queue_mode())
        with self.assertRaises(RuntimeError):
            social.shipper()
        p = self.new(when=store.iso(store.now() + dt.timedelta(hours=3)), channels=["instagram"])
        r = social.schedule({"id": p["id"], "pressed": "user"})
        self.assertFalse(r["ok"])  # Instagram: only the Mac scheduler can take it, and it is off
        self.assertEqual(store.queue(), [])
        social.save_settings({"local_scheduler": True})
        r = social.schedule({"id": p["id"], "pressed": "user"})
        self.assertTrue(r["ok"], r)
        self.assertEqual([x["runner"] for x in store.queue()], ["local"])  # never "vps"
        self.assertFalse(social.get_state({})["vps_installed"])

    def test_move_and_confirm(self):
        p = self.new(channels=["facebook"], when=store.iso(store.now() + dt.timedelta(hours=3)))
        social.schedule({"id": p["id"], "pressed": "user"})
        nw = store.now() + dt.timedelta(days=2)
        social.move({"id": p["id"], "when": store.iso(nw)})
        self.assertTrue(store.get(p["id"])["move"])
        self.assertFalse(any(c[0] == "reschedule" for c in social._fakes["facebook"].calls))  # a drag alone changes nothing
        r = social.confirm_move({"id": p["id"], "pressed": "user"})
        self.assertTrue(r["ok"], r)
        self.assertTrue(any(c[0] == "reschedule" for c in social._fakes["facebook"].calls))
        self.assertIsNone(store.get(p["id"])["move"])

    def test_delete_live(self):
        p = self.new(channels=["facebook"])
        social.post_now({"id": p["id"], "pressed": "user"})
        r = social.delete_post({"id": p["id"], "pressed": "user", "channels": ["facebook"]})
        self.assertTrue(r["ok"])
        self.assertEqual(store.get(p["id"])["results"]["facebook"]["status"], "deleted")

    def test_claude_drafts_only(self):
        d = social.add_draft({"caption": "Draft by Claude", "channels": ["facebook"]})
        self.assertEqual(d["status"], "draft")
        self.assertIn(d["id"], [x["id"] for x in social.list_drafts()])
        store.update(d["id"], lambda p: p["results"].__setitem__("facebook", {"status": "scheduled"}))
        with self.assertRaises(ValueError):
            social.add_draft({"id": d["id"], "caption": "changed"})
        self.assertFalse(hasattr(social, "mcp_post"))


class FakeMeta:
    """Stands in for the Graph API in _social_meta (records calls, never sends)."""

    def __init__(self):
        self.calls, self.n = [], 0

    def post(self, path, params):
        self.calls.append(("POST", path, params))
        self.n += 1
        return {"id": f"c{self.n}"}

    def get(self, path, **params):
        self.calls.append(("GET", path, params))
        if path.endswith("content_publishing_limit"):
            return {"data": [{"quota_usage": 3, "config": {"quota_total": 100}}]}
        if params.get("fields", "").startswith("status_code"):
            return {"status_code": "FINISHED"}
        return {"permalink": "https://www.instagram.com/p/x/", "permalink_url": "https://www.facebook.com/x"}


class MetaPublishersFaked(unittest.TestCase):
    def setUp(self):
        from areas import _social_meta as sm
        self.sm = sm
        self.f = FakeMeta()
        self.p = [mock.patch.object(sm, "_post", self.f.post), mock.patch.object(sm, "_get", self.f.get),
                  mock.patch.object(sm, "page", lambda force=False: {"id": "703318833095953", "ig_id": "1784", "token": "x", "name": "ML"}),
                  mock.patch.object(sm, "_multipart", lambda url, fields, ff=None, path=None, timeout=900: self.f.post(url, fields) | {"post_id": "703_9"})]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()

    def test_ig_carousel(self):
        job = {"channel": "instagram", "text": "Cap", "first_comment": "#tags", "location": "", "alt_text": "",
               "media": [{"kind": "image", "public_url": "https://h/a.jpg"}, {"kind": "video", "public_url": "https://h/b.mp4"}]}
        r = self.sm.InstagramPublisher(sleep=lambda s: None).publish(job, lambda s, m: None)
        posts = [c for c in self.f.calls if c[0] == "POST"]
        self.assertEqual(posts[0][2], {"image_url": "https://h/a.jpg", "is_carousel_item": "true"})
        self.assertEqual(posts[1][2]["media_type"], "VIDEO")
        self.assertEqual(posts[2][2]["media_type"], "CAROUSEL")
        self.assertEqual(posts[3][1], "1784/media_publish")
        self.assertEqual(posts[4][1], f"{r['post_id']}/comments")

    def test_ig_needs_public_url(self):
        with self.assertRaises(pub.NotSupported):
            self.sm.InstagramPublisher().publish({"text": "x", "media": [{"kind": "image"}]}, lambda s, m: None)

    def test_fb_find_recent(self):
        since = store.now() - dt.timedelta(minutes=2)
        rows = {"data": [{"id": "703_old", "message": "Seven nights on the reef.", "created_time": "2020-01-01T00:00:00+0000"},
                         {"id": "703_new", "message": "Seven  nights on the REEF.\n\n#liveaboard", "created_time": store.iso(store.now()), "permalink_url": "u"}]}
        with mock.patch.object(self.sm, "_get", lambda path, **kw: rows):
            hit = self.sm.FacebookPublisher().find_recent({"text": "Seven nights on the reef.\n\n#liveaboard"}, since)
            self.assertEqual(hit, {"post_id": "703_new", "url": "u"})
            self.assertIsNone(self.sm.FacebookPublisher().find_recent({"text": "Something else"}, since))

    def test_fb_scheduled_link_post(self):
        when = store.now() + dt.timedelta(hours=2)
        self.sm.FacebookPublisher().schedule({"text": "Hi", "link": "https://www.masterliveaboards.com/", "media": []}, when, lambda s, m: None)
        c = self.f.calls[0]
        self.assertEqual(c[1], "703318833095953/feed")
        self.assertEqual(c[2]["published"], "false")
        self.assertEqual(c[2]["scheduled_publish_time"], int(when.timestamp()))


class Insights(unittest.TestCase):
    def test_lifetime_preferred(self):
        from areas import _social_meta as sm
        x = {"id": "1_2", "created_time": "2026-11-08T15:53:01+0000", "permalink_url": "https://www.facebook.com/reel/9/", "status_type": "added_video",
             "shares": {"count": 2}, "reactions": {"summary": {"total_count": 10}}, "comments": {"summary": {"total_count": 1}},
             "insights": {"data": [{"name": "post_total_media_view_unique", "period": "lifetime", "values": [{"value": 1271}]},
                                   {"name": "post_total_media_view_unique", "period": "day", "values": [{"value": 0}]},
                                   {"name": "post_clicks_by_type", "period": "lifetime", "values": [{"value": {"link clicks": 4, "other clicks": 6}}]}]}}
        r = sm.fb_row(x)
        self.assertEqual((r["reach"], r["link_clicks"], r["clicks"], r["engagement"], r["format"]), (1271, 4, 10, 13, "Reel"))


class LinkedIn(unittest.TestCase):
    def test_not_connected(self):
        from areas import _social_linkedin as li
        with self.assertRaises(li.NotConnected):
            li.LinkedInPublisher(token="", org="").publish({"text": "x", "media": []}, lambda s, m: None)

    def test_image_post_against_fake(self):
        from areas import _social_linkedin as li
        calls = []

        def http(method, url, headers, body=None):
            calls.append((method, url, headers, body))
            if "initializeUpload" in url:
                return 200, {}, json.dumps({"value": {"uploadUrl": "https://up.example/1", "image": "urn:li:image:A"}}).encode()
            if method == "PUT":
                return 201, {}, b""
            return 201, {"x-restli-id": "urn:li:share:9"}, b""
        img = write("li.jpg", jpg(1200, 627))
        r = li.LinkedInPublisher(token="T", org="urn:li:organization:1", http=http).publish(
            {"text": "Hello", "alt_text": "Reef", "media": [{"kind": "image", "path": str(img)}]}, lambda s, m: None)
        self.assertEqual(r["post_id"], "urn:li:share:9")
        body = json.loads(calls[-1][3])
        self.assertEqual(body["content"]["media"], {"id": "urn:li:image:A", "altText": "Reef"})
        self.assertEqual(calls[-1][2]["LinkedIn-Version"], li.VERSION)
        self.assertEqual(body["lifecycleState"], "PUBLISHED")


class Boost(unittest.TestCase):
    D = {"campaigns": [{"id": "c1", "name": "Camp", "objective": "OUTCOME_ENGAGEMENT"}], "adsets": [{"id": "s1", "name": "Set", "campaign_id": "c1"}],
         "ads": [], "account": {"currency": "USD"}}

    def setUp(self):
        import meta_ads
        self.m = meta_ads
        self.p = [mock.patch.object(meta_ads, "load", lambda *a, **k: self.D), mock.patch.object(meta_ads, "_items", lambda extra=None: {})]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()

    def test_check_summary_ops(self):
        c = self.m._normalise({"type": "meta_boost_post", "adset_id": "s1", "post_id": "703318833095953_1564491939056465", "name": "Boost reef"})
        self.assertEqual(self.m.check(c, self.D, {}), ([], []))
        self.assertIn("boost, Paused", self.m.summarise(c, self.D, {}))
        st = self.m.ops_for(c, {})[0]
        self.assertEqual(st["kind"], "new_ad")
        self.assertEqual(st["creative"]["object_story_id"], c["post_id"])
        self.assertEqual(st["adset"], "s1")
        bad = self.m._normalise({"type": "meta_boost_post", "adset_id": "s1", "post_id": "123", "name": "x"})
        self.assertTrue(self.m.check(bad, self.D, {})[0])
        self.assertTrue(self.m.check({**c, "adset_id": "nope"}, self.D, {})[0])

    @unittest.skipUnless(REAL, "SOCIAL_REAL=1 for Meta's validate_only check")
    def test_validate_only_real(self):
        for x in self.p:
            x.stop()
        self.p = []
        self.m.use("meta")
        rows = __import__("areas._social_meta", fromlist=["x"]).published(0)["posts"]
        fb = next(r for r in rows if r["channel"] == "facebook")
        st = {"kind": "new_ad", "creative": {"name": "Studio boost check", "object_story_id": fb["id"]}, "check_creative": True,
              "adset": None, "source_creative": None}
        self.assertEqual(self.m._check_step(st), "")  # validate_only: Meta creates nothing


@unittest.skipUnless(REAL, "SOCIAL_REAL=1 for the read-only Meta read")
class RealRead(unittest.TestCase):
    def test_read(self):
        from areas import _social_meta as sm
        c = sm.published(0)
        self.assertEqual(c["errors"], [])
        fb = [r for r in c["posts"] if r["channel"] == "facebook"]
        ig = [r for r in c["posts"] if r["channel"] == "instagram"]
        print(f"\nREAL READ: {len(fb)} Facebook posts, {len(ig)} Instagram media, {len(c['scheduled'])} Meta-scheduled; "
              f"first FB reach {fb[0]['reach']}, first IG views {ig[0]['views']}")
        self.assertTrue(fb and ig)
        self.assertNotIn("token", json.dumps(c).lower())
        h = social.health()
        self.assertEqual([r["ok"] for r in h[:2]], [True, True])


class Planning(unittest.TestCase):
    """Slots, best time, quick picks, UTM, CSV, autolists, hashtags, analytics, competitors (pure, from _social_plan)."""

    def test_slots(self):
        from areas import _social_plan as plan
        self.assertEqual(plan.parse_slots("Tue 09:00, thursday 18:30"), [(1, 9, 0), (3, 18, 30)])
        with self.assertRaises(ValueError):
            plan.parse_slots(["Tue 9am"])
        tz = dt.datetime.now().astimezone().tzinfo
        mon = dt.datetime(2026, 11, 16, 8, 0, tzinfo=tz)  # a Monday
        slots = {"instagram": ["Tue 09:00", "Thu 18:00"]}
        self.assertEqual(plan.next_free_slot(slots, ["instagram"], [], mon), dt.datetime(2026, 11, 17, 9, 0, tzinfo=tz))
        taken = [{"channels": ["instagram"], "when": store.iso(dt.datetime(2026, 11, 17, 9, 0, tzinfo=tz))}]
        self.assertEqual(plan.next_free_slot(slots, ["instagram"], taken, mon), dt.datetime(2026, 11, 19, 18, 0, tzinfo=tz))
        self.assertIsNone(plan.next_free_slot(slots, ["instagram"], taken, mon, day=dt.date(2026, 11, 17)))
        self.assertIsNone(plan.next_free_slot({}, ["linkedin"], [], mon))

    def test_heatmap_and_best(self):
        from areas import _social_plan as plan
        tz = dt.timezone.utc
        posts = [{"channel": "instagram", "when": f"2026-11-{d:02d}T18:00:00+00:00", "engagement": 200, "reach": 1000} for d in (3, 10)]
        posts += [{"channel": "instagram", "when": "2026-11-04T07:00:00+00:00", "engagement": 10, "reach": 1000}]
        h = plan.heatmap(posts, "instagram", None, tz)
        self.assertEqual(h["posts"], 3)
        self.assertEqual((h["top"][0]["day"], h["top"][0]["hour"]), (1, 18))  # Tuesday 18:00
        online = [(dt.datetime(2026, 11, 4, 8, 0, tzinfo=tz), {str(x): (5000 if x == 12 else 100) for x in range(24)})]
        h2 = plan.heatmap([], "instagram", [(e, {int(k): v for k, v in d.items()}) for e, d in online], tz)
        self.assertEqual(h2["top"][0]["hour"], 20)  # 12:00 Pacific = 20:00 UTC
        nb = plan.next_best(h["top"], dt.datetime(2026, 11, 16, 8, 0, tzinfo=tz))
        self.assertEqual(nb[0], dt.datetime(2026, 11, 17, 18, 0, tzinfo=tz))
        q = plan.quick_times(dt.datetime(2026, 11, 16, 8, 0, tzinfo=tz), nb[0], nb)
        self.assertEqual([x["label"][:8] for x in q][:4], ["Today 18", "Tomorrow", "Tomorrow", "Tomorrow"])

    def test_utm(self):
        from areas import _social_plan as plan
        u = plan.add_utm("https://www.masterliveaboards.com/egypt/?a=1", "facebook", "red_sea")
        self.assertIn("utm_source=facebook", u)
        self.assertIn("utm_medium=social", u)
        self.assertIn("a=1", u)
        keep = "https://www.masterliveaboards.com/?utm_source=x"
        self.assertEqual(plan.add_utm(keep, "facebook", "c"), keep)
        self.assertIn("utm_campaign=c).", plan.tracked_text("See (https://e.com/p).", "linkedin", "c").replace("%29", ")") + ").")

    def test_csv(self):
        from areas import _social_plan as plan
        rows = plan.parse_csv("date,time,channels,caption,link\n2026-11-25,09:30,fb;ig,Red Sea week,https://e.com\n25/11/2026,x,tiktok,Hi,\n")
        self.assertEqual(rows[0]["fields"]["channels"], ["facebook", "instagram"])
        self.assertTrue(rows[0]["fields"]["when"].startswith("2026-11-25T09:30"))
        self.assertFalse(rows[0]["problems"])
        self.assertEqual(len(rows[1]["problems"]), 3)  # bad time, unknown channel, no channels

    def test_autolist_fill(self):
        from areas import _social_plan as plan
        tz = dt.timezone.utc
        posts = {"a": {"id": "a", "caption": "A", "channels": ["facebook"], "approval": "approved"}, "b": {"id": "b", "caption": "B", "channels": ["facebook"]}}
        times = [dt.datetime(2026, 11, 17, 9, tzinfo=tz), dt.datetime(2026, 11, 19, 9, tzinfo=tz), dt.datetime(2026, 11, 24, 9, tzinfo=tz)]
        new, nxt = plan.autolist_fill({"id": "L", "post_ids": ["a", "b"], "next": 1}, posts, times)
        self.assertEqual([x["caption"] for x in new], ["B", "A", "B"])
        self.assertEqual(nxt, 0)
        self.assertTrue(all(x["autolist"] == "L" and "results" not in x for x in new))
        self.assertEqual([x.get("approval") for x in new], [None, "needed", None])  # "a" was approved: its copy needs approval again

    def test_hashtags_analytics_report_competitors(self):
        from areas import _social_plan as plan
        rows = [{"channel": "instagram", "when": "2026-11-05T10:00:00+00:00", "text": "Reef #RedSea #liveaboard", "engagement": 100, "reach": 1000, "format": "Reel"},
                {"channel": "instagram", "when": "2026-11-06T10:00:00+00:00", "text": "#redsea mantas", "engagement": 50, "reach": 500, "format": "Image"},
                {"channel": "facebook", "when": "2026-11-06T10:00:00+00:00", "text": "#liveaboard", "engagement": 10, "reach": 100, "format": "Photo", "link_clicks": 3}]
        r = plan.hashtag_rank(rows)
        self.assertEqual(r[0], {"tag": "#redsea", "posts": 2, "avg_engagement": 75.0})
        a = plan.analytics(rows, store.parse_when("2026-11-01T00:00:00+00:00"), store.parse_when("2026-12-01T00:00:00+00:00"),
                           {"instagram": {"followers": 9000, "growth": 30}})
        self.assertEqual(a["channels"]["instagram"]["engagement_rate"], 10.0)
        self.assertEqual(a["channels"]["instagram"]["followers"], 9000)
        self.assertEqual(a["channels"]["facebook"]["link_clicks"], 3)
        self.assertEqual(a["top"][0]["engagement"], 100)
        page = plan.report_html(a)
        self.assertIn("<table>", page)
        self.assertNotIn("\u2014", page)
        f = plan.competitor_facts("x", {"followers_count": 10, "media_count": 5, "media": {"data": [
            {"like_count": 10, "comments_count": 2, "timestamp": "2026-11-01T00:00:00+0000"},
            {"like_count": 20, "comments_count": 0, "timestamp": "2026-11-08T00:00:00+0000"}]}})
        self.assertEqual((f["avg_likes"], f["avg_comments"], f["posts_per_week"]), (15, 1.0, 1.0))


class Scheduler(unittest.TestCase):
    """Studio's own scheduler on the Mac: Schedule puts IG/LinkedIn in the local queue, tick() publishes at the (fake) time."""

    def setUp(self):
        fresh()
        social._fakes.clear()
        pub.FakePublisher.live.clear()
        social._shipper = None
        self.img = write("pic2.jpg", jpg(1080, 1350))
        self.ml = mock.patch.object(social, "media_lookup", lambda mid: (self.img, "jpg", "image"))
        self.ml.start()
        self.lc = mock.patch.object(social, "link_check", lambda u: "")
        self.lc.start()

    def tearDown(self):
        self.ml.stop()
        self.lc.stop()

    def post(self, hours=3, **kw):
        return store.save({"caption": "Reefs of the north.", "channels": ["instagram", "linkedin"], "media": [{"media_id": "m", "kind": "image"}],
                           "alt_text": "Reef", "when": store.iso(store.now() + dt.timedelta(hours=hours)), **kw})

    def test_off_blocks_on_queues(self):
        p = self.post()
        self.assertFalse(social.schedule({"id": p["id"], "pressed": "user"})["ok"])
        social.save_settings({"local_scheduler": True})
        r = social.schedule({"id": p["id"], "pressed": "user"})
        self.assertTrue(r["ok"], r)
        self.assertEqual({x["runner"] for x in store.queue()}, {"local"})
        self.assertEqual(r["results"]["instagram"]["via"], "local")

    def test_tick_publishes_at_fake_time(self):
        social.save_settings({"local_scheduler": True})
        p = self.post()
        social.schedule({"id": p["id"], "pressed": "user"})
        self.assertEqual(social.tick(store.now())["published"], [])  # not yet
        at = store.now() + dt.timedelta(hours=3, seconds=30)
        r = social.tick(at)
        self.assertEqual(len(r["published"]), 2)
        self.assertEqual(r["synced"], 2)
        q = store.get(p["id"])
        self.assertEqual(store.status(q), "published")
        self.assertTrue(q["results"]["instagram"]["post_id"].startswith("fake-instagram"))
        self.assertEqual(social.tick(at + dt.timedelta(minutes=1))["published"], [])  # never twice

    def test_missed_not_posted_late(self):
        social.save_settings({"local_scheduler": True})
        p = self.post()
        social.schedule({"id": p["id"], "pressed": "user"})
        r = social.tick(store.now() + dt.timedelta(hours=4))  # the Mac was asleep for an hour
        self.assertEqual(len(r["missed"]), 2)
        self.assertFalse([c for c in social._fakes["instagram"].calls if c[0] == "publish"])
        q = store.get(p["id"])
        self.assertEqual(store.status(q), "missed")
        self.assertIn("Post now", q["results"]["instagram"]["error"])
        r = social.post_now({"id": p["id"], "pressed": "user"})  # Post now still works on a missed post
        self.assertTrue(r["ok"], r)

    def test_late_minutes_allows(self):
        social.save_settings({"local_scheduler": True, "late_minutes": 90})
        p = self.post()
        social.schedule({"id": p["id"], "pressed": "user"})
        self.assertEqual(len(social.tick(store.now() + dt.timedelta(hours=4))["published"]), 2)

    def test_tick_off_does_nothing_and_vps_items_ignored(self):
        social.save_settings({"local_scheduler": True})
        store.queue_add({"post_id": "x", "channel": "instagram", "due": store.iso(store.now() - dt.timedelta(minutes=1)), "payload": {}, "runner": "vps"})
        self.assertEqual(social.tick()["published"], [])
        social.save_settings({"local_scheduler": False})
        self.assertTrue(social.tick()["off"])

    def test_start_scheduler_not_on_import(self):
        self.assertFalse(social._sched.get("thread"))
        with mock.patch.object(social, "tick", lambda at=None: None):
            self.assertTrue(social.start_scheduler(interval=3600))
            self.assertFalse(social.start_scheduler(interval=3600))
        social._sched["stop"].set()
        social._sched.clear()

    def test_approval_gate(self):
        social.save_settings({"local_scheduler": True})
        p = self.post(approval="needed", note="Check the price")
        r = social.schedule({"id": p["id"], "pressed": "user"})
        self.assertTrue(r["needs_approval"])
        self.assertEqual(r["note"], "Check the price")
        self.assertTrue(social.post_now({"id": p["id"], "pressed": "user"})["needs_approval"])
        social.approve({"id": p["id"], "pressed": "user"})
        self.assertTrue(social.schedule({"id": p["id"], "pressed": "user"})["ok"])

    def test_slot_and_add_to_queue(self):
        social.save_settings({"slots": {"instagram": "Tue 09:00", "linkedin": ["Tue 09:00"]}})
        with self.assertRaises(ValueError):
            social.save_settings({"slots": {"instagram": "someday"}})
        p = store.save({"caption": "x", "channels": ["instagram"]})
        q = social.add_to_queue({"id": p["id"]})
        t = store.parse_when(q["when"])
        self.assertEqual((t.weekday(), t.hour, t.minute), (1, 9, 0))
        p2 = store.save({"caption": "y", "channels": ["instagram"]})
        t2 = store.parse_when(social.add_to_queue({"id": p2["id"]})["when"])
        self.assertEqual((t2 - t).days, 7)  # the next Tuesday: the first is taken
        nxt_tue = t + dt.timedelta(days=14)
        s = social.get_slot({"day": nxt_tue.date().isoformat(), "channels": "instagram"})
        self.assertEqual((s["from"], store.parse_when(s["when"]).hour), ("queue", 9))
        wed = nxt_tue + dt.timedelta(days=1)
        self.assertEqual(social.get_slot({"day": wed.date().isoformat(), "channels": "instagram"})["from"], "default")
        self.assertEqual(store.parse_when(social.get_slot({"day": wed.date().isoformat(), "hour": "15"})["when"]).hour, 15)

    def test_utm_in_job_and_view(self):
        p = store.save({"caption": "Book: https://www.masterliveaboards.com/x", "channels": ["facebook", "instagram"], "link": "https://www.masterliveaboards.com/",
                        "title": "Red Sea week"})
        v = social._view(p)
        self.assertIn("utm_campaign=red_sea_week", v["final_links"]["facebook"])
        self.assertNotIn("instagram", v["final_links"])
        job = pub.build_job(p, "facebook", lambda m: None, social.tracker(p))
        self.assertIn("utm_source=facebook", job["text"])
        store.update(p["id"], lambda x: x.__setitem__("utm", False))
        self.assertEqual(pub.build_job(store.get(p["id"]), "facebook", lambda m: None, social.tracker(store.get(p["id"])))["link"], "https://www.masterliveaboards.com/")

    def test_csv_import_makes_drafts_only(self):
        img = write("csvimg.jpg", jpg(1080, 1080))
        text = f"date,time,channels,caption,media\n2030-01-07,09:00,ig,Reef day,{img}\n2030-01-08,09:00,fb,Bahamas trip,\n"
        with mock.patch.object(social, "media_upload", lambda b: {"media_id": "mid", "kind": "image", "name": b["filename"]}):
            pv = social.csv_preview({"text": text})
            self.assertEqual((pv["ok"], pv["total"]), (1, 2))
            self.assertTrue(any("Bahamas" in x for x in pv["rows"][1]["problems"]))
            self.assertEqual(store.posts(), [])  # preview saves nothing
            r = social.csv_import({"text": text})
        self.assertEqual((len(r["created"]), r["skipped"]), (1, 1))
        p = store.get(r["created"][0])
        self.assertEqual((store.status(p), p["media"][0]["media_id"]), ("draft", "mid"))
        self.assertEqual(store.queue(), [])

    def test_library_and_autolist(self):
        social.save_settings({"slots": {"facebook": ["Mon 09:00", "Wed 09:00", "Fri 09:00"]}})
        t = social.library_save({"kind": "templates", "item": {"name": "Deal", "caption": "Seven nights", "hashtags": "liveaboard"}})
        self.assertEqual(store.library()["templates"][0]["id"], t["id"])
        with self.assertRaises(ValueError):
            social.library_save({"kind": "templates", "item": {"name": "Bad", "caption": "Bahamas"}})
        hs = social.library_save({"kind": "hashtag_sets", "item": {"name": "Egypt", "tags": "redsea, #egypt"}})
        self.assertEqual(hs["tags"], "#redsea #egypt")
        a = store.save({"caption": "Evergreen A", "channels": ["facebook"]})
        lst = social.library_save({"kind": "autolists", "item": {"name": "Evergreen", "post_ids": [a["id"]], "channels": ["facebook"]}})
        r = social.autolist_fill({"id": lst["id"], "days": 7})
        self.assertEqual(len(r["created"]), 3)
        self.assertTrue(all(store.status(store.get(i)) == "draft" for i in r["created"]))
        self.assertEqual(store.queue(), [])  # never scheduled by itself
        self.assertEqual(len(social.autolist_fill({"id": lst["id"], "days": 7})["created"]), 0)  # slots now used

    def test_settings_competitors(self):
        social.save_settings({"competitors": ["@some.dive_shop", "other"]})
        self.assertEqual(store.settings()["competitors"], ["some.dive_shop", "other"])
        with self.assertRaises(ValueError):
            social.save_settings({"competitors": ["not a name!"]})
        with mock.patch.object(social, "_sm") as sm:
            sm.return_value._cached.side_effect = lambda name, max_age, fn: fn()
            sm.return_value.business_discovery.side_effect = [{"followers_count": 5, "media": {"data": []}}, RuntimeError("not a business account")]
            r = social.get_competitors({})
        self.assertEqual(r["rows"][0]["followers"], 5)
        self.assertIn("not a business account", r["rows"][1]["error"])

    def test_test_tick_only_in_test_mode(self):
        with mock.patch.object(social, "test_mode", lambda: False):
            with self.assertRaises(PermissionError):
                social.test_tick({})


class Races(unittest.TestCase):
    """Round-2 review bugs, reproduced with fakes and a fake clock."""

    def setUp(self):
        fresh()
        social._fakes.clear()
        pub.FakePublisher.live.clear()
        social._shipper = None
        self.img = write("pic3.jpg", jpg(1080, 1350))
        self.ml = mock.patch.object(social, "media_lookup", lambda mid: (self.img, "jpg", "image"))
        self.ml.start()
        self.lc = mock.patch.object(social, "link_check", lambda u: "")
        self.lc.start()
        social.save_settings({"local_scheduler": True})
        self.p = store.save({"caption": "Race test reefs.", "channels": ["instagram"], "media": [{"media_id": "m", "kind": "image"}],
                             "alt_text": "Reef", "when": store.iso(store.now() + dt.timedelta(hours=3))})
        self.assertTrue(social.schedule({"id": self.p["id"], "pressed": "user"})["ok"])
        self.at = store.now() + dt.timedelta(hours=3, seconds=30)

    def tearDown(self):
        self.ml.stop()
        self.lc.stop()

    def posts_made(self):
        return [c for c in social.publishers()["instagram"].calls if c[0] == "publish"]

    def during_round(self, fn):
        """Run fn right after the publisher read the due list (the race window the reviewer found)."""
        real = store.due

        def due(*a, **k):
            out = real(*a, **k)
            fn()
            return out
        return mock.patch.object(store, "due", due)

    def test_cancel_during_round_wins(self):
        with self.during_round(lambda: self.assertTrue(social.cancel({"id": self.p["id"], "pressed": "user"})["ok"])):
            r = social.tick(self.at)
        self.assertEqual(r["published"], [])
        self.assertEqual(self.posts_made(), [])
        self.assertEqual(store.status(store.get(self.p["id"])), "draft")  # not overwritten to Published by the sync

    def test_move_during_round_posts_once_at_new_time(self):
        later = store.now() + dt.timedelta(days=1)

        def mv():
            social.move({"id": self.p["id"], "when": store.iso(later)})
            self.assertTrue(social.confirm_move({"id": self.p["id"], "pressed": "user"})["ok"])
        with self.during_round(mv):
            social.tick(self.at)
        self.assertEqual(self.posts_made(), [])  # not at the old time
        self.assertEqual(store.status(store.get(self.p["id"])), "scheduled")
        social.tick(later + dt.timedelta(seconds=30))
        self.assertEqual(len(self.posts_made()), 1)  # once, at the new time
        self.assertEqual(store.status(store.get(self.p["id"])), "published")

    def test_cancel_while_publishing_is_too_late(self):
        it = store.queue()[0]
        self.assertTrue(store.queue_claim(it["id"], it["due"]))
        r = social.cancel({"id": self.p["id"], "pressed": "user"})
        self.assertFalse(r["ok"])
        self.assertIn("too late to cancel", r["message"])
        self.assertEqual(store.get(self.p["id"])["results"]["instagram"]["status"], "scheduled")
        social.move({"id": self.p["id"], "when": store.iso(store.now() + dt.timedelta(days=1))})
        self.assertFalse(social.confirm_move({"id": self.p["id"], "pressed": "user"})["ok"])  # nor moved

    def stale(self, live_too):
        it = store.queue()[0]
        store.queue_claim(it["id"], it["due"])
        old = store.iso(store.now() - dt.timedelta(minutes=20))
        store.queue_set(it["id"], claimed_at=old)
        if live_too:  # it did go out before the crash
            pub.FakePublisher.live.append(("instagram", it["payload"]["text"], old, {"post_id": "fake-ig-live", "url": "u"}))
        return it

    def test_stale_publishing_found_live(self):
        self.stale(True)
        social.sync_queue()
        r = store.get(self.p["id"])["results"]["instagram"]
        self.assertEqual((r["status"], r["post_id"]), ("published", "fake-ig-live"))
        self.assertEqual(self.posts_made(), [])

    def test_stale_publishing_needs_checking_then_check_again(self):
        self.stale(False)
        social.sync_queue()
        q = store.get(self.p["id"])
        self.assertEqual(store.status(q), "needs_checking")
        self.assertIn("Needs checking", q["results"]["instagram"]["error"])
        with self.assertRaises(ValueError) as e:
            social.post_now({"id": self.p["id"], "pressed": "user", "confirm_repost": True})  # no repost path any more
        self.assertIn("Check this channel first", str(e.exception))
        self.assertEqual(self.posts_made(), [])
        r = social.check_again({"id": self.p["id"], "channel": "instagram"})  # not found on the channel -> failed
        self.assertTrue(r["ok"])
        self.assertEqual(store.get(self.p["id"])["results"]["instagram"]["status"], "failed")
        r = social.post_now({"id": self.p["id"], "pressed": "user"})  # now a normal Post now
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(self.posts_made()), 1)

    def test_fresh_publishing_is_left_alone(self):
        it = store.queue()[0]
        store.queue_claim(it["id"], it["due"])
        social.sync_queue()
        self.assertEqual(store.queue()[0]["status"], "publishing")

    def test_cli_cancel_too_late(self):
        it = store.queue()[0]
        store.queue_claim(it["id"], it["due"])
        with mock.patch("sys.stderr", io.StringIO()) as err:
            self.assertEqual(social_publisher.main(["--data", TMP, "--cancel", it["id"]]), 2)
        self.assertIn("too late", err.getvalue())

    def test_approval_only_by_the_user(self):
        d = social.add_draft({"caption": "Claude draft", "channels": ["facebook"], "approval": "approved"})
        self.assertEqual(store.get(d["id"])["approval"], "")  # Claude can't approve
        d = social.add_draft({"caption": "Claude draft", "channels": ["facebook"], "approval": "needs_approval"})
        self.assertEqual(store.get(d["id"])["approval"], "needed")
        social.add_draft({"id": d["id"], "approval": ""})
        self.assertEqual(store.get(d["id"])["approval"], "needed")  # nor remove the need
        with self.assertRaises(PermissionError):
            social.approve({"id": d["id"]})
        social.save({"id": d["id"], "post": {"approval": "approved"}})
        self.assertEqual(store.get(d["id"])["approval"], "needed")  # save can't approve either
        store.update(d["id"], lambda x: x.__setitem__("approval", "approved"))  # approved without the user's button
        self.assertTrue(social.approval_needed(store.get(d["id"])))
        social.approve({"id": d["id"], "pressed": "user"})
        self.assertFalse(social.approval_needed(store.get(d["id"])))
        social.add_draft({"id": d["id"], "caption": "Changed by Claude"})
        self.assertTrue(social.approval_needed(store.get(d["id"])))  # changed after approval: approve again

    def test_csv_runs_media_checks(self):
        png_file = write("csv.png", png(1080, 1080))
        text = f"date,time,channels,caption,media\n2030-01-07,09:00,ig,No media row,\n2030-01-08,09:00,ig,PNG row,{png_file}\n2030-01-09,09:00,fb,Text only is fine,\n"
        rows = social.csv_preview({"text": text})["rows"]
        self.assertTrue(any("text-only" in x for x in rows[0]["problems"]))
        self.assertTrue(any("JPEG" in x for x in rows[1]["problems"]))
        self.assertEqual(rows[2]["problems"], [])

    def test_start_scheduler_concurrent(self):
        social._sched.clear()
        made = []
        real = social._start_thread

        def slow(interval):
            time.sleep(0.05)
            made.append(1)
            return real(interval)
        with mock.patch.object(social, "_start_thread", slow), mock.patch.object(social, "tick", lambda at=None: None):
            ts = [threading.Thread(target=social.start_scheduler, kwargs={"interval": 3600}) for _ in range(5)]
            [t.start() for t in ts]
            [t.join() for t in ts]
        self.assertEqual(len(made), 1)
        social._sched["stop"].set()
        social._sched.clear()


class Round3(unittest.TestCase):
    """Re-review round 3: transactional moves, autolist approval, one late-posting rule for both publishers."""

    def setUp(self):
        fresh()
        social._fakes.clear()
        pub.FakePublisher.live.clear()
        social._shipper = None
        self.img = write("pic4.jpg", jpg(1080, 1350))
        self.ml = mock.patch.object(social, "media_lookup", lambda mid: (self.img, "jpg", "image"))
        self.ml.start()
        self.lc = mock.patch.object(social, "link_check", lambda u: "")
        self.lc.start()

    def tearDown(self):
        self.ml.stop()
        self.lc.stop()
        social._shipper = None

    def scheduled(self, **settings):
        social.save_settings({"local_scheduler": True, **settings})
        p = store.save({"caption": "Round three reefs.", "channels": ["instagram"], "media": [{"media_id": "m", "kind": "image"}],
                        "alt_text": "Reef", "when": store.iso(store.now() + dt.timedelta(hours=3))})
        self.assertTrue(social.schedule({"id": p["id"], "pressed": "user"})["ok"])
        return store.get(p["id"])

    def move(self, p, days=1):
        social.move({"id": p["id"], "when": store.iso(store.now() + dt.timedelta(days=days))})
        return social.confirm_move({"id": p["id"], "pressed": "user"})

    def test_move_build_failure_keeps_old_schedule(self):
        p = self.scheduled()
        old = p["results"]["instagram"]["queue_id"]
        with mock.patch.object(pub, "build_job", side_effect=RuntimeError("media gone")):
            r = self.move(p)
        self.assertFalse(r["ok"])
        self.assertIn("still scheduled for", r["results"]["instagram"]["error"])
        q = store.get(p["id"])
        self.assertEqual((q["results"]["instagram"]["status"], q["results"]["instagram"]["queue_id"]), ("scheduled", old))
        self.assertEqual([(x["id"], x["status"]) for x in store.queue()], [(old, "scheduled")])
        social.sync_queue()  # the safety net leaves a healthy schedule alone
        self.assertEqual(store.status(store.get(p["id"])), "scheduled")
        self.assertEqual(len(social.tick(store.now() + dt.timedelta(hours=3, seconds=30))["published"]), 1)  # still goes out

    def test_move_ok_swaps_items(self):
        p = self.scheduled()
        old = p["results"]["instagram"]["queue_id"]
        self.assertTrue(self.move(p)["ok"])
        q = {x["id"]: x["status"] for x in store.queue()}
        new = store.get(p["id"])["results"]["instagram"]["queue_id"]
        self.assertEqual((q[old], q[new]), ("cancelled", "scheduled"))

    def test_stuck_scheduled_is_unblocked(self):
        p = self.scheduled()
        store.queue_cancel(p["id"], "instagram")  # e.g. a move that half failed in an older version
        social.sync_queue()
        q = store.get(p["id"])
        self.assertEqual(q["results"]["instagram"]["status"], "not_scheduled")
        self.assertIn("please schedule again", q["results"]["instagram"]["error"])
        social.save({"id": p["id"], "post": {"caption": "Edited after"}})  # editing works again
        self.assertTrue(social.schedule({"id": p["id"], "pressed": "user"})["ok"])  # and scheduling again
        p2 = self.scheduled()
        store.queue_cancel(p2["id"], "instagram")
        social.sync_queue()
        self.assertTrue(store.remove(p2["id"]))

    def test_meta_schedule_not_touched_by_safety_net(self):
        p = store.save({"caption": "FB only.", "channels": ["facebook"], "when": store.iso(store.now() + dt.timedelta(hours=3))})
        self.assertTrue(social.schedule({"id": p["id"], "pressed": "user"})["ok"])
        social.sync_queue()
        self.assertEqual(store.status(store.get(p["id"])), "scheduled")

    def test_autolist_copy_needs_approval(self):
        social.save_settings({"local_scheduler": True, "slots": {"facebook": ["Mon 09:00", "Thu 09:00"]}})
        a = store.save({"caption": "Needs a look", "channels": ["facebook"], "approval": "needed"})
        b = store.save({"caption": "Was approved", "channels": ["facebook"]})
        social.approve({"id": b["id"], "pressed": "user"})
        lst = social.library_save({"kind": "autolists", "item": {"name": "E", "post_ids": [a["id"], b["id"]], "channels": ["facebook"]}})
        made = social.autolist_fill({"id": lst["id"], "days": 8})["created"]
        self.assertTrue(made)
        for i in made:
            c = store.get(i)
            self.assertEqual(c["approval"], "needed")
            self.assertTrue(social.schedule({"id": i, "pressed": "user"}).get("needs_approval"))

    def test_grace_travels_with_the_item(self):
        self.scheduled(late_minutes=90)
        it = store.queue()[0]
        self.assertEqual(it["max_late"], 90)
        social.save_settings({"late_minutes": 0})  # a later change does not shorten what was promised at Schedule
        an_hour_late = store.parse_when(it["due"]) + dt.timedelta(hours=1)
        fakes = {c: pub.FakePublisher(c) for c in store.CHANNELS}
        r = social_publisher.run_once(store, pub, fakes, live=False, at=an_hour_late)  # the VPS job's defaults
        self.assertEqual(r["would"], [it["id"]])
        r = social_publisher.run_once(store, pub, fakes, live=False, at=an_hour_late, max_late=30)  # explicit --max-late wins
        self.assertEqual(r["missed"], [it["id"]])
        old = {**it, "id": "noitem", "max_late": None}
        old.pop("max_late")
        ready, missed = store.due([old], store.parse_when(it["due"]) + dt.timedelta(minutes=5))  # no grace on the item: 2 minutes
        self.assertEqual((ready, len(missed)), ([], 1))


class NeedsChecking(unittest.TestCase):
    """Round 5: one rule for Needs checking; the VPS runner is parked; the grace default is 2."""

    def setUp(self):
        fresh()
        social._fakes.clear()
        pub.FakePublisher.live.clear()
        social._shipper = None
        self.img = write("pic6.jpg", jpg(1080, 1350))
        self.ml = mock.patch.object(social, "media_lookup", lambda mid: (self.img, "jpg", "image"))
        self.ml.start()
        self.lc = mock.patch.object(social, "link_check", lambda u: "")
        self.lc.start()
        social.save_settings({"local_scheduler": True})
        self.p = store.save({"caption": "Checking reefs.", "channels": ["instagram", "facebook"], "media": [{"media_id": "m", "kind": "image"}],
                             "alt_text": "Reef", "when": store.iso(store.now() + dt.timedelta(hours=3))})
        store.update(self.p["id"], lambda x: x["results"].__setitem__("instagram", {"status": "needs_checking", "via": "local",
                                                                                   "at": store.iso(store.now()), "error": "Needs checking"}))

    def tearDown(self):
        self.ml.stop()
        self.lc.stop()

    def refused(self, fn):
        with self.assertRaises(ValueError) as e:
            fn()
        self.assertIn("Check this channel first (Check again)", str(e.exception))

    def test_everything_blocked_while_checking(self):
        pid = self.p["id"]
        self.refused(lambda: social.schedule({"id": pid, "pressed": "user"}))
        self.refused(lambda: social.post_now({"id": pid, "pressed": "user"}))
        self.refused(lambda: social.post_now({"id": pid, "pressed": "user", "confirm_repost": True}))
        self.refused(lambda: social.save({"id": pid, "post": {"caption": "edit"}}))
        self.refused(lambda: social.save({"id": pid, "post": {"when": store.iso(store.now() + dt.timedelta(days=2))}}))
        self.refused(lambda: social.move({"id": pid, "when": store.iso(store.now() + dt.timedelta(days=1))}))
        self.refused(lambda: social.add_to_queue({"id": pid}))
        store.update(pid, lambda x: x.__setitem__("move", {"when": store.iso(store.now() + dt.timedelta(days=1))}))
        r = social.confirm_move({"id": pid, "pressed": "user"})
        self.assertFalse(r["ok"])
        self.assertIn("Check this channel first", r["message"])
        with self.assertRaises(ValueError):
            store.remove(pid)  # nor deleted: it may be live
        self.assertEqual(social._fakes.get("instagram").calls if social._fakes else [], [])
        self.assertEqual(store.queue(), [])

    def test_check_again_found_and_failed(self):
        pub.FakePublisher.live.append(("instagram", store.caption_for(self.p, "instagram"), store.iso(store.now()), {"post_id": "ig-live", "url": "u"}))
        r = social.check_again({"id": self.p["id"], "channel": "instagram"})
        self.assertTrue(r["ok"])
        self.assertEqual(store.get(self.p["id"])["results"]["instagram"]["post_id"], "ig-live")
        with self.assertRaises(ValueError):
            social.check_again({"id": self.p["id"], "channel": "instagram"})  # nothing to check any more
        self.assertTrue(social.schedule({"id": self.p["id"], "pressed": "user", "channels": ["facebook"]})["ok"])  # unblocked

    def test_check_again_lookup_error_stays(self):
        social.publishers()["instagram"].fail = "find"
        r = social.check_again({"id": self.p["id"], "channel": "instagram"})
        self.assertFalse(r["ok"])
        self.assertEqual(store.get(self.p["id"])["results"]["instagram"]["status"], "needs_checking")

    def test_mac_flow_unchanged(self):
        p = store.save({"caption": "Mac flow.", "channels": ["instagram"], "media": [{"media_id": "m", "kind": "image"}], "alt_text": "Reef",
                        "when": store.iso(store.now() + dt.timedelta(hours=3))})
        self.assertTrue(social.schedule({"id": p["id"], "pressed": "user"})["ok"])
        social.move({"id": p["id"], "when": store.iso(store.now() + dt.timedelta(hours=5))})
        self.assertTrue(social.confirm_move({"id": p["id"], "pressed": "user"})["ok"])
        self.assertEqual(len(social.tick(store.now() + dt.timedelta(hours=5, seconds=30))["published"]), 1)
        self.assertEqual(store.status(store.get(p["id"])), "published")

    def test_grace_default_two_in_both_runners(self):
        social.save_settings({"late_minutes": 90})
        due = store.now() - dt.timedelta(minutes=10)
        for runner, ch in (("local", "linkedin"), ("vps", "facebook")):  # items without a stored grace
            store.queue_add({"post_id": self.p["id"], "channel": ch, "due": store.iso(due), "payload": {"text": "x", "media": []}, "runner": runner})
        r = social.tick()  # Studio's scheduler must not use the current 90-minute setting
        self.assertEqual((len(r["missed"]), r["published"]), (1, []))
        fakes = {c: pub.FakePublisher(c) for c in store.CHANNELS}
        r = social_publisher.run_once(store, pub, fakes, live=True, runner="vps")  # the (parked) job's own defaults
        self.assertEqual((len(r["missed"]), r["published"]), (1, []))


class Round6(unittest.TestCase):
    """Re-review round 6: save guard by any id path, unticked open channels, parked-VPS results resolvable."""

    def setUp(self):
        fresh()
        social._fakes.clear()
        pub.FakePublisher.live.clear()
        self.p = store.save({"caption": "Round six.", "channels": ["instagram", "facebook"]})
        store.update(self.p["id"], lambda x: x["results"].__setitem__("instagram", {"status": "needs_checking", "via": "local",
                                                                                   "at": store.iso(store.now()), "error": "Needs checking"}))

    def test_save_guard_any_id_path(self):
        pid = self.p["id"]
        for body in ({"post": {"id": pid, "caption": "sneaky"}}, {"id": pid, "post": {"caption": "x"}}, {"id": pid, "caption": "y"}):
            with self.assertRaises(ValueError):
                social.save(body)
        with self.assertRaises(ValueError) as e:
            social.save({"id": pid, "post": {"id": "other", "caption": "x"}})
        self.assertIn("differ", str(e.exception))
        with self.assertRaises(ValueError):  # second guard in the store itself
            store.save({"id": pid, "caption": "direct"})
        self.assertEqual(store.get(pid)["caption"], "Round six.")

    def test_untick_open_channel_refused(self):
        pid = self.p["id"]
        with self.assertRaises(ValueError):
            social.save({"post": {"id": pid, "channels": ["facebook"]}})
        store.update(pid, lambda x: x.__setitem__("channels", ["facebook"]))  # even if it were unticked some other way
        self.assertEqual(store.status(store.get(pid)), "needs_checking")
        self.assertIn("instagram", social._checking(store.get(pid)))

    def test_mcp_add_draft_same_guard(self):
        with self.assertRaises(ValueError):
            social.add_draft({"id": self.p["id"], "channels": ["facebook"], "caption": "claude"})
        self.assertEqual(set(store.get(self.p["id"])["channels"]), {"instagram", "facebook"})

    def test_check_again_old_vps_result(self):
        pid = self.p["id"]
        store.update(pid, lambda x: x["results"]["instagram"].update(via="vps", queue_id="gone"))
        r = social.check_again({"id": pid, "channel": "instagram"})  # not found on the channel -> failed
        self.assertTrue(r["ok"])
        self.assertIn("not managed by Studio", r["message"])
        self.assertEqual(store.get(pid)["results"]["instagram"]["status"], "failed")
        store.update(pid, lambda x: x["results"]["instagram"].update(status="needs_checking"))
        pub.FakePublisher.live.append(("instagram", store.caption_for(store.get(pid), "instagram"), store.iso(store.now()), {"post_id": "ig-x", "url": "u"}))
        social.check_again({"id": pid, "channel": "instagram"})
        self.assertEqual(store.get(pid)["results"]["instagram"]["status"], "published")


class DataPolicy(unittest.TestCase):
    """Studio's data policy for SoMe: manual = keep until Refresh; auto = each cache's own time limit; 0 always fetches."""

    def setUp(self):
        fresh()
        import datapolicy
        self.dp = datapolicy
        for f in Path(DP_DIR).glob("*"):
            f.unlink()
        for f in Path(TMP).glob("social_cache_*"):
            f.unlink()
        from areas import _social_meta as sm
        self.sm = sm
        self.n = {"fb": 0, "ig": 0, "page": 0, "online": 0}

        def fb(limit):
            self.n["fb"] += 1
            return [{"id": "1_1", "channel": "facebook", "when": store.iso(store.now()), "text": "x", "engagement": 1, "reach": 10}]

        def ig(limit):
            self.n["ig"] += 1
            return []
        self.p = [mock.patch.object(sm, "_fb_posts", fb), mock.patch.object(sm, "_ig_posts", ig), mock.patch.object(sm, "_fb_scheduled", lambda: [])]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()

    def auto(self, on=True):
        (Path(DP_DIR) / "settings.json").write_text(json.dumps({"data_policy": "auto" if on else "manual"}))

    def age(self, name, seconds):
        f = Path(TMP) / name
        c = json.loads(f.read_text())
        c["ts"] -= seconds
        f.write_text(json.dumps(c))

    def test_manual_keeps_until_refresh(self):
        self.sm.published()
        self.age("social_cache_meta.json", 10 * 86400)  # very old: still kept in manual mode
        self.sm.published()
        self.assertEqual(self.n["fb"], 1)
        self.assertTrue(self.dp.fetched("social"))  # marked for GET /api/data/status
        self.sm.published(0)  # 0 still forces
        self.assertEqual(self.n["fb"], 2)

    def test_auto_expires(self):
        self.auto()
        self.sm.published()
        self.sm.published()
        self.assertEqual(self.n["fb"], 1)
        self.age("social_cache_meta.json", self.sm.CACHE_TTL + 5)
        self.sm.published()
        self.assertEqual(self.n["fb"], 2)

    def test_cached_helper_both_modes(self):
        calls = []
        fn = lambda: calls.append(1) or {"v": len(calls)}  # noqa: E731
        self.sm._cached("social_cache_online.json", 6 * 3600, fn)
        self.age("social_cache_online.json", 7 * 3600)
        self.sm._cached("social_cache_online.json", 6 * 3600, fn)
        self.assertEqual(len(calls), 1)  # manual: kept
        self.auto()
        self.sm._cached("social_cache_online.json", 6 * 3600, fn)
        self.assertEqual(len(calls), 2)  # auto: 6 hours passed
        self.sm._cached("social_cache_online.json", 6 * 3600, fn)
        self.assertEqual(len(calls), 2)

    def test_refresh_invalidates_everything(self):
        self.sm.published()
        self.sm._cached("social_cache_online.json", 6 * 3600, lambda: {"x": 1})
        (Path(TMP) / "social_cache_acct_30.json").write_text(json.dumps({"ts": time.time(), "data": {}}))
        with mock.patch.object(self.sm, "info", lambda force=False: {}):
            r = social.refresh({})
        self.assertEqual(r["errors"], [])
        self.assertEqual(self.n["fb"], 2)
        self.assertFalse((Path(TMP) / "social_cache_online.json").exists())
        self.assertFalse((Path(TMP) / "social_cache_acct_30.json").exists())
        self.assertGreater(self.dp.refreshed_at("social"), 0)
        self.sm.published()  # the copy the refresh just wrote stays fresh
        self.assertEqual(self.n["fb"], 2)
        old = json.loads((Path(TMP) / "social_cache_meta.json").read_text())
        self.dp.invalidate("social")  # a Refresh in another process (the MCP) after this copy
        self.sm.published()
        self.assertEqual(self.n["fb"], 3)
        self.assertLess(old["ts"], self.dp.refreshed_at("social"))

    def test_page_token_only_on_auth_error(self):
        sm = self.sm
        n = {"get": 0, "calls": []}

        def get(path, **kw):
            n["get"] += 1
            return {"id": sm.PAGE_ID, "name": "ML", "access_token": f"tok{n['get']}", "instagram_business_account": {"id": "17", "username": "ml"}}

        def call(method, path, params=None, token=None, **kw):
            n["calls"].append(token)
            if token == "tok1" and len(n["calls"]) > 2:
                raise sm.meta_api.MetaError({"message": "Error validating access token", "code": 190}, status=400)
            return {"data": []}
        sm._page.clear()
        with mock.patch.object(sm.meta_api, "get", get), mock.patch.object(sm.meta_api, "call", call), mock.patch.object(sm.meta_api, "TOKEN", "test-token"):
            sm._get("a")
            sm._get("b")
            self.assertEqual(n["get"], 1)  # no time-based re-fetch
            sm._get("c")  # auth error -> fetched once more, tried again
            self.assertEqual(n["get"], 2)
            self.assertEqual(n["calls"][-1], "tok2")
            with mock.patch.object(sm.meta_api, "call", side_effect=sm.meta_api.MetaError({"message": "bad", "code": 100})):
                with self.assertRaises(sm.meta_api.MetaError):
                    sm._get("d")  # other errors: no token re-fetch
            self.assertEqual(n["get"], 2)
        sm._page.clear()

    def test_page_load_does_not_call_meta(self):
        sm = self.sm
        sm._page.clear()
        (Path(TMP) / "social_cache_page.json").write_text(json.dumps({"ts": time.time(), "data": {"page_id": "1", "page_name": "ML", "ig_id": "17", "ig_username": "ml"}}))
        self.sm.published()
        before = dict(self.n)
        with mock.patch.object(sm, "page", side_effect=AssertionError("Meta called on page load")):
            st = social.get_state({})
            social.get_calendar({})
            social.get_feed({})
        self.assertEqual(st["meta"]["page_name"], "ML")
        self.assertEqual(self.n, before)


if __name__ == "__main__":
    unittest.main()
