"""Meta A/B tests and creative tests (meta_tests.py). Run from the engine folder:
    python3 tests/test_meta_tests.py

Offline: every Meta call goes to a fake that records it; nothing is sent anywhere. The fake refuses any POST to a test
endpoint made with validate_only, so a preview that tried one would fail here.
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("MSTUDIO_HOME", _tf.mkdtemp(prefix="mstudio-test-"))
_os.environ["MSTUDIO_KEYCHAIN"] = "0"

import datetime as dt
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
TMP = Path(tempfile.mkdtemp(prefix="meta-tests-test-"))
os.environ["STUDIO_DATA_DIR"] = str(TMP)
os.environ["META_BUSINESS_ID"] = "b1"

import core  # noqa: E402

core.STUDIO_DIR = TMP
import datapolicy  # noqa: E402

datapolicy.DIR = str(TMP)
import meta_ads as meta  # noqa: E402
import meta_tests  # noqa: E402

SRC = {"id": "cr1", "object_story_spec": {"page_id": "p1", "link_data": {"link": "https://example.com/", "message": "Hello\n\nhttps://example.com/",
                                                                       "name": "Head", "image_hash": "h1",
                                                                       "call_to_action": {"type": "LEARN_MORE", "value": {"link": "https://example.com/"}}}}}
TG = {"geo_locations": {"countries": ["GB"], "regions": [{"key": "1"}]}, "age_min": 25, "age_max": 60,
      "custom_audiences": [{"id": "aud1", "name": "Visitors"}], "flexible_spec": [{"interests": [{"id": "i1"}]}]}
DATA = {"account": {"currency": "GBP"},
        "campaigns": [{"id": "c1", "name": "Camp", "status": "ACTIVE", "effective_status": "ACTIVE", "objective": "OUTCOME_SALES"}],
        "adsets": [{"id": "s1", "name": "Set one", "campaign_id": "c1", "status": "ACTIVE", "effective_status": "ACTIVE",
                    "daily_budget": "2000", "optimization_goal": "OFFSITE_CONVERSIONS", "targeting": TG},
                   {"id": "s2", "name": "Set two", "campaign_id": "c1", "status": "ACTIVE", "effective_status": "ACTIVE",
                    "daily_budget": "2000", "optimization_goal": "OFFSITE_CONVERSIONS", "targeting": TG}],
        "ads": [{"id": "a1", "name": "Ad one", "adset_id": "s1", "campaign_id": "c1", "status": "ACTIVE", "effective_status": "ACTIVE", "creative": SRC},
                {"id": "a2", "name": "Ad two", "adset_id": "s1", "campaign_id": "c1", "status": "ACTIVE", "effective_status": "ACTIVE", "creative": SRC},
                {"id": "a3", "name": "Ad three", "adset_id": "s2", "campaign_id": "c1", "status": "PAUSED", "effective_status": "PAUSED", "creative": SRC}],
        "audiences": [], "insights": {}}
TODAY = dt.date.today()
D = lambda n: (TODAY + dt.timedelta(days=n)).isoformat()  # noqa: E731


def iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+0000")


NOW = time.time()
STUDIES = [
    {"id": "900", "name": "Running AB", "type": "SPLIT_TEST", "start_time": iso(NOW - 5 * 86400), "end_time": iso(NOW + 5 * 86400),
     "cells": {"data": [{"id": "c900a", "treatment_percentage": 50, "adsets": {"data": [{"id": "s2", "name": "Set two"}]}},
                        {"id": "c900b", "treatment_percentage": 50, "adsets": {"data": [{"id": "s9", "name": "Other"}]}}]},
     "objectives": {"data": [{"id": "o900", "name": "cost_per_result", "type": "NONSALES", "is_primary": True}]}},
    {"id": "901", "name": "Old campaign test", "type": "SPLIT_TEST_V2", "start_time": iso(NOW - 90 * 86400), "end_time": iso(NOW - 80 * 86400),
     "cells": {"data": [{"id": "c901a", "treatment_percentage": 50, "campaigns": {"data": [{"id": "c1", "name": "Camp"}]}},
                        {"id": "c901b", "treatment_percentage": 50, "campaigns": {"data": [{"id": "c2", "name": "Camp two"}]}}]},
     "objectives": {"data": [{"id": "o901", "name": "cost_per_result", "type": "NONSALES", "is_primary": True}]}},
    {"id": "902", "name": "Later", "type": "SPLIT_TEST", "start_time": iso(NOW + 3 * 86400), "end_time": iso(NOW + 10 * 86400),
     "cells": {"data": []}, "objectives": {"data": []}},
]


class Fake:
    """Records every call. GETs answer from fixtures; POSTs return new ids."""

    def __init__(self):
        self.posts, self.gets, self.n = [], [], 0
        self.insights = {}
        self.objective_results = {}
        self.fail_on = None

    def get(self, path, **params):
        self.gets.append((path, params))
        if path.endswith("/insights"):
            return {"data": self.insights.get(path.split("/")[0], [])}
        if path in ("s1", "s2"):
            if params.get("fields") == meta_tests.meta.F_COPY:
                s = next(x for x in DATA["adsets"] if x["id"] == path)
                return {**s, "billing_event": "IMPRESSIONS", "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
                        "promoted_object": {"pixel_id": "px1", "custom_event_type": "PURCHASE"}, "attribution_spec": [{"event_type": "CLICK_THROUGH", "window_days": 7}]}
            return {"id": path, "name": path, "account_id": "123", "effective_status": "ACTIVE"}
        if path in ("a1", "a2", "a3", "c1"):
            return {"id": path, "name": path, "account_id": "123", "effective_status": "ACTIVE"}
        s = next((x for x in STUDIES if x["id"] == path), None)
        if s:
            return s
        raise AssertionError(f"unexpected GET {path}")

    def get_all(self, path, **params):
        self.gets.append((path, params))
        if path == "b1/ad_studies":
            return STUDIES
        if path.endswith("/objectives"):
            sid = path.split("/")[0]
            objs = next((x["objectives"]["data"] for x in STUDIES if x["id"] == sid), [])
            return [{**o, **({"results": self.objective_results[sid]} if sid in self.objective_results else {})} for o in objs]
        raise AssertionError(f"unexpected GET (all) {path}")

    def post(self, path, params, validate=False, retry_rate=True):
        if "ad_studies" in path or path in {s["id"] for s in STUDIES}:
            if validate:
                raise AssertionError("a test endpoint must never get a validate_only POST")
        self.posts.append((path, json.loads(json.dumps(params)), validate))
        if validate:
            return {"success": True}
        if self.fail_on and self.fail_on in path:
            raise meta.MetaError({"message": "refused", "code": 100})
        self.n += 1
        return {"id": f"new{self.n}", "success": True}


def live(level, oid):
    if level == "campaign":
        return {"name": "Camp", "objective": "OUTCOME_SALES"}
    if level == "ad":
        return {"name": oid, "status": "ACTIVE", "adset_id": "s1", "creative": SRC}
    return {"name": oid, "status": "ACTIVE", "targeting": TG}


class Base(unittest.TestCase):
    def setUp(self):
        self.f = Fake()
        core.clear(meta.acct()) if meta.act() else None
        meta._cur.act = "act_123"
        core.clear(meta.acct())
        meta_tests._mem.clear()
        for f in TMP.glob("meta_tests_*"):
            f.unlink()
        self.p = [mock.patch.object(meta, "load", lambda *a, **k: DATA), mock.patch.object(meta, "get", self.f.get),
                  mock.patch.object(meta, "get_all", self.f.get_all), mock.patch.object(meta, "post", self.f.post),
                  mock.patch.object(meta, "_live", live), mock.patch.object(meta, "_pixel", lambda: "px1"),
                  mock.patch.object(meta, "refresh_account", lambda: {"fetched_at": "x"}),
                  mock.patch.object(meta, "link_problem", lambda u: ""), mock.patch.object(meta, "page_check", lambda t, u: [])]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()


class Checks(Base):
    def add1(self, c):
        return meta.add([c])[0]

    def test_split_equal_shares_and_names(self):
        it = self.add1({"type": "meta_test_split", "name": "T", "level": "adsets", "end_date": D(10),
                        "cells": [{"ids": ["s1"]}, {"id": "s2"}]})
        c = it["change"]
        self.assertEqual(c["level"], "adset")
        self.assertEqual([x["percent"] for x in c["cells"]], [50, 50])
        self.assertEqual([x["name"] for x in c["cells"]], ["Cell A", "Cell B"])
        self.assertEqual(it["problems"], [], it)
        self.assertIn("Meta new A/B test", it["summary"])
        self.assertEqual(meta_tests.split_percent(3), [34, 33, 33])

    def test_split_problems(self):
        cases = [({"cells": [{"ids": ["s1"]}]}, "2-5 cells"),
                 ({"cells": [{"ids": ["s1"], "percent": 95}, {"ids": ["s2"], "percent": 5}]}, "at least 10"),
                 ({"cells": [{"ids": ["s1"], "percent": 60}, {"ids": ["s2"], "percent": 30}]}, "add up to 100"),
                 ({"cells": [{"ids": ["s1"]}, {"ids": ["s1"]}]}, "more than one cell"),
                 ({"cells": [{"ids": ["s1"]}, {"ids": ["nope"]}]}, "not found"),
                 ({"cells": [{"ids": ["s1"]}, {"ids": ["d-zzz"]}]}, "not a new ad set"),
                 ({"end_date": D(-1)}, "end date must be after"),
                 ({"start_date": D(-2)}, "past"),
                 ({"end_date": None}, "needs an end date"),
                 ({"name": ""}, "needs a name"),
                 ({"level": "ad"}, "level must be")]
        for extra, want in cases:
            c = {"type": "meta_test_split", "name": "T", "level": "adset", "end_date": D(10), "cells": [{"ids": ["s1"]}, {"ids": ["s2"]}], **extra}
            it = self.add1(c)
            self.assertTrue(any(want in p for p in it["problems"]), (extra, it["problems"]))
        it = self.add1({"type": "meta_test_split", "name": "T", "end_date": D(60), "cells": [{"ids": ["s1"]}, {"ids": ["s2"]}]})
        self.assertTrue(any("up to 30 days" in w for w in it["warnings"]))
        it = self.add1({"type": "meta_test_split", "name": "T", "end_date": D(2), "cells": [{"ids": ["s1"]}, {"ids": ["s2"]}]})
        self.assertTrue(any("at least 7 days" in w for w in it["warnings"]))

    def test_creative_checks(self):
        ok = self.add1({"type": "meta_test_creative", "name": "C", "ads": ["a1", "a2"], "daily_budget": 30, "end_date": D(10)})
        self.assertEqual(ok["problems"], [])
        self.assertIn("2 ads", ok["summary"])
        for extra, want in (({"ads": ["a1"]}, "2-7 ads"), ({"ads": ["a1"] * 8}, "2-7 ads"), ({"ads": ["a1", "a1"]}, "twice"),
                            ({"daily_budget": None}, "either daily_budget"), ({"budget_percent": 10}, "either daily_budget"),
                            ({"daily_budget": 0.5}, "at least 1.00")):
            c = {"type": "meta_test_creative", "name": "C", "ads": ["a1", "a2"], "daily_budget": 30, "end_date": D(10), **extra}
            if c["daily_budget"] is None:
                c.pop("daily_budget")
            it = self.add1(c)
            self.assertTrue(any(want in p for p in it["problems"]), (extra, it["problems"]))
        core.clear(meta.acct())
        mixed = self.add1({"type": "meta_test_creative", "name": "C", "ads": ["a1", "a3"], "budget_percent": 20, "end_date": D(10)})
        self.assertEqual(mixed["problems"], [])
        self.assertTrue(any("different ad sets" in w for w in mixed["warnings"]))

    def test_end_check(self):
        it = self.add1({"type": "meta_test_end", "study_id": 900})
        self.assertEqual(it["change"]["study_id"], "900")
        self.assertEqual(it["problems"], [])
        self.assertTrue(any("final" in w for w in it["warnings"]))
        self.assertTrue(self.add1({"type": "meta_test_end", "study_id": "x"})["problems"])

    def test_variant_builder(self):
        ch = meta_tests.variant_changes("s1", {"what": "audience", "countries": ["de"], "age_min": 30}, end_date=D(14))
        self.assertEqual([c["type"] for c in ch], ["meta_new_adset", "meta_new_ad", "meta_new_ad", "meta_test_split"])
        self.assertEqual(ch[0]["from_adset"], "s1")
        self.assertEqual(ch[-1]["cells"][1]["ids"], ["d-v1"])
        items = meta.add(ch)
        self.assertEqual([i["problems"] for i in items], [[]] * 4, [i["problems"] for i in items])
        new_set = items[0]["id"]
        self.assertEqual(items[1]["change"]["adset_id"], f"d-{new_set}")
        self.assertEqual(items[-1]["change"]["cells"][1]["ids"], [f"d-{new_set}"])  # the ref became the draft item id
        self.assertIn("copy of ad set", items[0]["summary"])
        cr = meta_tests.variant_changes("s1", {"what": "creative", "headline": "New head"}, end_date=D(14))
        self.assertTrue(all(c.get("headline") == "New head" for c in cr if c["type"] == "meta_new_ad"))
        for bad in ({"what": "colour"}, {"what": "audience"}, {"what": "audience", "budget": 3}, {"what": "optimisation", "x": 1}):
            with self.assertRaises(ValueError):
                meta_tests.variant_changes("s1", bad, end_date=D(14))
        with self.assertRaises(ValueError):
            meta_tests.build("nope", {})

    def test_end_changes_pause(self):
        out = meta_tests.end_changes("900", pause=True)
        self.assertEqual(out[0], {"type": "meta_test_end", "study_id": "900", "name": "Running AB"})
        self.assertEqual({(c["level"], c["id"]) for c in out[1:]}, {("adset", "s2"), ("adset", "s9")})


class Live(Base):
    def test_ops_split_and_creative_params(self):
        s = meta.ops_for(meta._normalise({"type": "meta_test_split", "name": "T", "level": "adset", "end_date": D(10),
                                          "cells": [{"ids": ["s1"], "percent": 70}, {"ids": ["s2"], "percent": 30}]}), {})[0]
        self.assertEqual(s["kind"], "study")
        self.assertTrue(s["permanent"])
        self.assertEqual(s["params"]["type"], "SPLIT_TEST")
        self.assertEqual(s["params"]["cells"][0], {"name": "Cell A", "treatment_percentage": 70, "adsets": ["s1"]})
        self.assertIn("no validate_only", s["line"])
        c = meta.ops_for(meta._normalise({"type": "meta_test_creative", "name": "C", "ads": ["a1", "a2", "a3"], "daily_budget": 15,
                                          "end_date": D(10)}), {})[0]
        self.assertEqual(c["params"]["type"], "SPLIT_TEST_V2")
        self.assertEqual(c["params"]["creative_test_config"], {"daily_budget": 1500})
        self.assertEqual([x["treatment_percentage"] for x in c["params"]["cells"]], [34, 33, 33])
        self.assertEqual([x["ads"] for x in c["params"]["cells"]], [["a1"], ["a2"], ["a3"]])
        self.assertTrue(c["spend"])

    def test_times(self):
        t0 = 1_800_000_000
        st, en = meta_tests._times({"end_date": D(3)}, now=t0)
        self.assertEqual(st, t0 + meta_tests.START_LEAD)
        self.assertEqual(en, int(dt.datetime.combine(TODAY + dt.timedelta(days=3), dt.time(23, 59), dt.timezone.utc).timestamp()))
        st, _ = meta_tests._times({"start_date": D(2), "end_date": D(9)})
        self.assertEqual(st, int(dt.datetime.combine(TODAY + dt.timedelta(days=2), dt.time(0, 0), dt.timezone.utc).timestamp()))

    def test_preview_reads_only_for_tests(self):
        meta.add(meta_tests.variant_changes("s1", {"what": "placement", "advantage": False, "publisher_platforms": ["facebook"]},
                                            end_date=D(14)))
        p = meta.preview()
        self.assertTrue(p["ok"], p)
        self.assertTrue(p["permanent"])
        self.assertTrue(any(e["permanent"] for e in p["items"]))
        self.assertFalse(any("ad_studies" in path for path, _, _ in self.f.posts))
        self.assertTrue(all(v for _, _, v in self.f.posts), "the preview only sends validate_only")
        adset_checks = [prm for path, prm, v in self.f.posts if path == "act_123/adsets"]
        self.assertEqual(len(adset_checks), 1)
        prm = adset_checks[0]
        self.assertEqual(prm["status"], "ACTIVE")  # the variant runs inside the test window (M3)
        self.assertTrue(p["spend"])
        self.assertEqual(prm["targeting"]["publisher_platforms"], ["facebook"])
        self.assertEqual(prm["targeting"]["custom_audiences"], TG["custom_audiences"])  # the copy keeps the rest
        self.assertEqual(prm["targeting"]["flexible_spec"], TG["flexible_spec"])
        self.assertEqual(prm["daily_budget"], 2000)
        self.assertEqual(prm["promoted_object"], {"pixel_id": "px1", "custom_event_type": "PURCHASE"})
        self.assertEqual(prm["attribution_spec"], [{"event_type": "CLICK_THROUGH", "window_days": 7}])
        self.assertEqual(prm["targeting"]["targeting_automation"]["individual_setting"], {"device": 0})

    def test_preview_refuses_busy_objects(self):
        meta.add([{"type": "meta_test_split", "name": "T", "level": "adset", "end_date": D(10), "cells": [{"ids": ["s1"]}, {"ids": ["s2"]}]}])
        p = meta.preview()
        self.assertFalse(p["ok"])
        self.assertTrue(any("Running AB" in x for e in p["items"] for x in e["problems"]), p)

    def test_preview_refuses_other_account(self):
        orig = self.f.get
        self.f.get = lambda path, **k: {**orig(path, **k), "account_id": "999"} if path == "a1" else orig(path, **k)
        with mock.patch.object(meta, "get", self.f.get):
            meta.add([{"type": "meta_test_creative", "name": "C", "ads": ["a1", "a2"], "daily_budget": 10, "end_date": D(10)}])
            p = meta.preview()
        self.assertTrue(any("not in this ad account" in x for e in p["items"] for x in e["problems"]), p)

    def test_push_variant_end_to_end_and_undo(self):
        meta.add(meta_tests.variant_changes("s1", {"what": "audience", "countries": ["DE"]}, test_name="Geo", end_date=D(14)))
        r = meta.push(accept_spend=True)
        self.assertFalse(r["ok"])
        self.assertIn("can't be undone", r["message"])
        self.assertEqual([x for x in self.f.posts if not x[2]], [], "nothing was sent for real without the tick")
        r = meta.push(accept_spend=True, accept_permanent=True)
        self.assertTrue(r["ok"], r)
        real = [(path, prm) for path, prm, v in self.f.posts if not v]
        self.assertEqual([p for p, _ in real], ["act_123/adsets", "act_123/adcreatives", "act_123/ads", "act_123/adcreatives",
                                                "act_123/ads", "b1/ad_studies"])
        study = real[-1][1]
        self.assertEqual(study["type"], "SPLIT_TEST")
        self.assertEqual(study["cells"][0]["adsets"], ["s1"])
        self.assertEqual(study["cells"][1]["adsets"], ["new1"])  # the variant made earlier in this push
        self.assertGreater(study["start_time"], time.time())
        self.assertNotIn("cooldown_start_time", study)
        self.assertEqual(real[0][1]["targeting"]["geo_locations"], {"countries": ["DE"]})
        self.assertEqual(real[2][1]["adset_id"], "new1")
        local = json.loads((TMP / "meta_tests_meta_123.json").read_text())
        self.assertEqual(local["new6"]["cells"][1], {"name": "Variant", "level": "adset", "ids": ["new1"]})
        h = meta.history()[0]
        self.assertTrue(h["can_undo"])
        self.f.posts.clear()
        u = meta.undo(r["push_id"])
        self.assertFalse(u["ok"])
        self.assertTrue(any("can't be removed from Meta" in x for x in u["not_undone"]), u)
        self.assertEqual(sorted(p for p, _, _ in self.f.posts), ["new1", "new3", "new5"])  # ads + ad set deleted, test left
        self.assertTrue(all(prm == {"status": "DELETED"} for _, prm, _ in self.f.posts))

    def test_creative_push_with_new_ad_and_rollback(self):
        meta.add([{"type": "meta_new_ad", "ref": "n1", "adset_id": "s1", "name": "Fresh", "from_ad": "a1", "headline": "Other"},
                  {"type": "meta_test_creative", "name": "C", "ads": ["a1", "d-n1"], "daily_budget": 20, "end_date": D(10)}])
        d = core.draft(meta.acct())["items"]
        self.assertEqual(d[1]["change"]["ads"], ["a1", "d-" + d[0]["id"]])
        r = meta.push(accept_spend=True, accept_permanent=True)
        self.assertTrue(r["ok"], r)
        study = [prm for p, prm, v in self.f.posts if p == "b1/ad_studies"][0]
        self.assertEqual(study["cells"][1]["ads"], ["new2"])
        self.assertEqual((study["cooldown_start_time"], study["observation_end_time"]), (study["start_time"], study["end_time"]))
        self.assertEqual(study["creative_test_config"], {"daily_budget": 2000})
        # Meta refuses the test: the new ad made in this push is deleted again, nothing kept
        core.clear(meta.acct())
        self.f.posts.clear()
        self.f.fail_on = "ad_studies"
        meta.add([{"type": "meta_new_ad", "ref": "n1", "adset_id": "s1", "name": "Fresh", "from_ad": "a1"},
                  {"type": "meta_test_creative", "name": "C", "ads": ["a1", "d-n1"], "daily_budget": 20, "end_date": D(10)}])
        r = meta.push(accept_spend=True, accept_permanent=True)
        self.assertFalse(r["ok"])
        self.assertIn("refused", r["message"])
        self.assertEqual([(p, prm) for p, prm, v in self.f.posts if prm == {"status": "DELETED"}], [("new5", {"status": "DELETED"})])  # new4 is its creative

    def test_end_test(self):
        meta.add(meta_tests.end_changes("900"))
        p = meta.preview()
        self.assertTrue(p["ok"] and p["permanent"], p)
        self.assertEqual(self.f.posts, [])
        r = meta.push(accept_permanent=True)
        self.assertTrue(r["ok"], r)
        (path, prm, v), = self.f.posts
        self.assertEqual((path, v), ("900", False))
        self.assertAlmostEqual(prm["end_time"], time.time() + meta_tests.END_LEAD, delta=5)
        self.assertNotIn("observation_end_time", prm)
        self.assertFalse(meta.history()[0]["can_undo"])
        for sid, want in (("901", "ended already"), ("902", "has not started")):
            core.clear(meta.acct())
            meta.add([{"type": "meta_test_end", "study_id": sid}])
            p = meta.preview()
            self.assertTrue(any(want in x for e in p["items"] for x in e["problems"]), p)

    def test_end_v2_moves_observation_end(self):
        s = meta.ops_for({"type": "meta_test_end", "study_id": "900"}, {})[0]
        s["has_observation"] = True
        rec = meta_tests.do(s, {})
        self.assertIsNone(rec["undo"])
        self.assertEqual(self.f.posts[0][1]["observation_end_time"], self.f.posts[0][1]["end_time"])


class Reading(Base):
    def test_list(self):
        rows = meta_tests.studies()
        self.assertEqual([r["status"] for r in rows], ["Scheduled", "Running", "Ended"])
        r = rows[1]
        self.assertEqual((r["label"], r["cells"][0]["level"], r["cells"][0]["ids"], r["cells"][0]["names"]), ("A/B test", "adset", ["s2"], ["Set two"]))
        self.assertEqual(rows[2]["cells"][0]["level"], "campaign")
        n = len(self.f.gets)
        meta_tests.studies()
        self.assertEqual(len(self.f.gets), n, "cached: a second look pulls nothing")
        meta_tests.studies(refresh=True)
        self.assertEqual(len(self.f.gets), n + 1)
        self.assertEqual(meta_tests.status_of({"canceled_time": "x"}), "Cancelled")

    def test_parse_results(self):
        r = meta_tests.parse_results([json.dumps({"high_performer_ids": ["a1"], "confidence_by_ad_object_id": {"a1": 0.92, "a2": "0.4"},
                                                  "winner_ad_object_id": None})])
        self.assertEqual((r["high_performer_ids"], r["confidence"], r["winner"]), (["a1"], {"a1": 92.0, "a2": 40.0}, None))
        r = meta_tests.parse_results([{"confidence_by_ad_object_id": [{"ad_object_id": "a3", "confidence": 71}]}])
        self.assertEqual(r["confidence"], {"a3": 71.0})
        self.assertFalse(meta_tests.parse_results(None)["available"])

    def ins(self, spend, imp, clicks, res):
        return [{"spend": str(spend), "impressions": str(imp), "clicks": str(clicks),
                 "actions": [{"action_type": "offsite_conversion.fb_pixel_purchase", "value": str(res)}] if res else []}]

    def test_split_verdicts(self):
        self.f.insights = {"s2": self.ins(100, 20000, 400, 50), "s9": self.ins(100, 20000, 300, 25)}
        x = meta_tests.results("900")
        a, b = x["study"]["cells"]
        self.assertEqual((a["cost_per_result"], b["cost_per_result"], a["ctr"]), (2.0, 4.0, 2.0))
        self.assertIn('"Set two" gets results for less', x["verdict"])
        self.assertFalse(x["thin"])
        self.assertTrue(any("not Meta's significance test" in n for n in x["notes"]))
        self.assertEqual(self.f.gets[-1][1]["time_range"]["until"], TODAY.isoformat())
        self.f.insights = {"s2": self.ins(100, 20000, 400, 5), "s9": self.ins(100, 20000, 300, 3)}
        x = meta_tests.results("900", refresh=True)
        self.assertTrue(x["thin"])
        self.assertIn("Too early to call", x["verdict"])
        self.assertIn("Wait for at least", x["verdict"])
        ended = {**x, "study": {**x["study"], "status": "Ended"}}
        self.assertIn("Not enough data to call", meta_tests.verdict(ended)["verdict"])
        self.f.insights = {"s2": self.ins(100, 20000, 400, 50), "s9": self.ins(104, 20000, 300, 50)}
        self.assertIn("Too close to call", meta_tests.results("900", refresh=True)["verdict"])
        self.f.insights = {}
        self.assertIn("No delivery yet", meta_tests.results("900", refresh=True)["verdict"])
        self.assertIn("Not started yet", meta_tests.results("902")["verdict"])
        with self.assertRaises(ValueError):
            meta_tests.results("../x")

    def test_creative_verdicts(self):
        meta_tests._remember("900", {"kind": "creative", "cells": [{"name": "Ad 1", "level": "ad", "ids": ["a1"]},
                                                                   {"name": "Ad 2", "level": "ad", "ids": ["a2"]}]})
        self.f.objective_results["900"] = [{"high_performer_ids": ["a2"], "confidence_by_ad_object_id": {"a2": 0.88, "a1": 0.12}}]
        self.f.insights = {"a1": self.ins(10, 400, 9, 1), "a2": self.ins(10, 400, 20, 3)}
        x = meta_tests.results("900")
        self.assertEqual(x["study"]["label"], "Creative test")
        self.assertIn("Meta's high performer: Ad two (confidence 88%)", x["verdict"])
        self.assertEqual(x["leader"], "a2")
        self.assertTrue(x["thin"])  # under 1,000 impressions
        self.f.objective_results["900"] = []
        self.assertIn("no results for this test yet", meta_tests.results("900", refresh=True)["verdict"])


class Routes(Base):
    def test_http_routes(self):
        import http.client
        import threading
        from http.server import ThreadingHTTPServer
        import server

        def use(k):
            meta._cur.act = "act_123"
            return "act_123"
        srv = ThreadingHTTPServer(("127.0.0.1", 0), server.H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with mock.patch.object(meta, "use", use), mock.patch.object(meta, "is_meta", lambda k: str(k or "").startswith("meta")):
                def call(method, path, body=None):
                    c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
                    c.request(method, path, json.dumps(body) if body else None,
                              {"Content-Type": "application/json", "X-Studio-Press": server.PRESS} if body else {})
                    r = c.getresponse()
                    return r.status, json.loads(r.read())
                st, rows = call("GET", "/api/meta/tests?acct=meta")
                self.assertEqual((st, len(rows)), (200, 3))
                st, res = call("GET", "/api/meta/tests/results?acct=meta&id=902")
                self.assertEqual((st, res["study"]["status"]), (200, "Scheduled"))
                st, items = call("POST", "/api/meta/tests/add", {"acct": "meta", "mode": "end", "test": {"study_id": "900"}})
                self.assertEqual((st, items[0]["change"]["type"]), (200, "meta_test_end"))
                st, _ = call("POST", "/api/meta/tests/refresh", {"acct": "meta"})
                self.assertEqual(st, 200)
                st, page = 0, None
                c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
                c.request("GET", "/static/meta_tests.js")
                self.assertEqual(c.getresponse().status, 200)
            self.assertEqual(self.f.posts, [], "routes only read and draft")
        finally:
            srv.shutdown()
            srv.server_close()


class ReviewFixes(Base):
    """Fixes from the independent review: M1 copy fields, M2 keep what a made test uses, M3 variants deliver."""

    def test_m1_copy_keeps_regional_and_dsa_fields(self):
        extra = {"regional_regulated_categories": ["SINGAPORE_UNIVERSAL"], "dsa_beneficiary": "B", "dsa_payor": "P",
                 "regional_regulation_identities": {"singapore_universal_beneficiary": "1", "singapore_universal_payer": "1"}}
        orig = self.f.get
        with mock.patch.object(meta, "get", lambda path, **k: {**orig(path, **k), **extra} if path == "s1" else orig(path, **k)):
            st = meta.ops_for(meta._normalise({"type": "meta_new_adset", "from_adset": "s1", "campaign_id": "c1", "name": "V"}), {})[0]
        for k, v in extra.items():
            self.assertEqual(st["params"][k], v, k)
        self.assertIn("regional_regulation_identities", meta.F_COPY)

    def test_m1_age_on_advantage_audience_is_a_suggestion(self):
        adv = {"age_min": 20, "age_max": 65, "age_range": [20, 65], "targeting_automation": {"advantage_audience": 1}}
        tg, txt = meta._set_age(adv, 30, 50)
        self.assertEqual((tg["age_range"], tg["age_min"], tg["age_max"]), ([30, 50], 20, 65))
        self.assertIn("suggestion", txt)
        tg, _ = meta._set_age(adv, 18, None)
        self.assertEqual((tg["age_range"], tg["age_min"]), ([18, 65], 18))  # hard minimum lowered under a lower suggestion
        tg, txt = meta._set_age({"age_min": 25, "age_max": 60, "age_range": [25, 60]}, 30, 55)
        self.assertEqual((tg["age_min"], tg["age_max"], "age_range" in tg), (30, 55, False))
        self.assertEqual(adv["age_range"], [20, 65])  # the input is not changed

    def test_m3_variant_runs_in_the_test_window(self):
        meta.add(meta_tests.variant_changes("s1", {"what": "audience", "countries": ["DE"]}, end_date=D(14)))
        r = meta.push(accept_permanent=True)
        self.assertFalse(r["ok"])
        self.assertIn("raise spend", r["message"])  # Active variants need the spend tick
        r = meta.push(accept_spend=True, accept_permanent=True)
        self.assertTrue(r["ok"], r)
        real = [(p_, prm) for p_, prm, v in self.f.posts if not v]
        aset, study = real[0][1], real[-1][1]
        self.assertEqual(aset["status"], "ACTIVE")
        self.assertEqual((aset["start_time"], aset["end_time"]), (study["start_time"], study["end_time"]))  # one clock
        self.assertTrue(all(prm["status"] == "ACTIVE" for p_, prm in real if p_ == "act_123/ads"))

    def test_m3_paused_cells_warn_and_activate_rules(self):
        paused = {**DATA, "adsets": [dict(DATA["adsets"][0]), {**DATA["adsets"][1], "status": "PAUSED", "effective_status": "PAUSED"}]}
        with mock.patch.object(meta, "load", lambda *a, **k: paused):
            it = meta.add([{"type": "meta_test_split", "name": "T", "end_date": D(10), "cells": [{"ids": ["s1"]}, {"ids": ["s2"]}]}])[0]
            self.assertTrue(any("is paused" in w for w in it["warnings"]), it["warnings"])
            core.clear(meta.acct())
            it = meta.add([{"type": "meta_test_creative", "name": "C", "ads": ["a1", "a3"], "daily_budget": 5, "end_date": D(10)}])[0]
            self.assertTrue(any("Ad 2" in w and "paused" in w for w in it["warnings"]), it["warnings"])
        core.clear(meta.acct())
        items = meta.add([{"type": "meta_new_adset", "ref": "x", "from_adset": "s1", "name": "Plain copy"},
                          {"type": "meta_test_split", "name": "T", "end_date": D(10), "cells": [{"ids": ["s1"]}, {"ids": ["d-x"]}]}])
        self.assertTrue(any("created Paused" in w for w in items[1]["warnings"]), items[1]["warnings"])
        core.clear(meta.acct())
        bad = meta.add([{"type": "meta_new_adset", "campaign_id": "c1", "name": "N", "daily_budget": 5, "countries": ["GB"],
                         "activate_with_test": True},
                        {"type": "meta_new_ad", "adset_id": "s1", "name": "A", "from_ad": "a1", "activate_with_test": True}])
        self.assertTrue(any("only for a variant" in x for x in bad[0]["problems"]), bad[0]["problems"])
        self.assertTrue(any("needs the test's end_date" in x for x in bad[0]["problems"]))
        self.assertTrue(any("only for ads in a new variant" in x for x in bad[1]["problems"]), bad[1]["problems"])

    def two_tests(self):
        meta.add([{"type": "meta_new_campaign", "name": "Other", "objective": "traffic"}])
        meta.add(meta_tests.variant_changes("s1", {"what": "audience", "countries": ["DE"]}, test_name="First", end_date=D(14)))
        meta.add([{"type": "meta_test_creative", "name": "Second", "ads": ["a3", "a2"], "daily_budget": 5, "end_date": D(10)}])

    def test_m2_second_test_refused_keeps_first_and_what_it_uses(self):
        self.two_tests()
        calls = {"n": 0}
        real_post = self.f.post

        def post(path, params, validate=False, retry_rate=True):
            if path == "b1/ad_studies" and not validate:
                calls["n"] += 1
                if calls["n"] == 2:
                    raise meta.MetaError({"message": "second test refused", "code": 100})
            return real_post(path, params, validate, retry_rate)
        with mock.patch.object(meta, "post", post):
            r = meta.push(accept_spend=True, accept_permanent=True)
        self.assertFalse(r["ok"])
        self.assertTrue(r["partial"])
        made = [(p_, prm) for p_, prm, v in self.f.posts if not v and prm != {"status": "DELETED"}]
        deleted = [p_ for p_, prm, v in self.f.posts if prm == {"status": "DELETED"}]
        self.assertEqual(deleted, ["new1"], "only the unrelated new campaign is reversed")
        self.assertEqual(r["kept"], 4)  # the variant ad set, its 2 ads and the first test
        self.assertIn("Kept on Meta: 4 change(s)", r["message"])
        self.assertIn("Reversed: 1 of 1", r["message"])
        self.assertNotIn("nothing was kept", r["message"])
        rec = json.loads((TMP / "live_meta_123.jsonl").read_text().splitlines()[-1])
        self.assertTrue(rec["partial"])
        self.assertEqual(len(rec["ops"]), 4)
        self.assertTrue(any(o.get("created") == "study" for o in rec["ops"]))
        self.assertEqual(len(rec["rolled_back"]), 1)
        left = [i["change"]["type"] for i in core.draft(meta.acct())["items"]]
        self.assertEqual(sorted(left), ["meta_new_campaign", "meta_test_creative"])  # kept items left the draft
        h = meta.history()[0]
        self.assertTrue(h["can_undo"] and h["partial"])
        self.assertIn("End the test first", h["undo_warning"])
        u = meta.undo(h["id"])
        self.assertTrue(any("End the test first" in x for x in u["not_undone"]))

    def test_m2_test_then_refused_end(self):
        meta.add(meta_tests.variant_changes("s1", {"what": "audience", "countries": ["DE"]}, end_date=D(14)))
        meta.add(meta_tests.end_changes("900"))
        real_post = self.f.post
        with mock.patch.object(meta, "post", lambda path, params, validate=False, retry_rate=True:
                               (_ for _ in ()).throw(meta.MetaError({"message": "end refused", "code": 100}))
                               if path == "900" and not validate else real_post(path, params, validate, retry_rate)):
            r = meta.push(accept_spend=True, accept_permanent=True)
        self.assertTrue(r["partial"])
        self.assertEqual(r["kept"], 4)
        self.assertFalse([p_ for p_, prm, v in self.f.posts if prm == {"status": "DELETED"}])

    def test_keep_rules(self):
        done = [{"line": "camp", "id": "C", "undo": {"status": "DELETED"}, "created": "campaign"},
                {"line": "set", "id": "S", "undo": {"status": "DELETED"}, "created": "adset", "parent": "C"},
                {"line": "ad", "id": "A", "undo": {"status": "DELETED"}, "created": "ad", "parent": "S"},
                {"line": "other", "id": "O", "undo": {"status": "DELETED"}, "created": "adset", "parent": "c1"},
                {"line": "edit", "id": "s2", "undo": {"status": "ACTIVE"}},
                {"line": "test", "id": "T", "undo": None, "created": "study", "uses": ["S"]}]
        kept, back = meta._keep(done)
        self.assertEqual([r["line"] for r in kept], ["camp", "set", "ad", "test"])
        self.assertEqual([r["line"] for r in back], ["other", "edit"])
        self.assertEqual(meta._keep(done[:5]), ([], done[:5]))

    def test_lows(self):
        # account time zone: days are midnight there
        with mock.patch.object(meta, "load", lambda *a, **k: {**DATA, "account": {"timezone_name": "Asia/Bangkok"}}):
            e = meta_tests._epoch(dt.date(2027, 1, 2))
            self.assertEqual(e, int(dt.datetime(2027, 1, 1, 17, 0, tzinfo=dt.timezone.utc).timestamp()))
            self.assertEqual(meta_tests._local_time("2027-01-01T17:00:00+0000"), "2027-01-02 00:00")
        # Ads Manager creative tests: no ad sets or campaigns in the cells
        row = meta_tests._row({"id": "77", "type": "SPLIT_TEST_V2", "cells": {"data": [{"id": "x"}, {"id": "y"}]}})
        self.assertEqual(row["label"], "Creative test")
        # ended split test with an empty cell
        data = {"study": {"status": "Ended", "kind": "split", "start": "", "cells": [
            {"id": "1", "name": "A", "ids": ["s1"], "names": ["A"], "spend": 10, "impressions": 2000, "results": 4, "cost_per_result": 2.5},
            {"id": "2", "name": "B", "ids": [], "names": [], "spend": 0, "impressions": 0, "results": 0, "cost_per_result": None}]},
            "meta": meta_tests.parse_results(None), "objective": {}, "period": {"since": None}}
        self.assertIn("Ended without results in B", meta_tests.verdict(data)["verdict"])
        data["study"]["cells"][0].update(spend=0, impressions=0)
        self.assertIn("Ended without results", meta_tests.verdict(data)["verdict"])
        # two drafted tests on the same ad set
        meta.add([{"type": "meta_test_split", "name": "One", "end_date": D(10), "cells": [{"ids": ["s1"]}, {"ids": ["s2"]}]}])
        two = meta.add([{"type": "meta_test_split", "name": "Two", "end_date": D(10), "cells": [{"ids": ["s1"]}, {"ids": ["s2"]}]}])[0]
        self.assertTrue(any('also in the drafted test "One"' in x for x in two["problems"]), two["problems"])
        # push tick wording
        self.assertIn('isMeta() ? "permanent changes" : "removals"', (HERE / "index.html").read_text())


class Mcp(Base):
    def test_tools(self):
        import mcp_server
        names = {t["name"] for t in mcp_server.TOOLS}
        self.assertTrue({"meta_tests_list", "meta_test_results", "meta_test_add_to_draft"} <= names)
        with mock.patch.object(meta, "use", lambda k: "act_123"):
            rows = mcp_server.call("meta_tests_list", {})
            self.assertEqual(len(rows), 3)
            items = mcp_server.call("meta_test_add_to_draft", {"mode": "split", "test": {"name": "T", "ids": ["s1", "s2"], "end_date": D(10)},
                                                                "note": "why"})
            self.assertEqual((items[0]["by"], items[0]["problems"]), ("claude", []))
            self.assertEqual(self.f.posts, [], "the MCP tool only drafts")
            self.assertEqual(mcp_server.call("meta_test_results", {"study_id": "902"})["study"]["status"], "Scheduled")
        desc = next(t for t in mcp_server.TOOLS if t["name"] == "add_to_draft")["description"]
        self.assertIn("meta_test_split", desc)

    def test_no_em_dash_or_names(self):
        for f in ("meta_tests.py", "static/meta_tests.js"):
            txt = (HERE / f).read_text()
            self.assertNotIn("\u2014", txt, f)
            self.assertNotIn("ML Suite", txt, f)
            self.assertNotIn("Liveaboard", txt, f)


if __name__ == "__main__":
    unittest.main()
