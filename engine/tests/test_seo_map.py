"""Topical map: edits, Claude's proposals (accept / reject), CSV import, and the report joined with Search Console."""
import os as _os, tempfile as _tf
_os.environ.setdefault("MSTUDIO_HOME", _tf.mkdtemp(prefix="mstudio-test-"))
_os.environ["MSTUDIO_KEYCHAIN"] = "0"
_os.environ["SEO_STUDIO_DIR"] = _tf.mkdtemp(prefix="seo-map-test-")

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from areas import _seo_map as tm  # noqa: E402

CSV = """Level,Entity Type,Page Title,URL Slug,Full URL,Parent Entity,Primary Keywords,Purpose,Priority,Existing Post
Root,Hub,Liveaboard Diving,/liveaboard-diving/,https://example.com/liveaboard-diving,,"liveaboard diving, dive liveaboard",Define the scope,Critical,
Level 1,Region Hub,Red Sea Liveaboards,/red-sea-liveaboards/,https://example.com/red-sea-liveaboards,Liveaboard Diving,red sea liveaboard,Regional overview,High,
Level 4,Species,Hammerhead Sharks,,,Red Sea Liveaboards,"hammerhead sharks red sea",Species guide,Medium,
Level 5,Boat,Blue Horizon,/blue-horizon/,https://example.com/blue-horizon,Red Sea Liveaboards,blue horizon liveaboard,Boat page,High,
Level 4,Species,Orphan Topic,,,Not In Sheet,orphan query,,Low,
"""


class Map(unittest.TestCase):
    def setUp(self):
        p = Path(_os.environ["SEO_STUDIO_DIR"]) / tm.FILE
        if p.exists():
            p.unlink()

    def test_import_builds_tree(self):
        tm.import_csv(CSV)
        m = tm.load()
        by = {n["title"]: n for n in m["nodes"].values()}
        self.assertEqual(len(by), 5)
        self.assertEqual(by["Liveaboard Diving"]["type"], "root")
        self.assertEqual(by["Red Sea Liveaboards"]["type"], "hub")
        self.assertEqual(by["Blue Horizon"]["type"], "commercial")
        self.assertEqual(by["Blue Horizon"]["section"], "core")
        self.assertEqual(by["Hammerhead Sharks"]["section"], "outer")
        self.assertEqual(by["Red Sea Liveaboards"]["parent"], by["Liveaboard Diving"]["id"])
        self.assertEqual(by["Hammerhead Sharks"]["parent"], by["Red Sea Liveaboards"]["id"])
        self.assertEqual(by["Liveaboard Diving"]["queries"], ["liveaboard diving", "dive liveaboard"])
        self.assertEqual(by["Liveaboard Diving"]["priority"], 1)
        self.assertEqual(by["Orphan Topic"]["parent"], "")  # parent not in the sheet: kept, noted, not failed
        self.assertIn("Not In Sheet".lower(), by["Orphan Topic"]["note"])

    def test_claude_proposes_user_accepts_or_rejects(self):
        r = tm.propose([{"op": "site", "site": {"central_entity": "liveaboard diving"}},
                        {"op": "add", "ref": "hub", "node": {"title": "Maldives Liveaboards", "type": "hub", "section": "core"}},
                        {"op": "add", "node": {"title": "Manta season", "parent": "hub", "queries": "maldives manta season"}}],
                       note="first pass")
        self.assertEqual(tm.load()["nodes"], {})  # nothing changed until accepted
        pid = r["proposal"]["id"]
        self.assertEqual(len(tm.load()["proposals"]), 1)
        tm.accept(pid)
        m = tm.load()
        self.assertEqual(m["site"]["central_entity"], "liveaboard diving")
        by = {n["title"]: n for n in m["nodes"].values()}
        self.assertEqual(by["Manta season"]["parent"], by["Maldives Liveaboards"]["id"])
        self.assertEqual(m["proposals"], [])
        r2 = tm.propose([{"op": "remove", "id": by["Manta season"]["id"]}])
        tm.reject(r2["proposal"]["id"])
        self.assertIn(by["Manta season"]["id"], tm.load()["nodes"])
        with self.assertRaises(ValueError):
            tm.propose([{"op": "add", "node": {"title": "x", "type": "nonsense"}}])  # a broken proposal never lands
        self.assertTrue(any(x["kind"] == "accept" for x in tm.history()))

    def test_report_finds_gaps_and_updates(self):
        tm.import_csv(CSV)
        by = {n["title"]: n for n in tm.load()["nodes"].values()}
        hub = by["Red Sea Liveaboards"]["id"]
        tm.edit([{"op": "update", "id": by["Blue Horizon"]["id"], "node": {"links": [{"to": hub, "anchor": "Red Sea liveaboards"}]}}])
        cur = {"/liveaboard-diving": {"clicks": 20, "impressions": 3000, "ctr": 0.006, "position": 4.0},
               "/red-sea-liveaboards": {"clicks": 40, "impressions": 900, "ctr": 0.044, "position": 12.3},
               "/blue-horizon": {"clicks": 5, "impressions": 200, "ctr": 0.025, "position": 6}}
        prev = {"/liveaboard-diving": {"clicks": 60, "impressions": 3200, "ctr": 0.02, "position": 3.8}}
        qp = [["red sea liveaboard", "/some-blog-post", 30, 600, 0.05, 5.0],
              ["red sea liveaboard", "/red-sea-liveaboards", 2, 100, 0.02, 14.0],
              ["blue horizon liveaboard", "/blue-horizon", 5, 150, 0.03, 4.0],
              ["best time to dive egypt", "/egypt-guide", 10, 500, 0.02, 7.0]]
        posts = [{"key": "/blue-horizon", "links_to": []}]
        r = tm.report(cur, prev, qp, posts=posts, min_imps=50)
        rows = {x["title"]: x for x in r["nodes"]}
        kinds = lambda t: {i["kind"] for i in rows[t]["issues"]}  # noqa: E731
        self.assertEqual(rows["Hammerhead Sharks"]["state"], "gap")
        self.assertIn("missing_page", kinds("Hammerhead Sharks"))
        self.assertIn("losing", kinds("Liveaboard Diving"))
        self.assertIn("low_ctr", kinds("Liveaboard Diving"))
        self.assertIn("striking", kinds("Red Sea Liveaboards"))
        self.assertIn("query_elsewhere", kinds("Red Sea Liveaboards"))
        self.assertIn("missing_links", kinds("Blue Horizon"))  # the map says link to the hub, the page does not
        r2 = tm.report({}, {}, [], posts=[], min_imps=50)
        self.assertEqual({x["title"]: x["state"] for x in r2["nodes"]}["Blue Horizon"], "gap")  # URL with no page behind it
        self.assertEqual(r["coverage"]["core"]["nodes"], 3)
        self.assertEqual(r["coverage"]["core"]["live"], 3)
        pages = [g["page"] for g in r["unmapped"]]
        self.assertIn("/egypt-guide", pages)  # demand with no node
        self.assertNotIn("/blue-horizon", pages)


if __name__ == "__main__":
    unittest.main()
