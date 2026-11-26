"""S6: UI logic run in node (skipped without node): the Email send-time zone on DST edges matches the engine's to_utc,
and the Meta Edit panel never sets a status next to an End test status and folds two same-kind items when asked."""
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from areas import email

ENGINE = Path(__file__).resolve().parents[1]


def _between(src, start, end):
    i = src.index(start)
    return src[i:src.index(end, i)]


def _node(prog):
    out = subprocess.run(["node", "-e", prog], capture_output=True, text=True, timeout=30, env={**os.environ, "TZ": "Asia/Bangkok"})
    if out.returncode:
        raise AssertionError(out.stderr)
    return json.loads(out.stdout)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class ZoneToDate(unittest.TestCase):
    CASES = [("Europe/London", "2026-03-29T00:30"), ("Europe/London", "2026-03-29T01:00"), ("Europe/London", "2026-03-29T01:30"),
             ("Europe/London", "2026-03-29T02:00"), ("Europe/London", "2026-10-25T00:59"), ("Europe/London", "2026-10-25T01:00"),
             ("Europe/London", "2026-10-25T01:30"), ("Europe/London", "2026-10-25T02:00"), ("Europe/London", "2026-11-12T09:00"),
             ("America/New_York", "2026-03-08T02:30"), ("America/New_York", "2026-11-01T01:30"), ("Australia/Sydney", "2026-04-05T02:30"),
             ("Australia/Sydney", "2026-10-04T02:30"), ("Asia/Bangkok", "2026-06-01T12:00")]

    def test_same_as_engine(self):
        src = (ENGINE / "static" / "email.js").read_text()
        fn = _between(src, "function zoneParts(d){", "const num = v =>")
        prog = ("let TZ; const E = {get state(){ return {send_tz: TZ} }}; const sendTz = () => E.state.send_tz;\n" + fn
                + f"\nconst cases = {json.dumps(self.CASES)};\n"
                  "console.log(JSON.stringify(cases.map(([z, v]) => { TZ = z; return zoneToDate(v).toISOString().slice(0, 16) })))")
        got = _node(prog)
        want = [email.to_utc(v, z).strftime("%Y-%m-%dT%H:%M") for z, v in self.CASES]
        self.assertEqual(got, want)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class PanelPlan(unittest.TestCase):
    def run_plan(self, r, ch, olds, opt=None, draft=None):
        src = (ENGINE / "index.html").read_text()
        code = _between(src, "const MBASE = [", "// -> {changes, per:") + _between(src, "const PANEL_FIELDS = {", "function asFlush(){")
        prog = (f"const S = {{draftItems: {json.dumps(draft if draft is not None else olds)}}};\n" + code
                + f"\nconsole.log(JSON.stringify(panelPlan({json.dumps(r)}, {json.dumps(ch)}, {json.dumps(olds)}, {json.dumps(opt or {})})))")
        return _node(prog)

    END = {"id": "e1", "by": "claude", "note": "End test", "change": {"type": "meta_status", "level": "adset", "id": "7", "status": "Paused", "with_end": "t1"}}

    def test_no_plain_status_next_to_end_test(self):
        # live Active, the End test pauses; the panel says Active (or anything else): no plain status is added
        for st in ("Active", "Paused"):
            p = self.run_plan({"id": "7"}, [{"type": "meta_status", "level": "adset", "id": "7", "status": st}], [self.END])
            self.assertEqual(p["add"], [], st)
            self.assertEqual(p["remove"], [])
            self.assertEqual(p["keep"], ["e1"])

    def test_end_test_seen_by_the_row_when_not_in_olds(self):
        r = {"id": "7", "_withEnd": self.END["change"]}
        p = self.run_plan(r, [{"type": "meta_status", "level": "adset", "id": "7", "status": "Active"}], [], draft=[])
        self.assertEqual(p["add"], [])

    def test_other_changes_still_saved(self):
        p = self.run_plan({"id": "7"}, [{"type": "meta_status", "level": "adset", "id": "7", "status": "Active"},
                                        {"type": "meta_rename", "level": "adset", "id": "7", "new_name": "New"}], [self.END])
        self.assertEqual([a["change"]["type"] for a in p["add"]], ["meta_rename"])

    def test_two_same_kind_items_fold_on_open(self):
        a = {"id": "a", "by": "claude", "note": "ages", "change": {"type": "meta_targeting", "level": "adset", "id": "7", "age_min": 25}}
        b = {"id": "b", "by": "you", "note": "", "change": {"type": "meta_targeting", "level": "adset", "id": "7", "countries": ["GB", "DE"]}}
        pc = {"type": "meta_targeting", "level": "adset", "id": "7", "age_min": 25, "countries": ["GB", "DE"]}
        p = self.run_plan({"id": "7"}, [pc], [a, b])
        self.assertEqual(sorted(p["keep"]), ["a", "b"])  # without the fold both stay as they are
        p = self.run_plan({"id": "7"}, [pc], [a, b], {"fold": True})
        self.assertEqual(sorted(p["remove"]), ["a", "b"])
        self.assertEqual(len(p["add"]), 1)
        self.assertEqual(p["add"][0]["change"], pc)
        self.assertEqual(p["add"][0]["note"], "ages")
        self.assertEqual([x["id"] for x in p["announce"]], ["a"])
        self.assertEqual(p["folded"], [{"type": "meta_targeting", "n": 2}])


class OverviewLabels(unittest.TestCase):
    def test_top_mover_is_the_post_title(self):
        from areas import seo
        titles = {"/blog/six-facts-about-turtles": "Six facts about sea turtles"}
        self.assertEqual(seo._mover_label("/blog/six-facts-about-turtles", titles), "Six facts about sea turtles")
        self.assertEqual(seo._mover_label("/blog/not-cached", titles), "not-cached")  # not in the list: the slug, as before
        self.assertEqual(seo._mover_label("/", {}), "Homepage")

    def test_meta_clicks_are_link_clicks(self):
        import overview
        self.assertEqual(overview._link_clicks({"clicks": "500", "inline_link_clicks": "120"}), 120)
        self.assertEqual(overview._link_clicks({"clicks": "500"}), 0)


class DrainSmall(unittest.TestCase):
    """A refused small body is read for at most 2 seconds in all, even when the client trickles bytes."""
    def test_total_deadline(self):
        import socket
        import threading
        import time
        import server
        a, b = socket.socketpair()
        stop = threading.Event()

        def trickle():
            try:
                while not stop.is_set():
                    b.sendall(b"x")
                    time.sleep(0.3)
            except OSError:
                pass
        threading.Thread(target=trickle, daemon=True).start()
        h = server.H.__new__(server.H)
        h.headers = {"Content-Length": "100000"}
        h.connection = a
        h.rfile = a.makefile("rb")
        t0 = time.monotonic()
        h._drain_small()
        took = time.monotonic() - t0
        stop.set()
        a.close()
        b.close()
        self.assertLess(took, 2.8)
        self.assertGreater(took, 1.5)


if __name__ == "__main__":
    unittest.main()
