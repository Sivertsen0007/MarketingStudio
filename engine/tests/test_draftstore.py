"""draftstore: item transactions, version bumps, the outbox, remote items, migration of every old draft file, threads."""
import os as _os, tempfile as _tf
_os.environ.setdefault("MSTUDIO_HOME", _tf.mkdtemp(prefix="mstudio-test-"))
_os.environ["MSTUDIO_KEYCHAIN"] = "0"

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import draftstore as ds  # noqa: E402

ADS = {"version": 7, "items": [
    {"id": "a1b2c3d4", "change": {"type": "campaign", "campaign": "X", "budget": 40.0}, "by": "claude", "note": "", "ts": "2026-11-01T10:00:00",
     "summary": "X budget", "problems": [], "warnings": []},
    {"id": "e5f6a7b8", "change": {"type": "keyword", "campaign": "X", "ad_group": "Y", "keyword": "dive"}, "by": "you", "note": "n",
     "ts": "2026-11-01T10:01:00", "summary": "kw", "problems": [], "warnings": ["w"]}]}
EMAIL = {"version": 3, "drafts": {"101": {"fields": {"subject": "Hi"}, "by": "claude", "at": "2026-11-01T10:00:00Z", "note": None},
                                  "77": {"fields": {"name": "Oct"}, "by": "you", "at": "2026-11-01T11:00:00Z", "note": "x"}}}
SEO = {"version": 12, "changes": [{"id": "c000000001", "post_id": 5, "field": "meta_desc", "kind": "edit", "old": "a", "new": "b", "by": "claude"}],
       "new_posts": [{"id": "n000000001", "node_id": "n_1", "title": "New", "by": "you", "edited_by": "claude"}]}
MAP = {"site": {"central_entity": "liveaboard diving", "source_context": "", "central_search_intent": "", "exclude": []},
       "nodes": {"n_1": {"id": "n_1", "title": "Root", "by": "you"}, "n_2": {"id": "n_2", "title": "Hub", "parent": "n_1", "by": "claude"}},
       "proposals": [{"id": "p_1", "by": "claude", "ops": [], "summary": []}], "version": 4}
SOCIAL = {"posts": [{"id": "abcdef0123", "title": "Post", "by": "you", "results": {}}, {"id": "0123456789", "title": "Two", "by": "claude"}]}
LEGACY = {"draft_4387091070.json": (ADS, ["ads:4387091070"]), "draft_meta_644.json": (ADS, ["ads:meta_644"]),
          "email_draft.json": (EMAIL, ["email"]), "seo_draft.json": (SEO, ["seo:changes", "seo:new_posts"]),
          "seo_topical_map.json": (MAP, ["seo:map_nodes", "seo:map_site", "seo:map_proposals"]), "social_draft.json": (SOCIAL, ["social"])}


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="draftstore-test-"))

    def raw(self, name):
        return json.loads((self.root / name).read_text())


class Transactions(Base):
    def test_put_get_delete_and_order(self):
        with ds.tx("ads:1", self.root, by="you") as t:
            t.put("a", {"id": "a", "by": "claude", "v": 1}, "claude")
            t.put("b", {"id": "b", "by": "you", "v": 1})
            t.put("c", {"id": "c", "v": 1})
        with ds.tx("ads:1", self.root) as t:
            self.assertEqual(t.ids(), ["a", "b", "c"])
            self.assertEqual(t.get("a")["data"]["v"], 1)
            self.assertEqual((t.get("a")["author"], t.get("a")["version"]), ("claude", 1))
            self.assertIsNone(t.get("zz"))
            t.put("a", {"id": "a", "by": "claude", "v": 2}, "you")  # replaced in place: keeps its position
            self.assertTrue(t.delete("b", "you"))
            self.assertFalse(t.delete("b"))
        recs = ds.items("ads:1", self.root)
        self.assertEqual([r["id"] for r in recs], ["a", "c"])
        a = recs[0]
        self.assertEqual((a["data"]["v"], a["version"], a["author"], a["updated_by"], a["deleted"]), (2, 2, "claude", "you", False))
        self.assertEqual(self.raw("draft_1.json")["items"], [{"id": "a", "by": "claude", "v": 2}, {"id": "c", "v": 1}])
        self.assertNotIn(ds.META, ds.read("ads:1", self.root))
        tomb = self.raw("draft_1.json")[ds.META]["colls"]["ads:1"]["items"]["b"]
        self.assertTrue(tomb["deleted"])
        self.assertEqual(tomb["version"], 2)

    def test_dict_and_single_collections(self):
        with ds.tx("email", self.root) as t:
            t.put("101", {"fields": {"subject": "A"}, "by": "you"})
            t.put("77", {"fields": {"subject": "B"}, "by": "claude"})
            t.put("101", {"fields": {"subject": "C"}, "by": "you"})
        self.assertEqual(list(ds.read("email", self.root)["drafts"]), ["101", "77"])
        with ds.tx("seo:map_site", self.root, bump=False) as t:
            t.put("anything", {"central_entity": "diving"})  # one item: always id "site"
        self.assertEqual(ds.read("seo:map_site", self.root)["site"], {"central_entity": "diving"})
        self.assertEqual([r["id"] for r in ds.items("seo:map_site", self.root)], ["site"])

    def test_raise_saves_nothing(self):
        with ds.tx("social", self.root) as t:
            t.put("p1", {"id": "p1"})
        with self.assertRaises(RuntimeError):
            with ds.tx("social", self.root) as t:
                t.delete("p1")
                raise RuntimeError("stop")
        self.assertEqual([p["id"] for p in ds.read("social", self.root)["posts"]], ["p1"])

    def test_unknown_collection(self):
        with self.assertRaises(ValueError):
            ds.layout("nope")


class Versions(Base):
    def test_file_version_bumps_per_tx(self):
        for _ in range(3):
            with ds.tx("email", self.root) as t:
                pass  # even a tx that changes nothing bumps, as the areas did
        self.assertEqual(ds.read("email", self.root)["version"], 3)
        with ds.tx("email", self.root, bump=False) as t:
            t.put("1", {"fields": {}})
        self.assertEqual(ds.read("email", self.root)["version"], 3)
        with ds.doc("seo:changes", self.root) as d:  # doc(): the caller bumps
            d["version"] += 5
            d["changes"].append({"id": "x"})
        self.assertEqual(ds.read("seo:changes", self.root)["version"], 5)
        with ds.doc("ads:9", self.root, bump=True) as d:
            d["items"].append({"id": "q"})
        self.assertEqual(ds.read("ads:9", self.root)["version"], 1)

    def test_item_and_collection_versions(self):
        for v in (1, 1, 2):
            with ds.tx("social", self.root) as t:
                t.put("p", {"id": "p", "v": v})
        meta = self.raw("social_draft.json")[ds.META]["colls"]["social"]
        self.assertEqual(meta["items"]["p"]["version"], 2)  # the same data twice is not a change
        self.assertEqual(meta["version"], 2)
        self.assertNotIn("version", ds.read("social", self.root))
        with ds.tx("social", self.root) as t:
            self.assertEqual(t.version, 2)


class Outbox(Base):
    def test_records_and_coalesces(self):
        with ds.tx("ads:1", self.root, by="you") as t:
            t.put("a", {"id": "a"})
            t.put("b", {"id": "b"})
        with ds.tx("ads:1", self.root) as t:
            t.put("a", {"id": "a", "x": 1})
            t.delete("b")
        with ds.tx("email", self.root) as t:
            t.put("5", {"fields": {}})
        box = ds.outbox(self.root)
        self.assertEqual(sorted((e["coll"], e["id"], e["op"]) for e in box),
                         [("ads:1", "a", "put"), ("ads:1", "b", "delete"), ("email", "5", "put")])
        self.assertTrue(all(set(e) == {"coll", "id", "op", "at"} for e in box))

    def test_old_writer_changes_are_recorded(self):
        with ds.tx("ads:1", self.root) as t:
            t.put("a", {"id": "a", "v": 1})
        d = self.raw("draft_1.json")  # an older Studio rewrites the file and keeps the block as it found it
        d["items"][0]["v"] = 2
        d["items"].append({"id": "b", "by": "claude"})
        (self.root / "draft_1.json").write_text(json.dumps(d))
        with ds.tx("ads:1", self.root):
            pass
        recs = {r["id"]: r for r in ds.items("ads:1", self.root)}
        self.assertEqual((recs["a"]["version"], recs["b"]["author"]), (2, "claude"))
        self.assertIn(("ads:1", "b", "put"), [(e["coll"], e["id"], e["op"]) for e in ds.outbox(self.root)])

    def test_apply_remote_skips_outbox(self):
        with ds.tx("social", self.root) as t:
            t.put("p", {"id": "p", "title": "mine"})
        before = ds.outbox(self.root)
        ds.apply_remote("social", {"id": "r", "data": {"id": "r", "title": "theirs"}, "version": 4, "author": "alex",
                                   "updated_by": "sam", "updated_at": "2026-11-15T10:00:00+00:00"}, self.root)
        ds.apply_remote("social", {"id": "p", "data": {"id": "p", "title": "edited remotely"}, "version": 9, "author": "you",
                                   "updated_by": "alex", "updated_at": "2026-11-15T10:01:00+00:00"}, self.root)
        self.assertEqual(ds.outbox(self.root), before)
        recs = {r["id"]: r for r in ds.items("social", self.root)}
        self.assertEqual((recs["r"]["version"], recs["r"]["author"], recs["r"]["updated_by"]), (4, "alex", "sam"))
        self.assertEqual((recs["p"]["data"]["title"], recs["p"]["version"]), ("edited remotely", 9))
        with ds.tx("social", self.root):
            pass  # the next local commit does not mistake the remote writes for local ones
        self.assertEqual(ds.outbox(self.root), before)
        ds.apply_remote("social", {"id": "r", "deleted": True, "version": 5, "author": "alex", "updated_by": "alex"}, self.root)
        self.assertEqual([p["id"] for p in ds.read("social", self.root)["posts"]], ["p"])
        self.assertEqual(ds.outbox(self.root), before)

    def test_apply_remote_bumps_file_version(self):
        with ds.tx("email", self.root) as t:
            t.put("1", {"fields": {}})
        ds.apply_remote("email", {"id": "2", "data": {"fields": {"subject": "x"}}, "version": 1, "author": "alex", "updated_by": "alex"}, self.root)
        d = ds.read("email", self.root)
        self.assertEqual((d["version"], list(d["drafts"])), (2, ["1", "2"]))

    def test_apply_remote_many_matches_one_by_one(self):
        """The batch gives exactly what the same items one apply_remote at a time give: data, order, metas, versions,
        outbox, and each expect checked against the item as the earlier entries left it."""
        import shutil
        for coll, mk in (("social", lambda i, t: {"id": i, "title": t}), ("email", lambda i, t: {"fields": {"subject": t}}),
                         ("seo:map_site", lambda i, t: {"central_entity": t})):
            a, b = Path(tempfile.mkdtemp(prefix="ds-a-")), Path(tempfile.mkdtemp(prefix="ds-b-"))
            ids = ["site"] if coll == "seo:map_site" else ["x", "y", "z"]
            with ds.tx(coll, a, by="you") as t:
                for i in ids:
                    t.put(i, mk(i, "mine " + i))
            for f in a.iterdir():
                shutil.copy2(f, b / f.name)
            h = {i: ds.peek(coll, i, a)[1] for i in ids}
            r = lambda i, v, t=None, gone=False: ({"id": i, "version": v, "author": "alex", "updated_by": "sam",  # noqa: E731
                                                  "updated_at": "2026-11-15T10:00:00+00:00",
                                                  **({"deleted": True} if gone else {"data": mk(i, t)})})
            last = ids[-1]
            entries = [(r(ids[0], 3, "theirs"), h[ids[0]]),     # expect holds: written, outbox entry goes
                       (r(last, 4, "stale"), "0" * 16),         # expect fails: not written, pending edit kept
                       (r(last, 5, gone=True), h[last]),        # delete
                       (r(last, 6, "back"), ""),                # gone now, so "" holds: comes back at the end
                       (r("new", 1, "fresh"), None),            # no expectation: written
                       (r(ids[0], 7, "again"), h[ids[0]])]      # the earlier entry changed it: fails
            if coll == "seo:map_site":
                entries = [e for e in entries if e[0]["id"] == "site"]
            one = [ds.apply_remote(coll, it, a, expect=e) for it, e in entries]
            many = ds.apply_remote_many(coll, entries, b)
            self.assertEqual(many, one, coll)
            name = ds.layout(coll).file
            self.assertEqual(json.loads((b / name).read_text()), json.loads((a / name).read_text()), coll)
            self.assertEqual(one, [True, False, False, False, False] if coll == "seo:map_site" else [True, False, True, True, True, False])
            if coll != "seo:map_site":  # deleted then put again: back at the end, after the earlier ones
                self.assertEqual([x["id"] for x in ds.items(coll, b)], ["x", "y", "z", "new"])

    def test_apply_remote_many_one_write(self):
        with ds.tx("ads:1", self.root) as t:
            t.put("k", {"id": "k", "budget": 1}, "you")  # a pending local edit
        _, hk = ds.peek("ads:1", "k", self.root)
        saves = []
        real = ds._save
        ds._save = lambda path, d: (saves.append(path.name), real(path, d))
        try:
            got = ds.apply_remote_many("ads:1", [({"id": str(n), "data": {"id": str(n), "budget": n}, "version": 1}, "")
                                                 for n in range(500)] + [({"id": "k", "data": {"id": "k", "budget": 9}, "version": 2}, "x")],
                                       self.root)
        finally:
            ds._save = real
        self.assertEqual(saves, ["draft_1.json"])
        self.assertEqual(got, [True] * 500 + [False])
        self.assertEqual(ds.peek("ads:1", "k", self.root)[0]["budget"], 1)  # never overwritten
        self.assertEqual([(e["id"], e["op"]) for e in ds.outbox(self.root)], [("k", "put")])
        self.assertEqual(ds.read("ads:1", self.root)["version"], 1 + 500)
        self.assertEqual(ds.peek("ads:1", "k", self.root)[1], hk)


class Migration(Base):
    def write_all(self):
        for name, (shape, _) in LEGACY.items():
            (self.root / name).write_text(json.dumps(shape, indent=1))

    def test_every_old_shape(self):
        self.write_all()
        raw = {name: (self.root / name).read_bytes() for name in LEGACY}
        done = ds.migrate(self.root)
        self.assertEqual(sorted(done), sorted(LEGACY))
        for name, (shape, colls) in LEGACY.items():
            stem = name[:-len(".json")]
            self.assertEqual((self.root / f"{stem}.pre-draftstore.json").read_bytes(), raw[name], name)
            self.assertEqual(ds.read(colls[0], self.root), shape, name)  # nothing lost, same shape and version
            self.assertIn(ds.META, self.raw(name))
        recs = {r["id"]: r for r in ds.items("ads:4387091070", self.root)}
        self.assertEqual((recs["a1b2c3d4"]["author"], recs["e5f6a7b8"]["author"]), ("claude", "you"))
        self.assertEqual([r["id"] for r in ds.items("email", self.root)], ["101", "77"])
        self.assertEqual([r["id"] for r in ds.items("seo:new_posts", self.root)], ["n000000001"])
        self.assertEqual(ds.items("seo:new_posts", self.root)[0]["updated_by"], "claude")
        self.assertEqual(sorted(r["id"] for r in ds.items("seo:map_nodes", self.root)), ["n_1", "n_2"])
        self.assertEqual(ds.items("seo:map_site", self.root)[0]["data"]["central_entity"], "liveaboard diving")
        self.assertEqual([r["id"] for r in ds.items("seo:map_proposals", self.root)], ["p_1"])
        self.assertEqual([r["id"] for r in ds.items("social", self.root)], ["abcdef0123", "0123456789"])
        box = ds.outbox(self.root)
        self.assertEqual(len(box), 2 + 2 + 2 + 2 + 4 + 2)  # every migrated item is a pending local put
        self.assertTrue(all(e["op"] == "put" for e in box))
        # idempotent: a second run changes nothing, the backups stay the original bytes
        after = {name: (self.root / name).read_bytes() for name in LEGACY}
        self.assertEqual(ds.migrate(self.root), [])
        with ds.tx("email", self.root, bump=False):
            pass
        self.assertEqual({name: (self.root / name).read_bytes() for name in LEGACY}, after)
        self.assertEqual(ds.outbox(self.root), box)
        for name in LEGACY:
            self.assertEqual((self.root / f"{name[:-5]}.pre-draftstore.json").read_bytes(), raw[name])

    def test_first_write_migrates(self):
        (self.root / "email_draft.json").write_text(json.dumps(EMAIL))
        with ds.tx("email", self.root, by="you") as t:
            t.put("5", {"fields": {"subject": "new"}, "by": "you"})
        d = ds.read("email", self.root)
        self.assertEqual((d["version"], list(d["drafts"])), (4, ["101", "77", "5"]))
        self.assertTrue((self.root / "email_draft.pre-draftstore.json").exists())
        (self.root / "email_draft.json").write_text(json.dumps(EMAIL))  # a fresh old-shape file later: backup kept as it was
        (self.root / "email_draft.pre-draftstore.json").write_text("first")
        with ds.tx("email", self.root):
            pass
        self.assertEqual((self.root / "email_draft.pre-draftstore.json").read_text(), "first")

    def test_broken_file_is_kept_aside(self):
        (self.root / "social_draft.json").write_text("{not json")
        self.assertEqual(ds.read("social", self.root), {"posts": []})
        with ds.tx("social", self.root) as t:
            t.put("p", {"id": "p"})
        self.assertEqual(len(list(self.root.glob("social_draft.corrupt-*.json"))), 1)


class Threads(Base):
    def test_two_writers_lose_nothing(self):
        def writer(tag):
            for i in range(60):
                with ds.tx("ads:1", self.root, by=tag) as t:
                    t.put(f"{tag}{i}", {"id": f"{tag}{i}", "by": tag})

        def doc_writer():
            for i in range(60):
                with ds.doc("ads:1", self.root, bump=True) as d:
                    d["items"].append({"id": f"d{i}", "by": "claude"})

        ts = [threading.Thread(target=writer, args=("a",)), threading.Thread(target=writer, args=("b",)), threading.Thread(target=doc_writer)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        d = ds.read("ads:1", self.root)
        self.assertEqual(len(d["items"]), 180)
        self.assertEqual(d["version"], 180)
        self.assertEqual(len({e["id"] for e in ds.outbox(self.root)}), 180)


class Areas(Base):
    def test_email_drafts_same_shape_and_versions(self):
        from areas import email as em  # noqa: PLC0415
        old = em.DATA_DIR
        em.DATA_DIR = self.root
        self.addCleanup(setattr, em, "DATA_DIR", old)
        r = em.add_draft({"campaign_id": 12, "fields": {"subject": "Hi"}}, by="claude")
        self.assertEqual(set(r), {"campaign_id", "fields", "by", "at", "note"})
        em.add_draft({"campaign_id": 7, "fields": {"name": "N"}})
        em.add_draft({"campaign_id": 12, "fields": {"preview_text": "P"}})
        self.assertEqual([d["campaign_id"] for d in em.list_drafts()], ["12", "7"])
        self.assertEqual(em.list_drafts()[0]["fields"], {"subject": "Hi", "preview_text": "P"})
        self.assertFalse(em.remove_draft(99))
        self.assertTrue(em.remove_draft(7))
        d = self.raw("email_draft.json")
        self.assertEqual(d["version"], 5)
        self.assertEqual(d[ds.META]["colls"]["email"]["items"]["12"]["author"], "claude")
        self.assertEqual(d[ds.META]["colls"]["email"]["items"]["12"]["updated_by"], "you")


if __name__ == "__main__":
    unittest.main()
