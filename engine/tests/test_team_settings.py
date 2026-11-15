"""Team settings, the shared calendar's clash warnings and the shared library (team_settings.py, shared_calendar.py),
against the real team server.

Run from the engine folder:  python3 tests/test_team_settings.py
Like test_team.py: starts `python3 -m teamserver serve` on a free port in a temp folder, and runs each Studio in its
own subprocess with its own MSTUDIO_HOME. Never touches the real data folder or the Keychain (MSTUDIO_KEYCHAIN=0).
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

ENGINE = Path(__file__).resolve().parents[1]
REPO = ENGINE.parent

PRELUDE = """
import json, sys, datetime as dt
sys.path.insert(0, ENGINE_DIR)
import appconfig, core, draftstore, team, team_settings as ts, shared_calendar as cal
from areas import email as em, _social_store as store, _social_checks as schecks, _seo_checks as seochk
ROOT = core.STUDIO_DIR
RULE = {"id": "cheap", "level": "block", "pattern": r"\\bcheap\\b", "message": "say good value, not cheap", "areas": []}
def social_probs(text):
    return schecks.copy_problems("caption", text)
def ads_warn(text):
    return core._warn([{"Headline 1": text}])
def ads_block(text):
    return core.check({"type": "rsa", "campaign": "C", "ad_group": "G", "final_url": "https://example.com/",
                       "headlines": [{"text": text}, {"text": "Two"}, {"text": "Three"}], "descriptions": [{"text": "A fine line"}, {"text": "Another"}]})[0]
out = None
"""


def free_port():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Shared(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="mstudio-teamset-")
        cls.data = os.path.join(cls.tmp.name, "server")
        cls.port = free_port()
        cls.url = f"http://127.0.0.1:{cls.port}"
        cls.n = 0
        cls.cli("init", "--email", "owner@example.test", "--name", "Olive", "--team", "Test team", "--public-url", cls.url)
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

    def home(self):
        return tempfile.mkdtemp(dir=self.tmp.name, prefix="home-")

    def run_in(self, home, code, timeout=90):
        env = {**os.environ, "MSTUDIO_HOME": home, "MSTUDIO_KEYCHAIN": "0", "STUDIO_DATA_DIR": os.path.join(home, "dp")}
        for k in ("SEO_STUDIO_DIR", "SOCIAL_DIR", "TEAM_SERVER_URL", "TEAM_DEVICE_TOKEN"):
            env.pop(k, None)
        src = f"ENGINE_DIR = {str(ENGINE)!r}\n" + PRELUDE + textwrap.dedent(code) + "\nprint('@@' + json.dumps(out, default=str))\n"
        r = subprocess.run([sys.executable, "-c", src], cwd=ENGINE, env=env, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            self.fail(f"client failed:\n{r.stderr}")
        line = next(ln for ln in reversed(r.stdout.splitlines()) if ln.startswith("@@"))
        return json.loads(line[2:])

    def member(self, name, role="editor"):
        Shared.n += 1
        link = self.cli("invite", "--email", f"{name.lower()}{self.n}@example.test", "--role", role, "--name", f"{name}{self.n}")
        h = self.home()
        st = self.run_in(h, f"out = team.join({link!r}, device_name='test mac')")
        self.assertTrue(st["joined"], st)
        return h, st["me"]["name"]

    def put_team(self, h, key, value):
        return self.run_in(h, f"out = ts.save({key!r}, {value!r}, scope='team')['keys'][{key!r}]")

    def clear_team(self, h, key):
        self.run_in(h, f"out = ts.save({key!r}, None, scope='team')['keys'][{key!r}]['source']")

    # ---------- solo ----------

    def test_new_install_has_no_copy_rules(self):
        h = self.home()
        r = self.run_in(h, """
first = ts.first_run()
again = ts.first_run()
out = {"first": first, "again": again, "rules": ts.get("copy_rules"), "social": social_probs("Bahamas, guaranteed \\u2014 yes"),
       "ads": ads_warn("Book a cabin"), "seo": seochk.text_rules("title", "A yacht in the Bahamas"),
       "email": em.copy_problems("Guaranteed", "subject"), "source": ts.info()["keys"]["copy_rules"]["source"]}""")
        self.assertEqual((r["first"], r["again"], r["rules"]), (False, False, {"rules": []}))
        self.assertEqual((r["social"], r["ads"], r["seo"], r["email"]), ([[], []], [], [], []))
        self.assertEqual(r["source"], "local")

    def test_existing_install_keeps_its_rules_and_solo_is_unchanged(self):
        h = self.home()
        Path(h, "config.json").write_text(json.dumps({"EMAIL_PLATFORM_NAME": "Mail"}))  # an install set up before
        r = self.run_in(h, """
legacy = ts.first_run()
s0 = store.settings()
local_social = json.load(open(store.data_dir() / "social_settings.json")) if (store.data_dir() / "social_settings.json").exists() else {}
ts.save("calendar_limits", {"max_per_channel_day": 4})
out = {"legacy": legacy, "n": len(ts.get("copy_rules")["rules"]), "social": social_probs("Off to the Bahamas"),
       "social_warn": social_probs("A luxury yacht"), "ads": ads_warn("Book a cabin"), "ads_block": ads_block("Bahamas trips"),
       "seo": [i["level"] for i in seochk.text_rules("title", "A yacht")], "email": em.copy_problems("Guaranteed", "subject"),
       "slots": s0["slots"], "limits": ts.get("calendar_limits"), "joined": ts.in_team()}""")
        self.assertTrue(r["legacy"])
        self.assertGreater(r["n"], 10)
        self.assertTrue(r["social"][0] and "Bahamas" in r["social"][0][0])
        self.assertTrue(r["social_warn"][1] and "vessel" in r["social_warn"][1][0])
        self.assertTrue(any("spaces" in w for w in r["ads"]))
        self.assertTrue(any("Bahamas" in p for p in r["ads_block"]))
        self.assertEqual(r["seo"], ["warn"])
        self.assertEqual(r["email"][0]["level"], "error")
        self.assertEqual(r["slots"]["linkedin"], ["Wed 10:00"])  # SoMe defaults as before
        self.assertEqual(r["limits"]["max_per_channel_day"], 4)
        self.assertEqual(r["limits"]["min_gap_hours"], 3)
        self.assertFalse(r["joined"])

    def test_existing_install_enforces_exactly_what_it_did(self):
        """Regression (review of ts-07): the copied built-in rules match the old hardcoded checks, message for message."""
        h = self.home()
        Path(h, "config.json").write_text("{}")
        r = self.run_in(h, """
import gae, meta_ads
ts.first_run()
EM = "\u2014"
def meta(t):
    return meta_ads.check({"type": "meta_new_campaign", "name": "Camp", "objective": "OUTCOME_TRAFFIC", "primary_text": t},
                          d={"campaigns": [], "adsets": [], "ads": []}, items={})
def seo(field, t):
    return [i["msg"] for i in seochk.text_rules(field, t)]
out = {
  "g_glued": gae.lint([{"Headline 1": "VisitTheBahamas"}]), "g_digits": gae.lint([{"Description 1": "Bahamas2027"}]),
  "g_url": gae.lint([{"Final URL": "https://example.com/bahamas_trips"}]),
  "g_order": gae.lint([{"Headline 1": "Dive the reef " + EM + " now, it is far too long"}]),
  "core_glued": core.check({"type": "callout", "campaign": "C", "text": "VisitTheBahamas"})[0],
  "m_glued": meta("VisitTheBahamas")[0], "m_word": meta("Dive the Bahamas")[0], "m_em": meta("a " + EM + " b")[0],
  "seo_attr": seo("content", '<p>ok</p><img alt="a ' + EM + ' b" src="https://x/y.jpg">'),
  "seo_entity": seo("content", "<p>a &mdash; b</p>"),
  "seo_emoji": {c: seo("title", "x " + c) for c in ("\u2934", "\u2935", "\u3030", "\u303d", "\u2b06", "\u200d", "\u2b50")},
  "seo_bahamas": seo("title", "Dive the Bahamas"),
  "g_own": core._warn([{"Headline 1": "We own the boats"}]), "m_own": meta("We own the boats")[1],
  "social": social_probs("Off to the Bahamas \U0001F420 with expeditions"),
  "email": em.copy_problems("Spring " + EM + " dates \U0001F420", "subject"),
}""")
        ban = "mentions Bahamas (ML ban)"
        # M1: Google Ads blocks "bahamas" anywhere, Meta only as a word
        self.assertEqual(r["g_glued"], [f"row 1 Headline 1: {ban}"])
        self.assertEqual(r["g_digits"], [f"row 1 Description 1: {ban}"])
        self.assertEqual(r["g_url"], [f"row 1 Final URL: {ban}"])
        self.assertEqual(r["core_glued"], [ban])
        self.assertEqual(r["m_glued"], [])
        self.assertEqual(r["m_word"], ["primary_text: never mention the Bahamas"])
        self.assertEqual(r["m_em"], ["primary_text: em dash - use a normal dash"])
        self.assertEqual(r["g_order"][0], "row 1 Headline 1: contains an em dash")  # before the length limit, as before
        self.assertIn("chars > 30", r["g_order"][1])
        # M2: SEO em dash on the raw field (alt text counts), &mdash; does not
        self.assertEqual(r["seo_attr"], ["Has an em dash. Use a regular dash or rewrite."])
        self.assertEqual(r["seo_entity"], [])
        # M3: the old SEO emoji class exactly
        flagged = {c: bool(v) for c, v in r["seo_emoji"].items()}
        self.assertEqual(flagged, {"\u2934": True, "\u2935": True, "\u3030": True, "\u303d": True, "\u2b06": False,
                                   "\u200d": False, "\u2b50": True})
        self.assertEqual(r["seo_emoji"]["\u2934"], ["Has an emoji. ML copy uses none."])
        self.assertEqual(r["seo_bahamas"], ['Never use "Bahamas" in ML copy. Found: "Bahamas".'])
        # LOW: the ownership warning stays Meta (and SoMe) only
        self.assertEqual(r["g_own"], [])
        self.assertEqual(r["m_own"], ["primary_text: no ownership claims about vessels"])
        self.assertEqual(r["social"], [["caption: never mention the Bahamas", "caption: no emojis in ML posts"],
                                       ['caption: no "Expeditions" in ML copy']])
        self.assertEqual([p["message"] for p in r["email"]], ["No emojis (subject).", "No em dash; use a dash (-) or rewrite (subject)."])

    def test_settings_are_checked(self):
        h = self.home()
        r = self.run_in(h, """
errs = []
for key, v in (("copy_rules", {"rules": [{"pattern": "(", "level": "warn"}]}), ("copy_rules", {"rules": [{"pattern": "x", "level": "nope"}]}),
               ("copy_rules", {"rules": [{"pattern": "x", "areas": ["tv"]}]}), ("calendar_limits", {"max_per_channel_day": 0}),
               ("email_test_addresses", {"addresses": ["not an address"]}), ("seo_sites", {"prod": "ftp://x"}),
               ("social", {"slots": {"facebook": ["Someday"]}}), ("nope", {})):
    try:
        ts.save(key, v)
        errs.append(None)
    except ValueError as e:
        errs.append(str(e))
try:
    ts.save("copy_rules", {"rules": []}, scope="team")
    team_err = None
except ValueError as e:
    team_err = str(e)
ts.save("email_test_addresses", {"addresses": ["A@Example.com", "b@example.com"]})
out = {"errs": errs, "team_err": team_err, "cfg": appconfig.get("STUDIO_TEST_EMAIL"), "em": em.TEST_ADDRESSES}""")
        self.assertTrue(all(r["errs"]), r["errs"])
        self.assertIn("Join a team", r["team_err"])
        self.assertEqual(r["cfg"], "a@example.com, b@example.com")
        self.assertEqual(r["em"], ["a@example.com", "b@example.com"])

    # ---------- the team ----------

    def test_admin_rule_reaches_the_other_member(self):
        a, an = self.member("Ada", "admin")
        b, _ = self.member("Ben")
        try:
            set_ = self.put_team(a, "copy_rules", {"rules": [{"id": "cheap", "level": "block", "pattern": r"\bcheap\b",
                                                             "message": "say good value, not cheap", "areas": []}]})
            self.assertEqual((set_["source"], set_["team"]["updated_by"]), ("team", an))
            r = self.run_in(b, """
before = social_probs("Cheap trips")
team.sync()
info = ts.info()["keys"]["copy_rules"]
try:
    ts.save("copy_rules", {"rules": []})
    err = None
except ValueError as e:
    err = str(e)
out = {"before": before, "social": social_probs("Cheap trips"), "ads": ads_block("Cheap trips"),
       "seo": seochk.text_rules("title", "Cheap trips"), "email": em.copy_problems("Cheap trips", "subject"),
       "source": info["source"], "editable": info["editable"], "by": info["team"]["updated_by"], "err": err}""")
            self.assertEqual(r["before"], [[], []])  # not synced yet: this Mac's own (none)
            self.assertEqual(r["social"][0], ["caption: say good value, not cheap"])
            self.assertTrue(any("good value" in p for p in r["ads"]))
            self.assertEqual(r["seo"][0]["level"], "error")
            self.assertEqual(r["email"][0]["level"], "error")
            self.assertEqual((r["source"], r["editable"], r["by"]), ("team", False, an))
            self.assertIn("Set by your team", r["err"])
            # an editor can't change the team's value; the admin clears it and everyone is back to their own
            r = self.run_in(b, """
try:
    ts.save("copy_rules", {"rules": []}, scope="team")
    out = None
except ValueError as e:
    out = str(e)""")
            self.assertIn("owner or admin", r)
        finally:
            self.clear_team(a, "copy_rules")
        r = self.run_in(b, """
team._on_event("settings", '{"key": "copy_rules", "version": 9}')  # the SSE event refreshes the cache
out = {"social": social_probs("Cheap trips"), "source": ts.info()["keys"]["copy_rules"]["source"]}""")
        self.assertEqual((r["social"], r["source"]), ([[], []], "local"))

    def test_team_social_and_email_settings_override_and_lock(self):
        a, _ = self.member("Adi", "admin")
        b, _ = self.member("Bo")
        try:
            self.put_team(a, "social", {"late_minutes": 15, "slots": {"facebook": ["Mon 08:00"]}})
            self.put_team(a, "email_test_addresses", {"addresses": ["team@example.com"]})
            r = self.run_in(b, """
from areas import social
appconfig.set_many({"STUDIO_TEST_EMAIL": "me@example.com"})
team.sync()
s = store.settings()
try:
    social.save_settings({"late_minutes": 30})
    err = None
except ValueError as e:
    err = str(e)
social.save_settings({"local_scheduler": True})  # a field the team did not set: still this Mac's own
out = {"late": s["late_minutes"], "fb": s["slots"]["facebook"], "li": s["slots"]["linkedin"], "err": err,
       "local": store.settings()["local_scheduler"], "own": store.settings(team=False)["late_minutes"],
       "tests": em.TEST_ADDRESSES, "fields": social.get_state({})["team_fields"]}""")
            self.assertEqual((r["late"], r["fb"], r["li"]), (15, ["Mon 08:00"], ["Wed 10:00"]))
            self.assertIn("Set by your team", r["err"])
            self.assertTrue(r["local"])
            self.assertEqual(r["own"], 0)
            self.assertEqual(r["tests"], ["team@example.com"])
            self.assertEqual(r["fields"], ["late_minutes", "slots"])
        finally:
            self.clear_team(a, "social")
            self.clear_team(a, "email_test_addresses")
        r = self.run_in(b, "team.sync(); out = {'late': store.settings()['late_minutes'], 'tests': em.TEST_ADDRESSES}")
        self.assertEqual(r, {"late": 0, "tests": ["me@example.com"]})

    def test_library_syncs_and_viewers_cannot_add(self):
        a, an = self.member("Lia")
        b, _ = self.member("Lou")
        v, _ = self.member("Vic", "viewer")
        r = self.run_in(a, """
x = ts.library_add({"kind": "headline", "text": "Seven nights, one ocean", "label": "Winter"})
y = ts.library_add({"kind": "utm", "text": "?utm_source=news&utm_medium=email&utm_campaign=winter"})
bad = []
for item in ({"kind": "image", "text": "http://insecure.example/x.jpg"}, {"kind": "utm", "text": "source=x"}, {"kind": "nope", "text": "x"}):
    try:
        ts.library_add(item)
    except ValueError as e:
        bad.append(str(e))
team.sync()
out = {"x": x["id"], "y": y["text"], "bad": len(bad)}""")
        self.assertEqual(r["bad"], 3)
        self.assertEqual(r["y"], "utm_source=news&utm_medium=email&utm_campaign=winter")
        got = self.run_in(b, "team.sync(); out = ts.library_list()")
        mine = [i for i in got["items"] if i["id"] == r["x"]]
        self.assertEqual(len(mine), 1)
        self.assertEqual((mine[0]["text"], mine[0]["by"]), ("Seven nights, one ocean", an))
        self.assertEqual(len(self.run_in(b, "out = ts.library_list('utm')")["items"]) >= 1, True)
        got = self.run_in(v, """
team.sync()
try:
    ts.library_add({"kind": "cta", "text": "Book now"})
    err = None
except ValueError as e:
    err = str(e)
out = {"n": len(ts.library_list()["items"]), "can_add": ts.library_list()["can_add"], "err": err}""")
        self.assertGreaterEqual(got["n"], 2)
        self.assertFalse(got["can_add"])
        self.assertIn("Viewers", got["err"])
        # delete reaches the others too
        self.run_in(a, f"ts.library_delete({r['x']!r}); team.sync(); out = 1")
        got = self.run_in(b, "team.sync(); out = [i['id'] for i in ts.library_list()['items']]")
        self.assertNotIn(r["x"], got)

    def test_clash_warnings_name_who_scheduled(self):
        a, an = self.member("Cleo")
        b, bn = self.member("Cal")
        r = self.run_in(a, """
w = dt.datetime(2030, 3, 5, 9, 0, tzinfo=dt.timezone.utc)
p1 = store.save({"title": "Manta season", "channels": ["facebook"], "when": w.isoformat(), "caption": "Mantas",
                 "link": "https://example.com/mantas/?utm_source=facebook", "media": [{"media_id": "img1", "kind": "image"}]})
p2 = store.save({"title": "Reef report", "channels": ["facebook", "instagram"], "when": (w + dt.timedelta(hours=6)).isoformat(),
                 "caption": "Reefs", "media": [{"media_id": "img9", "kind": "image"}]})
c3 = [x for x in em.adapter().list_campaigns() if x["id"] == "demo-3"][0]
cal.mark_email(c3, c3["scheduled_for"])
team.sync()
out = {"p1": p1["id"]}""")
        r = self.run_in(b, f"""
team.sync()
w = dt.datetime(2030, 3, 5, 9, 0, tzinfo=dt.timezone.utc)
cand = store.save({{"title": "Manta Season!", "channels": ["facebook"], "when": (w + dt.timedelta(hours=1)).isoformat(),
                   "caption": "Mantas again", "link": "https://www.example.com/mantas", "media": [{{"media_id": "img1", "kind": "image"}}]}})
soc = cal.social_clashes(cand)
far = store.save({{"title": "Something else", "channels": ["linkedin"], "when": (w + dt.timedelta(days=20)).isoformat(), "caption": "x"}})
c3 = [x for x in em.adapter().list_campaigns() if x["id"] == "demo-3"][0]
when = cal.parse(c3["scheduled_for"]) - dt.timedelta(days=1)
mail = cal.email_clashes({{"id": "new-1", "subject": "Another offer", "list_id": "l1"}}, when)
other_list = cal.email_clashes({{"id": "new-2", "subject": "Another offer", "list_id": "l2"}}, when)
ts.save("calendar_limits", {{"same_link_days": 0, "min_gap_hours": 0.5}})
relaxed = sorted({{x["rule"] for x in cal.social_clashes(cand)}})
cl = cal.calendar_list(w.date().isoformat(), (w + dt.timedelta(days=2)).date().isoformat())
out = {{"soc": soc, "far": cal.social_clashes(far), "mail": mail, "other_list": other_list, "relaxed": relaxed,
       "owners": sorted({{e["owner"]["name"] for e in cl["entries"]}}), "n": len(cl["entries"])}}""")
        rules = {x["rule"] for x in r["soc"]}
        self.assertEqual(rules, {"per_day", "gap", "link", "image", "topic"})
        gap = next(x for x in r["soc"] if x["rule"] == "gap")
        self.assertIn(an, gap["message"])  # who scheduled the other one
        self.assertEqual(gap["other"]["owner"]["name"], an)
        self.assertIn("Manta season", gap["message"])
        self.assertEqual(r["far"], [])
        aud = [x for x in r["mail"] if x["rule"] == "audience"]
        self.assertEqual(len(aud), 1)
        self.assertIn(an, aud[0]["message"])  # from the synced calendar mark
        self.assertEqual([x for x in r["other_list"] if x["rule"] == "audience"], [])
        self.assertEqual(r["relaxed"], ["image", "per_day", "topic"])
        self.assertEqual(r["owners"], sorted({an, bn}))
        self.assertEqual(r["n"], 4)  # Manta (fb), Reef (fb + ig), the candidate (fb)

    def test_scheduling_returns_clashes_and_marks_email(self):
        a, an = self.member("Sam")
        r = self.run_in(a, """
from areas import email as em2
c = [x for x in em.adapter().list_campaigns() if x["id"] == "demo-4"][0]
when = cal.parse([x for x in em.adapter().list_campaigns() if x["id"] == "demo-3"][0]["scheduled_for"]) + dt.timedelta(hours=1)
em.FROM_ADDRESS = ""
em._fetch_status = lambda url, timeout=8: (200, "")
em.add_draft({"campaign_id": "demo-4", "fields": {"list_id": "l1", "segment_id": None}})
res = em.p_schedule({"id": "demo-4", "confirm": em.CONFIRM, "when": when.astimezone(em.LONDON).strftime("%Y-%m-%dT%H:%M")})
marks = [r["id"] for r in draftstore.items("calendar", ROOT)]
em.p_cancel({"id": "demo-4", "confirm": em.CONFIRM})
out = {"ok": res.get("ok"), "msg": res.get("message"), "rules": sorted({x["rule"] for x in res.get("clashes") or []}), "marks": marks,
       "after": [r["id"] for r in draftstore.items("calendar", ROOT)]}""")
        self.assertTrue(r["ok"], r["msg"])
        self.assertEqual(r["rules"], ["audience", "gap"])
        self.assertEqual(r["marks"], ["email_demo-4"])
        self.assertEqual(r["after"], [])


if __name__ == "__main__":
    unittest.main()
