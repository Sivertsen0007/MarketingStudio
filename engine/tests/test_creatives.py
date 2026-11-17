"""Creative and asset performance (creatives.py, areas/creatives.py, the Refresh menu source, the MCP tool). Run from the
engine folder:
    python3 tests/test_creatives.py

Offline: Google and Meta reads are replaced with fixed answers (fakes); nothing is sent anywhere. Runs in a temp
MSTUDIO_HOME with MSTUDIO_KEYCHAIN=0, so the real data folder and the Keychain are never touched.
"""
import os as _os, tempfile as _tf
_os.environ["MSTUDIO_HOME"] = _tf.mkdtemp(prefix="mstudio-creatives-test-")
_os.environ["MSTUDIO_KEYCHAIN"] = "0"

import datetime as dt
import json
import os
import sys
import tempfile
import unittest
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
TMP = Path(tempfile.mkdtemp(prefix="creatives-test-"))
os.environ["STUDIO_DATA_DIR"] = str(TMP)

import core  # noqa: E402

core.STUDIO_DIR = TMP
import creatives  # noqa: E402
import datapolicy  # noqa: E402
import meta_ads as meta  # noqa: E402

G_ACCT = {"id": "1234567890", "key": "1234567890", "name": "Google test", "platform": "google"}
M_ACCT = {"id": "meta_555", "key": "meta_555", "name": "Meta test", "platform": "meta", "act": "act_555"}
EVIL = '<img src=x onerror="alert(1)">'


def _real_urlopen(*a, **k):
    raise AssertionError("a test tried to reach the network")


urllib.request.urlopen = _real_urlopen  # nothing in these tests may call out


# ---------------------------------------------------------------- Google fakes

def g_row(ad, field, text=None, imp=0, clk=0, cost=0.0, conv=0.0, val=0.0, kind="TEXT", asset="1", channel="SEARCH",
          ad_type="RESPONSIVE_SEARCH_AD", status="ENABLED", ag=("10", "Group A"), camp=("100", "Search A"), url="", yt="", enabled=True):
    a = {"id": asset, "type": kind}
    if kind == "TEXT":
        a["textAsset"] = {"text": text}
    elif kind == "IMAGE":
        a["imageAsset"] = {"fullSize": {"url": url}}
        a["name"] = text or ""
    elif kind == "YOUTUBE_VIDEO":
        a["youtubeVideoAsset"] = {"youtubeVideoId": yt, "youtubeVideoTitle": text}
    return {"customer": {"currencyCode": "GBP"}, "campaign": {"id": camp[0], "name": camp[1], "status": "ENABLED", "advertisingChannelType": channel},
            "adGroup": {"id": ag[0], "name": ag[1]}, "adGroupAd": {"status": status, "ad": {"id": ad, "type": ad_type}},
            "adGroupAdAssetView": {"fieldType": field, "enabled": enabled, "performanceLabel": "PENDING"}, "asset": a,
            "metrics": {"impressions": str(imp), "clicks": str(clk), "costMicros": str(int(cost * 1e6)), "conversions": conv,
                        "conversionsValue": val, "videoTrueviewViews": "0"}}


def google_rows():
    return [
        g_row("1", "HEADLINE", "Dive the reef", 1000, 50, 25.0, 2, 300.0, asset="11"),
        g_row("2", "HEADLINE", "Dive  the reef ", 600, 10, 5.0, 0, 0, asset="12", ag=("20", "Group B")),  # same text, other spacing
        g_row("1", "HEADLINE", "Dive the  reef", 0, 0, 0.0, asset="15"),  # the same text twice in one ad: one change for it
        g_row("1", "HEADLINE", "Book today", 100, 1, 1.0, asset="13"),
        g_row("1", "DESCRIPTION", EVIL, 900, 20, 9.0, 1, 0, asset="14"),
        g_row("3", "MARKETING_IMAGE", "photo.jpg", 4000, 40, 20.0, kind="IMAGE", asset="21", channel="DEMAND_GEN",
              ad_type="DEMAND_GEN_MULTI_ASSET_AD", url="https://img.example/1", camp=("200", "Demand Gen A"), ag=("30", "DG")),
        g_row("3", "YOUTUBE_VIDEO", "Our film", 700, 7, 3.5, kind="YOUTUBE_VIDEO", asset="22", channel="DEMAND_GEN",
              ad_type="DEMAND_GEN_MULTI_ASSET_AD", yt="abcDEF12345", camp=("200", "Demand Gen A"), ag=("30", "DG")),
    ]


def pmax_rows():
    return [{"customer": {"currencyCode": "GBP"}, "campaign": {"id": "300", "name": "PMax A", "status": "ENABLED"},
             "assetGroup": {"id": "40", "name": "AG one", "status": "ENABLED"},
             "assetGroupAsset": {"fieldType": "HEADLINE", "status": "ENABLED"}, "asset": {"id": "51", "type": "TEXT", "textAsset": {"text": "Sail away"}},
             "metrics": {"impressions": "2000", "clicks": "30", "costMicros": "12000000", "conversions": 3, "conversionsValue": 0}},
            {"customer": {"currencyCode": "GBP"}, "campaign": {"id": "300", "name": "PMax A", "status": "ENABLED"},
             "assetGroup": {"id": "40", "name": "AG one", "status": "ENABLED"},
             "assetGroupAsset": {"fieldType": "MARKETING_IMAGE", "status": "ENABLED"},
             "asset": {"id": "52", "type": "IMAGE", "imageAsset": {"fullSize": {"url": "javascript:alert(1)"}}},
             "metrics": {"impressions": "300", "clicks": "3", "costMicros": "1000000", "conversions": 0, "conversionsValue": 0}}]


RSA_LIVE = {"1": {"headlines": [{"text": "Dive the reef", "pinnedField": "HEADLINE_1"}, {"text": "Book today"}, {"text": "Small groups"}],
                  "descriptions": [{"text": EVIL}, {"text": "Second description"}]},
            "2": {"headlines": [{"text": "Dive the reef"}, {"text": "Other one"}, {"text": "Third"}, {"text": "Fourth"}],
                  "descriptions": [{"text": "D1"}, {"text": "D2"}]}}


PMAX_COUNT = {"n": 5, "enabled": 5}  # headlines in asset group 40 now (the counts read before a PMax text change)


def fake_google(log):
    def query(cid, gaql):
        log.append(gaql)
        assert gaql.lstrip().startswith("SELECT")
        if "asset_group_asset.source" in gaql:
            assert "asset_group.id IN (40)" in gaql and "field_type = HEADLINE" in gaql, gaql
            return [{"assetGroup": {"id": "40"}, "assetGroupAsset": {"status": "ENABLED" if i < PMAX_COUNT["enabled"] else "PAUSED",
                                                                       "source": "ADVERTISER"}} for i in range(PMAX_COUNT["n"])] \
                + [{"assetGroup": {"id": "40"}, "assetGroupAsset": {"status": "ENABLED", "source": "AUTOMATICALLY_CREATED"}}]
        if "FROM customer" in gaql:
            return [{"customer": {"timeZone": "Pacific/Auckland"}}]
        if "FROM ad_group_ad_asset_view" in gaql:
            return google_rows()
        if "FROM asset_group_asset" in gaql:
            return pmax_rows()
        if "FROM ad_group_ad WHERE" in gaql:
            ids = gaql.split("IN (")[1].split(")")[0].replace(" ", "").split(",")
            return [{"adGroupAd": {"ad": {"id": i, "responsiveSearchAd": RSA_LIVE[i]}}} for i in ids if i in RSA_LIVE]
        raise AssertionError(gaql)
    return query


# ---------------------------------------------------------------- Meta fakes

def creative_feed(bodies, titles):
    return {"id": "cr1", "asset_feed_spec": {"bodies": [{"text": b} for b in bodies], "titles": [{"text": t} for t in titles],
                                             "descriptions": [], "optimization_type": "DEGREES_OF_FREEDOM"},
            "object_story_spec": {"page_id": "1"}, "thumbnail_url": "https://thumb.example/f.jpg"}


META_ADS = [
    {"id": "a1", "name": "Flexible one", "campaign_id": "c1", "adset_id": "s1", "effective_status": "ACTIVE",
     "campaign": {"name": "Camp one"}, "adset": {"name": "Set one"}, "creative": creative_feed(["Body A", "Body B"], ["Title A"])},
    {"id": "a2", "name": "Flexible two", "campaign_id": "c1", "adset_id": "s1", "effective_status": "PAUSED",
     "campaign": {"name": "Camp one"}, "adset": {"name": "Set one"}, "creative": creative_feed(["Body A"], ["Title B"])},
    {"id": "a3", "name": "Single image", "campaign_id": "c2", "adset_id": "s2", "effective_status": "ACTIVE",
     "campaign": {"name": "Camp two"}, "adset": {"name": "Set two"},
     "creative": {"id": "cr3", "thumbnail_url": "https://thumb.example/3.jpg",
                  "object_story_spec": {"page_id": "1", "link_data": {"message": EVIL, "name": "Head 3", "link": ""}}}},
    {"id": "a4", "name": "No delivery", "campaign_id": "c2", "adset_id": "s2", "effective_status": "ACTIVE",
     "creative": creative_feed(["Never shown"], ["T"])},
]


def ins(ad, imp, clk, spend, results=0, video=False):
    r = {"ad_id": ad, "account_currency": "USD", "impressions": str(imp), "clicks": str(clk), "spend": str(spend),
         "actions": [{"action_type": "lead", "value": str(results)}, {"action_type": "link_click", "value": str(clk)}] if results else
         [{"action_type": "link_click", "value": str(clk)}],
         "action_values": [{"action_type": "offsite_conversion.fb_pixel_purchase", "value": "50"}] if results else []}
    if video:
        for k, v in (("video_play_actions", 100), ("video_thruplay_watched_actions", 20), ("video_p25_watched_actions", 60),
                     ("video_p50_watched_actions", 40), ("video_p75_watched_actions", 25), ("video_p100_watched_actions", 10),
                     ("video_avg_time_watched_actions", 6)):
            r[k] = [{"action_type": "video_view", "value": str(v)}]
    return r


BD = {"body_asset": [dict(ins("a1", 3000, 60, 30.0, 2), body_asset={"text": "Body A", "id": "b1"}),
                     dict(ins("a1", 1000, 10, 10.0), body_asset={"text": "Body B", "id": "b2"}),
                     dict(ins("a2", 500, 5, 4.0), body_asset={"text": "Body A", "id": "b3"})],
      "title_asset": [dict(ins("a1", 4000, 70, 40.0, 2), title_asset={"text": "Title A", "id": "t1"})],
      "image_asset": [dict(ins("a1", 4000, 70, 40.0, 2), image_asset={"hash": "h1", "url": "https://img.example/h1", "id": "i1"})],
      "video_asset": [dict(ins("a2", 500, 5, 4.0, video=True), video_asset={"video_id": "v9", "thumbnail_url": "https://thumb.example/v9.jpg", "id": "vv"})]}


class FakeMeta:
    """Answers the GETs fetch_meta makes, and records them."""

    def __init__(self, fail_on=None, page_split=False):
        self.calls = []
        self.fail_on = fail_on
        self.page_split = page_split

    def __call__(self, path, **params):
        self.calls.append((path, params))
        if path.startswith("https://"):  # a next page
            return {"data": [ins("a3", 2000, 30, 20.0, 1, video=True)]}
        if path == "act_555/insights" and "breakdowns" not in params:
            assert params["level"] == "ad"
            rows = [ins("a1", 4000, 70, 40.0, 2), ins("a2", 500, 5, 4.0, video=True)]
            if self.page_split:
                return {"data": rows, "paging": {"next": "https://graph.example/next"}}
            return {"data": rows + [ins("a3", 2000, 30, 20.0, 1, video=True)]}
        if path == "act_555/ads":
            f = params["filtering"][0]
            assert (f["field"], f["operator"]) == ("id", "IN")
            assert "a4" not in f["value"]  # only ads that delivered
            return {"data": [a for a in META_ADS if a["id"] in f["value"]], "paging": {"next": "https://graph.example/ads-next"}}
        if path == "act_555/insights":
            bd = params["breakdowns"]
            assert "," not in bd, "one breakdown per call"
            f = params["filtering"][0]
            assert f["field"] == "ad.id" and f["operator"] == "IN"
            assert set(f["value"]) == {"a1", "a2"}, f["value"]  # only asset-feed ads that delivered
            if self.fail_on == bd:
                raise meta.MetaError({"message": "User request limit reached", "code": 17})
            return {"data": BD.get(bd, [])}
        raise AssertionError(path)


META_DATA = {"account": {"currency": "USD"}, "campaigns": [{"id": "c1", "name": "Camp one"}, {"id": "c2", "name": "Camp two"}],
             "adsets": [{"id": "s1", "campaign_id": "c1", "name": "Set one"}, {"id": "s2", "campaign_id": "c2", "name": "Set two"}],
             "ads": META_ADS, "audiences": [], "insights": {}}


def win30():
    return creatives.window(30)


def g_snap(a, win):
    return creatives.fetch_google(a["id"], win, query=fake_google([]))


def m_snap(a, win):
    return creatives.fetch_meta(a["act"], win, get=FakeMeta(), busy=None)


def patches():
    return [mock.patch.object(creatives, "accounts", lambda: [G_ACCT, M_ACCT]),
            mock.patch.object(creatives, "find_account", lambda ref, accts=None: next(a for a in [G_ACCT, M_ACCT] if ref in (a["id"], a["key"]))),
            mock.patch.object(creatives, "_fetch", lambda a, win: g_snap(a, win) if a["platform"] == "google" else m_snap(a, win)),
            mock.patch.object(meta, "load", lambda *a, **k: META_DATA),
            mock.patch.object(meta, "link_problem", lambda url: ""),
            mock.patch.object(meta, "page_check", lambda text, url: []),
            mock.patch.object(meta, "settings", lambda: {"meta_accounts": [{"act": "act_555", "name": "Meta test"}], "views": []})]


class Window(unittest.TestCase):
    def test_days_and_ranges(self):
        w = creatives.window(30)
        self.assertEqual((w["key"], w["g_cond"], w["meta"]), ("30d", "segments.date DURING LAST_30_DAYS", {"date_preset": "last_30d"}))
        w = creatives.window(90, today=dt.date(2026, 11, 17))
        self.assertEqual(w["g_cond"], "segments.date BETWEEN '2026-08-19' AND '2026-11-16'")
        w = creatives.window(start="2026-10-01", end="2026-10-31", today=dt.date(2026, 11, 17))
        self.assertEqual(w["meta"], {"time_range": {"since": "2026-10-01", "until": "2026-10-31"}})
        self.assertEqual(w["key"], "2026-10-01_2026-10-31")
        for bad in ({"days": 5}, {"days": "x"}, {"start": "2026-10-05", "end": "2026-10-01"}, {"start": "2026-13-01", "end": "2026-10-01"},
                    {"start": "2026-11-01", "end": "2030-01-01"}):
            with self.assertRaises(ValueError):
                creatives.window(**bad)

    def test_ninety_days_in_the_account_time_zone(self):
        log = []
        w = creatives.google_window("1234567890", creatives.window(90), fake_google(log))
        import zoneinfo
        today = dt.datetime.now(zoneinfo.ZoneInfo("Pacific/Auckland")).date()
        e = today - dt.timedelta(days=1)
        self.assertEqual(w["g_cond"], f"segments.date BETWEEN '{e - dt.timedelta(days=89)}' AND '{e}'")
        self.assertEqual(len(log), 1)
        self.assertIn("FROM customer", log[0])
        self.assertEqual(creatives.google_window("1234567890", creatives.window(30), fake_google(log))["g_cond"],
                         "segments.date DURING LAST_30_DAYS")
        self.assertEqual(len(log), 1)  # LAST_30_DAYS is already in the account's zone: no read

    def test_finish(self):
        m = creatives.finish({"impressions": 1000, "clicks": 50, "cost": 25.0, "conversions": 2, "value": 100.0})
        self.assertEqual((m["ctr"], m["cpc"], m["cpa"], m["roas"]), (5.0, 0.5, 12.5, 4.0))
        m = creatives.finish({"impressions": 0, "clicks": 0, "cost": 0, "conversions": 0, "value": 0})
        self.assertEqual((m["ctr"], m["cpa"], m["roas"]), (None, None, None))


class Google(unittest.TestCase):
    def setUp(self):
        self.log = []
        self.snap = creatives.fetch_google("1234567890", win30(), query=fake_google(self.log))

    def test_two_searches_read_only(self):
        self.assertEqual(len(self.log), 2)
        self.assertTrue(all(q.startswith("SELECT") for q in self.log))
        self.assertIn("FROM ad_group_ad_asset_view", self.log[0])
        self.assertIn("FROM asset_group_asset", self.log[1])
        self.assertNotIn("performance_label", " ".join(self.log))
        self.assertEqual(self.snap["currency"], "GBP")

    def test_grouped_across_ads(self):
        v = creatives.account_view(G_ACCT, self.snap, "ads")
        reef = next(r for r in v["rows"] if r["text"].startswith("Dive"))
        self.assertEqual((reef["ads"], reef["impressions"], reef["clicks"], reef["cost"]), (2, 1600, 60, 30.0))
        self.assertEqual((reef["ctr"], reef["cpa"], reef["roas"]), (3.75, 15.0, 10.0))
        self.assertFalse(reef["low_volume"])
        self.assertEqual(set(reef["actions"]), {"pause_ad", "replace_text", "remove_text"})
        book = next(r for r in v["rows"] if r["text"] == "Book today")
        self.assertTrue(book["low_volume"])
        self.assertEqual(v["rows"][0]["cost"], 30.0)  # sorted by cost, highest first
        img = next(r for r in v["rows"] if r["kind"] == "image")
        self.assertEqual((img["image_url"], img["actions"], img["channels"]), ("https://img.example/1", [], ["Demand Gen"]))
        vid = next(r for r in v["rows"] if r["kind"] == "video")
        self.assertEqual(vid["video_id"], "abcDEF12345")
        self.assertTrue(any("read-only" in w for w in v["warnings"]))
        flat = creatives.account_view(G_ACCT, self.snap, "ads", grouped=False)
        self.assertEqual(flat["count"], 7)
        only = creatives.account_view(G_ACCT, self.snap, "ads", field="headline", sort="ctr")
        self.assertEqual({r["field"] for r in only["rows"]}, {"HEADLINE"})
        self.assertEqual(only["rows"][0]["text"], "Dive the reef")
        self.assertEqual(creatives.account_view(G_ACCT, self.snap, "ads", search="group b")["count"], 1)

    def test_pmax(self):
        v = creatives.account_view(G_ACCT, self.snap, "pmax")
        head = next(r for r in v["rows"] if r["field"] == "HEADLINE")
        self.assertEqual((head["impressions"], head["cost"], head["cpa"]), (2000, 12.0, 4.0))
        self.assertEqual(set(head["actions"]), {"replace_text", "remove_text"})
        img = next(r for r in v["rows"] if r["field"] == "MARKETING_IMAGE")
        self.assertEqual(img["image_url"], "")  # only https links reach the page
        self.assertEqual(img["actions"], [])
        with self.assertRaises(ValueError):
            creatives.account_view(G_ACCT, self.snap, "assets")


class Meta(unittest.TestCase):
    def test_reads_and_breakdowns(self):
        get = FakeMeta()
        s = creatives.fetch_meta("act_555", win30(), get=get, busy=None)
        self.assertEqual(len(get.calls), 2 + len(creatives.BREAKDOWNS))
        self.assertTrue(all(p.get("date_preset") == "last_30d" for path, p in get.calls if path.endswith("insights")))
        self.assertEqual(set(s["ads"]), {"a1", "a2", "a3"})  # a4 never delivered
        self.assertEqual(s["currency"], "USD")
        self.assertEqual((s["ads"]["a1"]["edit_type"], s["ads"]["a3"]["edit_type"]), ("meta_feed_copy", "meta_copy"))
        v = creatives.account_view(M_ACCT, s, "assets")
        body = next(r for r in v["rows"] if r["text"] == "Body A")
        self.assertEqual((body["ads"], body["impressions"], body["conversions"], body["value"]), (2, 3500, 2.0, 50.0))
        self.assertEqual(body["actions"], ["pause_ad", "replace_text", "remove_text"])
        img = next(r for r in v["rows"] if r["kind"] == "image")
        self.assertEqual(img["actions"], ["pause_ad"])  # images: read-only apart from pausing the ad
        vid = next(r for r in v["rows"] if r["kind"] == "video")
        self.assertEqual((vid["plays"], vid["thruplays"], vid["p100"], vid["avg_watch"], vid["thruplay_rate"]), (100, 20, 10, 6.0, 20.0))
        self.assertIn("2 of 3 ads", v["warnings"][0])
        ads = creatives.account_view(M_ACCT, s, "ads")
        a3 = next(r for r in ads["rows"] if r["members"][0]["owner_id"] == "a3")
        self.assertEqual((a3["primary_text"], a3["headline"], a3["plays"]), (EVIL, "Head 3", 100))
        self.assertEqual(a3["actions"], ["pause_ad", "replace_text"])
        a1 = next(r for r in ads["rows"] if r["members"][0]["owner_id"] == "a1")
        self.assertTrue(a1["flexible"])
        self.assertNotIn("plays", a1)

    def test_ads_from_the_ads_cache(self):
        known = {a["id"]: a for a in META_ADS if a["id"] != "a3"}  # the Ads area's Meta cache, without the newest ad
        get = FakeMeta()
        s = creatives.fetch_meta("act_555", win30(), get=get, busy=None, known=known)
        ads_calls = [p for path, p in get.calls if path == "act_555/ads"]
        self.assertEqual([c["filtering"][0]["value"] for c in ads_calls], [["a3"]])  # only the ad it lacks
        self.assertFalse([p for p, _ in get.calls if p.startswith("https://")])  # the (empty) next page of ads is not read
        self.assertEqual(set(s["ads"]), {"a1", "a2", "a3"})
        get = FakeMeta()
        creatives.fetch_meta("act_555", win30(), get=get, busy=None, known={a["id"]: a for a in META_ADS})
        self.assertEqual(len(get.calls), 1 + len(creatives.BREAKDOWNS))  # no ads call at all

    def test_known_from_meta_cache_file(self):
        with mock.patch.object(meta, "settings", lambda: {"meta_accounts": [{"act": "act_555", "name": "Meta test"}], "views": []}):
            meta.use("meta_555")
        f = core.STUDIO_DIR / "meta_cache_meta_555.json"
        f.write_text(json.dumps({**META_DATA, "fetched_at": dt.datetime.now().isoformat(timespec="minutes")}))
        try:
            k = creatives._meta_known("act_555")
            self.assertEqual(set(k), {"a1", "a2", "a3", "a4"})
            self.assertEqual((k["a1"]["campaign"]["name"], k["a3"]["adset"]["name"]), ("Camp one", "Set two"))
            self.assertEqual(creatives._meta_known("act_999"), {})
        finally:
            f.unlink()

    def test_paging(self):
        get = FakeMeta(page_split=True)
        s = creatives.fetch_meta("act_555", win30(), get=get, busy=None)
        self.assertIn("a3", s["ad_metrics"])
        self.assertEqual(len(get.calls), 3 + len(creatives.BREAKDOWNS))

    def test_back_off_stops_early(self):
        n = {"calls": 0}

        def busy(kind=None):
            n["calls"] += 1
            return "Meta usage is at 92%" if n["calls"] > 4 else None
        get = FakeMeta()
        s = creatives.fetch_meta("act_555", win30(), get=get, busy=busy)
        self.assertEqual(len(get.calls), 4)  # insights, ads, body, title; then it stops
        self.assertEqual(s["missing"], list(creatives.BREAKDOWNS[2:]))
        self.assertIn("92%", s["warnings"][0])
        self.assertEqual(creatives.account_view(M_ACCT, s, "assets")["missing"], s["missing"])
        with self.assertRaises(creatives.MetaBusy):
            creatives.fetch_meta("act_555", win30(), get=FakeMeta(), busy=lambda kind=None: "busy")

    def test_fetch_the_rest(self):
        busy = {"on": True}
        n = {"c": 0}

        def b(kind=None):
            n["c"] += 1
            return "Meta usage is at 99%" if busy["on"] and n["c"] > 3 else None
        snap = creatives.fetch_meta("act_555", win30(), get=FakeMeta(), busy=b)
        self.assertEqual(snap["missing"], list(creatives.BREAKDOWNS[1:]))
        busy["on"] = False
        get = FakeMeta()
        creatives.fill_missing(snap, win30(), get=get, busy=b)
        self.assertEqual(len(get.calls), len(creatives.BREAKDOWNS) - 1)  # only the missing ones, no ads / insights again
        self.assertTrue(all(p.get("breakdowns") for _, p in get.calls))
        self.assertEqual((snap["missing"], snap["warnings"]), ([], []))
        self.assertIn("image_asset", snap["breakdowns"])

    def test_stopped_before_the_ads(self):
        n = {"c": 0}

        def b(kind=None):
            n["c"] += 1
            return "Meta usage is at 104%" if n["c"] == 2 else None
        s = creatives.fetch_meta("act_555", win30(), get=FakeMeta(), busy=b, known={})
        self.assertEqual(s["missing"], ["ads"] + list(creatives.BREAKDOWNS))
        self.assertIn("the ads' creatives", s["warnings"][0])
        v = creatives.account_view(M_ACCT, s, "ads")
        self.assertEqual(v["count"], 3)  # metrics and names from the insights row already
        a1 = next(r for r in v["rows"] if r["members"][0]["owner_id"] == "a1")
        self.assertEqual((a1["members"][0]["ad_name"], a1["actions"], a1["impressions"]), ("Ad a1", [], 4000))
        get = FakeMeta()
        creatives.fill_missing(s, win30(), get=get, busy=None, known={})
        self.assertEqual([p.get("breakdowns", "ads") for _, p in get.calls], ["ads"] + list(creatives.BREAKDOWNS))
        self.assertEqual(s["missing"], [])
        self.assertEqual(s["ads"]["a1"]["name"], "Flexible one")
        self.assertTrue(creatives.account_view(M_ACCT, s, "assets")["count"])

    def test_ads_kept_per_call(self):
        many = {f"x{n}": ins(f"x{n}", 10, 1, 1.0) for n in range(150)}

        class Many(FakeMeta):
            def __call__(self, path, **params):
                self.calls.append((path, params))
                if path == "act_555/insights":
                    return {"data": list(many.values())}
                ids = params["filtering"][0]["value"]
                return {"data": [{"id": i, "name": "Ad " + i, "effective_status": "ACTIVE", "creative": {"id": "c"}} for i in ids if i != "x10"]}
        n = {"c": 0}

        def b(kind=None):
            n["c"] += 1
            return "Meta usage is at 101%" if n["c"] == 3 else None
        s = creatives.fetch_meta("act_555", win30(), get=Many(), busy=b, known={})
        self.assertEqual(s["missing"][0], "ads")
        done = [i for i, a in s["ads"].items() if a["read"]]
        self.assertEqual(len(done), 100)  # the first call's ads are kept
        self.assertEqual(s["ads"]["x10"]["why_not"], "Meta no longer returns this ad (deleted)")
        get = Many()
        creatives.fill_missing(s, win30(), get=get, busy=None, known={})
        self.assertEqual(len(get.calls[0][1]["filtering"][0]["value"]), 50)  # only the other 50
        self.assertTrue(all(a["read"] for a in s["ads"].values()))

    def test_busy_per_kind(self):
        u = {"x-business-use-case-usage": {"1": [{"type": "ads_management", "call_count": 8, "total_time": 108, "estimated_time_to_regain_access": 2},
                                                 {"type": "ads_insights", "call_count": 10, "total_time": 12}]},
             "x-fb-ads-insights-throttle": {"app_id_util_pct": 3, "acc_id_util_pct": 4}, "x-app-usage": {"call_count": 1}}
        with mock.patch("meta_api.usage", lambda: u):
            self.assertIn("108%", creatives._meta_busy("management"))
            self.assertIsNone(creatives._meta_busy("insights"))  # insights reads have their own limit
            self.assertIn("108%", creatives._meta_busy())
        u["x-fb-ads-insights-throttle"]["acc_id_util_pct"] = 95
        with mock.patch("meta_api.usage", lambda: u):
            self.assertIn("95%", creatives._meta_busy("insights"))
        seen = []
        creatives.fetch_meta("act_555", win30(), get=FakeMeta(), busy=lambda kind=None: seen.append(kind), known={})
        self.assertEqual(seen, ["insights", "management"] + ["insights"] * len(creatives.BREAKDOWNS))

    def test_rate_limit_keeps_what_was_read(self):
        get = FakeMeta(fail_on="image_asset")
        s = creatives.fetch_meta("act_555", win30(), get=get, busy=None)
        self.assertEqual(set(s["breakdowns"]), {"body_asset", "title_asset", "description_asset"})
        self.assertEqual(s["missing"][0], "image_asset")
        self.assertEqual(len(get.calls), 2 + 4)  # no retry of the refused call

    def test_other_errors_raise(self):
        class Bad(FakeMeta):
            def __call__(self, path, **params):
                if params.get("breakdowns"):
                    raise meta.MetaError({"message": "Invalid parameter", "code": 100})
                return super().__call__(path, **params)
        with self.assertRaises(meta.MetaError):
            creatives.fetch_meta("act_555", win30(), get=Bad(), busy=None)

    def test_network_error_keeps_what_was_read(self):
        import urllib.error

        class Drop(FakeMeta):
            def __call__(self, path, **params):
                if params.get("breakdowns") == "image_asset":
                    self.calls.append((path, params))
                    raise urllib.error.URLError("connection reset")
                if params.get("breakdowns") == "video_asset":
                    self.calls.append((path, params))
                    raise meta.MetaError({"message": "timed out"})  # get_once: no answer from Meta
                return super().__call__(path, **params)
        s = creatives.fetch_meta("act_555", win30(), get=Drop(), busy=None)
        self.assertEqual(set(s["breakdowns"]), {"body_asset", "title_asset", "description_asset"})
        self.assertEqual(s["missing"][0], "image_asset")
        self.assertIn("connection reset", s["warnings"][0])
        snap = creatives.fill_missing(s, win30(), get=Drop(), busy=None, known={})
        self.assertIn("image_asset", snap["missing"])

    def test_ads_view_labels_wait_for_the_creative(self):
        s = creatives.fetch_meta("act_555", win30(), get=FakeMeta(), busy=None)
        s["ads"]["a3"].update(read=False, format="")
        rows = {m["ad_id"]: m for m in creatives._members(s, "ads")}
        self.assertEqual(rows["a3"]["field_label"], "Not read yet")
        self.assertEqual(rows["a1"]["field_label"].split()[0], "Flexible")

    def test_no_delivery_one_call(self):
        get = mock.Mock(return_value={"data": []})
        s = creatives.fetch_meta("act_555", win30(), get=get, busy=None)
        self.assertEqual(get.call_count, 1)
        self.assertEqual((s["ads"], s["breakdowns"]), ({}, {}))


class Cache(unittest.TestCase):
    def setUp(self):
        self.p = patches()
        for x in self.p:
            x.start()
        self.fetches = []
        orig = creatives._fetch

        def counted(a, win):
            self.fetches.append((a["id"], win["key"]))
            return orig(a, win)
        self.c = mock.patch.object(creatives, "_fetch", counted)
        self.c.start()
        datapolicy.invalidate(creatives.SOURCE)

    def tearDown(self):
        self.c.stop()
        for x in self.p:
            x.stop()

    def test_fetched_once_then_kept(self):
        v = creatives.status("1234567890", days=30)
        self.assertEqual(v["platform"], "google")
        self.assertEqual(len(self.fetches), 1)
        creatives.status("1234567890", days=30)
        creatives.status("1234567890", days=30, view="pmax")
        self.assertEqual(len(self.fetches), 1)  # manual mode: kept until Refresh
        creatives.status("1234567890", days=7)
        self.assertEqual(len(self.fetches), 2)  # a range never fetched: once
        creatives.status("1234567890", days=30, refresh=True)
        self.assertEqual(len(self.fetches), 3)
        r = creatives.refresh("meta_555", 30)
        self.assertEqual((r["errors"], len(self.fetches)), ([], 4))
        self.assertTrue(datapolicy.fetched("creatives:meta_555"))
        creatives.status("1234567890", days=30)
        self.assertEqual(len(self.fetches), 4)
        datapolicy.invalidate("all")  # Refresh data > All
        creatives.status("1234567890", days=30)
        self.assertEqual(len(self.fetches), 5)

    def test_fetch_rest_through_the_cache(self):
        n = {"c": 0}

        def stopped(a, win):
            self.fetches.append((a["id"], win["key"]))
            if a["platform"] == "google":
                return g_snap(a, win)

            def b(kind=None):
                n["c"] += 1
                return "Meta usage is at 99%" if n["c"] > 2 else None
            return creatives.fetch_meta(a["act"], win, get=FakeMeta(), busy=b)
        with mock.patch.object(creatives, "_fetch", stopped):
            v = creatives.status("meta_555", "assets", days=30)
        self.assertEqual(v["missing"], list(creatives.BREAKDOWNS))
        self.assertEqual(v["count"], 0)
        with self.assertRaises(ValueError):
            creatives.fetch_rest("1234567890", 30)
        get = FakeMeta()
        with mock.patch.object(creatives, "_meta_busy", lambda kind=None: None), mock.patch("meta_api.get_once", get):
            r = creatives.fetch_rest("meta_555", 30)
        self.assertEqual(r["missing"], [])
        self.assertEqual(len(get.calls), len(creatives.BREAKDOWNS))
        v = creatives.status("meta_555", "assets", days=30)
        self.assertGreater(v["count"], 0)
        self.assertEqual(len(self.fetches), 1)  # the cached ad data was kept, not read again

    def test_refresh_menu_source(self):
        import datasources  # noqa: PLC0415
        st = datasources.status()
        self.assertIn("creatives", [r["source"] for r in st["sources"]])
        out = datasources.refresh("creatives", "meta_555", days="14")
        self.assertEqual(out["results"][0]["source"], "creatives")
        self.assertEqual(out["results"][0]["window"], "last 14 days")
        self.assertEqual(self.fetches[-1], ("meta_555", "14d"))

    def test_area_routes(self):
        import areas  # noqa: PLC0415
        self.assertIn("creatives", areas.AREAS)
        a = areas.AREAS["creatives"]
        self.assertEqual((set(a.GET), set(a.POST)), ({"view"}, {"refresh", "rest", "draft"}))
        v = a.GET["view"]({"account": "meta_555", "view": "ads", "days": "30", "grouped": "0", "min_impressions": "3000"})
        self.assertEqual((v["view"], v["grouped"]), ("ads", False))
        self.assertEqual(sum(r["low_volume"] for r in v["rows"]), 2)
        with self.assertRaises(ValueError):
            a.POST["draft"]({"account": "meta_555"})

    def test_mcp(self):
        import mcp_server  # noqa: PLC0415
        tool = next(t for t in mcp_server.TOOLS if t["name"] == "creative_performance")
        self.assertNotIn("push", json.dumps(tool).lower().replace("push live", ""))
        out = mcp_server.call("creative_performance", {"account": "1234567890", "field": "headline", "limit": 1})
        self.assertEqual((out["platform"], len(out["rows"]), out["count"]), ("google", 1, 2))
        self.assertNotIn("members", out["rows"][0])
        self.assertNotIn("actions", out["rows"][0])
        out = mcp_server.call("creative_performance", {"account": "meta_555", "view": "assets", "sort": "ctr"})
        self.assertEqual(out["currency"], "USD")
        self.assertTrue(all(r.get("ctr") is not None for r in out["rows"]))


class Draft(unittest.TestCase):
    def setUp(self):
        self.p = patches()
        for x in self.p:
            x.start()
        self.log = []
        datapolicy.invalidate(creatives.SOURCE)
        core.clear("1234567890")
        meta.use("meta_555")
        core.clear(meta.acct())
        self.g = creatives.status("1234567890", days=30)
        self.gp = creatives.status("1234567890", days=30, view="pmax")
        self.m = creatives.status("meta_555", days=30, view="assets")
        self.ma = creatives.status("meta_555", days=30, view="ads")
        self.meta_get = FakeMeta()
        PMAX_COUNT.update(n=5, enabled=5)

    def tearDown(self):
        for x in self.p:
            x.stop()

    def key(self, v, text):
        return next(r["key"] for r in v["rows"] if r["text"] == text)

    def add(self, account, view, key, action, **kw):
        kw.setdefault("meta_get", self.meta_get)
        return creatives.add_to_draft(account, view, key, action, days=30, query=fake_google(self.log), **kw)

    def test_rsa_replace_headline_everywhere(self):
        r = self.add("1234567890", "ads", self.key(self.g, "Dive the reef"), "replace_text", new_text="Dive the outer reef")
        self.assertEqual((len(r["added"]), r["skipped"]), (2, []))
        self.assertEqual(len([q for q in self.log if "FROM ad_group_ad WHERE" in q]), 1)  # one fresh read of both ads
        items = core.draft("1234567890")["items"]
        c = next(i["change"] for i in items if i["change"]["local_id"] == 1)
        self.assertEqual((c["type"], c["edit"], c["campaign"], c["ad_group"]), ("rsa", True, "Search A", "Group A"))
        self.assertEqual(c["headlines"][0], {"text": "Dive the outer reef", "pin": 1})  # the pin stays
        self.assertEqual([h["text"] for h in c["headlines"][1:]], ["Book today", "Small groups"])
        self.assertEqual(len(c["descriptions"]), 2)
        self.assertTrue(items[0]["note"].startswith("Creatives:"))
        self.assertEqual(items[0]["problems"], [])
        again = self.add("1234567890", "ads", self.key(self.g, "Dive the reef"), "replace_text", new_text="Something else")
        self.assertEqual(again["added"], [])
        self.assertIn("already has a change", again["skipped"][0]["why"])

    def test_rsa_remove_respects_minimum_and_one_ad(self):
        r = self.add("1234567890", "ads", self.key(self.g, "Dive the reef"), "remove_text")
        self.assertEqual(len(r["added"]), 1)  # ad 2 keeps 3 headlines
        self.assertIn("fewer than 3", r["skipped"][0]["why"])  # ad 1 would drop to 2
        core.clear("1234567890")
        r = self.add("1234567890", "ads", self.key(self.g, "Dive the reef"), "pause_ad", ad_ids=["2"])
        c = core.draft("1234567890")["items"][0]["change"]
        self.assertEqual(c, {"type": "rsa", "edit": True, "campaign": "Search A", "ad_group": "Group B", "local_id": 2, "status": "paused"})

    def test_rsa_text_limits_and_read_only_rows(self):
        with self.assertRaises(ValueError):
            self.add("1234567890", "ads", self.key(self.g, "Dive the reef"), "replace_text", new_text="x" * 31)
        with self.assertRaises(ValueError):
            self.add("1234567890", "ads", self.key(self.g, "Dive the reef"), "replace_text", new_text="  ")
        img = next(r["key"] for r in self.g["rows"] if r["kind"] == "image")
        with self.assertRaises(ValueError):
            self.add("1234567890", "ads", img, "pause_ad")  # Demand Gen: no draft change type
        with self.assertRaises(ValueError):
            self.add("1234567890", "ads", "nope", "pause_ad")
        with self.assertRaises(ValueError):
            self.add("1234567890", "ads", img, "delete_everything")
        self.assertEqual(core.draft("1234567890")["items"], [])

    def test_pmax_replace_is_add_plus_remove(self):
        r = self.add("1234567890", "pmax", self.key(self.gp, "Sail away"), "replace_text", new_text="Sail further")
        self.assertEqual(len(r["added"]), 2)
        ch = [i["change"] for i in core.draft("1234567890")["items"]]
        self.assertEqual(ch[0], {"type": "pmax_asset", "campaign": "PMax A", "asset_group": "AG one", "field": "headline", "texts": ["Sail further"]})
        self.assertEqual(ch[1], {"type": "pmax_asset", "campaign": "PMax A", "asset_group": "AG one", "field": "headline", "remove": True, "texts": ["Sail away"]})
        self.assertEqual(len(self.log), 1)  # one read of the asset group's counts
        self.assertIn("asset_group_asset.source", self.log[0])

    def test_pmax_full_group_removes_first(self):
        PMAX_COUNT.update(n=15, enabled=15)
        self.add("1234567890", "pmax", self.key(self.gp, "Sail away"), "replace_text", new_text="Sail further")
        ch = [i["change"] for i in core.draft("1234567890")["items"]]
        self.assertEqual([bool(c.get("remove")) for c in ch], [True, False])

    def test_pmax_remove_at_minimum_is_skipped(self):
        PMAX_COUNT.update(n=4, enabled=3)  # 3 enabled (+1 paused, +1 created by Google, which doesn't count)
        r = self.add("1234567890", "pmax", self.key(self.gp, "Sail away"), "remove_text")
        self.assertEqual(r["added"], [])
        self.assertIn("needs at least 3", r["skipped"][0]["why"])
        PMAX_COUNT.update(n=4, enabled=4)
        r = self.add("1234567890", "pmax", self.key(self.gp, "Sail away"), "remove_text")
        self.assertEqual(len(r["added"]), 1)

    def test_pmax_counts_unreadable_still_drafts(self):
        def broken(cid, gaql):
            raise OSError("no network in tests")
        r = creatives.add_to_draft("1234567890", "pmax", self.key(self.gp, "Sail away"), "replace_text", new_text="Sail on", days=30, query=broken)
        self.assertEqual(len(r["added"]), 2)  # the preview checks the counts again

    def test_rsa_replace_never_duplicates(self):
        live = {"1": RSA_LIVE["1"], "2": {"headlines": [{"text": "Dive the reef"}, {"text": "Other one"}, {"text": "Dive  the  reef"}, {"text": "Fourth"}],
                                          "descriptions": [{"text": "D1"}, {"text": "D2"}]}}
        with mock.patch.dict(RSA_LIVE, live):
            r = self.add("1234567890", "ads", self.key(self.g, "Dive the reef"), "replace_text", new_text="Dive deeper", ad_ids=["2"])
        self.assertEqual(len(r["added"]), 1, r)
        c = core.draft("1234567890")["items"][0]["change"]
        self.assertEqual([h["text"] for h in c["headlines"]], ["Dive deeper", "Other one", "Fourth"])
        with self.assertRaises(ValueError):  # same text again
            self.add("1234567890", "ads", self.key(self.g, "Dive the reef"), "replace_text", new_text="Dive  the reef")

    def test_clash_without_local_id(self):
        core.add("1234567890", [{"type": "rsa", "campaign": "Search A", "ad_group": "Group A", "final_url": "https://e.com",
                                 "headlines": ["New 1", "New 2", "New 3"], "descriptions": ["D1", "D2"]}])  # a new RSA: no clash
        core.add("1234567890", [{"type": "rsa", "edit": True, "campaign": "Search A", "ad_group": "group a",
                                 "headlines": ["H1", "H2", "H3"], "descriptions": ["D1", "D2"]}])
        r = self.add("1234567890", "ads", self.key(self.g, "Dive the reef"), "replace_text", new_text="Dive the outer reef")
        self.assertEqual(len(r["added"]), 1)  # ad 2 (Group B) only
        self.assertEqual(r["skipped"][0]["name"], "Search A / Group A")
        self.assertIn("already has a change", r["skipped"][0]["why"])
        self.assertTrue(creatives._clash("google", [{"change": {"type": "rsa", "edit": True, "ad_id": "1", "headlines": ["x"]}}],
                                         {"type": "rsa", "local_id": 1, "campaign": "C", "ad_group": "G", "headlines": ["y"]}))
        self.assertFalse(creatives._clash("google", [{"change": {"type": "rsa", "edit": True, "ad_id": "9", "headlines": ["x"]}}],
                                          {"type": "rsa", "local_id": 1, "campaign": "C", "ad_group": "G", "headlines": ["y"]}))
        # a new RSA waiting in the draft (not an edit) never blocks an edit of an existing ad in that ad group
        self.assertFalse(creatives._clash("google", [{"change": {"type": "rsa", "campaign": "C", "ad_group": "G", "headlines": ["a", "b", "c"],
                                                                 "descriptions": ["d", "e"]}}],
                                          {"type": "rsa", "edit": True, "campaign": "C", "ad_group": "G", "local_id": 123,
                                           "headlines": ["x", "y", "z"], "descriptions": ["d", "e"]}))
        self.assertFalse(creatives._clash("google", [{"change": {"type": "rsa", "campaign": "C", "ad_group": "G", "status": "paused"}}],
                                          {"type": "rsa", "local_id": 1, "campaign": "C", "ad_group": "G", "headlines": ["y"]}))

    def test_double_width_text_length(self):
        with self.assertRaises(ValueError) as e:  # 16 double-width characters count as 32 > 30
            self.add("1234567890", "ads", self.key(self.g, "Dive the reef"), "replace_text", new_text="\u65e5" * 16)
        self.assertIn("double-width", str(e.exception))
        r = self.add("1234567890", "ads", self.key(self.g, "Dive the reef"), "replace_text", new_text="\u65e5" * 15)
        self.assertEqual(len(r["added"]), 2)
        self.assertEqual(core.gae.width("ab\uff21\u65e5"), 6)

    def test_meta_feed_text(self):
        r = self.add("meta_555", "assets", self.key(self.m, "Body A"), "replace_text", new_text="Body C", by="you")
        self.assertEqual(len(r["added"]), 2, r)
        fresh = [c for c in self.meta_get.calls if c[0] == "act_555/ads"]
        self.assertEqual(len(fresh), 1)  # one fresh read of both ads' text lists
        self.assertEqual(fresh[0][1]["filtering"][0]["value"], ["a1", "a2"])
        items = core.draft(meta.acct())["items"]
        by_id = {i["change"]["id"]: i for i in items}
        self.assertEqual(by_id["a1"]["change"], {"type": "meta_feed_copy", "level": "ad", "id": "a1", "bodies": ["Body C", "Body B"]})
        self.assertEqual(by_id["a2"]["change"]["bodies"], ["Body C"])
        self.assertEqual(by_id["a1"]["problems"], [])
        core.clear(meta.acct())
        r = self.add("meta_555", "assets", self.key(self.m, "Body A"), "remove_text")
        self.assertEqual(len(r["added"]), 1)
        self.assertIn("only primary text", r["skipped"][0]["why"])
        self.assertEqual(core.draft(meta.acct())["items"][0]["change"]["bodies"], ["Body B"])

    def test_meta_feed_text_from_a_fresh_read(self):
        class Now(FakeMeta):
            def __call__(self, path, **params):
                if path == "act_555/ads":
                    self.calls.append((path, params))
                    return {"data": [{"id": "a1", "creative": creative_feed(["Body A", "Added on Meta since", "Body A "], ["Title A"])}]}
                return super().__call__(path, **params)
        r = self.add("meta_555", "assets", self.key(self.m, "Body A"), "replace_text", new_text="Body C", meta_get=Now())
        self.assertEqual(len(r["added"]), 1, r)
        self.assertIn("didn't return the ad", r["skipped"][0]["why"])  # a2 is gone on Meta now
        c = core.draft(meta.acct())["items"][0]["change"]
        self.assertEqual(c["bodies"], ["Body C", "Added on Meta since"])  # Meta's list now, the text once

    def test_meta_pause_and_ad_copy(self):
        key = next(r["key"] for r in self.ma["rows"] if r["members"][0]["owner_id"] == "a3")
        r = self.add("meta_555", "ads", key, "pause_ad")
        self.assertEqual(core.draft(meta.acct())["items"][0]["change"], {"type": "meta_status", "level": "ad", "id": "a3", "status": "paused"})
        core.clear(meta.acct())
        r = self.add("meta_555", "ads", key, "replace_text", new_text="New headline", text_field="headline")
        self.assertEqual(len(r["added"]), 1, r)
        self.assertEqual(core.draft(meta.acct())["items"][0]["change"], {"type": "meta_copy", "level": "ad", "id": "a3", "headline": "New headline"})
        flex = next(r["key"] for r in self.ma["rows"] if r["members"][0]["owner_id"] == "a2")
        with self.assertRaises(ValueError):  # paused flexible ad: no pause, and its copy is changed per text in Assets
            self.add("meta_555", "ads", flex, "replace_text", new_text="x")
        with self.assertRaises(ValueError):
            self.add("meta_555", "ads", key, "replace_text", new_text="x", text_field="link")

    def test_trial_campaign_refused_rest_added(self):
        real = core.add

        def add(acct, changes, by="you", note=None):
            if changes[0].get("ad_group") == "Group A":
                raise ValueError("Not added to the draft. RSAs in Search A / Group A: This is an experiment's trial campaign")
            return real(acct, changes, by=by, note=note)
        with mock.patch.object(core, "add", add):
            r = self.add("1234567890", "ads", self.key(self.g, "Dive the reef"), "pause_ad")
        self.assertEqual(len(r["added"]), 1)
        self.assertEqual(r["skipped"][0]["name"], "Search A / Group A")
        self.assertIn("trial campaign", r["skipped"][0]["why"])

    def test_needs_the_view_first(self):
        with self.assertRaises(ValueError):
            creatives.add_to_draft("1234567890", "ads", "k", "pause_ad", days=7)


class PmaxPushCounts(unittest.TestCase):
    """live._pmax_asset_ops: a replace (add + remove) is checked against what the asset group holds after the earlier
    items of the same push. Fakes only: the asset group read is a fake query, nothing is sent."""

    def ops(self, changes, have, enable_new=False):
        import live  # noqa: PLC0415
        rows = [{"assetGroupAsset": {"status": "ENABLED", "source": "ADVERTISER"}, "asset": {"id": str(i), "textAsset": {"text": f"H{i}"}}}
                for i in range(have)]

        class Ids:
            acct = "1234567890"
            _n = 0

            def campaign(self, name):
                return "customers/1234567890/campaigns/300", "ref"

            def asset_group(self, camp, name):
                return "customers/1234567890/assetGroups/40"

            def tmp(self, kind):
                Ids._n -= 1
                return f"customers/1234567890/{kind}/{Ids._n}"
        ids, out = Ids(), []
        with mock.patch.object(live, "query", lambda acct, gaql: rows):
            for c in changes:
                try:
                    out.append(("ok", live._pmax_asset_ops(ids, c, enable_new)))
                except ValueError as ex:
                    out.append(("refused", str(ex)))
        return out

    def test_replace_in_a_full_group(self):
        base = {"type": "pmax_asset", "campaign": "PMax A", "asset_group": "AG one", "field": "headline"}
        rm, add = {**base, "remove": True, "texts": ["H0"]}, {**base, "texts": ["New one"]}
        self.assertEqual([r[0] for r in self.ops([rm, add], 15)], ["ok", "ok"])  # the removal frees the slot
        self.assertEqual([r[0] for r in self.ops([add], 15)], ["refused"])

    def test_replace_at_the_minimum(self):
        base = {"type": "pmax_asset", "campaign": "PMax A", "asset_group": "AG one", "field": "headline"}
        rm, add = {**base, "remove": True, "texts": ["H0"]}, {**base, "texts": ["New one"]}
        self.assertEqual([r[0] for r in self.ops([add, rm], 3, enable_new=True)], ["ok", "ok"])
        out = self.ops([add, rm], 3)  # the new text goes in paused: the group would keep 2 enabled
        self.assertEqual(out[1][0], "refused")
        self.assertIn("at least 3", out[1][1])
        self.assertIn("new texts go in paused", out[1][1])
        out = self.ops([rm], 3)  # a plain removal: no adds in the push, so no hint about new texts
        self.assertEqual(out[0][0], "refused")
        self.assertNotIn("new texts", out[0][1])


class Text(unittest.TestCase):
    def test_house_style(self):
        for f in ("creatives.py", "areas/creatives.py", "static/creatives.js", "tests/test_creatives.py"):
            t = (HERE / f).read_text(encoding="utf-8")
            self.assertNotIn(chr(0x2014), t, f)
            for word in ("ML " + "Suite", "Master " + "Live" + "aboards", "Live" + "aboard"):
                self.assertNotIn(word, t, f)

    def test_ui_escapes_platform_text(self):
        js = (HERE / "static/creatives.js").read_text(encoding="utf-8")
        for raw in ("${r.text}", "${r.headline}", "${r.primary_text}", "${m.where}", "${r.image_url}", "${m.ad_name}", "${a.name}"):
            self.assertNotIn(raw, js)
        self.assertIn("safeUrl", js)


if __name__ == "__main__":
    unittest.main(verbosity=1)
