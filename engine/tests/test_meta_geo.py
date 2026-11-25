"""Meta ad set countries: the country list, the location rules (check + live plan) and the display split.
Run from the engine folder:  python3 -m unittest tests.test_meta_geo

Offline: fake data only; Meta reads are replaced with fixed answers and nothing is sent anywhere.
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("MSTUDIO_HOME", _tf.mkdtemp(prefix="mstudio-test-"))
_os.environ["MSTUDIO_KEYCHAIN"] = "0"

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
TMP = Path(tempfile.mkdtemp(prefix="meta-geo-test-"))
os.environ["STUDIO_DATA_DIR"] = str(TMP)

import core  # noqa: E402

core.STUDIO_DIR = TMP
import geo_countries as geo  # noqa: E402
import meta_ads as meta  # noqa: E402

TG_PLAIN = {"geo_locations": {"countries": ["GB"]}, "age_min": 18, "age_max": 65}
TG_MIXED = {"geo_locations": {"countries": ["GB"], "regions": [{"key": "3856", "name": "Bavaria", "country_code": "DE"}],
                              "cities": [{"key": "2421836", "name": "Paris", "country_code": "FR", "radius": 10, "distance_unit": "mile"}],
                              "location_types": ["home", "recent"]},
            "excluded_geo_locations": {"countries": ["NO"]}, "age_min": 25, "age_max": 60}
TG_REGIONS_ONLY = {"geo_locations": {"regions": [{"key": "3856", "name": "Bavaria", "country_code": "DE"}]}, "age_min": 18, "age_max": 65}
DATA = {"account": {"currency": "EUR"},
        "campaigns": [{"id": "c1", "name": "Camp", "objective": "OUTCOME_TRAFFIC", "status": "ACTIVE", "effective_status": "ACTIVE"},
                      {"id": "c2", "name": "Jobs", "objective": "OUTCOME_LEADS", "status": "ACTIVE", "effective_status": "ACTIVE",
                       "special_ad_categories": ["EMPLOYMENT"]},
                      {"id": "c3", "name": "Vote", "objective": "OUTCOME_AWARENESS", "status": "ACTIVE", "effective_status": "ACTIVE",
                       "special_ad_categories": ["ISSUES_ELECTIONS_POLITICS"]}],
        "adsets": [{"id": "s1", "name": "Plain", "campaign_id": "c1", "status": "ACTIVE", "effective_status": "ACTIVE", "targeting": TG_PLAIN},
                   {"id": "s2", "name": "Mixed", "campaign_id": "c1", "status": "ACTIVE", "effective_status": "ACTIVE", "targeting": TG_MIXED},
                   {"id": "s3", "name": "Regions", "campaign_id": "c1", "status": "ACTIVE", "effective_status": "ACTIVE",
                    "targeting": TG_REGIONS_ONLY},
                   {"id": "s4", "name": "Jobs set", "campaign_id": "c2", "status": "ACTIVE", "effective_status": "ACTIVE",
                    "targeting": {"geo_locations": {"countries": ["GB"]}, "excluded_geo_locations": {"regions": [{"key": "1", "name": "Wales",
                                                                                                                   "country_code": "GB"}]}}},
                   {"id": "s5", "name": "Vote set", "campaign_id": "c3", "status": "ACTIVE", "effective_status": "ACTIVE", "targeting": TG_PLAIN}],
        "ads": [], "audiences": [], "insights": {}}
LIVE = {s["id"]: s for s in DATA["adsets"]}


def live(level, oid):
    if level == "campaign":
        return {"name": "Camp", "objective": "OUTCOME_TRAFFIC"}
    s = LIVE[str(oid)]
    return json.loads(json.dumps({"name": s["name"], "status": s["status"], "campaign_id": s["campaign_id"], "targeting": s["targeting"]}))


def tgt(sid, **kw):
    return meta._normalise({"type": "meta_targeting", "level": "adset", "id": sid, **kw})


class Geo(unittest.TestCase):
    def setUp(self):
        for p in (mock.patch.object(meta, "_live", live), mock.patch.object(meta, "load", lambda *a, **k: DATA),
                  mock.patch.object(core, "draft", lambda a: {"items": []})):
            p.start()
            self.addCleanup(p.stop)

    def problems(self, c):
        return meta.check(c, DATA, {})[0]

    def warnings(self, c):
        return meta.check(c, DATA, {})[1]

    # ---- the list ----
    def test_country_list(self):
        self.assertEqual(len(geo.NAMES), 250)  # 249 ISO 3166-1 alpha-2 + XK (Kosovo, as Meta uses it)
        self.assertEqual(geo.name("gb"), "United Kingdom")
        for g, codes in geo.GROUPS:
            self.assertTrue(all(c in geo.NAMES and c not in geo.RESTRICTED for c in codes), g)
        self.assertEqual(len(dict(geo.GROUPS)["European Union"]), 27)
        ui = geo.for_ui()
        self.assertEqual(len(ui["countries"]), 250)
        self.assertIn("restricted", next(c for c in ui["countries"] if c["code"] == "IR"))
        self.assertNotIn("restricted", next(c for c in ui["countries"] if c["code"] == "GB"))

    def test_code_validation(self):
        self.assertEqual(geo.code_problems(["GB", "de"]), [])
        p = geo.code_problems(["GB", "ZZ", "GBR", "1"])
        self.assertEqual(len(p), 1)
        self.assertIn("ZZ", p[0])
        self.assertIn("GBR", p[0])
        self.assertTrue(any("not a country code: UK" in x for x in self.problems(tgt("s1", countries=["UK"]))))
        new = meta._normalise({"type": "meta_new_adset", "campaign_id": "c1", "name": "N", "daily_budget": 10, "countries": ["xx"]})
        self.assertTrue(any("not a country code: XX" in x for x in self.problems(new)))

    def test_normalise_cleans_codes(self):
        self.assertEqual(tgt("s1", countries=[" gb", "DE", "gb"])["countries"], ["GB", "DE"])
        self.assertEqual(tgt("s1", countries="gb, de")["countries"], ["GB", "DE"])

    # ---- the rules ----
    def test_restricted_countries_refused(self):
        for code in ("CU", "IR", "KP", "SY", "RU"):
            p = self.problems(tgt("s1", countries=["GB", code]))
            self.assertTrue(any(f"({code}) can't be targeted" in x for x in p), (code, p))
        new = meta._normalise({"type": "meta_new_adset", "campaign_id": "c1", "name": "N", "daily_budget": 10, "countries": ["GB", "IR"]})
        self.assertTrue(any("Iran (IR) can't be targeted" in x for x in self.problems(new)))
        self.assertEqual(self.problems(tgt("s1", countries=["GB", "BY", "UA"])), [])

    def test_overlap_with_region_and_city_refused(self):
        p = self.problems(tgt("s2", countries=["GB", "DE"]))
        self.assertTrue(any("Germany (DE) overlaps the region Bavaria" in x for x in p), p)
        p = self.problems(tgt("s2", countries=["FR"]))
        self.assertTrue(any("France (FR) overlaps the city Paris" in x for x in p), p)
        self.assertEqual(self.problems(tgt("s2", countries=["GB", "IE"])), [])

    def test_overlap_with_exclusion_refused(self):
        p = self.problems(tgt("s2", countries=["GB", "NO"]))
        self.assertTrue(any("Norway (NO) is also excluded" in x for x in p), p)

    def test_at_least_one_location(self):
        p = self.problems(tgt("s1", countries=[], countries_clear=True))
        self.assertTrue(any("at least one location" in x for x in p), p)
        self.assertEqual(self.problems(tgt("s3", countries=[], countries_clear=True)), [])  # Bavaria stays
        self.assertEqual(self.problems(tgt("s2", countries_clear=True)), [])  # Bavaria and Paris stay
        new = meta._normalise({"type": "meta_new_adset", "campaign_id": "c1", "name": "N", "daily_budget": 10, "countries": []})
        self.assertIn("the new ad set needs at least one country", self.problems(new))

    def test_live_plan_refuses_too(self):  # the plan reads the live targeting right before sending: same rules
        for bad in (["GB", "DE"], ["NO"], ["CU"]):
            with self.assertRaises(ValueError):
                meta.ops_for(tgt("s2", countries=bad))
        with self.assertRaises(ValueError):
            meta.ops_for(tgt("s1", countries=[], countries_clear=True))

    # ---- an empty list is not "remove every country" ----
    def test_empty_list_changes_nothing_without_the_clear_flag(self):
        c = tgt("s2", countries=[], age_min=30)  # e.g. an old draft: [] next to an age change
        self.assertEqual(self.problems(c), [])
        g = meta.ops_for(c)[0]["params"]["targeting"]["geo_locations"]
        self.assertEqual(g["countries"], ["GB"])
        self.assertIn("nothing to change", " ".join(self.problems(tgt("s2", countries=[]))))
        self.assertTrue(any("countries_clear goes with an empty" in x for x in self.problems(tgt("s2", countries=["GB"], countries_clear=True))))
        self.assertNotIn("countries", meta.summarise(tgt("s2", countries=[], age_min=30), DATA).split(":", 1)[1])

    def test_mcp_audience_tool_refuses_country_changes(self):
        import mcp_server  # noqa: PLC0415
        with mock.patch.object(meta, "use", lambda k: "act_1"), mock.patch.object(meta, "add", lambda *a, **k: ["added"]):
            for bad in ({"countries": []}, {"countries_clear": True}, {"countries": ["DE"]}):
                with self.assertRaises(ValueError):
                    mcp_server.call_audiences("audience_add_to_draft", {"account": "meta", "changes": [
                        {"type": "meta_targeting", "level": "adset", "id": "s1", "audiences_add": ["e1"], **bad}]})
            self.assertEqual(mcp_server.call_audiences("audience_add_to_draft", {"account": "meta", "changes": [
                {"type": "meta_targeting", "level": "adset", "id": "s1", "audiences_add": ["e1"]}]}), ["added"])

    # ---- the plan sends what was checked ----
    def test_string_countries_are_pushed_as_checked(self):
        raw = {"type": "meta_targeting", "level": "adset", "id": "s1", "countries": "gb, de"}  # not normalised (synced draft)
        self.assertEqual(self.problems(raw), [])
        st = meta.ops_for(raw)[0]
        self.assertEqual(st["params"]["targeting"]["geo_locations"]["countries"], ["GB", "DE"])
        self.assertIn("more countries", st["spend"])
        with self.assertRaises(ValueError):
            meta.ops_for({"type": "meta_targeting", "level": "adset", "id": "s1", "countries": "gb cu"})

    def test_new_adset_codes_checked_again_in_the_plan(self):
        base = {"type": "meta_new_adset", "campaign_id": "c1", "name": "N", "daily_budget": 10}
        for bad in (["GB", "IR"], ["xx"], [], "gb, ru"):
            with self.assertRaises(ValueError):
                meta.ops_for({**base, "countries": bad}, {})
        tg = meta.ops_for({**base, "countries": "gb, de"}, {})[0]["params"]["targeting"]
        self.assertEqual(tg["geo_locations"]["countries"], ["GB", "DE"])

    # ---- country groups and pins ----
    def test_country_group_overlap(self):
        def probs(groups, codes):
            return geo.geo_problems(codes, {"geo_locations": {"country_groups": groups}})
        self.assertTrue(any("Germany (DE) is already in the country group eea" in x for x in probs(["eea"], ["DE"])))
        self.assertTrue(any("already in the country group worldwide" in x for x in probs(["worldwide"], ["GB"])))
        self.assertEqual(probs(["eea"], ["GB", "US"]), [])
        self.assertEqual(probs(["nafta"], ["GB"]), [])
        self.assertTrue(probs(["nafta"], ["MX"]))
        self.assertEqual(probs(["latam"], ["BR"]), [])  # unknown group: not refused ...
        w = geo.geo_warnings(["BR"], {"geo_locations": {"country_groups": ["latam"]}})
        self.assertTrue(w and "latam" in w[0], w)  # ... but warned
        self.assertEqual(geo.geo_warnings(["BR"], {"geo_locations": {"country_groups": ["eea"]}}), [])
        self.assertIn("country_groups", geo.for_ui())
        self.assertIsNone(geo.for_ui()["country_groups"]["worldwide"])

    def test_unnamed_pin(self):
        tg = {"geo_locations": {"custom_locations": [{"latitude": 48.1, "longitude": 11.6, "radius": 10, "country_code": "DE"}]}}
        p = geo.geo_problems(["DE"], tg)
        self.assertTrue(any("overlaps a map pin (Germany)" in x for x in p), p)
        self.assertEqual(geo.summary(tg["geo_locations"]), "1 map pin")
        self.assertEqual(geo.sub_places(tg["geo_locations"])[0][1], "a map pin")

    # ---- the payload ----
    def test_regions_and_cities_kept_in_the_payload(self):
        st = meta.ops_for(tgt("s2", countries=["GB", "IE", "NL"]))[0]
        g = st["params"]["targeting"]["geo_locations"]
        self.assertEqual(g["countries"], ["GB", "IE", "NL"])
        self.assertEqual(g["regions"], TG_MIXED["geo_locations"]["regions"])
        self.assertEqual(g["cities"], TG_MIXED["geo_locations"]["cities"])
        self.assertEqual(g["location_types"], ["home", "recent"])
        self.assertEqual(st["params"]["targeting"]["excluded_geo_locations"], {"countries": ["NO"]})
        self.assertEqual(st["undo"]["targeting"]["geo_locations"], {**TG_MIXED["geo_locations"]})
        # no countries left: the countries key goes, the regions and cities stay
        g = meta.ops_for(tgt("s2", countries=[], countries_clear=True))[0]["params"]["targeting"]["geo_locations"]
        self.assertNotIn("countries", g)
        self.assertEqual([r["name"] for r in g["regions"]], ["Bavaria"])

    def test_spend_flag_when_countries_added(self):
        st = meta.ops_for(tgt("s1", countries=["GB", "DE"]))[0]
        self.assertIn("more countries", st["spend"])
        st = meta.ops_for(tgt("s3", countries=["GB"]))[0]  # regions-only ad set gets a country
        self.assertIn("more countries", st["spend"])
        st = meta.ops_for(tgt("s2", countries=[], countries_clear=True))[0]
        self.assertNotIn("more countries", st["spend"])
        self.assertIn("brings back countries", st["undo_spend"])

    # ---- special ad categories ----
    def test_special_categories(self):
        w = self.warnings(tgt("s4", countries=["GB", "IE"]))
        self.assertTrue(any("excluded locations" in x for x in w), w)
        w = self.warnings(tgt("s5", countries=["GB", "FR"]))
        self.assertTrue(any("authorised" in x and "FR" in x for x in w), w)
        self.assertEqual(self.warnings(tgt("s1", countries=["GB", "FR"])), [])
        self.assertEqual(geo.special_warnings(["HOUSING"], {"geo_locations": {"zips": [{"key": "US:94304", "country": "US"}]}})[0][:20],
                         "special ad category ")

    # ---- the display ----
    def test_display_split(self):
        t = meta._targeting(TG_MIXED)
        self.assertEqual(t["countries"], "GB")
        self.assertEqual(t["locations"], "GB + 1 region, 1 city")
        self.assertEqual(t["geo"]["countries"], ["GB"])
        self.assertEqual([p["name"] for p in t["geo"]["places"]], ["Bavaria", "Paris"])
        self.assertEqual(t["geo"]["excluded_countries"], ["NO"])
        self.assertEqual(meta._targeting(TG_REGIONS_ONLY)["countries"], "None (1 region)")
        self.assertEqual(meta._targeting(TG_REGIONS_ONLY)["locations"], "1 region")
        rows = {r["id"]: r for r in meta.rows(DATA, "adsets")}
        self.assertEqual(rows["s2"]["countries"], "GB")
        self.assertNotIn("Bavaria", rows["s2"]["countries"])
        st = meta.ops_for(tgt("s2", countries=["GB", "IE"]))[0]
        self.assertIn("locations GB + 1 region, 1 city -> GB, IE + 1 region, 1 city", st["line"])
        self.assertIn("countries GB, IE", meta.summarise(tgt("s2", countries=["GB", "IE"]), DATA))
        self.assertIn("regions / cities only", meta.summarise(tgt("s3", countries=[], countries_clear=True), DATA))
        self.assertEqual(rows["s3"]["countries"], "None (1 region)")  # regions only: never an empty Countries cell


if __name__ == "__main__":
    unittest.main()
