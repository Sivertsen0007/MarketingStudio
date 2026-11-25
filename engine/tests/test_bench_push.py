"""A small run of the push speed benchmark (tests/bench_push.py): 20 mixed changes per channel, 0.05 s per fake call.
Guards the batching / parallel work against regressions with generous limits, so the suite stays quick.
Offline: fake Meta Graph and fake Google Ads API only.

Run from the engine folder:  python3 -m unittest tests.test_bench_push
"""
import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests import bench_push as bench  # noqa: E402

N, LATENCY = 20, 0.05


class SmallBench(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = bench.run(N, LATENCY, baseline=True, reuse=True)

    def test_every_run_succeeds(self):
        for ch in ("meta", "meta_reuse", "meta_seq", "google"):
            for what in ("preview", "push"):
                x = self.r[ch][what]
                with self.subTest(ch=ch, what=what):
                    self.assertTrue(x["ok"], (x["problems"], x["message"]))

    def test_meta_reads_are_batched(self):
        for ch in ("meta", "meta_reuse", "meta_seq"):
            for what in ("preview", "push"):
                c = self.r[ch][what]["calls"]
                with self.subTest(ch=ch, what=what):
                    # the plan's live reads: one batched GET per level (campaign, ad set, ad)
                    self.assertLessEqual(c.get("GET ids", 0) + c.get("GET one", 0) + c.get("GET edge", 0), 5, c)
                    # after a push, the read-back: one batched GET per level of what was made or changed, never per item
                    self.assertEqual(c.get("GET status", 0), 3 if what == "push" else 0, c)
        self.assertEqual(self.r["meta"]["preview"]["calls"].get("CHECK"), 22)  # one per step; a copied ad makes two checks
        for ch in ("meta", "meta_seq"):  # the default (REUSE_CHECKS off): the push checks every step again
            self.assertEqual(self.r[ch]["push"]["calls"].get("CHECK"), 22, ch)
        # switched on: the push right after an unchanged preview reuses its passes (same account, token, params, live state)
        self.assertNotIn("CHECK", self.r["meta_reuse"]["push"]["calls"])
        push = self.r["meta"]["push"]["calls"]
        self.assertEqual(push.get("CREATE", 0) + push.get("UPDATE", 0), 25, push)
        self.assertNotIn("CREATE", self.r["meta"]["preview"]["calls"])
        self.assertNotIn("UPDATE", self.r["meta"]["preview"]["calls"])

    def test_meta_runs_side_by_side(self):
        self.assertGreater(self.r["meta"]["preview"]["peak"], 1)
        self.assertEqual(self.r["meta_seq"]["preview"]["peak"], 1)
        # checks side by side beat one at a time (generous: half the gain is enough)
        par, seq = self.r["meta"]["preview"]["wall"], self.r["meta_seq"]["preview"]["wall"]
        self.assertLess(par, seq * 0.75, (par, seq))

    def test_google_reads_are_batched(self):
        pv, pu = self.r["google"]["preview"]["calls"], self.r["google"]["push"]["calls"]
        self.assertLessEqual(pv.get("query", 0) + pv.get("query (batched)", 0), 10, pv)
        self.assertEqual(pv.get("mutate (validateOnly)"), 1, pv)
        self.assertNotIn("mutate (apply)", pv)
        self.assertEqual((pu.get("mutate (validateOnly)"), pu.get("mutate (apply)")), (1, 1), pu)

    def test_generous_time_limits(self):
        for ch in ("meta", "google"):
            for what in ("preview", "push"):
                with self.subTest(ch=ch, what=what):
                    self.assertLess(self.r[ch][what]["wall"], 15.0)

    def test_report_prints_a_table(self):
        out = io.StringIO()
        bench.report([self.r], file=out)
        txt = out.getvalue()
        self.assertIn("Meta, sequential", txt)
        self.assertIn("Google", txt)
        self.assertNotIn("—", txt)


if __name__ == "__main__":
    unittest.main()
