"""Budget pacing (pacing.py, areas/pacing.py, the pacing_targets team setting, the MCP tools). Run from the engine folder:
    python3 tests/test_pacing.py

Offline: Google and Meta reads are replaced with fixed answers (fakes); nothing is sent anywhere. Runs in a temp
MSTUDIO_HOME with MSTUDIO_KEYCHAIN=0, so the real data folder and the Keychain are never touched.
"""
import os as _os, tempfile as _tf
_os.environ["MSTUDIO_HOME"] = _tf.mkdtemp(prefix="mstudio-pacing-test-")
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
TMP = Path(tempfile.mkdtemp(prefix="pacing-test-"))
os.environ["STUDIO_DATA_DIR"] = str(TMP)

import core  # noqa: E402

core.STUDIO_DIR = TMP
import datapolicy  # noqa: E402
import meta_ads as meta  # noqa: E402
import pacing  # noqa: E402
import team_settings  # noqa: E402

D = dt.date
AS_OF = D(2026, 11, 17)  # a Tuesday
NOW = dt.datetime(2026, 11, 17, 12, 0, tzinfo=dt.timezone.utc)
G_ACCT = {"id": "1234567890", "key": "1234567890", "name": "Google test", "platform": "google"}
M_ACCT = {"id": "meta_555", "key": "meta_555", "name": "Meta test", "platform": "meta", "act": "act_555"}


def days_back(n, value, as_of=AS_OF, weekend=None):
    """{iso date: spend} for the n days before as_of (weekend: other value on Sat/Sun)."""
    out = {}
    for k in range(1, n + 1):
        d = as_of - dt.timedelta(days=k)
        out[d.isoformat()] = weekend if weekend is not None and d.weekday() >= 5 else value
    return out


class Forecast(unittest.TestCase):
    def test_flat_spend(self):
        f = pacing.forecast(days_back(28, 100.0), AS_OF, budget=None, active=True)
        self.assertEqual(f["mtd_done"], 1600.0)  # 1-16 November
        self.assertEqual(f["base"], 100.0)
        self.assertEqual(f["future_days"], 13)  # 18-30 November
        self.assertEqual(f["total"], 1600 + 100 + 1300)
        self.assertTrue(f["weekday"])

    def test_weekday_pattern(self):
        f = pacing.weekday_factors({D.fromisoformat(k): v for k, v in days_back(28, 100.0, weekend=20.0).items()},
                                   [AS_OF - dt.timedelta(days=k) for k in range(1, 29)])
        self.assertLess(f[5], 0.6)
        self.assertGreater(f[1], 1.1)
        self.assertAlmostEqual(sum(f.values()) / 7, 1.0, places=6)
        fc = pacing.forecast(days_back(28, 100.0, weekend=20.0), AS_OF, active=True)
        # 13 future days: 18-30 Nov has 4 weekend days; a flat forecast would put weekends at the average
        flat = (5 * 100 + 2 * 20) / 7 * 13
        self.assertLess(fc["rest"], flat)
        self.assertAlmostEqual(fc["rest"], 9 * 100 + 4 * 20, delta=15)  # the true weekday pattern: 9 weekdays, 4 weekend days

    def test_short_history_is_flat(self):
        f = pacing.weekday_factors({}, [AS_OF - dt.timedelta(days=k) for k in range(1, 8)])
        self.assertEqual(set(f.values()), {1.0})
        fc = pacing.forecast(days_back(5, 50.0), AS_OF, active=True)  # campaign started 5 days ago
        self.assertEqual(fc["base"], 50.0)
        self.assertFalse(fc["weekday"])

    def test_paused_adds_nothing(self):
        d = days_back(28, 100.0)
        d[AS_OF.isoformat()] = 30.0
        f = pacing.forecast(d, AS_OF, active=False)
        self.assertEqual(f["total"], 1630.0)
        self.assertEqual(f["rest"], 0.0)

    def test_budget_caps_rest(self):
        f = pacing.forecast(days_back(28, 100.0), AS_OF, budget=40.0, active=True)
        self.assertEqual(f["rest"], 40.0 * 13)
        self.assertEqual(f["today_expected"], 40.0)

    def test_today_spent_floor(self):
        d = days_back(28, 100.0)
        d[AS_OF.isoformat()] = 180.0  # already over the run-rate today
        f = pacing.forecast(d, AS_OF, active=True)
        self.assertEqual(f["today_expected"], 180.0)

    def test_needed_and_flag(self):
        self.assertAlmostEqual(pacing.needed_daily(3000, 1600, 14), 100.0)
        self.assertEqual(pacing.needed_daily(1000, 1600, 14), 0.0)
        self.assertEqual(pacing.flag(3300, 3000, 5)[0], "over")
        self.assertEqual(pacing.flag(3300, 3000, 5)[1], 300.0)
        self.assertEqual(pacing.flag(2800, 3000, 5)[0], "under")
        self.assertEqual(pacing.flag(3100, 3000, 5)[0], "on_track")
        self.assertEqual(pacing.flag(3100, None, 5)[0], "no_target")

    def test_month_end(self):
        f = pacing.forecast(days_back(28, 10.0, as_of=D(2026, 11, 30)), D(2026, 11, 30), active=True)
        self.assertEqual(f["future_days"], 0)
        self.assertEqual(f["days_left"], 1)


def fake_google(rows_by_query):
    calls = []

    def q(cid, gaql):
        calls.append(gaql)
        for key, rows in rows_by_query.items():
            if key in gaql:
                return rows
        raise AssertionError("unexpected query " + gaql[:60])
    q.calls = calls
    return q


def google_rows(spend=100.0):
    camps = [
        {"campaign": {"id": "11", "name": "Search A", "status": "ENABLED", "servingStatus": "SERVING", "advertisingChannelType": "SEARCH",
                      "biddingStrategyType": "MANUAL_CPC", "primaryStatusReasons": ["BUDGET_CONSTRAINED"]},
         "campaignBudget": {"amountMicros": "100000000", "period": "DAILY", "explicitlyShared": False}},
        {"campaign": {"id": "12", "name": "Shared B", "status": "ENABLED", "servingStatus": "SERVING", "advertisingChannelType": "SEARCH",
                      "biddingStrategyType": "TARGET_CPA"},
         "campaignBudget": {"amountMicros": "50000000", "period": "DAILY", "explicitlyShared": True}},
        {"campaign": {"id": "13", "name": "Paused C", "status": "PAUSED", "servingStatus": "SERVING", "advertisingChannelType": "SEARCH"},
         "campaignBudget": {"amountMicros": "20000000", "period": "DAILY"}},
        {"campaign": {"id": "14", "name": "Total D", "status": "ENABLED", "servingStatus": "SERVING", "advertisingChannelType": "PERFORMANCE_MAX"},
         "campaignBudget": {"amountMicros": "0", "period": "CUSTOM_PERIOD", "totalAmountMicros": "900000000"}},
    ]
    cost = []
    for d, v in days_back(28, spend).items():
        cost.append({"campaign": {"id": "11", "name": "Search A", "status": "ENABLED"}, "segments": {"date": d}, "metrics": {"costMicros": str(int(v * 1e6))}})
        cost.append({"campaign": {"id": "12", "name": "Shared B", "status": "ENABLED"}, "segments": {"date": d}, "metrics": {"costMicros": "50000000"}})
    cost.append({"campaign": {"id": "99", "name": "Old removed", "status": "REMOVED"}, "segments": {"date": "2026-11-02"}, "metrics": {"costMicros": "7000000"}})
    return {"FROM customer": [{"customer": {"timeZone": "Europe/London", "currencyCode": "GBP"}}],
            "campaign_budget.amount_micros": camps, "metrics.cost_micros": cost}


class Readers(unittest.TestCase):
    def test_google(self):
        q = fake_google(google_rows())
        s = pacing.fetch_google("1234567890", query=q, now=NOW)
        self.assertEqual(s["today"], "2026-11-17")
        self.assertEqual(s["currency"], "GBP")
        by = {c["id"]: c for c in s["campaigns"]}
        self.assertEqual(by["11"]["budget"], 100.0)
        self.assertEqual(by["11"]["budget_kind"], "daily")
        self.assertTrue(by["11"]["budget_limited"])
        self.assertEqual(by["12"]["budget_kind"], "shared")
        self.assertTrue(by["12"]["smart_bidding"])
        self.assertFalse(by["13"]["active"])
        self.assertEqual(by["14"]["budget_kind"], "total")
        self.assertEqual(by["99"]["status"], "Removed")
        self.assertEqual(by["99"]["daily"], {"2026-11-02": 7.0})
        self.assertIn("BETWEEN '2026-10-20' AND '2026-11-17'", q.calls[-1])  # 28 days back is earlier than the 1st
        self.assertTrue(all(x.lstrip().upper().startswith("SELECT") for x in q.calls))

    def test_meta(self):
        s = meta_snapshot()
        by = {c["id"]: c for c in s["campaigns"]}
        self.assertEqual(s["account_id"], "meta_555")
        self.assertEqual(by["c1"]["budget_kind"], "daily")  # CBO
        self.assertEqual(by["c1"]["budget"], 50.0)
        self.assertEqual(by["c1"]["units"][0]["learning"], "in the learning phase")
        self.assertEqual(by["c2"]["budget_kind"], "adsets")  # ABO
        self.assertEqual([u["budget"] for u in by["c2"]["units"]], [20.0, 10.0])
        self.assertEqual(by["c2"]["budget"], 30.0)
        self.assertEqual(by["c3"]["budget_kind"], "lifetime")
        self.assertNotIn("c4", by)  # deleted, no spend this month


META_CAMPS = [
    {"id": "c1", "name": "CBO one", "status": "ACTIVE", "effective_status": "ACTIVE", "daily_budget": "5000", "budget_remaining": "2000"},
    {"id": "c2", "name": "ABO two", "status": "ACTIVE", "effective_status": "ACTIVE"},
    {"id": "c3", "name": "Lifetime three", "status": "ACTIVE", "effective_status": "ACTIVE", "lifetime_budget": "300000"},
    {"id": "c4", "name": "Gone", "status": "DELETED", "effective_status": "DELETED", "daily_budget": "1000"},
]
META_SETS = [
    {"id": "s1", "campaign_id": "c1", "name": "Set 1", "status": "ACTIVE", "effective_status": "ACTIVE", "learning_stage_info": {"status": "LEARNING"}},
    {"id": "s2", "campaign_id": "c2", "name": "Set 2a", "status": "ACTIVE", "effective_status": "ACTIVE", "daily_budget": "2000",
     "learning_stage_info": {"status": "SUCCESS"}},
    {"id": "s3", "campaign_id": "c2", "name": "Set 2b", "status": "ACTIVE", "effective_status": "ACTIVE", "daily_budget": "1000"},
    {"id": "s4", "campaign_id": "c3", "name": "Set 3", "status": "ACTIVE", "effective_status": "ACTIVE", "lifetime_budget": "300000"},
]


def meta_fakes(spend_c1=50.0):
    sent = []

    def get(path, **p):
        sent.append(("GET", path, p))
        return {"name": "Meta test", "currency": "USD", "timezone_name": "Europe/London"}

    def get_all(path, **p):
        sent.append(("GET", path, p))
        if path.endswith("/campaigns"):
            return META_CAMPS
        if path.endswith("/adsets"):
            return META_SETS
        level = p["level"]
        assert p["time_increment"] == 1 and p["time_range"]["until"] == "2026-11-17"
        rows = []
        for d, _ in days_back(28, 0).items():
            if level == "campaign":
                rows += [{"campaign_id": "c1", "spend": str(spend_c1), "date_start": d}, {"campaign_id": "c2", "spend": "30", "date_start": d}]
            else:
                rows += [{"adset_id": "s2", "spend": "20", "date_start": d}, {"adset_id": "s3", "spend": "10", "date_start": d}]
        return rows
    return get, get_all, sent


def meta_snapshot(spend_c1=50.0):
    get, get_all, sent = meta_fakes(spend_c1)
    s = pacing.fetch_meta("act_555", get=get, get_all=get_all, now=NOW)
    assert all(m == "GET" for m, _, _ in sent)
    return s


TG = {"accounts": {}, "tolerance_pct": 5.0, "meta_step_pct": 20.0}


class Views(unittest.TestCase):
    def test_google_account_target(self):
        snap = pacing.fetch_google("1234567890", query=fake_google(google_rows()), now=NOW)
        tg = {**TG, "accounts": {G_ACCT["id"]: {"monthly": 6000, "campaigns": {}}}}
        v = pacing.account_view(G_ACCT, snap, tg)
        by = {c["id"]: c for c in v["campaigns"]}
        self.assertNotIn("13", by)  # paused, no spend, no target: hidden
        self.assertIn("99", by)  # removed, but spent this month
        self.assertEqual(v["mtd"], 1600 + 800 + 7)
        self.assertEqual(v["target"], 6000)
        self.assertEqual(v["flag"], "under")
        # Search A is the only adjustable campaign: it gets the rest of the target
        a = by["11"]
        self.assertEqual(a["target_source"], "share")
        self.assertEqual(len(a["recommendations"]), 1)
        r = a["recommendations"][0]
        self.assertTrue(r["raise"])
        self.assertEqual(r["level"], "campaign")
        self.assertIn("2x", r["why"])
        self.assertIn("30.4x", r["why"])
        self.assertEqual(by["12"]["recommendations"], [])  # shared budget: left alone
        self.assertTrue(any("Shared budget" in n for n in by["12"]["notes"]))
        # landing check: spend so far + needed per day x days left = the share
        self.assertAlmostEqual(a["mtd"] - a["today"] + a["needed_daily"] * v["days_left"], a["target"], delta=0.05)

    def test_google_big_jump_and_lower(self):
        snap = pacing.fetch_google("1234567890", query=fake_google(google_rows()), now=NOW)
        own = {**TG, "accounts": {G_ACCT["id"]: {"monthly": None, "campaigns": {"11": 9000}}}}
        r = pacing.account_view(G_ACCT, snap, own)["campaigns"]
        rec = next(c for c in r if c["id"] == "11")["recommendations"][0]
        self.assertTrue(any("Large change" in w for w in rec["warnings"]))
        self.assertEqual(next(c for c in r if c["id"] == "11")["flag"], "under")
        low = {**TG, "accounts": {G_ACCT["id"]: {"monthly": None, "campaigns": {"11": 2100}}}}
        rec = next(c for c in pacing.account_view(G_ACCT, snap, low)["campaigns"] if c["id"] == "11")["recommendations"][0]
        self.assertFalse(rec["raise"])
        self.assertAlmostEqual(rec["proposed"], (2100 - 1600) / 14, places=2)
        ok = {**TG, "accounts": {G_ACCT["id"]: {"monthly": None, "campaigns": {"11": 3000}}}}
        self.assertEqual(next(c for c in pacing.account_view(G_ACCT, snap, ok)["campaigns"] if c["id"] == "11")["recommendations"], [])

    def test_meta_step_cap_and_learning(self):
        snap = meta_snapshot()
        tg = {**TG, "accounts": {M_ACCT["id"]: {"monthly": None, "campaigns": {"c1": 3000, "c2": 1200}}}}
        v = pacing.account_view(M_ACCT, snap, tg)
        by = {c["id"]: c for c in v["campaigns"]}
        r = by["c1"]["recommendations"][0]
        self.assertEqual(r["current"], 50.0)
        self.assertEqual(r["proposed"], 60.0)  # +20% step, not the full need
        self.assertTrue(r["capped"])
        self.assertGreater(r["goal"], 100)
        self.assertIn("restart Meta's learning", r["why"])
        self.assertTrue(any("learning phase" in w for w in r["warnings"]))
        # ABO: one recommendation per ad set, each within the step
        rs = by["c2"]["recommendations"]
        self.assertEqual({x["id"] for x in rs}, {"s2", "s3"})
        for x in rs:
            self.assertEqual(x["level"], "adset")
            self.assertLessEqual(abs(x["change_pct"]), 20.0001)
        self.assertEqual(by["c3"]["recommendations"], [])
        self.assertTrue(any("Lifetime" in n for n in by["c3"]["notes"]))

    def test_meta_lower_step(self):
        snap = meta_snapshot()
        tg = {**TG, "meta_step_pct": 10.0, "accounts": {M_ACCT["id"]: {"monthly": None, "campaigns": {"c1": 900}}}}
        r = next(c for c in pacing.account_view(M_ACCT, snap, tg)["campaigns"] if c["id"] == "c1")["recommendations"][0]
        self.assertEqual(r["proposed"], 45.0)  # -10% step
        self.assertFalse(r["raise"])


class Cache(unittest.TestCase):
    def test_kept_until_refresh(self):
        n = {"calls": 0}

        def fetch(a):
            n["calls"] += 1
            return pacing.fetch_google(a["id"], query=fake_google(google_rows()), now=NOW)
        pacing.snapshot(G_ACCT, fetch=fetch)
        pacing.snapshot(G_ACCT, fetch=fetch)
        self.assertEqual(n["calls"], 1)
        pacing.invalidate(G_ACCT["id"])
        pacing.snapshot(G_ACCT, fetch=fetch)
        self.assertEqual(n["calls"], 2)
        pacing.snapshot(G_ACCT, fetch=fetch, force=True)
        self.assertEqual(n["calls"], 3)
        datapolicy.invalidate("all")
        pacing.snapshot(G_ACCT, fetch=fetch)
        self.assertEqual(n["calls"], 4)
        self.assertTrue(datapolicy.fetched("pacing:" + G_ACCT["id"]))


class Targets(unittest.TestCase):
    def test_clean(self):
        ok = team_settings.clean("pacing_targets", {"accounts": {"1234567890": {"monthly": "5000", "campaigns": {"11": 100, "12": None}}}})
        self.assertEqual(ok["accounts"]["1234567890"], {"monthly": 5000, "campaigns": {"11": 100}})
        self.assertEqual(ok["tolerance_pct"], 5)
        for bad in ({"accounts": {"bad id!": {"monthly": 1}}}, {"accounts": {"x": {"monthly": -1}}},
                    {"accounts": {"x": {"campaigns": {"a b": 1}}}}, {"accounts": {}, "meta_step_pct": 90},
                    {"accounts": {"x": {"monthly": "lots"}}}):
            with self.assertRaises(ValueError):
                team_settings.clean("pacing_targets", bad)

    def test_save_local(self):
        pacing.save_targets({"account": "1234567890", "monthly": 4000})
        pacing.save_targets({"account": "1234567890", "campaigns": {"11": 1500}})
        pacing.save_targets({"tolerance_pct": 8})
        t = pacing.targets()
        self.assertEqual(t["accounts"]["1234567890"], {"monthly": 4000, "campaigns": {"11": 1500}})
        self.assertEqual(t["tolerance_pct"], 8)
        pacing.save_targets({"account": "1234567890", "monthly": None, "campaigns": {"11": None}})
        self.assertNotIn("1234567890", pacing.targets()["accounts"])
        with self.assertRaises(ValueError):
            pacing.save_targets({"share": True})  # not in a team
        self.assertIn("pacing_targets", team_settings.info()["keys"])


def g_snap(a):
    return pacing.fetch_google(a["id"], query=fake_google(google_rows()), now=NOW)


def m_snap(a):
    return meta_snapshot()


META_DATA = {"account": {"currency": "USD"}, "campaigns": META_CAMPS, "adsets": META_SETS, "ads": [], "audiences": [], "insights": {}}


class Draft(unittest.TestCase):
    def setUp(self):
        self.p = [mock.patch.object(pacing, "accounts", lambda: [G_ACCT, M_ACCT]),
                  mock.patch.object(pacing, "_fetch", lambda a, now=None: g_snap(a) if a["platform"] == "google" else m_snap(a)),
                  mock.patch.object(pacing, "targets", lambda: {**TG, "accounts": {G_ACCT["id"]: {"monthly": 6000, "campaigns": {}},
                                                                                     M_ACCT["id"]: {"monthly": None, "campaigns": {"c1": 3000}}}}),
                  mock.patch.object(meta, "load", lambda *a, **k: META_DATA),
                  mock.patch.object(meta, "settings", lambda: {"meta_accounts": [{"act": "act_555", "name": "Meta test"}], "views": []})]
        for x in self.p:
            x.start()
        for a in (G_ACCT, M_ACCT):
            pacing.invalidate(a["id"])
        core.clear("1234567890")

    def tearDown(self):
        for x in self.p:
            x.stop()

    def test_google_to_draft(self):
        r = pacing.add_to_draft("1234567890")
        self.assertEqual(len(r["added"]), 1, r)
        items = core.draft("1234567890")["items"]
        self.assertEqual(len(items), 1)
        c = items[0]["change"]
        self.assertEqual((c["type"], c["campaign"]), ("campaign", "Search A"))
        self.assertGreater(c["budget"], 100)
        self.assertTrue(items[0]["note"].startswith("Pacing:"))
        self.assertEqual(items[0]["problems"], [])
        pacing.add_to_draft("1234567890")  # again: replaces its own item, no duplicate
        self.assertEqual(len(core.draft("1234567890")["items"]), 1)
        core.clear("1234567890")
        core.add("1234567890", [{"type": "campaign", "campaign": "Search A", "budget": 120}], by="you", note="mine")
        r = pacing.add_to_draft("1234567890")
        self.assertEqual(r["added"], [])
        self.assertIn("already has a budget change", r["skipped"][0]["why"])
        self.assertEqual(core.draft("1234567890")["items"][0]["note"], "mine")  # the user's change is untouched

    def test_meta_to_draft(self):
        r = pacing.add_to_draft("meta_555", ids=["c1"], by="claude")
        self.assertEqual(len(r["added"]), 1, r)
        meta.use("meta_555")
        items = core.draft(meta.acct())["items"]
        c = items[0]["change"]
        self.assertEqual(c, {"type": "meta_budget", "level": "campaign", "id": "c1", "budget": 60.0})
        self.assertEqual(items[0]["by"], "claude")
        self.assertEqual(items[0]["problems"], [])
        self.assertIn("learning", items[0]["note"])

    def test_status_and_drift(self):
        v = pacing.status()
        self.assertEqual({x["account"] for x in v["accounts"]}, {G_ACCT["id"], M_ACCT["id"]})
        self.assertEqual(v["errors"], [])
        rec = pacing.drift_check(refresh=True)
        self.assertEqual(rec["checked"], 2)
        self.assertEqual([x["account"] for x in rec["drifting"]], [G_ACCT["id"]])
        self.assertEqual(pacing.last_drift()["drifting"][0]["flag"], "under")

    def test_mcp(self):
        import mcp_server  # noqa: PLC0415
        names = {t["name"] for t in mcp_server.TOOLS}
        self.assertTrue({"pacing_status", "pacing_add_to_draft"} <= names)
        out = mcp_server.call("pacing_status", {"account": "meta_555"})
        self.assertEqual(out["accounts"][0]["account"], "meta_555")
        out = mcp_server.call("pacing_add_to_draft", {"account": "1234567890"})
        self.assertEqual(core.draft("1234567890")["items"][0]["by"], "claude")
        self.assertNotIn("push", json.dumps([t["name"] for t in mcp_server.PACING_TOOLS]))

    def test_area_routes(self):
        import areas  # noqa: PLC0415
        self.assertIn("pacing", areas.AREAS)
        self.assertEqual(list(areas.AREAS)[0], "pacing")
        a = areas.AREAS["pacing"]
        self.assertEqual(set(a.GET), {"status"})
        self.assertEqual(set(a.POST), {"refresh", "targets", "draft"})
        self.assertEqual(len(a.GET["status"]({})["accounts"]), 2)


class Job(unittest.TestCase):
    def test_register(self):
        import scheduler  # noqa: PLC0415
        scheduler._reset_for_tests()
        row = pacing.register_job()
        self.assertFalse(row["enabled"])
        self.assertTrue(row["refresh"])  # held while Refresh data is manual: no pulls on its own
        self.assertIn("daily at 07:00", row["schedule"])
        scheduler._reset_for_tests()


class Text(unittest.TestCase):
    def test_house_style(self):
        for f in ("pacing.py", "areas/pacing.py", "static/pacing.js", "tests/test_pacing.py"):
            t = (HERE / f).read_text(encoding="utf-8")
            self.assertNotIn(chr(0x2014), t, f)
            for word in ("ML " + "Suite", "Master " + "Liveaboards"):
                self.assertNotIn(word, t, f)


if __name__ == "__main__":
    unittest.main(verbosity=1)
