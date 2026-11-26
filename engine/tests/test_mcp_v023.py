"""v0.2.3 S4: MCP without Google Ads Editor (U1), add_to_draft refusing unreadable changes (U28), cache-only read
tools and honest descriptions (U12), Keychain failures never fall back to a plain file (K1), honest refresh results
(U5), Overview area refresh without a Google / Meta rebuild (U6) and the area tile contract (C24).

Offline: temp MSTUDIO_HOME, an empty GAE_EDITOR_DIR (no Editor), MSTUDIO_KEYCHAIN=0 except where a test turns the
Keychain on with `security` faked. The real Keychain, Google, Meta and the network are never touched.
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("MSTUDIO_HOME", _tf.mkdtemp(prefix="mstudio-test-"))
_os.environ["MSTUDIO_KEYCHAIN"] = "0"
_os.environ["GAE_EDITOR_DIR"] = _tf.mkdtemp(prefix="gae-no-editor-")

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
import core  # noqa: E402
import datapolicy  # noqa: E402
import datasources  # noqa: E402
import demandgen  # noqa: E402
import gae  # noqa: E402
import mcp_server  # noqa: E402
import overview  # noqa: E402

ML = gae.ACCOUNTS["ml"]
EM = chr(0x2014)


def rpc(msgs):
    """Run the MCP server as Claude Code does (stdio) with no Editor; -> the replies by id."""
    p = subprocess.run([sys.executable, str(HERE / "mcp_server.py")], input="\n".join(json.dumps(m) for m in msgs) + "\n",
                       capture_output=True, text=True, timeout=120, env=dict(_os.environ))
    return {r["id"]: r for r in (json.loads(x) for x in p.stdout.splitlines() if x.strip())}, p.returncode


def tool(i, name, args=None):
    return {"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": name, "arguments": args or {}}}


def text(r):
    return r["result"]["content"][0]["text"]


class NoEditor(unittest.TestCase):
    def test_server_stays_up_without_editor(self):
        out, rc = rpc([{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                       tool(2, "list_accounts"), tool(3, "check_draft"), tool(4, "send_draft_to_editor", {"user_confirmed": True}),
                       tool(5, "get_draft"), {"jsonrpc": "2.0", "id": 6, "method": "ping"}])
        self.assertEqual(rc, 0)
        self.assertEqual(sorted(out), [1, 2, 3, 4, 5, 6])  # every request answered, nothing after list_accounts lost
        accts = json.loads(text(out[2]))
        self.assertEqual({a["key"] for a in accts if not str(a.get("id", "")).startswith("meta")}, set(gae.ACCOUNTS))
        self.assertTrue(out[3]["result"]["isError"])
        self.assertIn("preview_live_push", text(out[3]))
        self.assertIn("Push live", text(out[4]))
        self.assertIn("items", json.loads(text(out[5])))

    def test_no_editor_is_both_kinds_of_error(self):
        with self.assertRaises(RuntimeError):  # caught by `except Exception` in long-running servers
            gae.editor_version_dir()
        with self.assertRaises(SystemExit):  # the gae CLI still just prints and stops
            gae.editor_version_dir()

    def test_tool_error_survives_sys_exit(self):
        with mock.patch.object(mcp_server, "call", side_effect=SystemExit("Google Ads Editor is open.")):
            r = mcp_server.handle(tool(1, "list_accounts"))
        self.assertTrue(r["isError"])
        self.assertEqual(r["content"][0]["text"], "Error: Google Ads Editor is open.")


class AddToDraft(unittest.TestCase):
    def setUp(self):
        core.clear(ML)

    def test_unreadable_changes_are_refused_and_nothing_is_stored(self):
        for bad, words in (([{"type": "bogus"}], "unknown type 'bogus'"), ([{"campaign": "X"}], "has no type"),
                           ([{"type": "keyword", "campaign": "X"}], "required field"), (["x"], "not an object"),
                           ([{"type": "callout", "campaign": "X", "text": "ok"}, {"type": "bogus"}], "change 2")):
            with self.assertRaises(ValueError) as cm:
                mcp_server.call("add_to_draft", {"changes": bad})
            self.assertIn(words, str(cm.exception))
            self.assertIn("Nothing added", str(cm.exception))
        self.assertEqual(core.draft(ML)["items"], [])

    def test_rule_problems_are_kept(self):
        items = mcp_server.call("add_to_draft", {"changes": [{"type": "callout", "campaign": "X", "text": "x" * 40}]})
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0]["problems"])  # stored, with its problem shown to the user
        self.assertEqual(len(core.draft(ML)["items"]), 1)

    def test_changes_as_json_text(self):
        items = mcp_server.call("add_to_draft", {"changes": json.dumps([{"type": "campaign", "campaign": "X", "budget": 5}])})
        self.assertEqual(items[0]["problems"], [])


class CacheOnly(unittest.TestCase):
    def test_google_grid_without_cache_fetches_nothing(self):
        with mock.patch("gapi._save", side_effect=AssertionError("fetched")):
            with self.assertRaises(mcp_server.NotCached) as cm:
                mcp_server.call("list_entities", {"kind": "campaigns"})
            with self.assertRaises(mcp_server.NotCached):
                mcp_server.call("audiences_list", {})
        self.assertIn("refresh_data", str(cm.exception))

    def test_video_library_cache_only(self):
        f = demandgen._cache(ML)
        f.unlink(missing_ok=True)
        with mock.patch.object(demandgen, "_live_mod", side_effect=AssertionError("fetched")):
            with self.assertRaises(mcp_server.NotCached):
                mcp_server.call("video_assets_list", {})
            f.write_text(json.dumps({"account": ML, "fetched_at": "2026-11-26T10:00:00", "errors": {}, "videos": [{"id": "1"}]}))
            v = mcp_server.call("video_assets_list", {"part": "videos"})
        self.assertEqual(v["videos"], [{"id": "1"}])
        f.unlink()

    def test_descriptions(self):
        r = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertIn("Drafts and team comments only", r["instructions"])
        self.assertIn("list_accounts", r["instructions"])
        tools = {t["name"]: t for t in mcp_server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["tools"]}
        self.assertNotIn("downloaded in Editor", tools["list_accounts"]["description"])
        self.assertNotIn("ml or mluk", tools["refresh_data"]["description"])
        for n in ("meta_tests_list", "meta_test_results", "video_assets_list", "audiences_list"):
            self.assertIn("fetches nothing", tools[n]["description"], n)
        for n in ("team_sync", "comment_add", "review_request", "setup_import_env_file"):
            self.assertIn(n, tools)
        self.assertNotIn(EM, json.dumps(list(tools.values())) + r["instructions"])
        readme = (HERE.parent / "README.md").read_text()
        for n in ("team_status", "team_sync", "reviews_list", "review_request", "team_activity", "comments_list", "comment_add",
                  "mentions_list", "notifications_list"):
            self.assertIn(f"`{n}`", readme, n)


class Keychain(unittest.TestCase):
    """MSTUDIO_KEYCHAIN on, `security` faked: never the real Keychain, never a quiet plain-text fallback."""

    def setUp(self):
        self.env = mock.patch.dict(_os.environ, {"MSTUDIO_KEYCHAIN": "1"})
        self.env.start()
        appconfig.SECRETS_FILE.unlink(missing_ok=True)
        appconfig._cache.clear()
        appconfig._KC["error"] = None

    def tearDown(self):
        self.env.stop()
        appconfig._cache.clear()
        appconfig._KC["error"] = None

    def test_no_security_tool_refuses_to_save(self):
        with mock.patch("appconfig.shutil.which", return_value=None), \
                mock.patch("appconfig.subprocess.run", side_effect=AssertionError("ran security")):
            with self.assertRaises(appconfig.KeychainError) as cm:
                appconfig.set_many({"META_ACCESS_TOKEN": "tok-secret"})
            self.assertIn("not saved", str(cm.exception))
            self.assertEqual(appconfig.get("META_ACCESS_TOKEN"), "")
            st = appconfig.status()
        self.assertFalse(appconfig.SECRETS_FILE.exists())  # no plain-text fallback
        self.assertTrue(st["keychain_error"])
        self.assertNotIn("tok-secret", json.dumps(st))

    def test_timeout_is_reported_not_swallowed(self):
        with mock.patch("appconfig.shutil.which", return_value="/usr/bin/security"), \
                mock.patch("appconfig.subprocess.run", side_effect=subprocess.TimeoutExpired("security", 10)):
            self.assertEqual(appconfig.get("WP_STUDIO_APP_PASSWORD"), "")
            self.assertNotIn("WP_STUDIO_APP_PASSWORD", appconfig._cache)  # asked again next time
            self.assertIn("10 seconds", appconfig.status()["keychain_error"])
            with self.assertRaises(appconfig.KeychainError):
                appconfig.set_secret("TEAM_DEVICE_TOKEN", "x")
        self.assertFalse(appconfig.SECRETS_FILE.exists())

    def test_not_found_is_not_an_error(self):
        done = subprocess.CompletedProcess(["security"], 44, "", "")
        with mock.patch("appconfig.shutil.which", return_value="/usr/bin/security"), \
                mock.patch("appconfig.subprocess.run", return_value=done):
            self.assertEqual(appconfig.get("LINKEDIN_ACCESS_TOKEN", "none"), "none")
            self.assertIsNone(appconfig.status()["keychain_error"])

    def test_plain_file_only_with_keychain_off(self):
        _os.environ["MSTUDIO_KEYCHAIN"] = "0"
        appconfig.set_many({"LINKEDIN_ACCESS_TOKEN": "li-tok"})
        self.assertEqual(json.loads(appconfig.SECRETS_FILE.read_text())["LINKEDIN_ACCESS_TOKEN"], "li-tok")


class Refresh(unittest.TestCase):
    def test_fx_failure_is_reported(self):
        with mock.patch("fx._fetch", return_value=None):
            fx_file = core.STUDIO_DIR / "fx.json"
            fx_file.unlink(missing_ok=True)
            r = datasources.refresh("fx")["results"][0]
            self.assertIn("built-in", r["errors"][0])
            core.STUDIO_DIR.mkdir(parents=True, exist_ok=True)
            fx_file.write_text(json.dumps({"base": "EUR", "date": "2026-11-09", "rates": {"EUR": 1.0, "GBP": 0.85}, "source": "ECB",
                                           "fetched_ts": time.time() - 99999, "fetched_at": "2026-11-09T10:00"}))
            r = datasources.refresh("fx")["results"][0]
        self.assertTrue(r["stale"])
        self.assertIn("9 November 2026", r["errors"][0])

    def test_fx_success_has_no_errors(self):
        with mock.patch("fx._fetch", return_value={"base": "EUR", "date": "2026-11-26", "rates": {"EUR": 1.0, "GBP": 0.85}, "host": "fake"}):
            r = datasources.refresh("fx")["results"][0]
        self.assertEqual(r["errors"], [])

    def test_demo_email_is_not_fresh_data(self):
        r = datasources.refresh("email")["results"][0]
        if r.get("demo"):
            self.assertIn("demo data", r["errors"][0])


class Overview(unittest.TestCase):
    CAMPS = [{"channel": "Google", "account": "ML", "campaign": "Red Sea", "status": "Enabled", "currency": "GBP", "spend": 10.0,
              "clicks": 5, "impressions": 50, "results": 1.0, "select_cabin": 1.0, "destination": "Egypt / Red Sea"},
             {"channel": "Meta", "account": "ML Meta", "campaign": "Maldives", "status": "Active", "currency": "USD", "spend": 20.0,
              "clicks": 7, "impressions": 70, "results": 2.0, "select_cabin": 0, "destination": "Maldives"}]

    def write_cache(self, at):
        raw = {"days": 7, "totals": [{"channel": "Google", "currency": "GBP", "spend": 10.0, "clicks": 5, "results": 1.0, "select_cabin": 1.0,
                                     "campaigns": 1, "cost_per_select_cabin": 10.0}], "destinations": [], "campaigns": self.CAMPS,
               "areas": [{"title": "Email", "area": "email", "tiles": [{"k": "Old", "v": "1", "hi": 0}]},
                         {"title": "SoMe", "area": "social", "tiles": [{"k": "Posts", "v": "2", "hi": "Facebook 1 · Instagram 1"}]}],
               "errors": [], "fetched_at": "2026-11-26T09:00:00", "_at": at}
        overview._write_cache("7", raw)
        return raw

    def test_area_refresh_keeps_google_and_meta(self):
        t0 = time.time() - 60
        self.write_cache(t0)
        datapolicy.invalidate("email")  # what an Email refresh records
        fake = {"title": "Email", "tiles": [{"k": "Campaigns sent", "v": "9", "hi": 1}], "note": ""}
        import areas
        with mock.patch.object(overview, "_google", side_effect=AssertionError("asked Google")), \
                mock.patch.object(overview, "_meta", side_effect=AssertionError("asked Meta")), \
                mock.patch.object(areas.AREAS["email"], "overview", return_value=fake):
            o = overview.build("7")
        self.assertEqual([c["campaign"] for c in o["campaigns"]], ["Red Sea", "Maldives"])
        em = next(b for b in o["areas"] if b["title"] == "Email")
        self.assertEqual(em["tiles"][0]["v"], "9")
        self.assertTrue(em["tiles"][0]["hi"])
        self.assertNotIn("email", o["newer_sources"])
        so = next(b for b in o["areas"] if b["title"] == "SoMe")  # C24: breakdown text is the title, not a highlight
        self.assertEqual(so["tiles"][0], {"k": "Posts", "v": "2", "hi": False, "title": "Facebook 1 · Instagram 1"})

    def test_area_refresh_does_not_invalidate_overview(self):
        self.write_cache(time.time() - 60)
        before = datapolicy.refreshed_at("overview")
        datasources.refresh("email")
        self.assertEqual(datapolicy.refreshed_at("overview"), before)

    def test_failed_rebuild_keeps_previous_channel_figures(self):
        self.write_cache(time.time() - 60)

        def g(days, failed=None):
            failed.append("ML")
            return [], ["Google ML: Not signed in to Google."]
        with mock.patch.object(overview, "_google", side_effect=g), \
                mock.patch.object(overview, "_meta", return_value=([], ["Meta: network down"])):
            o = overview.build("7", force=True)
        self.assertEqual(sorted(c["campaign"] for c in o["campaigns"]), ["Maldives", "Red Sea"])
        self.assertTrue(all(c.get("stale") for c in o["campaigns"]))
        self.assertTrue(any("showing the figures fetched" in e for e in o["errors"]))
        self.assertEqual(sum(t["campaigns"] for t in o["totals"]), 2)

    def test_tile_contract(self):
        self.assertEqual(overview.tile({"k": "a", "v": "1", "hi": "x · y"}), {"k": "a", "v": "1", "hi": False, "title": "x · y"})
        self.assertEqual(overview.tile({"k": "a", "v": "1", "hi": 1})["hi"], True)
        self.assertEqual(overview.tile({"k": "a", "v": "1", "hi": ""}), {"k": "a", "v": "1", "hi": False})


if __name__ == "__main__":
    unittest.main()
