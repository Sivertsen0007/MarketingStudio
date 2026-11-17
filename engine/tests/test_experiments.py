"""Experiment trial campaigns: read-only in the grids and tree, refused in the draft, Send to Editor and Push live;
a warning (not a block) for a base campaign with a running experiment.

Run from the engine folder:  python3 -m unittest tests/test_experiments.py
A fake Google Ads Editor folder (GAE_EDITOR_DIR) and a temp MSTUDIO_HOME; the Google Ads API is replaced by fakes.
Never touches the real data folder, Editor's real database or the Keychain.
"""
import os as _os, tempfile as _tf
_os.environ["MSTUDIO_HOME"] = _tf.mkdtemp(prefix="mstudio-test-")
_os.environ["MSTUDIO_KEYCHAIN"] = "0"
_EDITOR = _tf.mkdtemp(prefix="gae-editor-test-")
_os.environ["GAE_EDITOR_DIR"] = _EDITOR

import json
import re
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core  # noqa: E402
import draftstore  # noqa: E402
import experiments  # noqa: E402
import gae  # noqa: E402
import gapi  # noqa: E402
import live  # noqa: E402

ED_ACCT = "1111111111"   # downloaded in the fake Editor
API_ACCT = "2222222222"  # API only
TRIAL, BASE, PLAIN = "Brand trial", "Brand", "Generic"
CAMPAIGN_COLS = ("localId integer primary key, remoteId integer, name text, status integer, campaignType integer, state integer, "
                 "trialParentId integer, trialParentName text, comment text")


def _editor_db(path, trial_column=True):
    db = sqlite3.connect(str(path))
    cols = CAMPAIGN_COLS if trial_column else CAMPAIGN_COLS.replace("trialParentId integer, trialParentName text, ", "")
    db.execute(f"create table Campaign ({cols})")
    db.execute("create table AdGroup (localId integer primary key, remoteId integer, name text, parentId integer, status integer, "
               "state integer, comment text, maxCpc integer)")
    db.execute("create table GeoLocation (remoteId integer, locationShortName text, countryCode text, locationPath text, reach integer)")
    rows = [(1, 101, BASE, 0, 0, 0, 0, ""), (2, 102, PLAIN, 0, 0, 0, 0, ""),
            (3, 103, TRIAL, 0, 0, 0, (gae.PARENT_CAMPAIGN << 32) | 1, BASE)]
    for r in rows:
        if trial_column:
            db.execute("insert into Campaign (localId, remoteId, name, status, campaignType, state, trialParentId, trialParentName) "
                       "values (?,?,?,?,?,?,?,?)", r)
        else:
            db.execute("insert into Campaign (localId, remoteId, name, status, campaignType, state) values (?,?,?,?,?,?)", r[:6])
    for i, (camp, name) in enumerate(((1, "Core"), (2, "Wide"), (3, "Core")), 1):
        db.execute("insert into AdGroup (localId, remoteId, name, parentId, status, state) values (?,?,?,?,0,0)",
                   (i, 200 + i, name, (gae.PARENT_CAMPAIGN << 32) | camp))
    db.commit()
    db.close()


vdir = Path(_EDITOR) / "1"
vdir.mkdir()
sqlite3.connect(str(vdir / "ape.db")).close()
_editor_db(vdir / f"ape_{ED_ACCT}.db")


def _item(change, iid="x1"):
    return {"id": iid, "change": change, "by": "you", "note": "", "ts": "2026-11-17T10:00:00", "summary": "s", "problems": [],
            "warnings": []}


# API fakes: three campaigns (ids 101-103), 103 is the trial of 101 in a running experiment
CAMP_ROWS = [{"campaign": {"id": "101", "name": BASE, "experimentType": "BASE"}},
             {"campaign": {"id": "102", "name": PLAIN, "experimentType": "BASE"}},
             {"campaign": {"id": "103", "name": TRIAL, "experimentType": "EXPERIMENT"}}]
ARM_ROWS = [{"experimentArm": {"resourceName": "customers/2/experimentArms/9~1", "experiment": "customers/2/experiments/9", "control": True,
                               "campaigns": ["customers/2/campaigns/101"]}, "experiment": {"status": "ENABLED"}},
            {"experimentArm": {"resourceName": "customers/2/experimentArms/9~2", "experiment": "customers/2/experiments/9",
                               "campaigns": ["customers/2/campaigns/103"]}, "experiment": {"status": "ENABLED"}}]


class FakeApi:
    """Stands in for live.query / live.mutate: answers the queries Push live asks, records every mutate."""

    def __init__(self, fail_experiments=False):
        self.mutates, self.queries, self.fail = [], [], fail_experiments

    def query(self, acct, gaql):
        self.queries.append(gaql)
        if gaql == experiments.CAMPAIGNS_GAQL:
            if self.fail:
                raise RuntimeError("Google Ads API: offline")
            return CAMP_ROWS
        if gaql == experiments.ARMS_GAQL:
            return ARM_ROWS
        if gaql.startswith("SELECT campaign.id, campaign.name FROM campaign"):
            return [{"campaign": {"id": r["campaign"]["id"], "name": r["campaign"]["name"]}} for r in CAMP_ROWS]
        if "FROM campaign WHERE campaign.id IN" in gaql:
            want = set(re.findall(r"\d+", gaql.split(" IN ", 1)[1]))
            return [r for r in CAMP_ROWS if r["campaign"]["id"] in want]
        if "FROM ad_group WHERE ad_group.id IN" in gaql:
            want = set(re.findall(r"\d+", gaql.split(" IN ", 1)[1]))
            return [{"adGroup": {"id": str(g)}, "campaign": next(r["campaign"] for r in CAMP_ROWS if r["campaign"]["id"] == str(c))}
                    for g, c in ((201, 101), (202, 102), (203, 103)) if str(g) in want]
        if "FROM campaign WHERE campaign.resource_name" in gaql:
            return [{"campaign": {"status": "ENABLED", "campaignBudget": "customers/2/campaignBudgets/5", "biddingStrategyType": "MANUAL_CPC"},
                     "campaignBudget": {"amountMicros": "1000000", "explicitlyShared": False}}]
        raise AssertionError("unexpected query: " + gaql)

    def mutate(self, acct, ops, apply):
        self.mutates.append((ops, apply))
        return True, {}


class Rules(unittest.TestCase):
    def test_from_editor_reads_trial_parent(self):
        db = core._db(ED_ACCT)
        info = experiments.from_editor(db)
        self.assertEqual(info["trials"], {TRIAL.lower(): BASE})
        self.assertEqual(info["bases"], {BASE.lower(): [TRIAL]})

    def test_old_editor_without_trial_column(self):
        p = Path(_tf.mkdtemp()) / "old.db"
        _editor_db(p, trial_column=False)
        self.assertEqual(experiments.from_editor(sqlite3.connect(str(p))), experiments.empty())

    def test_from_api(self):
        info = experiments.from_api(CAMP_ROWS, ARM_ROWS)
        self.assertEqual(info["trials"], {TRIAL.lower(): BASE})
        self.assertEqual(info["bases"], {BASE.lower(): [TRIAL]})

    def test_from_api_halted_experiment_is_no_base_but_trial_stays(self):
        halted = [{**r, "experiment": {"status": "HALTED"}} for r in ARM_ROWS]
        info = experiments.from_api(CAMP_ROWS, halted)
        self.assertIn(TRIAL.lower(), info["trials"])
        self.assertEqual(info["bases"], {})

    def test_check(self):
        info = experiments.from_api(CAMP_ROWS, ARM_ROWS)
        for change in ({"type": "campaign", "campaign": TRIAL, "status": "paused"},
                       {"type": "keyword", "campaign": TRIAL, "ad_group": "Core", "keyword": "x", "match": "exact"},
                       {"type": "rsa", "edit": True, "campaign": " brand TRIAL ", "ad_group": "Core", "status": "paused"}):
            p, w = experiments.check(info, change)
            self.assertEqual(len(p), 1)
            self.assertIn(experiments.MSG, p[0])
            self.assertEqual(w, [])
        p, w = experiments.check(info, {"type": "campaign", "campaign": BASE, "budget": 5})
        self.assertEqual(p, [])
        self.assertIn("running experiment", w[0])
        self.assertEqual(experiments.check(info, {"type": "campaign", "campaign": PLAIN, "budget": 5}), ([], []))
        self.assertEqual(experiments.check(info, {"type": "campaign", "new": True, "campaign": TRIAL, "budget": 5}), ([], []))
        self.assertEqual(experiments.check(info, {"type": "sitelink", "level": "account", "campaign": TRIAL, "text": "a"}), ([], []))

    def test_message_is_exact(self):
        self.assertEqual(experiments.MSG, "This is an experiment's trial campaign: change it from the experiment")


class Draft(unittest.TestCase):
    def setUp(self):
        core.clear(ED_ACCT)

    def test_add_refuses_trial_and_its_children(self):
        for change in ({"type": "campaign", "campaign": TRIAL, "status": "paused"},
                       {"type": "ad_group", "campaign": TRIAL, "ad_group": "Core", "status": "paused"},
                       {"type": "negative", "campaign": TRIAL, "keywords": ["free"], "match": "phrase"}):
            with self.assertRaises(ValueError) as cm:
                core.add(ED_ACCT, [{"type": "campaign", "campaign": PLAIN, "status": "paused"}, change])
            self.assertIn(experiments.MSG, str(cm.exception))
        self.assertEqual(core.draft(ED_ACCT)["items"], [])  # nothing added, not even the good change

    def test_add_warns_for_base(self):
        items = core.add(ED_ACCT, {"type": "campaign", "campaign": BASE, "status": "paused"})
        self.assertEqual(items[0]["problems"], [])
        self.assertTrue(any("running experiment" in w for w in items[0]["warnings"]))
        plain = core.add(ED_ACCT, {"type": "campaign", "campaign": PLAIN, "status": "paused"})
        self.assertFalse(any("experiment" in w for w in plain[0]["warnings"]))

    def test_send_to_editor_refuses_trial_that_got_in(self):
        # a change can reach the draft without core.add (a teammate's item, an older Studio): Send to Editor refuses it
        with draftstore.tx(core._coll(ED_ACCT), core.STUDIO_DIR, by="you") as t:
            t.put("t1", _item({"type": "campaign", "campaign": TRIAL, "status": "paused"}, "t1"), "you")
        res = core.validate(ED_ACCT)
        self.assertFalse(res[0]["ok"])
        self.assertIn(experiments.MSG, res[0]["problems"][0])
        out = core.push(ED_ACCT)
        self.assertFalse(out["ok"])
        self.assertIn(experiments.MSG, out["output"])
        self.assertEqual(len(core.draft(ED_ACCT)["items"]), 1)  # still there, nothing sent

    def test_gae_apply_refuses_trial(self):
        p = Path(_tf.mkdtemp()) / "a.db"
        _editor_db(p)
        db = sqlite3.connect(str(p))
        db.row_factory = sqlite3.Row
        with self.assertRaises(ValueError) as cm:
            gae.apply_changes(db, [{"type": "campaign", "campaign": TRIAL, "status": "paused"}], "t", [])
        self.assertIn(experiments.MSG, str(cm.exception))


class Grids(unittest.TestCase):
    def test_editor_tree_badges(self):
        tree = {c["name"]: c for c in core.tree(ED_ACCT)}
        self.assertEqual(tree[TRIAL]["experiment"], "trial")
        self.assertEqual(tree[TRIAL]["experiment_base"], BASE)
        self.assertEqual(tree[BASE]["experiment"], "base")
        self.assertNotIn("experiment", tree[PLAIN])

    def test_api_base_cache_carries_experiments(self):
        def query(acct, gaql):
            if gaql.endswith("FROM campaign") and "campaign.experiment_type" in gaql:
                return [{"campaign": {**r["campaign"], "status": "ENABLED", "advertisingChannelType": "SEARCH"}} for r in CAMP_ROWS]
            if gaql == experiments.ARMS_GAQL:
                return ARM_ROWS
            return []
        old_q, old_h = live.query, live.gads.headers
        live.query, live.gads.headers = query, lambda: {}
        try:
            d = gapi._build_base(API_ACCT)
        finally:
            live.query, live.gads.headers = old_q, old_h
        self.assertEqual(d["experiments"], experiments.from_api(CAMP_ROWS, ARM_ROWS))
        d["fetched_at"], d["_at"] = "2099-01-01T00:00:00", 4e9
        gapi._write(gapi._file(API_ACCT, "base"), json.loads(json.dumps(d)))
        rows = {r["campaign"]: r for r in gapi.grid(API_ACCT, "campaigns", with_stats=False)}
        self.assertEqual(rows[TRIAL]["experiment"], "trial")
        self.assertEqual(rows[BASE]["experiment"], "base")
        self.assertNotIn("experiment", rows[PLAIN])
        tree = {c["name"]: c for c in gapi.tree(API_ACCT)}
        self.assertEqual(tree[TRIAL]["experiment"], "trial")
        # what this Mac knows without asking Google: the cache
        self.assertIn(TRIAL.lower(), experiments.known(API_ACCT)["trials"])


class PushLive(unittest.TestCase):
    def run_plan(self, items, fake):
        old = live.query, live.mutate
        live.query, live.mutate = fake.query, fake.mutate
        try:
            return live.plan("3333333333", items=items, source="api")
        finally:
            live.query, live.mutate = old

    def test_trial_refused_before_google_is_asked(self):
        fake = FakeApi()
        p = self.run_plan([_item({"type": "campaign", "campaign": TRIAL, "status": "paused"})], fake)
        self.assertFalse(p["ok"])
        self.assertIn(experiments.MSG, p["items"][0]["problems"][0])
        self.assertEqual(p["items"][0]["lines"], [])
        self.assertEqual(fake.mutates, [])

    def test_mixed_push_is_blocked_whole(self):
        fake = FakeApi()
        p = self.run_plan([_item({"type": "campaign", "campaign": PLAIN, "status": "paused"}, "a"),
                           _item({"type": "keyword", "campaign": TRIAL, "ad_group": "Core", "keyword": "x", "match": "exact"}, "b")], fake)
        self.assertFalse(p["ok"])
        self.assertEqual(fake.mutates, [])

    def test_base_warns_and_validates(self):
        fake = FakeApi()
        p = self.run_plan([_item({"type": "campaign", "campaign": BASE, "status": "paused"})], fake)
        self.assertTrue(p["ok"], p)
        self.assertTrue(any("running experiment" in w for w in p["items"][0]["warnings"]))
        self.assertEqual(len(fake.mutates), 1)
        self.assertFalse(fake.mutates[0][1])  # validateOnly

    def test_plain_has_no_warning(self):
        p = self.run_plan([_item({"type": "campaign", "campaign": PLAIN, "status": "paused"})], FakeApi())
        self.assertTrue(p["ok"], p)
        self.assertEqual(p["items"][0]["warnings"], [])

    def test_api_down_still_uses_cached_facts(self):
        # the experiments query fails: what Editor / the cache know still blocks (ED_ACCT's Editor knows the trial)
        fake = FakeApi(fail_experiments=True)
        old = live.query, live.mutate
        live.query, live.mutate = fake.query, fake.mutate
        try:
            p = live.plan(ED_ACCT, items=[_item({"type": "campaign", "campaign": TRIAL, "status": "paused"})], source="api")
        finally:
            live.query, live.mutate = old
        self.assertFalse(p["ok"])
        self.assertIn(experiments.MSG, p["items"][0]["problems"][0])
        self.assertEqual(fake.mutates, [])



def _op(res, kind="campaignOperation"):
    return {kind: {"update": {"resourceName": res, "status": "PAUSED"}, "updateMask": "status"}}


class Undo(unittest.TestCase):
    """An undo is a push too: refused before anything is sent when it touches a trial campaign."""
    ACCT = "4444444444"

    def undo(self, rec, fake):
        live._log(self.ACCT).parent.mkdir(parents=True, exist_ok=True)
        live._log(self.ACCT).write_text(json.dumps({"id": "p1", "ts": "2026-11-01T10:00:00", "ok": True, **rec}) + "\n")
        old = live.query, live.mutate
        live.query, live.mutate = fake.query, fake.mutate
        try:
            return live.undo(self.ACCT, "p1")
        finally:
            live.query, live.mutate = old

    def assert_refused(self, rec):
        fake = FakeApi()
        with self.assertRaises(ValueError) as cm:
            self.undo(rec, fake)
        self.assertIn(experiments.MSG, str(cm.exception))
        self.assertEqual(fake.mutates, [])

    def test_logged_change_on_trial(self):
        self.assert_refused({"items": [_item({"type": "campaign", "campaign": TRIAL, "status": "paused"})],
                             "ops": [{"line": "l", "op": _op("customers/4/campaigns/103"), "undo": _op("customers/4/campaigns/103")}]})

    def test_remove_created_in_a_campaign_that_is_now_a_trial(self):
        # the item names a plain campaign, but the created resource sits in campaign 103, a trial today
        self.assert_refused({"items": [_item({"type": "negative", "campaign": PLAIN, "keywords": ["x"], "match": "phrase"})],
                             "ops": [{"line": "l", "op": {"campaignCriterionOperation": {"create": {"campaign": "customers/4/campaigns/103"}}},
                                      "undo": "remove-created", "resource": "customers/4/campaignCriteria/103~55"}]})

    def test_ad_group_level_resource_in_a_trial(self):
        self.assert_refused({"items": [_item({"type": "keyword", "campaign": PLAIN, "ad_group": "Wide", "keywords": ["x"], "match": "exact"})],
                             "ops": [{"line": "l", "op": {"adGroupCriterionOperation": {"create": {"adGroup": "customers/4/adGroups/203"}}},
                                      "undo": "remove-created", "resource": "customers/4/adGroupCriteria/203~7"}]})

    def test_plain_undo_goes_ahead(self):
        fake = FakeApi()
        r = self.undo({"items": [_item({"type": "campaign", "campaign": PLAIN, "status": "paused"})],
                       "ops": [{"line": "l", "op": _op("customers/4/campaigns/102"), "undo": _op("customers/4/campaigns/102")}]}, fake)
        self.assertTrue(r["ok"], r)
        self.assertEqual([a for _, a in fake.mutates], [False, True])  # validate first, then apply (fake)


class KnownCache(unittest.TestCase):
    def test_cached_until_a_file_changes(self):
        experiments._KNOWN.clear()
        a = experiments.known(ED_ACCT)
        self.assertIs(experiments.known(ED_ACCT), a)
        f = gae.editor_version_dir() / f"ape_{ED_ACCT}.db"
        st = f.stat()
        _os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 10 ** 9))
        b = experiments.known(ED_ACCT)
        self.assertIsNot(b, a)
        self.assertEqual(b, a)


if __name__ == "__main__":
    unittest.main()
