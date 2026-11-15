"""Reviews (approvals), approval policy and owners: a real server on a free port (see test_server.Base)."""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from test_server import Base  # noqa: E402
from teamserver.reviews import content_hash  # noqa: E402


class ReviewBase(Base):
    def setUp(self):
        super().setUp()
        self.alex = self.join_new("alex@example.com", "editor", "Alex")
        self.bo = self.join_new("bo@example.com", "editor", "Bo")
        self.vic = self.join_new("vic@example.com", "viewer", "Vic")
        # Bo may approve
        self.ok("PATCH", "/api/v1/members/%d" % self.bo["member"]["id"], {"approver": True}, self.owner["token"])

    def ask(self, who, items, reviewers, **kw):
        body = {"items": items, "reviewers": [r["member"]["id"] for r in reviewers], **kw}
        return self.call("POST", "/api/v1/reviews", body, who["token"])


class TestReviews(ReviewBase):
    def test_request_approve_and_stale(self):
        self.put(self.alex, "seo:changes", "c1", 0, {"id": "c1", "new": "Hello"})
        self.put(self.alex, "seo:changes", "c2", 0, {"id": "c2", "new": "World"})
        s, rv = self.ask(self.alex, [{"coll": "seo:changes", "id": "c1", "version": 1},
                                     {"coll": "seo:changes", "id": "c2", "version": 1}], [self.bo], note="Please look")
        self.assertEqual(s, 200, rv)
        self.assertEqual(rv["status"], "open")
        self.assertEqual(rv["area"], "seo")
        self.assertEqual(rv["items"][0]["hash"], content_hash({"id": "c1", "new": "Hello"}))
        self.assertFalse(rv["stale"])
        # requester cannot decide their own review, nor can someone not asked, nor a non-approver
        s, out = self.call("POST", "/api/v1/reviews/%d/decide" % rv["id"], {"decision": "approve"}, self.alex["token"])
        self.assertEqual(s, 403, out)
        s, out = self.call("POST", "/api/v1/reviews/%d/decide" % rv["id"], {"decision": "approve"}, self.owner["token"])
        self.assertEqual(s, 403, out)
        out = self.ok("POST", "/api/v1/reviews/%d/decide" % rv["id"], {"decision": "approve", "note": "Fine"},
                      self.bo["token"])
        self.assertEqual(out["status"], "approved")
        self.assertEqual(out["decided_by"], self.bo["member"]["id"])
        s, out = self.call("POST", "/api/v1/reviews/%d/decide" % rv["id"], {"decision": "approve"}, self.bo["token"])
        self.assertEqual(s, 409)
        # bookkeeping-only change: still covered; content change: stale
        self.put(self.alex, "seo:changes", "c1", 1, {"id": "c1", "new": "Hello", "at": "later", "problems": ["x"]})
        out = self.ok("GET", "/api/v1/reviews/%d" % rv["id"], token=self.alex["token"])
        self.assertFalse(out["items"][0]["stale"])
        self.put(self.alex, "seo:changes", "c1", 2, {"id": "c1", "new": "Hello there"})
        out = self.ok("GET", "/api/v1/reviews/%d" % rv["id"], token=self.alex["token"])
        self.assertTrue(out["items"][0]["stale"])
        self.assertFalse(out["items"][1]["stale"])
        self.assertEqual(out["items"][0]["current_version"], 3)
        self.assertTrue(out["stale"])
        # deleting the other item makes it stale too; current=1 then hides the review
        self.ok("POST", "/api/v1/items", {"ops": [{"op": "delete", "coll": "seo:changes", "id": "c2", "base_version": 1}]},
                self.alex["token"])
        cur = self.ok("GET", "/api/v1/reviews?status=approved&current=1", token=self.bo["token"])["reviews"]
        self.assertEqual(cur, [])
        allr = self.ok("GET", "/api/v1/reviews?status=approved", token=self.bo["token"])["reviews"]
        self.assertEqual([r["id"] for r in allr], [rv["id"]])

    def test_filters_events_and_stream(self):
        stream_bo = self.open_stream(self.bo["token"])
        stream_vic = self.open_stream(self.vic["token"])
        time.sleep(0.1)
        self.put(self.alex, "email", "55", 0, {"fields": {"subject": "Hi"}})
        s, rv = self.ask(self.alex, [{"coll": "email", "id": "55", "version": 1}], [self.bo])
        self.assertEqual(s, 200, rv)
        self.assertEqual(self.ok("GET", "/api/v1/reviews?to_me=1", token=self.bo["token"])["reviews"][0]["id"], rv["id"])
        self.assertEqual(self.ok("GET", "/api/v1/reviews?to_me=1", token=self.alex["token"])["reviews"], [])
        self.assertEqual(len(self.ok("GET", "/api/v1/reviews?mine=1", token=self.alex["token"])["reviews"]), 1)
        self.assertEqual(self.ok("GET", "/api/v1/reviews?mine=1", token=self.bo["token"])["reviews"], [])
        self.assertEqual(len(self.ok("GET", "/api/v1/reviews?status=open", token=self.vic["token"])["reviews"]), 1)
        self.assertEqual(self.call("GET", "/api/v1/reviews?status=nope", token=self.vic["token"])[0], 400)
        ev = self.wait_for(stream_bo, "review")
        self.assertEqual(ev["id"], rv["id"])
        self.ok("POST", "/api/v1/reviews/%d/decide" % rv["id"], {"decision": "request_changes", "note": "Shorter"},
                self.bo["token"])
        evs = self.ok("GET", "/api/v1/events?since=0", token=self.alex["token"])["events"]
        kinds = [e["kind"] for e in evs]
        self.assertIn("review_requested", kinds)
        self.assertIn("review_decided", kinds)
        dec = [e for e in evs if e["kind"] == "review_decided"][0]
        self.assertEqual(dec["data"]["status"], "changes_requested")
        self.assertEqual(dec["coll"], "review")
        # Vic (not involved) never gets a review event
        self.assertFalse(self.saw(stream_vic, "review", timeout=1)[0])
        # comments on the review reuse the comments API with coll "review"
        self.ok("POST", "/api/v1/comments", {"coll": "review", "id": str(rv["id"]), "body": "Done, have another look"},
                self.alex["token"])
        self.assertEqual(self.ok("GET", "/api/v1/reviews/%d" % rv["id"], token=self.bo["token"])["comments"], 1)

    def test_bad_requests(self):
        self.put(self.alex, "social", "p1", 0, {"id": "p1", "text": "x"})
        item = [{"coll": "social", "id": "p1", "version": 1}]
        self.assertEqual(self.ask(self.vic, item, [self.bo])[0], 403)          # viewers can't ask
        self.assertEqual(self.ask(self.alex, item, [self.alex])[0], 400)       # not yourself
        self.assertEqual(self.ask(self.alex, item, [])[0], 400)                # someone
        s, out = self.ask(self.alex, item, [self.vic])                         # must be able to approve
        self.assertEqual(s, 400, out)
        self.assertIn("can't approve", out["error"])
        s, out = self.ask(self.alex, [{"coll": "social", "id": "p1", "version": 3}], [self.bo])
        self.assertEqual(s, 409, out)
        self.assertEqual(out["current_version"], 1)
        self.assertEqual(self.ask(self.alex, [{"coll": "BAD", "id": "p1", "version": 1}], [self.bo])[0], 400)
        self.assertEqual(self.ask(self.alex, [{"coll": "social", "id": "p1", "version": "1"}], [self.bo])[0], 400)
        # version 0 = no such item (e.g. an email campaign with no Studio draft)
        s, rv = self.ask(self.alex, [{"coll": "email", "id": "99", "version": 0}], [self.owner])
        self.assertEqual(s, 200, rv)
        self.assertEqual(rv["items"][0]["hash"], "")
        self.assertFalse(rv["items"][0]["stale"])
        # admins and owners may approve without the flag
        self.assertEqual(self.ok("POST", "/api/v1/reviews/%d/decide" % rv["id"], {"decision": "approve"},
                                 self.owner["token"])["status"], "approved")
        self.assertEqual(self.call("POST", "/api/v1/reviews/%d/decide" % rv["id"], {"decision": "maybe"},
                                   self.owner["token"])[0], 400)
        self.assertEqual(self.call("GET", "/api/v1/reviews/999", token=self.alex["token"])[0], 404)

    def test_withdraw(self):
        self.put(self.alex, "social", "p1", 0, {"id": "p1", "text": "x"})
        s, rv = self.ask(self.alex, [{"coll": "social", "id": "p1", "version": 1}], [self.bo])
        self.assertEqual(self.call("POST", "/api/v1/reviews/%d/withdraw" % rv["id"], None, self.bo["token"])[0], 403)
        out = self.ok("POST", "/api/v1/reviews/%d/withdraw" % rv["id"], None, self.alex["token"])
        self.assertEqual(out["status"], "withdrawn")
        self.assertEqual(self.call("POST", "/api/v1/reviews/%d/withdraw" % rv["id"], None, self.alex["token"])[0], 409)
        self.assertEqual(self.call("POST", "/api/v1/reviews/%d/decide" % rv["id"], {"decision": "approve"},
                                   self.bo["token"])[0], 409)

    def test_from_changeset(self):
        cs = {"id": "%d-abc" % self.alex["member"]["id"], "name": "Spring sale"}
        ops = [{"op": "put", "coll": "ads:123", "id": i, "base_version": 0, "data": {"id": i}, "changeset": cs}
               for i in ("a1", "a2")]
        ops.append({"op": "put", "coll": "ads:123", "id": "a3", "base_version": 0, "data": {"id": "a3"}})
        self.ok("POST", "/api/v1/items", {"ops": ops}, self.alex["token"])
        s, rv = self.call("POST", "/api/v1/reviews", {"changeset": cs, "from_changeset": True,
                                                      "reviewers": [self.bo["member"]["id"]]}, self.alex["token"])
        self.assertEqual(s, 200, rv)
        self.assertEqual(sorted(i["id"] for i in rv["items"]), ["a1", "a2"])
        self.assertEqual(rv["title"], "Spring sale")
        self.assertEqual(rv["changeset"], cs)
        # someone else's changeset id finds nothing of theirs
        s, out = self.call("POST", "/api/v1/reviews", {"changeset": cs, "from_changeset": True,
                                                       "reviewers": [self.bo["member"]["id"]]}, self.owner["token"])
        self.assertEqual(s, 400, out)


class TestPolicyOwners(ReviewBase):
    def test_policy(self):
        self.assertEqual(self.ok("GET", "/api/v1/policy", token=self.vic["token"]),
                         {"require_approval": {"ads": False, "email": False, "seo": False, "social": False}})
        self.assertEqual(self.call("PUT", "/api/v1/policy", {"require_approval": {"ads": True}}, self.alex["token"])[0], 403)
        out = self.ok("PUT", "/api/v1/policy", {"require_approval": {"ads": True, "seo": True}}, self.owner["token"])
        self.assertTrue(out["require_approval"]["ads"])
        out = self.ok("PUT", "/api/v1/policy", {"require_approval": {"seo": False}}, self.owner["token"])
        self.assertEqual(out["require_approval"], {"ads": True, "email": False, "seo": False, "social": False})
        self.assertEqual(self.call("PUT", "/api/v1/policy", {"require_approval": {"tv": True}}, self.owner["token"])[0], 400)
        self.assertEqual(self.call("PUT", "/api/v1/policy", {"require_approval": {"ads": 1}}, self.owner["token"])[0], 400)

    def test_owners(self):
        mid = self.alex["member"]["id"]
        self.assertEqual(self.call("PUT", "/api/v1/owners", {"owners": {"email/1": mid}}, self.alex["token"])[0], 403)
        out = self.ok("PUT", "/api/v1/owners", {"owners": {"ads:123/campaign/Brand": mid, "seo/post/42": mid}},
                      self.owner["token"])
        self.assertEqual(out["owners"]["seo/post/42"], mid)
        out = self.ok("PUT", "/api/v1/owners", {"owners": {"seo/post/42": None}}, self.owner["token"])
        self.assertEqual(out["owners"], {"ads:123/campaign/Brand": mid})
        self.assertEqual(self.ok("GET", "/api/v1/owners", token=self.vic["token"]), out)
        self.assertEqual(self.call("PUT", "/api/v1/owners", {"owners": {"Bad": mid}}, self.owner["token"])[0], 400)
        self.assertEqual(self.call("PUT", "/api/v1/owners", {"owners": {"x": 999}}, self.owner["token"])[0], 400)
        self.assertEqual(self.call("PUT", "/api/v1/owners", {"owners": {"x\n": mid}}, self.owner["token"])[0], 400)


if __name__ == "__main__":
    unittest.main()
