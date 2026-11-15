"""End-to-end tests: a real server on a free port with a temp data dir and an injected clock."""
import http.client
import json
import os
import queue
import shutil
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
        token, _ = self.srv.store.create_invite(email, role, name)
        return token

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
        self.assertEqual(status, 400)
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


if __name__ == "__main__":
    unittest.main()
