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


class Reviews(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="mstudio-reviews-")
        cls.data = os.path.join(cls.tmp.name, "server")
        cls.port = free_port()
        cls.url = f"http://127.0.0.1:{cls.port}"
        cls.n = 0
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
        link = self.cli("invite", "--email", f"{name.lower()}{self.n}@example.test", "--role", role, "--name", f"{name}{self.n}")
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
        self.coll = f"ads:{9000 + self.n}"  # every test its own Ads collection on the shared server
        self.policy(ads=False, email=False, seo=False, social=False)

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
        out = self.run_in(alex, """
            em.add_draft({"campaign_id": "77", "fields": {"subject": "Hello"}})
            team.sync()
            calls = []
            act = lambda: calls.append(1) or {"ok": True, "message": "sent"}
            blocked = tr.single_action("email", "email", "77", {"why": "launch"}, "Sent an email", act)
            out = {"blocked": blocked, "calls": len(calls), "gate": tr.gate("email", "email", "77")}
        """)
        self.assertTrue(out["blocked"]["needs_team_approval"])
        self.assertEqual(out["calls"], 0)
        self.assertEqual(out["gate"]["needs"][0]["id"], "77")
        rv = self.run_in(alex, f"out = tr.request({{'group': {{'area': 'email', 'key': '77'}}, 'reviewers': [{me_b['id']}]}})")
        self.run_in(bo, f"out = tr.decide({{'id': {rv['id']}, 'decision': 'approve'}})")
        out = self.run_in(alex, """
            calls = []
            act = lambda: calls.append(1) or {"ok": True, "message": "sent"}
            res = tr.single_action("email", "email", "77", {"why": "launch"}, "Sent an email", act)
            out = {"res": res, "calls": len(calls)}
        """)
        self.assertTrue(out["res"]["ok"])
        self.assertEqual(out["calls"], 1)
        self.assertTrue(out["res"]["reported"])
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
        self.assertEqual({n["id"] for n in out["team"]["needs"]}, {"c1", "c2"})
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
