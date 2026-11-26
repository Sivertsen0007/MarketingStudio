"""Data policy tests: Studio pulls once and keeps it until the user refreshes. Run from the studio folder:
    python3 -m unittest tests/test_datapolicy.py

Offline: every builder that would call Google, Meta, ML Suite or WordPress is replaced with a recorder, and Studio's
data folder is a temp folder (the user's settings.json, caches and archive are never touched).
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("MSTUDIO_HOME", _tf.mkdtemp(prefix="mstudio-test-"))
_os.environ["MSTUDIO_KEYCHAIN"] = "0"

import datetime as dt
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
TMP = Path(tempfile.mkdtemp(prefix="datapolicy-test-"))
os.environ["STUDIO_DATA_DIR"] = str(TMP)
os.environ["SEO_STUDIO_DIR"] = str(TMP)

import core  # noqa: E402

core.STUDIO_DIR = TMP
import datapolicy  # noqa: E402
import datasources  # noqa: E402
import gapi  # noqa: E402
import meta_ads as meta  # noqa: E402
import overview  # noqa: E402
import server  # noqa: E402
import mcp_server  # noqa: E402
from areas import activity as A  # noqa: E402
from areas import _activity_archive as archive  # noqa: E402
from areas import email as em  # noqa: E402

archive.DIR = str(TMP / "activity")
meta.SETTINGS = TMP / "settings.json"
gapi.SETTINGS = TMP / "settings.json"
ML, MLUK = core.resolve("ml"), core.resolve("mluk")
OLD = (dt.datetime.now() - dt.timedelta(days=3)).isoformat(timespec="seconds")


def set_mode(m):
    s = json.loads((TMP / "settings.json").read_text()) if (TMP / "settings.json").exists() else {}
    s["data_policy"] = m
    (TMP / "settings.json").write_text(json.dumps(s))


class Base(unittest.TestCase):
    def setUp(self):
        for f in list(TMP.glob("*.json")):
            f.unlink()
        set_mode("manual")
        self.built = []
        self._bb, self._bs = gapi._build_base, gapi._build_stats
        gapi._build_base = lambda cid: (self.built.append(("base", cid)) or {"rows": {}, "tree": [], "camp_status": {}, "group_status": {}})
        gapi._build_stats = lambda cid, cond, key: (self.built.append((key, cid)) or {"stats": {}})
        self._threads = []
        self._thread = threading.Thread

        test = self

        class Spy(threading.Thread):
            def start(self):
                test._threads.append(self)
                super().start()
        threading.Thread = Spy
        self._mrefresh = meta.refresh
        self.meta_pulls = []

        def fake_meta_refresh():
            self.meta_pulls.append(meta.act())
            d = {"account": {"currency": "USD"}, "campaigns": [], "adsets": [], "ads": [], "audiences": [], "insights": {},
                 "fetched_at": dt.datetime.now().isoformat(timespec="minutes"), "_at": time.time()}
            meta._cache_file().write_text(json.dumps(d))
            datapolicy.mark(meta._src())
            return d
        meta.refresh = fake_meta_refresh
        gapi._bg.clear()
        meta._bg.clear()

    def tearDown(self):
        gapi._build_base, gapi._build_stats = self._bb, self._bs
        threading.Thread = self._thread
        meta.refresh = self._mrefresh

    def write_google(self, cid, key, fetched=OLD):
        at = dt.datetime.fromisoformat(fetched).timestamp()
        body = {"rows": {}, "tree": [], "camp_status": {}, "group_status": {}} if key == "base" else {"stats": {}}
        gapi._file(cid, key).write_text(json.dumps({**body, "fetched_at": fetched, "_at": at}))


class Manual(Base):
    def test_fresh_rule(self):
        self.assertTrue(datapolicy.fresh("google:1", 10 ** 7, 60), "manual: no time-based expiry")
        self.assertFalse(datapolicy.fresh("google:1", None, 60), "nothing cached: fetch once")
        self.assertFalse(datapolicy.fresh("google:1", 5, 0), "ttl 0 is an explicit fetch")
        set_mode("auto")
        self.assertFalse(datapolicy.fresh("google:1", 120, 60))
        self.assertTrue(datapolicy.fresh("google:1", 30, 60))

    def test_google_old_cache_is_kept_and_no_thread(self):
        self.write_google(ML, "base")
        self.write_google(ML, "LAST_7_DAYS")
        gapi.base(ML)
        gapi.stats(ML, 7)
        self.assertEqual(self.built, [], "a 3-day-old cache is used as it is")
        self.assertEqual(self._threads, [], "no background re-fetch in manual mode")

    def test_google_new_range_fetches_once(self):
        self.write_google(ML, "base")
        gapi.stats(ML, None, "2026-10-01", "2026-10-31")
        gapi.stats(ML, None, "2026-10-01", "2026-10-31")
        self.assertEqual(self.built, [("2026-10-01_2026-10-31", ML)], "a new range is fetched once, then reused")

    def test_auto_mode_still_refreshes_in_background(self):
        set_mode("auto")
        self.write_google(ML, "base")
        gapi.base(ML)
        for t in self._threads:
            t.join(5)
        self.assertEqual(len(self._threads), 1, "auto mode keeps the old background refresh")
        self.assertEqual(self.built, [("base", ML)])

    def test_meta_old_cache_is_kept_and_no_thread(self):
        meta.use("meta") if meta.settings()["meta_accounts"] else None
        meta._cache_file().write_text(json.dumps({"account": {}, "campaigns": [], "adsets": [], "ads": [], "audiences": [], "insights": {},
                                                  "fetched_at": OLD[:16]}))
        meta.load()
        meta.validate.__doc__  # validate() reads load(max_age=60, wait=True): cached in manual mode too
        meta.load(max_age=60, wait=True)
        self.assertEqual(self.meta_pulls, [])
        self.assertEqual(self._threads, [])
        meta.load(0, wait=True)
        self.assertEqual(len(self.meta_pulls), 1, "max_age=0 still pulls")

    def test_meta_insights_kept(self):
        f = core.STUDIO_DIR / f"meta_ins_{meta.acct()}_campaign_7d.json"
        f.write_text(json.dumps({"at": time.time() - 30 * 86400, "data": {"1": {"spend": 2}}}))
        meta._ins_mem.clear()
        calls = []
        real = meta.get_all
        meta.get_all = lambda *a, **k: calls.append(a) or []
        try:
            self.assertEqual(meta.insights("campaign", 7), {"1": {"spend": 2}})
            self.assertTrue(meta.insights_cached("campaign", 7))
            self.assertFalse(meta.insights_cached("campaign", 30))
        finally:
            meta.get_all = real
        self.assertEqual(calls, [])

    def test_invalidate_beats_old_copies_only(self):
        self.write_google(ML, "base")
        self.write_google(ML, "LAST_30_DAYS")
        gapi.refresh(ML)  # base + 7 days
        self.assertEqual(sorted(self.built), sorted([("base", ML), ("LAST_7_DAYS", ML)]))
        self.built.clear()
        gapi.stats(ML, 7)
        self.assertEqual(self.built, [], "what the refresh just wrote stays fresh")
        gapi.stats(ML, 30)
        self.assertEqual(self.built, [("LAST_30_DAYS", ML)], "a range cached before the refresh is fetched again, once")
        gapi.stats(ML, 30)
        self.assertEqual(self.built, [("LAST_30_DAYS", ML)])

    def test_email_mcp_cache_kept(self):
        a = em.McpAdapter("https://example.invalid", "t", cache_path=TMP / "email_mcp.json")
        n = []
        a._cached("k", lambda: n.append(1) or 1)
        c = json.loads((TMP / "email_mcp.json").read_text())
        c["items"]["k"]["t"] -= 30 * 86400
        (TMP / "email_mcp.json").write_text(json.dumps(c))
        a._cached("k", lambda: n.append(1) or 1)
        self.assertEqual(n, [1], "manual: an old ML Suite read is kept")
        a._cached("k", lambda: n.append(1) or 1, fresh=True)
        self.assertEqual(n, [1, 1], "Refresh reads again")

    def test_email_probe_kept(self):
        class P:
            source, base_url, _token = "api", "https://example.invalid", "t"
            n = 0

            def ping(self):
                P.n += 1
        em._probes.clear()
        p = P()
        em.probe(p)
        k = next(iter(em._probes))
        em._probes[k] = (em._probes[k][0] - 86400, *em._probes[k][1:])
        em.probe(p)
        self.assertEqual(P.n, 1)


class RefreshPerSource(Base):
    def test_google_one_account_only(self):
        calls = []
        real_meta, real_email = datasources._one_meta, em.refresh
        datasources._one_meta = lambda k: calls.append(("meta", k))
        em.refresh = lambda b=None: calls.append(("email",)) or {}
        try:
            r = datasources.refresh("google", "mluk", days="7")
        finally:
            datasources._one_meta, em.refresh = real_meta, real_email
        self.assertEqual(sorted(self.built), sorted([("base", MLUK), ("LAST_7_DAYS", MLUK)]), "only MLUK is fetched")
        self.assertEqual(calls, [], "no other source is touched")
        self.assertEqual(r["results"][0]["account"], MLUK)

    def test_google_all_accounts(self):
        datasources.refresh("google")
        self.assertEqual({c for _, c in self.built}, {ML, MLUK})

    def test_unknown_source(self):
        with self.assertRaises(ValueError):
            datasources.refresh("nope")

    def test_status_reads_files_only(self):
        self.write_google(ML, "base")
        st = datasources.status()
        self.assertEqual(self.built, [])
        g = [r for r in st["sources"] if r["source"] == "google"]
        self.assertTrue(any(r["fetched_at"] == OLD for r in g))
        self.assertEqual(st["mode"], "manual")
        self.assertEqual({r["source"] for r in st["sources"]} >= {"google", "email", "seo", "social", "activity"}, True)


class Overview(Base):
    def test_cached_until_overview_refresh(self):
        n = []
        real = overview._build
        overview._build = lambda days, old=None: n.append(days) or {"days": int(days), "totals": [], "areas": [], "destinations": [], "campaigns": [],
                                                          "errors": [], "note": ""}
        try:
            overview.build("7")
            overview.build("7")
            self.assertEqual(n, ["7"], "opening Overview again pulls nothing")
            overview.build("30")
            self.assertEqual(n, ["7", "30"], "a new days value is fetched once")
            time.sleep(0.01)
            gapi.refresh(MLUK)
            o = overview.build("7")
            self.assertEqual(n, ["7", "30"], "a refresh of one Google account does not rebuild Overview from every account")
            self.assertTrue(any(k.startswith("google") for k in o["newer_sources"]), "Overview says a source is newer")
            overview.build("7", force=True)
            self.assertEqual(len(n), 3, "Overview's own Refresh")
        finally:
            overview._build = real


class AfterPush(Base):
    def test_google_push_refreshes_that_account_only(self):
        server._after_live(MLUK, {"ok": True})
        for t in self._threads:
            t.join(5)
        self.assertEqual({c for _, c in self.built}, {MLUK})
        self.assertIsNotNone(datapolicy.fetched(f"google:{MLUK}"))
        self.built.clear()
        server._after_live(ML, {"ok": False})
        self.assertEqual(self.built, [], "a failed push refreshes nothing")

    def test_meta_refresh_account_only_current(self):
        meta._cur.act = "act_111"
        meta.refresh_account()
        self.assertEqual(self.meta_pulls, ["act_111"])


class ActivityPolicy(Base):
    def setUp(self):
        super().setUp()
        A._synced.clear()
        self.q = []

    def query(self, cid, gaql):
        self.q.append(gaql)
        return []

    def test_manual_does_not_resync_archived_account(self):
        acc = [{"id": "1", "name": "Test"}]
        now = dt.datetime.now(dt.timezone.utc)
        real_tz = A.gch.account_tz
        A.gch.account_tz = lambda cid, q: dt.timezone.utc
        try:
            A.sync_google(accounts=acc, query=self.query, now=now)
            first = len(self.q)
            self.assertGreater(first, 0, "first time: nothing archived, sync once")
            A._synced.clear()
            A.sync_google(accounts=acc, query=self.query, now=now)
            self.assertEqual(len(self.q), first, "manual: opening again syncs nothing")
            A.sync_google(force=True, accounts=acc, query=self.query, now=now)
            self.assertGreater(len(self.q), first, "Refresh syncs")
        finally:
            A.gch.account_tz = real_tz

    def test_warning_after_20_days(self):
        real = A._google_accounts
        A._google_accounts = lambda: [{"id": "9", "name": "X"}]
        try:
            now = dt.datetime.now(dt.timezone.utc)
            archive.save("google", "9", [], now - dt.timedelta(days=25), now - dt.timedelta(days=22), now - dt.timedelta(days=22))
            w = A.sync_status(now)["warning"]
            self.assertIn("Refresh Activity within 7 days", w)
            self.assertIn("Google history from", w)
        finally:
            A._google_accounts = real


class Mcp(Base):
    def test_tools_listed_and_read_only(self):
        names = {t["name"] for t in mcp_server.TOOLS}
        self.assertTrue({"data_status", "refresh_data"} <= names)
        st = mcp_server.call("data_status", {})
        self.assertEqual(st["mode"], "manual")
        mcp_server.call("refresh_data", {"source": "google", "account": "ml"})
        self.assertEqual({c for _, c in self.built}, {ML})

    def test_list_entities_uses_cache(self):
        self.write_google(ML, "base")
        self.write_google(ML, "LAST_7_DAYS")
        real = gapi.use_api
        gapi.use_api = lambda acct: True
        try:
            mcp_server.call("list_entities", {"kind": "campaigns", "account": "ml"})
        finally:
            gapi.use_api = real
        self.assertEqual(self.built, [], "an MCP read uses the cached data")


class ModeSwitch(Base):
    """The Settings switch (POST /api/settings/data_policy): manual stays the default, auto only when chosen."""

    def test_set_mode(self):
        (TMP / "settings.json").write_text(json.dumps({"views": [{"name": "Mine"}], "meta_accounts": []}))
        self.assertEqual(datapolicy.mode(), "manual")
        self.assertEqual(datapolicy.set_mode("auto"), {"data_policy": "auto"})
        self.assertTrue(datapolicy.background_ok())
        kept = json.loads((TMP / "settings.json").read_text())
        self.assertEqual((kept["views"], kept["data_policy"]), ([{"name": "Mine"}], "auto"))  # other settings kept
        datapolicy.set_mode("manual")
        self.assertTrue(datapolicy.manual())
        for bad in ("Auto", "", None, "always"):
            with self.assertRaises(ValueError):
                datapolicy.set_mode(bad)
        self.assertEqual(datapolicy.mode(), "manual")

    def test_route_and_scheduler_hold(self):
        import http.client
        import scheduler
        from http.server import ThreadingHTTPServer
        srv = ThreadingHTTPServer(("127.0.0.1", 0), server.H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)

        def post(body, press=True):
            c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
            h = {"Content-Type": "application/json"}
            if press:
                h["X-Studio-Press"] = server.PRESS
            c.request("POST", "/api/settings/data_policy", json.dumps(body), h)
            r = c.getresponse()
            return r.status, json.loads(r.read())
        self.assertEqual(post({"mode": "auto"}, press=False)[0], 403)
        self.assertEqual(datapolicy.mode(), "manual")
        self.assertEqual(post({"mode": "nonsense"})[0], 400)
        self.assertEqual(post({"mode": "auto"}), (200, {"data_policy": "auto"}))
        self.assertEqual(scheduler.status()["policy"], "auto")
        self.assertEqual(post({"mode": "manual"}), (200, {"data_policy": "manual"}))
        self.assertEqual(scheduler.status()["policy"], "manual")


class CallLog(unittest.TestCase):
    def test_counts_host_and_path_without_query(self):
        os.environ["STUDIO_CALL_LOG"] = "1"
        import urllib.request
        real = urllib.request.urlopen
        try:
            urllib.request.urlopen = lambda req, *a, **k: "ok"
            self.assertTrue(datapolicy.install_call_log())
            urllib.request.urlopen("https://graph.facebook.com/v23.0/act_1/insights?access_token=SECRET")
            c = datapolicy.calls(reset=True)
            self.assertEqual(c["by_host"], {"graph.facebook.com": 1})
            self.assertNotIn("SECRET", json.dumps(c))
        finally:
            urllib.request.urlopen = real
            del os.environ["STUDIO_CALL_LOG"]


if __name__ == "__main__":
    unittest.main()
