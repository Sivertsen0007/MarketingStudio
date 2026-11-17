"""Demand Gen / YouTube video ads: draft checks, the Google Ads operations Push live sends, undo, the trial guard,
uploads kept out of Push live and Send to Editor, the Videos reads and the MCP tools.

Run from the engine folder:  python3 -m unittest tests/test_demandgen.py
A temp MSTUDIO_HOME, no Keychain, no Editor; the Google Ads API is replaced by a fake that records every request.
Nothing here reaches Google.
"""
import os as _os, tempfile as _tf
_os.environ["MSTUDIO_HOME"] = _tf.mkdtemp(prefix="mstudio-test-")
_os.environ["MSTUDIO_KEYCHAIN"] = "0"
_os.environ["GAE_EDITOR_DIR"] = _tf.mkdtemp(prefix="gae-editor-test-")

import json
import re
import struct
import sys
import unittest
import zlib
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core  # noqa: E402
import demandgen  # noqa: E402
import draftstore  # noqa: E402
import experiments  # noqa: E402
import live  # noqa: E402

A = "3333333333"
VID, VID2, NEWVID = "AAAAAAAAAAA", "BBBBBBBBB-_", "CCCCCCCCCCC"
CAMPS = {"101": ("DG Brand", "DEMAND_GEN", "BASE", "MAXIMIZE_CONVERSIONS"), "102": ("Search X", "SEARCH", "BASE", "MANUAL_CPC"),
         "103": ("DG trial", "DEMAND_GEN", "EXPERIMENT", "TARGET_CPA"), "104": ("DG Geo", "DEMAND_GEN", "BASE", "TARGET_CPA")}
GROUPS = {"201": ("YT", "101"), "202": ("Words", "102"), "203": ("T", "103"), "204": ("Geo AG", "104")}
ADS = {"301": ("201", "DEMAND_GEN_VIDEO_RESPONSIVE_AD", "ENABLED"), "302": ("201", "DEMAND_GEN_MULTI_ASSET_AD", "PAUSED"),
       "303": ("202", "RESPONSIVE_SEARCH_AD", "ENABLED")}


def png(w, h):
    raw = b"".join(b"\x00" + b"\x10\x40\x80" * w for _ in range(h))
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)  # noqa: E731
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


class FakeGoogle:
    """live.query / live.mutate stand-in: answers the reads Push live asks, records every mutate (and refuses none)."""

    def __init__(self, currency="GBP", minimum="4000000", mutate_ok=True):
        self.queries, self.mutates, self.cur, self.min, self.ok = [], [], currency, minimum, mutate_ok
        self.channels = {"selectedChannels": {"youtubeInStream": True, "youtubeInFeed": True, "youtubeShorts": True, "discover": True,
                                              "gmail": True}}

    def query(self, acct, g):
        self.queries.append(g)
        if g == experiments.CAMPAIGNS_GAQL:
            return [{"campaign": {"id": i, "name": n, "experimentType": x}} for i, (n, _, x, _) in CAMPS.items()]
        if g == experiments.ARMS_GAQL:
            return []
        if g.startswith("SELECT campaign.id, campaign.name FROM campaign"):
            return [{"campaign": {"id": i, "name": n}} for i, (n, *_r) in CAMPS.items()]
        if g.startswith("SELECT ad_group.id, ad_group.name, campaign.id FROM ad_group"):
            return [{"adGroup": {"id": i, "name": n}, "campaign": {"id": c}} for i, (n, c) in GROUPS.items()]
        m = re.search(r"FROM campaign WHERE campaign.resource_name = 'customers/\d+/campaigns/(\d+)'", g)
        if m and "advertising_channel_type" in g:
            n, ch, _, bid = CAMPS[m.group(1)]
            return [{"campaign": {"advertisingChannelType": ch, "status": "PAUSED", "campaignBudget": f"customers/{A}/campaignBudgets/5{m.group(1)}",
                                  "biddingStrategyType": bid, **({"targetCpa": {"targetCpaMicros": "10000000"}} if bid == "TARGET_CPA" else {})},
                     "campaignBudget": {"amountMicros": "20000000", "explicitlyShared": False}}]
        if g.startswith("SELECT customer.currency_code FROM customer"):
            return [{"customer": {"currencyCode": self.cur}}]
        if "FROM currency_constant" in g:
            return [{"currencyConstant": {"code": self.cur, "demandGenInfo": {"minimumBudgetPerDayMicros": self.min}}}] if self.min else []
        if "FROM campaign_criterion WHERE campaign.resource_name" in g:
            return [{"campaignCriterion": {"type": "LOCATION"}}] if "/campaigns/104'" in g else []
        if "FROM ad_group WHERE ad_group.resource_name" in g:
            return [{"adGroup": {"status": "ENABLED", "demandGenAdGroupSettings": {"channelControls": self.channels}}}]
        m = re.search(r"FROM ad_group_ad WHERE ad_group_ad.resource_name = 'customers/\d+/adGroupAds/(\d+)~(\d+)'", g)
        if m:
            ad = ADS.get(m.group(2))
            if not ad or ad[0] != m.group(1):
                return []
            body = {"type": ad[1], "finalUrls": ["https://example.com/old"]}
            if ad[1] == "DEMAND_GEN_VIDEO_RESPONSIVE_AD":
                body["demandGenVideoResponsiveAd"] = {"headlines": [{"text": "Old one"}], "longHeadlines": [{"text": "Old long"}],
                                                      "descriptions": [{"text": "Old description"}], "businessName": {"text": "Old name"},
                                                      "videos": [{"asset": f"customers/{A}/assets/900"}], "logoImages": [{"asset": f"customers/{A}/assets/950"}]}
            return [{"adGroupAd": {"status": ad[2], "ad": body}}]
        if "asset.type = YOUTUBE_VIDEO AND asset.youtube_video_asset.youtube_video_id IN" in g:
            return [{"asset": {"resourceName": f"customers/{A}/assets/900", "youtubeVideoAsset": {"youtubeVideoId": VID}}}] if f"'{VID}'" in g else []
        m = re.search(r"FROM asset WHERE asset.id = (\d+)", g)
        if m:
            sizes = {"950": (512, 512, "IMAGE"), "951": (600, 300, "IMAGE"), "952": (0, 0, "TEXT")}
            if m.group(1) not in sizes:
                return []
            w, h, t = sizes[m.group(1)]
            return [{"asset": {"resourceName": f"customers/{A}/assets/{m.group(1)}", "type": t,
                               "imageAsset": {"fullSize": {"widthPixels": str(w), "heightPixels": str(h)}}}}]
        if "asset.type = CALL_TO_ACTION" in g:
            return [{"asset": {"resourceName": f"customers/{A}/assets/970", "callToActionAsset": {"callToAction": "LEARN_MORE"}}}]
        raise AssertionError("unexpected query: " + g)

    def mutate(self, acct, ops, apply):
        self.mutates.append((ops, apply))
        if not self.ok:
            return False, [{"message": "nope", "index": 0}]
        if not apply:
            return True, {}
        out = []
        for i, o in enumerate(ops):
            kind = next(iter(o))
            out.append({live.RESULT_KEY[kind]: {"resourceName": f"customers/{A}/x/{1000 + i}"}})
        return True, {"mutateOperationResponses": out}


AD = dict(type="dg_video_ad", campaign="DG Brand", ad_group="YT", videos=[VID], headlines=["Dive the reef", "Small groups", "Book a space"],
          long_headlines=["Liveaboard trips to the best reefs", "Expert guides on every trip", "See next season's trips"],
          descriptions=["Reefs and wrecks from a liveaboard", "Small groups and expert guides", "Prices for next season"],
          business_name="Dive trips", logo_asset_id="950", final_url="https://example.com/trips")


def item(change, iid="i1"):
    p, w = core.check(change, A)
    return {"id": iid, "change": change, "by": "you", "note": "", "ts": "2026-11-17T10:00:00", "summary": core._summary(change),
            "problems": p, "warnings": w}


class Base(unittest.TestCase):
    def setUp(self):
        core.clear(A)
        (core.STUDIO_DIR).mkdir(parents=True, exist_ok=True)
        (core.STUDIO_DIR / "account_currency.json").write_text(json.dumps({A: "GBP"}))
        experiments._KNOWN.clear()
        self.f = FakeGoogle()
        patch = mock.patch.multiple(live, query=self.f.query, mutate=self.f.mutate)
        patch.start()
        self.addCleanup(patch.stop)

    def plan(self, *changes, enable_new=False):
        return live.plan(A, enable_new, items=[item(c, f"i{n}") for n, c in enumerate(changes)], source="api")

    def ops(self, p):
        return [o["op"] for o in p["ops"]]


class VideoIds(unittest.TestCase):
    def test_ids_and_urls(self):
        for raw in (VID, f"https://www.youtube.com/watch?v={VID}&t=10", f"youtube.com/watch?v={VID}", f"https://youtu.be/{VID}?si=x",
                    f"https://m.youtube.com/shorts/{VID}", f"https://www.youtube.com/embed/{VID}", f"https://www.youtube-nocookie.com/embed/{VID}",
                    f" {VID} "):
            self.assertEqual(demandgen.video_id(raw), VID, raw)

    def test_not_ids(self):
        for raw in ("", "short", "AAAAAAAAAAAA", "https://vimeo.com/123456789", f"https://evil.example/watch?v={VID}",
                    "https://www.youtube.com/channel/UCxxxxxxxxxxxxxxxxxxxxxx", "AAAAAAAAA<A", None):
            with self.assertRaises(ValueError, msg=raw):
                demandgen.video_id(raw)

    def test_image_size(self):
        self.assertEqual(demandgen.image_size(png(300, 200)), ("PNG", 300, 200))
        self.assertEqual(demandgen.image_size(b"GIF89a" + struct.pack("<HH", 128, 128) + b"\0" * 10), ("GIF", 128, 128))
        jpeg = b"\xff\xd8\xff\xe0" + struct.pack(">H", 16) + b"\0" * 14 + b"\xff\xc0" + struct.pack(">HBHH", 17, 8, 256, 512) + b"\0" * 10
        self.assertEqual(demandgen.image_size(jpeg), ("JPEG", 512, 256))
        with self.assertRaises(ValueError):
            demandgen.image_size(b"nope")


class Checks(Base):
    def p(self, c):
        return core.check(c, A)[0]

    def test_all_live_only(self):
        for t in demandgen.TYPES:
            self.assertTrue(core.live_only({"type": t}), t)

    def test_new_campaign(self):
        good = dict(type="dg_campaign", new=True, campaign="DG New", budget=20, ad_group="AG", channels="youtube", locations=[{"id": 2826}])
        self.assertEqual(self.p(good), [])
        self.assertIn("budget 3.00 GBP/day is under Demand Gen's minimum of 4.00 GBP/day", self.p({**good, "budget": 3}))
        self.assertIn("budget must be more than 0", self.p({**good, "budget": 0}))
        self.assertTrue(any("bidding must be" in x for x in self.p({**good, "bidding": "manual_cpc"})))
        self.assertIn("target_cpa must be a number", self.p({**good, "bidding": "target_cpa"}))
        self.assertTrue(any("only goes with" in x for x in self.p({**good, "bidding": "maximize_clicks", "target_cpa": 5})))
        self.assertIn("ad_group is missing", self.p({k: v for k, v in good.items() if k != "ad_group"}))
        self.assertTrue(any("unknown" in x for x in self.p({**good, "channels": ["youtube_shorts", "tiktok"]})))
        self.assertTrue(any("language 'xx'" in x for x in self.p({**good, "languages": "en,xx"})))
        self.assertIn("start_date must be YYYY-MM-DD", self.p({**good, "start_date": "1/12/2026"}))
        w = core.check({**good, "status": "enabled"}, A)[1]
        self.assertTrue(any("always created Paused" in x for x in w))

    def test_unknown_currency_only_warns(self):
        (core.STUDIO_DIR / "account_currency.json").write_text(json.dumps({A: "THB"}))
        p, w = core.check(dict(type="dg_campaign", new=True, campaign="X", budget=1, ad_group="AG"), A)
        self.assertEqual(p, [])
        self.assertTrue(any("minimum daily budget" in x for x in w))

    def test_edit_campaign(self):
        self.assertIn("nothing to change: give status, budget or target_cpa", self.p(dict(type="dg_campaign", campaign="DG Brand")))
        self.assertIn("status must be enabled or paused", self.p(dict(type="dg_campaign", campaign="DG Brand", status="removed")))
        self.assertEqual(self.p(dict(type="dg_campaign", campaign="DG Brand", budget=30, target_cpa=12)), [])

    def test_video_ad_limits(self):
        self.assertEqual(self.p(AD), [])
        p = self.p({**AD, "videos": [VID, VID2, NEWVID, "DDDDDDDDDDD", "EEEEEEEEEEE", "FFFFFFFFFFF"]})
        self.assertIn("videos: 6 given, Google allows 1 to 5", p)
        self.assertIn("videos: 0 given, Google allows 1 to 5", self.p({**AD, "videos": []}))
        self.assertIn("videos: the same video is listed twice", self.p({**AD, "videos": [VID, f"https://youtu.be/{VID}"]}))
        self.assertTrue(any("not a YouTube video id" in x for x in self.p({**AD, "videos": ["https://vimeo.com/1"]})))
        self.assertIn("Short headline 1: 41 chars > 40", self.p({**AD, "headlines": ["x" * 41]}))
        self.assertEqual([x for x in self.p({**AD, "headlines": ["x" * 40]}) if "chars" in x], [])
        self.assertIn("Long headline 2: 91 chars > 90", self.p({**AD, "long_headlines": ["ok", "y" * 91]}))
        self.assertIn("Video description 1: 91 chars > 90", self.p({**AD, "descriptions": ["z" * 91]}))
        self.assertIn("headlines: 6 given, Google allows 1 to 5", self.p({**AD, "headlines": [f"h{i}" for i in range(6)]}))
        self.assertIn("headlines: the same text is listed twice", self.p({**AD, "headlines": ["Same", "same "]}))
        self.assertIn("business name: 26 chars > 25", self.p({**AD, "business_name": "b" * 26}))
        self.assertIn("business_name is missing", self.p({**AD, "business_name": ""}))
        self.assertTrue(any("logo" in x for x in self.p({k: v for k, v in AD.items() if k != "logo_asset_id"})))
        self.assertIn("final_url must be a full https:// address", self.p({**AD, "final_url": "example.com"}))
        self.assertTrue(any("call_to_action must be" in x for x in self.p({**AD, "call_to_action": "click_here"})))
        self.assertIn("breadcrumb1: 16 chars > 15", self.p({**AD, "breadcrumb1": "b" * 16}))
        w = core.check({**AD, "headlines": ["One"]}, A)[1]
        self.assertTrue(any("recommends at least 3" in x for x in w))

    def test_logo_file(self):
        d = Path(_tf.mkdtemp())
        (d / "sq.png").write_bytes(png(200, 200))
        (d / "wide.png").write_bytes(png(300, 100))
        (d / "tiny.png").write_bytes(png(64, 64))
        base = {k: v for k, v in AD.items() if k != "logo_asset_id"}
        self.assertEqual(self.p({**base, "logo_file": str(d / "sq.png")}), [])
        self.assertTrue(any("square" in x for x in self.p({**base, "logo_file": str(d / "wide.png")})))
        self.assertTrue(any("at least 128x128" in x for x in self.p({**base, "logo_file": str(d / "tiny.png")})))
        self.assertTrue(any("not found" in x for x in self.p({**base, "logo_file": str(d / "gone.png")})))
        self.assertTrue(any("full path" in x for x in self.p({**base, "logo_file": "sq.png"})))
        self.assertIn("give logo_asset_id or logo_file, not both", self.p({**AD, "logo_file": str(d / "sq.png")}))

    def test_edit_ad(self):
        self.assertTrue(any("ad_id" in x for x in self.p(dict(type="dg_video_ad", edit=True, campaign="DG Brand", ad_group="YT", headlines=["New"]))))
        self.assertIn("nothing to change", self.p(dict(type="dg_video_ad", edit=True, campaign="DG Brand", ad_group="YT", ad_id="301")))
        self.assertEqual(self.p(dict(type="dg_video_ad", edit=True, campaign="DG Brand", ad_group="YT", ad_id="301", headlines=["New one"])), [])

    def test_status_ad_and_assets(self):
        self.assertEqual(self.p(dict(type="dg_ad", campaign="DG Brand", ad_group="YT", ad_id="302", status="enabled")), [])
        self.assertIn("status must be enabled or paused", self.p(dict(type="dg_ad", campaign="DG Brand", ad_group="YT", ad_id="302", status="removed")))
        self.assertIn("videos: give one or more YouTube video ids or URLs", self.p(dict(type="video_asset", videos=[])))
        self.assertEqual(self.p(dict(type="video_asset", videos=[f"https://youtu.be/{NEWVID}", {"id": VID2, "name": "Teaser"}])), [])

    def test_ad_group(self):
        self.assertEqual(self.p(dict(type="dg_ad_group", new=True, campaign="DG Brand", ad_group="New", channels="youtube")), [])
        self.assertIn("nothing to change: give status or channels", self.p(dict(type="dg_ad_group", campaign="DG Brand", ad_group="YT")))
        self.assertIn("locations and languages of an existing ad group are changed in Google Ads",
                      self.p(dict(type="dg_ad_group", campaign="DG Brand", ad_group="YT", locations=[{"id": 1}], status="paused")))
        self.assertIn("channels: pick at least one channel", self.p(dict(type="dg_ad_group", campaign="DG Brand", ad_group="YT", channels=[" "])))

    def test_upload_checks(self):
        d = Path(_tf.mkdtemp())
        (d / "v.mp4").write_bytes(b"\0" * 100)
        (d / "empty.mp4").write_bytes(b"")
        ok = dict(type="video_upload", file=str(d / "v.mp4"), title="Teaser")
        self.assertEqual(self.p(ok), [])
        self.assertIn("title is missing", self.p({**ok, "title": " "}))
        self.assertIn("title: 101 chars > 100", self.p({**ok, "title": "t" * 101}))
        self.assertIn("title and description can't contain < or >", self.p({**ok, "description": "<b>"}))
        self.assertTrue(any("brand channel" in x for x in self.p({**ok, "channel_id": "UC" + "x" * 22})))
        self.assertIn("the Google-managed channel only takes unlisted videos", self.p({**ok, "privacy": "public"}))
        self.assertTrue(any("must be a video" in x for x in self.p({**ok, "file": str(d / "v.txt")})))
        self.assertIn("the file is empty", self.p({**ok, "file": str(d / "empty.mp4")}))
        p, w = core.check({**ok, "file": str(d / "elsewhere.mp4")}, A)
        self.assertEqual(p, [])  # another Mac may have it
        self.assertTrue(any("not on this Mac" in x for x in w))
        self.assertTrue(any("not found on this Mac" in x for x in demandgen.upload_problems({**ok, "file": str(d / "elsewhere.mp4")})))

    def test_summaries(self):
        self.assertIn("New Demand Gen campaign DG New (Maximise conversions, 20/day, Paused) with ad group AG, YouTube only",
                      core._summary(dict(type="dg_campaign", new=True, campaign="DG New", budget=20, ad_group="AG", channels="youtube")))
        self.assertEqual(core._summary(AD), "New Demand Gen video ad in DG Brand / YT: Dive the reef (1 video)")
        self.assertIn("Upload to YouTube (Google-managed channel, unlisted): T (v.mp4)", core._summary(dict(type="video_upload", file="/x/v.mp4", title="T")))


class Draft(Base):
    def test_claude_cannot_add_an_upload(self):
        with self.assertRaises(ValueError) as e:
            core.add(A, [dict(type="video_upload", file="/x/v.mp4", title="T")], by="claude")
        self.assertIn("Upload button", str(e.exception))
        self.assertEqual(core.draft(A)["items"], [])
        self.assertEqual(len(core.add(A, [dict(type="video_upload", file="/x/v.mp4", title="T")], by="you")), 1)

    def test_trial_refused_base_campaign_new_is_fine(self):
        exps = experiments.from_api([{"campaign": {"id": "103", "name": "DG trial", "experimentType": "EXPERIMENT"}}])
        with mock.patch.object(experiments, "known", lambda acct: exps):
            for c in (dict(AD, campaign="DG trial", ad_group="T"), dict(type="dg_ad", campaign="DG trial", ad_group="T", ad_id="1", status="paused"),
                      dict(type="dg_ad_group", new=True, campaign="DG trial", ad_group="X"), dict(type="dg_campaign", campaign="DG trial", budget=9)):
                with self.assertRaises(ValueError, msg=c["type"]):
                    core.add(A, [c])
            self.assertEqual(len(core.add(A, [dict(type="dg_campaign", new=True, campaign="DG trial", budget=9, ad_group="A"),
                                              dict(type="video_asset", videos=[NEWVID])])), 2)

    def test_trial_refused_at_push(self):
        p = self.plan(dict(AD, campaign="DG trial", ad_group="T"))
        self.assertFalse(p["ok"])
        self.assertIn(experiments.MSG, p["items"][0]["problems"][0])
        self.assertEqual(self.f.mutates, [])


class Ops(Base):
    def test_new_campaign_full(self):
        p = self.plan(dict(type="dg_campaign", new=True, campaign="DG New", budget=20, bidding="target_cpa", target_cpa=15, ad_group="AG1",
                           locations=[{"id": 2826, "name": "United Kingdom"}], languages="en,de", channels="youtube",
                           start_date="2026-12-01", end_date="2026-12-31"),
                      dict(type="dg_ad_group", new=True, campaign="DG New", ad_group="AG2", channels=["youtube_shorts", "discover"]),
                      dict(AD, campaign="DG New", ad_group="AG1", videos=[VID, f"https://youtu.be/{NEWVID}"], call_to_action="book_now"))
        self.assertTrue(p["ok"], p["items"])
        self.assertFalse(p["spend"])
        ops = self.ops(p)
        b = ops[0]["campaignBudgetOperation"]["create"]
        self.assertEqual((b["amountMicros"], b["explicitlyShared"], b["deliveryMethod"]), ("20000000", False, "STANDARD"))
        c = ops[1]["campaignOperation"]["create"]
        self.assertEqual((c["status"], c["advertisingChannelType"], c["targetCpa"]), ("PAUSED", "DEMAND_GEN", {"targetCpaMicros": "15000000"}))
        self.assertEqual((c["startDate"], c["endDate"], c["campaignBudget"]), ("2026-12-01", "2026-12-31", b["resourceName"]))
        self.assertNotIn("advertisingChannelSubType", c)
        g = ops[2]["adGroupOperation"]["create"]
        self.assertEqual((g["status"], g["campaign"]), ("PAUSED", c["resourceName"]))
        self.assertNotIn("type", g)
        self.assertEqual(g["demandGenAdGroupSettings"]["channelControls"]["selectedChannels"],
                         {"youtubeInStream": True, "youtubeInFeed": True, "youtubeShorts": True, "discover": False, "gmail": False,
                          "display": False, "maps": False})
        crit = [o["adGroupCriterionOperation"]["create"] for o in ops[3:6]]
        self.assertEqual([x.get("language", x.get("location")) for x in crit],
                         [{"languageConstant": "languageConstants/1000"}, {"languageConstant": "languageConstants/1001"},
                          {"geoTargetConstant": "geoTargetConstants/2826"}])
        self.assertTrue(all(x["adGroup"] == g["resourceName"] for x in crit))
        g2 = ops[6]["adGroupOperation"]["create"]
        self.assertEqual(g2["demandGenAdGroupSettings"]["channelControls"]["selectedChannels"]["discover"], True)
        # the ad: the known video reused, the new one created first, the CTA created (only LEARN_MORE exists)
        assets = [o["assetOperation"]["create"] for o in ops if "assetOperation" in o]
        self.assertEqual([a.get("youtubeVideoAsset", a.get("callToActionAsset")) for a in assets],
                         [{"youtubeVideoId": NEWVID}, {"callToAction": "BOOK_NOW"}])
        ad = ops[-1]["adGroupAdOperation"]["create"]
        self.assertEqual((ad["status"], ad["adGroup"]), ("PAUSED", g["resourceName"]))
        self.assertTrue(ad["ad"]["name"].startswith("AG1 video ad "))
        dg = ad["ad"]["demandGenVideoResponsiveAd"]
        self.assertEqual(dg["videos"], [{"asset": f"customers/{A}/assets/900"}, {"asset": assets[0]["resourceName"]}])
        self.assertEqual(dg["logoImages"], [{"asset": f"customers/{A}/assets/950"}])
        self.assertEqual(dg["callToActions"], [{"asset": assets[1]["resourceName"]}])
        self.assertEqual(dg["businessName"], {"text": "Dive trips"})
        self.assertEqual([h["text"] for h in dg["longHeadlines"]], AD["long_headlines"])
        self.assertEqual(ad["ad"]["finalUrls"], ["https://example.com/trips"])
        # one validate-only request, nothing applied
        self.assertEqual([(len(o), apply) for o, apply in self.f.mutates], [(len(ops), False)])

    def test_bidding_schemes(self):
        for bid, want in (("maximize_conversions", {"maximizeConversions": {}}), ("maximize_clicks", {"targetSpend": {}})):
            p = self.plan(dict(type="dg_campaign", new=True, campaign="N " + bid, budget=10, bidding=bid, ad_group="A"))
            c = self.ops(p)[1]["campaignOperation"]["create"]
            self.assertEqual({k: c[k] for k in want}, want)
        p = self.plan(dict(type="dg_campaign", new=True, campaign="N", budget=10, target_cpa=8, ad_group="A"))
        self.assertEqual(self.ops(p)[1]["campaignOperation"]["create"]["maximizeConversions"], {"targetCpaMicros": "8000000"})

    def test_enable_new_ad_groups_and_ads_but_never_campaigns(self):
        p = self.plan(dict(type="dg_campaign", new=True, campaign="N", budget=10, ad_group="A"), dict(AD, campaign="N", ad_group="A"),
                      enable_new=True)
        ops = self.ops(p)
        self.assertEqual(ops[1]["campaignOperation"]["create"]["status"], "PAUSED")
        self.assertEqual(ops[2]["adGroupOperation"]["create"]["status"], "ENABLED")
        self.assertEqual(ops[-1]["adGroupAdOperation"]["create"]["status"], "ENABLED")

    def test_live_budget_minimum(self):
        self.f.min = "6000000"  # Google's live floor is stricter than Studio's table: the push trusts Google
        p = self.plan(dict(type="dg_campaign", new=True, campaign="N", budget=5, ad_group="A"))
        self.assertFalse(p["ok"])
        self.assertIn("budget 5.00 GBP/day is under Demand Gen's minimum of 6.00 GBP/day", p["items"][0]["problems"])
        self.assertEqual(self.f.mutates, [])
        p = self.plan(dict(type="dg_campaign", campaign="DG Brand", budget=5))
        self.assertIn("under Demand Gen's minimum", p["items"][0]["problems"][0])

    def test_edit_campaign(self):
        p = self.plan(dict(type="dg_campaign", campaign="DG Brand", budget=30, status="enabled", target_cpa=12))
        self.assertTrue(p["ok"], p["items"])
        st, bud, cpa = p["ops"]
        self.assertEqual(st["spend"], "enables a campaign")
        self.assertEqual(bud["spend"], "budget raise (+50%)")
        self.assertEqual(bud["op"]["campaignBudgetOperation"]["update"]["amountMicros"], "30000000")
        self.assertEqual(bud["undo"]["campaignBudgetOperation"]["update"]["amountMicros"], "20000000")
        self.assertEqual(cpa["op"]["campaignOperation"]["updateMask"], "maximizeConversions.targetCpaMicros")
        self.assertEqual(cpa["undo"]["campaignOperation"]["update"]["maximizeConversions"], {"targetCpaMicros": "0"})
        p = self.plan(dict(type="dg_campaign", campaign="DG Geo", target_cpa=5))
        self.assertEqual(p["ops"][0]["op"]["campaignOperation"]["update"]["targetCpa"], {"targetCpaMicros": "5000000"})
        self.assertIsNone(p["ops"][0]["spend"])  # lower target CPA

    def test_not_demand_gen(self):
        p = self.plan(dict(type="dg_campaign", campaign="Search X", budget=30))
        self.assertIn('"Search X" is not a Demand Gen campaign', p["items"][0]["problems"])
        p = self.plan(dict(type="dg_ad", campaign="Search X", ad_group="Words", ad_id="303", status="paused"))
        self.assertIn('"Search X" is not a Demand Gen campaign', p["items"][0]["problems"])

    def test_campaign_level_targeting(self):
        p = self.plan(dict(type="dg_ad_group", new=True, campaign="DG Geo", ad_group="New", locations=[{"id": 2826}]))
        self.assertIn("leave locations and languages out", p["items"][0]["problems"][0])
        p = self.plan(dict(type="dg_ad_group", new=True, campaign="DG Geo", ad_group="New", channels="youtube"))
        self.assertTrue(p["ok"])
        self.assertEqual(len(p["ops"]), 1)

    def test_ad_group_edit_is_one_operation(self):
        p = self.plan(dict(type="dg_ad_group", campaign="DG Brand", ad_group="YT", status="paused", channels="youtube"))
        self.assertTrue(p["ok"], p["items"])
        self.assertEqual(len(p["ops"]), 1)
        o = p["ops"][0]
        up = o["op"]["adGroupOperation"]
        self.assertEqual(up["update"]["status"], "PAUSED")
        self.assertIn("status", up["updateMask"].split(","))
        self.assertIn("demandGenAdGroupSettings.channelControls.selectedChannels.maps", up["updateMask"])
        undo = o["undo"]["adGroupOperation"]
        self.assertEqual(undo["update"]["demandGenAdGroupSettings"]["channelControls"]["selectedChannels"]["discover"], True)
        self.assertEqual(undo["update"]["status"], "ENABLED")
        p = self.plan(dict(type="dg_ad_group", campaign="DG Brand", ad_group="YT", channels="google_owned"))
        o = p["ops"][0]
        self.assertEqual(o["op"]["adGroupOperation"]["updateMask"], "demandGenAdGroupSettings.channelControls.channelStrategy")
        self.assertIn("selectedChannels.youtubeShorts", o["undo"]["adGroupOperation"]["updateMask"])
        self.f.channels = {"channelStrategy": "ALL_CHANNELS"}
        o = self.plan(dict(type="dg_ad_group", campaign="DG Brand", ad_group="YT", channels="youtube"))["ops"][0]
        self.assertEqual(o["undo"]["adGroupOperation"]["updateMask"], "demandGenAdGroupSettings.channelControls.channelStrategy")
        self.assertEqual(o["undo"]["adGroupOperation"]["update"]["demandGenAdGroupSettings"]["channelControls"], {"channelStrategy": "ALL_CHANNELS"})

    def test_edit_video_ad(self):
        p = self.plan(dict(type="dg_video_ad", edit=True, campaign="DG Brand", ad_group="YT", ad_id="301", headlines=["New one", "Two"],
                           videos=[VID, NEWVID], final_url="https://example.com/new", status="paused"))
        self.assertTrue(p["ok"], p["items"])
        ops = p["ops"]
        self.assertEqual(ops[0]["op"]["adGroupAdOperation"]["update"]["status"], "PAUSED")
        self.assertEqual(ops[1]["op"]["assetOperation"]["create"]["youtubeVideoAsset"], {"youtubeVideoId": NEWVID})
        up = ops[2]["op"]["adOperation"]
        self.assertEqual(up["update"]["resourceName"], f"customers/{A}/ads/301")
        self.assertEqual(sorted(up["updateMask"].split(",")), ["demandGenVideoResponsiveAd.headlines", "demandGenVideoResponsiveAd.videos", "finalUrls"])
        before = ops[2]["undo"]["adOperation"]["update"]
        self.assertEqual(before["demandGenVideoResponsiveAd"]["headlines"], [{"text": "Old one"}])
        self.assertEqual(before["finalUrls"], ["https://example.com/old"])

    def test_edit_refuses_other_ad_types(self):
        p = self.plan(dict(type="dg_video_ad", edit=True, campaign="DG Brand", ad_group="YT", ad_id="302", headlines=["New"]))
        self.assertIn("only video ads can be edited here", p["items"][0]["problems"][0])
        p = self.plan(dict(type="dg_ad", campaign="DG Brand", ad_group="YT", ad_id="302", status="enabled"))
        self.assertTrue(p["ok"])
        self.assertTrue(p["spend"])
        p = self.plan(dict(type="dg_ad", campaign="DG Brand", ad_group="YT", ad_id="999", status="enabled"))
        self.assertIn("ad 999 not found live", p["items"][0]["problems"][0])

    def test_logo_checks_at_push(self):
        p = self.plan(dict(AD, logo_asset_id="951"))
        self.assertIn("must be square", p["items"][0]["problems"][0])
        p = self.plan(dict(AD, logo_asset_id="952"))
        self.assertIn("is not an image asset", p["items"][0]["problems"][0])
        d = Path(_tf.mkdtemp())
        (d / "logo.png").write_bytes(png(200, 200))
        p = self.plan({**{k: v for k, v in AD.items() if k != "logo_asset_id"}, "logo_file": str(d / "logo.png")})
        self.assertTrue(p["ok"], p["items"])
        img = self.ops(p)[0]["assetOperation"]["create"]
        self.assertEqual(img["type"], "IMAGE")
        self.assertTrue(img["imageAsset"]["data"])

    def test_video_asset(self):
        p = self.plan(dict(type="video_asset", videos=[{"id": f"https://youtu.be/{NEWVID}", "name": "Teaser"}, VID2]))
        self.assertTrue(p["ok"], p["items"])
        self.assertEqual([o["assetOperation"]["create"]["youtubeVideoAsset"]["youtubeVideoId"] for o in self.ops(p)], [NEWVID, VID2])
        self.assertEqual(self.ops(p)[0]["assetOperation"]["create"]["name"], "Teaser")
        self.assertEqual([o["undo"] for o in p["ops"]], [None, None])  # assets stay in the library
        p = self.plan(dict(type="video_asset", videos=[VID]))
        self.assertIn(f"video {VID} is already in the account's asset library", p["items"][0]["problems"])

    def test_google_refusal_goes_to_the_item(self):
        self.f.ok = False
        p = self.plan(dict(type="dg_ad", campaign="DG Brand", ad_group="YT", ad_id="302", status="paused"))
        self.assertFalse(p["ok"])
        self.assertIn("Google: nope", p["items"][0]["problems"])


class PushUndo(Base):
    def test_push_and_undo(self):
        core.add(A, [dict(type="dg_campaign", new=True, campaign="N", budget=10, ad_group="A", languages="en"), dict(AD, campaign="N", ad_group="A")])
        r = live.push(A, accept_spend=True)
        self.assertTrue(r["ok"], r)
        self.assertEqual([apply for _, apply in self.f.mutates], [False, True])
        self.assertEqual(core.draft(A)["items"], [])
        h = live.history(A)[0]
        self.assertTrue(h["can_undo"])
        n = len(self.f.mutates[-1][0])
        u = live.undo(A, h["id"])
        self.assertTrue(u["ok"], u)
        removes = [next(iter(o.values())) for o in self.f.mutates[-1][0]]
        self.assertTrue(all("remove" in x for x in removes))
        self.assertEqual(len(removes), n - 1)  # the budget stays (as for Search campaigns)
        self.assertTrue(any("budget" in x for x in u["not_undone"]))

    def test_uploads_stay_out_of_push_and_editor(self):
        d = Path(_tf.mkdtemp())
        (d / "v.mp4").write_bytes(b"\0" * 10)
        core.add(A, [dict(type="video_upload", file=str(d / "v.mp4"), title="T"), dict(type="dg_ad", campaign="DG Brand", ad_group="YT", ad_id="302",
                                                                                         status="paused")])
        pv = live.preview(A)
        self.assertEqual([e["summary"] for e in pv["items"]], ["Demand Gen ad 302 in DG Brand / YT: paused"])
        self.assertTrue(pv["ok"])
        r = live.push(A)
        self.assertTrue(r["ok"])
        left = core.draft(A)["items"]
        self.assertEqual([i["change"]["type"] for i in left], ["video_upload"])
        self.assertFalse(any("youTubeVideoUpload" in json.dumps(o) for o, _ in self.f.mutates))
        r = live.push(A)
        self.assertFalse(r["ok"])  # nothing left to push: the upload is never pushed
        with mock.patch.object(core, "validate", lambda acct: []):
            out = core.push(A)
        self.assertFalse(out["ok"])
        self.assertIn("The draft is empty", out["output"])


class Library(Base):
    def test_library_reads_and_caches(self):
        rows = {
            "FROM asset WHERE asset.type = YOUTUBE_VIDEO": [{"asset": {"id": "900", "resourceName": f"customers/{A}/assets/900", "name": "",
                                                                       "youtubeVideoAsset": {"youtubeVideoId": VID, "youtubeVideoTitle": ""}}}],
            "FROM video": [{"video": {"id": VID, "title": "Reef film", "durationMillis": "61500"}}],
            "FROM you_tube_video_upload": [{"youTubeVideoUpload": {"resourceName": f"customers/{A}/youTubeVideoUploads/5", "videoUploadId": "5",
                                                                  "videoId": VID, "state": "PROCESSED", "videoPrivacy": "UNLISTED"}}],
            "FROM campaign WHERE": [{"campaign": {"id": "101", "name": "DG Brand", "status": "PAUSED", "biddingStrategyType": "TARGET_CPA",
                                                  "experimentType": "BASE"}, "campaignBudget": {"amountMicros": "40000000"}},
                                    {"campaign": {"id": "103", "name": "DG trial", "status": "ENABLED", "experimentType": "EXPERIMENT"}}],
            "FROM ad_group WHERE": [{"adGroup": {"id": "201", "name": "YT", "status": "ENABLED", "demandGenAdGroupSettings": {"channelControls": {
                "selectedChannels": {"youtubeInStream": True, "youtubeInFeed": True, "youtubeShorts": True}}}}, "campaign": {"id": "101"}}],
            "FROM ad_group_ad WHERE": [{"adGroupAd": {"status": "ENABLED", "ad": {"id": "301", "type": "DEMAND_GEN_VIDEO_RESPONSIVE_AD", "finalUrls": ["https://e.com"],
                                                                                  "demandGenVideoResponsiveAd": {"headlines": [{"text": "H"}], "videos": [{"asset": f"customers/{A}/assets/900"}],
                                                                                                                 "logoImages": [{"asset": f"customers/{A}/assets/950"}]}}},
                                        "adGroup": {"id": "201"}, "campaign": {"id": "101"}}],
            "FROM asset WHERE asset.type = IMAGE": [{"asset": {"id": "950", "name": "Logo", "imageAsset": {"fullSize": {"widthPixels": "512", "heightPixels": "512", "url": "u"}}}},
                                                     {"asset": {"id": "951", "name": "Wide", "imageAsset": {"fullSize": {"widthPixels": "600", "heightPixels": "300"}}}}],
            "FROM asset WHERE asset.type = CALL_TO_ACTION": [{"asset": {"id": "970", "callToActionAsset": {"callToAction": "LEARN_MORE"}}}],
        }
        calls = []

        def q(acct, g):
            calls.append(g)
            return next(v for k, v in rows.items() if k in g)
        with mock.patch.object(live, "query", q):
            lib = demandgen.library(A, refresh=True)
            n = len(calls)
            again = demandgen.library(A)
        self.assertEqual(len(calls), n, "cached until a refresh")
        self.assertEqual(again, lib)
        self.assertTrue(all(g.startswith("SELECT ") for g in calls))
        self.assertEqual(lib["videos"], [{"asset_id": "900", "video_id": VID, "name": "", "title": "Reef film", "duration_s": 62}])
        self.assertEqual(lib["uploads"][0]["state"], "PROCESSED")
        self.assertEqual([c["trial"] for c in lib["campaigns"]], [False, True])
        self.assertEqual(lib["ad_groups"][0]["channels"], "YouTube only")
        self.assertEqual(lib["ads"][0]["videos"], [VID])
        self.assertEqual(lib["ads"][0]["logo_asset_id"], "950")
        self.assertEqual([x["asset_id"] for x in lib["logos"]], ["950"])
        self.assertEqual(lib["ctas"], ["learn_more"])

    def test_video_info(self):
        def q(acct, g):
            if "FROM video WHERE" in g:
                return [{"video": {"id": VID, "title": "Reef", "durationMillis": "15000"}}]
            return []
        with mock.patch.object(live, "query", q):
            r = demandgen.video_info(A, f"https://youtu.be/{VID}")
        self.assertEqual((r["video_id"], r["title"], r["duration_s"], r["in_library"]), (VID, "Reef", 15, False))
        with mock.patch.object(live, "query", lambda a, g: []):
            r = demandgen.video_info(A, NEWVID)
        self.assertEqual((r["known"], r["title"]), (False, ""))
        with self.assertRaises(ValueError):
            demandgen.video_info(A, "nope")


class Mcp(Base):
    def test_tools(self):
        import mcp_server
        names = {t["name"] for t in mcp_server.TOOLS}
        self.assertTrue({"video_assets_list", "demandgen_add_to_draft"} <= names)
        self.assertFalse(any("upload" in n for n in names), "no tool uploads anything")
        items = mcp_server.call("demandgen_add_to_draft", {"account": A, "changes": [AD], "note": "why"})
        self.assertEqual((items[0]["by"], items[0]["problems"]), ("claude", []))
        for bad in ([dict(type="video_upload", file="/x/v.mp4", title="T")], [{"type": "campaign", "campaign": "X", "status": "paused"}]):
            with self.assertRaises(ValueError):
                mcp_server.call("demandgen_add_to_draft", {"account": A, "changes": bad})
        with self.assertRaises(ValueError):
            mcp_server.call("add_to_draft", {"account": A, "changes": [dict(type="video_upload", file="/x/v.mp4", title="T")]})
        self.assertEqual(len(core.draft(A)["items"]), 1)
        with mock.patch.object(demandgen, "library", lambda acct, refresh=False: {"account": acct, "fetched_at": "t", "errors": {}, "videos": [1], "ads": [2]}):
            self.assertEqual(mcp_server.call("video_assets_list", {"account": A, "part": "videos"})["videos"], [1])
        self.assertEqual(self.f.mutates, [], "draft only")


if __name__ == "__main__":
    unittest.main()
