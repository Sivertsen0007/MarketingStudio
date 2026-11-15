"""Team live tests (engine/team_live.py) against the real team server: presence, comments and mentions, comment
counts, claims, the activity feed and the tints (marks).

Run from the engine folder:  python3 -m unittest tests/test_team_live.py
Uses the harness of tests/test_team.py: the real `teamserver serve` on a free port in a temp folder, and each Studio
in its own subprocess with its own MSTUDIO_HOME. Never touches the real data folder or the Keychain.
"""
import os
import sys
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_team as tt  # noqa: E402

tt.TeamSync.n = getattr(tt.TeamSync, "n", 0)

LIVE = "import team_live\n"


class TeamLive(unittest.TestCase):
    setUpClass = classmethod(tt.TeamSync.setUpClass.__func__)
    tearDownClass = classmethod(tt.TeamSync.tearDownClass.__func__)
    cli = classmethod(tt.TeamSync.cli.__func__)
    home = tt.TeamSync.home
    run_in = tt.TeamSync.run_in
    member = tt.TeamSync.member
    sync = tt.TeamSync.sync

    def live(self, h, code):
        return self.run_in(h, LIVE + code)

    def test_solo_user_gets_nothing(self):
        h = self.home()
        r = self.live(h, "out = {'state': team_live.state(), 'marks': team_live.marks(), 'ping': team_live.ping()}")
        self.assertEqual(r["state"], {"joined": False, "removed": False})
        self.assertEqual(r["marks"], {"joined": False, "marks": {}})
        self.assertFalse(r["ping"]["joined"])

    def test_presence_round_trip(self):
        a, sa = self.member("Pia")
        b, sb = self.member("Pete")
        cid = "c" + uuid.uuid4().hex[:6]
        r = self.live(a, f"out = team_live.presence_beat('email', 'email', {cid!r}, True)")
        mine = [p for p in r["presence"] if p["me"]]
        self.assertEqual(mine[0]["coll"], "email")
        got = self.live(b, "out = team_live.presence()")
        p = next(x for x in got["presence"] if x["id"] == cid)
        self.assertTrue(p["editing"])
        self.assertEqual(p["area"], "email")
        self.assertEqual(p["member"]["name"], sa["me"]["name"])
        self.assertTrue(p["member"]["colour"].startswith("#"))
        self.assertFalse(p["me"])
        # leaving the editor: editing false
        self.live(a, f"out = team_live.presence_beat('email', 'email', {cid!r}, False)")
        got = self.live(b, "out = team_live.presence()")
        self.assertFalse(next(x for x in got["presence"] if x["id"] == cid)["editing"])

    def test_comment_mention_reaches_the_other_member_and_counts(self):
        a, sa = self.member("Cleo")
        b, sb = self.member("Carl")
        cid = "c" + uuid.uuid4().hex[:6]
        bname = sb["me"]["name"]
        r = self.live(a, f"out = team_live.comment_add('email', {cid!r}, 'Can you check the subject, @{bname}?')")
        c = r["comment"]
        self.assertEqual([m["name"] for m in c["mentions"]], [bname])
        self.assertTrue(c["mine"])
        got = self.live(b, f"""
m = team_live.mentions(True)
thread = team_live.comments('email', {cid!r})
reply = team_live.comment_add('email', {cid!r}, 'Done', parent={c['id']})
counts = team_live.counts(['email'])['counts']['email'][{cid!r}]
read = team_live.mentions_read([{c['id']}])
out = {{"m": m, "thread": thread, "counts": counts, "after": team_live.mentions(True)}}""")
        self.assertGreaterEqual(got["m"]["unread"], 1)
        hit = next(x for x in got["m"]["mentions"] if x["comment"]["id"] == c["id"])
        self.assertFalse(hit["read"])
        self.assertEqual(hit["comment"]["author"]["name"], sa["me"]["name"])
        self.assertEqual(got["thread"]["comments"][0]["body"], c["body"])
        self.assertEqual(got["counts"]["open"], 1)
        self.assertEqual(got["counts"]["total"], 2)
        self.assertNotIn(c["id"], [x["comment"]["id"] for x in got["after"]["mentions"]])
        # resolving the thread: no open threads left; only the author edits the body
        r = self.live(a, f"""
team_live.comment_edit({c['id']}, resolved=True)
edited = team_live.comment_edit({c['id']}, body='Can you check the subject?')
out = {{"counts": team_live.counts('email')['counts']['email'][{cid!r}], "edited": edited}}""")
        self.assertEqual(r["counts"]["open"], 0)
        self.assertTrue(r["edited"]["comment"]["edited_at"])
        r = self.live(b, f"""
try:
    team_live.comment_edit({c['id']}, body='mine now')
    out = 'edited'
except team.TeamError as e:
    out = e.status""")
        self.assertEqual(r, 403)

    def test_claim_conflict(self):
        a, sa = self.member("Dora")
        b, _ = self.member("Dan")
        nid = "n_" + uuid.uuid4().hex[:8]
        r = self.live(a, f"out = team_live.claim('seo:map_nodes', {nid!r}, 'writing the draft')")
        self.assertTrue(r["ok"])
        self.assertTrue(r["claim"]["mine"])
        r = self.live(b, f"out = {{'try': team_live.claim('seo:map_nodes', {nid!r}), 'all': team_live.claims()}}")
        self.assertFalse(r["try"]["ok"])
        self.assertEqual(r["try"]["held_by"]["member"]["name"], sa["me"]["name"])
        self.assertIn(sa["me"]["name"], r["try"]["message"])
        held = next(c for c in r["all"]["claims"] if c["id"] == nid)
        self.assertFalse(held["mine"])
        self.assertEqual(held["note"], "writing the draft")
        self.live(a, f"out = team_live.release('seo:map_nodes', {nid!r})")
        r = self.live(b, f"out = team_live.claim('seo:map_nodes', {nid!r})")
        self.assertTrue(r["ok"])

    def test_feed_shows_edit_session_after_sync(self):
        a, sa = self.member("Eve")
        b, _ = self.member("Ed")
        x1, x2 = "e" + uuid.uuid4().hex[:9], "e" + uuid.uuid4().hex[:9]
        self.live(a, f"""
seo_put({x1!r}, {{"post_id": 7, "field": "title", "old": "Old", "new": "New one"}})
out = team.sync()""")
        f1 = self.live(b, f"out = team_live.feed(person={sa['me']['id']!r})")
        sess = [e for e in f1["events"] if e["kind"] == "edit_session"]
        self.assertEqual(len(sess), 1)
        self.assertEqual(sess[0]["who"]["name"], sa["me"]["name"])
        self.assertIn("seo", f1["areas"])
        sid = sess[0]["data"]["session"]
        self.live(a, f"""
seo_put({x2!r}, {{"post_id": 7, "field": "seo_title", "old": "", "new": "Better"}})
out = team.sync()""")
        f2 = self.live(b, f"out = team_live.feed(person={sa['me']['id']!r})")
        sess = [e for e in f2["events"] if e["kind"] == "edit_session"]
        self.assertEqual(len(sess), 1)  # the grown session replaced its earlier copy
        self.assertEqual(sess[0]["data"]["session"], sid)
        self.assertEqual(sess[0]["data"]["count"], 2)
        self.assertEqual(self.live(b, "out = team_live.feed(area='email', person=" + repr(sa["me"]["id"]) + ")")["events"], [])

    def test_marks_tint_another_members_changes(self):
        a, sa = self.member("Fay")
        b, _ = self.member("Finn")
        x = "m" + uuid.uuid4().hex[:9]
        self.live(a, f"""
seo_put({x!r}, {{"post_id": 9, "field": "title", "old": "Old title", "new": "First try", "by": "you"}})
out = team.sync()""")
        r = self.live(b, f"""
team.sync()
out = team_live.marks('seo:changes')""")
        m = r["marks"]["seo:changes"][x]
        self.assertEqual(m["who"]["name"], sa["me"]["name"])
        self.assertEqual(m["parents"], {"post_id": "9"})
        self.assertTrue(any(f["field"] == "New" or f["key"] == "title" for f in m["fields"]), m["fields"])
        # a second edit by Fay: the hover says old -> new from what Finn had before
        self.live(a, f"""
seo_put({x!r}, {{"post_id": 9, "field": "title", "old": "Old title", "new": "Second try", "by": "you"}})
out = team.sync()""")
        r = self.live(b, f"""
team.sync()
out = team_live.marks('seo:changes')""")
        f = next(f for f in r["marks"]["seo:changes"][x]["fields"] if f["key"] == "new")
        self.assertEqual((f["old"], f["new"]), ("First try", "Second try"))
        # Fay's own copy has no tint; once Finn edits it, his copy has none either
        self.assertNotIn(x, self.live(a, "out = team_live.marks('seo:changes')")["marks"].get("seo:changes", {}))
        r = self.live(b, f"""
seo_put({x!r}, {{"post_id": 9, "field": "title", "old": "Old title", "new": "Mine", "by": "you"}})
out = team_live.marks('seo:changes')""")
        self.assertNotIn(x, r["marks"].get("seo:changes", {}))

    def test_live_actions_reach_the_feed(self):
        a, sa = self.member("Hal")
        r = self.live(a, """
import threading
def done():
    for t in threading.enumerate():
        if t is not threading.current_thread() and t.daemon:
            t.join(5)
team_live.after_post("/api/area/seo/publish", {}, {"ok": True, "live": False, "message": "dry run"})
team_live.after_post("/api/area/email/send_now", {"id": "c9"}, {"ok": False, "error": "checks failed"})
team_live.after_post("/api/area/email/send_now", {"id": "c9"}, {"ok": True})
team_live.after_post("/api/draft/add", {}, {"ok": True})
done()
out = [e for e in team_live.feed(person=%r)["events"] if e["kind"] != "join"]""" % sa["me"]["id"])
        self.assertEqual([(e["kind"], e["area"], e["coll"], e["id"]) for e in r], [("publish", "email", "email", "c9")])
        self.assertEqual(r[0]["summary"], "sent an email campaign")

    def test_mcp_tools(self):
        a, _ = self.member("Gil")
        cid = "c" + uuid.uuid4().hex[:6]
        r = self.run_in(a, f"""
import mcp_server
names = {{t["name"] for t in mcp_server.TOOLS}}
c = mcp_server.call("comment_add", {{"coll": "social", "id": {cid!r}, "body": "Shorter caption?"}})
out = {{"names": sorted(names & {{"team_activity", "comments_list", "comment_add", "mentions_list"}}), "c": c,
       "list": mcp_server.call("comments_list", {{"coll": "social", "id": {cid!r}}}),
       "feed": mcp_server.call("team_activity", {{"limit": 5}}), "mentions": mcp_server.call("mentions_list", {{}})}}""")
        self.assertEqual(r["names"], ["comment_add", "comments_list", "mentions_list", "team_activity"])
        self.assertEqual(r["c"]["comment"]["body"], "Claude: Shorter caption?")
        self.assertEqual(r["list"]["comments"][0]["id"], r["c"]["comment"]["id"])
        self.assertTrue(any(e["kind"] == "comment" for e in r["feed"]["events"]))
        self.assertIn("unread", r["mentions"])


if __name__ == "__main__":
    unittest.main()
