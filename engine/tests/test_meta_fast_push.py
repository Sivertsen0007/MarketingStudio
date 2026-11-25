"""Fast Meta preview / push: batched live reads, parallel validate_only checks, parallel writes inside a phase,
per-account pacing. Run from the engine folder:
    python3 tests/test_meta_fast_push.py

Offline: a fake Graph API replaces meta_api._once (the one place a request leaves the machine), so the real
meta_ads.get/post/get_all and meta_api.call run and every HTTP call is counted. Nothing is sent anywhere.
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("MSTUDIO_HOME", _tf.mkdtemp(prefix="mstudio-test-"))
_os.environ["MSTUDIO_KEYCHAIN"] = "0"

import collections
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
TMP = Path(tempfile.mkdtemp(prefix="meta-fast-push-test-"))
os.environ["STUDIO_DATA_DIR"] = str(TMP)

import core  # noqa: E402

core.STUDIO_DIR = TMP
import meta_ads as meta  # noqa: E402
import meta_api  # noqa: E402

ACT = "act_1"


def creative(n):
    return {"id": f"cr{n}", "object_story_spec": {"page_id": "p1", "link_data": {
        "link": "https://example.com/", "message": f"Text {n}", "name": f"Head {n}",
        "call_to_action": {"type": "LEARN_MORE", "value": {"link": "https://example.com/"}}}}}


def world():
    objs = {}
    for n in range(1, 4):
        objs[f"c{n}"] = {"id": f"c{n}", "name": f"Camp {n}", "status": "ACTIVE", "objective": "OUTCOME_TRAFFIC"}
    for n in range(1, 5):
        objs[f"s{n}"] = {"id": f"s{n}", "name": f"Set {n}", "status": "ACTIVE", "campaign_id": "c1", "daily_budget": "1000",
                         "targeting": {"geo_locations": {"countries": ["GB"]}}}
    for n in range(1, 9):
        objs[f"a{n}"] = {"id": f"a{n}", "name": f"Ad {n}", "status": "ACTIVE", "adset_id": "s1", "creative": creative(n)}
    objs["c9"] = {"id": "c9", "name": "Twin", "status": "PAUSED", "objective": "OUTCOME_TRAFFIC", "special_ad_categories": []}
    return objs


class FakeGraph:
    """Enough of the Graph API for plan / push. Thread-safe. refuse: {(path, frozenset(param keys)) or path} that a
    real (non validate_only) POST refuses."""

    def __init__(self, refuse=(), refuse_check=(), delay=0.0):
        self.objs = world()
        self.start = json.loads(json.dumps(self.objs))
        self.calls = []
        self.refuse, self.refuse_check, self.delay = set(refuse), set(refuse_check), delay
        self.lock = threading.Lock()
        self.n = 0
        self.inflight = self.peak = 0

    def count(self, kind):
        return sum(1 for c in self.calls if c[0] == kind)

    def __call__(self, method, path, params=None, validate=False, token=None):
        params = dict(params or {})
        with self.lock:
            self.inflight += 1
            self.peak = max(self.peak, self.inflight)
        try:
            if self.delay:
                time.sleep(self.delay)
            with self.lock:
                return self._handle(method, path, params, validate)
        finally:
            with self.lock:
                self.inflight -= 1

    def _handle(self, method, path, params, validate):
        if method == "GET":
            self.calls.append(("GET", path, params.get("ids")))
            if path == "":
                ids = params["ids"].split(",")
                if any(i not in self.objs for i in ids):
                    raise meta_api.MetaError({"message": "Some of the aliases you requested do not exist", "code": 100}, 400)
                return {i: json.loads(json.dumps(self.objs[i])) for i in ids}
            if path.endswith("/campaigns"):
                return {"data": [o for o in self.objs.values() if o["id"].startswith("c") and o.get("status") in ("ACTIVE", "PAUSED")]}
            if path not in self.objs:
                raise meta_api.MetaError({"message": f"Object {path} does not exist", "code": 100}, 400)
            return json.loads(json.dumps(self.objs[path]))
        self.calls.append(("VALIDATE" if validate else "POST", path, None))
        if validate:
            if path in self.refuse_check:
                raise meta_api.MetaError({"message": f"refused {path}", "code": 100}, 400)
            return {"success": True}
        if path in self.refuse:
            raise meta_api.MetaError({"message": f"refused {path}", "code": 100}, 400)
        if path.startswith(ACT + "/"):
            edge = path.split("/", 1)[1]
            self.n += 1
            new = f"new-{edge}-{self.n}"
            self.objs[new] = {"id": new, "edge": edge, **{k: v for k, v in params.items()}}
            return {"id": new}
        self.objs[path].update(params)
        return {"success": True}

    def existing_state(self):
        """Fields push touches on objects that existed before, compared before / after."""
        def proj(o):
            cr = o.get("creative") or {}
            crid = str(cr.get("id") or cr.get("creative_id"))
            return (o.get("name"), o.get("status"), str(o.get("daily_budget")), "new" if crid.startswith("new-") else crid)
        return {k: proj(self.objs[k]) for k in self.start}

    def made(self):
        """Campaigns, ad sets and ads a push created, without the generated ids: [(edge, name, status)] sorted.
        (Creatives have no status: a rollback leaves them unused in the library, as before.)"""
        return sorted((o["edge"], o.get("name"), o.get("status")) for o in self.objs.values()
                      if o.get("edge") and o["edge"] != "adcreatives")


def items():
    ch = []
    ch += [{"type": "meta_status", "level": "ad", "id": f"a{n}", "status": "paused"} for n in range(1, 6)]
    ch.append({"type": "meta_status", "level": "adset", "id": "s1", "status": "paused"})
    ch += [{"type": "meta_budget", "level": "adset", "id": f"s{n}", "budget": 5 + n} for n in range(1, 5)]
    ch += [{"type": "meta_rename", "level": "campaign", "id": f"c{n}", "new_name": f"Renamed {n}"} for n in range(1, 4)]
    ch += [{"type": "meta_copy", "level": "ad", "id": f"a{n}", "primary_text": f"New text {n}"} for n in (7, 8)]
    ch.append({"type": "meta_new_campaign", "name": "New A", "objective": "traffic"})
    ch.append({"type": "meta_new_campaign", "name": "New B", "objective": "traffic", "daily_budget": 20})
    ch.append({"type": "meta_new_adset", "name": "Set in old", "campaign_id": "c1", "countries": ["gb"], "daily_budget": 10})
    ch.append({"type": "meta_new_adset", "name": "Set in new", "campaign_draft": "i15", "countries": ["gb"], "daily_budget": 10})
    ch.append({"type": "meta_new_ad", "name": "Copy ad", "adset_id": "s1", "from_ad": "a1"})
    assert len(ch) == 20
    return [{"id": f"i{n}", "change": meta._normalise(c), "by": "claude", "summary": f"change {n}", "problems": []}
            for n, c in enumerate(ch)]


DATA = {"campaigns": [], "adsets": [], "ads": [], "audiences": []}


class Base(unittest.TestCase):
    def setUp(self):
        self.items = items()
        meta_api._limiters.clear()
        self.p = [mock.patch.object(meta, "load", lambda *a, **k: DATA), mock.patch.object(meta, "act", lambda: ACT),
                  mock.patch.object(meta, "_only", lambda only: self.items),
                  mock.patch.object(core, "draft", lambda a: {"items": self.items}),
                  mock.patch.object(core, "remove", lambda a, ids: None),
                  mock.patch.object(meta_api, "PACE_RATE", 10000.0), mock.patch.object(meta_api, "PACE_BURST", 10000)]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()
        meta_api._limiters.clear()

    def graph(self, **kw):
        g = FakeGraph(**kw)
        p = mock.patch.object(meta_api, "_once", g)
        p.start()
        self.addCleanup(p.stop)
        return g


def strip(p):
    return json.loads(json.dumps({k: v for k, v in p.items()}, sort_keys=True, default=str))


class Preview(Base):
    def test_twenty_items_few_reads(self):
        g = self.graph()
        p = meta.plan(workers=5)
        self.assertTrue(p["ok"], [e["problems"] for e in p["items"] if e["problems"]])
        # one batched read per level (campaign, ad set, ad: the creative comes inline with the ad) + the stand-in campaigns
        self.assertEqual(g.count("GET"), 4, g.calls)
        self.assertEqual(sorted(c[1] for c in g.calls if c[0] == "GET"), ["", "", "", ACT + "/campaigns"])
        self.assertEqual(g.count("VALIDATE"), 21)
        self.assertEqual(g.count("POST"), 0)  # a preview never writes

    def test_parallel_plan_matches_sequential(self):
        self.graph(delay=0.002)
        par = meta.plan(workers=5)
        self.graph()
        seq = meta.plan(workers=1)
        self.assertEqual(strip(par), strip(seq))

    def test_refused_check_lands_on_its_item_in_order(self):
        self.graph(refuse_check={"a3", ACT + "/adcreatives"}, delay=0.001)
        par = meta.plan(workers=6)
        self.graph(refuse_check={"a3", ACT + "/adcreatives"})
        seq = meta.plan(workers=1)
        self.assertEqual(strip(par), strip(seq))
        bad = {e["id"]: e["problems"] for e in par["items"] if e["problems"]}
        self.assertEqual(set(bad), {"i2", "i13", "i14", "i19"})
        self.assertEqual(bad["i2"], ["Meta: refused a3"])
        self.assertFalse(par["ok"])

    def test_missing_object_falls_back_to_its_own_read(self):
        self.items.append({"id": "i20", "change": meta._normalise({"type": "meta_status", "level": "ad", "id": "a404", "status": "paused"}),
                           "by": "you", "summary": "gone", "problems": []})
        g = self.graph()
        p = meta.plan()
        bad = {e["id"]: e["problems"] for e in p["items"] if e["problems"]}
        self.assertEqual(list(bad), ["i20"])
        self.assertIn("a404", bad["i20"][0])
        # the batched ad read was refused (one id gone), so each ad was read on its own; the rest still batched
        self.assertEqual(len([c for c in g.calls if c[0] == "GET" and c[1] == ""]), 3)
        self.assertEqual(len([c for c in g.calls if c[0] == "GET" and c[1].startswith("a") and c[1] != ACT + "/campaigns"]), 8)

    def test_cache_is_per_plan(self):
        g = self.graph()
        meta.plan()
        meta.plan()
        self.assertEqual(g.count("GET"), 8)  # the second plan reads live again
        self.assertIsNone(getattr(meta._plan_tls, "live", None))
        meta._live("ad", "a1")
        self.assertEqual(g.count("GET"), 9)  # outside a plan: no cache

    def test_worker_threads_use_the_callers_account(self):
        seen = set()
        lock = threading.Lock()

        def check(s):
            with lock:
                seen.add(getattr(meta._cur, "act", None))
            return ""
        self.graph()
        meta._cur.act = "act_77"
        try:
            with mock.patch.object(meta, "_check_step", check):
                meta.plan(workers=5)
        finally:
            meta._cur.act = None
        self.assertEqual(seen, {"act_77"})


class Push(Base):
    def push(self, g, workers, **kw):
        cb = []
        with mock.patch.object(meta, "refresh_account", lambda: None):
            res = meta.push(accept_spend=True, workers=workers, progress=lambda *a: cb.append(a), **kw)
        return res, cb

    def test_parallel_push_same_result_as_sequential(self):
        g1 = self.graph()
        r1, _ = self.push(g1, 1)
        seq_state, seq_made = g1.existing_state(), g1.made()
        g2 = self.graph(delay=0.002)
        r2, cb = self.push(g2, 5)
        self.assertTrue(r1["ok"] and r2["ok"], (r1["message"], r2["message"]))
        self.assertEqual(r1["applied"], r2["applied"])
        self.assertEqual(g2.existing_state(), seq_state)
        self.assertEqual(g2.made(), seq_made)
        self.assertGreater(g2.peak, 1)  # really ran side by side
        self.assertEqual([x["status"] for x in r2["results"]], ["ok"] * 20)
        self.assertEqual([x["key"] for x in r2["results"]], [f"i{n}" for n in range(20)])
        res = {x["key"]: x for x in r2["results"]}
        self.assertEqual(res["i0"]["ids"], {"object_id": "a1"})
        self.assertEqual(set(res["i19"]["ids"]), {"ad_id", "creative_id"})
        self.assertEqual(set(res["i15"]["ids"]), {"campaign_id"})
        self.assertEqual(set(res["i13"]["ids"]), {"object_id", "creative_id"})
        sends = [a for a in cb if a[4] in ("ok", "failed")]
        self.assertEqual([a[0] for a in sends], list(range(1, 21)))
        self.assertTrue(all(a[1] == 20 for a in sends))
        self.assertEqual(len([a for a in cb if a[4] == "checked"]), 20)  # one per step (a new ad's step makes 2 checks)
        # phases stay in order: every new campaign before any new ad set, ad sets before the new ad, then edits
        posts = [c[1] for c in g2.calls if c[0] == "POST"]
        camp = [i for i, p in enumerate(posts) if p == ACT + "/campaigns"]
        sets = [i for i, p in enumerate(posts) if p == ACT + "/adsets"]
        ads = [i for i, p in enumerate(posts) if p == ACT + "/ads"]
        edits = [i for i, p in enumerate(posts) if not p.startswith(ACT)]
        self.assertLess(max(camp), min(sets))
        self.assertLess(max(sets), min(ads))
        self.assertLess(max(ads), min(edits))

    def test_same_ad_set_edits_stay_in_order(self):
        steps = [{"kind": "update", "id": "s1", "params": {"status": "PAUSED"}}, {"kind": "update", "id": "s2", "params": {}},
                 {"kind": "update", "id": "s1", "params": {"daily_budget": 1}}, {"kind": "create", "edge": "campaigns"},
                 {"kind": "new_ad", "media_id": "m1"}, {"kind": "new_ad", "card_media": ["m2"]}, {"kind": "study"}]
        lanes = meta._lanes(list(enumerate(steps)))
        self.assertEqual([[i for i, _s in lane] for lane in lanes], [[0, 2], [1], [3], [4, 5], [6]])

    def failing(self, workers, refuse):
        g = self.graph(refuse=refuse, delay=0.002 if workers > 1 else 0)
        r, cb = self.push(g, workers)
        return g, r, cb

    def test_mid_phase_failure_rolls_back_everything(self):
        for workers in (1, 5):
            with self.subTest(workers=workers):
                g, r, cb = self.failing(workers, {"a3"})
                self.assertFalse(r["ok"])
                self.assertIn("refused a3", r["message"])
                self.assertEqual(g.existing_state(), {k: v for k, v in FakeGraph().existing_state().items()})
                self.assertTrue(g.made())
                self.assertEqual({s for _e, _n, s in g.made()}, {"DELETED"})  # everything made in the push is deleted again
                res = {x["key"]: x for x in r["results"]}
                self.assertEqual(res["i2"]["status"], "failed")
                self.assertIn("refused a3", res["i2"]["error"])
                self.assertEqual({x["status"] for k, x in res.items() if k != "i2"}, {"skipped"})
                self.assertTrue(all(x["ids"] == {} for x in r["results"]))
                self.assertTrue(res["i15"]["rolled_back"])
                self.assertIn("failed", [a[4] for a in cb])
                self.assertIn("reversed", [a[4] for a in cb])
                log = [json.loads(x) for x in meta._log().read_text().splitlines()][-1]
                self.assertFalse(log["ok"])
                self.assertEqual(log["ops"], [])
                self.assertEqual(log["rollback_errors"], [])

    def test_failure_in_create_phase_stops_later_phases(self):
        for workers in (1, 5):
            with self.subTest(workers=workers):
                g, r, _ = self.failing(workers, {ACT + "/adsets"})
                self.assertFalse(r["ok"])
                posts = [c[1] for c in g.calls if c[0] == "POST"]
                self.assertNotIn(ACT + "/ads", posts)  # the ad phase never started
                self.assertFalse([p for p in posts if p in g.start and g.start[p]["id"].startswith("a")])
                self.assertEqual({s for _e, _n, s in g.made()}, {"DELETED"})
                self.assertEqual(g.existing_state(), FakeGraph().existing_state())

    def test_refused_push_reports_items(self):
        g = self.graph(refuse_check={"a3"})
        r, _ = self.push(g, 5)
        self.assertFalse(r["ok"])
        self.assertIn("preview", r)
        res = {x["key"]: x for x in r["results"]}
        self.assertEqual(res["i2"]["status"], "failed")
        self.assertEqual(res["i0"]["status"], "skipped")
        self.assertEqual(g.count("POST"), 0)

    def test_spend_tick_still_required(self):
        g = self.graph()
        with mock.patch.object(meta, "refresh_account", lambda: None):
            r = meta.push(accept_spend=False)
        self.assertFalse(r["ok"])
        self.assertIn("raise spend", r["message"])
        self.assertEqual(g.count("POST"), 0)

    def test_refresh_is_optional(self):
        calls = []
        self.graph()
        with mock.patch.object(meta, "refresh_account", lambda: calls.append(1)):
            r = meta.push(accept_spend=True, refresh=False)
        self.assertTrue(r["ok"])
        self.assertEqual((calls, r["refreshed"]), ([], False))
        self.items[:] = items()
        self.graph()
        with mock.patch.object(meta, "refresh_account", lambda: calls.append(1)):
            r = meta.push(accept_spend=True)
        self.assertEqual((calls, r["refreshed"]), ([1], True))

    def test_broken_progress_callback_never_stops_a_push(self):
        self.graph()

        def boom(*a):
            raise RuntimeError("ui gone")
        with mock.patch.object(meta, "refresh_account", lambda: None):
            r = meta.push(accept_spend=True, progress=boom)
        self.assertTrue(r["ok"])


class JobLayer(Base):
    """engine/jobs.py passes progress= (found by parameter name) and reads result["items"]."""

    def test_push_takes_progress_by_name_and_old_alias(self):
        import inspect
        self.assertIn("progress", inspect.signature(meta.push).parameters)
        self.assertIn("progress", inspect.signature(meta.preview).parameters)
        for kw in ("progress", "progress_cb"):
            with self.subTest(kw=kw):
                self.items[:] = items()
                self.graph()
                cb = []
                with mock.patch.object(meta, "refresh_account", lambda: None):
                    r = meta.push(accept_spend=True, **{kw: lambda *a: cb.append(a)})
                self.assertTrue(r["ok"])
                self.assertEqual(len([a for a in cb if a[4] == "ok"]), 20)

    def test_items_mirror_results_with_job_statuses(self):
        self.graph()
        with mock.patch.object(meta, "refresh_account", lambda: None):
            r = meta.push(accept_spend=True)
        self.assertEqual([x["status"] for x in r["items"]], ["done"] * 20)
        self.assertEqual([x["key"] for x in r["items"]], [x["key"] for x in r["results"]])
        self.assertEqual(r["items"][0], {"key": "i0", "label": "change 0", "status": "done", "ids": {"object_id": "a1"}, "error": None})
        self.items[:] = items()
        self.graph(refuse={"a3"})
        with mock.patch.object(meta, "refresh_account", lambda: None):
            r = meta.push(accept_spend=True, workers=1)
        st = {x["key"]: x["status"] for x in r["items"]}
        self.assertEqual(st["i2"], "error")
        self.assertEqual(st["i0"], "reversed")  # applied, then rolled back
        self.assertEqual(st["i3"], "skipped")   # never sent (after the refusal, one at a time)
        self.assertEqual(set(st.values()), {"error", "reversed", "skipped"})

    def test_refused_up_front_has_items(self):
        self.graph()
        with mock.patch.object(meta, "refresh_account", lambda: None):
            r = meta.push(accept_spend=False)
        self.assertFalse(r["ok"])
        self.assertEqual({x["status"] for x in r["items"]}, {"skipped"})

    def test_preview_reports_checks(self):
        self.graph()
        cb = []
        p = meta.preview(progress=lambda *a: cb.append(a))
        self.assertTrue(p["ok"])
        self.assertEqual([a[0] for a in cb], list(range(1, 21)))
        self.assertTrue(all(a[1] == 20 and a[4] == "checked" for a in cb))
        self.assertEqual({a[3] for a in cb}, {f"i{n}" for n in range(20)})


class Pacing(unittest.TestCase):
    def setUp(self):
        self.saved = (dict(meta_api._usage), meta_api._usage_seq, meta_api._usage_mono)

    def tearDown(self):
        meta_api._usage.clear()
        meta_api._usage.update(self.saved[0])
        meta_api._usage_seq, meta_api._usage_mono = self.saved[1], self.saved[2]

    def usage(self, **headers):
        meta_api._usage.clear()
        meta_api._record_usage({k.replace("_", "-"): json.dumps(v) for k, v in headers.items()})

    def test_bucket_bursts_then_paces(self):
        self.usage()
        meta_api._usage_seq = 0
        slept = []
        lim = meta_api.Limiter(rate=10, burst=3)
        with mock.patch.object(meta_api.time, "sleep", lambda s: slept.append(s)):
            for _ in range(3):
                self.assertEqual(lim.acquire(), 0.0)
            lim.acquire()
        self.assertTrue(slept and 0 < slept[0] <= 0.1001)

    def test_usage_headers_slow_down(self):
        self.usage(x_app_usage={"call_count": 92, "total_cputime": 10, "total_time": 10})
        self.assertEqual(meta_api.pressure()[0], 92)
        slept = []
        lim = meta_api.Limiter(rate=10, burst=5)
        with mock.patch.object(meta_api.time, "sleep", lambda s: slept.append(s)):
            lim.acquire()
            lim.acquire()  # no bursts near the limit, and a tenth of the rate
        self.assertTrue(slept and slept[0] > 0.5, slept)

    def test_business_usage_regain_is_waited_once(self):
        self.usage(x_business_use_case_usage={"123": [{"type": "ADS_MANAGEMENT", "call_count": 40, "total_cputime": 5,
                                                         "total_time": 5, "estimated_time_to_regain_access": 2}]})
        pct, regain, _ = meta_api.pressure()
        self.assertEqual((pct, regain), (40, 120))
        slept = []
        lim = meta_api.Limiter(rate=1000, burst=5)
        with mock.patch.object(meta_api.time, "sleep", lambda s: slept.append(s)):
            lim.acquire()
            lim.acquire()
        self.assertEqual(slept, [meta_api.PACE_HOLD_MAX])

    def test_stale_usage_is_ignored(self):
        self.usage(x_ad_account_usage={"acc_id_util_pct": 99})
        meta_api._usage_mono -= meta_api.PACE_FRESH + 1
        self.assertEqual(meta_api.pressure()[0], 0.0)


if __name__ == "__main__":
    unittest.main()
