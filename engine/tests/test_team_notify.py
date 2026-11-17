"""Team notifications on the Studio side (engine/team_notify.py) against the real team server: the bell list with
who and labels, unread counts in the top bar, read marks kept in step with mentions, settings round trip (Focus, Away,
backups), pending items changed by a teammate through a real sync, webhook URL checks and the MCP tool.

Run from the engine folder:  python3 -m unittest tests/test_team_notify.py
Uses the harness of tests/test_team.py: the real `teamserver serve` on a free port in a temp folder (no SMTP settings,
private webhooks refused), each Studio in its own subprocess with its own MSTUDIO_HOME. Never touches the real data
folder or the Keychain, and sends no email or webhook.
"""
import os
import sys
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_team as tt  # noqa: E402

tt.TeamSync.n = getattr(tt.TeamSync, "n", 0)

NOTIFY = "import team_live, team_notify\n"


class TeamNotify(unittest.TestCase):
    setUpClass = classmethod(tt.TeamSync.setUpClass.__func__)
    tearDownClass = classmethod(tt.TeamSync.tearDownClass.__func__)
    cli = classmethod(tt.TeamSync.cli.__func__)
    home = tt.TeamSync.home
    run_in = tt.TeamSync.run_in
    member = tt.TeamSync.member
    sync = tt.TeamSync.sync

    def nt(self, h, code):
        return self.run_in(h, NOTIFY + code)

    def test_solo_user_gets_nothing(self):
        h = self.home()
        r = self.nt(h, """
out = {}
for name, fn in (("list", team_notify.notifications), ("prefs", team_notify.prefs)):
    try:
        fn(); out[name] = "called"
    except ValueError as e:
        out[name] = str(e)
out["state"] = team_live.state()""")
        self.assertIn("Join a team first", r["list"])
        self.assertIn("Join a team first", r["prefs"])
        self.assertEqual(r["state"], {"joined": False, "removed": False})

    def test_own_alert_lands_on_my_bell_only(self):
        a, sa = self.member("Ada")
        b, _ = self.member("Bea")
        r = self.nt(a, """
out = {"sent": team_notify.send_alert("ML (Google Ads): Spend today is 132%", {"rule": "r1"}),
       "list": team_notify.notifications()}""")
        self.assertEqual(r["sent"], "sent")
        n = r["list"]["notifications"][0]
        self.assertEqual((n["kind"], n["area"], n["data"]["rule"]), ("alert", "ads", "r1"))
        self.assertIn("Spend today is 132%", n["text"])
        r = self.nt(b, "out = team_notify.notifications()")
        self.assertEqual([x["kind"] for x in r["notifications"]], [])
        r = self.nt(a, """
team_notify.save_prefs({"rules": {"alert": False}})
out = team_notify.send_alert("again")""")
        self.assertEqual(r, "off")
        r = self.nt(self.home(), "out = team_notify.send_alert('solo')")
        self.assertEqual(r, "solo")

    def test_mention_on_the_bell_and_read_in_step(self):
        a, sa = self.member("Nia")
        b, sb = self.member("Ned")
        cid = "c" + uuid.uuid4().hex[:6]
        bname = sb["me"]["name"]
        c = self.nt(a, f"out = team_live.comment_add('email', {cid!r}, 'Subject ok, @{bname}?')")["comment"]
        r = self.nt(b, """
lst = team_notify.notifications()
out = {"list": lst, "state": team_live.state()["unread"], "bell": team_notify.unread(),
       "route": team_notify.get("/api/team/notify/unread", {})}""")
        n = r["list"]["notifications"][0]
        self.assertEqual(n["kind"], "mention")
        self.assertEqual(n["who"]["name"], sa["me"]["name"])
        self.assertEqual((n["coll"], n["item_id"]), ("email", cid))
        self.assertFalse(n["read"])
        self.assertIn("mentioned you", n["text"])
        self.assertEqual(r["list"]["unread"], 1)
        self.assertEqual((r["state"], r["bell"], r["route"]["unread"]), (1, 1, 1))
        r = self.nt(b, f"""
team_notify.post("/api/team/notify/read", {{"ids": [{n['id']}]}})
out = {{"bell": team_notify.unread(), "mentions": team_live.mentions(True)["unread"]}}""")
        self.assertEqual(r, {"bell": 0, "mentions": 0})
        # the other way round: a mention marked read in the Team tab marks the notification read
        c2 = self.nt(a, f"out = team_live.comment_add('email', {cid!r}, 'One more, @{bname}')")["comment"]
        r = self.nt(b, f"""
before = team_notify.unread()
team_live.mentions_read([{c2['id']}])
out = {{"before": before, "after": team_notify.unread(), "all": team_notify.notifications()["notifications"]}}""")
        self.assertEqual((r["before"], r["after"]), (1, 0))
        self.assertTrue(all(x["read"] for x in r["all"]))
        self.assertNotEqual(c["id"], c2["id"])

    def test_prefs_round_trip_focus_away_and_backups(self):
        a, sa = self.member("Ola")
        b, sb = self.member("Oli")
        bid = sb["me"]["id"]
        r = self.nt(a, f"""
first = team_notify.prefs()
saved = team_notify.save_prefs({{"follow": {{"ads": True}}, "email": {{"on": False, "timing": "daily"}},
                                 "focus": {{"on": True, "until": None}}, "away": {{"on": True, "backup": {bid}}},
                                 "daily_at": "08:30", "tz_offset": 60, "nonsense": 1}})
bad = {{}}
for body in ({{"webhook": {{"url": "https://127.0.0.1/hook"}}}}, {{"webhook": {{"url": "http://hooks.example.com/x"}}}},
             {{"bell": {{"timing": "weekly"}}}}):
    try:
        team_notify.save_prefs(body); bad[str(body)] = "saved"
    except team.TeamError as e:
        bad[str(body)] = e.status
out = {{"first": first, "saved": saved, "bad": bad, "again": team_notify.get("/api/team/notify/prefs", {{}})}}""")
        p = r["first"]["prefs"]
        self.assertTrue(all(p["rules"].values()))
        self.assertFalse(any(p["follow"].values()))
        self.assertFalse(r["first"]["server"]["email"])
        names = {x["id"]: x for x in r["first"]["backups"]}
        self.assertIn(bid, names)
        self.assertNotIn(sa["me"]["id"], names)
        self.assertFalse(names[bid]["may_approve"])
        s = r["saved"]["prefs"]
        self.assertTrue(s["follow"]["ads"])
        self.assertEqual((s["email"]["timing"], s["daily_at"], s["tz_offset"]), ("daily", "08:30", 60))
        self.assertTrue(s["focus"]["active"])
        self.assertIsNone(s["focus"]["until"])
        self.assertTrue(s["away"]["active"])
        self.assertEqual(s["away"]["backup"], bid)
        self.assertFalse(s["away"]["backup_ok"])  # not an approver: reviews are not passed on
        self.assertEqual(set(r["bad"].values()), {400})
        self.assertTrue(r["again"]["prefs"]["focus"]["active"])
        r = self.nt(a, "out = team_notify.post('/api/team/notify/prefs', {'focus': {'on': False}, 'away': {'on': False}})")
        self.assertFalse(r["prefs"]["focus"]["active"])
        self.assertFalse(r["prefs"]["away"]["active"])

    def test_pending_item_changed_by_a_teammate_after_sync(self):
        a, sa = self.member("Pam")
        b, sb = self.member("Pat")
        iid = "p" + uuid.uuid4().hex[:8]
        self.run_in(a, f"seo_put({iid!r}, {{'new': 'Mine'}})\nout = team.sync()")
        self.sync(b)
        self.run_in(b, f"seo_put({iid!r}, {{'new': 'Pat changed it'}})\nout = team.sync()")
        r = self.nt(a, "out = team_notify.notifications()")
        hit = [x for x in r["notifications"] if x["kind"] == "pending_changed"]
        self.assertEqual(len(hit), 1, r)
        self.assertEqual((hit[0]["coll"], hit[0]["item_id"]), ("seo:changes", iid))
        self.assertEqual(hit[0]["who"]["name"], sb["me"]["name"])
        self.assertTrue(hit[0]["label"])  # a readable name from the local draft (here the item id)
        # Pat's own edit of an item nobody has pending: nothing for anyone
        r = self.nt(b, "out = team_notify.notifications()")
        self.assertEqual([x for x in r["notifications"] if x["kind"] == "pending_changed"], [])

    def test_ping_bumps_on_the_notify_event(self):
        r = self.nt(self.home(), """
before = team_live.ping()["kinds"].get("notify")
team_notify.on_event("notify", '{"unread": 1}')
team_notify.on_event("presence", '{}')
out = {"before": before, "after": team_live.ping()["kinds"].get("notify")}""")
        self.assertIsNone(r["before"])
        self.assertIsNotNone(r["after"])

    def test_mcp_tool_lists_read_only(self):
        a, sa = self.member("Quin")
        b, sb = self.member("Quil")
        self.nt(a, f"out = team_live.comment_add('social', 'q1', 'Look, @{sb['me']['name']}')")
        r = self.run_in(b, """
import mcp_server
names = [t["name"] for t in mcp_server.TOOLS]
out = {"listed": "notifications_list" in names, "res": mcp_server.call("notifications_list", {"unread_only": True})}""")
        self.assertTrue(r["listed"])
        self.assertEqual(r["res"]["notifications"][0]["kind"], "mention")
        self.assertEqual(r["res"]["unread"], 1)


if __name__ == "__main__":
    unittest.main()
