"""Speed benchmark: live preview and push against FAKE Google Ads and Meta APIs with realistic latency.

Run from the engine folder:
    python3 -m tests.bench_push                      # N = 20 and 200, 0.3 s per call, with the sequential Meta baseline
    python3 -m tests.bench_push --n 20 200 --latency 0.3 --no-baseline

Importable: bench_push.run(n, latency, ...) returns the measurements (tests/test_bench_push.py runs a small one).

Nothing leaves the machine. Meta: a fake Graph replaces meta_api._once (the one place a Meta request is sent), so the
real meta_ads reads / checks / writes, meta_api.call and the per-account pacing (meta_api.pace, real PACE_RATE and
PACE_BURST) all run. Google: live.query and live.mutate are replaced (the same seams tests/test_live_batched_reads.py
uses). Every fake call sleeps `latency` seconds (outside any lock, so calls side by side overlap as they would on the
network) and is recorded with its kind and start / end time, so a slow run can be broken down per call type.

Targets (v0.3): 20 changes preview and push under 5 s each per channel; 200 changes under 30 s.
Named bench_*, not test_*, so the full suite never picks it up as a (slow) test.
"""
import os as _os
import tempfile as _tf

_os.environ.setdefault("MSTUDIO_HOME", _tf.mkdtemp(prefix="mstudio-bench-"))
_os.environ["MSTUDIO_KEYCHAIN"] = "0"
_os.environ.setdefault("GAE_EDITOR_DIR", _tf.mkdtemp(prefix="gae-editor-bench-"))

import argparse  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import sys  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from contextlib import ExitStack  # noqa: E402
from pathlib import Path  # noqa: E402
from unittest import mock  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_TMP = Path(_tf.mkdtemp(prefix="bench-push-"))
_os.environ["STUDIO_DATA_DIR"] = str(_TMP)

import core  # noqa: E402

core.STUDIO_DIR = _TMP
import experiments  # noqa: E402
import gapi  # noqa: E402
import live  # noqa: E402
import meta_ads as meta  # noqa: E402
import meta_api  # noqa: E402

ACT = "act_900"
GACCT = "9999999999"
TARGET = {20: 5.0, 200: 30.0}  # seconds, per run (preview, push) and channel


def target_for(n):
    """Seconds allowed for n changes (linear between the two targets, the 200 target above that)."""
    if n <= 20:
        return TARGET[20]
    if n >= 200:
        return TARGET[200]
    return TARGET[20] + (TARGET[200] - TARGET[20]) * (n - 20) / 180.0


# ---------- call recording ----------

class Recorder:
    """Thread-safe log of fake calls: (kind, start, end). Also tracks the peak number of calls in flight."""

    def __init__(self, latency):
        self.latency = float(latency)
        self.lock = threading.Lock()
        self.calls = []
        self.inflight = self.peak = 0

    def timed(self, kind, fn):
        t0 = time.monotonic()
        with self.lock:
            self.inflight += 1
            self.peak = max(self.peak, self.inflight)
        try:
            if self.latency:
                time.sleep(self.latency)
            return fn()
        finally:
            t1 = time.monotonic()
            with self.lock:
                self.inflight -= 1
                self.calls.append((kind, t0, t1))

    def counts(self):
        out = {}
        for k, _a, _b in self.calls:
            out[k] = out.get(k, 0) + 1
        return out

    def spans(self, t_start):
        """kind -> (first start, last end) in seconds from t_start: where each call type sits in the run."""
        out = {}
        for k, a, b in self.calls:
            s, e = out.get(k, (a, b))
            out[k] = (min(s, a), max(e, b))
        return {k: (round(s - t_start, 2), round(e - t_start, 2)) for k, (s, e) in out.items()}


# ---------- fake Meta Graph API ----------

def _meta_world(n):
    """n campaigns, n ad sets (s<k> in c<k>), n ads (a<k> in s<k>, creative inline): one fresh object per draft item."""
    objs = {}
    for k in range(n):
        objs[f"c{k}"] = {"id": f"c{k}", "name": f"Camp {k}", "status": "ACTIVE", "objective": "OUTCOME_TRAFFIC",
                         "special_ad_categories": []}
        objs[f"s{k}"] = {"id": f"s{k}", "name": f"Set {k}", "status": "ACTIVE", "campaign_id": f"c{k}", "daily_budget": "1000",
                         "targeting": {"geo_locations": {"countries": ["GB"]}}}
        objs[f"a{k}"] = {"id": f"a{k}", "name": f"Ad {k}", "status": "ACTIVE", "adset_id": f"s{k}", "creative": {
            "id": f"cr{k}", "object_story_spec": {"page_id": "p1", "link_data": {
                "link": "https://example.com/", "message": f"Text {k}", "name": f"Head {k}",
                "call_to_action": {"type": "LEARN_MORE", "value": {"link": "https://example.com/"}}}}}}
    return objs


LOW_USAGE = {"x-app-usage": json.dumps({"call_count": 3, "total_cputime": 2, "total_time": 2}),
             "x-ad-account-usage": json.dumps({"acc_id_util_pct": 4})}


class FakeGraph:
    """Enough of the Graph API for preview / push: ?ids= batch reads, object reads, the campaigns edge, validate_only
    POSTs, creates on act_/<edge>, updates and deletes. Thread-safe; each call sleeps the latency outside the lock and
    reports low usage headers (so pacing never slows down for pressure, only for the bucket itself)."""

    def __init__(self, n, rec):
        self.objs = _meta_world(n)
        self.rec = rec
        self.lock = threading.Lock()
        self.made = 0

    def __call__(self, method, path, params=None, validate=False, token=None):
        params = dict(params or {})
        if method == "GET":
            kind = ("GET status" if "effective_status" in str(params.get("fields")) else "GET ids") if path == "" \
                else "GET edge" if path.endswith("/campaigns") else "GET one"
        elif method == "DELETE":
            kind = "DELETE"
        elif validate:
            kind = "CHECK"
        else:
            kind = "CREATE" if path.startswith(ACT + "/") else "UPDATE"

        def answer():
            with self.lock:
                out = self._handle(method, path, params, validate)
            meta_api._record_usage(LOW_USAGE)
            return out
        return self.rec.timed(kind, answer)

    def _handle(self, method, path, params, validate):
        if method == "GET":
            if path == "":
                ids = params["ids"].split(",")
                if any(i not in self.objs for i in ids):
                    raise meta_api.MetaError({"message": "Some of the aliases you requested do not exist", "code": 100}, 400)
                return {i: json.loads(json.dumps(self.objs[i])) for i in ids}
            if path.endswith("/campaigns"):
                return {"data": [json.loads(json.dumps(o)) for o in self.objs.values()
                                 if str(o["id"]).startswith("c") and o.get("status") in ("ACTIVE", "PAUSED")]}
            if path not in self.objs:
                raise meta_api.MetaError({"message": f"Object {path} does not exist", "code": 100}, 400)
            return json.loads(json.dumps(self.objs[path]))
        if method == "DELETE":
            self.objs.pop(path, None)
            return {"success": True}
        if validate:
            return {"success": True}
        if path.startswith(ACT + "/"):
            edge = path.split("/", 1)[1]
            self.made += 1
            new = f"new-{edge}-{self.made}"
            self.objs[new] = {"id": new, "edge": edge, **params}
            return {"id": new}
        if path not in self.objs:
            raise meta_api.MetaError({"message": f"Object {path} does not exist", "code": 100}, 400)
        self.objs[path].update(params)
        return {"success": True}


def meta_items(n):
    """n mixed draft items, each on its own objects: status, budget, rename, copy edit, new ad set, new ad, new campaign."""
    out = []
    for k in range(n):
        t = k % 8
        if t == 0:
            c = {"type": "meta_status", "level": "ad", "id": f"a{k}", "status": "paused"}
        elif t == 1:
            c = {"type": "meta_budget", "level": "adset", "id": f"s{k}", "budget": 15}
        elif t == 2:
            c = {"type": "meta_rename", "level": "campaign", "id": f"c{k}", "new_name": f"Renamed {k}"}
        elif t == 3:
            c = {"type": "meta_copy", "level": "ad", "id": f"a{k}", "primary_text": f"New text {k}"}
        elif t == 4:
            c = {"type": "meta_status", "level": "adset", "id": f"s{k}", "status": "paused"}
        elif t == 5:
            c = {"type": "meta_new_adset", "name": f"New set {k}", "campaign_id": f"c{k}", "countries": ["gb"], "daily_budget": 10}
        elif t == 6:
            c = {"type": "meta_new_ad", "name": f"New ad {k}", "adset_id": f"s{k}", "from_ad": f"a{k}"}
        else:
            c = {"type": "meta_new_campaign", "name": f"New camp {k}", "objective": "traffic"}
        out.append({"id": f"i{k}", "change": meta._normalise(c), "by": "bench", "summary": f"change {k}", "problems": []})
    return out


def _reset_meta_pacing():
    meta_api._limiters.clear()
    with meta_api._usage_lock:
        meta_api._usage.clear()
        meta_api._usage_seq = 0
        meta_api._usage_mono = 0.0


def bench_meta(n, latency, workers=None, op="both", reuse=False):
    """Meta preview then push (refresh=False) of n mixed changes. workers=None: the default (meta_ads.WORKERS).
    reuse: the push may reuse the preview's checks (meta_ads.REUSE_CHECKS); False checks every step again."""
    out = {}
    for what in (("preview", "push") if op == "both" else (op,)):
        rec = Recorder(latency)
        g = FakeGraph(n, rec)
        items = meta_items(n)
        waits = []
        wlock = threading.Lock()
        real_pace = meta_api.pace

        def pace(key, _real=real_pace):
            w = _real(key)
            with wlock:
                waits.append(w)
            return w
        _reset_meta_pacing()
        with ExitStack() as st:
            for p in (mock.patch.object(meta_api, "_once", g), mock.patch.object(meta_api, "pace", pace),
                      mock.patch.object(meta, "load", lambda *a, **k: {"campaigns": [], "adsets": [], "ads": [], "audiences": []}),
                      mock.patch.object(meta, "act", lambda: ACT), mock.patch.object(meta, "_only", lambda only: items),
                      mock.patch.object(core, "draft", lambda a: {"items": items}),
                      mock.patch.object(core, "remove", lambda a, ids: None),
                      mock.patch.object(meta, "refresh_account", lambda: None),
                      mock.patch.object(meta, "REUSE_CHECKS", bool(reuse))):
                st.enter_context(p)
            t0 = time.monotonic()
            if what == "preview":
                res = meta.preview(workers=workers)
            else:
                res = meta.push(accept_spend=True, refresh=False, workers=workers)
            wall = time.monotonic() - t0
        bad = [e["problems"] for e in (res.get("items") or res.get("preview", {}).get("items") or []) if e.get("problems")]
        out[what] = {"wall": wall, "ok": bool(res.get("ok")), "problems": bad[:3], "message": res.get("message"),
                     "calls": rec.counts(), "n_calls": len(rec.calls), "peak": rec.peak, "spans": rec.spans(t0),
                     "pace_wait": sum(waits), "pace_waits": sum(1 for w in waits if w > 0)}
    if op == "both":
        meta.forget_checks()
    return out


# ---------- fake Google Ads API ----------

def _in(gaql, field):
    m = re.search(re.escape(field) + r" = '?([^' ]+)'?", gaql)
    if m:
        return {m.group(1)}
    m = re.search(re.escape(field) + r" IN \(([^)]*)\)", gaql)
    if m:
        return {x.strip().strip("'") for x in m.group(1).split(",") if x.strip()}
    return None


class FakeGoogle:
    """n campaigns (Camp k, id 1000+k), each with one ad group (Group k, id 5000+k), two keywords and one RSA.
    Answers per-item and batched GAQL alike; mutate validates or applies with one result per operation."""

    def __init__(self, n, rec):
        self.n, self.rec = n, rec
        self.camps = {str(1000 + k): f"Camp {k}" for k in range(n)}
        self.groups = {str(5000 + k): (f"Group {k}", str(1000 + k)) for k in range(n)}

    def query(self, acct, g):
        kind = "query (batched)" if " IN (" in g else "query"
        return self.rec.timed(kind, lambda: self._query(g))

    def mutate(self, acct, ops, apply):
        def answer():
            if not apply:
                return True, {}
            return True, {"mutateOperationResponses": [{"result": {"resourceName": f"customers/{GACCT}/x/{i}"}}
                                                       for i in range(len(ops))]}
        return self.rec.timed("mutate (apply)" if apply else "mutate (validateOnly)", answer)

    def _query(self, g):
        A = GACCT
        if g == experiments.CAMPAIGNS_GAQL:
            return [{"campaign": {"id": i, "name": nm, "experimentType": "BASE"}} for i, nm in self.camps.items()]
        if g == experiments.ARMS_GAQL:
            return []
        if g.startswith("SELECT campaign.id, campaign.name FROM campaign"):
            return [{"campaign": {"id": i, "name": nm}} for i, nm in self.camps.items()]
        if g.startswith("SELECT ad_group.id, ad_group.name, campaign.id FROM ad_group"):
            return [{"adGroup": {"id": i, "name": nm}, "campaign": {"id": c}} for i, (nm, c) in self.groups.items()]
        frm = re.search(r" FROM (\w+) WHERE", g).group(1)
        res = _in(g, f"{frm}.resource_name")
        if frm == "campaign" and res is not None:
            return [{"campaign": {"resourceName": f"customers/{A}/campaigns/{i}", "status": "ENABLED",
                                  "campaignBudget": f"customers/{A}/campaignBudgets/9{i}", "biddingStrategyType": "MANUAL_CPC",
                                  "targetingSetting": {}},
                     "campaignBudget": {"amountMicros": "5000000", "explicitlyShared": False}}
                    for i in self.camps if f"customers/{A}/campaigns/{i}" in res]
        if frm == "ad_group" and res is not None:
            return [{"adGroup": {"resourceName": f"customers/{A}/adGroups/{i}", "status": "ENABLED", "cpcBidMicros": "1000000"}}
                    for i in self.groups if f"customers/{A}/adGroups/{i}" in res]
        if frm == "ad_group_criterion":
            gids = _in(g, "ad_group.id")
            out = []
            if "negative = TRUE" in g:
                return out
            for gid in self.groups:
                k = int(gid) - 5000
                for cid, text, match in ((f"{7000 + 2 * k}", f"kw {k} a", "EXACT"), (f"{7001 + 2 * k}", f"kw {k} b", "PHRASE")):
                    rn = f"customers/{A}/adGroupCriteria/{gid}~{cid}"
                    if (res is not None and rn in res) or (gids is not None and gid in gids):
                        out.append({"adGroup": {"id": gid}, "adGroupCriterion": {
                            "resourceName": rn, "criterionId": cid, "keyword": {"text": text, "matchType": match},
                            "status": "ENABLED", "cpcBidMicros": "900000"}})
            return out
        if frm == "ad_group_ad":
            gids = _in(g, "ad_group.id")
            out = []
            for gid in self.groups:
                k = int(gid) - 5000
                aid = str(9000 + k)
                rn = f"customers/{A}/adGroupAds/{gid}~{aid}"
                if (res is not None and rn in res) or (gids is not None and gid in gids):
                    out.append({"adGroup": {"id": gid}, "adGroupAd": {"resourceName": rn, "status": "ENABLED", "ad": {
                        "id": aid, "finalUrls": ["https://example.org/"],
                        "responsiveSearchAd": {"headlines": [{"text": f"H{k} {j}"} for j in range(3)],
                                               "descriptions": [{"text": f"D{k} {j}"} for j in range(2)]}}}})
            return out
        if frm == "campaign_criterion":
            return []
        raise AssertionError("unexpected query: " + g)


def google_items(n):
    """n mixed draft items, each on its own campaign / ad group: campaign status and budget, ad group status and max CPC,
    keyword max CPC and status, RSA status and headline edits, a new keyword, a new campaign negative."""
    out = []
    for k in range(n):
        camp, grp = f"Camp {k}", f"Group {k}"
        t = k % 10
        if t == 0:
            c = {"type": "campaign", "campaign": camp, "status": "paused"}
        elif t == 1:
            c = {"type": "campaign", "campaign": camp, "budget": 12}
        elif t == 2:
            c = {"type": "ad_group", "campaign": camp, "ad_group": grp, "status": "paused"}
        elif t == 3:
            c = {"type": "ad_group", "campaign": camp, "ad_group": grp, "max_cpc": 2}
        elif t == 4:
            c = {"type": "keyword", "edit": True, "campaign": camp, "ad_group": grp, "keywords": [f"kw {k} a"], "match": "exact",
                 "max_cpc": 1.5}
        elif t == 5:
            c = {"type": "keyword", "edit": True, "campaign": camp, "ad_group": grp, "keywords": [f"kw {k} b"], "match": "phrase",
                 "status": "paused"}
        elif t == 6:
            c = {"type": "rsa", "edit": True, "campaign": camp, "ad_group": grp, "status": "paused"}
        elif t == 7:
            c = {"type": "rsa", "edit": True, "campaign": camp, "ad_group": grp, "local_id": str(9000 + k),
                 "headlines": ["New one", "New two", "New three"], "descriptions": ["Desc one", "Desc two"]}
        elif t == 8:
            c = {"type": "keyword", "campaign": camp, "ad_group": grp, "keywords": [f"new kw {k}"], "match": "phrase"}
        else:
            c = {"type": "negative", "campaign": camp, "keywords": [f"free {k}"], "match": "phrase"}
        out.append({"id": f"g{k}", "change": c, "by": "bench", "summary": f"change {k}", "problems": [], "warnings": [],
                    "ts": "2026-11-25T10:00:00", "note": ""})
    return out


def bench_google(n, latency, op="both"):
    out = {}
    for what in (("preview", "push") if op == "both" else (op,)):
        rec = Recorder(latency)
        f = FakeGoogle(n, rec)
        items = google_items(n)
        with ExitStack() as st:
            for p in (mock.patch.object(live, "query", f.query), mock.patch.object(live, "mutate", f.mutate),
                      mock.patch.object(gapi, "source", lambda a: "api"),
                      mock.patch.object(core, "draft", lambda a: {"items": items}),
                      mock.patch.object(core, "remove", lambda a, ids: None)):
                st.enter_context(p)
            t0 = time.monotonic()
            if what == "preview":
                res = live.preview(GACCT, items=items, source="api")
            else:
                res = live.push(GACCT, accept_spend=True)
            wall = time.monotonic() - t0
        bad = [e["problems"] for e in (res.get("items") or res.get("preview", {}).get("items") or []) if e.get("problems")]
        out[what] = {"wall": wall, "ok": bool(res.get("ok")), "problems": bad[:3], "message": res.get("message"),
                     "calls": rec.counts(), "n_calls": len(rec.calls), "peak": rec.peak, "spans": rec.spans(t0),
                     "pace_wait": 0.0, "pace_waits": 0}
    return out


# ---------- run + report ----------

def run(n, latency, baseline=True, workers=None, reuse=False):
    """{"meta": {preview, push} (the default: the push checks every step again), "meta_reuse": {...} (the push reuses
    the preview's passes, REUSE_CHECKS on, if reuse), "meta_seq": {...} (workers=1, if baseline), "google": {...}}."""
    out = {"n": n, "latency": latency, "meta": bench_meta(n, latency, workers), "google": bench_google(n, latency)}
    if reuse:
        out["meta_reuse"] = bench_meta(n, latency, workers, reuse=True)
    if baseline:
        out["meta_seq"] = bench_meta(n, latency, workers=1, reuse=False)
    return out


def _calls_txt(calls):
    return ", ".join(f"{k} {v}" for k, v in sorted(calls.items()))


RUNS = (("meta", "Meta"), ("meta_reuse", "Meta, reuse checks"), ("google", "Google"), ("meta_seq", "Meta, sequential"))


def report(results, file=sys.stdout):
    """A small table, then the call breakdown of every run, then the missed targets."""
    p = lambda *a: print(*a, file=file)  # noqa: E731
    lat = results[0]["latency"] if results else 0
    p(f"\nLive preview / push benchmark (fake APIs, {lat:.2f} s per call; pacing: Meta {meta_api.PACE_RATE:g}/s, "
      f"burst {meta_api.PACE_BURST})")
    p(f"{'channel':<20}{'N':>5}{'preview s':>11}{'push s':>9}{'both s':>9}{'target':>9}  {'calls (prev / push)':<20}  verdict")
    missed = []
    for r in results:
        n = r["n"]
        tgt = target_for(n)
        for key, label in RUNS:
            if key not in r:
                continue
            pv, pu = r[key]["preview"], r[key]["push"]
            ok = pv["ok"] and pu["ok"]
            hit = pv["wall"] <= tgt and pu["wall"] <= tgt
            verdict = "-" if key == "meta_seq" else ("ok" if hit else "MISSED") if ok else "FAILED: " + str(pv["problems"] or pu["message"])
            p(f"{label:<20}{n:>5}{pv['wall']:>11.2f}{pu['wall']:>9.2f}{pv['wall'] + pu['wall']:>9.2f}{tgt:>9.0f}  "
              f"{str(pv['n_calls']) + ' / ' + str(pu['n_calls']):<20}  {verdict}")
            if key != "meta_seq" and not hit:
                missed.append((label, n, r[key]))
    p("\nCalls per type, peak calls in flight, Meta pacing wait (sum over threads), call-type time span [first start, last end]:")
    for r in results:
        for key, label in RUNS:
            if key not in r:
                continue
            for what in ("preview", "push"):
                x = r[key][what]
                spans = "; ".join(f"{k} {a:.1f}-{b:.1f}s" for k, (a, b) in sorted(x["spans"].items(), key=lambda kv: kv[1][0]))
                p(f"  {label} N={r['n']} {what}: {_calls_txt(x['calls'])} | peak {x['peak']} | pace wait {x['pace_wait']:.1f}s "
                  f"({x['pace_waits']} calls waited) | {spans}")
    if missed:
        p("\nMissed targets:")
        for label, n, x in missed:
            for what in ("preview", "push"):
                y = x[what]
                if y["wall"] > target_for(n):
                    floor = y["n_calls"] / max(meta_api.PACE_RATE, 1e-9) if label.startswith("Meta") else 0
                    p(f"  {label} N={n} {what}: {y['wall']:.1f}s > {target_for(n):.0f}s. {y['n_calls']} calls, peak {y['peak']} "
                      f"in flight" + (f"; the pacing bucket alone needs about {max(0.0, floor - meta_api.PACE_BURST / meta_api.PACE_RATE):.1f}s "
                                      f"for that many calls (pace wait summed over threads {y['pace_wait']:.1f}s)" if label.startswith("Meta") else ""))
    return missed


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--n", type=int, nargs="+", default=[20, 200])
    ap.add_argument("--latency", type=float, default=0.3)
    ap.add_argument("--no-baseline", action="store_true", help="skip the Meta workers=1 run (it is slow at N=200)")
    ap.add_argument("--reuse", action="store_true", help="also run Meta with the push reusing the preview's checks (REUSE_CHECKS)")
    ap.add_argument("--json", help="also write the raw measurements to this file")
    a = ap.parse_args(argv)
    results = []
    for n in a.n:
        print(f"running N={n} ...", file=sys.stderr, flush=True)
        results.append(run(n, a.latency, baseline=not a.no_baseline, reuse=a.reuse))
    missed = report(results)
    if a.json:
        Path(a.json).write_text(json.dumps(results, indent=1, default=str))
    return 1 if missed else 0


if __name__ == "__main__":
    sys.exit(main())
