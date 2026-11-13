"""Activity area tests. Run from the studio folder:  python3 -m unittest tests/test_activity.py

Offline by default: Google, Meta, WordPress and the logs are fixtures, and the archive lives in a temp folder.
Real reads (read-only) run only with ACTIVITY_REAL=1: Google change_event for ml (last 30 days) and Meta activities.
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("MSTUDIO_HOME", _tf.mkdtemp(prefix="mstudio-test-"))
_os.environ["MSTUDIO_KEYCHAIN"] = "0"

import csv
import datetime as dt
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

TMP = tempfile.mkdtemp(prefix="activity-test-")
os.environ["STUDIO_DATA_DIR"] = TMP  # datapolicy's status file, never the real one
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from areas import _activity_archive as archive  # noqa: E402
from areas import _activity_google as gch  # noqa: E402
from areas import _activity_local as loc  # noqa: E402
from areas import _activity_meta as mch  # noqa: E402
from areas import _activity_periods as per  # noqa: E402
from areas import activity as A  # noqa: E402

archive.DIR = TMP
LON = ZoneInfo("Europe/London")
UTC = dt.timezone.utc
MAC = dt.datetime.now().astimezone().tzinfo
EM = chr(0x2014)


def ce(i, when, rtype="AD_GROUP_CRITERION", op="UPDATE", old=None, new=None, fields="cpcBidMicros", client="GOOGLE_ADS_WEB_CLIENT",
       email="someone@masterliveaboards.com"):
    root = gch.ROOT[rtype]
    return {"campaign": {"name": "[16] - Search - NB - Tubbataha"}, "adGroup": {"name": "Tubbataha Diving"},
            "changeEvent": {"resourceName": f"customers/1/changeEvents/{i}~0~0", "changeDateTime": when, "changeResourceType": rtype,
                            "changeResourceName": f"customers/1/adGroupCriteria/5~{i}", "clientType": client, "userEmail": email,
                            "oldResource": {root: old or {}}, "newResource": {root: new or {}}, "resourceChangeOperation": op,
                            "changedFields": fields}}


class Periods(unittest.TestCase):
    def test_month_boundaries_london(self):
        now = dt.datetime(2026, 11, 10, 19, 0, tzinfo=LON)
        p = per.period("last_month", now=now)
        self.assertEqual(p["start"], dt.datetime(2026, 10, 1, tzinfo=LON))
        self.assertEqual(p["end"], dt.datetime(2026, 11, 1, tzinfo=LON))
        self.assertEqual(p["prev_start"], dt.datetime(2026, 9, 1, tzinfo=LON))
        self.assertEqual(p["prev_end"], p["start"])
        # October has the clock change: 1 Oct is BST (+1), 1 Nov is GMT
        self.assertEqual(p["start"].utcoffset(), dt.timedelta(hours=1))
        self.assertEqual(p["end"].utcoffset(), dt.timedelta(0))
        self.assertEqual(len(per.days(p["start"], p["end"], "Europe/London")), 31)

    def test_this_month_compares_same_stretch(self):
        now = dt.datetime(2026, 11, 10, 19, 0, tzinfo=LON)
        p = per.period("this_month", now=now)
        self.assertEqual(p["start"], dt.datetime(2026, 11, 1, tzinfo=LON))
        self.assertEqual(p["prev_start"], dt.datetime(2026, 10, 1, tzinfo=LON))
        self.assertEqual(p["prev_end"], dt.datetime(2026, 10, 10, 19, 0, tzinfo=LON))
        # 31 March vs February: the previous stretch is capped at the end of February
        p = per.period("this_month", now=dt.datetime(2027, 3, 31, 12, 0, tzinfo=LON))
        self.assertEqual(p["prev_end"], dt.datetime(2027, 3, 1, tzinfo=LON))

    def test_weeks_start_monday(self):
        now = dt.datetime(2026, 11, 10, 9, 0, tzinfo=LON)  # a Tuesday
        p = per.period("this_week", now=now)
        self.assertEqual(p["start"], dt.datetime(2026, 11, 9, tzinfo=LON))
        p = per.period("last_week", now=now)
        self.assertEqual((p["start"], p["end"]), (dt.datetime(2026, 11, 2, tzinfo=LON), dt.datetime(2026, 11, 9, tzinfo=LON)))
        self.assertEqual(p["prev_start"], dt.datetime(2026, 10, 26, tzinfo=LON))

    def test_quarter_and_year_turn(self):
        p = per.period("this_quarter", now=dt.datetime(2026, 11, 10, tzinfo=LON))
        self.assertEqual(p["start"], dt.datetime(2026, 10, 1, tzinfo=LON))
        self.assertEqual(p["prev_start"], dt.datetime(2026, 7, 1, tzinfo=LON))
        p = per.period("this_quarter", now=dt.datetime(2027, 2, 1, tzinfo=LON))
        self.assertEqual(p["prev_start"], dt.datetime(2026, 10, 1, tzinfo=LON))
        p = per.period("last_month", now=dt.datetime(2027, 1, 5, tzinfo=LON))
        self.assertEqual(p["start"], dt.datetime(2026, 12, 1, tzinfo=LON))
        self.assertEqual(p["prev_start"], dt.datetime(2026, 11, 1, tzinfo=LON))

    def test_rolling_and_custom(self):
        now = dt.datetime(2026, 11, 10, 15, 0, tzinfo=LON)
        p = per.period("last_7", now=now)
        self.assertEqual(p["start"], dt.datetime(2026, 11, 4, tzinfo=LON))
        self.assertEqual(p["prev_start"], dt.datetime(2026, 10, 28, tzinfo=LON))
        self.assertEqual(p["prev_end"], dt.datetime(2026, 11, 3, 15, 0, tzinfo=LON))
        p = per.period("custom", now=now, start="2026-10-20", end="2026-10-26")
        self.assertEqual((p["start"], p["end"]), (dt.datetime(2026, 10, 20, tzinfo=LON), dt.datetime(2026, 10, 27, tzinfo=LON)))
        self.assertEqual(p["prev_start"], dt.datetime(2026, 10, 13, tzinfo=LON))
        with self.assertRaises(ValueError):
            per.period("custom", now=now)

    def test_mac_zone(self):
        now = dt.datetime(2026, 11, 10, 1, 0, tzinfo=UTC)
        p = per.period("last_month", now=now, tz=MAC)
        self.assertEqual(p["start"].tzinfo, MAC)
        self.assertEqual((p["start"].day, p["start"].month, p["start"].hour), (1, 10, 0))
        bkk = per.period("this_week", now=now, tz="Asia/Bangkok")  # 08:00 on Tuesday 10 November in Bangkok
        self.assertEqual(bkk["start"], dt.datetime(2026, 11, 9, tzinfo=ZoneInfo("Asia/Bangkok")))
        # a change at 23:30 UTC on 31 October is 31 October in London but 1 November in Bangkok
        self.assertEqual(per.day_of("2026-10-31T23:30:00Z", "Europe/London"), "2026-10-31")
        self.assertEqual(per.day_of("2026-10-31T23:30:00Z", "Asia/Bangkok"), "2026-11-01")

    def test_parse_formats(self):
        self.assertEqual(per.parse("2026-11-08T15:53:01+0000"), dt.datetime(2026, 11, 8, 15, 53, 1, tzinfo=UTC))
        self.assertEqual(per.parse("2026-11-10 06:34:18.768239", assume=UTC).microsecond, 768239)
        self.assertEqual(per.parse("2026-11-10 06:34:18.7", assume=UTC).microsecond, 700000)


class GoogleMapping(unittest.TestCase):
    def test_bid_update(self):
        kw = {"text": "tubbataha", "matchType": "PHRASE"}
        r = gch.map_row(ce(1, "2026-11-10 06:34:17.988627", old={"cpcBidMicros": "600000", "keyword": kw},
                           new={"cpcBidMicros": "1500000", "keyword": kw}), "8931136258", "ML", UTC)
        self.assertEqual(r["type"], "Keyword changed")
        self.assertEqual(r["object"], "tubbataha [phrase]")
        self.assertEqual((r["old"], r["new"], r["fields"]), ("0.60", "1.50", "max CPC"))
        self.assertEqual(r["by"], "Google Ads web")
        self.assertEqual(r["who"], "someone@masterliveaboards.com")
        self.assertEqual(r["ts"], "2026-11-10T06:34:17Z")
        self.assertEqual(r["where"], "[16] - Search - NB - Tubbataha > Tubbataha Diving")

    def test_account_time_zone(self):
        r = gch.map_row(ce(2, "2026-07-01 10:00:00"), "1", "ML", LON)
        self.assertEqual(r["ts"], "2026-07-01T09:00:00Z")

    def test_create_remove_status(self):
        kw = {"keyword": {"text": "deep blue galapagos", "matchType": "EXACT"}, "negative": True, "status": "ENABLED"}
        r = gch.map_row(ce(3, "2026-11-01 10:00:00", "CAMPAIGN_CRITERION", "CREATE", new=kw, fields="keyword.text,negative", client="GOOGLE_ADS_API"), "1", "ML", UTC)
        self.assertEqual(r["type"], "Negative keyword (campaign) added")
        self.assertEqual(r["by"], "API")
        r = gch.map_row(ce(4, "2026-11-01 10:00:00", "CAMPAIGN", "UPDATE", old={"status": "PAUSED"}, new={"status": "REMOVED"}, fields="status"), "1", "ML", UTC)
        self.assertEqual((r["type"], r["old"], r["new"]), ("Campaign removed", "Paused", "Removed"))
        r = gch.map_row(ce(5, "2026-11-01 10:00:00", "CAMPAIGN", "UPDATE", old={"status": "PAUSED", "name": "A"}, new={"status": "ENABLED", "name": "A"},
                           fields="status", client="OTHER"), "1", "ML", UTC)
        self.assertEqual((r["type"], r["object"]), ("Campaign enabled", "A"))
        self.assertIn("Editor", r["by"])
        r = gch.map_row(ce(6, "2026-11-01 10:00:00", "CAMPAIGN_BUDGET", "UPDATE", old={"amountMicros": "45000000"}, new={"amountMicros": "80000000"},
                           fields="amountMicros"), "1", "ML", UTC)
        self.assertEqual((r["type"], r["old"], r["new"]), ("Budget changed", "45.00", "80.00"))
        r = gch.map_row(ce(7, "2026-11-01 10:00:00", "CAMPAIGN_CRITERION", "CREATE", new={"criterionId": "2826", "status": "ENABLED"},
                           fields="criterionId,status"), "1", "ML", UTC)
        self.assertEqual(r["type"], "Campaign targeting: location added")

    def test_paging_past_cap(self):
        calls = []
        base = dt.datetime(2026, 11, 10, 12, 0, tzinfo=UTC)

        def query(cid, q):
            calls.append(q)
            if len(calls) == 1:  # the cap: 3 rows, the last two at the same second, newest first
                return [ce(10, "2026-11-10 11:00:00"), ce(11, "2026-11-10 10:00:00"), ce(12, "2026-11-10 10:00:00")]
            return [ce(12, "2026-11-10 10:00:00"), ce(13, "2026-11-10 09:00:00")]
        old = gch.MAX_ROWS
        gch.MAX_ROWS = 3
        try:
            rows = gch.fetch("1", base - dt.timedelta(days=1), base, UTC, query)
        finally:
            gch.MAX_ROWS = old
        self.assertEqual([r["changeEvent"]["resourceName"].split("/")[-1] for r in rows], ["10~0~0", "11~0~0", "12~0~0", "13~0~0"])
        self.assertIn("<= '2026-11-10 10:00:00'", calls[1])
        self.assertIn("LIMIT 3", calls[0])

    def test_clamp_30_days(self):
        now = dt.datetime(2026, 11, 10, tzinfo=UTC)
        s, e = gch.clamp(now - dt.timedelta(days=90), now + dt.timedelta(days=1), now)
        self.assertGreater(s, now - dt.timedelta(days=30))
        self.assertEqual(e, now)
        self.assertIsNone(gch.clamp(now - dt.timedelta(days=90), now - dt.timedelta(days=60), now))


class MetaMapping(unittest.TestCase):
    def test_status_and_budget(self):
        x = {"actor_id": "123", "actor_name": "A Person", "application_name": "Power Editor", "event_time": "2026-11-09T08:21:36+0000",
             "event_type": "update_ad_set_run_status", "extra_data": json.dumps({"old_value": "Active", "new_value": "Paused", "type": "run_status"}),
             "object_id": "999", "object_name": "Tubbataha - Prospecting", "object_type": "CAMPAIGN", "translated_event_type": "Ad set status updated"}
        r = mch.map_row(x, "act_644", "ML Meta")
        self.assertEqual((r["type"], r["object_type"], r["old"], r["new"]), ("Ad set status changed", "Ad set", "Active", "Paused"))
        self.assertEqual((r["by"], r["who"], r["by_kind"]), ("Ads Manager", "A Person", "web"))
        self.assertIn("selected_adset_ids=999", r["link"])
        self.assertFalse(r["system"])
        x.update(event_type="update_ad_set_budget", application_name="ML API Access",
                 extra_data=json.dumps({"old_value": 5000, "new_value": 8000, "currency": "USD"}))
        r = mch.map_row(x, "act_644", "ML Meta")
        self.assertEqual((r["old"], r["new"], r["by_kind"]), ("50.00 USD", "80.00 USD", "api"))

    def test_system_and_unknown(self):
        x = {"actor_id": "0", "event_time": "2026-11-10T00:08:25+0000", "event_type": "update_ad_run_status", "object_type": "ADGROUP",
             "object_id": "1", "object_name": "P10", "extra_data": "{\"old_value\":\"Pending Review\",\"new_value\":\"Active\"}"}
        r = mch.map_row(x, "act_1", "ML Meta")
        self.assertTrue(r["system"])
        self.assertEqual((r["by"], r["who"]), ("Meta (automatic)", ""))
        self.assertFalse(A.counted(r))
        x.update(event_type="brand_new_event", translated_event_type="something new happened", actor_id="5", application_name="X")
        self.assertEqual(mch.map_row(x, "act_1", "ML Meta")["type"], "Something new happened")
        x.update(extra_data="not json")
        self.assertEqual(mch.map_row(x, "act_1", "ML Meta")["old"], "")

    def test_targeting_text(self):
        o, n = mch.old_new("update_ad_set_target_spec", json.dumps({"new_value": [{"content": "Location:", "children": ["Australia", "Canada"]}]}))
        self.assertEqual(n, "Location: Australia, Canada")


class Archive(unittest.TestCase):
    def setUp(self):
        archive.DIR = tempfile.mkdtemp(dir=TMP)

    def test_dedupe_and_coverage(self):
        t0 = dt.datetime(2026, 10, 1, tzinfo=UTC)
        rows = [{"id": "g:a", "ts": "2026-10-02T10:00:00Z", "x": 1}, {"id": "g:b", "ts": "2026-10-03T10:00:00Z"}]
        self.assertEqual(archive.save("google", "1", rows, t0, t0 + dt.timedelta(days=5)), 2)
        self.assertEqual(archive.save("google", "1", rows + [{"id": "g:c", "ts": "2026-10-06T10:00:00Z"}], t0 + dt.timedelta(days=4),
                                      t0 + dt.timedelta(days=6)), 1)
        self.assertEqual(len(archive.rows("google", t0, t0 + dt.timedelta(days=10))), 3)
        self.assertEqual(archive.rows("google", t0, t0 + dt.timedelta(days=10))[-1]["x"], 1)  # the first copy is kept
        cov = archive.coverage("google")
        self.assertEqual((cov["from"], cov["to"], cov["gaps"], cov["events"]), ("2026-10-01T00:00:00Z", "2026-10-07T00:00:00Z", [], 3))
        archive.save("google", "1", [], t0 + dt.timedelta(days=20), t0 + dt.timedelta(days=21))
        self.assertEqual(archive.coverage("google")["gaps"], [["2026-10-07T00:00:00Z", "2026-10-21T00:00:00Z"]])
        self.assertEqual(archive.last_fetch("google", "1"), t0 + dt.timedelta(days=21))
        self.assertEqual(archive.prune(t0 + dt.timedelta(days=3)), 2)
        self.assertTrue(archive.writable()[0])

    def test_sync_is_incremental(self):
        now = dt.datetime.now(UTC).replace(microsecond=0)
        seen = []

        def query(cid, q):
            if "customer.time_zone" in q:
                return [{"customer": {"timeZone": "Etc/GMT"}}]
            seen.append(q)
            return [ce(1, (now - dt.timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"))]
        acc = [{"id": "77", "name": "Test"}]
        A._synced.clear()
        r1 = A.sync_google(force=True, query=query, accounts=acc, now=now)
        r2 = A.sync_google(force=True, query=query, accounts=acc, now=now + dt.timedelta(hours=1))
        self.assertEqual((r1[0]["new"], r2[0]["new"]), (1, 0))
        self.assertIn((now - dt.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M"), seen[1])  # from the last read minus an hour
        self.assertEqual(A.sync_google(query=query, accounts=acc), [])  # within 15 minutes: no new read
        stored = archive.rows("google", now - dt.timedelta(days=1), now + dt.timedelta(days=1))
        self.assertIn("raw", stored[0])
        self.assertNotIn("raw", A.remap(stored[0]))
        self.assertEqual(A.remap(stored[0])["type"], "Ad group targeting changed")


class LocalSources(unittest.TestCase):
    def test_studio_logs(self):
        d = Path(tempfile.mkdtemp(dir=TMP))
        (d / "live_meta_644.jsonl").write_text(json.dumps({"id": "p1", "ts": "2026-11-10T17:27:15", "ok": True,
                                                           "items": [{"summary": "Meta ad X: paused", "by": "claude"}],
                                                           "ops": [{"line": "ad X: status Active -> Paused"}]}) + "\n" +
                                                json.dumps({"id": "p1-undo", "ts": "2026-11-10T17:32:19", "ok": True, "undo_of": "p1", "items": [], "ops": []}) + "\nnot json\n")
        (d / "pushed_8931136258.jsonl").write_text(json.dumps({"ts": "2026-11-09T10:00:00", "plan": "plan-1.json", "items": [{"summary": "kw", "by": "you"}]}) + "\n")
        (d / "seo_history.jsonl").write_text(json.dumps({"push_id": "s1", "at": "2026-11-09 11:00:00", "action": "publish", "target": "prod",
                                                         "post_id": 5, "title": "Mantas", "url": "https://x/", "after": {"meta_desc": "d"}}) + "\n")
        (d / "seo_dryrun.jsonl").write_text(json.dumps({"at": "2026-11-09 11:05:00", "action": "revision", "post_id": 5}) + "\n")
        (d / "social_draft.json").write_text(json.dumps({"posts": [{"id": "x", "title": "Post", "by": "you", "results": {
            "facebook": {"status": "published", "at": "2026-11-09T12:00:00+07:00", "url": "https://fb/x"}}}]}))
        rows = loc.studio_rows(d, dt.datetime(2026, 11, 1, tzinfo=UTC), dt.datetime(2026, 11, 11, tzinfo=UTC), {"meta_644": "ML Meta"})
        types = sorted(r["type"] for r in rows)
        self.assertEqual(types, ["Push live", "SEO publish", "SEO revision (dry run, nothing sent)", "Sent to Editor", "SoMe post published", "Undo"])
        push = next(r for r in rows if r["type"] == "Push live")
        self.assertEqual((push["account"], push["by"], push["who"]), ("ML Meta", "Studio (claude)", "claude"))
        self.assertFalse(next(r for r in rows if "dry run" in r["type"])["count"])

    def test_email_demo_never_counted(self):
        class Fake:
            demo = True

            def list_campaigns(self, status=None, limit=None):
                return [{"id": 1, "name": "Oct", "sent_at": "2026-10-17T09:00:00Z", "stats": {"sent": 100}}]
        rows, demo = loc.email_rows(dt.datetime(2026, 10, 1, tzinfo=UTC), dt.datetime(2026, 11, 1, tzinfo=UTC), Fake())
        self.assertTrue(demo and rows[0]["demo"])
        self.assertFalse(A.counted(rows[0]))
        Fake.demo = False
        rows, demo = loc.email_rows(dt.datetime(2026, 10, 1, tzinfo=UTC), dt.datetime(2026, 11, 1, tzinfo=UTC), Fake())
        self.assertTrue(A.counted(rows[0]))
        self.assertEqual(rows[0]["new"], "100 emails")

    def test_blog_published_and_edited(self):
        posts = [{"id": 1, "date_gmt": "2026-10-15T04:15:45", "modified_gmt": "2026-10-15T04:20:00", "title": {"rendered": "A &amp; B"}, "link": "u1"},
                 {"id": 2, "date_gmt": "2026-01-01T10:00:00", "modified_gmt": "2026-10-20T10:00:00", "title": {"rendered": "Old"}, "link": "u2"}]
        rows = loc.blog_rows(posts, dt.datetime(2026, 10, 1, tzinfo=UTC), dt.datetime(2026, 11, 1, tzinfo=UTC))
        self.assertEqual(sorted((r["type"], r["object"]) for r in rows), [("Post edited", "Old"), ("Post published", "A & B")])

    def test_blog_fetch_params(self):
        urls = []

        def http(url):
            urls.append(url)
            return 200, {"X-WP-TotalPages": "1"}, [{"id": 1, "date_gmt": "2026-10-15T04:15:45", "modified_gmt": "2026-10-15T04:15:45"}]
        out = loc.blog_fetch(dt.datetime(2026, 10, 1, tzinfo=UTC), dt.datetime(2026, 11, 1, tzinfo=UTC), http)
        self.assertEqual(len(out), 1)
        self.assertTrue(any("after=2026-09-30" in u and "before=2026-11-02" in u for u in urls))
        self.assertTrue(any("modified_after=" in u for u in urls))


def sample_rows():
    return [
        {"id": "1", "ts": "2026-10-02T10:00:00Z", "channel": "google", "account": "ML", "type": "Keyword added", "object": "tubbataha",
         "where": "", "old": "", "new": "Enabled", "by": "Google Ads web", "who": "staff@masterliveaboards.com", "link": ""},
        {"id": "2", "ts": "2026-10-03T10:00:00Z", "channel": "meta", "account": "ML Meta", "type": "Ad status changed", "object": "Ad, with comma",
         "where": "", "old": "Active", "new": "Paused", "by": "Ads Manager", "who": "A Person", "link": "https://x"},
        {"id": "3", "ts": "2026-10-04T10:00:00Z", "channel": "email", "account": "ML Suite (demo data)", "type": "Campaign sent", "object": "Oct",
         "by": "Demo data", "who": "", "demo": True, "count": False},
    ]


class CombineAndCsv(unittest.TestCase):
    def test_filters(self):
        rows = sample_rows()
        self.assertEqual([r["id"] for r in A.apply_filters(rows)], ["3", "2", "1"])
        self.assertEqual([r["id"] for r in A.apply_filters(rows, {"channel": "google,meta", "sort": "ts", "desc": "0"})], ["1", "2"])
        self.assertEqual([r["id"] for r in A.apply_filters(rows, {"q": "PAUSED"})], ["2"])
        self.assertEqual([r["id"] for r in A.apply_filters(rows, {"by": "Ads Manager"})], ["2"])

    def test_csv_without_and_with_who(self):
        rows = sample_rows()
        plain = A.to_csv(rows)
        self.assertNotIn("staff@masterliveaboards.com", plain)
        self.assertNotIn("A Person", plain)
        head = next(csv.reader(io.StringIO(plain)))
        self.assertNotIn("Who", head)
        full = A.to_csv(rows, include_who=True)
        self.assertIn("staff@masterliveaboards.com", full)
        parsed = list(csv.DictReader(io.StringIO(full)))
        self.assertEqual(parsed[1]["Object"], "Ad, with comma")
        self.assertEqual(parsed[0]["Time (UK)"], "2026-10-02 11:00")  # BST
        self.assertIn("demo", parsed[2]["Account"])
        self.assertNotIn(EM, full)

    def test_summarise_combines_sources(self):
        p = per.period("custom", start="2026-10-01", end="2026-10-31")
        rows = sample_rows() + [{"id": "4", "ts": "2026-09-20T10:00:00Z", "channel": "google", "type": "x", "account": "ML", "by": "API"},
                                {"id": "5", "ts": "2026-10-05T10:00:00Z", "channel": "blog", "type": "Post published", "account": "wp", "by": "WordPress"},
                                {"id": "6", "ts": "2026-10-05T11:00:00Z", "channel": "meta", "type": "First delivery", "system": True, "account": "ML Meta", "by": "Meta (automatic)"}]
        res = {}
        for s in A.SOURCES:
            mine = [r for r in rows if r["channel"] == s]
            cur, prev = A.split(mine, p)
            res[s] = {"status": "ok", "rows": cur, "count": sum(map(A.counted, cur)), "prev_count": sum(map(A.counted, prev)),
                      "by_tile": A._by_tile(cur), "prev_by_tile": A._by_tile(prev)}
        s = A.summarise(res, p)
        t = {x["key"]: (x["v"], x["prev"]) for x in s["tiles"]}
        self.assertEqual(t["google"], (1, 1))
        self.assertEqual(t["meta"], (1, 0))  # the automatic event is listed but not counted
        self.assertEqual(t["email"], (0, 0))  # demo
        self.assertEqual(t["blog_pub"], (1, 0))
        self.assertEqual(len(s["days"]), 31)
        self.assertEqual(sum(d["n"] for d in s["days"]), 3)

    def test_studio_push_tag(self):
        rows = [{"channel": "google", "account_id": "1", "ts": "2026-11-10T10:01:00Z", "by": "API", "by_kind": "GOOGLE_ADS_API"},
                {"channel": "google", "account_id": "1", "ts": "2026-11-10T12:00:00Z", "by": "API", "by_kind": "GOOGLE_ADS_API"}]
        A.tag_studio_pushes(rows, [{"object_type": "Google Ads push", "account_id": "1", "ts": "2026-11-10T10:00:00Z"}])
        self.assertEqual([r["by"] for r in rows], ["API (Studio push)", "API"])

    def test_no_em_dash_in_area_text(self):
        root = Path(__file__).resolve().parents[1]
        for f in ["areas/activity.py", "areas/_activity_google.py", "areas/_activity_meta.py", "areas/_activity_local.py",
                  "areas/_activity_periods.py", "areas/_activity_archive.py", "static/activity.js"]:
            self.assertNotIn(EM, (root / f).read_text(), f)

    def test_no_write_actions(self):
        self.assertEqual(A.POST, {})


@unittest.skipUnless(os.environ.get("ACTIVITY_REAL") == "1", "real reads only with ACTIVITY_REAL=1")
class RealReads(unittest.TestCase):
    def test_google_change_event_ml(self):
        import live
        now = dt.datetime.now(UTC)
        s, e = gch.clamp(now - dt.timedelta(days=30), now, now)
        tz = gch.account_tz("8931136258", live.query)
        rows = [gch.map_row(r, "8931136258", "ML", tz) for r in gch.fetch("8931136258", s, e, tz, live.query)]
        print(f"\nGoogle change_event, ml, last 30 days: {len(rows)} rows")
        self.assertGreater(len(rows), 0)

    def test_meta_activities(self):
        import meta_ads
        now = dt.datetime.now(UTC)
        raw = mch.fetch(meta_ads.DEFAULT_ACT, now - dt.timedelta(days=30), now, meta_ads.get_all)
        print(f"\nMeta activities, last 30 days: {len(raw)} rows")
        self.assertGreater(len(raw), 0)


if __name__ == "__main__":
    unittest.main()
