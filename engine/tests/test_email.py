"""Email area tests: checks, fake adapter, draft store, the user's buttons on fake data, settings file.

Run from the studio folder:  python3 -m unittest tests/test_email.py
Never touches a real ML Suite: SECRETS points at a temp file without a token, so the fake adapter is used.
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("MSTUDIO_HOME", _tf.mkdtemp(prefix="mstudio-test-"))
_os.environ["MSTUDIO_KEYCHAIN"] = "0"

import datetime as dt
import json
import os
import stat
import sys
import tempfile
import unittest
import urllib.parse
import urllib.request
from pathlib import Path

os.environ["STUDIO_DATA_DIR"] = tempfile.mkdtemp(prefix="email-test-dp-")  # datapolicy's status file, never the real one
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from areas import email as em  # noqa: E402
import progress  # noqa: E402

GOOD_HTML = ('<html><body><img src="https://media.mlmediahub.com/a.jpg" alt="Reef"><p>Hi {{first_name}}, spaces from GBP 900.</p>'
             '<a href="https://www.masterliveaboards.com/">Trips</a> <a href="mailto:dive@masterliveaboards.com">Mail</a>'
             '<a href="tel:+44123">Call</a> <a href="{{unsubscribe_url}}">Unsubscribe</a></body></html>')


def good(**kw):
    c = {"subject": "Red Sea spring dates", "preview_text": "New dates", "html": GOOD_HTML, "from_address": em.FROM_ADDRESS,
         "reply_to": em.FROM_ADDRESS, "list_id": "l1", "segment_id": None}
    c.update(kw)
    return c


def ok_fetch(url):
    return 200, ""


def msgs(probs, level=None):
    return " | ".join(p["message"] for p in probs if level is None or p["level"] == level)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self._old = (em.DATA_DIR, em.SECRETS, em.campaigns_root, em._fetch_status, em.FakeAdapter.SEND_SECONDS)
        em.DATA_DIR = t / "data"
        em.SECRETS = t / "secrets" / "marketing-studio.env"
        self.camp_root = t / "campaigns"
        em.campaigns_root = lambda: self.camp_root
        em._fetch_status = lambda url, timeout=8: (200, "")
        em._link_cache.clear()
        em.FakeAdapter.SEND_SECONDS = 0.6
        em._probes.clear()
        em._adapters.clear()
        # Network guard: tests may only reach this machine, never mlsuite.app or anything else.
        self._urlopen = urllib.request.urlopen

        def guarded(req, *a, **kw):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            host = urllib.parse.urlparse(url).hostname
            if host not in ("127.0.0.1", "localhost"):
                raise AssertionError(f"test tried to reach {host}")
            return self._urlopen(req, *a, **kw)
        urllib.request.urlopen = guarded

    def tearDown(self):
        urllib.request.urlopen = self._urlopen
        em.DATA_DIR, em.SECRETS, em.campaigns_root, em._fetch_status, em.FakeAdapter.SEND_SECONDS = self._old
        em._link_cache.clear()
        em._probes.clear()
        em._adapters.clear()
        self.tmp.cleanup()


class Checks(Base):
    def test_clean_email_passes(self):
        self.assertEqual(em.run_checks(good(), fetch=ok_fetch), [])

    def test_from_and_reply_to(self):
        p = em.run_checks(good(from_address="sales@masterliveaboards.com", reply_to=""), links=False)
        self.assertIn("From must be", msgs(p, "error"))
        self.assertIn("Reply-to must be", msgs(p, "error"))

    def test_unsubscribe_required(self):
        p = em.run_checks(good(html=GOOD_HTML.replace("{{unsubscribe_url}}", "https://x.com/u")), links=False)
        self.assertIn("{{unsubscribe_url}} is missing", msgs(p, "error"))
        self.assertEqual(em.run_checks(good(html=GOOD_HTML.replace("{{unsubscribe_url}}", "{{ unsubscribe_url }}")), links=False), [])

    def test_images(self):
        h = GOOD_HTML + '<img src="https://media.mlmediahub.com/b.webp"><img src="https://staging.mlmediahub.com/c.jpg">' \
                        '<img src="http://localhost:3000/d.png"><img src="https://cdn.example.com/e.jpg">'
        p = em.run_checks(good(html=h), links=False)
        self.assertIn(".webp", msgs(p, "error"))
        self.assertIn("staging.mlmediahub.com", msgs(p, "error"))
        self.assertIn("localhost", msgs(p, "error"))
        self.assertIn("cdn.example.com", msgs(p, "warn"))
        self.assertNotIn("cdn.example.com", msgs(p, "error"))

    def test_subject(self):
        self.assertIn("Subject is empty", msgs(em.run_checks(good(subject="  "), links=False), "error"))
        self.assertIn("61 characters", msgs(em.run_checks(good(subject="x" * 61), links=False), "warn"))
        self.assertEqual(em.run_checks(good(subject="x" * 60), links=False), [])

    def test_copy_rules(self):
        cases = [("Sunny days \U0001F31E", "No emojis", "error"), ("Off to the Bahamas", "Bahamas", "error"),
                 ("Our yacht is ready", "vessel", "error"), ("Mantas guaranteed", "guaranteed", "error"),
                 ("You will see whale sharks", "you will see", "error"), ("Cabins from GBP 900", "spaces", "warn"),
                 ("Spring \u2014 new dates", "em dash", "error")]
        for text, needle, level in cases:
            with self.subTest(text=text):
                p = em.run_checks(good(subject=text), links=False)
                self.assertIn(needle, msgs(p, level))
                p = em.run_checks(good(html=GOOD_HTML.replace("Hi {{first_name}}", text)), links=False)
                self.assertIn(needle, msgs(p, level))

    def test_copy_ignores_markup_but_reads_alt(self):
        h = GOOD_HTML.replace('alt="Reef"', 'alt="A yacht"').replace("<html>", '<html><head><style>.cabins{}</style></head>')
        p = em.run_checks(good(html=h), links=False)
        self.assertIn("vessel", msgs(p))
        self.assertNotIn("spaces", msgs(p))

    def test_audience_needed(self):
        self.assertIn("list or segment", msgs(em.run_checks(good(list_id=None), links=False), "error"))

    def test_links_skip_and_fail(self):
        self.assertEqual(em.link_urls(GOOD_HTML), ["https://www.masterliveaboards.com/"])
        h = GOOD_HTML + '<a href="https://bad.example.com/x">x</a><a href="#top">t</a><a href="https://x.com/?e={{email}}">m</a>'
        fetch = lambda u: (404, "Not Found") if "bad." in u else (200, "")
        p = em.run_checks(good(html=h), fetch=fetch)
        self.assertEqual(len([x for x in p if "Link does not answer" in x["message"]]), 1)
        self.assertIn("bad.example.com", msgs(p, "error"))

    def test_link_cache(self):
        calls = []
        fetch = lambda u: (calls.append(u), (301, ""))[1]
        self.assertEqual(em.check_link("https://a.example/", fetch)[0], True)
        em.check_link("https://a.example/", fetch)
        self.assertEqual(calls, ["https://a.example/"])

    def test_head_then_get(self):
        import urllib.error
        import urllib.request
        seen = []

        class R:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_open(req, timeout=8):
            seen.append(req.get_method())
            if req.get_method() == "HEAD":
                raise urllib.error.HTTPError(req.full_url, 405, "Method Not Allowed", {}, None)
            return R()
        old = urllib.request.urlopen
        urllib.request.urlopen = fake_open
        try:
            self.assertEqual(self._old[3]("https://x.example/")[0], 200)
        finally:
            urllib.request.urlopen = old
        self.assertEqual(seen, ["HEAD", "GET"])

    def test_render_local_highlights_text_only(self):
        out = em.render_local('<a href="{{unsubscribe_url}}">Hi {{first_name}}</a>')
        self.assertIn('href="{{unsubscribe_url}}"', out)
        self.assertIn("<mark", out)
        self.assertEqual(out.count("<mark"), 1)


class Fake(Base):
    def test_list_and_counts_only(self):
        a = em.FakeAdapter()
        rows = a.list_campaigns()
        self.assertGreaterEqual(len(rows), 4)
        self.assertTrue(all("html" not in r for r in rows))
        aud = a.audiences()
        for x in aud["lists"] + aud["segments"]:
            self.assertEqual(set(x), {"id", "name", "count"})
        self.assertEqual(len(a.list_campaigns(limit=1)), 1)
        self.assertTrue(all(r["status"] == "sent" for r in a.list_campaigns(status="sent")))

    def test_patch_drafts_only(self):
        a = em.FakeAdapter()
        c = a.patch("demo-4", {"subject": "New", "from_address": "x@y.z"})
        self.assertEqual(c["subject"], "New")
        self.assertEqual(c["from_address"], em.FROM_ADDRESS)
        with self.assertRaises(em.AdapterError) as cm:
            a.patch("demo-1", {"subject": "x"})
        self.assertEqual(cm.exception.code, 409)

    def test_schedule_cancel_and_send(self):
        a = em.FakeAdapter()
        with self.assertRaises(em.AdapterError):
            a.schedule("demo-5", em._iso(em._now() + dt.timedelta(days=1)))  # fails checks
        c = a.schedule("demo-4", em._iso(em._now() + dt.timedelta(days=1)))
        self.assertEqual(c["status"], "scheduled")
        self.assertEqual(a.cancel("demo-4")["status"], "draft")
        a.schedule("demo-4", em._iso(em._now()))
        import time
        time.sleep(0.8)
        p = a.progress("demo-4")
        self.assertEqual(p["status"], "sent")
        self.assertEqual(p["sent"], p["total"])
        self.assertEqual(a.get_campaign("demo-4")["status"], "sent")
        self.assertEqual(a.test_send("demo-1"), {"ok": True, "to": em.TEST_ADDRESS})

    def test_create(self):
        c = em.p_new_campaign({"name": "Hello", "confirm": em.CONFIRM})["campaign"]
        self.assertEqual(c["status"], "draft")
        self.assertIn("{{unsubscribe_url}}", c["html"])
        with self.assertRaises(ValueError):
            em.p_new_campaign({"name": " ", "confirm": em.CONFIRM})
        with self.assertRaises(ValueError):
            em.p_new_campaign({"name": "No press"})


class Drafts(Base):
    def test_add_merge_list_remove(self):
        em.add_draft({"campaign_id": "demo-4", "fields": {"subject": "A"}}, by="claude")
        em.add_draft({"campaign_id": "demo-4", "fields": {"html": "<p>x</p>", "segment_id": "s1"}}, by="you")
        d = em.list_drafts()
        self.assertEqual(len(d), 1)
        self.assertEqual(d[0]["fields"], {"subject": "A", "html": "<p>x</p>", "segment_id": "s1", "list_id": None})
        self.assertEqual(d[0]["by"], "you")
        self.assertTrue(em.remove_draft("demo-4"))
        self.assertEqual(em.list_drafts(), [])

    def test_rejects_fixed_fields(self):
        with self.assertRaises(ValueError):
            em.add_draft({"campaign_id": "demo-4", "fields": {"from_address": "x@y.z"}})
        with self.assertRaises(ValueError):
            em.add_draft({"campaign_id": "", "fields": {"subject": "x"}})

    def test_merged_view_and_save_to_mls(self):
        em.add_draft({"campaign_id": "demo-4", "fields": {"subject": "From the draft"}})
        g = em.g_campaign({"id": "demo-4"})
        self.assertEqual(g["campaign"]["subject"], "From the draft")
        self.assertNotEqual(g["original"]["subject"], "From the draft")
        self.assertEqual([r["id"] for r in em.g_campaigns({})["campaigns"] if r["draft"]], ["demo-4"])
        em.p_save_to_mls({"id": "demo-4"})
        self.assertEqual(em.FakeAdapter().get_campaign("demo-4")["subject"], "From the draft")
        self.assertEqual(em.list_drafts(), [])


class Buttons(Base):
    def test_need_user_press(self):
        for fn in (em.p_test_send, em.p_schedule, em.p_send_now, em.p_cancel):
            with self.subTest(fn=fn.__name__), self.assertRaises(ValueError):
                fn({"id": "demo-4"})

    def test_test_send_with_progress(self):
        r = em.p_test_send({"id": "demo-4", "confirm": em.CONFIRM})
        self.assertTrue(r["ok"], r)
        self.assertIn(em.TEST_ADDRESS, r["message"])
        p = progress.get(em.KEY)
        self.assertFalse(p["running"])
        self.assertTrue(p["ok"])
        self.assertEqual([x["state"] for x in p["items"]], ["done", "done", "done"])

    def test_checks_block(self):
        r = em.p_test_send({"id": "demo-5", "confirm": em.CONFIRM})
        self.assertFalse(r["ok"])
        self.assertIn("Stopped by the checks", r["message"])
        self.assertFalse(progress.get(em.KEY)["ok"])

    def test_schedule_and_cancel(self):
        when = (dt.datetime.now(em.LONDON) + dt.timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
        r = em.p_schedule({"id": "demo-4", "when": when, "confirm": em.CONFIRM})
        self.assertTrue(r["ok"], r)
        self.assertEqual(em.FakeAdapter().get_campaign("demo-4")["status"], "scheduled")
        self.assertFalse(em.p_schedule({"id": "demo-4", "when": "2020-01-01T09:00", "confirm": em.CONFIRM})["ok"])
        self.assertTrue(em.p_cancel({"id": "demo-4", "confirm": em.CONFIRM})["ok"])
        self.assertEqual(em.FakeAdapter().get_campaign("demo-4")["status"], "draft")

    def test_send_now_saves_draft_and_counts(self):
        em.add_draft({"campaign_id": "demo-4", "fields": {"subject": "Sent from the draft"}})
        r = em.p_send_now({"id": "demo-4", "confirm": em.CONFIRM})
        self.assertTrue(r["ok"], r)
        self.assertIn("4,385 of 4,385 sent", r["message"])
        c = em.FakeAdapter().get_campaign("demo-4")
        self.assertEqual((c["status"], c["subject"]), ("sent", "Sent from the draft"))
        p = progress.get(em.KEY)
        self.assertEqual(p["items"][-1]["sent"], 4385)
        self.assertEqual(em.list_drafts(), [])


class Settings(Base):
    def test_connect_keeps_other_keys_and_hides_token(self):
        em.SECRETS.parent.mkdir(parents=True)
        em.SECRETS.write_text("WP_APP_PASSWORD=abc\n")
        r = em.p_connect({"token": "mls_abcdefghijkl", "base_url": "http://127.0.0.1:9/"})
        self.assertNotIn("mls_abcdefghijkl", json.dumps(r))
        self.assertTrue(r["token_set"])
        txt = em.SECRETS.read_text()
        self.assertIn("WP_APP_PASSWORD=abc", txt)
        self.assertIn("MLSUITE_STUDIO_TOKEN=mls_abcdefghijkl", txt)
        self.assertEqual(stat.S_IMODE(os.stat(em.SECRETS).st_mode), 0o600)
        self.assertIsInstance(em.adapter(), em.FakeAdapter)  # the Studio API token does not answer (nothing on port 9)
        self.assertIn("did not answer", em.g_state({})["api_note"])
        self.assertNotIn("mls_abcdefghijkl", json.dumps(em.g_state({}), default=str))
        em.p_connect({"disconnect": True})
        self.assertNotIn("MLSUITE_STUDIO_TOKEN", em.SECRETS.read_text())
        self.assertIsInstance(em.adapter(), em.FakeAdapter)

    def test_connect_validation(self):
        with self.assertRaises(ValueError):
            em.p_connect({"token": "nope"})
        with self.assertRaises(ValueError):
            em.p_connect({"base_url": "mlsuite.app"})

    def test_real_adapter_error_has_no_token(self):
        a = em.StudioApiAdapter("http://127.0.0.1:9", "mls_secret_token_123", timeout=1)
        with self.assertRaises(em.AdapterError) as cm:
            a.list_campaigns(limit=1)
        self.assertNotIn("mls_secret", str(cm.exception))


class Helpers(Base):
    def test_to_utc(self):
        self.assertEqual(em.to_utc("2026-07-01T09:00").hour, 8)   # BST
        self.assertEqual(em.to_utc("2026-12-01T09:00").hour, 9)   # GMT
        self.assertEqual(em.to_utc("2026-12-01T09:00:00Z").hour, 9)

    def test_save_html(self):
        r = em.p_save_html({"folder": "Black November 2026", "name": "Early access!", "html": "<p>x</p>"})
        self.assertTrue(r["path"].endswith("black-november-2026/early-access.html"))
        self.assertEqual(Path(r["path"]).read_text(), "<p>x</p>")
        self.assertTrue(em.p_save_html({"folder": "black-november-2026", "slug": "early-access", "html": "<p>y</p>"})["replaced"])
        r = em.p_save_html({"folder": "../../etc", "slug": "x", "html": "<p>x</p>"})
        self.assertIn(str(self.camp_root.resolve()), r["path"])
        with self.assertRaises(ValueError):
            em.p_save_html({"folder": "a", "slug": "b", "html": " "})
        self.assertEqual(em.g_folders({})["folders"], ["black-november-2026", "etc"])

    def test_overview_and_health(self):
        o = em.overview("30")
        self.assertEqual(o["title"], "Email")
        self.assertIn("Demo data", o["note"])
        self.assertEqual(o["tiles"][0]["v"], "2")
        self.assertEqual(em.overview("7")["tiles"][0]["v"], "1")
        h = em.health(dt.date.today())
        self.assertEqual(len(h), 1)
        self.assertIsNone(h[0]["ok"])
        self.assertIn("Not connected", h[0]["detail"])

    def test_grid_rows(self):
        g = em.g_campaigns({"status": "sent"})
        self.assertTrue(g["demo"])
        r = g["campaigns"][0]
        self.assertEqual(r["open_rate"], round(100 * 6880 / 18102, 1))
        self.assertEqual(g["counts"]["all"], 5)
        self.assertEqual(len(em.g_campaigns({"search": "red sea"})["campaigns"]), 1)


# ---------------------------------------------------------------- a local fake of ML Suite's /api/mcp (route.ts + server.ts)
import http.server  # noqa: E402
import threading  # noqa: E402

GOOD_TOKEN = "mls_faketoken_for_tests_only"
FAKE_HTML = GOOD_HTML


class FakeMls:
    """JSON-RPC 2.0 over POST, plain JSON replies, Bearer mls_ token, tools named with underscores on the wire.
    Some results smuggle in personal data (subscriber rows, a person's name, addresses) to prove Studio drops them."""

    def __init__(self, studio_api=False, port=0):
        self.calls, self.created, self.studio_api, self.methods = [], [], studio_api, []
        me = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if me.studio_api and self.path.startswith("/api/studio/email/campaigns") and self.headers.get("Authorization") == "Bearer " + GOOD_TOKEN:
                    return self._json(200, {"campaigns": []})
                self.send_response(404)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<html>404</html>")

            def do_POST(self):
                if self.path != "/api/mcp":
                    return self._json(404, {"error": "not found"})
                if self.headers.get("Authorization") != "Bearer " + GOOD_TOKEN:
                    return self._json(401, {"jsonrpc": "2.0", "id": None, "error": {"code": -32000, "message": "unauthorised"}})
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                me.methods.append(body.get("method"))
                if "id" not in body and str(body.get("method", "")).startswith("notifications/"):
                    self.send_response(204)
                    self.end_headers()
                    return
                rid, m, params = body.get("id"), body.get("method"), body.get("params") or {}
                if m == "initialize":
                    return self._json(200, {"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                                                                                     "serverInfo": {"name": "mlsuite", "version": "1.0.0"}}})
                if m == "tools/call":
                    name, args = params.get("name"), params.get("arguments") or {}
                    me.calls.append((name, args))
                    try:
                        text = json.dumps(me.tool(name, args), indent=2)
                        res = {"content": [{"type": "text", "text": text}]}
                    except Exception as ex:  # noqa: BLE001
                        res = {"content": [{"type": "text", "text": f"Error: {ex}"}], "isError": True}
                    return self._json(200, {"jsonrpc": "2.0", "id": rid, "result": res})
                return self._json(200, {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"Method not found: {m}"}})

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), H)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    CAMPAIGNS = [
        {"id": "c1", "name": "October newsletter", "subject": "Autumn trips", "status": "sent", "listId": "L1", "listName": "All subscribers",
         "segmentId": None, "segmentName": None, "scheduledFor": "2026-11-01T09:00:00.000Z", "sentAt": "2026-11-01T09:00:00.000Z",
         "fromAddress": "dive@masterliveaboards.com", "fromName": "Master Liveaboards", "replyTo": "dive@masterliveaboards.com",
         "createdBy": "u1", "createdByName": "Jane Staff", "sendError": "bounce for joe.bloggs@example.com", "updatedAt": "2026-11-01T09:00:00.000Z"},
        {"id": "c2", "name": "Asia draft", "subject": "Asia dates", "status": "draft", "listId": None, "listName": None, "segmentId": "S1",
         "segmentName": "Asia interest", "scheduledFor": None, "sentAt": None, "fromAddress": "dive@masterliveaboards.com",
         "fromName": "Master Liveaboards", "replyTo": "dive@masterliveaboards.com", "createdByName": "Jane Staff"},
    ]

    def tool(self, name, a):
        if name == "email_list_campaigns":
            rows = [c for c in self.CAMPAIGNS if not a.get("status") or c["status"] == a["status"]]
            return rows[: int(a.get("limit") or 100)]
        if name == "email_get_campaign":
            c = next((x for x in self.CAMPAIGNS if x["id"] == a.get("id")), None)
            return c and {**c, "html": FAKE_HTML, "text": "Hi", "blocks": None, "abHtmlB": None}
        if name == "email_campaign_stats":
            return {"total": 1000, "queued": 0, "sent": 1000, "delivered": 980, "opened": 400, "clicked": 50, "bounced": 20, "complained": 1, "failed": 0}
        if name == "email_list_lists":
            return [{"id": "L1", "name": "All subscribers", "memberCount": 1200, "activeCount": 1000, "createdByName": "Jane Staff",
                     "subscribers": [{"email": "jane.doe@gmail.com", "firstName": "Jane"}]}]
        if name == "email_list_segments":
            return [{"id": "S1", "name": "Asia interest", "definition": {"match": "all", "conditions": [{"field": "email", "op": "eq", "value": "vip@gmail.com"}]}}]
        if name == "email_segment_count":
            return {"count": 321}
        if name == "email_overview":
            return {"drafts": 1, "scheduled": 0, "sentLast30": 1, "totalSubscribers": 1500, "activeSubscribers": 1000, "totalLists": 1}
        if name == "email_create_draft_campaign":
            self.created.append(a)
            nid = f"new{len(self.created)}"
            self.CAMPAIGNS = self.CAMPAIGNS + [{"id": nid, "name": a["name"], "subject": a["subject"], "status": "draft", "listId": a.get("listId"),
                                                 "listName": "All subscribers", "fromAddress": a.get("fromAddress"), "replyTo": a.get("replyTo"),
                                                 "fromName": a.get("fromName")}]
            return {"id": nid, "status": "draft", "note": "Created as a DRAFT."}
        raise ValueError(f"Unknown or not permitted tool: {name}")


class Mcp(Base):
    def setUp(self):
        super().setUp()
        self.mls = FakeMls()
        em.SECRETS.parent.mkdir(parents=True, exist_ok=True)
        em.p_connect({"base_url": self.mls.base, "mcp_token": GOOD_TOKEN})

    def tearDown(self):
        self.mls.stop()
        super().tearDown()

    def blob(self):
        cache = (em.DATA_DIR / "email_mcp_cache.json").read_text() if (em.DATA_DIR / "email_mcp_cache.json").exists() else ""
        return json.dumps([em.g_campaigns({}), em.g_audiences({}), em.g_campaign({"id": "c1"}), em.overview("30")], default=str) + cache

    def test_reads_through_mcp(self):
        st = em.g_state({})
        self.assertEqual((st["source"], st["source_label"]), ("mcp", "Live data from ML Suite (read-only)"))
        self.assertFalse(st["can"]["edit"])
        g = em.g_campaigns({})
        c1 = next(r for r in g["campaigns"] if r["id"] == "c1")
        self.assertEqual((c1["sent"], c1["opens"], c1["audience_count"], c1["audience"]), (1000, 400, 1000, "All subscribers"))
        self.assertEqual(c1["open_rate"], round(100 * 400 / 980, 1))
        c2 = next(r for r in g["campaigns"] if r["id"] == "c2")
        self.assertEqual((c2["audience_count"], c2["sent"]), (321, 0))
        self.assertEqual(em.g_audiences({}), {"lists": [{"id": "L1", "name": "All subscribers", "count": 1000}],
                                              "segments": [{"id": "S1", "name": "Asia interest", "count": 321}]})
        self.assertIn("{{unsubscribe_url}}", em.g_campaign({"id": "c1"})["campaign"]["html"])
        self.assertIn("initialize", self.mls.methods)

    def test_personal_data_dropped(self):
        b = self.blob()
        for bad in ("gmail.com", "example.com", "Jane", '"subscribers"', "firstName", "definition", "createdBy", "abHtmlB"):
            self.assertNotIn(bad, b)
        self.assertIn("dive@masterliveaboards.com", b)

    def test_cache(self):
        em.g_campaigns({})
        n = len(self.mls.calls)
        em.g_campaigns({})
        self.assertEqual(len(self.mls.calls), n)
        em.g_campaigns({"fresh": "1"})
        self.assertGreater(len(self.mls.calls), n)

    def test_wrong_token(self):
        em.p_connect({"mcp_token": "mls_wrong_token_123456"})
        with self.assertRaises(em.AdapterError) as cm:
            em.g_campaigns({})
        self.assertIn("401", str(cm.exception))
        self.assertNotIn("mls_wrong", str(cm.exception))
        r = em.p_test_connection({})
        self.assertFalse(r["mcp"]["ok"])
        self.assertNotIn("mls_wrong", json.dumps(r))

    def test_tool_error_is_reported(self):
        with self.assertRaises(em.AdapterError) as cm:
            em.adapter().call("email.nope")
        self.assertIn("not permitted", str(cm.exception))

    def test_create_draft_from_button(self):
        with self.assertRaises(ValueError):
            em.p_new_campaign({"name": "x"})  # no press
        with self.assertRaises(em.AdapterError):
            em.p_new_campaign({"name": "Seg", "subject": "S", "segment_id": "S1", "confirm": em.CONFIRM})
        r = em.p_new_campaign({"name": "December", "subject": "December dates", "list_id": "L1", "confirm": em.CONFIRM})
        self.assertEqual(r["campaign"]["status"], "draft")
        a = self.mls.created[-1]
        self.assertEqual((a["fromAddress"], a["replyTo"], a["listId"]), (em.FROM_ADDRESS, em.FROM_ADDRESS, "L1"))
        self.assertIn("{{unsubscribe_url}}", a["html"])

    def test_live_buttons_need_the_studio_api(self):
        for fn, extra in ((em.p_test_send, {}), (em.p_send_now, {}), (em.p_cancel, {}),
                          (em.p_schedule, {"when": (dt.datetime.now(em.LONDON) + dt.timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")})):
            with self.subTest(fn=fn.__name__):
                r = fn({"id": "c2", "confirm": em.CONFIRM, **extra})
                self.assertFalse(r["ok"])
                self.assertEqual(r["message"], em.NEEDS_API)
        em.add_draft({"campaign_id": "c2", "fields": {"html": "<p>x</p>"}})
        with self.assertRaises(ValueError):
            em.p_save_to_mls({"id": "c2"})
        used = {n for n, _ in self.mls.calls}
        self.assertTrue(used <= {"email_list_campaigns", "email_get_campaign", "email_campaign_stats", "email_list_lists",
                                 "email_list_segments", "email_segment_count", "email_overview"}, used)

    def test_preview_is_local(self):
        r = em.p_preview({"id": "c1"})
        self.assertIn("<mark", r["html"])
        self.assertNotIn("email_compile_preview", {n for n, _ in self.mls.calls})

    def test_health_and_overview(self):
        h = em.health(dt.date.today())
        self.assertEqual([(x["check"], x["ok"]) for x in h], [("MCP (read)", True)])
        o = em.overview("30")
        self.assertIn("Live data from ML Suite", o["note"])
        self.assertEqual(o["tiles"][0]["v"], "1")
        self.assertEqual(em.g_overview_numbers({})["numbers"]["activeSubscribers"], 1000)


class Choice(Base):
    def tearDown(self):
        self.mls.stop()
        super().tearDown()

    def test_studio_api_first_then_mcp_then_demo(self):
        self.mls = FakeMls(studio_api=False)
        em.SECRETS.parent.mkdir(parents=True, exist_ok=True)
        em.p_connect({"base_url": self.mls.base, "token": GOOD_TOKEN, "same_token": True})
        self.assertEqual(em.settings()["mcp_token"], GOOD_TOKEN)
        self.assertTrue(em.g_settings({})["same_token"])
        self.assertEqual(em.adapter().source, "mcp")  # Studio API not deployed (404 HTML)
        r = em.p_test_connection({})
        self.assertFalse(r["api"]["ok"])
        self.assertTrue(r["mcp"]["ok"])
        self.assertEqual(r["active"], "mcp")
        h = em.health(dt.date.today())
        self.assertEqual([(x["check"], x["ok"]) for x in h], [("Studio API", None), ("MCP (read)", True)])
        self.mls.studio_api = True
        em._probes.clear()
        self.assertEqual(em.adapter().source, "api")
        em.p_connect({"disconnect": True})
        self.assertEqual(em.adapter().source, "demo")
        self.assertNotIn(GOOD_TOKEN, em.SECRETS.read_text())


if __name__ == "__main__":
    unittest.main()
