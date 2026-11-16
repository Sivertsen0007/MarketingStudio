"""Team sync tests (engine/team.py) against the real team server.

Run from the engine folder:  python3 -m unittest tests/test_team.py
Starts `python3 -m teamserver serve` on a free port in a temp folder, makes invites with its CLI, and runs each Studio
in its own subprocess with its own MSTUDIO_HOME (modules read their paths at import). Never touches the real data
folder or the Keychain (MSTUDIO_KEYCHAIN=0).
"""
import json
import os
import socket
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
import json, sys, time
sys.path.insert(0, ENGINE_DIR)
import appconfig, core, draftstore, team
from areas import email as em
ROOT = core.STUDIO_DIR
def seo_put(iid, data):
    with draftstore.tx("seo:changes", ROOT, by="you") as t:
        t.put(iid, {"id": iid, **data}, "you")
def seo_get(iid):
    return draftstore.peek("seo:changes", iid, ROOT)[0]
def seo_delete(iid):
    with draftstore.tx("seo:changes", ROOT) as t:
        t.delete(iid)
def fake_join(role="editor", colour="#112233"):
    appconfig.set_many({"TEAM_SERVER_URL": "http://127.0.0.1:9"})
    appconfig.set_secret("TEAM_DEVICE_TOKEN", "t")
    team._save("state.json", {"member": {"id": 7, "name": "Me", "role": role, "colour": colour}, "team": "T", "cursor": 0})
def fake_api(pages, fail_post=None, health=None):
    calls = []
    def api(method, path, body=None, timeout=None):
        if path == "/api/v1/health":
            return health or {"ok": True, "protocol": team.PROTOCOL, "version": "9.9.9", "team": "T"}
        calls.append((method, path))
        if method == "POST":
            if fail_post:
                raise fail_post
            return {"results": [{"ok": True, "version": 1, "coll": o["coll"], "id": o["id"]} for o in body["ops"]]}
        if path.startswith("/api/v1/members"):
            return {"members": []}
        since = int(path.split("since=")[1].split("&")[0])
        return pages[since]
    team._api = api
    return calls
def item(coll, iid, v, data, deleted=False):
    return {"coll": coll, "id": iid, "version": v, "data": data, "deleted": deleted, "author": 1, "updated_by": 1,
            "updated_at": "2026-11-15T10:00:00Z", "changeset": None}
out = None
"""


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TeamSync(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="mstudio-team-")
        cls.data = os.path.join(cls.tmp.name, "server")
        cls.port = free_port()
        cls.url = f"http://127.0.0.1:{cls.port}"
        cls.n = 0
        cls.owner_link = cls.cli("init", "--email", "owner@example.test", "--name", "Olive", "--team", "Test team",
                                 "--public-url", cls.url)
        # the real server; only its join rate limit (10 per address per minute) is raised: every test joins new members
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
        h = tempfile.mkdtemp(dir=self.tmp.name, prefix="home-")
        return h

    def run_in(self, home, code, timeout=60):
        env = {**os.environ, "MSTUDIO_HOME": home, "MSTUDIO_KEYCHAIN": "0",
               "STUDIO_DATA_DIR": os.path.join(home, "dp")}
        for k in ("SEO_STUDIO_DIR", "SOCIAL_DIR", "TEAM_SERVER_URL", "TEAM_DEVICE_TOKEN"):
            env.pop(k, None)
        src = f"ENGINE_DIR = {str(ENGINE)!r}\n" + PRELUDE + textwrap.dedent(code) + "\nprint('@@' + json.dumps(out, default=str))\n"
        r = subprocess.run([sys.executable, "-c", src], cwd=ENGINE, env=env, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            self.fail(f"client failed:\n{r.stderr}")
        line = next(ln for ln in reversed(r.stdout.splitlines()) if ln.startswith("@@"))
        return json.loads(line[2:])

    def member(self, name, role="editor"):
        """A fresh Studio home joined to the team as a new member."""
        TeamSync.n += 1
        link = self.cli("invite", "--email", f"{name.lower()}{self.n}@example.test", "--role", role, "--name", f"{name}{self.n}")
        h = self.home()
        st = self.run_in(h, f"out = team.join({link!r}, device_name='test mac')")
        self.assertTrue(st["joined"], st)
        return h, st

    def sync(self, h):
        r = self.run_in(h, "out = team.sync()")
        self.assertTrue(r["ok"], r)
        return r

    # ---------- tests ----------

    def test_parse_link(self):
        import importlib
        sys.path.insert(0, str(ENGINE))
        team = importlib.import_module("team")
        self.assertEqual(team.parse_link("marketingstudio://join?server=https%3A%2F%2Fteam.example.test&invite=abc"),
                         ("https://team.example.test", "abc"))
        self.assertEqual(team.parse_link("https://team.example.test/join#tok"), ("https://team.example.test", "tok"))
        self.assertEqual(team.parse_link("https://team.example.test/"), ("https://team.example.test", ""))
        m, cf = team.merge3({"a": 1, "b": 1, "problems": []}, {"a": 2, "b": 1, "problems": ["x"]}, {"a": 1, "b": 3, "problems": []})
        self.assertEqual((m, cf), ({"a": 2, "b": 3, "problems": ["x"]}, []))
        m, cf = team.merge3({"fields": {"s": 1, "h": 1}, "at": "1"}, {"fields": {"s": 2, "h": 1}, "at": "2"},
                            {"fields": {"s": 1, "h": 3}, "at": "3"})
        self.assertEqual((m, cf), ({"fields": {"s": 2, "h": 3}, "at": "3"}, []))
        _, cf = team.merge3({"t": 1}, {"t": 2}, {"t": 3})
        self.assertEqual(cf[0][0], ("t",))

    def test_join_and_status(self):
        h, st = self.member("Bea")
        self.assertEqual(st["team"], "Test team")
        self.assertEqual(st["me"]["role"], "editor")
        self.assertTrue(st["me"]["colour"].startswith("#"))
        self.assertEqual(st["changeset"]["name"], "Untitled changes")
        self.assertFalse(st["auto_sync"])
        cfg = json.loads(Path(h, "config.json").read_text())
        self.assertEqual(cfg["TEAM_SERVER_URL"], self.url)
        self.assertNotIn("TEAM_DEVICE_TOKEN", cfg)  # the token is a secret
        self.assertIn("TEAM_DEVICE_TOKEN", json.loads(Path(h, "secrets.json").read_text()))
        again = self.run_in(h, f"""
try:
    team.join({self.url!r}, invite="x")
    out = "joined twice"
except ValueError as e:
    out = str(e)""")
        self.assertIn("already in a team", again)

    def test_edits_reach_the_other_member(self):
        a, sa = self.member("Ann")
        b, _ = self.member("Ben")
        r = self.run_in(a, """
items = core.add("111", {"type": "campaign_status", "campaign": "X", "status": "paused"}, by="you")
em.add_draft({"campaign_id": "42", "fields": {"subject": "Spring dates"}})
seo_put("c1", {"url": "/a", "title": "Old"})
before = len(draftstore.outbox(ROOT))
s = team.sync()
out = {"ads": items[0]["id"], "before": before, "sync": s, "after": draftstore.outbox(ROOT)}""")
        self.assertEqual(r["before"], 3)
        self.assertEqual(r["sync"]["sent"], 3)
        self.assertEqual(r["after"], [])  # outbox trimmed
        self.assertEqual(r["sync"]["status"]["pending_out"], 0)
        got = self.run_in(b, f"""
s = team.sync()
recs = draftstore.items("ads:111", ROOT)
out = {{"sync": s, "ads": core.draft("111")["items"], "email": em.list_drafts(), "seo": seo_get("c1"),
       "by": recs[0]["updated_by"], "outbox": draftstore.outbox(ROOT)}}""")
        self.assertGreaterEqual(got["sync"]["received"], 3)  # the server is shared by every test in this class
        self.assertEqual(got["ads"][0]["id"], r["ads"])
        self.assertEqual(next(d for d in got["email"] if d["campaign_id"] == "42")["fields"]["subject"], "Spring dates")
        self.assertEqual(got["seo"]["title"], "Old")
        self.assertEqual(got["by"], sa["me"]["name"])  # readable name, not a member id
        self.assertEqual(got["outbox"], [])  # remote items are not sent back

    def test_validate_only_changes_are_not_sent(self):
        a, _ = self.member("Val")
        r = self.run_in(a, """
core.add("222", {"type": "campaign_status", "campaign": "Y", "status": "paused"})
team.sync()
iid = core.draft("222")["items"][0]["id"]
with draftstore.tx("ads:222", ROOT) as t:
    d = t.get(iid)["data"]; d["problems"] = ["rewritten by validate"]; d["warnings"] = ["w"]
p = team.pending_count()
s = team.sync()
out = {"pending": p, "sent": s["sent"], "outbox": draftstore.outbox(ROOT)}""")
        self.assertEqual(r["pending"], 0)
        self.assertEqual(r["sent"], 0)
        self.assertEqual(r["outbox"], [])

    def test_different_fields_auto_merge(self):
        a, _ = self.member("Amy")
        b, _ = self.member("Bob")
        self.run_in(a, 'seo_put("m1", {"url": "/m", "title": "T0", "meta": "M0"}); team.sync()')
        self.sync(b)
        self.run_in(a, 'seo_put("m1", {"url": "/m", "title": "T-amy", "meta": "M0"}); out = team.sync()')
        rb = self.run_in(b, 'seo_put("m1", {"url": "/m", "title": "T0", "meta": "M-bob"}); s = team.sync(); out = {"s": s, "v": seo_get("m1")}')
        self.assertEqual(rb["s"]["conflicts"], 0, rb)
        self.assertEqual(rb["v"], {"id": "m1", "url": "/m", "title": "T-amy", "meta": "M-bob"})
        ra = self.run_in(a, 'team.sync(); out = seo_get("m1")')
        self.assertEqual(ra, {"id": "m1", "url": "/m", "title": "T-amy", "meta": "M-bob"})
        # email drafts: different fields inside "fields" merge too, even though "at" changed on both sides
        self.run_in(a, 'em.add_draft({"campaign_id": "9", "fields": {"subject": "S0", "preview_text": "P0"}}); team.sync()')
        self.sync(b)
        self.run_in(a, 'em.add_draft({"campaign_id": "9", "fields": {"subject": "S-amy"}}); team.sync()')
        rb = self.run_in(b, 'em.add_draft({"campaign_id": "9", "fields": {"preview_text": "P-bob"}}); s = team.sync(); out = {"s": s, "d": em.list_drafts()}')
        self.assertEqual(rb["s"]["conflicts"], 0)
        self.assertEqual(next(d for d in rb["d"] if d["campaign_id"] == "9")["fields"], {"subject": "S-amy", "preview_text": "P-bob"})

    def test_same_field_conflict_keep_mine_and_take_theirs(self):
        a, sa = self.member("Ava")
        b, _ = self.member("Bo")
        self.run_in(a, 'seo_put("k1", {"title": "T0"}); seo_put("k2", {"title": "U0"}); team.sync()')
        self.sync(b)
        self.run_in(a, 'seo_put("k1", {"title": "T-ava"}); seo_put("k2", {"title": "U-ava"}); team.sync()')
        rb = self.run_in(b, """
seo_put("k1", {"title": "T-bo"}); seo_put("k2", {"title": "U-bo"})
s = team.sync()
out = {"s": s, "c": team.conflicts(), "k1": seo_get("k1"), "status": team.status()}""")
        self.assertEqual(rb["s"]["conflicts"], 2)
        self.assertEqual(rb["status"]["conflicts"], 2)
        c = {x["id"]: x for x in rb["c"]["conflicts"]}
        self.assertEqual((c["k1"]["field"], c["k1"]["base"], c["k1"]["theirs"], c["k1"]["mine"]), ("title", "T0", "T-ava", "T-bo"))
        self.assertEqual(c["k1"]["who"], sa["me"]["name"])
        self.assertEqual(rb["k1"]["title"], "T-bo")  # local stays mine until resolved
        # A syncs again: still sees its own values (B's conflicting edits were not pushed)
        self.assertEqual(self.run_in(a, 'team.sync(); out = seo_get("k1")')["title"], "T-ava")
        rb = self.run_in(b, """
r1 = team.resolve("seo:changes", "k1", "title", "mine")
r2 = team.resolve("seo:changes", "k2", "title", "theirs")
out = {"r1": r1, "r2": r2, "k1": seo_get("k1"), "k2": seo_get("k2"), "status": team.status()}""")
        self.assertTrue(rb["r1"]["resolved"])
        self.assertEqual(rb["k1"]["title"], "T-bo")
        self.assertEqual(rb["k2"]["title"], "U-ava")
        self.assertEqual(rb["status"]["conflicts"], 0)
        self.assertEqual(rb["status"]["pending_out"], 0)
        ra = self.run_in(a, 'team.sync(); out = [seo_get("k1"), seo_get("k2")]')
        self.assertEqual([ra[0]["title"], ra[1]["title"]], ["T-bo", "U-ava"])
        # custom value
        self.run_in(a, 'seo_put("k3", {"title": "V0"}); team.sync()')
        self.sync(b)
        self.run_in(a, 'seo_put("k3", {"title": "V-ava"}); team.sync()')
        rb = self.run_in(b, 'seo_put("k3", {"title": "V-bo"}); team.sync(); team.resolve("seo:changes", "k3", "title", "custom", "V-both"); out = seo_get("k3")')
        self.assertEqual(rb["title"], "V-both")
        self.assertEqual(self.run_in(a, 'team.sync(); out = seo_get("k3")')["title"], "V-both")

    def test_remote_delete_vs_local_edit(self):
        a, _ = self.member("Al")
        b, _ = self.member("Bi")
        self.run_in(a, 'seo_put("d1", {"title": "D0"}); seo_put("d2", {"title": "E0"}); team.sync()')
        self.sync(b)
        self.run_in(a, 'seo_delete("d1"); seo_delete("d2"); team.sync()')
        rb = self.run_in(b, """
seo_put("d1", {"title": "D-bi"}); seo_put("d2", {"title": "E-bi"})
s = team.sync()
out = {"s": s, "c": team.conflicts(), "d1": seo_get("d1")}""")
        self.assertEqual(rb["s"]["conflicts"], 2)
        c = {x["id"]: x for x in rb["c"]["conflicts"]}
        self.assertEqual(c["d1"]["field"], "*")
        self.assertTrue(c["d1"]["theirs_missing"])
        self.assertEqual(rb["d1"]["title"], "D-bi")
        rb = self.run_in(b, """
team.resolve("seo:changes", "d1", "*", "theirs")
team.resolve("seo:changes", "d2", "*", "mine")
out = {"d1": seo_get("d1"), "d2": seo_get("d2"), "status": team.status()}""")
        self.assertIsNone(rb["d1"])
        self.assertEqual(rb["d2"]["title"], "E-bi")
        self.assertEqual(rb["status"]["conflicts"], 0)
        ra = self.run_in(a, 'team.sync(); out = [seo_get("d1"), seo_get("d2")]')
        self.assertIsNone(ra[0])
        self.assertEqual(ra[1]["title"], "E-bi")  # kept by B: back on the server

    def test_changeset_tagged_on_ops(self):
        a, _ = self.member("Cal")
        r = self.run_in(a, """
cs = team.changeset_get()["active"]
team.changeset_rename(cs["id"], "Spring copy")
seo_put("s1", {"title": "x"})
team.changeset_set(name="Summer")
seo_put("s2", {"title": "y"})
team.sync()
items = team._api("GET", "/api/v1/items?since=0&limit=5000")["items"]
out = {i["id"]: i["changeset"] for i in items if i["coll"] == "seo:changes" and i["id"] in ("s1", "s2")}""")
        self.assertEqual(r["s1"]["name"], "Spring copy")
        self.assertEqual(r["s2"]["name"], "Summer")
        self.assertNotEqual(r["s1"]["id"], r["s2"]["id"])

    def test_offline_outbox_waits(self):
        a, _ = self.member("Dee")
        dead = f"http://127.0.0.1:{free_port()}"
        r = self.run_in(a, f"""
appconfig.set_many({{"TEAM_SERVER_URL": {dead!r}}})
seo_put("o1", {{"title": "offline"}})
s = team.sync()
p = team.status()["pending_out"]
appconfig.set_many({{"TEAM_SERVER_URL": {self.url!r}}})
s2 = team.sync()
out = {{"s": s, "p": p, "s2": s2, "box": draftstore.outbox(ROOT)}}""")
        self.assertFalse(r["s"]["ok"])
        self.assertTrue(r["s"]["offline"])
        self.assertEqual(r["p"], 1)
        self.assertTrue(r["s2"]["ok"])
        self.assertEqual(r["s2"]["sent"], 1)
        self.assertEqual(r["box"], [])

    def test_live_stream_counts_remote_changes(self):
        a, _ = self.member("Eve")
        b, _ = self.member("Fay")
        self.sync(b)  # catch up on the other tests' items first
        token_a = json.loads(Path(a, "secrets.json").read_text())["TEAM_DEVICE_TOKEN"]
        r = self.run_in(b, f"""
import urllib.request
team.start_listener()
for _ in range(50):
    if team.status()["online"]: break
    time.sleep(0.1)
req = urllib.request.Request({self.url!r} + "/api/v1/items", method="POST", data=json.dumps({{"ops": [
    {{"op": "put", "coll": "seo:changes", "id": "live1", "base_version": 0, "data": {{"id": "live1", "title": "L"}}}}]}}).encode(),
    headers={{"Content-Type": "application/json", "Authorization": "Bearer " + {token_a!r}}})
urllib.request.urlopen(req, timeout=10).read()
n = 0
for _ in range(100):
    n = team.status()["remote_waiting"]
    if n: break
    time.sleep(0.1)
before = seo_get("live1")
team.settings(auto_sync=True)
for _ in range(100):
    if seo_get("live1"): break
    time.sleep(0.1)
out = {{"waiting": n, "before": before, "after": seo_get("live1"), "status": team.status()}}""")
        self.assertEqual(r["waiting"], 1)
        self.assertIsNone(r["before"])  # not applied until a sync (Auto-sync off)
        self.assertEqual(r["after"]["title"], "L")  # Auto-sync on: applied
        self.assertTrue(r["status"]["auto_sync"])

    def test_revoked_device_shows_removed(self):
        b, sb = self.member("Gus")
        dev = sb["device"]["id"]
        r = self.run_in(b, f"""
import subprocess
team.start_listener()
for _ in range(50):
    if team.status()["online"]: break
    time.sleep(0.1)
subprocess.run([sys.executable, "-m", "teamserver", "revoke", "{dev}", "--data", {self.data!r}], cwd={str(REPO)!r}, check=True, capture_output=True)
for _ in range(150):
    if team.status()["removed"]: break
    time.sleep(0.1)
via_stream = team.status()["removed"]
s = team.sync()
out = {{"via_stream": via_stream, "sync": s, "status": team.status()}}""")
        self.assertTrue(r["via_stream"])
        self.assertFalse(r["sync"]["ok"])
        self.assertTrue(r["status"]["removed"])
        left = self.run_in(b, 'seo_put("keep", {"title": "k"}); out = {"st": team.leave(), "keep": seo_get("keep")}')
        self.assertFalse(left["st"]["joined"])
        self.assertEqual(left["keep"]["title"], "k")  # local drafts stay

    def test_leave_revokes_and_keeps_drafts(self):
        a, _ = self.member("Hal")
        r = self.run_in(a, 'seo_put("h1", {"title": "h"}); team.sync(); out = {"st": team.leave(), "h1": seo_get("h1")}')
        self.assertFalse(r["st"]["joined"])
        self.assertEqual(r["h1"]["title"], "h")
        cfg = json.loads(Path(a, "config.json").read_text())
        self.assertNotIn("TEAM_SERVER_URL", cfg)
        self.assertNotIn("TEAM_DEVICE_TOKEN", json.loads(Path(a, "secrets.json").read_text()))
        out = self.cli("members")
        self.assertIn("REVOKED", out)

    # ---------- sync error handling (review fixes) ----------

    def test_push_failures_do_not_block_the_pull(self):
        a, _ = self.member("Ivy")
        b, _ = self.member("Jon")
        self.sync(a)
        self.run_in(b, 'seo_put("p-from-b", {"title": "from b"}); team.sync()')
        r = self.run_in(a, """
seo_put("p-big", {"title": "x" * 600000})
seo_put("p-nan", {"title": "n", "score": float("nan")})
seo_put("p-ok", {"title": "fine"})
s = team.sync()
s2 = team.sync()
st = team.status()
seo_put("p-big", {"title": "small now"})
s3 = team.sync()
out = {"s": s, "s2": s2, "st": st, "s3": s3, "got": seo_get("p-from-b"), "box": draftstore.outbox(ROOT)}""")
        self.assertTrue(r["s"]["ok"], r["s"])
        self.assertFalse(r["s"]["offline"])
        self.assertEqual(r["s"]["sent"], 1)
        self.assertEqual(r["s"]["failed"], 2)
        self.assertEqual(r["got"]["title"], "from b")  # the pull ran
        reasons = {f["id"]: f["reason"] for f in r["st"]["failed"]}
        self.assertIn("too large", reasons["p-big"])
        self.assertIn("NaN", reasons["p-nan"])
        self.assertEqual((r["s2"]["sent"], r["s2"]["failed"]), (0, 0))  # not sent again until edited
        self.assertEqual(r["s3"]["sent"], 1)  # edited: sent
        self.assertEqual([e["id"] for e in r["box"]], ["p-nan"])

    def test_refused_request_is_retried_op_by_op(self):
        a, _ = self.member("Kim")
        r = self.run_in(a, """
c = team.changeset_get()
c["list"].append({"id": "7-long", "name": "n" * 150}); c["active"] = {"id": "7-long", "name": "n" * 150}
team._save("changeset.json", c)
seo_put("r-bad", {"title": "under a name the server refuses"})
team.changeset_set(name="Normal")
seo_put("r-good", {"title": "ok"})
out = team.sync()""")
        self.assertTrue(r["ok"], r)
        self.assertFalse(r["offline"])
        self.assertEqual(r["sent"], 1)
        failed = {f["id"]: f["reason"] for f in r["status"]["failed"]}
        self.assertEqual(list(failed), ["r-bad"])
        self.assertIn("too long", failed["r-bad"])

    def test_viewer_never_pushes(self):
        v, _ = self.member("Lou", role="viewer")
        r = self.run_in(v, 'seo_put("v1", {"title": "mine"}); out = team.sync()')
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["sent"], 0)
        self.assertIn("Viewers can't send changes", r["message"])
        self.assertFalse(r["status"]["can_send"])

    def test_malformed_remote_items_are_skipped(self):
        h = self.home()
        r = self.run_in(h, """
fake_join()
good = item("seo:changes", "g1", 1, {"id": "g1", "title": "good"})
pages = {0: {"items": [
    item("ads:../../evil", "x", 1, {"id": "x"}),
    item("ads:12ab", "x", 1, {"id": "x"}),
    item("seo:changes", "bad id/..", 1, {"id": "bad id/.."}),
    item("seo:changes", "m1", 1, {"id": "other"}),
    item("seo:changes", "m2", 1, ["not", "a", "dict"]),
    item("seo:changes", "m3", "1", {"id": "m3"}),
    item("nope:coll", "z", 1, {}),
    "not an object",
    good], "cursor": 9, "more": False}}
fake_api(pages)
s = team.sync()
import os
out = {"s": s, "st": team.status(), "g1": seo_get("g1"), "files": sorted(os.listdir(ROOT)),
       "parent": sorted(os.listdir(ROOT.parent)), "cursor": team._state()["cursor"]}""")
        self.assertTrue(r["s"]["ok"], r["s"])
        self.assertEqual(r["s"]["received"], 1)
        self.assertEqual(r["s"]["skipped"], 8)
        self.assertEqual(r["g1"]["title"], "good")
        self.assertEqual(r["cursor"], 9)  # kept advancing
        self.assertEqual(len(r["st"]["skipped"]), 8)
        self.assertFalse([f for f in r["files"] if "evil" in f or f.startswith("draft_")])
        self.assertNotIn("evil", " ".join(r["parent"]))

    def test_cursor_reset_resyncs_without_duplicates(self):
        h = self.home()
        r = self.run_in(h, """
fake_join()
a1 = item("seo:changes", "a1", 1, {"id": "a1", "title": "one"})
fake_api({0: {"items": [a1], "cursor": 5, "more": False}})
s1 = team.sync()
a2 = item("seo:changes", "a2", 1, {"id": "a2", "title": "two"})
calls = fake_api({5: {"items": [], "cursor": 0, "more": False, "reset": True},
                  0: {"items": [a1, a2], "cursor": 2, "more": False}})
s2 = team.sync()
out = {"s1": s1, "s2": s2, "cursor": team._state()["cursor"], "calls": calls, "a2": seo_get("a2"),
       "box": draftstore.outbox(ROOT)}""")
        self.assertEqual(r["s1"]["received"], 1)
        self.assertEqual(r["s2"]["received"], 1)  # a1 already known (same version): not applied twice
        self.assertEqual(r["a2"]["title"], "two")
        self.assertEqual(r["cursor"], 2)
        self.assertEqual(r["box"], [])

    def test_protocol_messages(self):
        r = self.run_in(self.home(), """
P = team.PROTOCOL
out = {"same": team.protocol_problem({"ok": True, "protocol": P, "version": "0.1.0"}),
       "older": team.protocol_problem({"protocol": P - 1, "version": "0.0.9"}),
       "newer": team.protocol_problem({"protocol": P + 1, "version": "2.0.0"}),
       "bad": [team.protocol_problem(b) for b in ({}, {"protocol": "1"}, {"protocol": True}, None)]}""")
        self.assertEqual(r["same"], "")
        self.assertTrue(r["older"].startswith("The team server needs an update"), r["older"])
        self.assertIn("0.0.9", r["older"])
        self.assertTrue(r["newer"].startswith("Marketing Studio needs an update"), r["newer"])
        for m in r["bad"]:
            self.assertTrue(m.startswith("The team server needs an update"), m)

    def test_server_reports_version_and_protocol(self):
        with urllib.request.urlopen(self.url + "/api/v1/health", timeout=5) as r:
            h = json.loads(r.read())
        self.assertEqual(h["protocol"], 1)
        self.assertRegex(h["version"], r"^\d+\.\d+\.\d+$")
        a, _ = self.member("Vic")
        st = self.sync(a)["status"]
        self.assertEqual(st["server_version"], h["version"])
        self.assertEqual(st["server_problem"], "")

    def test_sync_stops_on_another_protocol(self):
        h = self.home()
        r = self.run_in(h, """
fake_join()
seo_put("x1", {"title": "stays local"})
calls = fake_api({0: {"items": [item("seo:changes", "t9", 1, {"id": "t9", "title": "theirs"})], "cursor": 1, "more": False}},
                 health={"ok": True, "protocol": team.PROTOCOL + 1, "version": "2.0.0"})
s1 = team.sync()
fake_api({0: {"items": [], "cursor": 0, "more": False}}, health={"ok": True, "protocol": team.PROTOCOL - 1, "version": "0.0.1"})
s2 = team.sync()
fake_api({0: {"items": [], "cursor": 0, "more": False}})
s3 = team.sync()
out = {"s1": s1, "s2": s2, "s3": s3, "calls": calls, "t9": seo_get("t9")}""")
        self.assertFalse(r["s1"]["ok"])
        self.assertEqual([c for c in r["calls"] if c[0] == "POST" or "since=" in c[1]], [])  # nothing sent or pulled
        self.assertIsNone(r["t9"])
        self.assertTrue(r["s1"]["message"].startswith("Marketing Studio needs an update"), r["s1"]["message"])
        self.assertTrue(r["s2"]["message"].startswith("The team server needs an update"), r["s2"]["message"])
        self.assertEqual(r["s2"]["status"]["server_problem"], r["s2"]["message"])
        self.assertTrue(r["s3"]["ok"], r["s3"])  # upgraded: checked again, syncs
        self.assertEqual(r["s3"]["status"]["server_problem"], "")
        self.assertEqual(r["s3"]["sent"], 1)

    def test_join_refuses_another_protocol(self):
        r = self.run_in(self.home(), """
team._http = lambda method, path, body=None, server=None, token=None, timeout=None: {"ok": True, "protocol": 0, "version": "0.0.1"}
try:
    team.join("http://127.0.0.1:9/join#abc")
    out = "joined"
except team.TeamError as e:
    out = [str(e), e.status, team.joined()]""")
        self.assertTrue(r[0].startswith("The team server needs an update"), r)
        self.assertEqual(r[1:], [426, False])

    def test_pull_runs_when_push_fails(self):
        h = self.home()
        r = self.run_in(h, """
fake_join()
seo_put("mine1", {"title": "local"})
fake_api({0: {"items": [item("seo:changes", "t1", 1, {"id": "t1", "title": "theirs"})], "cursor": 3, "more": False}},
         fail_post=team.TeamError("server error", 500))
s = team.sync()
out = {"s": s, "t1": seo_get("t1"), "box": [e["id"] for e in draftstore.outbox(ROOT)]}""")
        self.assertFalse(r["s"]["ok"])
        self.assertFalse(r["s"]["offline"])  # a server error is not "offline"
        self.assertIn("server error", r["s"]["message"])
        self.assertEqual(r["t1"]["title"], "theirs")  # pulled anyway
        self.assertEqual(r["box"], ["mine1"])  # still waiting

    def test_newer_remote_change_keeps_answers(self):
        a, _ = self.member("Max")
        b, _ = self.member("Ned")
        self.run_in(a, 'seo_put("q1", {"title": "T0", "meta": "M0"}); team.sync()')
        self.sync(b)
        self.run_in(a, 'seo_put("q1", {"title": "T-a", "meta": "M-a"}); team.sync()')
        rb = self.run_in(b, """
seo_put("q1", {"title": "T-b", "meta": "M-b"}); team.sync()
out = team.resolve("seo:changes", "q1", "title", "mine")""")
        self.assertFalse(rb["resolved"])
        self.run_in(a, 'seo_put("q1", {"title": "T-a", "meta": "M-a2"}); team.sync()')  # only meta changes again
        rb = self.run_in(b, 'team.sync(); out = team.conflicts()["conflicts"]')
        got = {c["field"]: c for c in rb}
        self.assertEqual(got["title"]["choice"], "mine")  # still valid: kept
        self.assertIsNone(got["meta"]["choice"])
        self.assertEqual(got["meta"]["theirs"], "M-a2")
        rb = self.run_in(b, 'r = team.resolve("seo:changes", "q1", "meta", "theirs"); out = {"r": r, "v": seo_get("q1")}')
        self.assertTrue(rb["r"]["resolved"])
        self.assertEqual(rb["v"], {"id": "q1", "title": "T-b", "meta": "M-a2"})
        self.assertEqual(self.run_in(a, 'team.sync(); out = seo_get("q1")'), {"id": "q1", "title": "T-b", "meta": "M-a2"})

    def test_pull_writes_each_page_once_and_keeps_local_edits(self):
        """A pull page is written with one atomic write per collection (not one per item), and the batch still never
        overwrites a pending local edit: one made before the sync (p1) or by another writer while the page was being
        checked (c5) goes through the merge and stays."""
        h = self.home()
        r = self.run_in(h, """
fake_join()
seo_put("p1", {"title": "mine"})
page0 = [item("seo:changes", "p1", 1, {"id": "p1", "title": "theirs"})]
page0 += [item("seo:changes", "c%d" % n, 1, {"id": "c%d" % n, "title": "t%d" % n}) for n in range(300)]
page1 = [item("seo:changes", "d%d" % n, 1, {"id": "d%d" % n, "title": "u%d" % n}) for n in range(300)]
fake_api({0: {"items": page0, "cursor": 1, "more": True}, 1: {"items": page1, "cursor": 2, "more": False}},
         fail_post=team.TeamError("server error", 500))  # p1 stays pending
writes, real_save, real_many, calls = [], draftstore._save, draftstore.apply_remote_many, []
draftstore._save = lambda path, d: (writes.append(path.name), real_save(path, d))
def many(coll, entries, root=None):
    calls.append(len(entries))
    if len(calls) == 1:
        seo_put("c5", {"title": "edited meanwhile"})  # another process, after the page was checked
    return real_many(coll, entries, root)
draftstore.apply_remote_many = many
s = team.sync()
draftstore._save, draftstore.apply_remote_many = real_save, real_many
out = {"s": s, "writes": writes, "calls": calls, "p1": seo_get("p1"), "c5": seo_get("c5"), "c6": seo_get("c6"),
       "d299": seo_get("d299"), "box": sorted(e["id"] for e in draftstore.outbox(ROOT)),
       "conf": sorted(c["id"] for c in team.conflicts()["conflicts"]), "n": len(draftstore.items("seo:changes", ROOT))}""")
        self.assertEqual(r["calls"], [300, 300])  # one batch per page
        self.assertEqual(r["writes"].count("seo_draft.json"), 3)  # page 0, page 1 and the other writer's edit; not 600
        self.assertEqual(r["s"]["received"], 599)
        self.assertEqual(r["p1"]["title"], "mine")
        self.assertEqual(r["c5"]["title"], "edited meanwhile")  # never overwritten
        self.assertEqual((r["c6"]["title"], r["d299"]["title"]), ("t6", "u299"))
        self.assertEqual(r["box"], ["c5", "p1"])
        self.assertEqual(r["conf"], ["c5", "p1"])
        self.assertEqual(r["n"], 601)

    def test_colour_sanitised_and_nested_bookkeeping_conflicts(self):
        h = self.home()
        r = self.run_in(h, """
fake_join(colour='red;background:url(x)')
m, cf = team.merge3({"fields": {"at": "1"}}, {"fields": {"at": "2"}}, {"fields": {"at": "3"}})
out = {"colour": team.status()["me"]["colour"], "cf": [list(c[0]) for c in cf]}""")
        self.assertEqual(r["colour"], "#888888")
        self.assertEqual(r["cf"], [["fields", "at"]])


if __name__ == "__main__":
    unittest.main()
