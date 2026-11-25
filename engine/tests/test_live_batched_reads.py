"""Push live preview / plan: live "before" values are read in batches (one GAQL query per resource type with an IN
list, chunked) and identical reads happen once per plan. The result must be the same as reading each item on its own.

Run from the engine folder:  python3 -m unittest tests/test_live_batched_reads.py
A temp MSTUDIO_HOME and an empty fake Google Ads Editor folder; the Google Ads API is replaced by a fake that answers
both the per-item reads and the batched ones. Never touches the real data folder, Editor's real database or the Keychain.
"""
import os as _os, tempfile as _tf
_os.environ["MSTUDIO_HOME"] = _tf.mkdtemp(prefix="mstudio-test-")
_os.environ["MSTUDIO_KEYCHAIN"] = "0"
_os.environ["GAE_EDITOR_DIR"] = _tf.mkdtemp(prefix="gae-editor-test-")

import json
import re
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import experiments  # noqa: E402
import live  # noqa: E402

A = "3333333333"
CAMPS = {"101": ("Brand", "ENABLED", "5000000"), "102": ("Generic", "PAUSED", "9000000"), "103": ("Gone live", "PAUSED", "1000000")}
GROUPS = {"201": ("Core", "101", "ENABLED", "1500000"), "202": ("Wide", "101", "PAUSED", "800000"),
          "203": ("Deep", "102", "ENABLED", "2000000"), "204": ("Ghost", "102", "ENABLED", "1")}
LIVE_GONE = {"103", "204"}  # in the name lists, but a read of the resource itself finds nothing (removed meanwhile)
KWS = [("201", "301", "reef dive", "EXACT", "ENABLED", "1000000"), ("201", "302", "reef dive", "PHRASE", "PAUSED", None),
       ("202", "303", "wreck", "EXACT", "ENABLED", "700000"), ("203", "304", "deep dive", "BROAD", "ENABLED", "1200000"),
       ("203", "305", "liveaboard", "EXACT", "PAUSED", "900000")]
ADS = [("201", "401", "ENABLED", ["https://example.org/a"], ["H1", "H2", "H3"], ["D1", "D2"]),
       ("202", "402", "PAUSED", ["https://example.org/b"], ["H4", "H5", "H6"], ["D3", "D4"]),
       ("203", "403", "ENABLED", ["https://example.org/c"], ["H7", "H8", "H9"], ["D5", "D6"]),
       ("203", "404", "PAUSED", ["https://example.org/d"], ["Hx", "Hy", "Hz"], ["Dx", "Dy"])]


def _in(gaql, field):
    """Values of `field = X` or `field IN (X, Y)` in a WHERE clause (quotes stripped), or None."""
    m = re.search(re.escape(field) + r" = '?([^' ]+)'?", gaql)
    if m:
        return {m.group(1)}
    m = re.search(re.escape(field) + r" IN \(([^)]*)\)", gaql)
    if m:
        return {x.strip().strip("'") for x in m.group(1).split(",") if x.strip()}
    return None


class FakeGoogle:
    """Answers per-item and batched GAQL alike; full rows (resourceName included, as the API does)."""

    def __init__(self, refuse_batches=False):
        self.queries, self.mutates, self.refuse = [], [], refuse_batches
        self.lock = threading.Lock()

    def query(self, acct, g):
        with self.lock:
            self.queries.append(g)
        if self.refuse and " IN (" in g:
            raise RuntimeError("Google Ads API: batched read refused")
        if g == experiments.CAMPAIGNS_GAQL:
            return [{"campaign": {"id": i, "name": n, "experimentType": "BASE"}} for i, (n, *_r) in CAMPS.items()]
        if g == experiments.ARMS_GAQL:
            return []
        if g.startswith("SELECT campaign.id, campaign.name FROM campaign"):
            return [{"campaign": {"id": i, "name": n}} for i, (n, *_r) in CAMPS.items()]
        if g.startswith("SELECT ad_group.id, ad_group.name, campaign.id FROM ad_group"):
            return [{"adGroup": {"id": i, "name": n}, "campaign": {"id": c}} for i, (n, c, *_r) in GROUPS.items()]
        frm = re.search(r" FROM (\w+) WHERE", g).group(1)
        res = _in(g, f"{frm}.resource_name")
        if frm == "campaign" and res is not None:
            out = []
            for i, (n, st, amt) in CAMPS.items():
                rn = f"customers/{A}/campaigns/{i}"
                if rn in res and i not in LIVE_GONE:
                    out.append({"campaign": {"resourceName": rn, "status": st, "campaignBudget": f"customers/{A}/campaignBudgets/9{i}",
                                             "biddingStrategyType": "MANUAL_CPC"},
                                "campaignBudget": {"amountMicros": amt, "explicitlyShared": False}})
            return out
        if frm == "ad_group" and res is not None:
            return [{"adGroup": {"resourceName": f"customers/{A}/adGroups/{i}", "status": st, "cpcBidMicros": cpc}}
                    for i, (n, c, st, cpc) in GROUPS.items() if f"customers/{A}/adGroups/{i}" in res and i not in LIVE_GONE]
        if frm == "ad_group_criterion":
            gids = _in(g, "ad_group.id")
            out = []
            for gid, cid, text, match, st, cpc in KWS:
                rn = f"customers/{A}/adGroupCriteria/{gid}~{cid}"
                if (res is not None and rn in res) or (gids is not None and gid in gids and "negative = FALSE" in g):
                    crit = {"resourceName": rn, "criterionId": cid, "keyword": {"text": text, "matchType": match}, "status": st}
                    if cpc:
                        crit["cpcBidMicros"] = cpc
                    out.append({"adGroup": {"id": gid}, "adGroupCriterion": crit})
            return out
        if frm == "ad_group_ad":
            gids = _in(g, "ad_group.id")
            out = []
            for gid, aid, st, urls, hs, ds in ADS:
                rn = f"customers/{A}/adGroupAds/{gid}~{aid}"
                if (res is not None and rn in res) or (gids is not None and gid in gids):
                    out.append({"adGroup": {"id": gid}, "adGroupAd": {"resourceName": rn, "status": st, "ad": {
                        "id": aid, "finalUrls": list(urls), "responsiveSearchAd": {"headlines": [{"text": h} for h in hs],
                                                                                    "descriptions": [{"text": d} for d in ds]}}}})
            return out
        if frm == "campaign_criterion":
            return []
        raise AssertionError("unexpected query: " + g)

    def mutate(self, acct, ops, apply):
        self.mutates.append((ops, apply))
        return True, {}


def _it(n, change):
    return {"id": f"i{n}", "change": change, "by": "you", "summary": f"s{n}", "problems": [], "warnings": [], "ts": "2026-11-25T10:00:00",
            "note": ""}


def mixed():
    """20 mixed draft items: campaign / ad group / keyword / RSA edits (with a few that must fail), plus creates."""
    ch = [
        {"type": "campaign", "campaign": "Brand", "status": "paused"},
        {"type": "campaign", "campaign": "Generic", "budget": 12, "status": "enabled"},
        {"type": "campaign", "campaign": "Brand", "budget": 4},
        {"type": "campaign", "campaign": "Gone live", "status": "enabled"},
        {"type": "ad_group", "campaign": "Brand", "ad_group": "Core", "max_cpc": 2},
        {"type": "ad_group", "campaign": "Brand", "ad_group": "Wide", "status": "enabled"},
        {"type": "ad_group", "campaign": "Generic", "ad_group": "Deep", "status": "paused", "max_cpc": 1},
        {"type": "ad_group", "campaign": "Generic", "ad_group": "Ghost", "status": "paused"},
        {"type": "keyword", "edit": True, "campaign": "Brand", "ad_group": "Core", "keywords": ["reef dive"], "match": "exact", "max_cpc": 1.5},
        {"type": "keyword", "edit": True, "campaign": "Brand", "ad_group": "Core", "keywords": ["reef dive"], "match": "phrase", "status": "enabled"},
        {"type": "keyword", "edit": True, "campaign": "Brand", "ad_group": "Wide", "keywords": ["wreck"], "match": "exact", "status": "removed"},
        {"type": "keyword", "edit": True, "campaign": "Generic", "ad_group": "Deep", "keywords": ["deep dive"], "match": "broad",
         "status": "paused"},
        {"type": "keyword", "edit": True, "campaign": "Generic", "ad_group": "Deep", "keywords": ["liveaboard"], "match": "exact", "max_cpc": 0.5},
        {"type": "keyword", "edit": True, "campaign": "Generic", "ad_group": "Deep", "keywords": ["not there"], "match": "exact", "max_cpc": 3},
        {"type": "rsa", "edit": True, "campaign": "Brand", "ad_group": "Core", "status": "paused"},
        {"type": "rsa", "edit": True, "campaign": "Brand", "ad_group": "Wide", "final_url": "https://example.org/new"},
        {"type": "rsa", "edit": True, "campaign": "Generic", "ad_group": "Deep", "ad_id": "404", "status": "enabled"},
        {"type": "rsa", "edit": True, "campaign": "Generic", "ad_group": "Deep", "local_id": "403",
         "headlines": ["New one", "New two", "New three"], "descriptions": ["Desc one", "Desc two"]},
        {"type": "keyword", "campaign": "Brand", "ad_group": "Wide", "keywords": ["new kw"], "match": "phrase"},
        {"type": "negative", "campaign": "Generic", "keywords": ["free"], "match": "phrase"},
    ]
    assert len(ch) == 20
    return [_it(n, c) for n, c in enumerate(ch)]


def _plan(fake, batch=True, items=None):
    with mock.patch.object(live, "query", fake.query), mock.patch.object(live, "mutate", fake.mutate), \
            mock.patch.object(live, "BATCH_READS", batch):
        p = live.plan(A, items=items if items is not None else mixed(), source="api")
    return json.loads(json.dumps(p))


class BatchedReads(unittest.TestCase):
    def test_same_result_as_per_item_reads_with_far_fewer_queries(self):
        one, many = FakeGoogle(), FakeGoogle()
        batched, per_item = _plan(one), _plan(many, batch=False)
        self.assertEqual(batched, per_item)
        # 7 batched reads; the two things a batch did not find ("Gone live", "Ghost") are read once more on their own
        self.assertLessEqual(len(one.queries), 9, "\n".join(one.queries))
        self.assertGreaterEqual(len(many.queries), 20)
        print(f"\n[live batched reads] 20 mixed items: {len(many.queries)} queries per item -> {len(one.queries)} batched",
              file=sys.stderr)

    def test_expected_outcomes(self):
        p = _plan(FakeGoogle())
        probs = {e["id"]: e["problems"] for e in p["items"]}
        self.assertTrue(any("not found live" in x for x in probs["i3"]), probs["i3"])
        self.assertTrue(any("not found live" in x for x in probs["i7"]), probs["i7"])
        self.assertTrue(any("not found in Google Ads" in x for x in probs["i13"]), probs["i13"])
        clean = [e for e in p["items"] if not e["problems"]]
        self.assertEqual(len(clean), 17)
        lines = {e["id"]: e["lines"] for e in p["items"]}
        self.assertEqual(lines["i1"], ["campaign status PAUSED -> ENABLED", "budget 9.00/day -> 12.00/day"])
        self.assertEqual(lines["i4"], ["max CPC 1.50 -> max CPC 2.00"])
        self.assertEqual(lines["i8"], ['"reef dive" max CPC 1.00 -> max CPC 1.50'])
        self.assertEqual(lines["i11"], ['"deep dive" status ENABLED -> status PAUSED'])
        self.assertEqual(lines["i16"], ["RSA 404 status PAUSED -> ENABLED"])
        self.assertFalse(p["ok"])  # three items have problems, so validateOnly is not even asked
        before = {o["line"]: o["undo"] for o in p["ops"]}
        self.assertIn("campaign status ENABLED -> PAUSED", before)

    def test_all_clean_items_validate_once(self):
        good = [i for i in mixed() if i["id"] not in ("i3", "i7", "i13")]  # 17 items, all found live
        f = FakeGoogle()
        p = _plan(f, items=good)
        self.assertTrue(p["ok"], [e["problems"] for e in p["items"] if e["problems"]])
        self.assertLessEqual(len(f.queries), 7, "\n".join(f.queries))
        self.assertEqual(len(f.mutates), 1)
        self.assertFalse(f.mutates[0][1], "validateOnly")
        self.assertEqual(p, _plan(FakeGoogle(), batch=False, items=good))

    def test_refused_batches_fall_back_to_per_item_reads(self):
        f = FakeGoogle(refuse_batches=True)
        self.assertEqual(_plan(f), _plan(FakeGoogle(), batch=False))
        self.assertTrue(any(" IN (" in q for q in f.queries))

    def test_identical_reads_happen_once(self):
        f = FakeGoogle()
        items = [_it(n, {"type": "campaign", "campaign": "Brand", "status": "paused"}) for n in range(5)]
        with mock.patch.object(live, "query", f.query), mock.patch.object(live, "mutate", f.mutate):
            live.plan(A, items=items, source="api")
        reads = [q for q in f.queries if "FROM campaign WHERE campaign.resource_name" in q]
        self.assertEqual(len(reads), 1, reads)
        self.assertEqual(f.queries.count(experiments.CAMPAIGNS_GAQL), 1)
        self.assertEqual(f.queries.count(experiments.ARMS_GAQL), 1)

    def test_chunks_long_in_lists(self):
        f = FakeGoogle()
        items = [_it(n, {"type": "campaign", "campaign": "Brand", "status": "paused"}) for n in range(3)]
        with mock.patch.object(live, "query", f.query), mock.patch.object(live, "mutate", f.mutate), \
                mock.patch.object(live, "CHUNK", 1):
            ids = live.Ids(A, "api")
            ids.batch_live("ad_group", [f"customers/{A}/adGroups/{g}" for g in ("201", "202", "203")])
        self.assertEqual(sum(1 for q in f.queries if " IN (" in q), 3)
        self.assertEqual(len(items), 3)

    def test_push_reads_live_again(self):
        f = FakeGoogle()
        good = [_it(0, {"type": "campaign", "campaign": "Brand", "status": "paused"})]
        with mock.patch.object(live, "query", f.query), mock.patch.object(live, "mutate", f.mutate):
            live.plan(A, items=good, source="api")
            n = len(f.queries)
            live.plan(A, items=good, source="api")
        self.assertEqual(len(f.queries), 2 * n, "every plan (and so every push) reads live from scratch")


if __name__ == "__main__":
    unittest.main()
