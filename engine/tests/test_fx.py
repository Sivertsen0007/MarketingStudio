"""Display currency tests. Run from the studio folder:  python3 -m unittest tests/test_fx.py

Offline: Frankfurter is faked, Studio's data folder is a temp folder (the user's settings.json and drafts are never touched).
Proves: convert/cache/fallback/offline in fx.py, that Overview adds converted totals only when asked, and that a draft
edit made while a display currency is set stores the account's own (native) amount.
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("MSTUDIO_HOME", _tf.mkdtemp(prefix="mstudio-test-"))
_os.environ["MSTUDIO_KEYCHAIN"] = "0"

import json
import re
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

import core  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="fx-test-"))
core.STUDIO_DIR = TMP
import fx  # noqa: E402
import meta_ads as meta  # noqa: E402
import overview  # noqa: E402
import server  # noqa: E402

meta.SETTINGS = TMP / "settings.json"
RATES = {"base": "EUR", "date": "2026-11-09", "rates": {"EUR": 1.0, "USD": 1.16, "GBP": 0.8343, "DKK": 7.4753}, "host": "fake"}
EM = chr(0x2014)


def fake_fetch(calls):
    def f():
        calls.append(1)
        return json.loads(json.dumps(RATES))
    return f


class FxTest(unittest.TestCase):
    def setUp(self):
        for f in TMP.glob("fx.json"):
            f.unlink()
        self._fetch = fx._fetch

    def tearDown(self):
        fx._fetch = self._fetch

    def test_convert(self):
        r = {"rates": RATES["rates"]}
        self.assertAlmostEqual(fx.convert(100, "GBP", "USD", r), 100 * 1.16 / 0.8343)
        self.assertEqual(fx.convert(12.5, "USD", "USD", r), 12.5)
        self.assertAlmostEqual(fx.convert(1.16, "USD", "EUR", r), 1.0)
        self.assertIsNone(fx.convert(None, "GBP", "USD", r))
        with self.assertRaises(ValueError):
            fx.convert(1, "GBP", "XYZ", r)

    def _auto(self):
        """The 12-hour expiry is datapolicy's auto mode; manual (the default) keeps rates until Refresh rates."""
        (TMP / "settings.json").write_text(json.dumps({"data_policy": "auto"}))
        self.addCleanup(lambda: (TMP / "settings.json").unlink(missing_ok=True))

    def test_manual_keeps_rates_until_refresh(self):
        calls = []
        fx._fetch = fake_fetch(calls)
        fx.latest()
        d = json.loads((TMP / "fx.json").read_text())
        d["fetched_ts"] = time.time() - 30 * 86400
        (TMP / "fx.json").write_text(json.dumps(d))
        fx.latest()
        self.assertEqual(len(calls), 1, "manual mode: old rates are kept, no fetch")
        fx.latest(force=True)
        self.assertEqual(len(calls), 2, "Refresh rates fetches")

    def test_cache_12_hours(self):
        self._auto()
        calls = []
        fx._fetch = fake_fetch(calls)
        a = fx.latest()
        b = fx.latest()
        self.assertEqual(len(calls), 1, "second call within 12 hours must use the cache")
        self.assertEqual(a["rates"], b["rates"])
        self.assertTrue((TMP / "fx.json").exists())
        d = json.loads((TMP / "fx.json").read_text())
        d["fetched_ts"] = time.time() - 13 * 3600
        (TMP / "fx.json").write_text(json.dumps(d))
        fx.latest()
        self.assertEqual(len(calls), 2, "a cache older than 12 hours is refreshed")
        fx.latest(force=True)
        self.assertEqual(len(calls), 3, "Refresh rates forces a fetch")

    def test_offline_uses_cache_then_fallback(self):
        self._auto()
        fx._fetch = lambda: None
        r = fx.latest()
        self.assertTrue(r["fallback"] and r["stale"])
        self.assertIn("rate may be out of date", fx.info()["note"])
        fx._fetch = fake_fetch([])
        fx.latest()
        d = json.loads((TMP / "fx.json").read_text())
        d["fetched_ts"] = 0
        (TMP / "fx.json").write_text(json.dumps(d))
        fx._fetch = lambda: None
        r = fx.latest()
        self.assertFalse(r["fallback"])
        self.assertTrue(r["stale"])
        self.assertEqual(r["rates"]["GBP"], 0.8343, "offline with a cache: the last cached rates, not the constants")

    def test_fetch_tries_both_hosts_with_real_user_agent(self):
        seen = []
        real = urllib.request.urlopen

        def boom(req, timeout=None):
            seen.append((req.full_url, req.get_header("User-agent")))
            raise OSError("offline")
        urllib.request.urlopen = boom
        try:
            self.assertIsNone(self._fetch())
        finally:
            urllib.request.urlopen = real
        self.assertEqual([u.split("/latest")[0] for u, _ in seen], fx.HOSTS)
        self.assertTrue(all(ua and "Python-urllib" not in ua for _, ua in seen))

    def test_note_and_date(self):
        fx._fetch = fake_fetch([])
        n = fx.info()["note"]
        self.assertEqual(n, "ECB reference rate via Frankfurter, 9 November 2026: 1 GBP = 1.39 USD")
        self.assertNotIn(EM, n)


class OverviewTest(unittest.TestCase):
    def sample(self):
        camps = [{"channel": "Google", "currency": "GBP", "spend": 100.0, "destination": "Maldives", "select_cabin": 4, "clicks": 10, "results": 1},
                 {"channel": "Meta", "currency": "USD", "spend": 116.0, "destination": "Maldives", "select_cabin": 6, "clicks": 20, "results": 2}]
        totals = [{"channel": "Google", "currency": "GBP", "spend": 100.0, "clicks": 10, "results": 1, "select_cabin": 4, "campaigns": 1, "cost_per_select_cabin": 25.0},
                  {"channel": "Meta", "currency": "USD", "spend": 116.0, "clicks": 20, "results": 2, "select_cabin": 6, "campaigns": 1, "cost_per_select_cabin": 19.33}]
        return {"campaigns": camps, "totals": totals, "destinations": [{"destination": "Maldives", "spend": {"Google GBP": 100.0, "Meta USD": 116.0}}]}

    def test_own_never_adds(self):
        o = overview.convert(self.sample(), "own", RATES)
        self.assertIsNone(o["display"])
        self.assertNotIn("all_channels", o)

    def test_converted_total(self):
        o = overview.convert(self.sample(), "EUR", {**RATES, "source": fx.SOURCE})
        a = o["all_channels"]
        eur_g = round(100 / 0.8343, 2)
        self.assertAlmostEqual(a["spend"], round(eur_g + 100.0, 2), places=2)
        self.assertEqual(a["select_cabin"], 10)
        self.assertAlmostEqual(a["cost_per_select_cabin"], round(a["spend"] / 10, 2))
        self.assertEqual(o["totals"][0]["spend"], 100.0, "native amounts stay")
        self.assertEqual(o["totals"][0]["spend_display"], eur_g)
        self.assertAlmostEqual(o["destinations"][0]["spend_display"], a["spend"], places=1)
        self.assertIn("9 November 2026", o["display"]["note"])
        self.assertIn("latest rate", o["display"]["note"])

    def test_unknown_currency_left_out(self):
        s = self.sample()
        s["campaigns"][1]["currency"] = s["totals"][1]["currency"] = "XYZ"
        o = overview.convert(s, "EUR", RATES)
        self.assertEqual(o["all_channels"]["channels"], ["Google"])
        self.assertIn("XYZ", o["display"]["note"])


class NativeEditTest(unittest.TestCase):
    """With a display currency set, an edit posted to the draft keeps the account's own amount."""

    @classmethod
    def setUpClass(cls):
        fx._fetch = fake_fetch([])
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), server.H)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def post(self, path, body):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "X-Studio-Press": server.PRESS, "Host": f"localhost:{self.port}"})
        return json.loads(urllib.request.urlopen(req, timeout=20).read())

    def get(self, path):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", headers={"Host": f"localhost:{self.port}"})
        return json.loads(urllib.request.urlopen(req, timeout=20).read())

    def test_display_setting_saved_and_edit_native(self):
        self.assertEqual(self.post("/api/settings/currency", {"currency": "eur"})["display_currency"], "EUR")
        self.assertEqual(json.loads(meta.SETTINGS.read_text())["display_currency"], "EUR")
        self.assertEqual(self.get("/api/fx")["display_currency"], "EUR")
        items = self.post("/api/draft/add", {"acct": "ml", "changes": [{"type": "campaign", "campaign": "FX test campaign", "budget": 40.0}], "by": "you"})
        acct = core.resolve("ml")
        d = json.loads((TMP / f"draft_{acct}.json").read_text())
        stored = [i for i in d["items"] if i["id"] == items[0]["id"]][0]["change"]["budget"]
        self.assertEqual(stored, 40.0, "the draft holds the native GBP amount, never a converted one")
        self.post("/api/draft/remove", {"acct": "ml", "ids": [items[0]["id"]]})
        self.assertEqual(self.post("/api/settings/currency", {"currency": "own"})["display_currency"], "own")
        with self.assertRaises(Exception):
            self.post("/api/settings/currency", {"currency": "XYZ"})

    def test_ui_never_converts_what_it_writes(self):
        """The page's changeFor()/Save-to-draft code (what becomes a draft change) must not call the display conversion."""
        page = (HERE / "index.html").read_text()
        body = page[page.index("function changeFor("):page.index("document.querySelectorAll(\"[data-bulk]\")")]
        self.assertNotRegex(body, r"toDisp|fxRate|curFmt")
        for name in ("function editAdset(", "function editCampaign("):
            fn = page[page.index(name):]
            fn = fn[:fn.index("\n}\n")]
            pushes = re.findall(r"budget:[^,}]+", fn)
            self.assertTrue(pushes and all("toDisp" not in p and "fxRate" not in p for p in pushes), name)
        self.assertNotIn(EM, page[page.index("// ---- display currency"):page.index("async function loadFx")])


if __name__ == "__main__":
    unittest.main()
