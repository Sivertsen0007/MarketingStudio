"""End-to-end tests: a real server on a free port with a temp data dir and an injected clock."""
import contextlib
import http.client
import io
import json
import os
import queue
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from teamserver.app import TeamServer  # noqa: E402
from teamserver import store as store_mod  # noqa: E402


class Clock:
    """A clock tests can move forward."""

    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="teamserver-test-")
        self.clock = Clock()
        self.srv = TeamServer(self.dir, host="127.0.0.1", port=0, public_url="https://team.example.test",
                              clock=self.clock, ping_interval=0.3).start()
        self.port = self.srv.port
        self.owner = self.join_new("owner@example.com", "owner", "Sam Owner")

    def tearDown(self):
        self.srv.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    # helpers

    def extra_server(self, **kw):
        """A second server with its own data dir and settings, plus a joined owner."""
        d = tempfile.mkdtemp(prefix="teamserver-test-")
        srv = TeamServer(d, host="127.0.0.1", port=0, clock=self.clock, **kw).start()
        self.addCleanup(shutil.rmtree, d, True)
        self.addCleanup(srv.stop)
        return srv

    def call_on(self, srv, *args, **kw):
        port = self.port
        self.port = srv.port
        try:
            return self.call(*args, **kw)
        finally:
            self.port = port

    def call(self, method, path, body=None, token=None, raw=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = dict(headers or {})
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        if data is not None:
            h["Content-Type"] = "application/json"
        if token:
            h["Authorization"] = "Bearer " + token
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        text = r.read().decode()
        c.close()
        try:
            out = json.loads(text) if text else None
        except ValueError:
            out = text
        return r.status, out

    def ok(self, method, path, body=None, token=None):
        status, out = self.call(method, path, body, token)
        self.assertEqual(status, 200, out)
        return out

    def invite(self, email, role, name=None):
        return self.srv.store.create_invite(email, role, name)["token"]

    def join_new(self, email, role, name=None):
        out = self.ok("POST", "/api/v1/join", {"invite": self.invite(email, role, name), "device_name": "Mac"})
        return {"token": out["device_token"], "member": out["member"]}

    def put(self, who, coll, iid, base, data):
        return self.ok("POST", "/api/v1/items",
                       {"ops": [{"op": "put", "coll": coll, "id": iid, "base_version": base, "data": data}]},
                       who["token"])

    def open_stream(self, token):
        """Read SSE events in a background thread into a queue of (event, data)."""
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", "/api/v1/stream?token=" + token)
        r = c.getresponse()
        self.assertEqual(r.status, 200)
        self.assertTrue(r.getheader("Content-Type").startswith("text/event-stream"))
        q = queue.Queue()

        def reader():
            ev = None
            try:
                while True:
                    line = r.fp.readline()
                    if not line:
                        break
                    line = line.decode().rstrip("\n")
                    if line.startswith(": ping"):
                        q.put(("ping", None))
                    elif line.startswith("event: "):
                        ev = line[7:]
                    elif line.startswith("data: ") and ev:
                        q.put((ev, json.loads(line[6:])))
                        ev = None
            except Exception:
                pass
            q.put(("closed", None))

        threading.Thread(target=reader, daemon=True).start()
        self.addCleanup(c.close)
        return q

    def saw(self, q, kind, timeout=3):
        """(True, data) once an event of `kind` arrives, (False, None) on timeout."""
        end = time.time() + timeout
        while time.time() < end:
            try:
                ev, data = q.get(timeout=max(end - time.time(), 0.01))
            except queue.Empty:
                break
            if ev == kind:
                return True, data
        return False, None

    def wait_for(self, q, kind, timeout=3):
        return self.saw(q, kind, timeout)[1]


class TestAuth(Base):
    def test_health_no_auth(self):
        out = self.ok("GET", "/api/v1/health")
        self.assertTrue(out["ok"])
        self.assertEqual(out["protocol"], 1)
        self.assertIn("version", out)
        self.assertIn("team", out)

    def test_join_flow(self):
        m = self.owner["member"]
        self.assertEqual(m["role"], "owner")
        self.assertEqual(m["email"], "owner@example.com")
        self.assertEqual(m["name"], "Sam Owner")
        self.assertTrue(m["colour"].startswith("#"))
        me = self.ok("GET", "/api/v1/me", token=self.owner["token"])
        self.assertEqual(me["member"]["id"], m["id"])
        events = self.ok("GET", "/api/v1/events?since=0", token=self.owner["token"])["events"]
        self.assertEqual(events[0]["kind"], "join")

    def test_tokens_stored_hashed(self):
        row = self.srv.store.q1("SELECT token_hash FROM devices LIMIT 1")
        self.assertEqual(row["token_hash"], store_mod.hash_token(self.owner["token"]))
        self.assertNotIn(self.owner["token"], json.dumps([dict(r) for r in self.srv.store.q("SELECT * FROM devices")]))

    def test_bad_invite(self):
        status, out = self.call("POST", "/api/v1/join", {"invite": "nope", "device_name": "x"})
        self.assertEqual(status, 401)
        self.assertIn("error", out)

    def test_expired_invite(self):
        tok = self.invite("late@example.com", "editor")
        self.clock.advance(7 * 86400 + 1)
        status, out = self.call("POST", "/api/v1/join", {"invite": tok})
        self.assertEqual(status, 401)
        self.assertIn("expired", out["error"])

    def test_reused_invite(self):
        tok = self.invite("once@example.com", "editor")
        self.ok("POST", "/api/v1/join", {"invite": tok})
        status, out = self.call("POST", "/api/v1/join", {"invite": tok})
        self.assertEqual(status, 401)
        self.assertIn("used", out["error"])

    def test_auth_required(self):
        for path in ("/api/v1/items", "/api/v1/events", "/api/v1/me", "/api/v1/members", "/api/v1/claims",
                     "/api/v1/presence", "/api/v1/mentions"):
            status, out = self.call("GET", path)
            self.assertEqual(status, 401, path)
            self.assertIn("error", out)
        status, _ = self.call("GET", "/api/v1/me", token="garbage")
        self.assertEqual(status, 401)

    def test_revoke_makes_token_fail(self):
        ed = self.join_new("ed@example.com", "editor", "Ed")
        me = self.ok("GET", "/api/v1/me", token=ed["token"])
        q = self.open_stream(ed["token"])
        self.ok("DELETE", "/api/v1/devices/%d" % me["device"]["id"], token=self.owner["token"])
        status, _ = self.call("GET", "/api/v1/me", token=ed["token"])
        self.assertEqual(status, 401)
        self.assertTrue(self.saw(q, "closed")[0])  # the open stream was closed

    def test_member_management_roles(self):
        ed = self.join_new("ed@example.com", "editor", "Ed")
        status, _ = self.call("POST", "/api/v1/invites", {"email": "x@example.com", "role": "editor"}, ed["token"])
        self.assertEqual(status, 403)
        out = self.ok("POST", "/api/v1/invites", {"email": "x@example.com", "role": "viewer"}, self.owner["token"])
        self.assertTrue(out["web"].startswith("https://team.example.test/join#"))
        self.assertTrue(out["app"].startswith("marketingstudio://join?server=https%3A%2F%2Fteam.example.test&invite="))
        status, _ = self.call("PATCH", "/api/v1/members/%d" % self.owner["member"]["id"], {"role": "viewer"},
                              ed["token"])
        self.assertEqual(status, 403)
        m = self.ok("PATCH", "/api/v1/members/%d" % ed["member"]["id"], {"role": "admin", "approver": True},
                    self.owner["token"])
        self.assertEqual((m["role"], m["approver"]), ("admin", True))
        status, _ = self.call("PATCH", "/api/v1/members/%d" % self.owner["member"]["id"], {"role": "editor"},
                              self.owner["token"])
        self.assertEqual(status, 409)  # last owner
        self.ok("PATCH", "/api/v1/members/%d" % ed["member"]["id"], {"active": False}, self.owner["token"])
        status, _ = self.call("GET", "/api/v1/me", token=ed["token"])
        self.assertEqual(status, 401)

    def test_colours_distinct(self):
        colours = {self.owner["member"]["colour"]}
        for i in range(5):
            colours.add(self.join_new("m%d@example.com" % i, "editor")["member"]["colour"])
        self.assertEqual(len(colours), 6)


class TestInput(Base):
    def test_bad_json_and_limits(self):
        t = self.owner["token"]
        status, out = self.call("POST", "/api/v1/items", raw=b"{not json", token=t)
        self.assertEqual(status, 400)
        self.assertIn("error", out)
        status, out = self.call("POST", "/api/v1/items", {"ops": [{"op": "put", "coll": "x" * 500, "id": "1",
                                                                    "base_version": 0, "data": {}}]}, t)
        self.assertEqual(status, 200)
        self.assertFalse(out["results"][0]["ok"])
        self.assertIn("too long", out["results"][0]["error"])
        status, out = self.call("POST", "/api/v1/items", raw=b"x", token=t,
                                headers={"Content-Length": str(50 * 1024 * 1024)})
        self.assertEqual(status, 413)
        status, out = self.call("GET", "/api/v1/nothing", token=t)
        self.assertEqual(status, 404)
        self.assertEqual(out, {"error": "not found"})
        status, out = self.call("GET", "/api/v1/items?since=abc", token=t)
        self.assertEqual(status, 400)


class TestItems(Base):
    def test_viewer_cannot_write(self):
        v = self.join_new("v@example.com", "viewer", "Vee")
        status, out = self.call("POST", "/api/v1/items",
                                {"ops": [{"op": "put", "coll": "email", "id": "a", "base_version": 0, "data": {}}]},
                                v["token"])
        self.assertEqual(status, 403)
        self.assertIn("error", out)
        self.ok("GET", "/api/v1/items?since=0", token=v["token"])  # can read

    def test_conflict(self):
        ed = self.join_new("ed@example.com", "editor", "Ed")
        r = self.put(self.owner, "ads:1", "kw1", 0, {"text": "a", "bid": 1})["results"][0]
        self.assertEqual(r, {"coll": "ads:1", "id": "kw1", "ok": True, "version": 1})
        self.assertTrue(self.put(self.owner, "ads:1", "kw1", 1, {"text": "b", "bid": 1})["results"][0]["ok"])
        r = self.put(ed, "ads:1", "kw1", 1, {"text": "a", "bid": 2})["results"][0]
        self.assertFalse(r["ok"])
        self.assertTrue(r["conflict"])
        self.assertEqual(r["server"]["version"], 2)
        self.assertEqual(r["server"]["data"], {"text": "b", "bid": 1})
        self.assertEqual(r["server"]["updated_by"], self.owner["member"]["id"])
        r = self.put(ed, "ads:1", "kw1", 2, {"text": "b", "bid": 2})["results"][0]
        self.assertEqual((r["ok"], r["version"]), (True, 3))
        # new item that already exists -> conflict
        r = self.put(ed, "ads:1", "kw1", 0, {"x": 1})["results"][0]
        self.assertTrue(r["conflict"])

    def test_cursor_and_delete(self):
        t = self.owner["token"]
        out = self.put(self.owner, "email", "a", 0, {"n": 1})
        c1 = out["cursor"]
        self.put(self.owner, "email", "b", 0, {"n": 2})
        everything = self.ok("GET", "/api/v1/items?since=0", token=t)
        self.assertEqual([i["id"] for i in everything["items"]], ["a", "b"])
        newer = self.ok("GET", "/api/v1/items?since=%d" % c1, token=t)
        self.assertEqual([i["id"] for i in newer["items"]], ["b"])
        self.assertEqual(self.ok("GET", "/api/v1/items?since=%d" % newer["cursor"], token=t)["items"], [])
        out = self.ok("POST", "/api/v1/items", {"ops": [{"op": "delete", "coll": "email", "id": "a",
                                                         "base_version": 1}]}, t)
        self.assertEqual(out["results"][0]["version"], 2)
        after = self.ok("GET", "/api/v1/items?since=%d" % newer["cursor"], token=t)["items"]
        self.assertEqual(len(after), 1)
        self.assertTrue(after[0]["deleted"])
        self.assertEqual(after[0]["version"], 2)
        out = self.ok("POST", "/api/v1/items", {"ops": [{"op": "delete", "coll": "email", "id": "zz",
                                                         "base_version": 0}]}, t)
        self.assertFalse(out["results"][0]["ok"])

    def test_changeset_and_limit_paging(self):
        t = self.owner["token"]
        self.ok("POST", "/api/v1/items", {"ops": [
            {"op": "put", "coll": "social", "id": str(i), "base_version": 0, "data": {"i": i},
             "changeset": {"id": "cs1", "name": "Spring"}} for i in range(5)]}, t)
        page = self.ok("GET", "/api/v1/items?since=0&limit=2", token=t)
        self.assertEqual(len(page["items"]), 2)
        self.assertTrue(page["more"])
        self.assertEqual(page["items"][0]["changeset"], {"id": "cs1", "name": "Spring"})
        rest = self.ok("GET", "/api/v1/items?since=%d" % page["cursor"], token=t)
        self.assertEqual(len(rest["items"]), 3)
        self.assertFalse(rest["more"])

    def test_edit_session_grouping(self):
        t = self.owner["token"]
        ed = self.join_new("ed@example.com", "editor", "Ed")
        for i in range(3):
            self.put(self.owner, "ads:4387091070", "k%d" % i, 0, {"i": i})
            self.clock.advance(60)
        self.put(self.owner, "ads:4387091070", "k0", 1, {"i": 9})  # same item again: still 3 distinct
        self.put(ed, "email", "e1", 0, {})
        sessions = [e for e in self.ok("GET", "/api/v1/events?since=0", token=t)["events"]
                    if e["kind"] == "edit_session"]
        self.assertEqual(len(sessions), 2)  # one per member
        mine = [e for e in sessions if e["actor"] == self.owner["member"]["id"]][0]
        self.assertEqual(mine["summary"], "3 changes in ads:4387091070")
        self.assertEqual(mine["data"]["count"], 3)
        self.assertEqual(mine["data"]["writes"], 4)
        self.assertEqual(mine["coll"], "ads:4387091070")
        self.assertEqual(mine["area"], "ads")
        session_id = mine["data"]["session"]
        # updating in place moves the event to a newer seq, same session id
        before = self.ok("GET", "/api/v1/events?since=0", token=t)["seq"]
        self.clock.advance(9 * 60)
        self.put(self.owner, "ads:4387091070", "k3", 0, {})
        newer = self.ok("GET", "/api/v1/events?since=%d" % before, token=t)["events"]
        self.assertEqual(len(newer), 1)
        self.assertEqual(newer[0]["data"]["session"], session_id)
        self.assertEqual(newer[0]["data"]["count"], 4)
        # 10 minutes idle -> a new session
        self.clock.advance(10 * 60)
        self.put(self.owner, "seo:changes", "p1", 0, {})
        sessions = [e for e in self.ok("GET", "/api/v1/events?since=0", token=t)["events"]
                    if e["kind"] == "edit_session" and e["actor"] == self.owner["member"]["id"]]
        self.assertEqual(len(sessions), 2)
        self.assertNotEqual(sessions[1]["data"]["session"], session_id)
        self.assertEqual(sessions[1]["summary"], "1 change in seo:changes")
        self.assertEqual(sessions[1]["id"], "p1")

    def test_post_event(self):
        t = self.owner["token"]
        ev = self.ok("POST", "/api/v1/events", {"kind": "push", "area": "ads", "coll": "ads:1",
                                                "summary": "Pushed 12 changes", "data": {"n": 12}}, t)
        self.assertEqual((ev["kind"], ev["actor"], ev["data"]), ("push", self.owner["member"]["id"], {"n": 12}))
        status, _ = self.call("POST", "/api/v1/events", {"kind": "edit_session", "area": "ads", "summary": "x"}, t)
        self.assertEqual(status, 400)
        v = self.join_new("v@example.com", "viewer")
        status, _ = self.call("POST", "/api/v1/events", {"kind": "push", "area": "ads", "summary": "x"}, v["token"])
        self.assertEqual(status, 403)


class TestComments(Base):
    def test_comments_mentions_counts(self):
        t = self.owner["token"]
        alex = self.join_new("alex.w@example.com", "editor", "Alex Whitley")
        kim = self.join_new("kim@example.com", "viewer", "Kim")
        c = self.ok("POST", "/api/v1/comments", {"coll": "email", "id": "c1",
                                                 "body": "Hey @AlexWhitley and @KIM, see this. mail me@x.com"}, t)
        self.assertEqual(sorted(c["mentions"]), sorted([alex["member"]["id"], kim["member"]["id"]]))
        c2 = self.ok("POST", "/api/v1/comments", {"coll": "email", "id": "c1", "body": "Done @alex.w.",
                                                  "parent": c["id"]}, kim["token"])  # viewer may comment
        self.assertEqual(c2["mentions"], [alex["member"]["id"]])
        self.assertEqual(c2["parent"], c["id"])
        self.ok("POST", "/api/v1/comments", {"coll": "email", "id": "c2", "body": "@sam self-mention"}, t)
        thread = self.ok("GET", "/api/v1/comments?coll=email&id=c1", token=t)["comments"]
        self.assertEqual([x["id"] for x in thread], [c["id"], c2["id"]])

        unread = self.ok("GET", "/api/v1/mentions?unread=1", token=alex["token"])["mentions"]
        self.assertEqual(len(unread), 2)
        self.ok("POST", "/api/v1/mentions/read", {"ids": [c["id"]]}, alex["token"])
        unread = self.ok("GET", "/api/v1/mentions?unread=1", token=alex["token"])["mentions"]
        self.assertEqual([m["comment"]["id"] for m in unread], [c2["id"]])
        allm = self.ok("GET", "/api/v1/mentions", token=alex["token"])["mentions"]
        self.assertEqual(len(allm), 2)
        self.assertEqual(self.ok("GET", "/api/v1/mentions?unread=1", token=t)["mentions"], [])

        counts = self.ok("GET", "/api/v1/comments/counts?coll=email", token=t)
        self.assertEqual(counts["c1"]["total"], 2)
        self.assertEqual(counts["c1"]["open"], 1)
        self.assertEqual(counts["c2"]["total"], 1)
        self.assertIsNotNone(counts["c1"]["latest"])

        # resolve: viewer cannot, editor can; edit body: author only
        status, _ = self.call("PATCH", "/api/v1/comments/%d" % c["id"], {"resolved": True}, kim["token"])
        self.assertEqual(status, 403)
        r = self.ok("PATCH", "/api/v1/comments/%d" % c["id"], {"resolved": True}, alex["token"])
        self.assertTrue(r["resolved"])
        self.assertEqual(self.ok("GET", "/api/v1/comments/counts?coll=email", token=t)["c1"]["open"], 0)
        status, _ = self.call("PATCH", "/api/v1/comments/%d" % c["id"], {"body": "hijack"}, alex["token"])
        self.assertEqual(status, 403)
        e = self.ok("PATCH", "/api/v1/comments/%d" % c["id"], {"body": "Only @kim now"}, t)
        self.assertEqual(e["mentions"], [kim["member"]["id"]])
        self.assertIsNotNone(e["edited_at"])

    def test_ambiguous_mention(self):
        self.join_new("jo@example.com", "editor", "Jo One")
        self.join_new("jo@other.example", "editor", "Jo")  # email local part 'jo' twice
        c = self.ok("POST", "/api/v1/comments", {"coll": "email", "id": "x", "body": "@jo hi"}, self.owner["token"])
        self.assertEqual(c["mentions"], [])

    def test_explicit_mentions(self):
        ed = self.join_new("ed@example.com", "editor", "Ed")
        c = self.ok("POST", "/api/v1/comments", {"coll": "email", "id": "x", "body": "look",
                                                 "mentions": [ed["member"]["id"]]}, self.owner["token"])
        self.assertEqual(c["mentions"], [ed["member"]["id"]])
        status, _ = self.call("POST", "/api/v1/comments", {"coll": "email", "id": "x", "body": "look",
                                                           "mentions": [999]}, self.owner["token"])
        self.assertEqual(status, 400)


class TestPresenceClaims(Base):
    def test_presence_expiry(self):
        t = self.owner["token"]
        ed = self.join_new("ed@example.com", "editor", "Ed")
        self.ok("POST", "/api/v1/presence", {"area": "ads", "coll": "ads:1", "id": "kw1", "editing": True}, t)
        self.clock.advance(60)
        self.ok("POST", "/api/v1/presence", {"area": "email"}, ed["token"])
        live = self.ok("GET", "/api/v1/presence", token=t)["presence"]
        self.assertEqual(len(live), 2)
        self.clock.advance(61)  # owner is now 121 s old
        live = self.ok("GET", "/api/v1/presence", token=t)["presence"]
        self.assertEqual([p["member"] for p in live], [ed["member"]["id"]])
        self.assertEqual(self.srv.store.expire_presence(), 1)
        self.clock.advance(120)
        self.assertEqual(self.ok("GET", "/api/v1/presence", token=t)["presence"], [])

    def test_claims(self):
        t = self.owner["token"]
        ed = self.join_new("ed@example.com", "editor", "Ed")
        v = self.join_new("v@example.com", "viewer", "Vee")
        c = self.ok("POST", "/api/v1/claims", {"coll": "seo:new_posts", "id": "p1", "note": "writing it"}, ed["token"])
        self.assertEqual((c["member"], c["note"]), (ed["member"]["id"], "writing it"))
        status, out = self.call("POST", "/api/v1/claims", {"coll": "seo:new_posts", "id": "p1"}, t)
        self.assertEqual(status, 409)
        self.assertEqual(out["claim"]["member"], ed["member"]["id"])
        status, _ = self.call("POST", "/api/v1/claims", {"coll": "seo:new_posts", "id": "p2"}, v["token"])
        self.assertEqual(status, 403)
        self.assertEqual(len(self.ok("GET", "/api/v1/claims", token=v["token"])["claims"]), 1)
        status, _ = self.call("DELETE", "/api/v1/claims", {"coll": "seo:new_posts", "id": "p1"}, v["token"])
        self.assertEqual(status, 403)
        self.ok("DELETE", "/api/v1/claims", {"coll": "seo:new_posts", "id": "p1"}, ed["token"])
        self.assertEqual(self.ok("GET", "/api/v1/claims", token=t)["claims"], [])
        kinds = [e["kind"] for e in self.ok("GET", "/api/v1/events?since=0", token=t)["events"]]
        self.assertEqual(kinds.count("claim"), 2)


class TestStream(Base):
    def test_items_event_and_ping(self):
        q = self.open_stream(self.owner["token"])
        self.assertTrue(self.saw(q, "ping")[0])
        out = self.put(self.owner, "email", "a", 0, {})
        data = self.wait_for(q, "items")
        self.assertEqual(data, {"cursor": out["cursor"]})
        self.assertIsNotNone(self.wait_for(q, "event"))

    def test_mention_only_to_mentioned(self):
        ed = self.join_new("ed@example.com", "editor", "Ed")
        other = self.join_new("other@example.com", "editor", "Other")
        q_ed = self.open_stream(ed["token"])
        q_other = self.open_stream(other["token"])
        time.sleep(0.1)
        c = self.ok("POST", "/api/v1/comments", {"coll": "email", "id": "x", "body": "@ed check"},
                    self.owner["token"])
        self.assertEqual(self.wait_for(q_ed, "mention"), {"comment_id": c["id"]})
        self.assertEqual(self.wait_for(q_other, "comment"), {"coll": "email", "id": "x"})
        self.assertFalse(self.saw(q_other, "mention", timeout=1)[0])

    def test_stream_closes_after_revoke_elsewhere(self):
        """A revoke written by another process (the CLI) closes the stream at the next ping."""
        ed = self.join_new("ed@example.com", "editor", "Ed")
        q = self.open_stream(ed["token"])
        with self.srv.store.lock:
            self.srv.store.db.execute("UPDATE devices SET revoked_at=1 WHERE member_id=?", (ed["member"]["id"],))
        self.assertTrue(self.saw(q, "closed")[0])

    def test_stream_needs_auth(self):
        status, _ = self.call("GET", "/api/v1/stream")
        self.assertEqual(status, 401)


class TestReview(Base):
    """Regression tests for the first review."""

    def api_invite(self, who, email, role="editor"):
        return self.call("POST", "/api/v1/invites", {"email": email, "role": role}, who["token"])

    def redeem(self, link):
        return self.call("POST", "/api/v1/join", {"invite": link["web"].split("#", 1)[1], "device_name": "x"})

    # 1
    def test_admin_cannot_take_over_existing_member(self):
        admin = self.join_new("admin@example.com", "admin", "Ada")
        ed = self.join_new("ed@example.com", "editor", "Ed")
        for email in ("owner@example.com", "ed@example.com", "OWNER@example.com"):
            status, out = self.api_invite(admin, email, "owner" if "owner" in email.lower() else "editor")
            self.assertEqual(status, 403, (email, out))
        status, _ = self.api_invite(admin, "owner@example.com", "editor")
        self.assertEqual(status, 403)
        # own extra device: fine, same identity, role untouched
        status, link = self.api_invite(admin, "admin@example.com", "viewer")
        self.assertEqual(status, 200)
        status, out = self.redeem(link)
        self.assertEqual((out["member"]["id"], out["member"]["role"]), (admin["member"]["id"], "admin"))
        self.assertEqual((link["role"], link["device"]), ("admin", True))  # echoes the member's real role
        # nobody else, owners included, may mint a device invite for a member
        status, _ = self.api_invite(self.owner, "ed@example.com", "viewer")
        self.assertEqual(status, 403)
        # an editor may add their own device; the role stays theirs
        status, link = self.api_invite(ed, "ed@example.com", "viewer")
        self.assertEqual((status, link["role"]), (200, "editor"))
        status, out = self.redeem(link)
        self.assertEqual((out["member"]["id"], out["member"]["role"]), (ed["member"]["id"], "editor"))

    def test_new_member_invite_cannot_claim_identity_later(self):
        admin = self.join_new("admin@example.com", "admin", "Ada")
        status, stale = self.api_invite(admin, "new@example.com", "editor")
        self.assertEqual(status, 200)
        self.join_new("new@example.com", "viewer", "New")  # someone joins with that email first
        status, out = self.redeem(stale)
        self.assertEqual(status, 403, out)

    # 2
    def test_admin_cannot_revoke_owner_devices(self):
        admin = self.join_new("admin@example.com", "admin", "Ada")
        owner_dev = self.ok("GET", "/api/v1/me", token=self.owner["token"])["device"]["id"]
        status, _ = self.call("DELETE", "/api/v1/devices/%d" % owner_dev, token=admin["token"])
        self.assertEqual(status, 403)
        self.ok("GET", "/api/v1/me", token=self.owner["token"])
        own = self.ok("GET", "/api/v1/me", token=admin["token"])["device"]["id"]
        ed = self.join_new("ed@example.com", "editor", "Ed")
        ed_dev = self.ok("GET", "/api/v1/me", token=ed["token"])["device"]["id"]
        self.ok("DELETE", "/api/v1/devices/%d" % ed_dev, token=admin["token"])
        self.ok("DELETE", "/api/v1/devices/%d" % own, token=admin["token"])

    # 3
    def test_stream_closes_after_revoke_while_events_flow(self):
        srv = self.extra_server(ping_interval=30, device_check_interval=0.2)
        port = self.port
        self.port = srv.port
        try:
            tok = srv.store.create_invite("a@example.com", "owner")["token"]
            a = self.ok("POST", "/api/v1/join", {"invite": tok})
            q = self.open_stream(a["device_token"])
            with srv.store.lock:
                srv.store.db.execute("UPDATE devices SET revoked_at=1")
            # keep traffic flowing through the stream without using the revoked token
            writer = {"id": 99, "name": "W", "role": "editor"}
            closed = False
            for i in range(30):
                srv.store.write_items(writer, [{"op": "put", "coll": "email", "id": str(i), "base_version": 0,
                                                "data": {}}])
                time.sleep(0.05)
                if self.saw(q, "closed", timeout=0.01)[0]:
                    closed = True
                    break
            self.assertTrue(closed)
        finally:
            self.port = port

    # 4
    def test_connection_cap_and_timeout(self):
        srv = self.extra_server(max_connections=2, request_timeout=1.0)
        idle = [socket.create_connection(("127.0.0.1", srv.port)) for _ in range(2)]
        for s_ in idle:
            self.addCleanup(s_.close)
        time.sleep(0.2)
        status, out = self.call_on(srv, "GET", "/api/v1/health")
        self.assertEqual(status, 503, out)
        idle[0].settimeout(3)
        self.assertEqual(idle[0].recv(10), b"")  # idle socket dropped after the read timeout
        time.sleep(0.3)
        status, _ = self.call_on(srv, "GET", "/api/v1/health")
        self.assertEqual(status, 200)

    def test_streams_per_device_capped(self):
        for _ in range(5):
            self.open_stream(self.owner["token"])
        time.sleep(0.1)
        status, out = self.call("GET", "/api/v1/stream", token=self.owner["token"])
        self.assertEqual(status, 429, out)

    # 5
    def test_join_page_escapes_and_prefers_public_url(self):
        status, page = self.call("GET", "/join", headers={"Host": "evil</script><script>alert(1)//"})
        self.assertEqual(status, 200)
        self.assertNotIn("evil", page)
        self.assertIn('"https://team.example.test"', page)
        srv = self.extra_server()
        status, page = self.call_on(srv, "GET", "/join", headers={"Host": "x</script>&"})
        self.assertNotIn("</script>&", page)
        self.assertIn("x\\u003c/script\\u003e\\u0026", page)

    # 6
    def test_odd_input_is_400(self):
        t = self.owner["token"]
        status, out = self.call("POST", "/api/v1/items", raw=b'{"ops":' + b"[" * 100000 + b"]" * 100000 + b"}",
                                token=t)
        self.assertEqual((status, out), (400, {"error": "body is nested too deeply"}))
        for q in ("since=99999999999999999999", "since=-1", "since=1e3"):
            status, out = self.call("GET", "/api/v1/items?" + q, token=t)
            self.assertEqual(status, 400, (q, out))
        status, out = self.call("POST", "/api/v1/comments", {"coll": "email", "id": "x", "body": "hi",
                                                            "mentions": [10 ** 30]}, t)
        self.assertEqual(status, 400, out)

    # 7
    def test_nan_infinity_and_real_ints(self):
        t = self.owner["token"]
        for raw in (b'{"ops":[{"op":"put","coll":"e","id":"a","base_version":0,"data":{"x":NaN}}]}',
                    b'{"ops":[{"op":"put","coll":"e","id":"a","base_version":0,"data":{"x":Infinity}}]}',
                    b'{"ops":[{"op":"put","coll":"e","id":"a","base_version":0,"data":{"x":1e400}}]}'):
            status, out = self.call("POST", "/api/v1/items", raw=raw, token=t)
            self.assertEqual(status, 400, (raw, out))
        for base in (0.0, 1.5, True, "0"):
            status, out = self.call("POST", "/api/v1/items", {"ops": [{"op": "put", "coll": "e", "id": "a",
                                                                       "base_version": base, "data": {}}]}, t)
            self.assertEqual(status, 200, (base, out))  # a bad op fails alone
            self.assertEqual((out["results"][0]["ok"], out["results"][0]["id"]), (False, "a"))
            self.assertIn("base_version", out["results"][0]["error"])

    def test_owner_cannot_impersonate_co_owner(self):
        co = self.join_new("co@example.com", "owner", "Co")
        status, _ = self.api_invite(self.owner, "co@example.com", "owner")
        self.assertEqual(status, 403)
        status, _ = self.api_invite(co, "owner@example.com", "owner")
        self.assertEqual(status, 403)

    def test_reactivation_does_not_restore_tokens(self):
        ed = self.join_new("ed@example.com", "editor", "Ed")
        mid = ed["member"]["id"]
        self.ok("PATCH", "/api/v1/members/%d" % mid, {"active": False}, self.owner["token"])
        self.ok("PATCH", "/api/v1/members/%d" % mid, {"active": True}, self.owner["token"])
        status, _ = self.call("GET", "/api/v1/me", token=ed["token"])
        self.assertEqual(status, 401)
        status, _ = self.api_invite(self.owner, "ed@example.com")
        self.assertEqual(status, 403)  # only Ed (or the host CLI) can add Ed's device
        status, out = self.call("POST", "/api/v1/join", {"invite": self.invite("ed@example.com", "viewer")})
        self.assertEqual((status, out["member"]["id"], out["member"]["role"]), (200, mid, "editor"))

    def test_join_rate_limit_ignores_forwarded_for_unless_trusted(self):
        for i in range(10):
            self.call("POST", "/api/v1/join", {"invite": "bad"}, headers={"X-Forwarded-For": "10.0.0.%d" % i})
        status, _ = self.call("POST", "/api/v1/join", {"invite": "bad"}, headers={"X-Forwarded-For": "10.0.1.1"})
        self.assertEqual(status, 429)
        srv = self.extra_server(trust_proxy=True)
        for i in range(15):
            status, _ = self.call_on(srv, "POST", "/api/v1/join", {"invite": "bad"},
                                     headers={"X-Forwarded-For": "1.1.1.1, 10.0.0.%d" % i})
            self.assertEqual(status, 401)


class TestReview2(Base):
    """Regression tests for the second review."""

    def api_invite(self, who, email, role="editor"):
        return self.call("POST", "/api/v1/invites", {"email": email, "role": role}, who["token"])

    def code(self, link):
        return link["web"].split("#", 1)[1]

    def raw(self, srv_port, data, shut_write=False, timeout=5):
        """Send raw bytes, return everything the server sends back until it closes."""
        sk = socket.create_connection(("127.0.0.1", srv_port))
        self.addCleanup(sk.close)
        sk.settimeout(timeout)
        sk.sendall(data)
        if shut_write:
            sk.shutdown(socket.SHUT_WR)
        out = b""
        while True:
            try:
                chunk = sk.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            out += chunk
        return out

    # M2
    def test_invite_dies_when_inviter_demoted_or_deactivated(self):
        admin = self.join_new("admin@example.com", "admin", "Ada")
        _, a = self.api_invite(admin, "a@example.com")
        _, b = self.api_invite(admin, "b@example.com")
        # join-time recheck (even if the invite row survived)
        with self.srv.store.lock:
            self.srv.store.db.execute("UPDATE members SET role='editor' WHERE id=?", (admin["member"]["id"],))
        status, out = self.call("POST", "/api/v1/join", {"invite": self.code(a)})
        self.assertEqual(status, 403, out)
        with self.srv.store.lock:
            self.srv.store.db.execute("UPDATE members SET role='admin' WHERE id=?", (admin["member"]["id"],))
        # demotion through the API voids pending invites
        self.ok("PATCH", "/api/v1/members/%d" % admin["member"]["id"], {"role": "editor"}, self.owner["token"])
        status, out = self.call("POST", "/api/v1/join", {"invite": self.code(b)})
        self.assertEqual(status, 401, out)
        # deactivation too
        admin2 = self.join_new("admin2@example.com", "admin", "Ad2")
        _, c = self.api_invite(admin2, "c@example.com")
        self.ok("PATCH", "/api/v1/members/%d" % admin2["member"]["id"], {"active": False}, self.owner["token"])
        status, _ = self.call("POST", "/api/v1/join", {"invite": self.code(c)})
        self.assertEqual(status, 401)

    def test_owner_invite_dies_when_owner_demoted(self):
        co = self.join_new("co@example.com", "owner", "Co")
        _, o = self.api_invite(co, "next@example.com", "owner")
        _, e = self.api_invite(co, "ed@example.com", "editor")
        self.ok("PATCH", "/api/v1/members/%d" % co["member"]["id"], {"role": "admin"}, self.owner["token"])
        status, _ = self.call("POST", "/api/v1/join", {"invite": self.code(o)})
        self.assertEqual(status, 401)
        self.assertEqual(self.call("POST", "/api/v1/join", {"invite": self.code(e)})[0], 200)  # admin may grant

    def test_delete_invite(self):
        admin = self.join_new("admin@example.com", "admin", "Ada")
        ed = self.join_new("ed@example.com", "editor", "Ed")
        _, mine = self.api_invite(admin, "x@example.com")
        _, owners = self.api_invite(self.owner, "boss@example.com", "owner")
        _, other = self.api_invite(self.owner, "y@example.com")
        listed = {i["id"] for i in self.ok("GET", "/api/v1/members", token=admin["token"])["invites"]}
        self.assertTrue({mine["id"], owners["id"], other["id"]} <= listed)
        self.assertEqual(self.call("DELETE", "/api/v1/invites/%d" % mine["id"], token=ed["token"])[0], 403)
        self.assertEqual(self.call("DELETE", "/api/v1/invites/%d" % owners["id"], token=admin["token"])[0], 403)
        self.ok("DELETE", "/api/v1/invites/%d" % mine["id"], token=admin["token"])
        self.ok("DELETE", "/api/v1/invites/%d" % other["id"], token=admin["token"])
        self.assertEqual(self.call("POST", "/api/v1/join", {"invite": self.code(mine)})[0], 401)
        self.assertEqual(self.call("DELETE", "/api/v1/invites/%d" % mine["id"], token=admin["token"])[0], 404)
        _, own_dev = self.api_invite(ed, "ed@example.com")  # an editor withdraws their own device invite
        self.ok("DELETE", "/api/v1/invites/%d" % own_dev["id"], token=ed["token"])
        _, used = self.api_invite(self.owner, "z@example.com")
        self.call("POST", "/api/v1/join", {"invite": self.code(used)})
        self.assertEqual(self.call("DELETE", "/api/v1/invites/%d" % used["id"], token=self.owner["token"])[0], 409)
        self.ok("DELETE", "/api/v1/invites/%d" % owners["id"], token=self.owner["token"])

    def test_update_member_uses_current_role(self):
        ed = self.join_new("ed@example.com", "editor", "Ed")
        v = self.join_new("v@example.com", "viewer", "Vee")
        stale = dict(ed["member"], role="admin")  # what a request might have seen a moment earlier
        with self.assertRaises(store_mod.ApiError) as cm:
            self.srv.store.update_member(stale, v["member"]["id"], {"role": "editor"})
        self.assertEqual(cm.exception.status, 403)

    # M3
    def test_total_request_deadline(self):
        srv = self.extra_server(request_deadline=1.0, request_timeout=5.0)
        sk = socket.create_connection(("127.0.0.1", srv.port))
        self.addCleanup(sk.close)
        sk.sendall(b"GET /api/v1/health HTTP/1.1\r\n")
        start = time.time()
        closed = False
        while time.time() - start < 4:
            try:
                sk.sendall(b"X-Slow: 1\r\n")  # one header every 0.2 s: never idle long enough for the timeout
            except OSError:
                closed = True
                break
            time.sleep(0.2)
            sk.settimeout(0.01)
            try:
                if sk.recv(1) == b"":
                    closed = True
                    break
            except socket.timeout:
                pass
            except OSError:
                closed = True
                break
        self.assertTrue(closed)
        self.assertLess(time.time() - start, 3)

    def test_per_ip_connection_cap(self):
        srv = self.extra_server(max_connections_per_ip=2)
        idle = [socket.create_connection(("127.0.0.1", srv.port)) for _ in range(2)]
        for sk in idle:
            self.addCleanup(sk.close)
        time.sleep(0.2)
        self.assertEqual(self.call_on(srv, "GET", "/api/v1/health")[0], 429)
        for sk in idle:
            sk.close()
        time.sleep(0.3)
        self.assertEqual(self.call_on(srv, "GET", "/api/v1/health")[0], 200)

    def test_per_forwarded_ip_cap_behind_proxy(self):
        srv = self.extra_server(trust_proxy=True, max_connections_per_ip=1, ping_interval=0.3)
        tok = srv.store.create_invite("a@example.com", "owner")["token"]
        a = self.call_on(srv, "POST", "/api/v1/join", {"invite": tok})[1]
        sk = socket.create_connection(("127.0.0.1", srv.port))
        self.addCleanup(sk.close)
        sk.sendall(("GET /api/v1/stream?token=%s HTTP/1.1\r\nHost: x\r\nX-Forwarded-For: 9.9.9.9\r\n\r\n"
                    % a["device_token"]).encode())
        sk.settimeout(3)
        self.assertIn(b"200", sk.recv(100))
        status, _ = self.call_on(srv, "GET", "/api/v1/health", headers={"X-Forwarded-For": "9.9.9.9"})
        self.assertEqual(status, 429)
        status, _ = self.call_on(srv, "GET", "/api/v1/health", headers={"X-Forwarded-For": "8.8.8.8"})
        self.assertEqual(status, 200)

    # LOWs
    def test_lone_surrogates_are_400(self):
        t = self.owner["token"]
        for raw in (b'{"ops":[{"op":"put","coll":"\\ud800","id":"a","base_version":0,"data":{}}]}',
                    b'{"ops":[{"op":"put","coll":"e","id":"a","base_version":0,"data":{"k":"\\udfff"}}]}'):
            status, out = self.call("POST", "/api/v1/items", raw=raw, token=t)
            self.assertEqual(status, 400, (raw, out))
        status, out = self.call("POST", "/api/v1/comments", raw=b'{"coll":"e","id":"\\ud83d","body":"x"}', token=t)
        self.assertEqual(status, 400, out)

    def test_short_body_is_408(self):
        req = ("POST /api/v1/items HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer %s\r\n"
               "Content-Type: application/json\r\nContent-Length: 100\r\n\r\n{\"ops\":" % self.owner["token"])
        out = self.raw(self.port, req.encode(), shut_write=True)
        self.assertTrue(out.startswith(b"HTTP/1.1 408"), out[:80])

    def test_get_with_body_closes_connection(self):
        req = (b"GET /api/v1/health HTTP/1.1\r\nHost: x\r\nContent-Length: 40\r\n\r\n"
               b"GET /api/v1/health HTTP/1.1\r\nHost: x\r\n\r\n")  # a smuggled second request
        out = self.raw(self.port, req)
        self.assertEqual(out.count(b"HTTP/1.1 200"), 1, out)


class TestReview3(Base):
    """Regression tests for the third review."""

    def test_huge_integer_is_rejected_fast(self):
        big = b"9" * 100000
        results = {}

        def health_loop():
            worst = 0.0
            for _ in range(10):
                t0 = time.time()
                self.call("GET", "/api/v1/health")
                worst = max(worst, time.time() - t0)
                time.sleep(0.05)
            results["health"] = worst

        th = threading.Thread(target=health_loop)
        th.start()
        t0 = time.time()
        status, out = self.call("POST", "/api/v1/join", raw=b'{"invite":' + big + b'}')
        took = time.time() - t0
        th.join()
        self.assertEqual(status, 400, out)
        self.assertIn("digits", out["error"])
        self.assertLess(took, 1.0)
        self.assertLess(results["health"], 1.0)
        status, out = self.call("POST", "/api/v1/items", raw=b'{"ops":[{"op":"put","coll":"e","id":"a",'
                                b'"base_version":0,"data":{"n":-' + big + b'}}]}', token=self.owner["token"])
        self.assertEqual(status, 400, out)
        # 20 digits still fine
        self.ok("POST", "/api/v1/items", {"ops": [{"op": "put", "coll": "e", "id": "a", "base_version": 0,
                                                   "data": {"n": 10 ** 19}}]}, self.owner["token"])

    def test_cursor_ahead_of_server_resets(self):
        t = self.owner["token"]
        self.put(self.owner, "email", "a", 0, {})
        out = self.ok("GET", "/api/v1/items?since=500", token=t)
        self.assertEqual((out["cursor"], out["reset"], out["items"]), (1, True, []))
        self.assertFalse(self.ok("GET", "/api/v1/items?since=1", token=t)["reset"])
        ev = self.ok("GET", "/api/v1/events?since=500", token=t)
        self.assertTrue(ev["reset"])
        self.assertLess(ev["seq"], 500)
        self.assertFalse(self.ok("GET", "/api/v1/events?since=0", token=t)["reset"])

    def test_verbose_log_redacts_token(self):
        srv = self.extra_server(verbose=True)
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            sk = socket.create_connection(("127.0.0.1", srv.port))
            sk.sendall(b"GET /api/v1/stream?token=SECRETTOKEN HTTP/1.1 extra\r\n\r\n")
            sk.settimeout(3)
            try:
                while sk.recv(4096):
                    pass
            except OSError:
                pass
            sk.close()
            self.call_on(srv, "GET", "/api/v1/stream?token=SECRETTOKEN2")
            time.sleep(0.2)
        log = buf.getvalue()
        self.assertIn("[redacted]", log)
        self.assertNotIn("SECRETTOKEN", log)

    def test_device_cap(self):
        st = self.srv.store
        spare = st.create_invite("owner@example.com", "owner")["token"]  # made while there was room
        for _ in range(9):  # owner already has one device
            st.join(st.create_invite("owner@example.com", "owner")["token"], "Mac")  # no HTTP: join rate limit
        with self.assertRaises(store_mod.ApiError) as cm:
            st.create_invite("owner@example.com", "owner")
        self.assertEqual(cm.exception.status, 409)
        status, out = self.call("POST", "/api/v1/join", {"invite": spare})
        self.assertEqual(status, 409, out)


class TestItemRules(Base):
    """Per-op errors and coll / id shapes on POST /items."""

    def test_bad_ops_fail_alone(self):
        t = self.owner["token"]
        big = {"blob": "x" * (600 * 1024)}
        ops = [
            {"op": "put", "coll": "email", "id": "good1", "base_version": 0, "data": {"a": 1}},
            {"op": "put", "coll": "email", "id": "big", "base_version": 0, "data": big},
            {"op": "put", "coll": "Email", "id": "x", "base_version": 0, "data": {}},
            {"op": "put", "coll": "email", "id": "has space", "base_version": 0, "data": {}},
            {"op": "put", "coll": "email", "id": "v", "base_version": "1", "data": {}},
            {"op": "put", "coll": "email", "id": "nodata", "base_version": 0},
            {"op": "rename", "coll": "email", "id": "r", "base_version": 0},
            "not an op",
            {"op": "put", "coll": "ads:meta_644981006414114", "id": "kw:1.a-b_c", "base_version": 0, "data": {}},
        ]
        out = self.ok("POST", "/api/v1/items", {"ops": ops}, t)
        r = out["results"]
        self.assertEqual(len(r), len(ops))
        self.assertEqual([x["ok"] for x in r], [True, False, False, False, False, False, False, False, True])
        for x in r[1:8]:
            self.assertIsInstance(x["error"], str)
        self.assertIn("too large", r[1]["error"])
        self.assertEqual((r[2]["coll"], r[2]["id"]), ("Email", "x"))
        self.assertNotIn("coll", r[7])
        ids = sorted(i["id"] for i in self.ok("GET", "/api/v1/items?since=0", token=t)["items"])
        self.assertEqual(ids, ["good1", "kw:1.a-b_c"])

    def test_whole_request_errors_stay(self):
        v = self.join_new("v@example.com", "viewer", "Vee")
        bad = {"ops": [{"op": "put", "coll": "BAD", "id": "a", "base_version": 0, "data": {}}]}
        self.assertEqual(self.call("POST", "/api/v1/items", bad, v["token"])[0], 403)
        status, _ = self.call("POST", "/api/v1/items", raw=b"x", token=self.owner["token"],
                              headers={"Content-Length": str(50 * 1024 * 1024)})
        self.assertEqual(status, 413)
        self.assertEqual(self.call("POST", "/api/v1/items", {"ops": []}, self.owner["token"])[0], 400)

    def test_shapes(self):
        st = store_mod
        for good in ("email", "seo:map_site", "ads:4387091070", "ads:meta_644981006414114", "social", "a_b:A.b-9"):
            self.assertEqual(st.need_coll({"coll": good}), good)
        for bad in ("Email", "ads:", ":x", "ads:x:y", "ads 1", "ads:x/y", "1ads", "ads:" + "9" * 200):
            with self.assertRaises(st.ApiError, msg=bad):
                st.need_coll({"coll": bad})
        for good in ("a", "kw:1", "A.b-c_d", "9" * 200, 42):
            self.assertEqual(st.need_item_id({"id": good}), str(good))
        for bad in ("", "a b", "a/b", "é", "9" * 201, True, 1.5):
            with self.assertRaises(st.ApiError, msg=repr(bad)):
                st.need_item_id({"id": bad})
        # the same shapes apply to comments and claims
        t = self.owner["token"]
        self.assertEqual(self.call("POST", "/api/v1/comments", {"coll": "email", "id": "a b", "body": "x"}, t)[0], 400)
        self.assertEqual(self.call("POST", "/api/v1/claims", {"coll": "Email", "id": "a"}, t)[0], 400)


if __name__ == "__main__":
    unittest.main()
