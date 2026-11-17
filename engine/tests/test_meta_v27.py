"""Meta Marketing API v27: Studio says every new default out loud. Run from the engine folder:
    python3 tests/test_meta_v27.py

Offline: the Meta reads Studio's builders make are replaced with fixed answers; nothing is sent anywhere.
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("MSTUDIO_HOME", _tf.mkdtemp(prefix="mstudio-test-"))
_os.environ["MSTUDIO_KEYCHAIN"] = "0"

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
TMP = Path(tempfile.mkdtemp(prefix="meta-v27-test-"))
os.environ["STUDIO_DATA_DIR"] = str(TMP)

import core  # noqa: E402

core.STUDIO_DIR = TMP
import meta_api  # noqa: E402
import meta_ads as meta  # noqa: E402

SRC = {"id": "cr1", "object_story_spec": {"page_id": "p1", "link_data": {"link": "https://example.com/", "message": "Hello",
                                                                       "name": "Head", "image_hash": "h1",
                                                                       "call_to_action": {"type": "LEARN_MORE", "value": {"link": "https://example.com/"}}}}}
DATA = {"campaigns": [], "adsets": [], "ads": [{"id": "a1", "adset_id": "s1", "creative": SRC}], "audiences": []}


def live(level, oid):
    return {"campaign": {"name": "Camp", "objective": "OUTCOME_SALES"},
            "adset": {"name": "Set", "status": "PAUSED", "targeting": {"geo_locations": {"countries": ["GB"]}, "age_min": 18,
                                                                        "age_max": 65, "user_os": ["iOS"]}},
            "ad": {"name": "Ad", "status": "PAUSED", "adset_id": "s1", "creative": SRC}}[level]


class V27(unittest.TestCase):
    def setUp(self):
        self.p = [mock.patch.object(meta, "_live", live), mock.patch.object(meta, "load", lambda *a, **k: DATA),
                  mock.patch.object(meta, "_pixel", lambda: "px1")]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()

    def test_version(self):
        self.assertEqual(meta_api.GRAPH_VERSION, "v27.0")
        self.assertTrue(meta_api.API.endswith("/v27.0"))

    def test_pin_device(self):
        self.assertEqual(meta._pin_device({})["targeting_automation"], {"advantage_audience": 0, "individual_setting": {"device": 0}})
        self.assertEqual(meta._pin_device({"targeting_automation": None})["targeting_automation"]["advantage_audience"], 0)
        t = {"targeting_automation": {"advantage_audience": 1}}
        out = meta._pin_device(t)
        self.assertEqual(out["targeting_automation"], {"advantage_audience": 1, "individual_setting": {"device": 0}})
        self.assertNotIn("individual_setting", t["targeting_automation"])  # the input is not changed
        on = {"targeting_automation": {"advantage_audience": 0, "individual_setting": {"device": 1, "age": 1}}}
        self.assertEqual(meta._pin_device(on), on)  # the user's own choice is kept

    def test_pin_showcase(self):
        self.assertEqual(meta._pin_showcase({})["degrees_of_freedom_spec"],
                         {"creative_features_spec": {"product_extensions": {"enroll_status": "OPT_OUT"}}})
        mine = {"degrees_of_freedom_spec": {"creative_features_spec": {"product_extensions": {"enroll_status": "OPT_IN"},
                                                                        "image_touchups": {"enroll_status": "OPT_IN"}}}}
        self.assertEqual(meta._pin_showcase(dict(mine)), mine)
        other = {"degrees_of_freedom_spec": {"creative_features_spec": {"image_touchups": {"enroll_status": "OPT_OUT"}}}}
        cfs = meta._pin_showcase(other)["degrees_of_freedom_spec"]["creative_features_spec"]
        self.assertEqual(cfs, {"image_touchups": {"enroll_status": "OPT_OUT"}, "product_extensions": {"enroll_status": "OPT_OUT"}})

    def test_new_adset_sets_device_and_advantage_audience(self):
        for aa in (False, True):
            c = meta._normalise({"type": "meta_new_adset", "campaign_id": "111", "name": "S", "daily_budget": 10, "countries": ["gb"],
                                 "advantage_audience": aa})
            p = meta.ops_for(c, {})[0]["params"]
            self.assertEqual(p["targeting"]["targeting_automation"],
                             {"advantage_audience": 1 if aa else 0, "individual_setting": {"device": 0}})

    def test_targeting_updates_and_undo_pin_device(self):
        for c in ({"type": "meta_targeting", "level": "adset", "id": "s1", "countries": ["GB", "DE"]},
                  {"type": "meta_placements", "level": "adset", "id": "s1", "advantage": True}):
            s = meta.ops_for(meta._normalise(c), {})[0]
            self.assertEqual(s["params"]["targeting"]["targeting_automation"]["individual_setting"], {"device": 0})
            self.assertEqual(s["undo"]["targeting"]["targeting_automation"]["individual_setting"], {"device": 0})

    def test_new_creatives_opt_out_of_product_showcase(self):
        steps = [meta.ops_for(meta._normalise({"type": "meta_new_ad", "adset_id": "s1", "name": "N", "from_ad": "a1"}), {})[0],
                 meta.ops_for(meta._normalise({"type": "meta_copy", "level": "ad", "id": "a1", "headline": "New"}), {})[0],
                 meta.ops_for(meta._normalise({"type": "meta_boost_post", "adset_id": "s1", "name": "B", "channel": "facebook",
                                               "post_id": "1_2"}), {})[0]]
        for s in steps:
            self.assertEqual(s["creative"]["degrees_of_freedom_spec"]["creative_features_spec"]["product_extensions"],
                             {"enroll_status": "OPT_OUT"}, s["line"])
        self.assertNotIn("degrees_of_freedom_spec", SRC)  # the cached source creative is not changed

    def test_media_creative_opt_out(self):
        p = meta._media_creative({"link": "https://example.com/", "primary_text": "T", "headline": "H"}, None, "image", "N")
        self.assertEqual(p["degrees_of_freedom_spec"]["creative_features_spec"]["product_extensions"], {"enroll_status": "OPT_OUT"})

    def test_instagram_boost_identity(self):
        """Most ads have no Instagram account: an Instagram boost still finds the pair that has one."""
        ads = [{"creative": {"object_story_spec": {"page_id": "p1"}}}] * 5 + \
              [{"creative": {"object_story_spec": {"page_id": "p1", "instagram_user_id": "ig1"}}}] * 3 + \
              [{"creative": {"object_story_spec": {"page_id": "p2", "instagram_user_id": "ig2"}}}]
        with mock.patch.object(meta, "load", lambda *a, **k: {**DATA, "ads": ads}):
            self.assertEqual(meta._identity(), {"page_id": "p1"})  # other new creatives: unchanged
            self.assertEqual(meta._identity(need_ig=True), {"page_id": "p1", "instagram_user_id": "ig1"})
            s = meta.ops_for(meta._normalise({"type": "meta_boost_post", "adset_id": "s1", "name": "B", "channel": "instagram",
                                              "post_id": "123"}), {})[0]
            self.assertEqual((s["creative"]["object_id"], s["creative"]["instagram_user_id"]), ("p1", "ig1"))
        with mock.patch.object(meta, "load", lambda *a, **k: {**DATA, "ads": ads[:5]}), \
                mock.patch.object(meta, "_page_for_account", lambda d=None: "p1"):
            with self.assertRaises(ValueError):
                meta.ops_for(meta._normalise({"type": "meta_boost_post", "adset_id": "s1", "name": "B", "channel": "instagram",
                                              "post_id": "123"}), {})

    def test_no_lookalike_or_moment_maker_fields(self):
        src = (HERE / "meta_ads.py").read_text()
        for word in ("lookalike_spec", "location_spec", "meta_moment_maker_spec"):
            self.assertNotIn(word, src)


if __name__ == "__main__":
    unittest.main(verbosity=1)
