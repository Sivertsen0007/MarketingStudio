"""v0.2.3 S5a: engine glue behind the Google Ads UI fixes.
- G9: an RSA edit can change only its display paths (Push live update mask, Editor fields, summary, length check).
- G10: Remove in the Sitelinks / Callouts grids unlinks them on Push live (campaign or account level) and undo links them again.
- U2: studioFmt reads Editor's numeric YYYYMMDD dates (20230524), not as epoch seconds (needs node; skipped without it).

Run from the engine folder:  python3 -m unittest tests/test_s5a_google_ui.py
Never asks Google: the ids resolver and the live reads are stubs.
"""
import os as _os, tempfile as _tf
_os.environ["MSTUDIO_HOME"] = _tf.mkdtemp(prefix="mstudio-test-")
_os.environ["MSTUDIO_KEYCHAIN"] = "0"
_os.environ["GAE_EDITOR_DIR"] = _tf.mkdtemp(prefix="gae-editor-test-")

import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core  # noqa: E402
import gae  # noqa: E402
import live  # noqa: E402

A = "3333333333"


class Ids:
    """The bits of live.Ids that ops_for uses for these change types."""
    acct = A

    def campaign(self, name):
        return f"customers/{A}/campaigns/101", {"name": name}

    def ad_group(self, camp, ag):
        return f"customers/{A}/adGroups/201", {"name": ag}

    def rsa_ads(self, camp, ag, local_id=None, ad_id=None):
        return ["401"]

    def sitelink(self, level, camp, text):
        return {"Dive deals": "701", "Brochure": "702"}[text]

    def callout(self, level, camp, text):
        return {"Free nitrox": "801"}[text]


LIVE_AD = {"adGroupAd": {"status": "ENABLED", "ad": {"finalUrls": ["https://example.org/a"], "responsiveSearchAd": {
    "headlines": [{"text": "H1"}, {"text": "H2"}, {"text": "H3"}], "descriptions": [{"text": "D1"}, {"text": "D2"}],
    "path1": "dive", "path2": ""}}}}


class RsaPaths(unittest.TestCase):
    def ops(self, c):
        with mock.patch.object(live, "_live", return_value=LIVE_AD):
            return live.ops_for(Ids(), c, False)

    def test_path_only_edit(self):
        ops = self.ops({"type": "rsa", "edit": True, "campaign": "C", "ad_group": "G", "ad_id": "401", "path1": "dive", "path2": "red-sea"})
        self.assertEqual(len(ops), 1)
        upd = ops[0]["op"]["adOperation"]
        self.assertEqual(upd["updateMask"], "responsiveSearchAd.path2")  # path1 unchanged: not sent
        self.assertEqual(upd["update"]["responsiveSearchAd"], {"path2": "red-sea"})
        self.assertEqual(ops[0]["undo"]["adOperation"]["update"]["responsiveSearchAd"], {"path2": ""})
        self.assertIn("display path", ops[0]["line"])

    def test_unchanged_paths_send_nothing(self):
        self.assertEqual(self.ops({"type": "rsa", "edit": True, "campaign": "C", "ad_group": "G", "ad_id": "401", "path1": "dive", "path2": ""}), [])

    def test_text_and_path(self):
        ops = self.ops({"type": "rsa", "edit": True, "campaign": "C", "ad_group": "G", "ad_id": "401", "path1": "", "path2": "",
                        "headlines": [{"text": "A"}, {"text": "B"}, {"text": "C"}], "descriptions": [{"text": "X"}, {"text": "Y"}]})
        mask = ops[0]["op"]["adOperation"]["updateMask"].split(",")
        self.assertIn("responsiveSearchAd.headlines", mask)
        self.assertIn("responsiveSearchAd.path1", mask)  # cleared
        self.assertEqual(ops[0]["op"]["adOperation"]["update"]["responsiveSearchAd"]["path1"], "")

    def test_check_and_summary(self):
        c = {"type": "rsa", "edit": True, "campaign": "C", "ad_group": "G", "path1": "liveaboard-holidays", "path2": ""}
        self.assertIn("Path 1: 19 chars > 15", core.check(c)[0])
        ok = {"type": "rsa", "edit": True, "campaign": "C", "ad_group": "G", "path1": "dive", "path2": "red-sea"}
        self.assertEqual(core.check(ok)[0], [])
        self.assertEqual(core._summary(ok), "RSAs in C / G: display path -> /dive/red-sea")


class AssetRemove(unittest.TestCase):
    def test_sitelinks_campaign(self):
        ops = live.ops_for(Ids(), {"type": "sitelink", "remove": True, "campaign": "Brand", "texts": ["Dive deals", "Brochure"]}, False)
        self.assertEqual([o["op"]["campaignAssetOperation"]["remove"] for o in ops],
                         [f"customers/{A}/campaignAssets/101~701~SITELINK", f"customers/{A}/campaignAssets/101~702~SITELINK"])
        self.assertEqual(ops[0]["undo"]["campaignAssetOperation"]["create"],
                         {"campaign": f"customers/{A}/campaigns/101", "asset": f"customers/{A}/assets/701", "fieldType": "SITELINK"})
        self.assertFalse(ops[0]["permanent"])
        self.assertEqual(ops[0]["line"], 'unlink sitelink "Dive deals" from "Brand"')

    def test_callout_account(self):
        ops = live.ops_for(Ids(), {"type": "callout", "remove": True, "level": "account", "campaign": "", "texts": ["Free nitrox"]}, False)
        self.assertEqual(ops[0]["op"]["customerAssetOperation"]["remove"], f"customers/{A}/customerAssets/801~CALLOUT")
        self.assertEqual(ops[0]["undo"]["customerAssetOperation"]["create"], {"asset": f"customers/{A}/assets/801", "fieldType": "CALLOUT"})

    def test_checks_pass(self):
        self.assertEqual(core.check({"type": "sitelink", "remove": True, "campaign": "Brand", "texts": ["Dive deals"]})[0], [])
        self.assertEqual(core.check({"type": "callout", "remove": True, "campaign": "Brand", "texts": ["Free nitrox"]})[0], [])


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class DateFormat(unittest.TestCase):
    def test_yyyymmdd_number(self):
        js = Path(__file__).resolve().parents[1] / "static" / "setup.js"
        src = js.read_text().split("(function(){", 1)[0]  # just window.studioFmt
        prog = ("const window = {};\n" + src + "\nconsole.log(JSON.stringify([window.studioFmt.date(20230524), window.studioFmt.date('20230524'),"
                " window.studioFmt.date(1700000000), window.studioFmt.date('')]))")
        out = subprocess.run(["node", "-e", prog], capture_output=True, text=True, timeout=30, env={**_os.environ, "TZ": "UTC"})
        self.assertEqual(out.returncode, 0, out.stderr)
        got = json.loads(out.stdout)
        self.assertEqual(got[:2], ["24 May 2023", "24 May 2023"])
        self.assertEqual(got[2], "14 Nov 2023")  # epoch seconds still work
        self.assertEqual(got[3], "")


if __name__ == "__main__":
    unittest.main()
