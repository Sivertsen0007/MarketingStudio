"""v0.2.3 S7 hardening: a failed Keychain read is remembered for a short while, a leftover plain secrets.json is named,
EMAIL_MERGE_TAGS is in Setup, the Overview keeps stale rows per failed account with their real fetch time, the MCP
audience tool refuses age_range, meta_desc hints, Studio-made Facebook videos are not "Outside Studio", a short
negative cache on the published-posts read, status codes as whole numbers in plain errors, and Email's send zone.

Offline: temp MSTUDIO_HOME, MSTUDIO_KEYCHAIN=0 except where a test turns it on with `security` faked. The real
Keychain, Google, Meta, WordPress and the network are never touched.
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("MSTUDIO_HOME", _tf.mkdtemp(prefix="mstudio-test-"))
_os.environ["MSTUDIO_KEYCHAIN"] = "0"
_os.environ["GAE_EDITOR_DIR"] = _tf.mkdtemp(prefix="gae-no-editor-")

import datetime as dt
import json
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

import appconfig  # noqa: E402
import mcp_server  # noqa: E402
import overview  # noqa: E402
import shared_calendar  # noqa: E402
from areas import _social_meta as sm  # noqa: E402
from areas import activity  # noqa: E402
from areas import email as em  # noqa: E402


class KeychainHold(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(_os.environ, {"MSTUDIO_KEYCHAIN": "1"})
        self.env.start()
        appconfig.SECRETS_FILE.unlink(missing_ok=True)
        appconfig._cache.clear()
        appconfig._kc_ok()

    def tearDown(self):
        self.env.stop()
        appconfig.SECRETS_FILE.unlink(missing_ok=True)
        appconfig._cache.clear()
        appconfig._kc_ok()

    def test_failure_remembered_then_asked_again(self):
        calls = []

        def hang(*a, **kw):
            calls.append(a)
            raise subprocess.TimeoutExpired("security", 10)
        with mock.patch("appconfig.shutil.which", return_value="/usr/bin/security"), \
                mock.patch("appconfig.subprocess.run", side_effect=hang):
            self.assertEqual(appconfig.get("WP_STUDIO_APP_PASSWORD"), "")
            st = appconfig.status()  # many secrets: none of them asks the Keychain again
            self.assertEqual(len(calls), 1)
            self.assertIn("10 seconds", st["keychain_error"])
            with self.assertRaises(appconfig.KeychainError):  # saving is the person's own action: it always asks
                appconfig.set_secret("TEAM_DEVICE_TOKEN", "x")
            self.assertEqual(len(calls), 2)
        found = subprocess.CompletedProcess(["security"], 0, "pw\n", "")
        with mock.patch("appconfig.shutil.which", return_value="/usr/bin/security"), \
                mock.patch("appconfig.subprocess.run", return_value=found), \
                mock.patch("appconfig.time.time", return_value=time.time() + appconfig._KC_HOLD + 1):
            self.assertEqual(appconfig.get("WP_STUDIO_APP_PASSWORD"), "pw")  # the hold is over: asked again
            self.assertIsNone(appconfig.status()["keychain_error"])

    def test_leftover_plain_file_is_named(self):
        appconfig.SECRETS_FILE.write_text(json.dumps({"META_ACCESS_TOKEN": "old-plain"}))
        done = subprocess.CompletedProcess(["security"], 44, "", "")
        with mock.patch("appconfig.shutil.which", return_value="/usr/bin/security"), \
                mock.patch("appconfig.subprocess.run", return_value=done):
            st = appconfig.status()
        self.assertIn("secrets.json", st["keychain_error"])
        self.assertNotIn("old-plain", json.dumps(st))
        self.assertFalse(any(k["set"] for k in st["keys"] if k["name"] == "META_ACCESS_TOKEN"))  # never read from it

    def test_no_note_with_keychain_off(self):
        _os.environ["MSTUDIO_KEYCHAIN"] = "0"
        appconfig.SECRETS_FILE.write_text("{}")
        self.assertIsNone(appconfig.status()["keychain_error"])

    def test_merge_tags_in_setup(self):
        row = next(k for k in appconfig.KEYS if k[0] == "EMAIL_MERGE_TAGS")
        self.assertEqual((row[2], row[3]), ("Email", False))


class OverviewStale(unittest.TestCase):
    PREV = [{"channel": "Google", "account": "ML", "campaign": "Red Sea", "status": "Enabled", "currency": "GBP", "spend": 10.0,
             "clicks": 5, "impressions": 50, "results": 1.0, "select_cabin": 1.0},
            {"channel": "Google", "account": "DT", "campaign": "Maldives G", "status": "Enabled", "currency": "USD", "spend": 3.0,
             "clicks": 1, "impressions": 9, "results": 0.0, "select_cabin": 0.0},
            {"channel": "Meta", "account": "ML Meta", "campaign": "Maldives", "status": "Active", "currency": "USD", "spend": 20.0,
             "clicks": 7, "impressions": 70, "results": 2.0, "select_cabin": 0}]

    def build(self, old, google, meta_rows=(), meta_fail=False):
        def m(days, failed=None):
            if meta_fail:
                failed.append("ML Meta")
                return [], ["Meta: network down"]
            return list(meta_rows), []
        with mock.patch.object(overview, "_google", side_effect=google), mock.patch.object(overview, "_meta", side_effect=m), \
                mock.patch.object(overview, "_area_blocks", return_value=[]):
            return overview._build("7", old)

    def test_account_list_failure_keeps_google_rows(self):
        real = overview._google
        with mock.patch.object(overview.gapi, "accounts_any", side_effect=RuntimeError("Not signed in to Google.")):
            out = self.build({"campaigns": json.loads(json.dumps(self.PREV)), "fetched_at": "2026-11-26T09:05:00"}, real)
        self.assertEqual(sorted(r["campaign"] for r in out["campaigns"] if r["channel"] == "Google"), ["Maldives G", "Red Sea"])
        self.assertTrue(all(r["stale"] for r in out["campaigns"] if r["channel"] == "Google"))
        self.assertFalse(any(r["channel"] == "Meta" for r in out["campaigns"]))  # Meta read fine (no rows): nothing kept

    def test_per_account_and_real_fetch_time(self):
        def g(days, failed=None):  # ML fails, DT reads fine
            failed.append("ML")
            return [{**self.PREV[1], "spend": 4.0}], ["Google ML: quota"]
        old = {"campaigns": json.loads(json.dumps(self.PREV)), "fetched_at": "2026-11-26T09:05:00"}
        first = self.build(old, g, meta_fail=True)
        rs = {r["campaign"]: r for r in first["campaigns"]}
        self.assertEqual(rs["Maldives G"]["spend"], 4.0)
        self.assertFalse(rs["Maldives G"].get("stale"))
        self.assertTrue(rs["Red Sea"]["stale"] and rs["Maldives"]["stale"])
        self.assertTrue(any("ML: could not be read now, showing the figures fetched 26 November 2026 09:05" in e for e in first["errors"]))
        # a second failure an hour later: the figures are still the 09:05 ones, not the 10:05 Overview's
        second = self.build({**first, "fetched_at": "2026-11-26T10:05:00"}, g, meta_fail=True)
        self.assertTrue(any("ML Meta: could not be read now, showing the figures fetched 26 November 2026 09:05" in e
                            for e in second["errors"]))
        self.assertFalse(any("10:05" in e for e in second["errors"]))



class Mcp(unittest.TestCase):
    def test_audience_tool_refuses_age_range(self):
        with mock.patch.object(mcp_server.meta, "use"), mock.patch.object(mcp_server.meta, "add", return_value={"ok": True}) as add:
            with self.assertRaises(ValueError):
                mcp_server.call_audiences("audience_add_to_draft", {"account": "meta", "changes": [
                    {"type": "meta_targeting", "adset": "A", "audiences_add": ["x"], "age_range": [25, 45]}]})
            add.assert_not_called()
            mcp_server.call_audiences("audience_add_to_draft", {"account": "meta", "changes": [
                {"type": "meta_targeting", "adset": "A", "audiences_add": ["x"]}]})
            add.assert_called_once()

    def test_meta_desc_hint(self):
        tools = {t["name"]: t for t in mcp_server.TOOLS}
        for n in ("seo_write_page", "seo_add_draft"):
            d = tools[n]["description"]
            self.assertIn("120-156", d, n)
            self.assertIn("yoast-rest-fields.php", d, n)
        self.assertNotIn("70-160", json.dumps(list(tools.values())))


class OutsideStudio(unittest.TestCase):
    def test_studio_video_is_not_outside(self):
        now = dt.datetime(2026, 11, 20, 12, tzinfo=dt.timezone.utc)
        posts = [{"id": "p1", "results": {"facebook": {"post_id": "998877", "object": "video", "status": "published",
                                                       "url": "https://www.facebook.com/123/videos/998877"}}}]
        live = {"posts": [
            {"id": "123_998877", "channel": "facebook", "when": "2026-11-20T12:00:00+00:00", "url": "https://www.facebook.com/123/videos/998877/"},
            {"id": "123_555", "channel": "facebook", "when": "2026-11-20T12:00:00+00:00", "url": "https://www.facebook.com/reel/998877"},
            {"id": "123_777", "channel": "facebook", "when": "2026-11-20T12:00:00+00:00", "url": "https://www.facebook.com/123/posts/1998877"},
            {"id": "17_998877", "channel": "instagram", "when": "2026-11-20T12:00:00+00:00", "url": "https://instagram.com/p/x"}],
            "scheduled": []}
        with mock.patch.object(shared_calendar, "_live_posts", return_value=live):
            out = shared_calendar._social_outside(now - dt.timedelta(days=1), now + dt.timedelta(days=1), posts)
        self.assertEqual(sorted(x["id"] for x in out), ["123_777", "17_998877"])  # a longer number is another post


class PublishedNegativeCache(unittest.TestCase):
    def test_failure_reused_briefly_refresh_asks(self):
        sm._FAILED["c"] = None
        n = []

        def fb(limit):
            n.append(1)
            raise sm.meta_api.MetaError({"message": "down"})
        with mock.patch.object(sm, "_fb_posts", fb), mock.patch.object(sm, "_ig_posts", lambda limit: []), \
                mock.patch.object(sm, "_fb_scheduled", lambda: []), mock.patch.object(sm, "_fresh", lambda *a: False):
            a = sm.published()
            b = sm.published()
            self.assertEqual(len(n), 1)
            self.assertIs(a, b)
            self.assertTrue(a["errors"])
            sm.published(0)  # Refresh
            self.assertEqual(len(n), 2)
            sm._FAILED["t"] -= sm.FAIL_TTL + 1
            sm.published()
            self.assertEqual(len(n), 3)
        sm._FAILED["c"] = None


class PlainError(unittest.TestCase):
    def test_codes_are_whole_numbers(self):
        self.assertIn("not found", activity.plain_error("Google: HTTP Error 404: Not Found"))
        self.assertIn("refused or has expired", activity.plain_error("Meta: (#401) bad"))
        self.assertEqual(activity.plain_error("Meta: campaign 1404233 has no budget"), "Meta: campaign 1404233 has no budget")
        self.assertNotIn("service had a problem", activity.plain_error("Activity: took 1500 ms, 3 rows skipped"))
        self.assertIn("service had a problem", activity.plain_error("Email: HTTP 503 Service Unavailable"))


class EmailZone(unittest.TestCase):
    def tearDown(self):
        _os.environ.pop("EMAIL_TIMEZONE", None)
        em._load_test_address()

    def test_send_zone_in_state_and_default(self):
        _os.environ["EMAIL_TIMEZONE"] = "Asia/Bangkok"
        em._load_test_address()
        a = em.adapter()
        self.assertEqual(em._source(a)["send_tz"], "Asia/Bangkok")
        seen = {}
        with mock.patch.object(em, "_team", side_effect=lambda body, what, fn: seen.setdefault("ok", True)), \
                mock.patch.object(em, "_now", return_value=dt.datetime(2026, 11, 26, 0, 0, tzinfo=dt.timezone.utc)), \
                mock.patch.object(em, "to_utc", wraps=em.to_utc) as tu:
            em.p_schedule({"id": "1", "when": "2026-11-27T09:00", "confirm": em.CONFIRM})
        self.assertEqual(tu.call_args[0][1], "Asia/Bangkok")
        self.assertEqual(em.to_utc("2026-11-27T09:00", "Asia/Bangkok"), dt.datetime(2026, 11, 27, 2, 0, tzinfo=dt.timezone.utc))

    def test_merge_tags_cache_key(self):
        src = (HERE / "areas" / "email.py").read_text()
        self.assertIn('self._cached("merge_tags2", get)', src)


if __name__ == "__main__":
    unittest.main()
