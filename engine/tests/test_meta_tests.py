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
                {"id": "a3", "name": "Ad three", "adset_id": "s2", "campaign_id": "c1", "status": "PAUSED", "effective_status": "PAUSED", "creative": SRC},
                {"id": "a4", "name": "Ad four", "adset_id": "s2", "campaign_id": "c1", "status": "ACTIVE", "effective_status": "ACTIVE", "creative": SRC}],
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
            return {"id": path, "name": path, "account_id": "123", "effective_status": "ACTIVE"}
        if path in ("a1", "a2", "a3", "a4", "c1"):
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
                 ({"cells": [{"ids": ["s1"]}, {"ids": ["d-zzz"]}]}, "only use ad sets that already exist"),
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

    def test_variant_flow_is_not_available(self):
        for call in (lambda: meta_tests.variant_changes("s1", {"what": "audience", "countries": ["DE"]}, end_date=D(14)),
                     lambda: meta_tests.build("variant", {"adset_id": "s1", "change": {"what": "audience"}})):
            with self.assertRaises(ValueError) as cm:
                call()
            self.assertIn("not available yet", str(cm.exception))
        items = meta.add([{"type": "meta_new_adset", "campaign_id": "c1", "name": "N", "daily_budget": 5, "countries": ["GB"],
                           "from_adset": "s1"},
                          {"type": "meta_new_adset", "campaign_id": "c1", "name": "M", "daily_budget": 5, "countries": ["GB"],
                           "activate_with_test": True},
                          {"type": "meta_new_ad", "adset_id": "s1", "name": "A", "from_ad": "a1", "activate_with_test": True}])
        for it, k in zip(items, ("from_adset", "activate_with_test", "activate_with_test")):
            self.assertTrue(any(x.startswith(k + ":") and "not available yet" in x for x in it["problems"]), it["problems"])
        for it in items:  # refused at push time too, whatever the draft says
            with self.assertRaises(ValueError):
                meta.ops_for(it["change"], {})
        self.assertFalse(hasattr(meta, "_copy_adset_ops"))
        self.assertFalse(hasattr(meta_tests, "variants_of"))

    def test_tests_use_existing_objects_only(self):
        items = meta.add([{"type": "meta_new_ad", "ref": "n1", "adset_id": "s1", "name": "Fresh", "from_ad": "a1"},
                          {"type": "meta_test_creative", "name": "C", "ads": ["a1", "d-n1"], "daily_budget": 20, "end_date": D(10)}])
        self.assertTrue(any("already exist on Meta" in x for x in items[1]["problems"]), items[1]["problems"])

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

    def test_creative_budget_in_zero_decimal_currency(self):
        """JPY / KRW have no minor unit on Meta: 3000 JPY is sent as 3000, never 300000; the minimum is the account's."""
        jpy = {**DATA, "account": {"currency": "JPY", "min_daily_budget": 150}}
        ch = {"type": "meta_test_creative", "name": "C", "ads": ["a1", "a2"], "daily_budget": 3000, "end_date": D(10)}
        with mock.patch.object(meta, "load", lambda *a, **k: jpy):
            c = meta.ops_for(meta._normalise(ch), {})[0]
            self.assertEqual(c["params"]["creative_test_config"], {"daily_budget": 3000})
            self.assertIn("test budget 3000/day", c["line"])
            self.assertEqual(c["spend"], "test budget 3000/day")
            p, _w = meta.check(meta._normalise({**ch, "daily_budget": 100}), jpy, {})
            self.assertTrue(any("at least 150 a day" in x for x in p), p)
            self.assertEqual(meta_tests._money(1234.5, jpy), "1,234")
            self.assertEqual(meta_tests._money(1234.5, DATA), "1,234.50")

    def test_times(self):
        t0 = 1_800_000_000
        st, en = meta_tests._times({"end_date": D(3)}, now=t0)
        self.assertEqual(st, t0 + meta_tests.START_LEAD)
        self.assertEqual(en, int(dt.datetime.combine(TODAY + dt.timedelta(days=3), dt.time(23, 59), dt.timezone.utc).timestamp()))
        st, _ = meta_tests._times({"start_date": D(2), "end_date": D(9)})
        self.assertEqual(st, int(dt.datetime.combine(TODAY + dt.timedelta(days=2), dt.time(0, 0), dt.timezone.utc).timestamp()))

    def test_preview_reads_only_for_tests(self):
        meta.add([{"type": "meta_test_creative", "name": "C", "ads": ["a1", "a2"], "daily_budget": 10, "end_date": D(10)}])
        p = meta.preview()
        self.assertTrue(p["ok"], p)
        self.assertTrue(p["permanent"])
        self.assertEqual(self.f.posts, [], "a test preview sends nothing at all, only reads")

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

    def test_push_creative_end_to_end_and_undo(self):
        meta.add([{"type": "meta_test_creative", "name": "C", "ads": ["a1", "a2"], "daily_budget": 20, "end_date": D(10)}])
        r = meta.push(accept_spend=True)
        self.assertFalse(r["ok"])
        self.assertIn("can't be undone", r["message"])
        self.assertEqual(self.f.posts, [], "nothing sent without the permanent tick")
        r = meta.push(accept_spend=True, accept_permanent=True)
        self.assertTrue(r["ok"], r)
        (path, study, v), = self.f.posts
        self.assertEqual((path, v), ("b1/ad_studies", False))
        self.assertEqual([c["ads"] for c in study["cells"]], [["a1"], ["a2"]])
        self.assertEqual((study["cooldown_start_time"], study["observation_end_time"]), (study["start_time"], study["end_time"]))
        self.assertEqual(study["creative_test_config"], {"daily_budget": 2000})
        self.assertGreater(study["start_time"], time.time())
        local = json.loads((TMP / "meta_tests_meta_123.json").read_text())
        self.assertEqual(local["new1"]["cells"][1], {"name": "Ad 2", "level": "ad", "ids": ["a2"]})
        self.assertNotIn("created", local["new1"])
        h = meta.history()[0]
        self.assertFalse(h["can_undo"], "a test alone can't be undone")
        self.assertEqual(core.draft(meta.acct())["items"], [])

    def test_no_test_post_is_ever_active(self):
        """Nothing Studio creates around a test is Active: every create in a push is Paused."""
        meta.add([{"type": "meta_new_ad", "adset_id": "s1", "name": "Fresh", "from_ad": "a1"},
                  {"type": "meta_test_creative", "name": "C", "ads": ["a1", "a2"], "daily_budget": 5, "end_date": D(10)}])
        r = meta.push(accept_spend=True, accept_permanent=True)
        self.assertTrue(r["ok"], r)
        for path, prm, v in self.f.posts:
            if "status" in prm:
                self.assertEqual(prm["status"], "PAUSED", (path, prm))

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


class Safety(Base):
    """Review fixes kept after the variant flow was taken out: what a made test keeps, stale dates, local note, LOWs."""

    def real(self):
        return [(p_, prm) for p_, prm, v in self.f.posts if not v]

    def refuse(self, pred):
        real_post = self.f.post

        def post(path, params, validate=False, retry_rate=True):
            if not validate and pred(path, params):
                raise meta.MetaError({"message": "refused", "code": 100})
            return real_post(path, params, validate, retry_rate)
        return mock.patch.object(meta, "post", post)

    def test_second_test_refused_keeps_the_first(self):
        meta.add([{"type": "meta_new_campaign", "name": "Other", "objective": "traffic"},
                  {"type": "meta_test_creative", "name": "First", "ads": ["a1", "a2"], "daily_budget": 5, "end_date": D(10)},
                  {"type": "meta_test_creative", "name": "Second", "ads": ["a3", "a4"], "daily_budget": 5, "end_date": D(10)}])
        n = {"k": 0}

        def second(path, params):
            if path == "b1/ad_studies":
                n["k"] += 1
                return n["k"] == 2
            return False
        with self.refuse(second):
            r = meta.push(accept_spend=True, accept_permanent=True)
        self.assertFalse(r["ok"])
        self.assertTrue(r["partial"])
        self.assertEqual(r["kept"], 1)
        self.assertIn("Kept on Meta: 1 change(s)", r["message"])
        self.assertIn("Reversed: 1 of 1", r["message"])
        self.assertEqual([p_ for p_, prm in self.real() if prm == {"status": "DELETED"}], ["new1"])  # the unrelated campaign
        rec = json.loads((TMP / "live_meta_123.jsonl").read_text().splitlines()[-1])
        self.assertTrue(rec["partial"])
        self.assertEqual([o.get("created") for o in rec["ops"]], ["study"])
        self.assertEqual(sorted(i["change"]["type"] for i in core.draft(meta.acct())["items"]),
                         ["meta_new_campaign", "meta_test_creative"])
        self.assertFalse(meta.history()[0]["can_undo"], "only a test was kept: nothing to undo")

    def test_test_then_refused_end(self):
        meta.add([{"type": "meta_test_creative", "name": "C", "ads": ["a1", "a2"], "daily_budget": 5, "end_date": D(10)}])
        meta.add(meta_tests.end_changes("900"))
        with self.refuse(lambda path, params: path == "900"):
            r = meta.push(accept_spend=True, accept_permanent=True)
        self.assertTrue(r["partial"])
        self.assertEqual(r["kept"], 1)

    def test_refused_delete_falls_back_to_paused(self):
        meta.add([{"type": "meta_new_campaign", "name": "Other", "objective": "traffic"},
                  {"type": "meta_test_creative", "name": "C", "ads": ["a1", "a2"], "daily_budget": 5, "end_date": D(10)}])
        with self.refuse(lambda path, params: path == "b1/ad_studies" or (path == "new1" and params == {"status": "DELETED"})):
            r = meta.push(accept_spend=True, accept_permanent=True)
        self.assertFalse(r["ok"])
        self.assertIn("set to Paused instead", r["message"])
        self.assertNotIn("nothing was kept", r["message"])
        self.assertIn(("new1", {"status": "PAUSED"}), self.real())

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
        sib = [{"line": "new", "id": "N", "undo": {"status": "DELETED"}, "created": "ad", "parent": "s1"},
               {"line": "unrelated", "id": "O", "undo": {"status": "DELETED"}, "created": "adset", "parent": "c1"},
               {"line": "test", "id": "T", "undo": None, "created": "study", "uses": ["s1"]}]
        kept, back = meta._keep(sib)  # an existing parent does not pull in its other children
        self.assertEqual([r["line"] for r in back], ["unrelated"])

    def test_stale_dates_are_refused_at_push(self):
        meta.add([{"type": "meta_test_creative", "name": "C", "ads": ["a1", "a2"], "daily_budget": 5, "start_date": D(1),
                   "end_date": D(10)}])
        it = core.draft(meta.acct())["items"][0]
        self.assertEqual(it["problems"], [])
        with core._locked(meta.acct()) as dr:  # days later: the draft's start is now in the past (problems not re-run)
            dr["items"][0]["change"]["start_date"] = D(-2)
        r = meta.push(accept_spend=True, accept_permanent=True)
        self.assertFalse(r["ok"])
        self.assertTrue(any("past" in x for e in r["preview"]["items"] for x in e["problems"]), r["preview"])
        self.assertEqual(self.f.posts, [])

    def test_local_note_failure_still_logs_the_push(self):
        meta.add([{"type": "meta_test_creative", "name": "C", "ads": ["a1", "a2"], "daily_budget": 5, "end_date": D(10)}])
        with mock.patch.object(meta_tests, "_remember", side_effect=OSError("disk full")):
            r = meta.push(accept_spend=True, accept_permanent=True)
        self.assertTrue(r["ok"], r)
        rec = json.loads((TMP / "live_meta_123.jsonl").read_text().splitlines()[-1])
        self.assertEqual(rec["ops"][0]["id"], "new1")
        self.assertIn("could not save its note", rec["ops"][0]["line"])

    def test_paused_cells_warn(self):
        paused = {**DATA, "adsets": [dict(DATA["adsets"][0]), {**DATA["adsets"][1], "status": "PAUSED", "effective_status": "PAUSED"}]}
        with mock.patch.object(meta, "load", lambda *a, **k: paused):
            it = meta.add([{"type": "meta_test_split", "name": "T", "end_date": D(10), "cells": [{"ids": ["s1"]}, {"ids": ["s2"]}]}])[0]
            self.assertTrue(any("is paused" in w for w in it["warnings"]), it["warnings"])
            core.clear(meta.acct())
            it = meta.add([{"type": "meta_test_creative", "name": "C", "ads": ["a1", "a3"], "daily_budget": 5, "end_date": D(10)}])[0]
            self.assertTrue(any("Ad 2" in w and "paused" in w for w in it["warnings"]), it["warnings"])

    def test_lows(self):
        with mock.patch.object(meta, "load", lambda *a, **k: {**DATA, "account": {"timezone_name": "Asia/Bangkok"}}):
            e = meta_tests._epoch(dt.date(2027, 1, 2))
            self.assertEqual(e, int(dt.datetime(2027, 1, 1, 17, 0, tzinfo=dt.timezone.utc).timestamp()))
            self.assertEqual(meta_tests._local_time("2027-01-01T17:00:00+0000"), "2027-01-02 00:00")
        row = meta_tests._row({"id": "77", "type": "SPLIT_TEST_V2", "cells": {"data": [{"id": "x"}, {"id": "y"}]}})
        self.assertEqual(row["label"], "Creative test")
        data = {"study": {"status": "Ended", "kind": "split", "start": "", "cells": [
            {"id": "1", "name": "A", "ids": ["s1"], "names": ["A"], "spend": 10, "impressions": 2000, "results": 4, "cost_per_result": 2.5},
            {"id": "2", "name": "B", "ids": [], "names": [], "spend": 0, "impressions": 0, "results": 0, "cost_per_result": None}]},
            "meta": meta_tests.parse_results(None), "objective": {}, "period": {"since": None}}
        self.assertIn("Ended without results in B", meta_tests.verdict(data)["verdict"])
        meta.add([{"type": "meta_test_split", "name": "One", "end_date": D(10), "cells": [{"ids": ["s1"]}, {"ids": ["s2"]}]}])
        two = meta.add([{"type": "meta_test_split", "name": "Two", "end_date": D(10), "cells": [{"ids": ["s1"]}, {"ids": ["s2"]}]}])[0]
        self.assertTrue(any('also in the drafted test "One"' in x for x in two["problems"]), two["problems"])
        self.assertIn('isMeta() ? "permanent changes" : "removals"', (HERE / "index.html").read_text())
        t = {"age_min": 20, "age_max": 65, "age_range": [30, 55], "targeting_automation": {"advantage_audience": 1}}
        self.assertEqual(meta._targeting(t)["age"], "30-55 (suggestion)")
        js = (HERE / "static/meta_tests.js").read_text()
        self.assertNotIn("variant", js.lower())


class UndoSafety(Base):
    """Undo never switches anything on silently, and pauses made with End test are not undone."""

    def test_end_with_pause_is_not_undone(self):
        items = meta_tests.add("end", {"study_id": "900", "pause": True})
        self.assertEqual([i["change"].get("with_end") for i in items[1:]], ["900", "900"])
        core.remove(meta.acct(), [i["id"] for i in items if i["problems"]])  # s9 is not in this account's data
        r = meta.push(False, False, True)
        self.assertTrue(r["ok"], r)
        rec = json.loads((TMP / "live_meta_123.jsonl").read_text().splitlines()[-1])
        self.assertTrue(all(o["undo"] is None for o in rec["ops"]), rec["ops"])
        self.assertTrue(any("switch it on by hand" in o["line"] for o in rec["ops"]))
        h = meta.history()[0]
        self.assertFalse(h["can_undo"])
        n = len(self.f.posts)
        with self.assertRaises(ValueError):
            meta.undo("nope")
        u = meta.undo(h["id"], accept_spend=True)  # even forced: nothing is switched back on
        self.assertFalse([prm for _, prm, _ in self.f.posts[n:] if prm.get("status") == "ACTIVE"])
        self.assertTrue(any("the test has ended" in x for x in u["not_undone"]), u)

    def test_undo_that_switches_on_needs_the_spend_tick(self):
        meta.add([{"type": "meta_status", "level": "adset", "id": "s1", "status": "paused"}])
        r = meta.push()
        self.assertTrue(r["ok"], r)
        h = meta.history()[0]
        self.assertTrue(h["can_undo"])
        self.assertEqual(len(h["undo_spend"]), 1)
        n = len(self.f.posts)
        u = meta.undo(h["id"])
        self.assertFalse(u["ok"])
        self.assertTrue(u["needs_spend"])
        self.assertEqual(self.f.posts[n:], [], "nothing sent without the tick")
        self.assertTrue(meta.history()[0]["can_undo"], "still undoable after the refusal")
        u = meta.undo(h["id"], accept_spend=True)
        self.assertTrue(u["ok"], u)
        self.assertEqual(self.f.posts[n:], [("s1", {"status": "ACTIVE"}, False)])

    def test_undo_without_switching_on_needs_no_tick(self):
        meta.add([{"type": "meta_rename", "level": "adset", "id": "s1", "new_name": "New"}])
        h = meta.history()[0] if meta.push()["ok"] else None
        self.assertEqual(h["undo_spend"], [])
        self.assertTrue(meta.undo(h["id"])["ok"])

    def test_from_adset_nested_in_a_cell_is_refused(self):
        it = meta.add([{"type": "meta_test_split", "name": "T", "end_date": D(10),
                        "cells": [{"ids": ["s1"]}, {"ids": ["s2"], "from_adset": "s1"}]}])[0]
        self.assertTrue(any(x.startswith("from_adset:") for x in it["problems"]), it["problems"])
        with self.assertRaises(ValueError):
            meta.ops_for(it["change"], {})
        bad = meta.add([{"type": "meta_status", "level": "adset", "id": "s1", "status": "active", "with_end": "900"}])[0]
        self.assertTrue(any("with_end is only for pausing" in x for x in bad["problems"]), bad["problems"])

    def test_undo_route_passes_the_tick(self):
        import server
        src = (HERE / "server.py").read_text()
        self.assertIn('meta.undo(body["push_id"], bool(body.get("accept_spend")))', src)
        self.assertIn("needs_spend", (HERE / "index.html").read_text())


class Mcp(Base):
    def test_tools(self):
        import mcp_server
        names = {t["name"] for t in mcp_server.TOOLS}
        self.assertTrue({"meta_tests_list", "meta_test_results", "meta_test_add_to_draft"} <= names)
        with mock.patch.object(meta, "use", lambda k: "act_123"):
            with self.assertRaises(mcp_server.NotCached):  # cache only: a cold cache asks for refresh=true, fetches nothing
                mcp_server.call("meta_test_results", {"study_id": "902"})
            rows = mcp_server.call("meta_tests_list", {"refresh": True})
            self.assertEqual(len(rows), 3)
            mcp_server.meta_tests._mem.clear()  # the next call reads the file written above
            self.assertEqual(mcp_server.call("meta_tests_list", {}), rows)
            items = mcp_server.call("meta_test_add_to_draft", {"mode": "split", "test": {"name": "T", "ids": ["s1", "s2"], "end_date": D(10)},
                                                                "note": "why"})
            self.assertEqual((items[0]["by"], items[0]["problems"]), ("claude", []))
            self.assertEqual(self.f.posts, [], "the MCP tool only drafts")
            self.assertEqual(mcp_server.call("meta_test_results", {"study_id": "902", "refresh": True})["study"]["status"], "Scheduled")
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
