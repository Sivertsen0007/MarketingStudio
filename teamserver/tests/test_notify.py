"""Notifications: rules, bell / email / webhook delivery, digests, Focus, Away, webhook checks, SMTP and retry.

A real server on a free port with an injected clock (see test_server.Base). Email goes to a local SMTP stub thread and
webhooks to a local HTTP stub; nothing leaves this machine. The sender thread is off (notify_interval=None): each test
calls notifier.run_once() itself, so timing is exact.
"""
import json
import os
import shutil
import socket
import socketserver
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from test_server import Base, Clock  # noqa: E402
from teamserver import notify  # noqa: E402
from teamserver.app import TeamServer  # noqa: E402
from teamserver.store import ApiError  # noqa: E402


class SmtpStub:
    """Just enough SMTP (EHLO, MAIL, RCPT, DATA, QUIT) to receive messages; no TLS, no auth."""

    def __init__(self):
        self.messages = []
        stub = self

        class H(socketserver.StreamRequestHandler):
            timeout = 5  # a client that stops talking cannot hold a handler thread

            def handle(self):
                w = lambda s: self.wfile.write(s.encode() + b"\r\n")  # noqa: E731
                w("220 stub ready")
                rcpt, mail = [], None
                while True:
                    line = self.rfile.readline()
                    if not line:
                        return
                    cmd = line.decode("utf-8", "replace").strip()
                    up = cmd.upper()
                    if up.startswith("EHLO") or up.startswith("HELO"):
                        w("250-stub")
                        w("250 8BITMIME")
                    elif up.startswith("MAIL FROM"):
                        mail = cmd[10:].strip()
                        w("250 ok")
                    elif up.startswith("RCPT TO"):
                        rcpt.append(cmd[8:].strip())
                        w("250 ok")
                    elif up == "DATA":
                        w("354 go on")
                        data = []
                        while True:
                            ln = self.rfile.readline()
                            if not ln or ln in (b".\r\n", b".\n"):
                                break
                            data.append(ln)
                        stub.messages.append({"from": mail, "to": list(rcpt),
                                              "data": b"".join(data).decode("utf-8", "replace")})
                        rcpt = []
                        w("250 queued")
                    elif up == "QUIT":
                        w("221 bye")
                        return
                    else:
                        w("250 ok")

        self.srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.thread.join(timeout=5)


class HookStub:
    """A local webhook receiver: records each POST body; answers with the next status in `statuses` (then 200)."""

    def __init__(self):
        self.posts = []
        self.statuses = []
        stub = self

        class H(BaseHTTPRequestHandler):
            timeout = 5

            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n)
                stub.posts.append({"path": self.path, "body": json.loads(body.decode()),
                                   "type": self.headers.get("Content-Type")})
                code = stub.statuses.pop(0) if stub.statuses else 200
                self.send_response(code)
                if 300 <= code < 400:
                    self.send_header("Location", "/elsewhere")
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.url = "http://127.0.0.1:%d/hook/secret-path" % self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def texts(self):
        return [p["body"]["text"] for p in self.posts]

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.thread.join(timeout=5)


class NotifyBase(Base):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="teamserver-test-")
        self.clock = Clock()  # 2027-01-15 08:00:00 UTC, on the hour
        self.smtp = SmtpStub()
        self.hook = HookStub()
        self.srv = TeamServer(self.dir, host="127.0.0.1", port=0, public_url="https://team.example.test",
                              clock=self.clock, ping_interval=0.3, notify_interval=None, allow_private_webhooks=True,
                              smtp={"host": "127.0.0.1", "port": self.smtp.port, "user": "", "password": "",
                                    "from": "team@example.test", "starttls": False}).start()
        self.port = self.srv.port
        self.owner = self.join_new("owner@example.com", "owner", "Sam Owner")
        self.alex = self.join_new("alex@example.com", "editor", "Alex")
        self.bo = self.join_new("bo@example.com", "editor", "Bo")
        self.cy = self.join_new("cy@example.com", "editor", "Cy")
        self.dee = self.join_new("dee@example.com", "editor", "Dee")
        for who in (self.bo, self.cy):
            self.ok("PATCH", "/api/v1/members/%d" % who["member"]["id"], {"approver": True}, self.owner["token"])

    def tearDown(self):
        self.srv.stop()
        self.smtp.stop()
        self.hook.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    # helpers
    def prefs(self, who, body):
        return self.ok("PUT", "/api/v1/notify/prefs", body, who["token"])["prefs"]

    def notes(self, who, **q):
        qs = "&".join("%s=%s" % kv for kv in q.items())
        return self.ok("GET", "/api/v1/notifications" + ("?" + qs if qs else ""), token=who["token"])

    def kinds(self, who):
        return [n["kind"] for n in self.notes(who)["notifications"]]

    def tick(self):
        self.srv.notifier.run_once()

    def push(self, who, area="ads", items=None, coll=None):
        body = {"kind": "push", "area": area, "summary": "pushed changes live: spring prices",
                "data": {"why": "spring prices", "items": items or []}}
        if coll:
            body["coll"] = coll
        return self.ok("POST", "/api/v1/events", body, who["token"])

    def write(self, who, coll, iid, base, data, cs=None):
        op = {"op": "put", "coll": coll, "id": iid, "base_version": base, "data": data}
        if cs:
            op["changeset"] = cs
        return self.ok("POST", "/api/v1/items", {"ops": [op]}, who["token"])

    def comment(self, who, coll, iid, body):
        return self.ok("POST", "/api/v1/comments", {"coll": coll, "id": iid, "body": body}, who["token"])


class TestRules(NotifyBase):
    def test_defaults(self):
        out = self.ok("GET", "/api/v1/notify/prefs", token=self.alex["token"])
        p = out["prefs"]
        self.assertEqual(p["rules"], {k: True for k in notify.ALWAYS})
        self.assertEqual(p["follow"], {"ads": False, "email": False, "seo": False, "social": False})
        self.assertEqual(p["bell"]["timing"], "instant")
        self.assertFalse(p["email"]["on"])
        self.assertFalse(p["webhook"]["on"])
        self.assertFalse(p["webhook"]["set"])
        self.assertFalse(p["focus"]["active"])
        self.assertFalse(p["away"]["active"])
        self.assertTrue(out["server"]["email"])
        self.assertEqual(self.notes(self.alex)["notifications"], [])

    def test_bad_prefs(self):
        for body in ({"rules": {"nope": True}}, {"follow": {"tv": True}}, {"email": {"timing": "weekly"}},
                     {"daily_at": "25:00"}, {"tz_offset": 2000}, {"focus": {"until": "soon"}},
                     {"away": {"backup": self.alex["member"]["id"]}}, {"away": {"backup": 999}},
                     {"rules": {"mention": "yes"}}):
            s, out = self.call("PUT", "/api/v1/notify/prefs", body, self.alex["token"])
            self.assertEqual(s, 400, (body, out))

    def test_edits_never_ping(self):
        self.prefs(self.alex, {"follow": {"ads": True, "seo": True}})
        self.write(self.bo, "ads:123", "campaign:1", 0, {"name": "Brand"})
        self.write(self.bo, "seo:changes", "c1", 0, {"new": "Hello"}, cs={"id": "cs1", "name": "Spring"})
        self.write(self.alex, "seo:changes", "c2", 0, {"new": "Mine"})  # no changeset: not pending
        self.write(self.bo, "seo:changes", "c2", 1, {"new": "Bo's"})
        self.tick()
        self.assertEqual(self.kinds(self.alex), [])
        self.assertEqual(self.kinds(self.bo), [])

    def test_pending_item_changed_by_someone_else(self):
        cs = {"id": "cs1", "name": "Spring"}
        self.write(self.alex, "seo:changes", "p1", 0, {"new": "Mine"}, cs=cs)
        self.write(self.alex, "seo:changes", "p2", 0, {"new": "Mine too"}, cs=cs)
        self.write(self.bo, "seo:changes", "p1", 1, {"new": "Bo was here"})
        n = self.notes(self.alex)["notifications"]
        self.assertEqual([x["kind"] for x in n], ["pending_changed"])
        self.assertEqual((n[0]["coll"], n[0]["item_id"]), ("seo:changes", "p1"))
        self.assertIn("Bo", n[0]["text"])
        self.assertEqual(n[0]["link"], "marketingstudio://open?coll=seo%3Achanges&id=p1")
        # the same item again in Bo's same edit session: no second ping; another of Alex's pending items: one more
        self.write(self.bo, "seo:changes", "p1", 2, {"new": "Bo again"})
        self.assertEqual(len(self.notes(self.alex)["notifications"]), 1)
        self.write(self.bo, "seo:changes", "p2", 1, {"new": "Bo"})
        self.assertEqual(len(self.notes(self.alex)["notifications"]), 2)
        # Alex pushes p1 and p2: no longer pending, so Cy's later edit pings nobody
        self.push(self.alex, "seo", [{"coll": "seo:changes", "id": "p1"}, {"coll": "seo:changes", "id": "p2"}])
        self.write(self.cy, "seo:changes", "p1", 3, {"new": "Cy"})
        self.assertEqual(len(self.notes(self.alex)["notifications"]), 2)
        # a rule turned off: nothing
        self.write(self.alex, "seo:changes", "p3", 0, {"new": "x"}, cs=cs)
        self.prefs(self.alex, {"rules": {"pending_changed": False}})
        self.write(self.cy, "seo:changes", "p3", 1, {"new": "y"})
        self.assertEqual(len(self.notes(self.alex)["notifications"]), 2)

    def test_follow_area_pushes(self):
        self.prefs(self.alex, {"follow": {"email": True}})
        self.push(self.bo, "ads")
        self.push(self.bo, "email", coll="email", items=[{"coll": "email", "id": "55"}])
        self.push(self.alex, "email")  # your own push never pings you
        n = self.notes(self.alex)["notifications"]
        self.assertEqual([x["kind"] for x in n], ["push"])
        self.assertEqual(n[0]["area"], "email")
        self.assertTrue(n[0]["text"].startswith("Bo pushed"))
        self.assertEqual(self.kinds(self.cy), [])

    def test_review_mention_decision_and_claim(self):
        self.write(self.alex, "email", "55", 0, {"fields": {"subject": "Hi"}})
        rv = self.ok("POST", "/api/v1/reviews", {"items": [{"coll": "email", "id": "55", "version": 1}],
                                                 "reviewers": [self.bo["member"]["id"]]}, self.alex["token"])
        n = self.notes(self.bo)["notifications"]
        self.assertEqual([x["kind"] for x in n], ["review_requested"])
        self.assertEqual((n[0]["coll"], n[0]["item_id"]), ("review", str(rv["id"])))
        conf = [{"coll": "email", "id": "55", "content_hash": rv["items"][0]["hash"]}]
        self.ok("POST", "/api/v1/reviews/%d/decide" % rv["id"], {"decision": "approve", "confirm": conf},
                self.bo["token"])
        n = self.notes(self.alex)["notifications"]
        self.assertEqual(n[0]["kind"], "review_decided")
        self.assertIn("approved", n[0]["text"])
        # a mention (also one added by editing a comment later)
        c = self.comment(self.bo, "email", "55", "Looks good @alex")
        c2 = self.comment(self.bo, "email", "55", "And this one")
        self.ok("PATCH", "/api/v1/comments/%d" % c2["id"], {"body": "And this one, @Cy"}, self.bo["token"])
        self.assertEqual(self.notes(self.alex)["notifications"][0]["data"]["comment_id"], c["id"])
        self.assertEqual(self.kinds(self.cy), ["mention"])
        # a claim on something Alex owns (owner scope) and on something Alex created
        self.ok("PUT", "/api/v1/owners", {"owners": {"social": self.alex["member"]["id"]}}, self.owner["token"])
        self.ok("POST", "/api/v1/claims", {"coll": "social", "id": "post9"}, self.bo["token"])
        self.ok("POST", "/api/v1/claims", {"coll": "email", "id": "55"}, self.cy["token"])
        self.ok("POST", "/api/v1/claims", {"coll": "seo:map_nodes", "id": "n1"}, self.cy["token"])
        n = [x for x in self.notes(self.alex)["notifications"] if x["kind"] == "claim"]
        self.assertEqual(sorted((x["coll"], x["item_id"]) for x in n), [("email", "55"), ("social", "post9")])

    def test_read_and_mentions_stay_in_step(self):
        c1 = self.comment(self.bo, "email", "1", "@alex one")
        c2 = self.comment(self.bo, "email", "1", "@alex two")
        out = self.notes(self.alex)
        self.assertEqual(out["unread"], 2)
        first = next(n for n in out["notifications"] if n["data"]["comment_id"] == c1["id"])
        r = self.ok("POST", "/api/v1/notifications/read", {"ids": [first["id"]]}, self.alex["token"])
        self.assertEqual((r["updated"], r["unread"]), (1, 1))
        unread = self.ok("GET", "/api/v1/mentions?unread=1", token=self.alex["token"])["mentions"]
        self.assertEqual([m["comment"]["id"] for m in unread], [c2["id"]])
        # the other way: marking the mention read marks its notification read
        self.ok("POST", "/api/v1/mentions/read", {"ids": [c2["id"]]}, self.alex["token"])
        self.assertEqual(self.notes(self.alex, unread=1)["notifications"], [])
        # someone else's notification ids do nothing
        self.comment(self.alex, "email", "1", "@bo hi")
        bo_note = self.notes(self.bo)["notifications"][0]["id"]
        self.assertEqual(self.ok("POST", "/api/v1/notifications/read", {"ids": [bo_note]}, self.alex["token"])["updated"], 0)
        self.assertEqual(self.ok("POST", "/api/v1/notifications/read", {"all": True}, self.bo["token"])["unread"], 0)


class TestDelivery(NotifyBase):
    def test_mention_instant_on_bell_even_in_digest_mode(self):
        self.prefs(self.alex, {"bell": {"timing": "daily"}, "email": {"on": True, "timing": "daily"},
                               "follow": {"ads": True}})
        stream = self.open_stream(self.alex["token"])
        self.comment(self.bo, "email", "7", "Please check @alex")
        ev = self.wait_for(stream, "notify")
        self.assertEqual(ev["unread"], 1)
        self.assertFalse(ev["quiet"])
        self.push(self.bo, "ads")  # a followed push: waits for the daily digest, on the bell too
        self.assertEqual(self.kinds(self.alex), ["mention"])
        self.tick()
        self.assertEqual(self.smtp.messages, [])  # email is daily: not yet
        self.clock.advance(3599)  # 08:59:59, daily_at 09:00
        self.tick()
        self.assertEqual(self.smtp.messages, [])
        self.clock.advance(1)
        self.tick()
        self.assertEqual(sorted(self.kinds(self.alex)), ["mention", "push"])
        self.assertEqual(len(self.smtp.messages), 1)
        msg = self.smtp.messages[0]
        self.assertEqual(msg["to"], ["<alex@example.com>"])
        self.assertIn("2 notifications (daily digest)", msg["data"])
        self.assertIn("marketingstudio://open?coll=email&id=7", msg["data"])
        self.tick()
        self.assertEqual(len(self.smtp.messages), 1)  # sent once

    def test_hourly_digest_batches_with_the_clock(self):
        self.prefs(self.alex, {"follow": {"seo": True}, "webhook": {"on": True, "timing": "hourly", "url": self.hook.url}})
        self.clock.advance(600)  # 08:10
        for _ in range(3):
            self.push(self.bo, "seo")
            self.clock.advance(60)
        self.tick()
        self.assertEqual(self.hook.posts, [])
        self.assertEqual(len(self.notes(self.alex)["notifications"]), 3)  # the bell is instant
        self.clock.advance(3600 - 600 - 180 - 1)  # 08:59:59
        self.tick()
        self.assertEqual(self.hook.posts, [])
        self.clock.advance(1)  # 09:00
        self.tick()
        self.assertEqual(len(self.hook.posts), 1)
        self.assertEqual(self.hook.posts[0]["path"], "/hook/secret-path")
        self.assertTrue(self.hook.posts[0]["type"].startswith("application/json"))
        text = self.hook.texts()[0]
        self.assertIn("3 notifications (hourly digest)", text)
        self.assertEqual(text.count("Bo pushed"), 3)
        self.tick()
        self.assertEqual(len(self.hook.posts), 1)

    def test_instant_webhook_and_smtp(self):
        self.prefs(self.alex, {"email": {"on": True}, "webhook": {"on": True, "url": self.hook.url}})
        self.comment(self.bo, "seo:map_nodes", "n_1", "@alex can you take this?")
        self.tick()
        self.assertEqual(len(self.hook.posts), 1)
        self.assertEqual(len(self.smtp.messages), 1)
        text = self.hook.texts()[0]
        self.assertIn("Bo mentioned you: @alex can you take this?", text)
        self.assertIn("marketingstudio://open?coll=seo%3Amap_nodes&id=n_1", text)
        data = self.smtp.messages[0]["data"]
        self.assertIn("Subject: Team: Bo mentioned you: @alex can you take this?", data)
        self.assertIn("From: team@example.test", data)
        self.assertIn("text/plain", data)
        for secret in (self.alex["token"], self.bo["token"], "secret-path"):
            self.assertNotIn(secret, text)
            self.assertNotIn(secret, data)
        # the webhook URL is write-only
        p = self.ok("GET", "/api/v1/notify/prefs", token=self.alex["token"])["prefs"]
        self.assertNotIn("secret-path", json.dumps(p))
        self.assertEqual((p["webhook"]["set"], p["webhook"]["host"]), (True, "127.0.0.1"))
        # test messages
        self.assertTrue(self.ok("POST", "/api/v1/notify/test", {"channel": "webhook"}, self.alex["token"])["ok"])
        self.assertTrue(self.ok("POST", "/api/v1/notify/test", {"channel": "email"}, self.alex["token"])["ok"])
        self.assertEqual((len(self.hook.posts), len(self.smtp.messages)), (2, 2))
        # removing the URL: the webhook is no longer used
        p = self.prefs(self.alex, {"webhook": {"url": None}})
        self.assertFalse(p["webhook"]["set"])
        self.comment(self.bo, "email", "3", "@alex again")
        self.tick()
        self.assertEqual(len(self.hook.posts), 2)
        self.assertEqual(len(self.smtp.messages), 3)

    def test_focus_holds_and_releases_one_digest(self):
        self.prefs(self.alex, {"follow": {"ads": True}, "webhook": {"on": True, "url": self.hook.url}})
        until = "2027-01-15T09:00:00Z"
        p = self.prefs(self.alex, {"focus": {"on": True, "until": until}})
        self.assertTrue(p["focus"]["active"])
        self.assertEqual(p["focus"]["until"], until)
        stream = self.open_stream(self.alex["token"])
        self.push(self.bo, "ads")
        self.comment(self.cy, "email", "8", "@alex quick one")
        ev = self.wait_for(stream, "notify")
        self.assertTrue(ev["quiet"])  # only the bell count
        self.tick()
        self.assertEqual(self.hook.posts, [])
        self.assertEqual(self.notes(self.alex)["unread"], 2)
        self.clock.advance(3600)  # Focus ends at 09:00
        self.tick()
        self.assertEqual(len(self.hook.posts), 1)
        self.assertIn("2 notifications while you were in Focus", self.hook.texts()[0])
        # Focus until turned off: held until the PUT that turns it off, then one digest
        self.prefs(self.alex, {"focus": {"on": True, "until": None}})
        self.push(self.bo, "ads")
        self.clock.advance(86400)
        self.tick()
        self.assertEqual(len(self.hook.posts), 1)
        self.prefs(self.alex, {"focus": {"on": False}})
        self.tick()
        self.assertEqual(len(self.hook.posts), 2)
        self.assertIn("1 notification while you were in Focus", self.hook.texts()[1])
        # afterwards: instant again, no digest wording
        self.push(self.bo, "ads")
        self.tick()
        self.assertEqual(len(self.hook.posts), 3)
        self.assertNotIn("while you were", self.hook.texts()[2])

    def test_away_routes_reviews_to_an_eligible_backup(self):
        bo, cy, dee = (w["member"]["id"] for w in (self.bo, self.cy, self.dee))
        self.prefs(self.bo, {"email": {"on": True}, "away": {"on": True, "backup": cy}})
        self.write(self.alex, "email", "60", 0, {"fields": {"subject": "Sale"}})
        rv = self.ok("POST", "/api/v1/reviews", {"items": [{"coll": "email", "id": "60", "version": 1}],
                                                 "reviewers": [bo]}, self.alex["token"])
        got = self.ok("GET", "/api/v1/reviews/%d" % rv["id"], token=self.alex["token"])
        self.assertEqual(got["reviewers"], [bo, cy])
        n = self.notes(self.cy)["notifications"]
        self.assertEqual(n[0]["kind"], "review_requested")
        self.assertEqual(n[0]["data"]["backup_for"], bo)
        self.assertIn("backup", n[0]["text"])
        # the backup may decide it
        conf = [{"coll": "email", "id": "60", "content_hash": got["items"][0]["hash"]}]
        self.ok("POST", "/api/v1/reviews/%d/decide" % rv["id"], {"decision": "approve", "confirm": conf}, self.cy["token"])
        # Bo still sees the request on the bell; email waits while Bo is away
        self.assertEqual(self.kinds(self.bo), ["review_requested"])
        self.tick()
        self.assertEqual(self.smtp.messages, [])
        # an ineligible backup (not an approver) is not added
        self.prefs(self.bo, {"away": {"backup": dee}})
        p = self.ok("GET", "/api/v1/notify/prefs", token=self.bo["token"])["prefs"]
        self.assertFalse(p["away"]["backup_ok"])
        self.write(self.alex, "email", "61", 0, {"fields": {"subject": "Sale 2"}})
        rv2 = self.ok("POST", "/api/v1/reviews", {"items": [{"coll": "email", "id": "61", "version": 1}],
                                                  "reviewers": [bo]}, self.alex["token"])
        self.assertEqual(self.ok("GET", "/api/v1/reviews/%d" % rv2["id"], token=self.alex["token"])["reviewers"], [bo])
        self.assertEqual(self.kinds(self.dee), [])
        # an approver who wrote an item in the review is not eligible either
        self.prefs(self.bo, {"away": {"backup": cy}})
        self.write(self.cy, "email", "62", 0, {"fields": {"subject": "Cy's"}})
        rv3 = self.ok("POST", "/api/v1/reviews", {"items": [{"coll": "email", "id": "62", "version": 1}],
                                                  "reviewers": [bo]}, self.alex["token"])
        self.assertEqual(self.ok("GET", "/api/v1/reviews/%d" % rv3["id"], token=self.alex["token"])["reviewers"], [bo])
        # back: one digest by email with everything that waited
        self.prefs(self.bo, {"away": {"on": False}})
        self.tick()
        self.assertEqual(len(self.smtp.messages), 1)
        self.assertIn("3 notifications while you were away", self.smtp.messages[0]["data"])
        # not away: a review to Bo stays with Bo
        self.write(self.alex, "email", "63", 0, {"fields": {"subject": "Sale 3"}})
        rv4 = self.ok("POST", "/api/v1/reviews", {"items": [{"coll": "email", "id": "63", "version": 1}],
                                                  "reviewers": [bo]}, self.alex["token"])
        self.assertEqual(self.ok("GET", "/api/v1/reviews/%d" % rv4["id"], token=self.alex["token"])["reviewers"], [bo])

    def test_retry_with_backoff_then_give_up(self):
        self.prefs(self.alex, {"webhook": {"on": True, "url": self.hook.url}})
        self.hook.statuses = [500, 503]
        self.comment(self.bo, "email", "9", "@alex one")
        self.tick()
        self.assertEqual(len(self.hook.posts), 1)
        p = self.ok("GET", "/api/v1/notify/prefs", token=self.alex["token"])["prefs"]
        self.assertIn("HTTP 500", p["webhook"]["last_error"])
        self.tick()  # backing off: no new attempt at the same moment
        self.assertEqual(len(self.hook.posts), 1)
        self.clock.advance(notify.BACKOFF)
        self.tick()
        self.assertEqual(len(self.hook.posts), 2)
        self.clock.advance(notify.BACKOFF * 2 - 1)
        self.tick()
        self.assertEqual(len(self.hook.posts), 2)
        self.clock.advance(1)
        self.tick()
        self.assertEqual(len(self.hook.posts), 3)  # third try answered 200
        p = self.ok("GET", "/api/v1/notify/prefs", token=self.alex["token"])["prefs"]
        self.assertEqual(p["webhook"]["last_error"], "")
        self.tick()
        self.assertEqual(len(self.hook.posts), 3)
        # a receiver that never works: given up after MAX_TRIES, not retried for ever
        self.hook.statuses = [500] * 50
        self.comment(self.bo, "email", "9", "@alex two")
        for _ in range(notify.MAX_TRIES + 3):
            self.tick()
            self.clock.advance(notify.BACKOFF_MAX)
        self.assertEqual(len(self.hook.posts), 3 + notify.MAX_TRIES)
        with self.srv.store.lock:
            states = [r["webhook"] for r in self.srv.store.db.execute(
                "SELECT webhook FROM notifications WHERE member_id=? ORDER BY id", (self.alex["member"]["id"],))]
        self.assertEqual(states, ["sent", "failed"])

    def test_rate_limit_per_member_folds_into_a_digest(self):
        old = notify.RATE["webhook"]
        notify.RATE["webhook"] = (2, 3600.0)
        self.addCleanup(notify.RATE.__setitem__, "webhook", old)
        self.prefs(self.alex, {"follow": {"ads": True}, "webhook": {"on": True, "url": self.hook.url}})
        for _ in range(4):
            self.push(self.bo, "ads")
            self.tick()
        self.assertEqual(len(self.hook.posts), 2)
        self.clock.advance(3600)
        self.tick()
        self.assertEqual(len(self.hook.posts), 3)
        self.assertIn("2 notifications", self.hook.texts()[2])

    def test_unknown_or_inactive_members_get_nothing(self):
        self.prefs(self.dee, {"webhook": {"on": True, "url": self.hook.url}})
        self.comment(self.bo, "email", "4", "@dee hello")
        self.ok("PATCH", "/api/v1/members/%d" % self.dee["member"]["id"], {"active": False}, self.owner["token"])
        self.tick()
        self.assertEqual(self.hook.posts, [])


    def test_review_to_you_instant_on_bell_in_digest_mode(self):
        self.prefs(self.bo, {"bell": {"timing": "daily"}, "follow": {"email": True}})
        self.push(self.alex, "email")  # followed: waits for the daily digest on the bell
        self.write(self.alex, "email", "70", 0, {"fields": {"subject": "Hi"}})
        self.ok("POST", "/api/v1/reviews", {"items": [{"coll": "email", "id": "70", "version": 1}],
                                            "reviewers": [self.bo["member"]["id"]]}, self.alex["token"])
        self.assertEqual(self.kinds(self.bo), ["review_requested"])
        self.clock.advance(3600)  # 09:00, the daily digest
        self.tick()
        self.assertEqual(sorted(self.kinds(self.bo)), ["push", "review_requested"])

    def test_redirects_are_not_followed(self):
        self.prefs(self.alex, {"webhook": {"on": True, "url": self.hook.url}})
        self.hook.statuses = [302]
        self.comment(self.bo, "email", "9", "@alex one")
        self.tick()
        self.assertEqual([p["path"] for p in self.hook.posts], ["/hook/secret-path"])  # never /elsewhere
        p = self.ok("GET", "/api/v1/notify/prefs", token=self.alex["token"])["prefs"]
        self.assertIn("HTTP 302", p["webhook"]["last_error"])

    def test_address_checked_again_at_send_time(self):
        self.prefs(self.alex, {"webhook": {"on": True, "url": self.hook.url}})
        self.srv.notifier.allow_private = False  # as if the host now resolved to a private address
        self.comment(self.bo, "email", "9", "@alex one")
        self.tick()
        self.assertEqual(self.hook.posts, [])
        p = self.ok("GET", "/api/v1/notify/prefs", token=self.alex["token"])["prefs"]
        self.assertTrue(p["webhook"]["last_error"])  # refused before any connection (plain http, private address)
        self.assertNotIn("secret-path", p["webhook"]["last_error"])
        # the same for an https URL that now resolves to a loopback address
        with self.assertRaises(ApiError) as cm:
            notify.post_webhook("https://127.0.0.1:%d/hook/secret-path" % self.hook.srv.server_address[1], "x")
        self.assertIn("private", cm.exception.message)
        self.assertEqual(self.hook.posts, [])

    def test_payloads_hold_no_other_members_emails(self):
        self.prefs(self.bo, {"email": {"on": True}, "webhook": {"on": True, "url": self.hook.url}})
        self.write(self.alex, "email", "71", 0, {"fields": {"subject": "Hi"}})
        self.ok("POST", "/api/v1/reviews", {"items": [{"coll": "email", "id": "71", "version": 1}],
                                            "reviewers": [self.bo["member"]["id"]]}, self.alex["token"])
        self.comment(self.cy, "email", "71", "@bo please")
        self.tick()
        self.assertEqual(len(self.hook.posts), 1)
        self.assertEqual(len(self.smtp.messages), 1)
        blobs = [json.dumps(self.hook.posts[0]["body"]), self.smtp.messages[0]["data"],
                 json.dumps(self.notes(self.bo))]
        for blob in blobs:
            for other in ("owner@example.com", "alex@example.com", "cy@example.com", "dee@example.com"):
                self.assertNotIn(other, blob)
            for tok in (self.bo["token"], self.cy["token"], self.alex["token"]):
                self.assertNotIn(tok, blob)

    def test_members_cannot_read_each_others_notifications_or_prefs(self):
        self.prefs(self.alex, {"webhook": {"on": True, "url": self.hook.url}, "focus": {"on": True, "until": None},
                               "follow": {"seo": True}})
        self.comment(self.bo, "email", "1", "@alex private note")
        bo_view = self.ok("GET", "/api/v1/notify/prefs", token=self.bo["token"])["prefs"]
        self.assertFalse(bo_view["webhook"]["set"])
        self.assertFalse(bo_view["focus"]["active"])
        self.assertFalse(bo_view["follow"]["seo"])
        self.assertEqual(self.notes(self.bo)["notifications"], [])
        # a member id in the body or the query changes nothing: always the caller's own
        alex_id = self.alex["member"]["id"]
        self.call("PUT", "/api/v1/notify/prefs", {"member_id": alex_id, "focus": {"on": False}}, self.bo["token"])
        self.assertTrue(self.ok("GET", "/api/v1/notify/prefs", token=self.alex["token"])["prefs"]["focus"]["active"])
        self.assertEqual(self.notes(self.bo, member_id=alex_id)["notifications"], [])
        # no token: refused
        self.assertEqual(self.call("GET", "/api/v1/notifications")[0], 401)
        self.assertEqual(self.call("GET", "/api/v1/notify/prefs")[0], 401)

class TestWebhookChecks(unittest.TestCase):
    @staticmethod
    def resolver(*addrs):
        return lambda host, port, *a: [(socket.AF_INET6 if ":" in x else socket.AF_INET, socket.SOCK_STREAM, 6, "",
                                        (x, port)) for x in addrs]

    def bad(self, url, allow=False, resolve=None):
        with self.assertRaises(ApiError) as cm:
            notify.check_webhook_url(url, allow, resolve or self.resolver("93.184.216.34"))
        self.assertEqual(cm.exception.status, 400)
        return cm.exception.message

    def test_https_public_only(self):
        ok = notify.check_webhook_url("https://hooks.example.com/services/T/B/x?a=1", False,
                                      self.resolver("93.184.216.34"))
        self.assertEqual(ok[:4], ("https", "hooks.example.com", 443, "/services/T/B/x?a=1"))
        self.assertIn("https", self.bad("http://hooks.example.com/x"))
        self.bad("ftp://hooks.example.com/x")
        self.bad("https://user:pw@hooks.example.com/x")
        self.bad("https:///nohost")
        self.bad("https://hooks.example.com/a b")
        self.bad("https://hooks.example.com/" + "a" * 3000)
        self.bad("")
        for addr in ("127.0.0.1", "10.1.2.3", "192.168.1.10", "172.16.0.1", "169.254.169.254", "0.0.0.0", "::1",
                     "fc00::1", "fe80::1", "::ffff:127.0.0.1", "100.64.0.1", "224.0.0.1"):
            self.assertIn("private", self.bad("https://hooks.example.com/x", resolve=self.resolver(addr)) + "private")
        # one bad address among good ones is enough to refuse (no picking the public one)
        self.bad("https://hooks.example.com/x", resolve=self.resolver("93.184.216.34", "10.0.0.1"))

        def nxdomain(*a):
            raise socket.gaierror("no such host")
        self.assertIn("resolved", self.bad("https://nope.invalid/x", resolve=nxdomain))

    def test_allow_private_for_tests(self):
        ok = notify.check_webhook_url("http://127.0.0.1:9/x", True, self.resolver("127.0.0.1"))
        self.assertEqual(ok[4], ["127.0.0.1"])
        notify.check_webhook_url("https://10.0.0.5/x", True, self.resolver("10.0.0.5"))
        # plain http is only for private addresses, even then
        self.bad("http://hooks.example.com/x", allow=True, resolve=self.resolver("93.184.216.34"))
        self.bad("https://x.example/x", allow=True, resolve=self.resolver("224.0.0.1"))

    def test_server_refuses_private_without_the_flag(self):
        d = tempfile.mkdtemp(prefix="teamserver-test-")
        self.addCleanup(shutil.rmtree, d, True)
        srv = TeamServer(d, host="127.0.0.1", port=0, notify_interval=None).start()
        self.addCleanup(srv.stop)
        tok = srv.store.create_invite("o@example.com", "owner", "O")["token"]
        import http.client  # noqa: PLC0415

        def call(method, path, body=None, token=None):
            c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
            h = {"Content-Type": "application/json"}
            if token:
                h["Authorization"] = "Bearer " + token
            c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
            r = c.getresponse()
            out = json.loads(r.read().decode() or "null")
            c.close()
            return r.status, out
        s, j = call("POST", "/api/v1/join", {"invite": tok, "device_name": "Mac"})
        token = j["device_token"]
        for url in ("https://127.0.0.1/x", "http://127.0.0.1:9/x", "https://localhost/x", "https://10.0.0.1/x"):
            s, out = call("PUT", "/api/v1/notify/prefs", {"webhook": {"on": True, "url": url}}, token)
            self.assertEqual(s, 400, (url, out))
        s, out = call("GET", "/api/v1/notify/prefs", token=token)
        self.assertFalse(out["prefs"]["webhook"]["set"])
        self.assertFalse(out["server"]["email"])  # no SMTP settings: email not offered
        s, out = call("POST", "/api/v1/notify/test", {"channel": "email"}, token)
        self.assertEqual(s, 400)


class TestSmtpSettings(unittest.TestCase):
    def test_from_args_and_env(self):
        import argparse  # noqa: PLC0415
        p = argparse.ArgumentParser()
        notify.add_cli_args(p)
        env = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith("TEAMSERVER_SMTP")}
        self.addCleanup(os.environ.update, env)
        self.assertIsNone(notify.smtp_from_env_and_args(p.parse_args([])))
        os.environ["TEAMSERVER_SMTP_PASSWORD"] = "pw-from-env"
        self.addCleanup(os.environ.pop, "TEAMSERVER_SMTP_PASSWORD", None)
        s = notify.smtp_from_env_and_args(p.parse_args(["--smtp-host", "mail.example.com", "--smtp-from",
                                                        "team@example.com", "--smtp-user", "u"]))
        self.assertEqual(s, {"host": "mail.example.com", "port": 587, "user": "u", "password": "pw-from-env",
                             "from": "team@example.com", "starttls": True})
        s = notify.smtp_from_env_and_args(p.parse_args(["--smtp-host", "h", "--smtp-from", "f@x.y", "--smtp-port", "25",
                                                        "--no-smtp-starttls"]))
        self.assertEqual((s["port"], s["starttls"]), (25, False))
        with self.assertRaises(SystemExit):
            notify.smtp_from_env_and_args(p.parse_args(["--smtp-host", "h"]))

    def test_due_times(self):
        prefs = notify.default_prefs()
        t = 1_800_000_000.0 + 600  # 08:10 UTC
        self.assertEqual(notify.due_at(t, "instant", prefs), t)
        self.assertEqual(notify.due_at(t, "hourly", prefs), 1_800_000_000.0 + 3600)
        self.assertEqual(notify.due_at(t, "daily", prefs), 1_800_000_000.0 + 3600)  # 09:00 the same day
        prefs["daily_at"] = "08:00"
        self.assertEqual(notify.due_at(t, "daily", prefs), 1_800_000_000.0 + 86400)  # tomorrow 08:00
        prefs.update(daily_at="09:00", tz_offset=60)  # 09:00 at UTC+1 = 08:00 UTC: passed, tomorrow
        self.assertEqual(notify.due_at(t, "daily", prefs), 1_800_000_000.0 + 86400)


if __name__ == "__main__":
    unittest.main()
