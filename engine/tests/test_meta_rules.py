"""Meta ads engine rules (v0.2.3): broken items never crash a preview, ages, budgets and currencies, links, names,
special ad categories, placements, lifetime budgets, lead form ad sets, copy lengths and the call to action list.
Run from the engine folder:  python3 -m unittest tests.test_meta_rules

Offline: fake data only; Meta reads are replaced with fixed answers and nothing is sent anywhere.
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("MSTUDIO_HOME", _tf.mkdtemp(prefix="mstudio-test-"))
_os.environ["MSTUDIO_KEYCHAIN"] = "0"

import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
TMP = Path(tempfile.mkdtemp(prefix="meta-rules-test-"))
os.environ["STUDIO_DATA_DIR"] = str(TMP)

import core  # noqa: E402

core.STUDIO_DIR = TMP
import meta_ads as meta  # noqa: E402

TODAY = dt.date.today()


def D(n):
    return (TODAY + dt.timedelta(days=n)).isoformat()


SRC = {"id": "cr1", "object_story_spec": {"page_id": "p1", "link_data": {"link": "https://example.com/", "message": "Hello",
                                                                       "name": "Head", "image_hash": "h1",
                                                                       "call_to_action": {"type": "LEARN_MORE", "value": {"link": "https://example.com/"}}}}}
TG = {"geo_locations": {"countries": ["GB", "DE"]}, "age_min": 18, "age_max": 65}
TG_ADV = {"geo_locations": {"countries": ["GB"]}, "age_min": 18, "age_max": 65, "age_range": [20, 65],
          "targeting_automation": {"advantage_audience": 1}}


def data(currency="EUR", min_daily=100):
    return {"account": {"currency": currency, "min_daily_budget": min_daily},
            "campaigns": [{"id": "c1", "name": "Camp", "objective": "OUTCOME_TRAFFIC", "status": "ACTIVE", "effective_status": "ACTIVE",
                           "daily_budget": "0", "lifetime_budget": "0"},
                          {"id": "c2", "name": "CBO daily", "objective": "OUTCOME_TRAFFIC", "status": "ACTIVE", "effective_status": "ACTIVE",
                           "daily_budget": "5000", "lifetime_budget": "0"},
                          {"id": "c3", "name": "CBO lifetime", "objective": "OUTCOME_TRAFFIC", "status": "ACTIVE",
                           "effective_status": "ACTIVE", "daily_budget": "0", "lifetime_budget": "100000"},
                          {"id": "c4", "name": "Jobs", "objective": "OUTCOME_LEADS", "status": "ACTIVE", "effective_status": "ACTIVE",
                           "special_ad_categories": ["EMPLOYMENT"]}],
            "adsets": [{"id": "s1", "name": "Daily", "campaign_id": "c1", "status": "ACTIVE", "effective_status": "ACTIVE",
                        "daily_budget": "2000", "lifetime_budget": "0", "start_time": "2026-01-01T00:00:00+0000", "targeting": TG},
                       {"id": "s2", "name": "Lifetime", "campaign_id": "c1", "status": "ACTIVE", "effective_status": "ACTIVE",
                        "daily_budget": "0", "lifetime_budget": "50000", "start_time": f"{D(5)}T00:00:00+0000",
                        "end_time": f"{D(40)}T23:59:00+0000", "targeting": TG},
                       {"id": "s3", "name": "Advantage", "campaign_id": "c1", "status": "ACTIVE", "effective_status": "ACTIVE",
                        "daily_budget": "2000", "targeting": TG_ADV},
                       {"id": "s4", "name": "Jobs set", "campaign_id": "c4", "status": "ACTIVE", "effective_status": "ACTIVE",
                        "daily_budget": "2000", "targeting": {"geo_locations": {"countries": ["GB"]}, "age_min": 18, "age_max": 65}},
                       {"id": "s5", "name": "Lead form", "campaign_id": "c1", "status": "ACTIVE", "effective_status": "ACTIVE",
                        "daily_budget": "2000", "optimization_goal": "LEAD_GENERATION", "targeting": TG}],
            "ads": [{"id": "a1", "name": "Ad", "adset_id": "s1", "campaign_id": "c1", "status": "ACTIVE", "effective_status": "ACTIVE",
                     "creative": SRC},
                    {"id": "a5", "name": "Lead ad", "adset_id": "s5", "campaign_id": "c1", "status": "ACTIVE",
                     "effective_status": "ACTIVE", "creative": SRC}],
            "audiences": [{"id": "lal1", "name": "LAL 1%", "subtype": "LOOKALIKE"}, {"id": "w1", "name": "Web", "subtype": "WEBSITE"}],
            "insights": {}}


class Rules(unittest.TestCase):
    def setUp(self):
        self.d = data()
        self.items = []

        def find_live(level, oid):
            coll = {"campaign": "campaigns", "adset": "adsets", "ad": "ads"}[level]
            o = next(x for x in self.d[coll] if x["id"] == str(oid))
            return json.loads(json.dumps(o))
        for p in (mock.patch.object(meta, "_live", find_live), mock.patch.object(meta, "load", lambda *a, **k: self.d),
                  mock.patch.object(core, "draft", lambda a: {"items": self.items}),
                  mock.patch.object(meta, "link_problem", lambda u: ""), mock.patch.object(meta, "page_check", lambda t, u: []),
                  mock.patch.object(meta, "_media_index", lambda: {"m1": {"kind": "image", "name": "pic.jpg"}}),
                  mock.patch.object(meta, "_pixel", lambda: "px1")):
            p.start()
            self.addCleanup(p.stop)

    def chk(self, c):
        return meta.check(meta._normalise(c), self.d, {})

    def problems(self, c):
        return " | ".join(self.chk(c)[0])

    def warnings(self, c):
        return " | ".join(self.chk(c)[1])

    def step(self, c):
        return meta.ops_for(meta._normalise(c), {})[0]

    # ---- M1: one broken item is its own refusal, never a crash ----
    def test_m1_plan_survives_a_broken_item(self):
        good = {"id": "g", "summary": "good", "by": "you", "problems": [],
                "change": meta._normalise({"type": "meta_status", "level": "adset", "id": "s1", "status": "paused"})}
        bad = {"id": "b", "summary": "bad", "by": "you", "problems": [],
               "change": meta._normalise({"type": "meta_rename", "level": "adset", "id": "s1", "new_name": "X"})}
        real = meta.ops_for

        def ops(c, items=None):
            if c["type"] == "meta_rename":
                raise RuntimeError("boom")
            return real(c, items)
        with mock.patch.object(meta, "_only", lambda only: [good, bad]), mock.patch.object(meta, "_check_step", lambda s: ""), \
                mock.patch.object(meta, "ops_for", ops):
            p = meta.plan()
        by = {e["id"]: e for e in p["items"]}
        self.assertEqual(by["g"]["problems"], [])
        self.assertEqual(by["b"]["problems"], ["Studio couldn't read this change (boom); edit or remove it and check again"])
        self.assertFalse(p["ok"])
        self.assertEqual(len(p["steps"]), 1)

    def test_m1_plan_survives_an_item_without_change_and_a_null_age(self):
        broken = {"id": "x", "summary": "x", "by": "you", "problems": []}
        raw = {"id": "n", "summary": "n", "by": "you", "problems": [],  # a synced draft: never normalised
               "change": {"type": "meta_new_adset", "campaign_id": "c1", "name": "S", "daily_budget": 10, "countries": ["GB"],
                          "age_min": None, "age_max": ""}}
        with mock.patch.object(meta, "_only", lambda only: [broken, raw]), mock.patch.object(meta, "_check_step", lambda s: ""):
            p = meta.plan()
        by = {e["id"]: e for e in p["items"]}
        self.assertIn("no change", " ".join(by["x"]["problems"]))
        self.assertEqual(by["n"]["problems"], [])
        tg = p["steps"][0]["params"]["targeting"]
        self.assertEqual((tg["age_min"], tg["age_max"]), (18, 65))

    def test_m1_normalise_ages(self):
        c = meta._normalise({"type": "meta_targeting", "level": "adset", "id": "s1", "age_min": None, "age_max": "40"})
        self.assertNotIn("age_min", c)
        self.assertEqual(c["age_max"], 40)
        c = meta._normalise({"type": "meta_targeting", "level": "adset", "id": "s1", "age_min": "", "age_max": 0})
        self.assertNotIn("age_min", c)
        self.assertNotIn("age_max", c)
        self.assertIn("whole number", self.problems({"type": "meta_targeting", "level": "adset", "id": "s1", "age_min": "abc"}))

    # ---- M2 / M21: budget type and "0" budgets ----
    def test_m2_budget_type_and_zero(self):
        self.assertEqual(self.problems({"type": "meta_budget", "level": "adset", "id": "s1", "budget": 30}), "")
        p = self.problems({"type": "meta_budget", "level": "adset", "id": "s1", "budget": 30, "lifetime": True})
        self.assertIn("this ad set has a daily budget, not a lifetime budget", p)
        self.assertNotIn("adset", p)
        self.assertIn("lifetime budget, not a daily budget", self.problems({"type": "meta_budget", "level": "adset", "id": "s2", "budget": 30}))
        with self.assertRaises(ValueError) as e:  # never ZeroDivisionError on "0"
            self.step({"type": "meta_budget", "level": "adset", "id": "s1", "budget": 30, "lifetime": True})
        self.assertIn("not a lifetime budget", str(e.exception))
        s = self.step({"type": "meta_budget", "level": "adset", "id": "s1", "budget": 30})
        self.assertEqual(s["params"], {"daily_budget": 3000})
        self.assertIn("20.00 -> 30.00 (+50%)", s["line"])

    def test_m21_lifetime_campaign(self):
        rows = {r["id"]: r for r in meta.rows(self.d, "campaigns")}
        self.assertEqual(rows["c3"]["budget_level"], "Campaign (CBO, lifetime)")
        self.assertEqual((rows["c3"]["budget_type"], rows["c3"]["lifetime_budget"], rows["c3"]["budget"]), ("lifetime", 1000.0, None))
        self.assertEqual((rows["c1"]["budget_level"], rows["c1"]["budget_type"]), ("Ad sets", None))  # "0" is no budget
        self.assertEqual(rows["c2"]["budget_type"], "daily")
        self.assertEqual(self.problems({"type": "meta_budget", "level": "campaign", "id": "c3", "budget": 2000, "lifetime": True}), "")
        self.assertIn("not a daily budget", self.problems({"type": "meta_budget", "level": "campaign", "id": "c3", "budget": 2000}))
        s = self.step({"type": "meta_budget", "level": "campaign", "id": "c3", "budget": 2000, "lifetime": True})
        self.assertEqual(s["params"], {"lifetime_budget": 200000})
        sets = {r["id"]: r for r in meta.rows(self.d, "adsets")}
        self.assertEqual(sets["s2"]["budget_type"], "lifetime")
        self.assertFalse(sets["s1"]["cbo"])  # campaign budgets of "0" are not a campaign budget
        self.assertEqual(self.problems({"type": "meta_budget", "level": "adset", "id": "s1", "budget": 25}), "")

    # ---- M3: ages on an ad set edit ----
    def test_m3_edit_ages(self):
        for kw in ({"age_min": 10}, {"age_max": 70}, {"age_min": 70, "age_max": 80}, {"age_min": 50, "age_max": 40}):
            self.assertIn("age must be 18-65", self.problems({"type": "meta_targeting", "level": "adset", "id": "s1", **kw}), kw)
        self.assertEqual(self.problems({"type": "meta_targeting", "level": "adset", "id": "s1", "age_min": 30}), "")
        self.d["adsets"][0]["targeting"] = {**TG, "age_min": 40}
        self.assertIn("this makes 40-30", self.problems({"type": "meta_targeting", "level": "adset", "id": "s1", "age_max": 30}))
        with self.assertRaises(ValueError):  # checked again against the live targeting right before it is sent
            meta.ops_for({"type": "meta_targeting", "level": "adset", "id": "s1", "age_max": 30}, {})

    # ---- M4: Advantage+ audience ages ----
    def test_m4_advantage_suggestion(self):
        c = {"type": "meta_targeting", "level": "adset", "id": "s3", "age_range": [25, 55]}
        self.assertEqual(self.problems(c), "")
        s = self.step(c)
        tg = s["params"]["targeting"]
        self.assertEqual((tg["age_min"], tg["age_max"], tg["age_range"]), (18, 65, [25, 55]))
        self.assertIn("age control 18-65, suggestion 20-65 -> control 18-65, suggestion 25-55", s["line"])
        self.assertIn("Advantage+ audience is on", self.problems({"type": "meta_targeting", "level": "adset", "id": "s3", "age_min": 30}))
        self.assertIn("Advantage+ audience is on", self.problems({"type": "meta_targeting", "level": "adset", "id": "s3", "age_max": 55}))
        self.assertEqual(self.problems({"type": "meta_targeting", "level": "adset", "id": "s3", "age_min": 21}), "")
        self.assertIn("Advantage+ audience is off", self.problems({"type": "meta_targeting", "level": "adset", "id": "s1", "age_range": [25, 55]}))
        self.assertIn("inside the hard limits", self.problems({"type": "meta_targeting", "level": "adset", "id": "s3", "age_min": 25,
                                                               "age_range": [20, 55]}))
        # an older Advantage+ ad set whose live minimum is above 25: changing only the maximum is not blocked by it
        self.d["adsets"][2]["targeting"] = {**TG_ADV, "age_min": 30, "age_range": [30, 65]}
        self.assertEqual(self.problems({"type": "meta_targeting", "level": "adset", "id": "s3", "age_max": 65}), "")
        self.assertIn("Advantage+ audience is on", self.problems({"type": "meta_targeting", "level": "adset", "id": "s3", "age_max": 60}))
        self.d["adsets"][2]["targeting"] = TG_ADV
        row = {r["id"]: r for r in meta.rows(self.d, "adsets")}["s3"]
        self.assertEqual((row["age"], row["age_control"]), ("20-65 (suggestion)", "18-65"))
        new = {"type": "meta_new_adset", "campaign_id": "c1", "name": "N", "daily_budget": 10, "countries": ["GB"], "advantage_audience": True}
        self.assertIn("Advantage+ audience is on", self.problems({**new, "age_min": 30}))
        self.assertEqual(self.problems({**new, "age_range": [30, 50]}), "")
        s = self.step({**new, "age_range": [30, 50]})
        self.assertEqual(s["params"]["targeting"]["age_range"], [30, 50])
        self.assertIn("control 18-65, suggestion 30-50", s["line"])

    # ---- M5: a half-filled age keeps the live value of the other side ----
    def test_m5_half_filled_age(self):
        c = meta._normalise({"type": "meta_targeting", "level": "adset", "id": "s1", "age_min": "", "age_max": 55})
        self.assertEqual(self.problems(c), "")
        tg = meta.ops_for(c, {})[0]["params"]["targeting"]
        self.assertEqual((tg["age_min"], tg["age_max"]), (18, 55))
        self.assertEqual(meta.summarise(c, self.d, {}), 'Meta adset "Daily": age up to 55')
        self.items = [{"id": "i1", "by": "you", "change": c}]
        out = meta.rows(self.d, "adsets")
        meta._overlay("adsets", out, None, None)
        self.assertEqual(next(r for r in out if r["id"] == "s1")["_draft"]["age"]["value"], "18-55")

    # ---- M6: the same countries in another order are no change ----
    def test_m6_reordered_countries(self):
        self.assertIn("nothing to change", self.problems({"type": "meta_targeting", "level": "adset", "id": "s1", "countries": ["DE", "GB"]}))
        c = {"type": "meta_targeting", "level": "adset", "id": "s1", "countries": ["DE", "GB"], "age_min": 25}
        self.assertEqual(self.problems(c), "")
        s = self.step(c)
        self.assertEqual(s["params"]["targeting"]["geo_locations"]["countries"], ["GB", "DE"])
        self.assertNotIn("more countries", s.get("spend", ""))
        self.items = [{"id": "i1", "by": "you", "change": meta._normalise(c)}]
        out = meta.rows(self.d, "adsets")
        meta._overlay("adsets", out, None, None)
        self.assertNotIn("countries", next(r for r in out if r["id"] == "s1")["_draft"])

    # ---- M8: links ----
    def test_m8_links(self):
        for bad in ("not a url", "javascript:alert(1)", "ftp://example.com/x", "https://", "https://exa mple.com"):
            self.assertIn("link", self.problems({"type": "meta_copy", "level": "ad", "id": "a1", "link": bad}), bad)
        self.assertIn("can't be empty", self.problems({"type": "meta_copy", "level": "ad", "id": "a1", "link": ""}))
        self.assertEqual(self.problems({"type": "meta_copy", "level": "ad", "id": "a1", "link": "https://example.com/egypt?x={{ad.id}}"}), "")
        self.assertEqual(meta._url_problem("https://example.com"), "")
        self.assertTrue(meta._url_problem("www.example.com"))
        car = {"type": "meta_new_ad", "adset_id": "s1", "name": "C", "primary_text": "T", "link": "https://example.com/",
               "cards": [{"media_id": "m1", "name": "A", "link": "https://example.com/a"}, {"media_id": "m1", "name": "B", "link": "javascript:x"}]}
        self.assertIn("card 2 link", self.problems(car))
        self.assertIn("link", self.problems({"type": "meta_new_ad", "adset_id": "s1", "name": "N", "media_id": "m1", "primary_text": "T",
                                             "headline": "H", "link": "example"}))

    # ---- M9: names ----
    def test_m9_names(self):
        self.assertIn("can't be empty", self.problems({"type": "meta_rename", "level": "adset", "id": "s1", "new_name": "   "}))
        self.assertIn("can't be empty", self.problems({"type": "meta_new_campaign", "name": "  ", "objective": "traffic"}))
        self.assertIn("can't be empty", self.problems({"type": "meta_new_ad", "adset_id": "s1", "name": " ", "from_ad": "a1"}))
        self.assertIn("Meta may refuse names", self.warnings({"type": "meta_rename", "level": "adset", "id": "s1", "new_name": "x" * 300}))
        self.assertEqual(meta._normalise({"type": "meta_rename", "level": "adset", "id": "s1", "new_name": "  New  "})["new_name"], "New")

    # ---- M10: special ad categories ----
    def test_m10_special_categories(self):
        new = {"type": "meta_new_adset", "campaign_id": "c4", "name": "N", "daily_budget": 10, "countries": ["GB"]}
        self.assertEqual(self.problems(new), "")
        self.assertIn("only allows age 18-65", self.problems({**new, "age_min": 25}))
        self.assertIn("only allows age 18-65", self.problems({"type": "meta_targeting", "level": "adset", "id": "s4", "age_max": 50}))
        self.assertIn("lookalike", self.problems({"type": "meta_targeting", "level": "adset", "id": "s4", "audiences_add": ["lal1"]}))
        self.assertEqual(self.problems({"type": "meta_targeting", "level": "adset", "id": "s4", "audiences_add": ["w1"]}), "")
        self.assertEqual(self.problems({"type": "meta_targeting", "level": "adset", "id": "s1", "audiences_add": ["lal1"]}), "")
        for obj in ("traffic", "leads", "awareness"):
            self.assertIn("job ad", self.warnings({"type": "meta_new_campaign", "name": "We're hiring dive guides", "objective": obj}))
        self.assertNotIn("job ad", self.warnings({"type": "meta_new_campaign", "name": "Hiring", "objective": "traffic",
                                                  "special_ad_categories": ["EMPLOYMENT"]}))

    # ---- M11: placements Meta refuses ----
    def test_m11_placements(self):
        for pubs in (["audience_network"], ["messenger"], ["audience_network", "messenger"]):
            self.assertIn("alone", self.problems({"type": "meta_placements", "level": "adset", "id": "s1", "advantage": False,
                                                  "publisher_platforms": pubs}), pubs)
        ig_an = {"type": "meta_placements", "level": "adset", "id": "s1", "advantage": False,
                 "publisher_platforms": ["instagram", "audience_network"]}
        self.assertEqual(self.problems(ig_an), "")
        self.assertIn("without Facebook may be refused", self.warnings(ig_an))
        self.assertNotIn("without Facebook", self.warnings({**ig_an, "publisher_platforms": ["facebook", "instagram", "audience_network"]}))

    # ---- M12 / M20: end dates ----
    def test_m12_lifetime_needs_end(self):
        self.assertIn("lifetime budget", self.problems({"type": "meta_end", "level": "adset", "id": "s2", "end_date": None}))
        with self.assertRaises(ValueError):
            self.step({"type": "meta_end", "level": "adset", "id": "s2", "end_date": None})
        self.assertEqual(self.problems({"type": "meta_end", "level": "adset", "id": "s1", "end_date": None}), "")
        self.assertIn("after the ad set's start date", self.problems({"type": "meta_end", "level": "adset", "id": "s2", "end_date": D(3)}))
        self.assertIn("24 hours", self.warnings({"type": "meta_end", "level": "adset", "id": "s2", "end_date": D(5)}))
        self.assertEqual(self.problems({"type": "meta_end", "level": "adset", "id": "s2", "end_date": D(20)}), "")

    def test_m20_end_today_same_rule(self):
        new = {"type": "meta_new_adset", "campaign_id": "c1", "name": "N", "daily_budget": 10, "countries": ["GB"]}
        for c in ({"type": "meta_end", "level": "adset", "id": "s1", "end_date": D(0)}, {**new, "end_date": D(0)}):
            self.assertIn("end date must be after today", self.problems(c))
        for c in ({"type": "meta_end", "level": "adset", "id": "s1", "end_date": D(1)}, {**new, "end_date": D(1)}):
            self.assertEqual(self.problems(c), "")

    # ---- M13: currency factor and the account's minimum ----
    def test_m13_currency(self):
        self.assertEqual(meta._cur_factor(self.d), 100)
        self.d = data("JPY", 100)
        self.assertEqual(meta._cur_factor(self.d), 1)
        self.d["adsets"][0]["daily_budget"] = "2000"
        s = self.step({"type": "meta_budget", "level": "adset", "id": "s1", "budget": 3000})
        self.assertEqual(s["params"], {"daily_budget": 3000})
        self.assertIn("2000 -> 3000", s["line"])
        self.assertEqual({r["id"]: r for r in meta.rows(self.d, "adsets")}["s1"]["budget"], 2000.0)
        self.assertIn("at least 100", self.problems({"type": "meta_budget", "level": "adset", "id": "s1", "budget": 50}))
        new = self.step({"type": "meta_new_campaign", "name": "C", "objective": "traffic", "daily_budget": 5000})
        self.assertEqual(new["params"]["daily_budget"], 5000)
        self.d = data("KRW", 1000)
        self.assertEqual(meta._minor(12000, self.d), 12000)
        self.d = data("GBP", 500)
        self.assertEqual(meta._min_budget(self.d), 5.0)
        self.assertIn("at least 5.00 GBP", self.problems({"type": "meta_budget", "level": "adset", "id": "s1", "budget": 3}))
        self.assertIn("at least 5.00", self.problems({"type": "meta_new_adset", "campaign_id": "c1", "name": "N", "daily_budget": 3,
                                                       "countries": ["GB"]}))
        self.assertEqual(meta._min_budget({"account": {}}), 1.0)  # older cache: the old floor
        cfg = meta.config(self.d)
        self.assertEqual((cfg["currency"], cfg["currency_factor"], cfg["decimals"], cfg["min_daily_budget"]), ("GBP", 100, 2, 5.0))
        self.assertEqual(meta.config(data("JPY"))["decimals"], 0)

    # ---- M14: new ads in lead form ad sets ----
    def test_m14_lead_form_adsets(self):
        up = {"type": "meta_new_ad", "adset_id": "s5", "name": "N", "media_id": "m1", "primary_text": "T", "headline": "H",
              "link": "https://example.com/"}
        self.assertIn("lead form", self.problems(up))
        car = {"type": "meta_new_ad", "adset_id": "s5", "name": "C", "primary_text": "T", "link": "https://example.com/",
               "cards": [{"media_id": "m1", "name": "A", "link": "https://example.com/a"}, {"media_id": "m1", "name": "B", "link": "https://example.com/b"}]}
        self.assertIn("lead form", self.problems(car))
        with self.assertRaises(ValueError):
            self.step(up)
        self.assertEqual(self.problems({"type": "meta_new_ad", "adset_id": "s5", "name": "N", "from_ad": "a5"}), "")
        self.assertEqual(self.problems({**up, "adset_id": "s1"}), "")

    # ---- M18: copy lengths, whatever else is wrong ----
    def test_m18_copy_lengths(self):
        w = self.warnings({"type": "meta_copy", "level": "ad", "id": "a1", "primary_text": "x" * 200, "headline": "h" * 50,
                           "description": "d" * 40, "link": "not a url"})
        for k in ("primary_text: 200 chars", "headline: 50 chars", "description: 40 chars"):
            self.assertIn(k, w)
        w = self.warnings({"type": "meta_new_ad", "adset_id": "s1", "name": "C", "primary_text": "T", "link": "https://example.com/",
                           "cards": [{"media_id": "m1", "name": "n" * 45, "description": "d" * 25, "link": "https://example.com/a"},
                                     {"media_id": "m1", "name": "B", "link": "https://example.com/b"}]})
        self.assertIn("card 1 name: 45 chars", w)
        self.assertIn("card 1 description: 25 chars", w)
        raw = meta.check({"type": "meta_new_ad", "adset_id": "s1", "name": "C", "primary_text": "T", "link": "https://example.com/",
                          "cards": [{"media_id": "m1", "name": "A", "link": "https://example.com/a"},
                                    {"media_id": "m1", "name": "n" * 45, "link": "https://example.com/b"}]}, self.d, {})[1]
        self.assertIn("card 2 name: 45 chars - Meta cuts it after about 40", raw)  # no index: numbered by position

    # ---- M19: the call to action list ----
    def test_m19_ctas(self):
        for cta in ("BOOK_NOW", "GET_QUOTE", "SEE_DETAILS", "LEARN_MORE"):
            self.assertEqual(self.problems({"type": "meta_copy", "level": "ad", "id": "a1", "cta": cta}), "", cta)
        self.assertIn("cta must be one of", self.problems({"type": "meta_copy", "level": "ad", "id": "a1", "cta": "BOGUS"}))
        values = [x["value"] for x in meta.config()["ctas"]]
        self.assertEqual(values, meta.CTAS)
        self.assertTrue({"BOOK_NOW", "GET_QUOTE", "SEE_DETAILS"} <= set(values))
        with mock.patch.object(meta, "last_push", lambda: None), mock.patch.object(meta, "refreshing", lambda: False):
            st = meta.state()
        self.assertEqual([x["value"] for x in st["meta_config"]["ctas"]], meta.CTAS)

    def test_no_em_dash(self):
        self.assertNotIn("—", (HERE / "meta_ads.py").read_text())


if __name__ == "__main__":
    unittest.main(verbosity=1)
