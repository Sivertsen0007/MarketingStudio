"""Ads alerts (alerts.py, areas/alerts.py, the scheduler jobs, the refresh hook, the MCP tools). Run from the engine folder:
    python3 tests/test_alerts.py

Offline: Google and Meta reads are fakes; macOS notifications and team sends are replaced, so nothing is shown or sent.
Runs in a temp MSTUDIO_HOME with MSTUDIO_KEYCHAIN=0, so the real data folder and the Keychain are never touched.
"""
import os as _os, tempfile as _tf
_os.environ["MSTUDIO_HOME"] = _tf.mkdtemp(prefix="mstudio-alerts-test-")
_os.environ["MSTUDIO_KEYCHAIN"] = "0"
_os.environ["MSTUDIO_NO_MAC_NOTIFY"] = "1"

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
TMP = Path(tempfile.mkdtemp(prefix="alerts-test-"))
os.environ["STUDIO_DATA_DIR"] = str(TMP)

import core  # noqa: E402

core.STUDIO_DIR = TMP
import alerts  # noqa: E402
import datapolicy  # noqa: E402
import scheduler  # noqa: E402

NOW = dt.datetime(2026, 11, 17, 12, 0, tzinfo=dt.timezone.utc).timestamp()  # a Tuesday, noon in London
TODAY = dt.date(2026, 11, 17)
G = {"id": "1234567890", "key": "1234567890", "name": "Google test", "platform": "google"}
M = {"id": "meta_555", "key": "meta_555", "name": "Meta test", "platform": "meta", "act": "act_555"}


def iso(k):
    return (TODAY - dt.timedelta(days=k)).isoformat()


def g_rows(today_cost=150.0, conv_week=(20, 12)):
    """Campaign 11 (budget 100/day) spends 100/day, 150 today; campaign 12 active but no delivery; 13 paused."""
    camps = [
        {"customer": {"timeZone": "Europe/London", "currencyCode": "GBP"},
         "campaign": {"id": "11", "name": "Search <A>", "status": "ENABLED", "servingStatus": "SERVING"},
         "campaignBudget": {"id": "b1", "amountMicros": "100000000", "period": "DAILY"}},
        {"customer": {"timeZone": "Europe/London", "currencyCode": "GBP"},
         "campaign": {"id": "12", "name": "Silent B", "status": "ENABLED", "servingStatus": "SERVING"},
         "campaignBudget": {"id": "b2", "amountMicros": "20000000", "period": "DAILY"}},
        {"customer": {"timeZone": "Europe/London", "currencyCode": "GBP"},
         "campaign": {"id": "13", "name": "Paused C", "status": "PAUSED", "servingStatus": "SERVING"},
         "campaignBudget": {"id": "b3", "amountMicros": "20000000", "period": "DAILY"}},
    ]
    daily = []
    for k in range(1, 15):
        conv = conv_week[0] / 7 if k <= 7 else conv_week[1] / 7  # this week vs the week before
        daily.append({"campaign": {"id": "11"}, "segments": {"date": iso(k)},
                      "metrics": {"costMicros": "100000000", "clicks": "50", "impressions": "1000", "conversions": conv,
                                  "conversionsValue": conv * 50}})
    daily.append({"campaign": {"id": "11"}, "segments": {"date": iso(0)},
                  "metrics": {"costMicros": str(int(today_cost * 1e6)), "clicks": "60", "impressions": "900", "conversions": 1}})
    daily.append({"campaign": {"id": "12"}, "segments": {"date": iso(3)}, "metrics": {"costMicros": "5000000", "impressions": "40"}})
    ads = [{"campaign": {"id": "11", "name": "Search <A>"}, "adGroup": {"id": "7", "name": "AG"},
            "adGroupAd": {"ad": {"id": "99", "name": "Bad ad"}, "policySummary": {"approvalStatus": "DISAPPROVED"}}}]
    return {"campaign.status != 'REMOVED'": camps, "segments.date BETWEEN": daily, "FROM ad_group_ad": ads}


def fake_query(table):
    calls = []

    def q(cid, gaql):
        calls.append(gaql)
        for k, rows in table.items():
            if k in gaql:
                return rows
        raise AssertionError("unexpected GAQL " + gaql[:80])
    q.calls = calls
    return q


def meta_node(learning="FAIL", adset_level=False):
    ins = []
    for k in range(0, 15):
        row = {"spend": "40" if k else "10", "clicks": "20", "impressions": "2000" if k else "0",
               "actions": [{"action_type": "lead", "value": "2"}, {"action_type": "link_click", "value": "18"}],
               "action_values": [{"action_type": "offsite_conversion.fb_pixel_purchase", "value": "80"}], "date_start": iso(k)}
        ins.append({**row, ("adset_id" if adset_level else "campaign_id"): "s1" if adset_level else "c1"})
    return {"name": "Meta test", "currency": "EUR", "timezone_name": "Europe/Copenhagen",
            "campaigns": {"data": [{"id": "c1", "name": "Leads", "effective_status": "ACTIVE"},
                                   {"id": "c2", "name": "Off", "effective_status": "PAUSED", "daily_budget": "1000"}]},
            "adsets": {"data": [{"id": "s1", "name": "Set 1", "campaign_id": "c1", "effective_status": "ACTIVE", "daily_budget": "5000",
                                 "learning_stage_info": {"status": learning}},
                                {"id": "s2", "name": "Set 2", "campaign_id": "c1", "effective_status": "ACTIVE", "daily_budget": "2500",
                                 "learning_stage_info": {"status": "SUCCESS"}}]},
            "ads": {"data": [{"id": "a1", "name": "Ad <x>", "campaign_id": "c1", "adset_id": "s1"}]},
            "insights": {"data": ins}}


def fake_get(node):
    calls = []

    def get(path, **params):
        calls.append((path, params))
        if params.get("fields") == "timezone_name":
            return {"timezone_name": node["timezone_name"]}
        return node
    get.calls = calls
    return get


def reset():
    for f in TMP.glob("alerts_*"):
        f.unlink()
    datapolicy.DIR = None


def rule(**kw):
    r = alerts.normalise({"account": "*", **kw}, accts=[G, M])
    r["id"] = kw.get("id", "r_" + kw["metric"])
    return r


class Rules(unittest.TestCase):
    def setUp(self):
        reset()

    def test_starters_seeded_off(self):
        rs = alerts.rules()
        self.assertEqual({r["starter"] for r in rs}, {s["starter"] for s in alerts.STARTERS})
        self.assertFalse(any(r["enabled"] for r in rs))
        cpa = next(r for r in rs if r["starter"] == "cpa_up_week")
        self.assertEqual((cpa["op"], cpa["value"], cpa["window"], cpa["min"]), ("up_pct", 50.0, "last_7_days", {"conversions": 10.0}))
        self.assertEqual(alerts.cadence(cpa), "daily")
        self.assertEqual(next(r for r in rs if r["starter"] == "learning_limited")["platform"], "meta")
        alerts.delete_rule("r_no_delivery")
        self.assertEqual(len(alerts.rules()), len(alerts.STARTERS) - 1)  # seeded once: a deleted starter stays deleted

    def test_validation(self):
        bad = [{"metric": "nope"}, {"metric": "cpa", "op": "above"}, {"metric": "cpa", "op": "sideways", "value": 1},
               {"metric": "cpa", "value": 1, "window": "forever"}, {"metric": "learning_limited", "platform": "google"},
               {"metric": "spend", "value": 1, "level": "adset", "account": G["id"]}, {"metric": "spend", "value": 1, "quiet_hours": 0},
               {"metric": "spend", "value": 1, "min": {"ctr": 2}}, {"metric": "disapproved_ads", "op": "up_pct", "value": 5},
               {"metric": "spend", "value": 1, "account": "nobody"}, {"metric": "spend", "value": 1, "object_id": "1"},
               {"metric": "spend", "value": "x"}, {"metric": "spend", "op": "up_pct", "value": 0}]
        for b in bad:
            with self.assertRaises(ValueError, msg=b):
                alerts.normalise(b, accts=[G, M])
        r = alerts.normalise({"metric": "spend", "value": 5, "account": "meta_555", "level": "adset"}, accts=[G, M])
        self.assertEqual(r["platform"], "meta")
        r = alerts.normalise({"metric": "no_delivery", "op": "below", "value": 9}, accts=[G, M])
        self.assertEqual((r["op"], r["value"]), ("above", 0))
        self.assertIn("Spend", alerts.normalise({"metric": "spend", "value": 5}, accts=[])["name"])

    def test_add_update_delete(self):
        with mock.patch("pacing.accounts", return_value=[G, M]):
            r = alerts.add_rule({"metric": "ctr", "op": "below", "value": 1.5, "account": G["key"], "level": "campaign"}, by="claude")
            self.assertEqual((r["account"], r["platform"], r["created_by"], r["enabled"]), (G["id"], "google", "claude", True))
            r2 = alerts.update_rule(r["id"], {"enabled": False})
            self.assertFalse(r2["enabled"])
            r3 = alerts.update_rule(r["id"], {"value": 2})
            self.assertEqual(r3["value"], 2.0)
            alerts.delete_rule(r["id"])
            self.assertNotIn(r["id"], [x["id"] for x in alerts.rules()])


class Google(unittest.TestCase):
    def setUp(self):
        reset()
        self.q = fake_query(g_rows())
        self.snap = alerts.fetch_google(G, {"metrics": True, "disapproved": True}, query=self.q, now=NOW)

    def test_reads(self):
        s = self.snap
        self.assertEqual((s["today"], s["tz"], s["currency"], s["day_part"]), ("2026-11-17", "Europe/London", "GBP", 0.5))
        self.assertEqual(len(self.q.calls), 3)
        self.assertTrue(all(c.startswith("SELECT") for c in self.q.calls))
        self.assertIn("BETWEEN '2026-11-03' AND '2026-11-17'", self.q.calls[1])
        self.assertTrue(s["campaigns"]["11"]["active"])
        self.assertFalse(s["campaigns"]["13"]["active"])
        self.assertEqual(s["daily"]["campaign"]["11"][iso(0)]["spend"], 150.0)
        q = fake_query(g_rows())
        alerts.fetch_google(G, {"metrics": True}, query=q, now=NOW)
        self.assertEqual(len(q.calls), 2)  # no disapproved query without a rule that needs it

    def test_spend_vs_budget(self):
        hits = alerts.evaluate(rule(metric="spend_vs_budget", op="above", value=120, window="today", level="campaign"), self.snap)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["object"]["id"], "11")
        self.assertEqual(hits[0]["value"], 150.0)
        self.assertIn("150%", hits[0]["text"])
        self.assertIn("daily budget 100.00 GBP", hits[0]["text"])
        self.assertEqual(alerts.evaluate(rule(metric="spend_vs_budget", op="above", value=160, window="today", level="campaign"), self.snap), [])
        acct = alerts.evaluate(rule(metric="spend_vs_budget", op="above", value=130, window="today"), self.snap)
        self.assertEqual(acct, [])  # account: 150 spent of 100 + 20 active budgets = 125%
        acct = alerts.evaluate(rule(metric="spend_vs_budget", op="above", value=124, window="today"), self.snap)
        self.assertEqual(round(acct[0]["value"]), 125)

    def test_no_delivery_and_disapproved(self):
        hits = alerts.evaluate(rule(metric="no_delivery", level="campaign"), self.snap)
        self.assertEqual([h["object"]["name"] for h in hits], ["Silent B"])  # paused C is not looked at
        hits = alerts.evaluate(rule(metric="disapproved_ads", op="above", value=0), self.snap)
        self.assertEqual(hits[0]["value"], 1)
        self.assertIn("Bad ad (Search <A>)", hits[0]["text"])

    def test_change_with_min_volume(self):
        r = rule(metric="cpa", op="up_pct", value=50, window="last_7_days", min={"conversions": 10})
        hits = alerts.evaluate(r, self.snap)  # 700 / 20 = 35 vs 700 / 12 = 58: CPA down, not up
        self.assertEqual(hits, [])
        snap = alerts.fetch_google(G, {"metrics": True}, query=fake_query(g_rows(conv_week=(11, 20))), now=NOW)
        hits = alerts.evaluate(r, snap)
        self.assertEqual(len(hits), 1)
        self.assertAlmostEqual(hits[0]["value"], 705 / 11, places=2)  # campaign 12 spent 5 three days ago
        self.assertAlmostEqual(hits[0]["previous"], 35.0, places=2)
        self.assertIn("up 83% on the 7 days before", hits[0]["text"])
        snap = alerts.fetch_google(G, {"metrics": True}, query=fake_query(g_rows(conv_week=(9, 20))), now=NOW)
        self.assertEqual(alerts.evaluate(r, snap), [])  # under the minimum volume
        snap = alerts.fetch_google(G, {"metrics": True}, query=fake_query(g_rows(conv_week=(20, 9))), now=NOW)
        self.assertEqual(alerts.evaluate(rule(metric="cpa", op="down_pct", value=10, window="last_7_days", min={"conversions": 10}), snap), [])

    def test_today_is_scaled(self):
        # today so far (half the day): 150 spend vs last Tuesday's 100 scaled to half = 50 -> up 200%
        hits = alerts.evaluate(rule(metric="spend", op="up_pct", value=150, window="today"), self.snap)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["previous"], 50.0)  # last Tuesday 100, scaled to half the day
        hits = alerts.evaluate(rule(metric="ctr", op="below", value=7, window="yesterday"), self.snap)
        self.assertEqual(len(hits), 1)  # 50 / 1000 = 5%
        self.assertIn("5.00%", hits[0]["text"])


class Meta(unittest.TestCase):
    def setUp(self):
        reset()

    def test_one_call_with_known_tz(self):
        get = fake_get(meta_node())
        need = alerts.needs([rule(metric="learning_limited", level="adset"), rule(metric="spend", op="above", value=1)])
        s = alerts.fetch_meta(M, need, get=get, now=NOW, tz_hint="Europe/Copenhagen")
        self.assertEqual(len(get.calls), 1)
        path, params = get.calls[0]
        self.assertEqual(path, "act_555")
        f = params["fields"]
        self.assertIn("adsets.limit(500){", f)
        self.assertIn("learning_stage_info", f)
        self.assertIn('insights.level(campaign).time_increment(1).time_range({"since":"2026-11-03","until":"2026-11-17"})', f)
        self.assertNotIn("ads.limit", f)  # no disapproved rule
        self.assertEqual(s["calls"], 1)
        self.assertEqual(s["daily"]["campaign"]["c1"][iso(1)]["conversions"], 2.0)
        self.assertEqual(s["daily"]["campaign"]["c1"][iso(1)]["value"], 80.0)
        hits = alerts.evaluate(rule(metric="learning_limited", level="adset"), s)
        self.assertEqual([h["object"]["name"] for h in hits], ["Set 1"])

    def test_unknown_tz_costs_one_small_get(self):
        get = fake_get(meta_node())
        alerts.fetch_meta(M, {"metrics": True}, get=get, now=NOW)
        self.assertEqual([p["fields"] for _, p in get.calls][0], "timezone_name")
        self.assertEqual(len(get.calls), 2)

    def test_adset_level_rolls_up_and_abo_budget(self):
        get = fake_get(meta_node(adset_level=True))
        r = rule(metric="spend_vs_budget", op="above", value=10, window="yesterday", level="campaign")
        need = alerts.needs([r, rule(metric="cpa", op="above", value=1, level="adset", window="yesterday")])
        self.assertTrue(need["adset_level"])
        s = alerts.fetch_meta(M, need, get=get, now=NOW, tz_hint="Europe/Copenhagen")
        self.assertEqual(s["daily"]["campaign"]["c1"][iso(1)]["spend"], 40.0)
        hits = alerts.evaluate(r, s)  # ABO: campaign budget = 50 + 25
        self.assertEqual(round(hits[0]["value"]), round(40 / 75 * 100))
        hits = alerts.evaluate(rule(metric="disapproved_ads", op="above", value=0, level="adset"), {**s, "disapproved": [
            {"id": "a1", "name": "Ad <x>", "campaign_id": "c1", "adset_id": "s1", "campaign": "Leads", "group": ""}]})
        self.assertEqual([h["object"]["id"] for h in hits], ["s1"])

    def test_paging_follows_next(self):
        node = meta_node()
        node["insights"]["paging"] = {"next": "https://graph.facebook.com/v27.0/next-page"}
        calls = []

        def get(path, **params):
            calls.append(path)
            if path.startswith("https://"):
                return {"data": [{"campaign_id": "c1", "spend": "1", "date_start": iso(0)}]}
            return node
        s = alerts.fetch_meta(M, {"metrics": True}, get=get, now=NOW, tz_hint="Europe/Copenhagen")
        self.assertEqual(len(calls), 2)
        self.assertEqual(s["calls"], 2)

    def test_pressure(self):
        u = {"x-business-use-case-usage": {"1": [{"type": "ads_management", "call_count": 12, "total_cputime": 80, "total_time": 3,
                                                 "estimated_time_to_regain_access": 0}]},
             "x-ad-account-usage": {"acc_id_util_pct": 20}, "x-fb-ads-insights-throttle": {"app_id_util_pct": 5, "acc_id_util_pct": 91}}
        self.assertEqual(alerts.meta_pressure(u), (91.0, 0.0))
        self.assertEqual(alerts._backoff_minutes(91, 0), 60)
        self.assertEqual(alerts._backoff_minutes(76, 0), 20)
        self.assertEqual(alerts._backoff_minutes(10, 0), 0)
        self.assertEqual(alerts._backoff_minutes(10, 7), 12)
        self.assertEqual(alerts.meta_pressure({"x-ad-account-usage": "garbage"}), (0.0, 0.0))


class Check(unittest.TestCase):
    def setUp(self):
        reset()
        alerts.rules()
        self.fetches = []
        self.sent = []
        self.mac = []
        p = [mock.patch("pacing.accounts", return_value=[G, M]), mock.patch("alerts._meta_usage", return_value={}),
             mock.patch("alerts._osascript", side_effect=lambda t, x: self.mac.append((t, x)) or "shown"),
             mock.patch("alerts._team_send", side_effect=lambda t, d: self.sent.append((t, d)) or "sent")]
        for x in p:
            x.start()
            self.addCleanup(x.stop)

    def fetch(self, a, need, tz_hint=None, now=None):
        self.fetches.append((a["id"], dict(need)))
        if a["platform"] == "google":
            return alerts.fetch_google(a, need, query=fake_query(g_rows()), now=NOW)
        return alerts.fetch_meta(a, need, get=fake_get(meta_node()), now=NOW, tz_hint=tz_hint or "Europe/Copenhagen")

    def on(self, rid):
        alerts.update_rule(rid, {"enabled": True})

    def test_nothing_on_reads_nothing(self):
        out = alerts.check(None, fetch=self.fetch, now=NOW)
        self.assertEqual(self.fetches, [])
        self.assertIn("No alert rules", out["message"])

    def test_fire_quiet_snooze_ack(self):
        self.on("r_spend_over_budget")
        self.on("r_disapproved_ads")
        out = alerts.check(None, fetch=self.fetch, now=NOW)
        self.assertEqual({x for x, _ in self.fetches}, {G["id"], M["id"]})
        texts = [f["text"] for f in out["fired"]]
        self.assertEqual(len(texts), 3, texts)  # Google: campaign 11 over budget, 1 disapproved ad; Meta: 1 disapproved ad
        self.assertEqual(len(self.mac), 1)  # one macOS notification per check
        self.assertTrue(self.mac[0][1].startswith("3 alerts: "))
        self.assertEqual(len(self.sent), 3)  # three or fewer: one team message each
        b = alerts.badge()
        self.assertEqual(b["unacked"], 3)
        self.assertEqual(alerts.history()[0]["delivered"], {"studio": "shown", "mac": "shown", "team": "sent"})
        # the quiet period: the same check an hour later fires nothing
        out = alerts.check(None, fetch=self.fetch, now=NOW + 3600)
        self.assertEqual((out["fired"], out["suppressed"]), ([], 3))
        # after the quiet period it fires again, unless snoozed
        h = alerts.history()
        g_over = next(x for x in h if x["metric"] == "spend_vs_budget")
        alerts.snooze(g_over["id"], 72)
        out = alerts.check(None, fetch=self.fetch, now=NOW + 25 * 3600)
        self.assertEqual(sorted(f["metric"] for f in out["fired"]), ["disapproved_ads", "disapproved_ads"])
        alerts.ack(every=True)
        self.assertEqual(alerts.badge()["unacked"], 0)
        alerts.snooze(g_over["id"], 0)  # wake it up
        out = alerts.check(None, fetch=self.fetch, now=NOW + 26 * 3600)
        self.assertEqual([f["metric"] for f in out["fired"]], ["spend_vs_budget"])

    def test_cadence_and_accounts(self):
        self.on("r_cpa_up_week")
        self.on("r_no_delivery")
        alerts.check("daily", fetch=self.fetch, now=NOW)
        self.assertTrue(all(n["metrics"] for _, n in self.fetches))
        self.fetches.clear()
        out = alerts.check("hourly", [G["id"]], fetch=self.fetch, now=NOW)
        self.assertEqual([x for x, _ in self.fetches], [G["id"]])
        self.assertEqual([f["object"]["name"] for f in out["fired"]], ["Silent B"])

    def test_meta_backoff(self):
        self.on("r_learning_limited")
        with mock.patch("alerts._meta_usage", return_value={"x-ad-account-usage": {"acc_id_util_pct": 80}}):
            out = alerts.check(None, fetch=self.fetch, now=NOW)
        self.assertEqual(self.fetches, [])
        self.assertIn("paused for 20 minutes", out["checked"][0]["skipped"])
        out = alerts.check(None, fetch=self.fetch, now=NOW + 600)
        self.assertIn("paused until", out["checked"][0]["skipped"])
        self.assertEqual(self.fetches, [])
        out = alerts.check(None, fetch=self.fetch, now=NOW + 1300)
        self.assertEqual(len(self.fetches), 1)

        class Limited(Exception):
            rate_limited = True

        def boom(a, need, tz_hint=None, now=None):
            raise Limited("(#17) User request limit reached access_token=SECRET123")
        st = alerts._state()
        st["last_fired"] = {}
        alerts._write(alerts.STATE_FILE, st)
        out = alerts.check(None, fetch=boom, now=NOW + 7200)
        self.assertNotIn("SECRET123", json.dumps(out))
        self.assertFalse(out["checked"][0]["ok"])
        st = alerts._state()
        self.assertGreaterEqual(st["meta_backoff"]["until"], NOW + 7200 + 3600)

    def test_busy_and_min_gap(self):
        self.on("r_no_delivery")
        alerts.check(None, fetch=self.fetch, now=NOW)
        n = len(self.fetches)
        out = alerts.check(None, fetch=self.fetch, now=NOW + 60, min_gap=300)
        self.assertEqual(len(self.fetches), n)
        self.assertTrue(all("checked 60s ago" in r["skipped"] for r in out["checked"]))
        with alerts._check_lock:
            self.assertTrue(alerts.check(None, fetch=self.fetch)["busy"])

    def test_many_alerts_one_team_message(self):
        alerts.add_rule({"metric": "impressions", "op": "above", "value": 0, "window": "yesterday", "level": "campaign", "quiet_hours": 1})
        alerts.add_rule({"metric": "clicks", "op": "above", "value": 0, "window": "yesterday", "level": "campaign", "quiet_hours": 1})
        alerts.add_rule({"metric": "spend", "op": "above", "value": 0, "window": "yesterday", "level": "campaign", "quiet_hours": 1})
        out = alerts.check(None, fetch=self.fetch, now=NOW)
        self.assertGreater(len(out["fired"]), 3)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("alerts:", self.sent[0][0])

    def test_settings_off(self):
        alerts.save_settings({"mac_notify": False, "team_notify": False})
        self.on("r_no_delivery")
        out = alerts.check(None, fetch=self.fetch, now=NOW)
        self.assertEqual((self.mac, self.sent), ([], []))
        self.assertEqual(out["fired"][0]["delivered"], {"studio": "shown", "mac": "off", "team": "off"})


class Wiring(unittest.TestCase):
    def setUp(self):
        reset()
        alerts.rules()

    def test_jobs_registered_and_held_in_manual(self):
        scheduler._reset_for_tests()
        alerts.register_jobs()
        rows = {j["name"]: j for j in scheduler.status()["jobs"]}
        self.assertEqual(rows["alerts_hourly"]["schedule"], "every 1 hour")
        self.assertTrue(rows["alerts_daily"]["schedule"].startswith("daily at 09:00"))
        self.assertTrue(rows["alerts_hourly"]["refresh"] and rows["alerts_hourly"]["held"])
        with mock.patch("alerts.check") as chk:
            started = scheduler.run_due(now=scheduler._jobs["alerts_hourly"]["next_run"] + 1)
        self.assertEqual(started, [])
        chk.assert_not_called()
        self.assertEqual(scheduler._jobs["alerts_hourly"]["last_status"], "held")
        (TMP / "settings.json").write_text(json.dumps({"data_policy": "auto"}))
        try:
            with mock.patch("alerts.check") as chk:
                started = scheduler.run_due(now=scheduler._jobs["alerts_hourly"]["next_run"] + 1)
                scheduler._jobs["alerts_hourly"]["worker"].join(5)
            self.assertIn("alerts_hourly", started)
            chk.assert_called_once_with("hourly", reason="hourly")
        finally:
            (TMP / "settings.json").unlink()
            scheduler._reset_for_tests()

    def test_after_refresh(self):
        with mock.patch("pacing.accounts", return_value=[G, M]), mock.patch("alerts.check") as chk:
            self.assertIsNone(alerts.after_refresh("google"))  # no rule on
            alerts.update_rule("r_learning_limited", {"enabled": True})
            self.assertIsNone(alerts.after_refresh("google"))  # a Meta-only rule
            self.assertIsNone(alerts.after_refresh("email"))
            self.assertEqual(alerts.after_refresh("meta", "meta_555", wait=True), ["meta_555"])
            chk.assert_called_once_with(None, ["meta_555"], reason="refresh", min_gap=alerts.MIN_GAP)
            alerts.update_rule("r_no_delivery", {"enabled": True})
            self.assertEqual(alerts.after_refresh("all", wait=True), [G["id"], M["id"]])

    def test_routes_and_mcp(self):
        import mcp_server  # noqa: PLC0415
        from areas import alerts as area  # noqa: PLC0415
        with mock.patch("pacing.accounts", return_value=[G, M]):
            v = area.g_view({})
            self.assertEqual(len(v["rules"]), len(alerts.STARTERS))
            self.assertEqual(v["policy"], "manual")
            r = mcp_server.call("alert_rule_add", {"metric": "roas", "op": "below", "value": 2, "window": "last_7_days", "account": "meta_555"})
            self.assertEqual((r["platform"], r["created_by"], r["enabled"]), ("meta", "claude", True))
            out = mcp_server.call("alerts_list", {})
            self.assertIn(r["id"], [x["id"] for x in out["rules"]])
            self.assertEqual(next(x for x in out["rules"] if x["id"] == r["id"])["cadence"], "daily")
            self.assertEqual(out["alerts"], [])
            names = {t["name"] for t in mcp_server.TOOLS}
            self.assertTrue({"alerts_list", "alert_rule_add"} <= names)
            with self.assertRaises(ValueError):
                mcp_server.call("alert_rule_add", {"metric": "spend"})
            area.p_rule({"id": r["id"], "enabled": False})
            self.assertFalse(next(x for x in alerts.rules() if x["id"] == r["id"])["enabled"])
            self.assertEqual(area.p_settings({"mac_notify": False})["mac_notify"], False)
            with self.assertRaises(ValueError):
                area.p_snooze({"id": "a_nope", "hours": 1})

    def test_osascript_args_not_script(self):
        with mock.patch("sys.platform", "darwin"), mock.patch.dict(os.environ, {"MSTUDIO_NO_MAC_NOTIFY": ""}), \
                mock.patch("subprocess.run") as run:
            self.assertEqual(alerts._osascript("Title", 'Odd "text" end run'), "shown")
        args = run.call_args[0][0]
        self.assertEqual(args[:3], ["osascript", "-e", alerts.MAC_SCRIPT])
        self.assertEqual(args[3:], ["Title", 'Odd "text" end run'])

    def test_ui_escapes(self):
        js = (HERE / "static" / "alerts.js").read_text()
        for raw in ("${x.text}", "${r.name}", "${x.rule_name}", "${a.name}"):
            self.assertNotIn(raw, js)
        for path in (HERE / "alerts.py", HERE / "areas" / "alerts.py", HERE / "static" / "alerts.js"):
            self.assertNotIn(chr(0x2014), path.read_text())


if __name__ == "__main__":
    unittest.main()
