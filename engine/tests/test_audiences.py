"""Audiences for Google Ads (audiences.py) and Meta (meta_audiences.py). Run from the engine folder:
    python3 tests/test_audiences.py

Offline: every Google and Meta call goes to a fake that records it; nothing is sent anywhere. The Google fake records
whether a mutate was validate-only; the Meta fake fails the test on any audience POST made before Push live.
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
TMP = Path(tempfile.mkdtemp(prefix="audiences-test-"))
os.environ["STUDIO_DATA_DIR"] = str(TMP)

import core  # noqa: E402

core.STUDIO_DIR = TMP
import datapolicy  # noqa: E402

datapolicy.DIR = str(TMP)
import audiences  # noqa: E402
import gapi  # noqa: E402
import live  # noqa: E402
import meta_ads as meta  # noqa: E402
import meta_audiences as ma  # noqa: E402
import mcp_server  # noqa: E402

A = "123"
RES = lambda i: f"customers/{A}/userLists/{i}"  # noqa: E731
WEB = {"resourceName": RES(11), "id": "11", "name": "Egypt visitors", "type": "RULE_BASED", "readOnly": False, "membershipLifeSpan": "30",
       "description": "",
       "ruleBasedUserList": {"prepopulationStatus": "FINISHED", "flexibleRuleUserList": {
           "inclusiveRuleOperator": "OR", "inclusiveOperands": [
               {"rule": {"ruleItemGroups": [{"ruleItems": [{"name": "url__", "stringRuleItem": {"operator": "CONTAINS", "value": "/egypt"}}]}]},
                "lookbackWindowDays": "30"}]}}}
UNUSED = {**WEB, "resourceName": RES(12), "id": "12", "name": "Unused list"}
CRM = {"resourceName": RES(13), "id": "13", "name": "Customer list one", "type": "CRM_BASED", "readOnly": False, "membershipLifeSpan": "540"}
SHARED = {"resourceName": RES(14), "id": "14", "name": "Shared remarketing", "type": "REMARKETING", "readOnly": True, "membershipLifeSpan": "540"}
COMBO = {"resourceName": RES(15), "id": "15", "name": "Combined one", "type": "LOGICAL", "readOnly": False, "membershipLifeSpan": "30",
         "logicalUserList": {"rules": [{"operator": "ANY", "ruleOperands": [{"userList": RES(11)}, {"userList": RES(14)}]}]}}
LISTS = [WEB, UNUSED, CRM, SHARED, COMBO]


def ul_row(u, **kw):
    return {"id": int(u["id"]), "list": u["name"], "audience": u["name"], "type": u["type"].replace("_", " ").title(),
            "size_search": 3000, "size_display": 1200, "size_range_search": "ONE_THOUSAND_TO_TEN_THOUSAND",
            "size_range_display": "LESS_THAN_FIVE_HUNDRED", "lifespan": u.get("membershipLifeSpan"), "read_only": u.get("readOnly"),
            "description": "", "status": "Enabled", "campaign_id": None, "ad_group_id": None,
            "_raw": {k: u[k] for k in ("type", "readOnly", "ruleBasedUserList", "logicalUserList") if k in u},
            "_logical": (u.get("logicalUserList") or {}).get("rules") or [], **kw}


BASE = {"rows": {"user_lists": [ul_row(u) for u in LISTS],
                 "audiences": [{"user_list_id": 11, "campaign": "Camp A", "ad_group": "", "status": "Enabled", "excluded": False},
                               {"user_list_id": 11, "campaign": "Camp B", "ad_group": "Group 1", "status": "Enabled", "excluded": True},
                               {"user_list_id": 12, "campaign": "Camp A", "ad_group": "", "status": "Removed", "excluded": False}]},
        "fetched_at": "2026-11-17T10:00:00", "errors": {}}


class GFake:
    """Google: answers GAQL from LISTS; records every mutate with its validateOnly flag."""

    def __init__(self):
        self.mutates, self.queries = [], []
        self.camp_users = {RES(11): [{"campaign": {"name": "Camp A"}, "campaignCriterion": {"negative": False}}]}

    def query(self, acct, q):
        self.queries.append(q)
        if "FROM user_list WHERE user_list.name = " in q:
            name = q.split("user_list.name = '", 1)[1].rsplit("'", 1)[0].replace("\\'", "'")
            return [{"userList": json.loads(json.dumps(u))} for u in LISTS if u["name"] == name]
        if "FROM user_list WHERE user_list.type IN (CRM_BASED, LOGICAL)" in q:
            return [{"userList": {"id": u["id"], "type": u["type"]}} for u in LISTS if u["type"] in ("CRM_BASED", "LOGICAL")]
        if "FROM user_list WHERE user_list.type = LOGICAL" in q:
            return [{"userList": COMBO}]
        if q.startswith("SELECT user_list.id, user_list.name FROM user_list"):
            return [{"userList": {"id": u["id"], "name": u["name"]}} for u in LISTS]
        if "FROM campaign_criterion WHERE campaign_criterion.user_list.user_list" in q:
            res = q.split("= '", 1)[1].split("'", 1)[0]
            return self.camp_users.get(res, [])
        if "FROM ad_group_criterion WHERE ad_group_criterion.user_list.user_list" in q:
            return []
        if "FROM campaign WHERE campaign.name" in q or "FROM campaign " in q and "campaign.name" in q:
            return [{"campaign": {"id": "900", "name": "Camp A", "resourceName": f"customers/{A}/campaigns/900"}}]
        if "FROM campaign_criterion" in q:
            return []
        if "FROM campaign" in q or "FROM ad_group" in q or "experiment" in q.lower():
            return []
        raise AssertionError("unexpected GAQL " + q[:120])

    def mutate(self, acct, ops, apply):
        self.mutates.append((json.loads(json.dumps(ops)), apply))
        if apply:
            return True, {"mutateOperationResponses": [{"userListResult": {"resourceName": RES(900 + i)}} for i in range(len(ops))]}
        return True, {}


class GBase(unittest.TestCase):
    def setUp(self):
        self.g = GFake()
        core.clear(A)
        for f in TMP.glob("live_*.jsonl"):
            f.unlink()
        self.p = [mock.patch.object(gapi, "base", lambda *a, **k: BASE), mock.patch.object(live, "query", self.g.query),
                  mock.patch.object(live, "mutate", self.g.mutate), mock.patch.object(gapi, "source", lambda a: "api"),
                  mock.patch.object(live, "_experiments", lambda a: live.experiments.empty())]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()

    def preview(self, changes):
        core.clear(A)
        core.add(A, changes)
        return live.preview(A)


WEBSITE = {"type": "user_list", "action": "create", "kind": "website", "name": "Red Sea visitors",
           "rules": [{"op": "contains", "value": "/egypt"}, {"op": "equals", "value": "https://example.org/red-sea/"}],
           "exclude": [{"op": "contains", "value": "/booking"}], "lifespan": 30}


class GoogleReading(GBase):
    def test_sizes_are_honest(self):
        self.assertEqual(audiences.size_range("ONE_THOUSAND_TO_TEN_THOUSAND"), "1,000-10,000")
        self.assertEqual(audiences.size_range("LESS_THAN_FIVE_HUNDRED"), "under 500")
        self.assertEqual(audiences.size_text(3000, "ONE_THOUSAND_TO_TEN_THOUSAND"), "about 3,000 (1,000-10,000)")
        self.assertEqual(audiences.size_text(80, "LESS_THAN_FIVE_HUNDRED"), "under 500")  # never an exact small count
        self.assertEqual(audiences.size_text(None, None), "unknown")

    def test_rows(self):
        r = {x["name"]: x for x in audiences.rows(A)["rows"]}
        self.assertEqual(r["Egypt visitors"]["used_by"], ['ad group "Camp B / Group 1" (excluded)', 'campaign "Camp A"', 'combined list "Combined one"'])
        self.assertEqual(r["Unused list"]["used_by"], [], "a removed criterion is not a use")
        self.assertTrue(r["Egypt visitors"]["editable"])
        self.assertEqual(r["Egypt visitors"]["rule"], 'URL contains "/egypt"')
        self.assertEqual((r["Egypt visitors"]["kind"], r["Egypt visitors"]["retention"]), ("website", "30 days"))
        self.assertFalse(r["Customer list one"]["editable"])
        self.assertTrue(r["Customer list one"]["customer_list"])
        self.assertIn("customer data", r["Customer list one"]["note"])
        self.assertFalse(r["Shared remarketing"]["editable"])
        self.assertEqual(r["Combined one"]["rule"], 'any of "Egypt visitors", "Shared remarketing"')
        self.assertEqual(r["Combined one"]["combine"], {"any": ["Egypt visitors", "Shared remarketing"]})
        self.assertEqual(r["Egypt visitors"]["size_display"], "under 500")
        self.assertEqual(audiences.rows(A)["rows"][-1]["name"], "Customer list one", "customer lists last")

    def test_grid_hides_raw(self):
        d = {**BASE, "camp_status": {}, "group_status": {}, "experiments": None, "tree": []}
        with mock.patch.object(gapi, "base", lambda *a, **k: d), mock.patch.object(gapi, "_cid", lambda a: a), \
                mock.patch.object(gapi, "stats", lambda *a, **k: {"stats": {}}):
            rows = gapi.grid(A, "user_lists")
        self.assertTrue(rows and all("_raw" not in r and "_logical" not in r for r in rows))


class GoogleChecks(GBase):
    def test_check(self):
        self.assertEqual(audiences.check(WEBSITE), ([], []))
        cases = [({"lifespan": 541}, "1-540"), ({"lifespan": 0}, "1-540"), ({"lifespan": None}, "how many days"),
                 ({"rules": [{"op": "regex", "value": "x"}]}, "op must be"), ({"rules": [{"op": "contains", "value": " "}]}, "empty"),
                 ({"rules": []}, "at least one URL rule"), ({"name": ""}, "needs a name"), ({"kind": "crm"}, "customer data"),
                 ({"members": ["a@b.c"]}, "customer data"), ({"match": "most"}, "match must")]
        for extra, want in cases:
            p, _ = audiences.check({**WEBSITE, **extra})
            self.assertTrue(any(want in x for x in p), (extra, p))
        self.assertEqual(audiences.check({**WEBSITE, "rules": [], "all_visitors": True})[0], [])
        self.assertIn("prepopulate", audiences.check({**WEBSITE, "prepopulate": True})[1][0])
        combo = {"type": "user_list", "action": "create", "kind": "combined", "name": "C", "any": ["A", "B"], "none": ["D"]}
        self.assertEqual(audiences.check(combo)[0], [])
        for extra, want in (({"any": ["A"], "none": []}, "at least two"), ({"any": [], "none": ["A", "B"]}, "must be on"),
                            ({"any": ["A", "A"]}, "twice")):
            self.assertTrue(any(want in x for x in audiences.check({**combo, **extra})[0]), extra)
        self.assertTrue(audiences.check({"type": "user_list", "action": "edit", "list": "X"})[0])
        self.assertEqual(audiences.check({"type": "user_list", "action": "remove", "list": "X"}), ([], []))

    def test_core_wiring(self):
        it = core.add(A, [WEBSITE])[0]
        self.assertEqual(it["problems"], [])
        self.assertTrue(core.live_only(WEBSITE))
        self.assertIn('New website list "Red Sea visitors" (30 days)', it["summary"])
        self.assertIn("not", it["summary"])

    def test_remove_in_use_refused_with_users(self):
        with self.assertRaises(ValueError) as ex:
            core.add(A, [{"type": "user_list", "action": "remove", "list": "Egypt visitors"}])
        msg = str(ex.exception)
        for who in ('campaign "Camp A"', 'ad group "Camp B / Group 1" (excluded)', 'combined list "Combined one"'):
            self.assertIn(who, msg)
        self.assertEqual(core.draft(A)["items"], [], "nothing added")
        with self.assertRaises(ValueError) as ex:
            core.add(A, [{"type": "user_list", "action": "remove", "list": "Customer list one"}])
        self.assertIn("customer data", str(ex.exception))
        self.assertEqual(len(core.add(A, [{"type": "user_list", "action": "remove", "list": "Unused list"}])), 1)


class GoogleLive(GBase):
    def test_create_website_ops(self):
        p = self.preview([{**WEBSITE, "prepopulate": True}])
        self.assertTrue(p["ok"], p)
        ops, apply = self.g.mutates[-1]
        self.assertFalse(apply, "a preview is validate-only")
        body = ops[0]["userListOperation"]["create"]
        self.assertEqual(body["membershipLifeSpan"], "30")
        self.assertEqual(body["ruleBasedUserList"]["prepopulationStatus"], "REQUESTED")
        f = body["ruleBasedUserList"]["flexibleRuleUserList"]
        self.assertEqual(f["inclusiveRuleOperator"], "OR")
        self.assertEqual([o["rule"]["ruleItemGroups"][0]["ruleItems"][0]["stringRuleItem"] for o in f["inclusiveOperands"]],
                         [{"operator": "CONTAINS", "value": "/egypt"}, {"operator": "EQUALS", "value": "https://example.org/red-sea/"}])
        self.assertEqual({o["lookbackWindowDays"] for o in f["inclusiveOperands"]}, {"30"})
        self.assertEqual(f["exclusiveOperands"][0]["rule"]["ruleItemGroups"][0]["ruleItems"][0]["name"], "url__")
        p = self.preview([{**WEBSITE, "match": "all"}])
        body = self.g.mutates[-1][0][0]["userListOperation"]["create"]
        self.assertNotIn("prepopulationStatus", body["ruleBasedUserList"], "Google refuses NONE: left out")
        self.assertEqual(body["ruleBasedUserList"]["flexibleRuleUserList"]["inclusiveRuleOperator"], "AND")
        self.preview([{**WEBSITE, "rules": [], "all_visitors": True}])
        f = self.g.mutates[-1][0][0]["userListOperation"]["create"]["ruleBasedUserList"]["flexibleRuleUserList"]
        self.assertEqual(f["inclusiveOperands"], [{"rule": {}, "lookbackWindowDays": "30"}])

    def test_create_name_taken_and_combined(self):
        p = self.preview([{**WEBSITE, "name": "Egypt visitors"}])
        self.assertFalse(p["ok"])
        self.assertIn("already exists", p["items"][0]["problems"][0])
        p = self.preview([{"type": "user_list", "action": "create", "kind": "combined", "name": "Both", "all": ["Egypt visitors"],
                           "none": ["Shared remarketing"]}])  # two lists, neither a customer nor a combined list
        self.assertTrue(p["ok"], p)
        rules = self.g.mutates[-1][0][0]["userListOperation"]["create"]["logicalUserList"]["rules"]
        self.assertEqual(rules, [{"operator": "ALL", "ruleOperands": [{"userList": RES(11)}]},
                                 {"operator": "NONE", "ruleOperands": [{"userList": RES(14)}]}])

    def test_same_push_reference_refused(self):
        n = len(self.g.mutates)
        p = self.preview([WEBSITE, {"type": "user_list", "action": "create", "kind": "combined", "name": "C",
                                    "any": ["Red Sea visitors", "Egypt visitors"]}])
        self.assertFalse(p["ok"])
        self.assertIn("push it first", p["items"][1]["problems"][0])
        self.assertEqual(len(self.g.mutates), n, "nothing sent to Google while a problem is open")

    def test_edit_is_name_description_days_only(self):
        p = self.preview([{"type": "user_list", "action": "edit", "list": "Egypt visitors", "new_name": "Egypt (all)", "lifespan": 60,
                           "description": "d"}])
        self.assertTrue(p["ok"], p)
        self.assertTrue(p["spend"], "longer membership is flagged")
        op = self.g.mutates[-1][0][0]["userListOperation"]
        self.assertEqual(op["updateMask"].split(","), ["name", "description", "membershipLifeSpan"])
        self.assertEqual(set(op["update"]), {"resourceName", "name", "description", "membershipLifeSpan"})
        undo = live.plan(A)["ops"][0]["undo"]["userListOperation"]["update"]
        self.assertEqual((undo["name"], undo["membershipLifeSpan"]), ("Egypt visitors", "30"))

    def test_undo_of_days_needs_the_tick(self):
        core.add(A, [{"type": "user_list", "action": "edit", "list": "Egypt visitors", "lifespan": 10}])  # shorter: no tick to push
        res = live.push(A)
        self.assertTrue(res["ok"], res)
        n = len(self.g.mutates)
        out = live.undo(A, res["push_id"])
        self.assertTrue(out.get("needs_spend"), out)
        self.assertEqual(len(self.g.mutates), n, "nothing sent before the tick")
        self.assertTrue(live.undo(A, res["push_id"], accept_spend=True)["ok"])
        core.add(A, [{"type": "user_list", "action": "edit", "list": "Unused list", "new_name": "Unused 2"}])
        res = live.push(A)
        self.assertTrue(live.undo(A, res["push_id"])["ok"], "undoing a rename needs no tick")
    def test_edit_refusals(self):
        for lst, want in (("Customer list one", "customer data"), ("Shared remarketing", "read and target only")):
            p = self.preview([{"type": "user_list", "action": "edit", "list": lst, "new_name": "x"}])
            self.assertIn(want, " ".join(p["items"][0]["problems"]))
    def test_remove_checked_live_then_push_and_undo(self):
        core.add(A, [{"type": "user_list", "action": "remove", "list": "Unused list"}])  # unused in the cache ...
        self.g.camp_users[RES(12)] = [{"campaign": {"name": "Camp Z"}, "campaignCriterion": {"negative": True}}]  # ... used live now
        p = live.preview(A)
        self.assertIn('campaign "Camp Z" (excluded)', " ".join(p["items"][0]["problems"]))
        self.g.camp_users.pop(RES(12))
        p = live.preview(A)
        self.assertTrue(p["ok"] and p["permanent"], p)
        res = live.push(A)
        self.assertFalse(res["ok"], "a permanent removal needs the tick")
        self.assertFalse(any(apply for _, apply in self.g.mutates))
        res = live.push(A, accept_permanent=True)
        self.assertTrue(res["ok"], res)
        self.assertEqual(self.g.mutates[-1], ([{"userListOperation": {"remove": RES(12)}}], True))
        with self.assertRaises(ValueError):
            live.undo(A, res["push_id"])  # nothing to bring back

    def test_push_create_then_undo_removes_it(self):
        core.add(A, [WEBSITE])
        res = live.push(A)
        self.assertTrue(res["ok"], res)
        created = RES(900)
        out = live.undo(A, res["push_id"])
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.g.mutates[-1], ([{"userListOperation": {"remove": created}}], True))
        self.assertEqual(self.g.mutates[-2], ([{"userListOperation": {"remove": created}}], False), "undo is checked first")

    def test_attach_uses_existing_audience_type(self):
        self.g.query  # attach a list to a campaign: the "audience" change type (core / live), unchanged
        it = core.add(A, [{"type": "audience", "campaign": "Camp A", "lists": ["Unused list"]}])[0]
        self.assertEqual(it["problems"], [])


# ---------------- Meta ----------------

AUDS = [
    {"id": "w1", "name": "Site visitors", "subtype": "WEBSITE", "approximate_count_lower_bound": 20, "approximate_count_upper_bound": 20,
     "retention_days": 180, "operation_status": {"code": 200, "description": "Normal"}},
    {"id": "e1", "name": "Page visitors", "subtype": "ENGAGEMENT", "approximate_count_lower_bound": 6100, "approximate_count_upper_bound": 7200,
     "retention_days": 365, "operation_status": {"code": 200, "description": "Normal"}},
    {"id": "e2", "name": "Small page", "subtype": "ENGAGEMENT", "approximate_count_lower_bound": 1000, "approximate_count_upper_bound": 1000,
     "retention_days": 365},
    {"id": "l1", "name": "LAL 1%", "subtype": "LOOKALIKE", "approximate_count_lower_bound": -1, "approximate_count_upper_bound": -1,
     "lookalike_spec": {"ratio": 0.01, "origin": [{"id": "e1", "name": "Page visitors"}]}},
    {"id": "c1", "name": "Past customers", "subtype": "CUSTOM", "approximate_count_lower_bound": 6400, "approximate_count_upper_bound": 7500},
    {"id": "w2", "name": "Unused site", "subtype": "WEBSITE", "approximate_count_lower_bound": 5000, "approximate_count_upper_bound": 6000,
     "retention_days": 30},
]
TG = {"geo_locations": {"countries": ["GB"]}, "age_min": 25, "age_max": 60, "custom_audiences": [{"id": "w1", "name": "Site visitors"}],
      "excluded_custom_audiences": [{"id": "c1", "name": "Past customers"}]}
DATA = {"account": {"currency": "GBP"}, "campaigns": [{"id": "cp1", "name": "Camp", "status": "ACTIVE", "effective_status": "ACTIVE",
                                                      "objective": "OUTCOME_SALES"}],
        "adsets": [{"id": "s1", "name": "Set one", "campaign_id": "cp1", "status": "ACTIVE", "effective_status": "ACTIVE", "targeting": TG}],
        "ads": [], "audiences": AUDS, "insights": {}, "fetched_at": "2026-11-17T10:00"}
WRULE = {"inclusions": {"operator": "or", "rules": [{"event_sources": [{"type": "pixel", "id": "px1"}], "retention_seconds": 30 * 86400,
                                                     "filter": {"operator": "and", "filters": [{"field": "url", "operator": "i_contains", "value": "/egypt"}]}}]}}


class MFake:
    def __init__(self):
        self.posts, self.gets, self.deletes, self.n = [], [], [], 0
        self.live_adsets = [{"id": "s1", "name": "Set one", "effective_status": "ACTIVE", "targeting": TG}]

    def get(self, path, **params):
        self.gets.append((path, params))
        if path in ("w1", "w2"):
            return {"id": path, "name": next(a["name"] for a in AUDS if a["id"] == path), "subtype": "WEBSITE", "description": "",
                    "rule": json.dumps(WRULE), "retention_days": 30}
        if path in ("e1", "l1"):
            return {"id": path, "name": "x", "subtype": "ENGAGEMENT" if path == "e1" else "LOOKALIKE", "description": "",
                    "rule": json.dumps({"inclusions": {"operator": "or", "rules": [{"event_sources": [{"type": "page", "id": "p1"}],
                                                                                    "retention_seconds": 365 * 86400, "filter": {
                                                                                        "operator": "and", "filters": [{"field": "event", "operator": "eq", "value": "page_visited"}]}}]}}),
                    "retention_days": 365}
        raise AssertionError(f"unexpected GET {path}")

    def get_all(self, path, **params):
        self.gets.append((path, params))
        if path.endswith("/adsets"):
            return self.live_adsets
        raise AssertionError(f"unexpected GET (all) {path}")

    def post(self, path, params, validate=False, retry_rate=True):
        self.posts.append((path, json.loads(json.dumps(params)), validate))
        if "customaudiences" in path or path in {a["id"] for a in AUDS}:
            if validate:
                raise AssertionError("an audience must never get a validate_only POST (not proven on Meta)")
        if validate:
            return {"success": True}
        self.n += 1
        return {"id": f"new{self.n}", "success": True}

    def delete(self, path):
        self.deletes.append(path)
        return {"success": True}


def mlive(level, oid):
    return {"name": oid, "status": "ACTIVE", "targeting": json.loads(json.dumps(TG))}


class MBase(unittest.TestCase):
    def setUp(self):
        self.f = MFake()
        meta._cur.act = "act_123"
        core.clear(meta.acct())
        for f in TMP.glob("live_meta_*.jsonl"):
            f.unlink()
        self.p = [mock.patch.object(meta, "load", lambda *a, **k: DATA), mock.patch.object(meta, "get", self.f.get),
                  mock.patch.object(meta, "get_all", self.f.get_all), mock.patch.object(meta, "post", self.f.post),
                  mock.patch.object(meta, "delete", self.f.delete), mock.patch.object(meta, "_live", mlive),
                  mock.patch.object(meta, "_pixel", lambda: "px1"), mock.patch.object(meta, "_page_for_account", lambda d=None: "p1"),
                  mock.patch.object(meta, "_identity", lambda src_cr=None, need_ig=False: {"page_id": "p1", "instagram_user_id": "ig1"}),
                  mock.patch.object(meta, "refresh_account", lambda: {"fetched_at": "x"})]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()

    def add1(self, c):
        return meta.add([c])[0]


MWEB = {"type": "meta_audience", "action": "create", "kind": "website", "name": "Egypt 30d", "retention_days": 30,
        "rules": [{"field": "url", "op": "contains", "value": "/egypt"}, {"field": "event", "value": "SelectCabin"}],
        "exclude": [{"field": "url", "op": "contains", "value": "/booking"}]}


class MetaReading(MBase):
    def test_sizes_are_honest(self):
        s = {a["id"]: ma.size_text(a) for a in AUDS}
        self.assertEqual(s["e1"], "6,100-7,200")
        self.assertIn("small (Meta shows 20", s["w1"])
        self.assertIn("small (Meta shows 1,000", s["e2"])
        self.assertEqual(s["l1"], "not available yet")

    def test_rows(self):
        r = {x["id"]: x for x in ma.rows(DATA)["rows"]}
        self.assertEqual(r["w1"]["used_by"], ['ad set "Set one"'])
        self.assertEqual(r["e1"]["used_by"], ['seed of lookalike "LAL 1%"'])
        self.assertEqual(r["c1"]["used_by"], ['ad set "Set one" (excluded)'])
        self.assertTrue(r["c1"]["customer_list"] and not r["c1"]["editable"] and not r["c1"]["seed_ok"])
        self.assertIn("customer data", r["c1"]["note"])
        self.assertEqual(r["l1"]["lookalike"], "1% of Page visitors")
        self.assertEqual((r["w1"]["retention"], r["w1"]["type"]), ("180 days", "Website"))
        self.assertFalse(r["w1"]["rules_editable"] or r["e1"]["rules_editable"], "rules are never edited on existing audiences")
        g = {x["id"]: x for x in meta.rows(DATA, "audiences")}
        self.assertEqual(g["w1"]["used_by"], 'ad set "Set one"')

    def test_detail_shows_rules_read_only(self):
        d = ma.detail("w1", meta)
        self.assertFalse(d["rules_editable"])
        self.assertIn('url contains "/egypt"', d["rule"])
        self.assertIn("pixel, 30 days", d["rule"])
        self.assertEqual(ma.detail("c1", meta)["customer_list"], True)
        self.assertNotIn("c1", [g[0] for g in self.f.gets], "a customer list is never read")


class MetaChecks(MBase):
    def test_website_and_engagement(self):
        it = self.add1(MWEB)
        self.assertEqual((it["problems"], it["warnings"]), ([], []))
        self.assertIn('Meta new website audience "Egypt 30d" (30 days): URL contains "/egypt" or event SelectCabin; not URL contains "/booking"',
                      it["summary"])
        for extra, want in (({"retention_days": 181}, "1-180"), ({"rules": [{"field": "event", "value": "bad event"}]}, "pixel event"),
                            ({"rules": [{"field": "referrer", "value": "x"}]}, "field must be"), ({"rules": []}, "at least one"),
                            ({"kind": "custom"}, "customer data"), ({"schema": ["EMAIL"]}, "customer data")):
            self.assertTrue(any(want in p for p in self.add1({**MWEB, **extra})["problems"]), extra)
        eng = {"type": "meta_audience", "action": "create", "kind": "engagement", "name": "IG", "source": "instagram", "event": "engaged",
               "retention_days": 365}
        it = self.add1(eng)
        self.assertEqual(it["problems"], [])
        self.assertIn("Instagram account", it["summary"])
        for extra, want in (({"source": "tiktok"}, "source must be"), ({"event": "page_visited"}, "event for instagram"),
                            ({"retention_days": 731}, "1-730"), ({"source": "video", "event": "3s"}, "not available yet"),
                            ({"source": "lead_form", "event": "opened"}, "form_ids"),
                            ({"source": "lead_form", "event": "opened", "form_ids": ["77"], "retention_days": 91}, "1-90")):
            self.assertTrue(any(want in p for p in self.add1({**eng, **extra})["problems"]), extra)

    def test_lookalike(self):
        lal = {"type": "meta_audience", "action": "create", "kind": "lookalike", "name": "LAL", "seed_id": "e1", "ratio": 3}
        it = self.add1(lal)
        self.assertEqual((it["problems"], it["warnings"]), ([], []))
        self.assertIn('3% like "Page visitors"', it["summary"])
        for extra, want in (({"country": "GB"}, "never sends a country"), ({"location_spec": {"geo_locations": {}}}, "never sends a country"),
                            ({"seed_id": "c1"}, "customer list"), ({"seed_id": "l1"}, "lookalike can't be the seed"),
                            ({"seed_id": "zz"}, "not found"), ({"ratio": 11}, "1-10"), ({"ratio": None, "from": 3, "to": 2}, "from below")):
            self.assertTrue(any(want in p for p in self.add1({**lal, **extra})["problems"]), extra)
        self.assertEqual(self.add1({**lal, "ratio": None, "from": 1, "to": 3})["problems"], [])
        self.assertTrue(any("fewer than 100" in w for w in self.add1({**lal, "seed_id": "w1"})["warnings"]), "20 people shown: warned")
        self.assertTrue(any("at least 100" in w for w in self.add1({**lal, "seed_id": "e2"})["warnings"]), "floor value: warned")

    def test_remove_in_use_refused_with_users(self):
        for aid, who in (("w1", 'ad set "Set one"'), ("e1", 'seed of lookalike "LAL 1%"')):
            with self.assertRaises(ValueError) as ex:
                meta.add([{"type": "meta_audience", "action": "remove", "id": aid}])
            self.assertIn(who, str(ex.exception))
        self.assertEqual(core.draft(meta.acct())["items"], [])
        with self.assertRaises(ValueError):  # in use (and a customer list: read-only either way)
            meta.add([{"type": "meta_audience", "action": "remove", "id": "c1"}])
        with mock.patch.dict(DATA, {"adsets": []}):
            it = self.add1({"type": "meta_audience", "action": "remove", "id": "c1"})
        self.assertIn("customer data", " ".join(it["problems"]), "customer lists are read-only")

    def test_edit_checks(self):
        self.assertEqual(self.add1({"type": "meta_audience", "action": "edit", "id": "w1", "name": "New name"})["problems"], [])
        for c, want in (({"id": "w1"}, "nothing to change"), ({"id": "c1", "name": "x"}, "customer data")):
            p = self.add1({"type": "meta_audience", "action": "edit", **c})["problems"]
            self.assertTrue(any(want in x for x in p), (c, p))


class MetaLive(MBase):
    def test_preview_sends_nothing_for_audiences(self):
        meta.add([MWEB, {"type": "meta_audience", "action": "create", "kind": "lookalike", "name": "LAL", "seed_id": "e1", "from": 1, "to": 3},
                  {"type": "meta_audience", "action": "edit", "id": "w2", "name": "Renamed"}])
        p = meta.preview()
        self.assertTrue(p["ok"], p)
        self.assertEqual(self.f.posts, [], "no Meta POST before Push live")

    def test_push_create_and_undo_deletes(self):
        meta.add([MWEB, {"type": "meta_audience", "action": "create", "kind": "lookalike", "name": "LAL", "seed_id": "e1", "from": 1, "to": 3},
                  {"type": "meta_audience", "action": "create", "kind": "engagement", "name": "Leads", "source": "lead_form", "event": "opened",
                   "form_ids": ["777"], "retention_days": 90}])
        res = meta.push()
        self.assertTrue(res["ok"], res)
        made = [(p, x) for p, x, v in self.f.posts if p.endswith("/customaudiences")]
        self.assertEqual(len(made), 3)
        web = next(x for _, x in made if x["name"] == "Egypt 30d")
        rule = json.loads(web["rule"])
        self.assertEqual(rule["inclusions"]["operator"], "or")
        self.assertEqual([r["filter"]["filters"] for r in rule["inclusions"]["rules"]],
                         [[{"field": "url", "operator": "i_contains", "value": "/egypt"}], [{"field": "event", "operator": "eq", "value": "SelectCabin"}]])
        self.assertEqual({r["retention_seconds"] for r in rule["inclusions"]["rules"]}, {30 * 86400})
        self.assertEqual(rule["inclusions"]["rules"][0]["event_sources"], [{"type": "pixel", "id": "px1"}])
        self.assertIn("exclusions", rule)
        lal = next(x for _, x in made if x["name"] == "LAL")
        self.assertEqual((lal["subtype"], lal["origin_audience_id"], json.loads(lal["lookalike_spec"])),
                         ("LOOKALIKE", "e1", {"starting_ratio": 0.01, "ratio": 0.03}))
        self.assertNotIn("country", json.dumps(made).lower(), "never a country")
        self.assertNotIn("location", json.dumps(made).lower())
        lead = json.loads(next(x for _, x in made if x["name"] == "Leads")["rule"])["inclusions"]["rules"][0]
        self.assertEqual((lead["event_sources"], lead["filter"]["filters"][0]["value"], lead["retention_seconds"]),
                         ([{"type": "lead", "id": "777"}], "lead_generation_opened", 90 * 86400), "the lead form id, not the Page id")
        out = meta.undo(res["push_id"])
        self.assertTrue(out["ok"], out)
        self.assertEqual(sorted(self.f.deletes), ["new1", "new2", "new3"])

    def test_edit_push_and_undo_is_name_only(self):
        meta.add([{"type": "meta_audience", "action": "edit", "id": "w1", "name": "Renamed", "description": "d"}])
        p = meta.preview()
        self.assertTrue(p["ok"] and not p["spend"], p)
        res = meta.push()
        self.assertTrue(res["ok"], res)
        self.assertEqual(self.f.posts[-1], ("w1", {"name": "Renamed", "description": "d"}, False))
        self.assertTrue(meta.undo(res["push_id"])["ok"], "no tick: who is in the audience didn't change")
        self.assertEqual(self.f.posts[-1], ("w1", {"name": "Site visitors", "description": ""}, False))
    def test_delete_checked_live_and_final(self):
        meta.add([{"type": "meta_audience", "action": "remove", "id": "w2"}])
        self.f.live_adsets = [{"id": "s9", "name": "New set", "effective_status": "ACTIVE", "targeting": {"excluded_custom_audiences": [{"id": "w2"}]}}]
        p = meta.preview()
        self.assertIn('ad set "New set" (excluded)', " ".join(p["items"][0]["problems"]))
        self.f.live_adsets = []
        p = meta.preview()
        self.assertTrue(p["ok"] and p["permanent"], p)
        self.assertFalse(meta.push()["ok"], "a delete needs the tick")
        self.assertEqual(self.f.deletes, [])
        res = meta.push(accept_permanent=True)
        self.assertTrue(res["ok"], res)
        self.assertEqual(self.f.deletes, ["w2"])
        h = meta.history()[0]
        self.assertFalse(h["can_undo"])

    def test_adset_attach_detach(self):
        it = self.add1({"type": "meta_targeting", "level": "adset", "id": "s1", "audiences_add": ["e1"], "excluded_remove": ["c1"]})
        self.assertEqual(it["problems"], [])
        self.assertIn('include "Page visitors"; stop excluding "Past customers"', it["summary"])
        p = meta.preview()
        self.assertTrue(p["ok"] and p["spend"], p)
        res = meta.push(accept_spend=True)
        self.assertTrue(res["ok"], res)
        path, params, _ = self.f.posts[-1]
        tg = params["targeting"]
        self.assertEqual(([a["id"] for a in tg["custom_audiences"]], "excluded_custom_audiences" in tg), (["w1", "e1"], False))
        bad = self.add1({"type": "meta_targeting", "level": "adset", "id": "s1", "audiences_add": ["zz"], "excluded_add": ["zz"]})
        self.assertTrue(any("not found" in x for x in bad["problems"]) and any("both" in x for x in bad["problems"]))
        self.assertTrue(self.add1({"type": "meta_targeting", "level": "adset", "id": "s1"})["problems"])
        core.clear(meta.acct())
        self.add1({"type": "meta_targeting", "level": "adset", "id": "s1", "audiences_remove": ["e2"]})
        self.assertIn("is not included", " ".join(meta.preview()["items"][0]["problems"]))


class Mcp(MBase):
    def test_tools(self):
        names = {t["name"] for t in mcp_server.TOOLS}
        self.assertTrue({"audiences_list", "audience_add_to_draft"} <= names)
        with mock.patch.object(meta, "use", lambda k: "act_123"):
            rows = mcp_server.call("audiences_list", {"account": "meta"})["rows"]
            self.assertEqual(len(rows), len(AUDS))
            items = mcp_server.call("audience_add_to_draft", {"account": "meta", "changes": [MWEB], "note": "why"})
            self.assertEqual((items[0]["by"], items[0]["change"]["type"]), ("claude", "meta_audience"))
            for bad in ({"type": "meta_status", "level": "ad", "id": "a1", "status": "active"},
                        {"type": "meta_targeting", "level": "adset", "id": "s1", "countries": ["DE"], "audiences_add": ["e1"]}):
                with self.assertRaises(ValueError):
                    mcp_server.call("audience_add_to_draft", {"account": "meta", "changes": [bad]})
        self.assertEqual(self.f.posts, [], "the MCP tools only read and draft")
        with mock.patch.object(gapi, "base", lambda *a, **k: BASE), mock.patch.object(core, "resolve", lambda a: A):
            self.assertEqual(mcp_server.call("audiences_list", {"account": "ml"})["platform"], "google")
            core.clear(A)
            self.assertEqual(len(mcp_server.call("audience_add_to_draft", {"account": "ml", "changes": [WEBSITE]})), 1)
            with self.assertRaises(ValueError):
                mcp_server.call("audience_add_to_draft", {"account": "ml", "changes": [{"type": "campaign", "campaign": "X", "status": "enabled"}]})
            with self.assertRaises(ValueError):
                mcp_server.call("audience_add_to_draft", {"account": "ml", "changes": [{"type": "user_list", "action": "remove", "list": "Egypt visitors"}]})
        self.assertFalse(any("push" in t["name"] and "audience" in t["name"] for t in mcp_server.TOOLS))


class Routes(MBase):
    def test_http_routes(self):
        import http.client
        import threading
        from http.server import ThreadingHTTPServer
        import server

        srv = ThreadingHTTPServer(("127.0.0.1", 0), server.H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            def use(k):
                meta._cur.act = "act_123"
                return "act_123"
            with mock.patch.object(meta, "use", use), mock.patch.object(gapi, "base", lambda *a, **k: BASE), \
                    mock.patch.object(core, "resolve", lambda a: A):
                def call(path):
                    c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
                    c.request("GET", path)
                    r = c.getresponse()
                    return r.status, r.read()
                st, body = call("/api/audiences?acct=meta")
                self.assertEqual((st, json.loads(body)["platform"]), (200, "meta"))
                st, body = call("/api/audiences/detail?acct=meta&id=w1")
                self.assertEqual((st, json.loads(body)["rules_editable"]), (200, False))
                st, body = call("/api/audiences?acct=ml")
                self.assertEqual((st, json.loads(body)["platform"]), (200, "google"))
                st, body = call("/static/audiences.js")
                self.assertEqual(st, 200)
                self.assertNotIn(chr(0x2014).encode(), body)
            self.assertEqual(self.f.posts, [], "routes only read")
        finally:
            srv.shutdown()
            srv.server_close()


PARAM = {**WEB, "resourceName": RES(16), "id": "16", "name": "Param list", "ruleBasedUserList": {"flexibleRuleUserList": {
    "inclusiveRuleOperator": "OR", "inclusiveOperands": [
        {"rule": {"ruleItemGroups": [{"ruleItems": [{"name": "url__", "stringRuleItem": {"operator": "CONTAINS", "value": "/a"}},
                                                    {"name": "ecomm_pagetype", "stringRuleItem": {"operator": "EQUALS", "value": "cart"}}]}]},
         "lookbackWindowDays": "30"}],
    "exclusiveOperands": [{"rule": {"ruleItemGroups": [{"ruleItems": [{"name": "ecomm_pagetype", "stringRuleItem": {"operator": "EQUALS", "value": "x"}}]}]}}]}}}


class ReviewFixes(GBase):
    """a08 review: customer lists never combined; rules Studio can't rebuild are never rewritten."""

    def test_customer_list_never_combined(self):
        with self.assertRaises(ValueError) as ex:
            core.add(A, [{"type": "user_list", "action": "create", "kind": "combined", "name": "Mix", "any": ["Egypt visitors", "Customer list one"]}])
        self.assertIn("customer list", str(ex.exception))
        self.assertEqual(core.draft(A)["items"], [])
        # the push checks it live too (a stale cache, or a draft made elsewhere)
        with mock.patch.object(audiences, "crm_member_refusals", lambda a, c: []):
            p = self.preview([{"type": "user_list", "action": "create", "kind": "combined", "name": "Mix", "any": ["Egypt visitors", "Customer list one"]}])
        self.assertFalse(p["ok"])
        self.assertIn("customer list", " ".join(p["items"][0]["problems"]))
        self.assertFalse(any(apply is False and "Mix" in json.dumps(o) for o, apply in self.g.mutates), "never sent to Google")

    def test_rules_with_custom_parameters_shown_honestly(self):
        LISTS.append(PARAM)
        base = {**BASE, "rows": {**BASE["rows"], "user_lists": BASE["rows"]["user_lists"] + [ul_row(PARAM)]}}
        try:
            with mock.patch.object(gapi, "base", lambda *a, **k: base):
                r = next(x for x in audiences.rows(A)["rows"] if x["name"] == "Param list")
                self.assertTrue(r["editable"])
                self.assertIn('custom rule "ecomm_pagetype"', r["rule"])
                p = self.preview([{"type": "user_list", "action": "edit", "list": "Param list", "new_name": "Param list 2"}])
                self.assertTrue(p["ok"], p)
                self.assertEqual(self.g.mutates[-1][0][0]["userListOperation"]["updateMask"], "name", "a rename touches the name only")
        finally:
            LISTS.remove(PARAM)


class MetaReviewFixes(MBase):
    def spend(self, c):
        core.clear(meta.acct())
        self.add1({"type": "meta_targeting", "level": "adset", "id": "s1", **c})
        p = meta.preview()
        self.assertTrue(p["ok"], p)
        return p["spend"]

    def test_widening_needs_the_tick(self):
        self.assertTrue(self.spend({"audiences_add": ["w2"]}), "a second include (OR) adds people")
        self.assertTrue(self.spend({"audiences_remove": ["w1"]}))
        self.assertTrue(self.spend({"excluded_remove": ["c1"]}))
        self.assertFalse(self.spend({"excluded_add": ["w2"]}), "an exclusion narrows")

    def test_undo_that_widens_needs_the_tick(self):
        core.clear(meta.acct())
        self.add1({"type": "meta_targeting", "level": "adset", "id": "s1", "excluded_add": ["w2"]})
        res = meta.push()
        self.assertTrue(res["ok"], res)
        n = len(self.f.posts)
        out = meta.undo(res["push_id"])
        self.assertTrue(out.get("needs_spend"), out)
        self.assertEqual(len(self.f.posts), n, "nothing sent before the tick")
        self.assertTrue(meta.history()[0]["undo_spend"])
        self.assertTrue(meta.undo(res["push_id"], accept_spend=True)["ok"])
        core.clear(meta.acct())
        self.add1({"type": "meta_targeting", "level": "adset", "id": "s1", "age_min": 30})
        res = meta.push()
        self.assertTrue(meta.undo(res["push_id"]).get("needs_spend"), "undoing a narrower age range widens it again")


ALL_OPS = ["CONTAINS", "EQUALS", "STARTS_WITH", "ENDS_WITH", "NOT_CONTAINS", "NOT_EQUALS", "NOT_STARTS_WITH", "NOT_ENDS_WITH"]
OPS_LIST = {**WEB, "resourceName": RES(18), "id": "18", "name": "Ops list", "ruleBasedUserList": {"flexibleRuleUserList": {
    "inclusiveRuleOperator": "OR", "inclusiveOperands": [
        {"rule": {"ruleItemGroups": [{"ruleItems": [{"name": "url__", "stringRuleItem": {"operator": op, "value": f"/{op.lower()}"}}]}]},
         "lookbackWindowDays": str(10 + i)} for i, op in enumerate(ALL_OPS)]}}}


class Round3(GBase):
    """a08 review round 3: one operator form, rename-only saves send no rules, customer lists checked by id."""

    def test_every_operator_one_form_on_create(self):
        js = (HERE / "static/audiences.js").read_text()
        for key, api in audiences.URL_OPS.items():
            self.assertIn(f'["{key}", ', js, "the UI offers exactly the engine's key")
            self.assertEqual(audiences.OP_LABEL[api], key)
        rules = [{"op": k, "value": f"/{k}"} for k in audiences.URL_OPS]
        p = self.preview([{**WEBSITE, "rules": rules, "exclude": []}])
        self.assertTrue(p["ok"], p)
        f = self.g.mutates[-1][0][0]["userListOperation"]["create"]["ruleBasedUserList"]["flexibleRuleUserList"]
        self.assertEqual([o["rule"]["ruleItemGroups"][0]["ruleItems"][0]["stringRuleItem"]["operator"] for o in f["inclusiveOperands"]],
                         ["CONTAINS", "EQUALS", "STARTS_WITH", "ENDS_WITH", "NOT_CONTAINS", "NOT_EQUALS", "NOT_STARTS_WITH", "NOT_ENDS_WITH"])
    def test_customer_list_checked_by_resolved_id(self):
        import sqlite3
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.execute("create table UserList(name text, remoteId integer)")
        db.execute("insert into UserList values ('VIP old name', 13)")  # list 13 is a customer list, renamed in Google since
        with mock.patch.object(core, "_db", lambda a: db), mock.patch.object(gapi, "source", lambda a: "editor"):
            p = self.preview([{"type": "user_list", "action": "create", "kind": "combined", "name": "Mix2", "any": ["Egypt visitors", "VIP old name"]}])
        self.assertFalse(p["ok"])
        self.assertIn("customer list", " ".join(p["items"][0]["problems"]))
        self.assertFalse(any("Mix2" in json.dumps(o) for o, _ in self.g.mutates), "never sent to Google")

    def test_customer_check_unreadable_refuses(self):
        real = self.g.query

        def broken(acct, q):
            if "CRM_BASED" in q:
                raise RuntimeError("Google Ads API: unavailable")
            return real(acct, q)
        with mock.patch.object(live, "query", broken):
            p = self.preview([{"type": "user_list", "action": "create", "kind": "combined", "name": "Mix", "any": ["Egypt visitors", "Unused list"]}])
        self.assertFalse(p["ok"])
        self.assertIn("can't read which lists are customer or combined lists", " ".join(p["items"][0]["problems"]))
        core.clear(A)
        with mock.patch.object(gapi, "base", mock.Mock(side_effect=RuntimeError("no data"))):
            with self.assertRaises(ValueError):
                core.add(A, [{"type": "user_list", "action": "create", "kind": "combined", "name": "Mix", "any": ["Egypt visitors", "Unused list"]}])


class MetaRound3(MBase):
    def test_every_targeting_undo_needs_the_tick(self):
        tg = {**TG, "custom_audiences": [{"id": "w1", "name": "Site visitors"}, {"id": "w2", "name": "Unused site"}]}
        with mock.patch.object(meta, "_live", lambda level, oid: {"name": oid, "status": "ACTIVE", "targeting": json.loads(json.dumps(tg))}):
            self.add1({"type": "meta_targeting", "level": "adset", "id": "s1", "audiences_remove": ["w2"]})
            res = meta.push(accept_spend=True)
            self.assertTrue(res["ok"], res)
            n = len(self.f.posts)
            out = meta.undo(res["push_id"])
            self.assertTrue(out.get("needs_spend"), out)
            self.assertEqual(len(self.f.posts), n, "nothing sent without the tick")
            self.assertTrue(meta.undo(res["push_id"], accept_spend=True)["ok"])



G_RULE_EDITS = [{"rules": [{"op": "contains", "value": "/x"}]}, {"exclude": [{"op": "contains", "value": "/x"}]}, {"match": "all"},
                {"any": ["Egypt visitors", "Unused list"]}, {"all": ["Unused list"]}, {"none": ["Unused list"]}, {"all_visitors": True}]
M_RULE_EDITS = [{"rules": [{"field": "url", "op": "contains", "value": "/x"}]}, {"exclude": [{"field": "url", "op": "contains", "value": "/x"}]},
                {"match": "all"}, {"retention_days": 7}, {"source": "page"}, {"event": "visited"}, {"form_ids": ["1"]}, {"seed_id": "e1"},
                {"ratio": 2}]


class NoRuleEdits(GBase):
    """Rule editing on existing audiences is cut: every edit path refuses rule fields, on both platforms."""

    def test_google_every_path(self):
        for extra in G_RULE_EDITS:
            c = {"type": "user_list", "action": "edit", "list": "Combined one" if any(k in extra for k in ("any", "all", "none")) else "Egypt visitors",
                 "new_name": "x", **extra}
            core.clear(A)
            with self.assertRaises(ValueError) as ex:  # the UI's POST /api/draft/add and add_to_draft both go through core.add
                core.add(A, [c])
            self.assertIn("rules can't be changed on an existing audience; create a new one", str(ex.exception), extra)
            with mock.patch.object(core, "resolve", lambda a: A), self.assertRaises(ValueError):
                mcp_server.call("audience_add_to_draft", {"account": "ml", "changes": [c]})
            with mock.patch.object(core, "resolve", lambda a: A), self.assertRaises(ValueError):
                mcp_server.call("add_to_draft", {"account": "ml", "changes": [c]})
            self.assertEqual(audiences.check(c)[0], ["rules can't be changed on an existing audience; create a new one"])
            # a draft that got in some other way (team sync, an older Studio) is refused at push, nothing sent
            n = len(self.g.mutates)
            with self.assertRaises(ValueError):
                audiences.ops(live.Ids(A, "api"), c)
            self.assertEqual(len(self.g.mutates), n)
        self.assertEqual(core.draft(A)["items"], [])

    def test_ui_payload(self):
        import http.client
        import threading
        from http.server import ThreadingHTTPServer
        import server
        srv = ThreadingHTTPServer(("127.0.0.1", 0), server.H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with mock.patch.object(core, "resolve", lambda a: A):
                c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
                c.request("POST", "/api/draft/add", json.dumps({"acct": "ml", "changes": [
                    {"type": "user_list", "action": "edit", "list": "Egypt visitors", "new_name": "E2", "rules": [{"op": "contains", "value": "/egypt"}]}]}),
                    {"Content-Type": "application/json", "X-Studio-Press": server.PRESS})
                r = c.getresponse()
                body = r.read().decode()
                self.assertNotEqual(r.status, 200)
                self.assertIn("rules can't be changed on an existing audience", body)
            self.assertEqual(core.draft(A)["items"], [])
        finally:
            srv.shutdown()
            srv.server_close()

    def test_nested_combined_refused(self):
        with self.assertRaises(ValueError) as ex:
            core.add(A, [{"type": "user_list", "action": "create", "kind": "combined", "name": "Nest", "any": ["Combined one", "Unused list"]}])
        self.assertIn("can't contain another one", str(ex.exception))
        with mock.patch.object(audiences, "crm_member_refusals", lambda a, c: []):  # at push, by resolved id
            p = self.preview([{"type": "user_list", "action": "create", "kind": "combined", "name": "Nest", "any": ["Combined one", "Unused list"]}])
        self.assertIn("can't contain another one", " ".join(p["items"][0]["problems"]))
        self.assertFalse(any("Nest" in json.dumps(o) for o, _ in self.g.mutates))


class MetaNoRuleEdits(MBase):
    def test_meta_every_path(self):
        for extra in M_RULE_EDITS:
            c = {"type": "meta_audience", "action": "edit", "id": "w2", "name": "x", **extra}
            core.clear(meta.acct())
            with self.assertRaises(ValueError) as ex:
                meta.add([c])
            self.assertIn("rules can't be changed on an existing audience; create a new one", str(ex.exception), extra)
            with mock.patch.object(meta, "use", lambda k: "act_123"):
                for tool in ("audience_add_to_draft", "add_to_draft"):
                    with self.assertRaises(ValueError):
                        mcp_server.call(tool, {"account": "meta", "changes": [c]})
            self.assertEqual(ma.check(c, DATA)[0], ["rules can't be changed on an existing audience; create a new one"])
            with self.assertRaises(ValueError):
                ma.ops_for(c, meta)
        self.assertEqual(self.f.posts, [])
        self.assertEqual(core.draft(meta.acct())["items"], [])

    def test_undo_of_create_checks_ad_sets_live(self):
        meta.add([MWEB])
        res = meta.push()
        self.assertTrue(res["ok"], res)
        made = [p for p, x, v in self.f.posts if p.endswith("/customaudiences")]
        self.assertEqual(len(made), 1)
        self.f.live_adsets = [{"id": "s5", "name": "Now uses it", "effective_status": "ACTIVE", "targeting": {"custom_audiences": [{"id": "new1"}]}}]
        out = meta.undo(res["push_id"])
        self.assertFalse(out["ok"])
        self.assertIn('ad set "Now uses it"', out["message"])
        self.assertEqual(self.f.deletes, [], "not deleted")

        def broken(path, **params):
            raise meta.MetaError({"message": "busy"})
        with mock.patch.object(meta, "get_all", broken):
            out = meta.undo(res["push_id"])
        self.assertFalse(out["ok"])
        self.assertIn("can't read the ad sets", out["message"])
        self.assertEqual(self.f.deletes, [])
        self.f.live_adsets = []
        self.assertTrue(meta.undo(res["push_id"])["ok"])
        self.assertEqual(self.f.deletes, ["new1"])


class Text(unittest.TestCase):
    def test_no_em_dash(self):
        for f in ("audiences.py", "meta_audiences.py", "static/audiences.js", "tests/test_audiences.py"):
            self.assertNotIn(chr(0x2014), (HERE / f).read_text(), f)


if __name__ == "__main__":
    unittest.main(verbosity=1)
