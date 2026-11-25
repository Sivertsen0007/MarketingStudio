"""New carousel ads (meta_new_ad with cards). Run from the engine folder:
    python3 tests/test_meta_carousel.py

Offline: Meta reads and writes are replaced with fakes; nothing is sent anywhere.
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("MSTUDIO_HOME", _tf.mkdtemp(prefix="mstudio-test-"))
_os.environ["MSTUDIO_KEYCHAIN"] = "0"

import base64
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
TMP = Path(tempfile.mkdtemp(prefix="meta-carousel-test-"))
os.environ["STUDIO_DATA_DIR"] = str(TMP)

import core  # noqa: E402

core.STUDIO_DIR = TMP
import meta_ads as meta  # noqa: E402

meta.MEDIA_DIR = TMP / "meta_media"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
SRC = {"id": "cr1", "object_story_spec": {"page_id": "p1", "instagram_user_id": "ig1",
                                          "link_data": {"link": "https://example.com/", "message": "Hello", "image_hash": "h1"}}}
DATA = {"campaigns": [{"id": "c1", "name": "Camp", "objective": "OUTCOME_SALES"}],
        "adsets": [{"id": "s1", "name": "Set", "campaign_id": "c1"}],
        "ads": [{"id": "a1", "adset_id": "s1", "creative": SRC}], "audiences": []}


def find(d, level, oid):
    for o in d[{"campaign": "campaigns", "adset": "adsets", "ad": "ads"}[level]]:
        if o["id"] == str(oid):
            return o
    raise ValueError(f"{level} {oid} not found")


class Carousel(unittest.TestCase):
    def setUp(self):
        self.p = [mock.patch.object(meta, "load", lambda *a, **k: DATA), mock.patch.object(meta, "_find", find),
                  mock.patch.object(meta, "act", lambda: "act_1"), mock.patch.object(meta, "link_problem", lambda url: ""),
                  mock.patch.object(meta, "_page_text", lambda url: "")]
        for x in self.p:
            x.start()
        salt = os.urandom(8)  # fresh files per test: an upload recorded by one test must not be reused by the next
        self.m = [meta.store_media(f"c{i}.png", PNG + salt + bytes([i]))["media_id"] for i in range(3)]

    def tearDown(self):
        for x in self.p:
            x.stop()

    def change(self, **kw):
        c = {"type": "meta_new_ad", "adset_id": "s1", "name": "Car", "primary_text": "Text\n\nhttps://example.com/",
             "link": "https://example.com/?u=1", "cta": "BOOK_TRAVEL",
             "cards": [{"media_id": m, "name": f"Card {i}", "description": f"D{i}", "link": f"https://example.com/{i}"}
                       for i, m in enumerate(self.m)]}
        c.update(kw)
        return meta._normalise(c)

    def test_valid_carousel_has_no_problems(self):
        problems, _ = meta.check(self.change(), DATA, {})
        self.assertEqual(problems, [])
        self.assertIn("carousel of 3 uploaded images", meta.summarise(self.change(), DATA, {}))

    def test_card_rules(self):
        c = self.change()
        self.assertIn("a carousel needs 2-10 cards", meta.check({**c, "cards": c["cards"][:1]}, DATA, {})[0])
        bad = self.change()
        bad["cards"][1] = {**bad["cards"][1], "media_id": "0" * 64, "name": ""}
        probs = meta.check(bad, DATA, {})[0]
        self.assertTrue(any("card 2: media_id not found" in p for p in probs), probs)
        self.assertTrue(any("card 2: needs name" in p for p in probs), probs)
        self.assertTrue(any("needs primary_text" in p for p in meta.check(self.change(primary_text=""), DATA, {})[0]))
        self.assertTrue(any("leave out from_ad" in p for p in meta.check(self.change(from_ad="a1"), DATA, {})[0]))

    def test_creative_keeps_order_and_no_end_card(self):
        with mock.patch.object(meta, "_stand_in_media", lambda kind: {"hash": "stand"}):
            s = meta.ops_for(self.change(), {})[0]
        ld = s["creative"]["object_story_spec"]["link_data"]
        self.assertEqual(s["kind"], "new_ad")
        self.assertEqual(s["card_media"], self.m)
        self.assertIs(ld["multi_share_optimized"], False)
        self.assertIs(ld["multi_share_end_card"], False)
        self.assertEqual([ch["name"] for ch in ld["child_attachments"]], ["Card 0", "Card 1", "Card 2"])
        self.assertEqual({ch["image_hash"] for ch in ld["child_attachments"]}, {"stand"})
        self.assertEqual(ld["child_attachments"][1]["call_to_action"], {"type": "BOOK_TRAVEL", "value": {"link": "https://example.com/1"}})
        self.assertEqual(s["creative"]["object_story_spec"]["page_id"], "p1")
        self.assertEqual(s["creative"]["degrees_of_freedom_spec"]["creative_features_spec"]["product_extensions"],
                         {"enroll_status": "OPT_OUT"})

    def test_push_uploads_every_card_and_creates_paused_ad(self):
        with mock.patch.object(meta, "_stand_in_media", lambda kind: {"hash": "stand"}):
            s = meta.ops_for(self.change(), {})[0]
        calls = []

        def post(path, params, **kw):
            calls.append((path, params))
            if path.endswith("/adimages"):
                return {"images": {"x": {"hash": "real-" + base64.b64decode(params["bytes"])[-1:].hex()}}}
            return {"id": "new-" + str(len(calls))}
        with mock.patch.object(meta, "post", post):
            rec = meta._do(s, {})
        self.assertEqual(len([c for c in calls if c[0].endswith("/adimages")]), 3)
        cr = next(p for path, p in calls if path.endswith("/adcreatives"))
        self.assertEqual([ch["image_hash"] for ch in cr["object_story_spec"]["link_data"]["child_attachments"]],
                         ["real-00", "real-01", "real-02"])
        ad = next(p for path, p in calls if path.endswith("/ads"))
        self.assertEqual((ad["status"], ad["adset_id"]), ("PAUSED", "s1"))
        self.assertEqual(rec["undo"], {"status": "DELETED"})
        self.assertEqual(len(rec["uploaded_cards"]), 3)
        # the step itself is not changed by the push (a retry starts clean)
        self.assertEqual({ch["image_hash"] for ch in s["creative"]["object_story_spec"]["link_data"]["child_attachments"]}, {"stand"})

    def test_single_media_ad_unchanged(self):
        c = meta._normalise({"type": "meta_new_ad", "adset_id": "s1", "name": "One", "media_id": self.m[0],
                             "primary_text": "T", "headline": "H", "link": "https://example.com/"})
        self.assertEqual(meta.check(c, DATA, {})[0], [])
        with mock.patch.object(meta, "_stand_in_media", lambda kind: {"hash": "stand"}):
            s = meta.ops_for(c, {})[0]
        self.assertNotIn("card_media", s)
        self.assertEqual(s["media_id"], self.m[0])

    def test_enable_new_creates_active_and_flags_spend(self):
        with mock.patch.object(meta, "_stand_in_media", lambda kind: {"hash": "stand"}):
            s = meta.ops_for(self.change(), {})[0]
        self.assertNotIn("status", s)
        meta._activate(s)
        self.assertEqual(s["status"], "ACTIVE")
        self.assertIn("(Active)", s["line"])
        self.assertTrue(s["spend"])
        calls = []

        def post(path, params, **kw):
            calls.append((path, params))
            return {"images": {"x": {"hash": "h"}}} if path.endswith("/adimages") else {"id": "n"}
        with mock.patch.object(meta, "post", post):
            meta._do(s, {})
        self.assertEqual(next(p for path, p in calls if path.endswith("/ads"))["status"], "ACTIVE")

    def test_plan_activates_only_new_ads_when_asked(self):
        item = {"id": "i1", "change": self.change(), "by": "claude", "summary": "x", "problems": []}
        with mock.patch.object(meta, "_only", lambda only: [item]), mock.patch.object(meta, "_check_step", lambda s: ""), \
                mock.patch.object(meta, "_stand_in_media", lambda kind: {"hash": "stand"}):
            off = meta.plan(enable_new=False)
            on = meta.plan(enable_new=True)
        self.assertEqual(off["steps"][0].get("status", "PAUSED"), "PAUSED")
        self.assertFalse(off["spend"])
        self.assertEqual(on["steps"][0]["status"], "ACTIVE")
        self.assertTrue(on["spend"])

    def test_reference_documents_cards(self):
        self.assertIn('"cards":[{"media_id"', meta.CHANGE_REFERENCE)


if __name__ == "__main__":
    unittest.main()
