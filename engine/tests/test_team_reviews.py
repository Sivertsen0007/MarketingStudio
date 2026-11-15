"""Team reviews (engine/team_reviews.py) against the real team server: approvals, push gating, push by person, the why
note, owners.

Run from the engine folder:  python3 -m unittest tests/test_team_reviews.py
Same set-up as test_team.py: a real `python3 -m teamserver serve` on a free port in a temp folder, each Studio in its own
subprocess with its own MSTUDIO_HOME, MSTUDIO_KEYCHAIN=0 (never the real data folder or Keychain). The push functions
are stand-ins that record which draft items they were given: nothing reaches Google, Meta, an email platform or a
social network.
"""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_team import ENGINE, PRELUDE, REPO, free_port  # noqa: E402

REVIEW_PRELUDE = PRELUDE + """
import team_reviews as tr
em._fetch_status = lambda url, timeout=8: (200, "")  # link checks never leave the Mac in tests
def ads_put(iid, summary, campaign="Brand", extra=None):
    with draftstore.tx(COLL, ROOT, by="you") as t:
        t.put(iid, {"id": iid, "summary": summary, "by": "you", "note": "", "problems": [], "warnings": [],
                    "change": {"type": "campaign_budget", "campaign": campaign, "budget": summary}, **(extra or {})}, "you")
def fake_push(only):
    global pushed
    pushed = None if only is None else sorted(only)
    return {"ok": True, "applied": 1, "message": "pushed"}
pushed = "not called"
def gated(body):
    global pushed
    pushed = "not called"
    r = tr.gated_push("ads", COLL, body, fake_push, "Pushed live to Google Ads")
    return {"res": r, "pushed": pushed}
def err(fn, *a):
    try:
        fn(*a)
    except Exception as e:  # noqa: BLE001
        return type(e).__name__ + ": " + str(e)
    return None
"""


SP = """
from areas import social as so
from areas import _social_store as sst
import datetime as dt
"""


class Reviews(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="mstudio-reviews-")
        cls.data = os.path.join(cls.tmp.name, "server")
        cls.port = free_port()
        cls.url = f"http://127.0.0.1:{cls.port}"
        Reviews.n = 0  # one counter, also for subclasses (each member and collection gets a unique number)
        cls.owner_link = cls.cli("init", "--email", "owner@example.test", "--name", "Olive", "--team", "Test team",
                                 "--public-url", cls.url)
        boot = ("import sys, teamserver.app as a; a.JOIN_LIMIT = (1000, 60); from teamserver.cli import main; "
                "sys.exit(main(sys.argv[1:]))")
        cls.srv = subprocess.Popen([sys.executable, "-c", boot, "serve", "--port", str(cls.port), "--data", cls.data],
                                   cwd=REPO, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                with urllib.request.urlopen(cls.url + "/api/v1/health", timeout=1) as r:
                    if json.loads(r.read()).get("ok"):
                        break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("team server did not start")
        cls.owner_home = tempfile.mkdtemp(dir=cls.tmp.name, prefix="home-")
        cls._run(cls.owner_home, f"COLL = None\nout = team.join({cls.owner_link!r}, device_name='owner mac')")

    @classmethod
    def tearDownClass(cls):
        cls.srv.terminate()
        cls.srv.wait(10)
        cls.tmp.cleanup()

    @classmethod
    def cli(cls, *args):
        r = subprocess.run([sys.executable, "-m", "teamserver", args[0], "--data", cls.data, *args[1:]], cwd=REPO,
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            raise RuntimeError(r.stderr)
        return next((ln.split("App:", 1)[1].strip() for ln in r.stdout.splitlines() if "App:" in ln), r.stdout)

    @classmethod
    def _run(cls, home, code, timeout=60):
        env = {**os.environ, "MSTUDIO_HOME": home, "MSTUDIO_KEYCHAIN": "0", "STUDIO_DATA_DIR": os.path.join(home, "dp")}
        for k in ("SEO_STUDIO_DIR", "SOCIAL_DIR", "TEAM_SERVER_URL", "TEAM_DEVICE_TOKEN"):
            env.pop(k, None)
        src = (f"ENGINE_DIR = {str(ENGINE)!r}\n" + REVIEW_PRELUDE + textwrap.dedent(code)
               + "\nprint('@@' + json.dumps(out, default=str))\n")
        r = subprocess.run([sys.executable, "-c", src], cwd=ENGINE, env=env, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            raise AssertionError(f"client failed:\n{r.stderr}")
        line = next(ln for ln in reversed(r.stdout.splitlines()) if ln.startswith("@@"))
        return json.loads(line[2:])

    def run_in(self, home, code):
        return self._run(home, f"COLL = {self.coll!r}\n" + textwrap.dedent(code))

    def member(self, name, role="editor", approver=False):
        Reviews.n += 1
        link = self.cli("invite", "--email", f"{name.lower()}{Reviews.n}@example.test", "--role", role, "--name", f"{name}{Reviews.n}")
        h = tempfile.mkdtemp(dir=self.tmp.name, prefix="home-")
        st = self.run_in(h, f"out = team.join({link!r}, device_name='test mac')")
        self.assertTrue(st["joined"], st)
        if approver:
            self.as_owner(f"out = team._api('PATCH', '/api/v1/members/{st['me']['id']}', {{'approver': True}})")
        return h, st["me"]

    def as_owner(self, code):
        return self.run_in(self.owner_home, code)

    def policy(self, **areas):
        return self.as_owner(f"out = tr.set_policy({{'require_approval': {areas!r}}})")

    def setUp(self):
        Reviews.n += 1
        self.coll = f"ads:{9000 + Reviews.n}"  # every test its own Ads collection on the shared server
        # a clean slate on the shared server: rules off, earlier tests' approvals withdrawn
        self.as_owner("""
tr.set_policy({"require_approval": {"ads": False, "email": False, "seo": False, "social": False}})
for r in team._api("GET", "/api/v1/reviews?status=open,approved,changes_requested")["reviews"]:
    team._api("POST", f"/api/v1/reviews/{r['id']}/withdraw", {})
team.sync()
for d in em.list_drafts():  # no shared email drafts left over from earlier tests
    em.remove_draft(d["campaign_id"])
team.sync()
out = None
""")

    # ---------- tests ----------

    def test_request_approve_gated_push_stale_and_policy_off(self):
        alex, me_a = self.member("Alex")
        bo, me_b = self.member("Bo", approver=True)
        self.policy(ads=True)
        out = self.run_in(alex, """
            ads_put("a1", "Budget 50")
            ads_put("a2", "Budget 70", campaign="Generic")
            team.sync()
            out = {"before": gated({"why": "Spring push"}), "plan": tr.plan("ads", tr.entries_of([COLL]))}
        """)
        self.assertEqual(out["before"]["pushed"], "not called")  # nothing approved: the push function never runs
        self.assertFalse(out["before"]["res"]["ok"])
        self.assertTrue(out["before"]["res"]["needs_team_approval"])
        self.assertEqual({n["id"] for n in out["plan"]["needs"]}, {"a1", "a2"})
        self.assertEqual(out["plan"]["needs"][0]["reason"], "needs approval")
        # Alex asks Bo to approve a1 only; Alex can't decide it, a non-approver can't be asked
        rv = self.run_in(alex, f"""
            out = {{"rv": tr.request({{"items": [{{"coll": COLL, "id": "a1"}}], "reviewers": [{me_b['id']}], "note": "Brand only"}}),
                   "self": err(tr.request, {{"items": [{{"coll": COLL, "id": "a1"}}], "reviewers": [{me_a['id']}]}})}}
            out["decide_own"] = err(tr.decide, {{"id": out["rv"]["id"], "decision": "approve"}})
        """)
        self.assertEqual(rv["rv"]["status"], "open")
        self.assertEqual(rv["rv"]["items"][0]["version"], 1)
        self.assertIn("own", rv["self"])
        self.assertIn("own review", rv["decide_own"])
        rid = rv["rv"]["id"]
        seen = self.run_in(bo, f"""
            todo = tr.list_reviews({{"filter": "to_me"}})["reviews"]
            v = tr.view({{"id": {rid}}})
            out = {{"todo": [r["id"] for r in todo], "can": v["can_decide"], "lines": v["items"][0]["lines"],
                   "match": v["items"][0]["local_matches"], "dec": tr.decide({{"id": {rid}, "decision": "approve", "note": "ok"}})}}
        """)
        self.assertEqual(seen["todo"], [rid])
        self.assertTrue(seen["can"])
        self.assertTrue(seen["match"])
        self.assertEqual(seen["lines"][0]["new"], "Budget 50")
        self.assertEqual(seen["dec"]["status"], "approved")
        # approved: the gated push includes a1 only; the why note is required and lands in the feed
        out = self.run_in(alex, """
            out = {"no_why": err(gated, {}), "push": gated({"why": "  Brand budget\\nfor spring  "})}
            out["events"] = [e for e in team._api("GET", "/api/v1/events?since=0")["events"] if e["kind"] == "push"]
        """)
        self.assertIn("why", out["no_why"])
        self.assertEqual(out["push"]["pushed"], ["a1"])
        self.assertTrue(out["push"]["res"]["reported"])
        ev = out["events"][-1]
        self.assertEqual(ev["area"], "ads")
        self.assertEqual(ev["data"]["why"], "Brand budget for spring")
        self.assertIn("Brand budget for spring", ev["summary"])
        self.assertEqual(ev["data"]["items"], [{"coll": self.coll, "id": "a1"}])
        # a bookkeeping-only change keeps the approval; a real edit makes it stale and drops it from the push
        out = self.run_in(alex, """
            ads_put("a1", "Budget 50", extra={"problems": ["checked"], "at": "now"})
            keep = gated({"why": "x"})["pushed"]
            ads_put("a1", "Budget 55")
            p = tr.plan("ads", tr.entries_of([COLL]))
            out = {"keep": keep, "after_edit": gated({"why": "x"}), "reasons": {n["id"]: n["reason"] for n in p["needs"]}}
            team.sync()
            out["server_stale"] = tr.list_reviews({"filter": "mine"})["reviews"][0]["items"][0]["stale"]
        """)
        self.assertEqual(out["keep"], ["a1"])
        self.assertEqual(out["after_edit"]["pushed"], "not called")
        self.assertEqual(out["reasons"]["a1"], "changed since it was approved")
        self.assertTrue(out["server_stale"])
        # policy off: no gating (everything goes), the why note is still asked for in a team
        self.policy(ads=False)
        out = self.run_in(alex, "out = {'push': gated({'why': 'all'}), 'no_why': err(gated, {})}")
        self.assertIsNone(out["push"]["pushed"])
        self.assertIn("why", out["no_why"])

    def test_push_by_person_changeset_and_offline(self):
        alex, me_a = self.member("Alex")
        bo, me_b = self.member("Bo", approver=True)
        cara, me_c = self.member("Cara", approver=True)
        self.run_in(bo, 'ads_put("b1", "Bo budget"); team.sync(); out = None')
        out = self.run_in(alex, """
            team.changeset_set(name="Autumn")
            ads_put("a1", "Alex budget")
            team.sync()
            p = tr.plan("ads", tr.entries_of([COLL]), "all")
            out = {"people": p["people"], "mine": gated({"why": "mine", "scope": "mine"})["pushed"],
                   "all": gated({"why": "all"})["pushed"]}
        """)
        self.assertEqual({p["name"]: p["count"] for p in out["people"]}, {"You": 1, me_b["name"]: 1})
        self.assertEqual(out["mine"], ["a1"])
        self.assertIsNone(out["all"])
        # the whole changeset goes for approval; Cara approves; with the policy on, "all approved" has a1 but not b1
        self.policy(ads=True)
        rv = self.run_in(alex, f"out = tr.request({{'changeset': True, 'reviewers': [{me_c['id']}]}})")
        self.assertEqual(rv["title"], "Autumn")
        self.assertEqual([i["id"] for i in rv["items"]], ["a1"])
        self.run_in(cara, f"out = tr.decide({{'id': {rv['id']}, 'decision': 'approve'}})")
        out = self.run_in(alex, """
            p = tr.plan("ads", tr.entries_of([COLL]))
            out = {"push": gated({"why": "approved only"})["pushed"], "needs": [n["id"] for n in p["needs"]]}
        """)
        self.assertEqual(out["push"], ["a1"])
        self.assertEqual(out["needs"], ["b1"])
        # Bo's view: b1 needs approval, the whole campaign can go as a group
        out = self.run_in(bo, f"out = tr.request({{'group': {{'area': 'ads', 'coll': COLL, 'key': 'Brand'}}, 'reviewers': [{me_c['id']}]}})")
        self.assertEqual(sorted(i["id"] for i in out["items"]), ["a1", "b1"])
        # offline with the policy on: nothing goes (fail closed)
        out = self.run_in(alex, """
            appconfig.set_many({"TEAM_SERVER_URL": "http://127.0.0.1:9"})
            out = gated({"why": "offline"})
        """)
        self.assertEqual(out["pushed"], "not called")

    def test_single_item_email_owners_and_solo(self):
        alex, me_a = self.member("Alex")
        bo, me_b = self.member("Bo", approver=True)
        self.policy(email=True)
        prelude = """
            import datetime as dt
            from zoneinfo import ZoneInfo
            W = (dt.datetime.now(ZoneInfo("Europe/London")) + dt.timedelta(days=2)).strftime("%Y-%m-%dT09:00")
            def sched():
                return em.p_schedule({"id": "demo-4", "confirm": em.CONFIRM, "when": W, "tz": "Europe/London", "why": "launch"})
            def status():
                return em.adapter().get_campaign("demo-4", fresh=True)["status"]
            def single():
                calls = []
                r = tr.single_action("email", "email", "demo-4", {"why": "launch"}, "Sent an email",
                                     lambda: calls.append(1) or {"ok": True, "message": "sent"})
                return {"ok": bool(r.get("ok")), "calls": len(calls), "message": r.get("message"), "reported": r.get("reported")}
        """
        # no approval: the real Schedule is refused and nothing changes on the platform
        prelude = textwrap.dedent(prelude)
        out = self.run_in(alex, prelude + "out = {'sched': sched(), 'status': status(), 'gate': tr.gate('email', 'email', 'demo-4'),"
                                          " 'nothing': err(tr.request, {'group': {'area': 'email', 'key': 'nope'}, 'reviewers': [1]})}")
        self.assertTrue(out["sched"].get("needs_team_approval"), out["sched"])
        self.assertEqual(out["status"], "draft")
        self.assertEqual(out["gate"]["needs"][0]["id"], "demo-4")
        self.assertIn("Can't read", out["nothing"])  # a review of nothing is refused
        # approval of the campaign as the platform has it (no Studio draft): the content that goes out is hashed
        rv = self.run_in(alex, f"out = tr.request({{'group': {{'area': 'email', 'key': 'demo-4'}}, 'reviewers': [{me_b['id']}]}})")
        self.assertGreaterEqual(rv["items"][0]["version"], 0)  # 0 unless a teammate has a Studio draft of it
        self.assertEqual(len(rv["items"][0]["content_hash"]), 64)
        seen = self.run_in(bo, f"v = tr.view({{'id': {rv['id']}}}); out = {{'match': v['items'][0]['local_matches'],"
                               f" 'lines': v['items'][0]['lines'], 'dec': tr.decide({{'id': {rv['id']}, 'decision': 'approve'}})['status']}}")
        self.assertTrue(seen["match"])
        self.assertIn("The new season opens", json.dumps(seen["lines"]))
        self.assertEqual(seen["dec"], "approved")
        out = self.run_in(alex, prelude + textwrap.dedent("""
            ok = single()
            # the review's hole: add a Studio draft, Save to platform (draft gone again), then Send: refused
            em.add_draft({"campaign_id": "demo-4", "fields": {"subject": "Sneaky new subject"}})
            em.p_save_to_platform({"id": "demo-4"})
            after_save = {"draft": em.list_drafts(), "single": single(), "sched": sched(), "status": status()}
            # an edit on the platform outside Studio, put back: covered again; changed: refused
            em.adapter().patch("demo-4", {"subject": "The new season opens"})
            back = single()
            em.adapter().patch("demo-4", {"preview_text": "Changed on the platform"})
            outside = single()
            em.adapter().patch("demo-4", {"preview_text": "Dates and spaces for the new season"})
            # the check right before sending: the platform copy is read again
            body = {"_team_expect": tr._single_entry("email", "demo-4")["hash"]}
            em.adapter().patch("demo-4", {"subject": "Last-second edit"})
            late = tr.recheck(body, "email", "demo-4")
            em.adapter().patch("demo-4", {"subject": "The new season opens"})
            final = sched()
            out = {"ok": ok, "after_save": after_save, "back": back, "outside": outside, "late": late,
                   "final": final, "status": status()}
        """))
        self.assertEqual(out["ok"]["calls"], 1)
        self.assertTrue(out["ok"]["reported"])
        self.assertEqual(out["after_save"]["draft"], [])
        self.assertEqual(out["after_save"]["single"]["calls"], 0)
        self.assertIn("changed since it was approved", out["after_save"]["single"]["message"])
        self.assertTrue(out["after_save"]["sched"]["needs_team_approval"])
        self.assertEqual(out["after_save"]["status"], "draft")
        self.assertEqual(out["back"]["calls"], 1)
        self.assertEqual(out["outside"]["calls"], 0)
        self.assertIn("changed after it was approved", out["late"])
        self.assertTrue(out["final"]["ok"], out["final"])
        self.assertTrue(out["final"]["reported"])
        self.assertEqual(out["status"], "scheduled")
        # owners: advice only, never for my own scopes; admins set them
        self.as_owner(f"out = tr.set_owners({{'owners': {{'email/77': {me_b['id']}, 'email/78': {me_a['id']}}}}})")
        out = self.run_in(alex, """
            out = {"77": tr.owner_note(["email/77", "email"]), "78": tr.owner_note(["email/78", "email"]),
                   "none": tr.owner_note(["email/79"]), "set": err(tr.set_owners, {"owners": {"email/1": 1}})}
        """)
        self.assertEqual(out["77"]["note"], f"Owned by {me_b['name']}")
        self.assertEqual(out["78"], {})
        self.assertEqual(out["none"], {})
        self.assertIn("admin", out["set"])
        # solo Studio: no gating, no why note, the push function gets everything
        solo = tempfile.mkdtemp(dir=self.tmp.name, prefix="home-")
        out = self.run_in(solo, """
            ads_put("s1", "Solo")
            calls = []
            r = tr.single_action("email", "email", "1", {}, "Sent", lambda: calls.append(1) or {"ok": True})
            out = {"push": gated({}), "single": r, "calls": len(calls), "gate": tr.gate("ads", COLL, "s1")}
        """)
        self.assertIsNone(out["push"]["pushed"])
        self.assertNotIn("team", out["push"]["res"])
        self.assertEqual(out["calls"], 1)
        self.assertFalse(out["gate"]["joined"])

    def test_fail_closed_scope_mine_push_snapshot_and_editor(self):
        alex, me_a = self.member("Alex")
        bo, me_b = self.member("Bo", approver=True)
        # never seen the rules and can't reach the server: live actions wait
        cold, _ = self.member("Cold")
        out = self.run_in(cold, """
            ads_put("c1", "Cold budget")
            appconfig.set_many({"TEAM_SERVER_URL": "http://127.0.0.1:9"})
            out = {"push": gated({"why": "x"}), "single": tr.single_action("social", "social", "x", {"why": "x"}, "Posted", lambda: {"ok": True})}
        """)
        self.assertEqual(out["push"]["pushed"], "not called")
        self.assertIn("Can't check the team's approval rules", out["push"]["res"]["message"])
        self.assertFalse(out["single"]["ok"])
        self.assertIn("Can't check the team's approval rules", out["single"]["message"])
        # Only mine for one item: someone else's post is refused
        self.run_in(bo, """
            with draftstore.tx("social", ROOT, by="you") as t:
                t.put("sp1", {"id": "sp1", "title": "Bo's post"}, "you")
            team.sync(); out = None
        """)
        out = self.run_in(alex, """
            team.sync()
            calls = []
            act = lambda: calls.append(1) or {"ok": True}
            mine = tr.single_action("social", "social", "sp1", {"why": "w", "scope": "mine"}, "Posted", act)
            out = {"mine": mine, "calls": len(calls)}
            tr.single_action("social", "social", "sp1", {"why": "w"}, "Posted", act)
            out["all_calls"] = len(calls)
        """)
        self.assertFalse(out["mine"]["ok"])
        self.assertIn("someone else's", out["mine"]["message"])
        self.assertEqual(out["calls"], 0)
        self.assertEqual(out["all_calls"], 1)
        # approved a1, then a1 is edited while the push runs: the push's own read leaves it out
        self.policy(ads=True)
        rv = self.run_in(alex, f"""
            ads_put("a1", "Budget 10")
            team.sync()
            out = tr.request({{"items": [{{"coll": COLL, "id": "a1"}}], "reviewers": [{me_b['id']}]}})
        """)
        self.run_in(bo, f"out = tr.decide({{'id': {rv['id']}, 'decision': 'approve'}})")
        out = self.run_in(alex, """
            def racy(only):
                ads_put("a1", "Budget 99")  # someone edits it after the plan was made
                items = draftstore.read(COLL, ROOT)["items"]
                return {"ok": True, "got": [i["id"] for i in items if tr.admits(only, i)]}
            ads_put("a1", "Budget 10")
            res = tr.gated_push("ads", COLL, {"why": "race"}, racy, "Pushed")
            # Send to Editor goes through the same gate and lands in the feed as send_to_editor
            ads_put("a1", "Budget 10")
            ed = tr.gated_push("ads", COLL, {"why": "to Editor"}, lambda only: {"ok": True, "output": "sent", "got": sorted(only)},
                               "Sent to Google Ads Editor", kind="send_to_editor")
            evs = [e for e in team._api("GET", "/api/v1/events?since=0")["events"] if e["kind"] == "send_to_editor"]
            out = {"race": res["got"], "editor": ed["got"], "ev": evs[-1]["summary"] if evs else None,
                   "no_why": err(tr.gated_push, "ads", COLL, {}, lambda only: {"ok": True}, "x", "send_to_editor")}
        """)
        self.assertEqual(out["race"], [])
        self.assertEqual(out["editor"], ["a1"])
        self.assertIn("to Editor", out["ev"])
        self.assertIn("why", out["no_why"])

    def shared_email(self, homes):
        """Members of one team share one email platform: point every Studio's demo platform at the same file."""
        d = tempfile.mkdtemp(dir=self.tmp.name, prefix="emailplatform-")
        return f"from areas import email as _em\n_em.FakeAdapter.__init__.__defaults__ = ({os.path.join(d, 'email_fake.json')!r},)\n"

    # regression tests from the third independent review: the approver sees everything that goes out
    def test_review_shows_every_social_field_sent(self):
        alex, _ = self.member("Alex")
        bo, me_b = self.member("Bo", approver=True)
        self.policy(social=True)
        pid = self.run_in(alex, SP + textwrap.dedent("""
            sst.save_settings({**sst.settings(), "local_scheduler": True})
            W = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=3)).replace(microsecond=0).isoformat()
            p = sst.save({"title": "Launch", "channels": ["linkedin"], "when": W, "caption": "Shown caption",
                          "captions": {"linkedin": "HIDDEN_LINKEDIN_TEXT"}, "first_comment": "HIDDEN_COMMENT",
                          "hashtags": "#hiddentag", "alt_text": "HIDDEN_ALT", "format": "text"}, "you")
            team.sync(); out = p["id"]
        """))
        rv = self.run_in(alex, f"PID={pid!r}\nout = tr.request({{'items': [{{'coll': 'social', 'id': PID}}], 'reviewers': [{me_b['id']}]}})")
        seen = self.run_in(bo, f"""
            team.sync()
            v = tr.view({{"id": {rv['id']}}})
            d = tr.decide({{"id": {rv['id']}, "decision": "approve"}})
            out = {{"lines": v["items"][0]["lines"], "status": d.get("status")}}
        """)
        out = self.run_in(alex, SP + textwrap.dedent(f"""
            PID = {pid!r}
            team.sync()
            r = so.schedule({{"id": PID, "pressed": "user", "why": "launch", "channels": ["linkedin"]}})
            q = [x["payload"] for x in sst.queue() if x["post_id"] == PID and x["status"] == "scheduled"]
            out = {{"ok": r.get("ok"), "msg": r.get("message"), "res": r.get("results"), "probs": r.get("problems"), "payload": json.dumps(q)}}
        """))
        shown = json.dumps(seen["lines"])
        live = out["payload"]
        for tok in ("HIDDEN_LINKEDIN_TEXT", "hiddentag"):
            self.assertIn(tok, live)
            self.assertIn(tok, shown, f"{tok} goes out but the approver was never shown it")
        for tok in ("HIDDEN_COMMENT", "HIDDEN_ALT"):  # every field of the post is on the screen, sent or not
            self.assertIn(tok, shown)

    def test_review_shows_full_email_html(self):
        alex, _ = self.member("Alex")
        bo, me_b = self.member("Bo", approver=True)
        self.policy(email=True)
        shared = self.shared_email([alex, bo])
        html = "<p>" + ("x" * 1200) + " HIDDEN_TAIL_PARAGRAPH <a href=\"{{unsubscribe_url}}\">Unsubscribe</a></p>"
        self.run_in(alex, shared + f"team.sync(); em.add_draft({{'campaign_id': 'demo-4', 'fields': {{'html': {html!r}}}}}); team.sync(); out=None")
        rv = self.run_in(alex, shared + f"out = tr.request({{'group': {{'area': 'email', 'key': 'demo-4'}}, 'reviewers': [{me_b['id']}]}})")
        seen = self.run_in(bo, shared + textwrap.dedent(f"""
            team.sync()
            v = tr.view({{"id": {rv['id']}}})
            d = tr.decide({{"id": {rv['id']}, "decision": "approve"}})
            out = {{"lines": v["items"][0]["lines"], "status": d.get("status")}}
        """))
        out = self.run_in(alex, shared + textwrap.dedent("""
            team.sync()
            s = em.p_send_now({"id": "demo-4", "confirm": em.CONFIRM, "why": "go"})
            c = em.adapter().get_campaign("demo-4", fresh=True)
            out = {"msg": s.get("message"), "probs": s.get("problems"), "ok": s.get("ok"), "status": c["status"], "html_has": "HIDDEN_TAIL" in (c.get("html") or "")}
        """))
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["html_has"])
        html_rows = [l for l in seen["lines"] if l["kind"] == "html"]
        self.assertEqual(len(html_rows), 1)
        self.assertIn("HIDDEN_TAIL", html_rows[0]["new"], "email body sent but the approver saw only a truncated html")

    def test_review_shows_full_new_page(self):
        alex, _ = self.member("Alex")
        bo, me_b = self.member("Bo", approver=True)
        self.policy(seo=True)
        out = self.run_in(alex, """
            from areas import seo
            r = seo.save_new_post({"title": "New page", "slug": "new-page", "content": "<p>" + "y"*800 + " HIDDEN_SEO_TAIL</p>", "status": "publish"}, by="you")
            team.sync()
            pv = seo.publish_preview({}, link_check=lambda u: (200, ""))
            out = {"new_pages": pv["new_pages"], "team": pv.get("team", {}).get("needs"), "ids": [x["id"] for x in seo._draft().get("new_posts", [])]}
        """)
        self.assertEqual(out["new_pages"], 0)
        nid = out["ids"][0]
        rv = self.run_in(alex, f"out = tr.request({{'items': [{{'coll': 'seo:new_posts', 'id': {nid!r}}}], 'reviewers': [{me_b['id']}]}})")
        seen = self.run_in(bo, f"team.sync(); v = tr.view({{'id': {rv['id']}}}); d = tr.decide({{'id': {rv['id']}, 'decision': 'approve'}}); out = {{'lines': v['items'][0]['lines'], 's': d.get('status')}}")
        out2 = self.run_in(alex, """
            from areas import seo
            team.sync()
            pv = seo.publish_preview({}, link_check=lambda u: (200, ""))
            r = seo.publish({"token": pv["token"], "why": "x"}, lambda u: (200, ""))
            out = {"new_pages": pv["new_pages"], "ok": r.get("ok"), "team_event": r.get("team_event"), "live": r.get("live")}
        """)
        self.assertEqual(seen["s"], "approved")
        self.assertTrue(out2["ok"], out2)
        self.assertEqual(out2["new_pages"], 1)
        self.assertTrue(out2["team_event"])
        content = [l for l in seen["lines"] if l["label"] == "content"]
        self.assertEqual(content[0]["kind"], "html")
        self.assertIn("HIDDEN_SEO_TAIL", content[0]["new"])

    def test_earlier_editor_cannot_approve(self):
        alex, _ = self.member("Alex")
        bo, me_b = self.member("Bo", approver=True)
        self.policy(ads=True)
        self.run_in(alex, "ads_put('e1', 'Budget 10'); team.sync(); out=None")
        self.run_in(bo, "team.sync(); ads_put('e1', 'BO WROTE THIS'); team.sync(); out=None")
        out = self.run_in(alex, """
            team.sync()
            with draftstore.tx(COLL, ROOT, by="you") as t:
                d = dict(draftstore.peek(COLL, "e1", ROOT)[0]); d["note"] = "ok"
                t.put("e1", d, "you")
            team.sync(); out = draftstore.peek(COLL, "e1", ROOT)[0]["summary"]
        """)
        rv = self.run_in(alex, f"out = err(tr.request, {{'items': [{{'coll': COLL, 'id': 'e1'}}], 'reviewers': [{me_b['id']}]}}) or tr.request({{'items': [{{'coll': COLL, 'id': 'e1'}}], 'reviewers': [{me_b['id']}]}})")
        self.assertIn("can't approve", rv)  # Bo edited it earlier: he can't be asked
        if isinstance(rv, dict):
            d = self.run_in(bo, f"team.sync(); out = err(tr.decide, {{'id': {rv['id']}, 'decision': 'approve'}}) or 'APPROVED'")
            self.assertNotEqual(d, "APPROVED", "Bo approved content he wrote himself (someone else touched it last)")

    def test_changes_requested_elsewhere_blocks_push(self):
        alex, _ = self.member("Alex")
        bo, me_b = self.member("Bo", approver=True)
        cy, me_c = self.member("Cy", approver=True)
        self.policy(ads=True)
        rv1 = self.run_in(alex, f"""
            ads_put("v1", "A1"); ads_put("v2", "A2"); team.sync()
            out = tr.request({{"items": [{{"coll": COLL, "id": "v1"}}, {{"coll": COLL, "id": "v2"}}], "reviewers": [{me_b['id']}]}})
        """)
        rv2 = self.run_in(alex, f"out = tr.request({{'items': [{{'coll': COLL, 'id': 'v1'}}], 'reviewers': [{me_c['id']}]}})")
        self.run_in(cy, f"team.sync(); out = tr.decide({{'id': {rv2['id']}, 'decision': 'request_changes'}})")
        self.run_in(bo, f"team.sync(); out = tr.decide({{'id': {rv1['id']}, 'decision': 'approve'}})")
        out = self.run_in(alex, "out = gated({'why': 'w'})['pushed']")
        self.assertEqual(out, ["v2"])  # v1 is held by Cy's request for changes, v2 is approved

    # final review: withdrawing a review with changes requested must not lift the hold
    def test_withdraw_does_not_lift_changes_requested(self):
        alex, _ = self.member("Alex")
        bo, me_b = self.member("Bo", approver=True)
        cy, me_c = self.member("Cy", approver=True)
        self.policy(ads=True)
        rv1 = self.run_in(alex, f"""
            ads_put("w1", "B1"); team.sync()
            out = tr.request({{"items": [{{"coll": COLL, "id": "w1"}}], "reviewers": [{me_b['id']}]}})
        """)
        rv2 = self.run_in(alex, f"out = tr.request({{'items': [{{'coll': COLL, 'id': 'w1'}}], 'reviewers': [{me_c['id']}]}})")
        self.run_in(bo, f"team.sync(); out = tr.decide({{'id': {rv1['id']}, 'decision': 'approve'}})['status']")
        self.run_in(cy, f"team.sync(); out = tr.decide({{'id': {rv2['id']}, 'decision': 'request_changes', 'note': 'no'}})['status']")
        before = self.run_in(alex, "out = gated({'why': 'w'})['pushed']")
        w = self.run_in(alex, f"out = err(tr.withdraw, {{'id': {rv2['id']}}})")
        after = self.run_in(alex, "out = gated({'why': 'w'})['pushed']")
        self.assertEqual(before, "not called")
        self.assertIn("only an admin or owner", w)
        self.assertEqual(after, "not called", "requester withdrew the changes-requested review and pushed unchanged content")
        # the hold lifts when the content changes (and then it needs a fresh approval)
        out = self.run_in(alex, """
            ads_put("w1", "B2"); team.sync()
            p = tr.plan("ads", tr.entries_of([COLL]))
            out = [n["reason"] for n in p["needs"]]
        """)
        self.assertEqual(out, ["needs approval"])  # no longer held, but the old approval is stale too

    def test_holds_and_approvals_read_every_page(self):
        out = self.run_in(self.home_solo(), """
            pages = {None: {"reviews": [{"id": 3}], "more": True, "next_before": 3},
                     3: {"reviews": [{"id": 2}], "more": True, "next_before": 2},
                     2: {"reviews": [{"id": 1}], "more": False, "next_before": None}}
            asked = []
            def api(method, path, body=None):
                asked.append(path)
                b = int(path.split("before=")[1]) if "before=" in path else None
                return pages[b]
            tr._api = api
            out = {"ids": [r["id"] for r in tr._all_reviews("status=changes_requested&current=1")], "asked": asked}
        """)
        self.assertEqual(out["ids"], [3, 2, 1])
        self.assertEqual(len(out["asked"]), 3)

    def home_solo(self):
        return tempfile.mkdtemp(dir=self.tmp.name, prefix="home-")

    # regression tests from the second independent review (M1, M2)
    def test_stale_review_cannot_be_approved_then_reverted(self):
        alex, _ = self.member("Alex")
        bo, me_b = self.member("Bo", approver=True)
        self.policy(ads=True)
        rv = self.run_in(alex, f"""
            ads_put("x1", "Budget 99999 EVIL")
            team.sync()
            r = tr.request({{"items": [{{"coll": COLL, "id": "x1"}}], "reviewers": [{me_b['id']}]}})
            ads_put("x1", "Budget 10 benign")
            team.sync()
            out = r
        """)
        seen = self.run_in(bo, f"""
            team.sync()
            v = tr.view({{"id": {rv['id']}}})
            d = tr.decide({{"id": {rv['id']}, "decision": "approve"}})
            out = {{"lines": v["items"][0]["lines"], "stale": v["items"][0]["stale"], "status": d["status"]}}
        """)
        out = self.run_in(alex, """
            team.sync()
            before = gated({"why": "w"})
            ads_put("x1", "Budget 99999 EVIL")
            team.sync()
            out = {"before": before["pushed"], "after": gated({"why": "w"})["pushed"]}
        """)
        self.assertEqual(seen["status"], "open")  # refused: stale, nothing approved
        self.assertNotIn("EVIL", json.dumps(seen["lines"]))  # the approver saw only the benign version
        # the approval must not cover the evil content the approver never saw
        self.assertNotEqual(out["after"], ["x1"], "approver approved a stale review; the requester reverted to the unseen content and pushed it")

    def test_social_move_keeps_the_approved_payload(self):
        alex, _ = self.member("Alex")
        bo, me_b = self.member("Bo", approver=True)
        cy, _ = self.member("Cy")
        self.policy(social=True)
        sp = """
            from areas import social as so
            from areas import _social_store as sst
            import datetime as dt
        """
        pid = self.run_in(alex, sp + """
            sst.save_settings({**sst.settings(), "local_scheduler": True})
            W = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=3)).replace(microsecond=0).isoformat()
            p = sst.save({"title": "Launch", "channels": ["linkedin"], "when": W, "caption": "Approved caption",
                          "format": "text"}, "you")
            team.sync(); out = p["id"]
        """)
        self.run_in(cy, "team.sync(); out=None")
        rv = self.run_in(alex, f"PID={pid!r}\nout = tr.request({{'items': [{{'coll': 'social', 'id': PID}}], 'reviewers': [{me_b['id']}]}})")
        self.run_in(bo, f"out = tr.decide({{'id': {rv['id']}, 'decision': 'approve'}})")
        # Cy edits the caption on his Mac (not synced yet)
        self.run_in(cy, sp.replace("import datetime as dt", "import datetime as dt\n            PID = " + repr(pid)) + """
            so.save({"id": PID, "post": {**sst.get(PID), "caption": "UNAPPROVED caption by Cy"}})
            out = None
        """)
        out = self.run_in(alex, sp.replace("import datetime as dt", "import datetime as dt\n            PID = " + repr(pid)) + """
            r = so.schedule({"id": PID, "pressed": "user", "why": "launch"})
            team.sync()
            q = [x["payload"] for x in sst.queue() if x["post_id"] == PID and x["status"] == "scheduled"]
            out = {"sched": {k: r.get(k) for k in ("ok", "message")}, "payload_before": json.dumps(q)[:400]}
        """)
        cy_sync = self.run_in(cy, "out = team.sync()")
        out2 = self.run_in(alex, sp.replace("import datetime as dt", "import datetime as dt\n            PID = " + repr(pid)) + """
            team.sync()
            p = sst.get(PID)
            W2 = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=4)).replace(microsecond=0).isoformat()
            so.move({"id": PID, "when": W2})
            cm = so.confirm_move({"id": PID, "pressed": "user"})
            q = [x["payload"] for x in sst.queue() if x["post_id"] == PID and x["status"] == "scheduled"]
            out = {"caption_now": p.get("caption"), "results": p.get("results"), "move": {k: cm.get(k) for k in ("ok", "message")},
                   "payload_after": json.dumps(q)[:600]}
        """)
        self.assertNotIn("UNAPPROVED", out2["payload_after"], "unapproved caption is in the scheduled queue payload")

    def test_seo_publish_gated_and_viewer(self):
        alex, me_a = self.member("Alex")
        bo, me_b = self.member("Bo", approver=True)
        vic, _ = self.member("Vic", role="viewer")
        self.policy(seo=True)
        out = self.run_in(alex, """
            seo_put("c1", {"post_id": 5, "post_title": "Post five", "url": "https://example.test/five/", "field": "title",
                           "old": "Old", "new": "New title"})
            seo_put("c2", {"post_id": 6, "post_title": "Post six", "url": "https://example.test/six/", "field": "title",
                           "old": "Old", "new": "Other"})
            team.sync()
            from areas import seo
            pv = seo.publish_preview({}, link_check=lambda u: (200, ""))
            out = {"posts": [p["post_id"] for p in pv["posts"]], "team": pv["team"]}
        """)
        self.assertEqual(out["posts"], [])
        self.assertEqual({n["id"] for n in out["team"]["needs"] if n["coll"] == "seo:changes"}, {"c1", "c2"})
        rv = self.run_in(alex, f"out = tr.request({{'group': {{'area': 'seo', 'key': '5'}}, 'reviewers': [{me_b['id']}]}})")
        self.assertEqual([i["id"] for i in rv["items"]], ["c1"])
        self.run_in(bo, f"out = tr.decide({{'id': {rv['id']}, 'decision': 'approve'}})")
        out = self.run_in(alex, """
            from areas import seo
            pv = seo.publish_preview({}, link_check=lambda u: (200, ""))
            out = {"posts": [p["post_id"] for p in pv["posts"]], "no_why": err(seo.publish, {"token": pv["token"]}, lambda u: (200, ""))}
        """)
        self.assertEqual(out["posts"], [5])
        self.assertIn("why", out["no_why"])
        # viewers can't ask for reviews
        out = self.run_in(vic, f"out = err(tr.request, {{'changeset': True, 'reviewers': [{me_b['id']}]}})")
        self.assertIn("Viewers", out)


if __name__ == "__main__":
    unittest.main()
