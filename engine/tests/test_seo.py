"""SEO area tests. Run from the studio folder:  python3 -m unittest tests/test_seo.py

Offline: WordPress, Search Console and GA4 are replaced with fixtures. Every write goes through DryRunWriter.
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("MSTUDIO_HOME", _tf.mkdtemp(prefix="mstudio-test-"))
_os.environ["MSTUDIO_KEYCHAIN"] = "0"

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="seo-test-")
os.environ["SEO_STUDIO_DIR"] = TMP
os.environ["STUDIO_DATA_DIR"] = TMP  # datapolicy's status file, never the real one
os.environ["SEO_SECRETS_FILE"] = str(Path(TMP) / "secrets" / "marketing-studio.env")
os.environ["SEO_DRY_DELAY"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import progress  # noqa: E402
from areas import _seo_checks as chk  # noqa: E402
from areas import _seo_common as c  # noqa: E402
from areas import _seo_wp as wp  # noqa: E402
from areas import seo  # noqa: E402
import _legacy_copy_rules  # noqa: E402
import team_settings  # noqa: E402
team_settings.local_set("copy_rules", {"rules": _legacy_copy_rules.RULES})  # the rules these tests check (a new install has none)

EM = chr(0x2014)


def _raw(pid, slug, title, body, cats=(1,), desc="A fine description of this post that is long enough to pass the check."):
    return {"id": pid, "slug": slug, "status": "publish", "link": f"https://masterliveaboards.com/{slug}/", "date": "2026-01-01T10:00:00",
            "modified": "2026-02-01T10:00:00", "title": {"rendered": title}, "content": {"rendered": body}, "excerpt": {"rendered": f"<p>{title}</p>"},
            "categories": list(cats), "meta": {"footnotes": ""}, "yoast_head_json": {"title": f"{title} - Master Liveaboards", "description": desc,
                                                                                    "robots": {"index": "index"}}, "_embedded": {}}


RAW = [
    _raw(1, "sea-turtles-facts", "Six facts about sea turtles", '<p>Turtles. <a href="https://www.masterliveaboards.com/manta-rays-guide/">mantas</a> '
                                                                  '<a href="/egypt/">Egypt</a> <a href="https://example.org/x">out</a></p>'),
    _raw(2, "manta-rays-guide", "Manta rays guide", '<p>Mantas and <a href="https://masterliveaboards.com/sea-turtles-facts">turtles</a></p>'),
    _raw(3, "moray-eels-facts", "Facts about moray eels", "<p>Eels, no links.</p>", desc=None),
    _raw(4, "sea-turtles-nesting", "Sea turtles nesting season", "<p>Nesting turtles.</p>"),
]
POSTS = wp.link_graph([wp.parse_post(p) for p in RAW])
wp.posts = lambda force=False: {"at": 0, "authed": False, "posts": POSTS}  # fixture instead of the REST API


def reset():
    for name in ("seo_draft.json", "seo_history.jsonl", "seo_dryrun.jsonl", "seo_settings.json", "seo_inspect.json"):
        p = Path(TMP) / name
        if p.exists():
            p.unlink()


class Checks(unittest.TestCase):
    def iss(self, field, new, old=None, **kw):
        return chk.check_change({"field": field, "new": new, "old": old, "kind": "edit", **kw}, link_check=lambda u: 200)

    def levels(self, issues):
        return sorted(i["level"] for i in issues)

    def test_lengths(self):
        self.assertEqual(self.levels(self.iss("seo_title", "Short")), ["warn"])
        self.assertEqual(self.iss("seo_title", "Liveaboard diving in Egypt: Red Sea trips 2027"), [])
        self.assertEqual(self.levels(self.iss("meta_desc", "Too short.")), ["warn"])
        self.assertEqual(self.levels(self.iss("meta_desc", "x" * 161)), ["warn"])
        self.assertEqual(self.iss("meta_desc", "Plan a Red Sea liveaboard: routes, seasons, water temperatures in °C and what each trip covers."), [])

    def test_em_dash_and_copy_rules(self):
        self.assertIn("error", self.levels(self.iss("title", f"Red Sea {EM} North")))
        self.assertIn("error", self.levels(self.iss("excerpt", "Diving the Bahamas")))
        self.assertIn("error", self.levels(self.iss("seo_title", "Diving fun \U0001F988 in Egypt for everyone")))
        msgs = " ".join(i["msg"] for i in self.iss("content", "<p>Our yacht. You will see whale sharks. Cabins from £1,200.</p>"))
        self.assertIn("vessel", msgs)
        self.assertIn("promise", msgs)
        self.assertIn("spaces", msgs)
        self.assertEqual(self.iss("content", "<p>Our vessel visits the reef. Spaces from £1,200.</p>"), [])

    def test_rules_ignore_urls_in_body(self):
        self.assertEqual(self.iss("content", '<p><a href="https://x.org/yacht-bahamas">Read more</a></p>'), [])

    def test_slug(self):
        self.assertIn("redirect", " ".join(i["msg"] for i in self.iss("slug", "new-slug", old="old-slug")))
        self.assertIn("error", self.levels(self.iss("slug", "Bad Slug")))

    def test_links(self):
        html = '<a href="https://ok.example/">a</a><a href="/egypt/">b</a><a href="https://dead.example/">c</a><a href="mailto:x@y.z">m</a>'
        codes = {"https://ok.example/": 200, "https://www.masterliveaboards.com/egypt/": 301, "https://dead.example/": 404}
        out = chk.check_links(html, link_check=lambda u: codes[u])
        self.assertEqual(len(out), 1)
        self.assertIn("dead.example", out[0]["msg"])

    def test_todo(self):
        self.assertEqual(chk.check_change({"field": "meta_desc", "kind": "todo"})[0]["level"], "info")


class LinkGraph(unittest.TestCase):
    def test_in_out(self):
        by = {p["id"]: p for p in POSTS}
        self.assertEqual(by[1]["links_out"], 2)  # manta post + /egypt (external link ignored)
        self.assertEqual(by[1]["links_in"], 1)  # from the manta post, trailing slash and www ignored
        self.assertEqual(by[2]["links_in"], 1)
        self.assertEqual(by[3]["links_in"], 0)
        self.assertEqual(by[3]["links_out"], 0)

    def test_words_and_keys(self):
        self.assertEqual(wp.word_count("<p>One two <b>three</b></p><script>x y z</script>"), 3)
        self.assertEqual(c.url_key("https://www.masterliveaboards.com/A-b/"), "/a-b")
        self.assertEqual(c.url_key("/a-b"), "/a-b")
        self.assertEqual(c.url_key("https://other.com/a"), "")

    def test_related(self):
        rel = wp.related(POSTS[3], POSTS)  # sea turtles nesting
        self.assertEqual(rel[0][1]["id"], 1)


class Opportunities(unittest.TestCase):
    def test_rules(self):
        qp = [["sea turtle facts", "/sea-turtles-facts", 2, 900, 0.002, 4.2],   # low CTR at position 4
              ["manta rays", "/manta-rays-guide", 60, 600, 0.1, 3.5],           # healthy CTR
              ["turtles", "/sea-turtles-facts", 1, 900, 0.001, 14.0]]           # position too low to count
        cur = {"/sea-turtles-facts": {"clicks": 10, "impressions": 900, "position": 4}, "/manta-rays-guide": {"clicks": 60}}
        prev = {"/sea-turtles-facts": {"clicks": 40, "position": 3}, "/manta-rays-guide": {"clicks": 61}}
        insp = {"https://masterliveaboards.com/moray-eels-facts/": {"verdict": "NEUTRAL", "coverage": "Crawled - currently not indexed"}}
        o = seo.opportunities_from(POSTS, qp, cur, prev, insp, 28)
        self.assertEqual([r["query"] for r in o["low_ctr"]], ["sea turtle facts"])
        self.assertEqual(o["low_ctr"][0]["suggest"]["field"], "seo_title")
        self.assertEqual([r["post_id"] for r in o["losing"]], [1])
        self.assertEqual([r["post_id"] for r in o["not_indexed"]], [3])
        self.assertEqual([r["post_id"] for r in o["no_meta"]], [3])
        self.assertIn(4, [r["post_id"] for r in o["few_links"]])
        sug = next(r for r in o["few_links"] if r["post_id"] == 4)["suggest"]
        self.assertTrue(sug and sug[0]["post_id"] == 1 and "sea-turtles-nesting" in sug[0]["note"])
        self.assertEqual(o["not_inspected"], 3)

    def test_cannibalisation(self):
        qp = [["turtles", "/sea-turtles-facts", 5, 500, 0.01, 5], ["turtles", "/sea-turtles-nesting", 1, 200, 0.005, 9],
              ["turtles", "/manta-rays-guide", 0, 10, 0, 40]]
        out = seo.cannibalisation("/sea-turtles-facts", qp)
        self.assertEqual(len(out), 1)
        self.assertEqual([o["key"] for o in out[0]["others"]], ["/sea-turtles-nesting"])


class Draft(unittest.TestCase):
    def setUp(self):
        reset()

    def test_add_replace_revert(self):
        r = seo.add_draft({"post_id": 1, "field": "seo_title", "new": "Sea turtle facts: six things divers should know"}, by="claude")
        self.assertEqual(r["change"]["old"], "Six facts about sea turtles - Master Liveaboards")
        seo.add_draft({"post_id": 1, "field": "seo_title", "new": "Sea turtles: six facts for divers on a liveaboard"}, by="you")
        d = seo.list_drafts()
        self.assertEqual(len(d["changes"]), 1)
        self.assertEqual(d["changes"][0]["old"], "Six facts about sea turtles - Master Liveaboards")  # original old kept
        seo.add_draft({"post_id": 1, "field": "seo_title", "new": "Six facts about sea turtles - Master Liveaboards"}, by="you")
        self.assertEqual(seo.list_drafts()["changes"], [])  # back to the live value = no change

    def test_todo_and_remove(self):
        s = {"post_id": 3, "field": "meta_desc", "kind": "todo", "note": "Write one"}
        seo.add_draft(s, by="claude")
        self.assertTrue(seo.add_draft(s, by="claude").get("duplicate"))
        seo.add_draft({"post_id": 2, "field": "title", "new": "Manta rays: a diver's guide"})
        d = seo.list_drafts()
        self.assertEqual(len(d["changes"]), 2)
        seo.remove_drafts([d["changes"][0]["id"]])
        self.assertEqual(len(seo.list_drafts()["changes"]), 1)
        seo.clear_drafts()
        self.assertEqual(seo.list_drafts()["changes"], [])

    def test_bad_input(self):
        with self.assertRaises(ValueError):
            seo.add_draft({"post_id": 1, "field": "price", "new": "x"})
        with self.assertRaises(ValueError):
            seo.add_draft({"post_id": 1, "field": "title"})
        with self.assertRaises(KeyError):
            seo.add_draft({"post_id": 999, "field": "title", "new": "x"})


class Writers(unittest.TestCase):
    def setUp(self):
        reset()

    def test_payload_mapping(self):
        body = wp.rest_payload({"title": "T", "seo_title": "S", "meta_desc": "M", "focus_kw": "k", "categories": ["3", 4]})
        self.assertEqual(body, {"title": "T", "categories": [3, 4],
                                "meta": {"_yoast_wpseo_title": "S", "_yoast_wpseo_metadesc": "M", "_yoast_wpseo_focuskw": "k"}})

    def test_dry_run_records(self):
        w = wp.DryRunWriter("staging", delay=0)
        self.assertFalse(w.live)
        w.update(2, {"meta_desc": "New"})
        log = c.read_jsonl("seo_dryrun.jsonl")
        self.assertEqual(log[-1]["payload"], {"meta": {"_yoast_wpseo_metadesc": "New"}})
        self.assertIn("staging.masterliveaboards.com", log[-1]["url"])
        w.restore(2, {"before": {"meta_desc": "Old"}, "revision_id": "dry-1"})
        self.assertEqual(c.read_jsonl("seo_dryrun.jsonl")[-1]["action"], "restore")

    def test_wp_writer_needs_login(self):
        with self.assertRaises(RuntimeError):
            wp.WpRestWriter("staging")

    def test_default_writer_is_dry(self):
        self.assertIsInstance(seo.writer(), wp.DryRunWriter)


class Publish(unittest.TestCase):
    def setUp(self):
        reset()

    def test_flow_with_progress_and_undo(self):
        seo.add_draft({"post_id": 1, "field": "meta_desc", "new": "Six facts about sea turtles that divers ask about on our Red Sea and Maldives trips."}, by="you")
        seo.add_draft({"post_id": 2, "field": "title", "new": "Manta rays: a diver's guide"}, by="you")
        seo.add_draft({"post_id": 3, "field": "meta_desc", "kind": "todo", "note": "Write one"}, by="claude")
        pv = seo.publish_preview(link_check=lambda u: 200)
        self.assertFalse(pv["blocked"])
        self.assertEqual(pv["writer"], "dry")
        self.assertEqual(len(pv["posts"]), 2)
        self.assertEqual(pv["todos"], 1)
        with self.assertRaises(ValueError):
            seo.publish({"token": "wrong"}, link_check=lambda u: 200)
        r = seo.publish({"token": pv["token"]}, link_check=lambda u: 200)
        self.assertTrue(r["ok"], r)
        self.assertEqual([x["steps"] for x in r["results"]], [list(seo.STEPS)] * 2)
        p = progress.get(seo.PKEY)
        self.assertFalse(p["running"])
        self.assertEqual([(x["state"], x["sent"], x["total"]) for x in p["items"]], [("done", 4, 4)] * 2)
        left = seo.list_drafts()["changes"]
        self.assertEqual([x["kind"] for x in left], ["todo"])  # edits published, the suggestion stays
        sent = [x for x in c.read_jsonl("seo_dryrun.jsonl") if x["action"] == "update"]
        self.assertEqual(len(sent), 2)
        h = seo.history()
        self.assertEqual(h[0]["push_id"], r["push_id"])
        u = seo.undo({"push_id": r["push_id"]})
        self.assertTrue(u["ok"])
        self.assertIs(u["live"], False)  # a dry run: never reported to the team
        self.assertIs(r["live"], False)
        self.assertEqual({x["state"] for x in progress.get(seo.PKEY)["items"]}, {"reversed"})
        self.assertTrue(seo.history()[0]["undone"])
        with self.assertRaises(ValueError):
            seo.undo({"push_id": r["push_id"]})

    def test_errors_block(self):
        seo.add_draft({"post_id": 2, "field": "title", "new": f"Manta rays {EM} the guide"}, by="claude")
        pv = seo.publish_preview(link_check=lambda u: 200)
        self.assertTrue(pv["blocked"])
        with self.assertRaises(ValueError):
            seo.publish({"token": pv["token"]}, link_check=lambda u: 200)

    def test_conflict_fails_that_post(self):
        seo.add_draft({"post_id": 2, "field": "title", "new": "Manta rays: a diver's guide"}, by="you")
        pv = seo.publish_preview(link_check=lambda u: 200)

        class Moved(wp.DryRunWriter):
            def current(self, post_id):
                return {**super().current(post_id), "title": "Someone edited this in wp-admin"}
        r = seo.publish({"token": pv["token"]}, link_check=lambda u: 200, w=Moved("staging", delay=0))
        self.assertFalse(r["ok"])
        self.assertIn("Changed in WordPress", r["results"][0]["error"])
        self.assertEqual(progress.get(seo.PKEY)["items"][0]["state"], "failed")
        self.assertEqual(len(seo.list_drafts()["changes"]), 1)  # kept in the draft


class LiveFake(wp.DryRunWriter):
    """A writer that behaves as live (records like the dry run, never calls WordPress). rendered/modified are overridable."""
    live = True

    def __init__(self, rendered=None, modified="2026-11-10T10:00:00", raw=None):
        super().__init__("staging", delay=0)
        self.rendered, self.mod, self.raw = rendered, modified, raw

    def current(self, post_id):
        cur = super().current(post_id)
        if self.raw is not None:
            cur["content"] = self.raw
        if self.rendered is not None:
            cur["_rendered"] = self.rendered
        cur["_modified"] = self.mod
        return cur

    def update(self, post_id, fields):
        return {**super().update(post_id, fields), "modified": self.mod}


class Conflicts(unittest.TestCase):
    def setUp(self):
        reset()

    def _publish(self, w):
        pv = seo.publish_preview(link_check=lambda u: 200)
        return seo.publish({"token": pv["token"]}, link_check=lambda u: 200, w=w)

    def test_rendered_body_checked_when_live(self):
        new = POSTS[2]["content"].replace("no links", "no links yet")
        seo.add_draft({"post_id": 3, "field": "content", "new": new, "base": "rendered"}, by="you")
        r = self._publish(LiveFake(rendered="<p>Eels, edited in wp-admin meanwhile.</p>", raw="<!-- wp:paragraph --><p>x</p>"))
        self.assertFalse(r["ok"])
        self.assertIn("Changed in WordPress", r["results"][0]["error"])

    def test_rendered_body_passes_when_unchanged(self):
        seo.add_draft({"post_id": 3, "field": "content", "new": "<p>Eels, now with more facts.</p>", "base": "rendered"}, by="you")
        # the WordPress source differs from the published HTML (block comments), but the published body is unchanged
        r = self._publish(LiveFake(rendered=POSTS[2]["rendered"], raw="<!-- wp:paragraph --><p>Eels, no links.</p><!-- /wp:paragraph -->"))
        self.assertTrue(r["ok"], r)

    def test_undo_stops_if_changed_since_publish(self):
        seo.add_draft({"post_id": 2, "field": "title", "new": "Manta rays: a diver's guide"}, by="you")
        r = self._publish(LiveFake(modified="2026-11-10T10:00:00"))
        self.assertTrue(r["ok"], r)
        n = len(c.read_jsonl("seo_dryrun.jsonl"))
        with self.assertRaises(ValueError) as e:
            seo.undo({"push_id": r["push_id"]}, w=LiveFake(modified="2026-11-10T12:30:00"))
        self.assertIn("changed in WordPress since Studio published it", str(e.exception))
        self.assertEqual(len(c.read_jsonl("seo_dryrun.jsonl")), n)  # nothing restored
        self.assertFalse(seo.history()[0]["undone"])
        u = seo.undo({"push_id": r["push_id"]}, w=LiveFake(modified="2026-11-10T10:00:00"))
        self.assertTrue(u["ok"])
        self.assertIn("values Studio saved", u["message"])

    def test_dry_undo_stops_if_site_changed(self):
        seo.add_draft({"post_id": 2, "field": "title", "new": "Manta rays: a diver's guide"}, by="you")
        r = self._publish(wp.DryRunWriter("staging", delay=0))

        class Edited(wp.DryRunWriter):
            def current(self, post_id):
                return {**super().current(post_id), "title": "Edited by someone else"}
        with self.assertRaises(ValueError):
            seo.undo({"push_id": r["push_id"]}, w=Edited("staging", delay=0))


class LiveMaker(LiveFake):
    """Live-looking writer that can create and trash pages (records only)."""
    made = []

    def create(self, fields, status="publish"):
        r = super().create(fields, status)
        LiveMaker.made.append((fields, status))
        return {**r, "id": 501, "link": f"https://staging.masterliveaboards.com/{fields['slug']}/", "modified": self.mod}

    def current(self, post_id):
        if post_id == 501:
            return {"_modified": self.mod}
        return super().current(post_id)


class NewPages(unittest.TestCase):
    def setUp(self):
        reset()
        from areas import _seo_map as tmap  # noqa: PLC0415
        self.tmap = tmap
        mp = Path(TMP) / tmap.FILE
        if mp.exists():
            mp.unlink()
        tmap.edit([{"op": "add", "node": {"title": "Best time to dive Raja Ampat", "queries": ["raja ampat best time"]}}])
        self.nid = next(iter(tmap.load()["nodes"]))

    def test_claude_writes_user_edits_publish_creates_and_undo_bins(self):
        r = seo.save_new_post({"node_id": self.nid, "title": "Best time to dive Raja Ampat", "content": "<h2>Seasons</h2><p>Oct to Apr.</p>",
                               "meta_desc": "When to dive Raja Ampat: the seasons, currents and manta months, with the best months for a liveaboard trip."})
        np_ = r["new_post"]
        self.assertEqual(np_["slug"], "best-time-to-dive-raja-ampat")
        self.assertEqual(np_["by"], "claude")
        again = seo.save_new_post({"node_id": self.nid, "content": "<h2>Seasons</h2><p>October to April.</p>"}, by="you")  # same node = same page
        self.assertEqual(again["new_post"]["id"], np_["id"])
        self.assertEqual(len(seo.list_drafts()["new_posts"]), 1)
        with self.assertRaises(ValueError):
            seo.save_new_post({"node_id": "nope", "title": "x"})
        pv = seo.publish_preview(link_check=lambda u: 200)
        self.assertEqual(pv["new_pages"], 1)
        self.assertTrue(pv["posts"][0]["new"])
        self.assertFalse(pv["blocked"])
        w = LiveMaker(modified="2026-11-15T10:00:00")
        res = seo.publish({"token": pv["token"]}, link_check=lambda u: 200, w=w)
        self.assertTrue(res["ok"], res)
        self.assertEqual(LiveMaker.made[-1][1], "publish")
        self.assertIn("October", LiveMaker.made[-1][0]["content"])
        self.assertEqual(seo.list_drafts()["new_posts"], [])
        self.assertTrue(self.tmap.load()["nodes"][self.nid]["url"].endswith("/best-time-to-dive-raja-ampat/"))  # node points at the page
        self.assertTrue(seo.history()[0]["posts"][0]["new"])
        u = seo.undo({"push_id": res["push_id"]}, w=w)
        self.assertTrue(u["ok"], u)
        self.assertIn("bin", u["message"])
        self.assertEqual(c.read_jsonl("seo_dryrun.jsonl")[-1]["action"], "trash")
        self.assertEqual(self.tmap.load()["nodes"][self.nid]["url"], "")

    def test_empty_body_blocks_and_dry_run_leaves_map(self):
        seo.save_new_post({"node_id": self.nid, "title": "Draft only"}, by="you")
        self.assertTrue(seo.publish_preview(link_check=lambda u: 200)["blocked"])
        seo.save_new_post({"node_id": self.nid, "content": "<p>Body.</p>", "status": "draft"}, by="you")
        pv = seo.publish_preview(link_check=lambda u: 200)
        res = seo.publish({"token": pv["token"]}, link_check=lambda u: 200, w=wp.DryRunWriter("staging", delay=0))
        self.assertTrue(res["ok"], res)
        self.assertEqual(c.read_jsonl("seo_dryrun.jsonl")[-1]["payload"]["status"], "draft")
        self.assertFalse(self.tmap.load()["nodes"][self.nid].get("url"))  # dry run: nothing made, node untouched


class Connect(unittest.TestCase):
    def setUp(self):
        reset()
        self._http = c.http

    def tearDown(self):
        c.http = self._http
        c.forget_wp_creds()

    def test_connect_saves_600_and_never_returns_password(self):
        c.http = lambda url, **kw: (200, {}, {"id": 9, "name": "Marketing Studio", "roles": ["editor"]})
        r = seo.connect({"user": "marketing-studio", "password": "abcd efgh ijkl mnop", "target": "staging"})
        self.assertNotIn("abcd", repr(r))
        self.assertEqual(stat.S_IMODE(os.stat(c.SECRETS).st_mode), 0o600)
        self.assertEqual(c.wp_creds(), ("marketing-studio", "abcd efgh ijkl mnop"))
        self.assertNotIn("abcd", repr(seo.get_settings()))
        self.assertEqual(c.scrub("failed with abcd efgh ijkl mnop"), "failed with ***")
        self.assertIsInstance(wp.WpRestWriter("staging"), wp.WpRestWriter)
        seo.disconnect()
        self.assertEqual(c.wp_creds(), (None, None))

    def test_wrong_role_and_bad_login(self):
        c.http = lambda url, **kw: (200, {}, {"roles": ["subscriber"]})
        with self.assertRaises(RuntimeError):
            seo.connect({"user": "u", "password": "p p", "target": "staging"})
        c.http = lambda url, **kw: (401, {}, {"message": "Sorry, p p is wrong"})
        with self.assertRaises(RuntimeError) as e:
            seo.connect({"user": "u", "password": "p p", "target": "staging"})
        self.assertNotIn("p p", str(e.exception))
        self.assertEqual(c.wp_creds(), (None, None))

    def test_live_mode_needs_login(self):
        with self.assertRaises(ValueError):
            seo.save_settings({"mode": "live"})
        self.assertEqual(seo.save_settings({"target": "prod"})["target"], "prod")


if __name__ == "__main__":
    unittest.main()
