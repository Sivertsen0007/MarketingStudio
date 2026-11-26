"""Google Ads rule checks (core.check / core.google_rules / gae.plan_rows + gae.lint): what blocks, what only warns.

T01 option B: Google's hard limits and clearly invalid input block (problems); style points (CAPS, "!!", double
spaces, duplicates of live rows, >3x jumps, no locations) are warnings. Also the v0.2.3 fixes G1 (the RSA dialog's
{text, pin} descriptions), G2 (Editor dry-run results kept apart; API-grid RSA edits by Google ad id), G8 (messages
name the field) and G15 (an empty bulk add explains itself).

Run from the engine folder:  python3 -m unittest tests/test_google_rules.py
A temp MSTUDIO_HOME, no Keychain, no Editor; nothing here reaches Google.
"""
import os as _os, tempfile as _tf
_os.environ["MSTUDIO_HOME"] = _tf.mkdtemp(prefix="mstudio-test-")
_os.environ["MSTUDIO_KEYCHAIN"] = "0"
_os.environ["GAE_EDITOR_DIR"] = _tf.mkdtemp(prefix="gae-editor-test-")

import re
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _legacy_copy_rules  # noqa: E402
import core  # noqa: E402
import gae  # noqa: E402
import live  # noqa: E402

A = "5555555555"
EM = "—"


def _rules(level, early=None):
    return [(re.compile(r["pattern"], re.I), r["message"]) for r in _legacy_copy_rules.RULES
            if r["level"] == level and set(r["areas"]) & {"google_ads", "ads"} and (early is None or bool(r.get("early")) == early)]


class Base(unittest.TestCase):
    """The old built-in Google Ads copy rules switched on (em dash, Bahamas block; guarantee, cabins warn)."""

    def setUp(self):
        core._KNOWN.clear()
        p1 = mock.patch.object(gae, "_block_rules", lambda: (_rules("block", True), _rules("block", False)))
        p2 = mock.patch.object(core, "copy_warn_rules", lambda: _rules("warn"))
        p3 = mock.patch.object(core, "_known_rows", lambda acct, kind: self.known.get(kind, []) if acct else [])
        self.known = {}
        for p in (p1, p2, p3):
            p.start()
            self.addCleanup(p.stop)

    def check(self, change, acct=None):
        return core.check(change, acct)

    def blocks(self, change, needle, acct=None):
        p, _ = self.check(change, acct)
        self.assertTrue(any(needle in x for x in p), f"expected a problem with {needle!r}, got {p}")

    def warns(self, change, needle, acct=None):
        p, w = self.check(change, acct)
        self.assertTrue(any(needle in x for x in w), f"expected a warning with {needle!r}, got {w} (problems {p})")
        self.assertFalse(any(needle in x for x in p), f"{needle!r} must not block: {p}")

    def clean(self, change, acct=None):
        p, w = self.check(change, acct)
        self.assertEqual(p, [], p)
        return w


def rsa(**kw):
    c = {"type": "rsa", "campaign": "[1] Brand", "ad_group": "Core", "final_url": "https://example.com/trips",
         "headlines": ["Liveaboard trips", "Dive the Red Sea", "Book your space"], "descriptions": ["Small groups.", "Expert guides."]}
    c.update(kw)
    return c


def dialog(texts, pins=None):
    """The RSA dialog's shape: every line is {text, pin} (index.html rsaDialog)."""
    return [{"text": t, "pin": (pins or {}).get(i, 0)} for i, t in enumerate(texts)]


class RsaDialogShape(Base):
    """G1: descriptions from the dialog are {text, pin} dicts - unwrap them like headlines."""

    LONG = "Seven nights on board with three dives a day, nitrox included and small groups always"  # 85 chars

    def test_long_description_in_dialog_shape_is_not_blocked(self):
        self.assertEqual(len(self.LONG), 85)
        c = rsa(headlines=dialog(["Liveaboard trips", "Dive the Red Sea", "Book your space"]), descriptions=dialog([self.LONG, "Expert guides."]))
        self.clean(c)

    def test_banned_words_in_dialog_descriptions_block(self):
        self.blocks(rsa(descriptions=dialog(["Sail to the Bahamas this winter.", "Expert guides."])), "Description 1: mentions Bahamas")
        self.blocks(rsa(descriptions=dialog([f"Small groups {EM} big reefs.", "Expert guides."])), "Description 1: contains an em dash")
        self.warns(rsa(descriptions=dialog(["Guaranteed whale sharks.", "Expert guides."])), "Description 1: never guarantee")

    def test_description_over_90_blocks_with_field_name(self):
        self.blocks(rsa(descriptions=dialog(["x" * 91, "Expert guides."])), "Description 1: 91 chars > 90")

    def test_pin_goes_to_description_position(self):
        rows = gae.plan_rows(rsa(descriptions=dialog(["One.", "Two."], {1: 2}), headlines=dialog(["A one", "B two", "C three"], {0: 1})))
        self.assertEqual(rows[0]["Description 1"], "One.")
        self.assertEqual(rows[0]["Description 2 position"], "2")
        self.assertNotIn("Description 1 position", rows[0])
        self.assertEqual(rows[0]["Headline 1 position"], "1")

    def test_edit_in_dialog_shape(self):
        c = rsa(edit=True, local_id=7, headlines=dialog(["A one", "B two", "C three"]), descriptions=dialog([self.LONG, "Two."], {0: 1}))
        c.pop("final_url")
        self.clean(c)
        self.assertEqual(gae.plan_rows(c)[0]["Description 1 position"], "1")


class RsaRules(Base):
    def test_counts_new_and_edit(self):
        self.blocks(rsa(headlines=["One", "Two"]), "headlines: an RSA needs 3-15 (got 2)")
        self.blocks(rsa(descriptions=["Only one."]), "descriptions: an RSA needs 2-4 (got 1)")
        self.blocks(rsa(edit=True, final_url=None, headlines=["One", "Two"], descriptions=["A.", "B."]), "headlines: an RSA needs 3-15")
        self.blocks(rsa(edit=True, final_url=None, descriptions=None), "both headlines and descriptions")

    def test_empty_and_duplicate_texts(self):
        self.blocks(rsa(headlines=["One", " ", "Three"]), "Headline 2: is empty")
        self.blocks(rsa(headlines=["Same text", "Other", "same  TEXT"]), "Headline 3: the same text is used twice")

    def test_final_url_and_paths(self):
        self.blocks(rsa(final_url="example.com"), "final_url")
        self.blocks(rsa(final_url="https://exa mple.com"), "final_url")
        self.blocks(rsa(final_url=None), "final_url is missing")
        self.blocks(rsa(path2="egypt"), "Path 2 needs Path 1")
        self.blocks(rsa(path1="x" * 16), "Path 1: 16 chars > 15")
        self.clean(rsa(path1="red-sea", path2="egypt"))
        self.warns(rsa(path1="red sea"), "Path 1: a space in a display path")
        self.clean({"type": "rsa", "edit": True, "campaign": "C", "ad_group": "G", "local_id": 3, "status": "paused"})

    def test_pins(self):
        self.blocks(rsa(headlines=dialog(["A one", "B two", "C three"], {0: 4})), "Headline 1 position: pin must be 1-3")
        self.blocks(rsa(descriptions=dialog(["A.", "B."], {1: 3})), "Description 2 position: pin must be 1-2")

    def test_emoji_blocks_style_warns(self):
        self.blocks(rsa(headlines=["Dive now \U0001F420", "Two", "Three"]), "Headline 1: emoji")
        self.warns(rsa(headlines=["BOOKING today", "Two", "Three"]), "Headline 1: written in CAPITALS")
        for ok in ("BOOK now", "PADI courses", "SCUBA trips", "UK and USA", "NAUI SSI EU"):
            self.assertFalse(any("CAPITALS" in w for w in self.clean(rsa(headlines=[ok, "Two", "Three"]))), ok)
        self.warns(rsa(descriptions=["Amazing!!", "Two."]), "Description 1: repeated punctuation")
        self.blocks(rsa(headlines=["Dive now!", "Two", "Three"]), "Headline 1: Google does not allow an exclamation mark")
        self.clean(rsa(descriptions=["Dive now!", "Two."]))
        self.warns(rsa(descriptions=["Small  groups.", "Two."]), "Description 1: double space")

    def test_status(self):
        self.blocks({"type": "rsa", "edit": True, "campaign": "C", "ad_group": "G", "status": "deleted"}, "status: must be")


class KeywordRules(Base):
    def kw(self, *kws, **extra):
        return {"type": "keyword", "campaign": "C", "ad_group": "G", "match": "phrase", "keywords": list(kws), **extra}

    def test_hard_limits(self):
        self.blocks(self.kw(""), "an empty keyword")
        self.blocks(self.kw("x" * 81), "81 chars > 80")
        self.blocks(self.kw("one two three four five six seven eight nine ten eleven"), "11 words > 10")
        self.blocks(self.kw("dive!"), "does not allow !")
        self.blocks(self.kw("red sea, egypt"), "does not allow ,")
        self.blocks(self.kw("[red sea]"), "match type syntax")
        self.blocks(self.kw('"red sea"'), "match type syntax")
        self.blocks(self.kw("dive", "Dive "), "listed twice")
        self.blocks(self.kw("dive", match="fuzzy"), "match: must be exact, phrase or broad")
        self.blocks(self.kw("dive", max_cpc=0), "max_cpc: must be more than 0")
        self.blocks(self.kw("dive", max_cpc=-1), "max_cpc: must be more than 0")
        self.clean(self.kw("red sea liveaboard", "dive & sail", max_cpc=1.2))

    def test_edits_and_removes_take_existing_keywords_as_they_are(self):
        # verifier repro: legacy broad match modifier keywords / negatives must stay editable and removable
        bmm = dict(self.kw("+liveaboard +egypt", match="broad"), edit=True)
        for extra in ({"status": "paused"}, {"max_cpc": 1.5}, {"status": "enabled"}):
            self.clean({**bmm, **extra})
        rm = {"type": "negative", "remove": True, "campaign": "C", "match": "broad", "keywords": ["+cheap", "[x]", "a, b"]}
        self.clean(rm)
        self.blocks({**rm, "keywords": [" "]}, "an empty keyword")
        self.blocks(dict(self.kw("+liveaboard"), edit=True, keywords=[""], status="paused"), "an empty keyword")
        self.blocks(self.kw("+liveaboard +egypt"), "remove the leading '+'")  # adding stays strict
        item = {"id": "k1", "change": {**bmm, "status": "paused"}, "by": "you", "summary": "s", "problems": [], "warnings": []}
        neg = {"id": "n1", "change": rm, "by": "you", "summary": "s", "problems": [], "warnings": []}
        with mock.patch.object(live, "ops_for", lambda *a: []), mock.patch.object(live, "_plan_experiments", lambda *a: live.experiments.empty()):
            p = live.plan(A, items=[item, neg], source="api")
        self.assertEqual([e["problems"] for e in p["items"]], [[], []])

    def test_live_duplicates_and_jumps_warn(self):
        self.known = {"keywords": [{"campaign": "C", "ad_group": "G", "keyword": "red sea", "match": "Phrase", "status": "Enabled", "max_cpc": 1.0}]}
        self.warns(self.kw("red sea"), 'Keyword "red sea" (phrase) is already in G', acct=A)
        self.warns(dict(self.kw("red sea", max_cpc=4), edit=True), "more than 3x", acct=A)
        self.clean(dict(self.kw("red sea", max_cpc=2), edit=True), acct=A)

    def test_negatives(self):
        neg = {"type": "negative", "campaign": "C", "match": "phrase", "keywords": ["cheap"]}
        self.blocks({**neg, "keywords": ["-cheap"]}, "remove the leading '-'")
        self.blocks({**neg, "keywords": [" "]}, "an empty keyword")
        self.known = {"keywords": [{"campaign": "C", "ad_group": "G", "keyword": "cheap liveaboard", "match": "Exact", "status": "Enabled"}],
                      "negatives": [{"campaign": "C", "ad_group": "", "keyword": "free", "match": "Phrase", "status": "Enabled", "level": "Campaign"}]}
        self.warns(neg, "blocks live keywords: cheap liveaboard", acct=A)
        self.warns({**neg, "keywords": ["free"]}, "already there", acct=A)
        self.assertEqual(self.check({**neg, "keywords": ["cheap"], "match": "exact"}, A)[1], [])
        self.assertEqual(self.check({**neg, "remove": True}, A), ([], []))


class ExtensionRules(Base):
    def test_sitelinks(self):
        sl = {"type": "sitelink", "campaign": "C", "text": "Our trips", "final_url": "https://example.com/t"}
        self.clean(sl)
        self.clean({**sl, "line1": "Seven nights", "line2": "Red Sea"})
        self.blocks({**sl, "line1": "Seven nights"}, "give both or neither")
        self.blocks({**sl, "final_url": "nope"}, "final_url")
        self.blocks({**sl, "text": "x" * 26}, "Link Text: 26 chars > 25")
        self.blocks({k: v for k, v in sl.items() if k != "final_url"}, "final_url is missing")

    def test_callouts(self):
        self.blocks({"type": "callout", "campaign": "C", "texts": ["Free nitrox", "free  nitrox"]}, "listed twice")
        self.blocks({"type": "callout", "campaign": "C", "texts": []}, "texts: give one or more")
        self.blocks({"type": "callout", "campaign": "C", "texts": ["x" * 26]}, "Callout text: 26 chars > 25")

    def test_snippets(self):
        sn = {"type": "snippet", "campaign": "C", "header": "Destinations", "values": ["Egypt", "Maldives", "Indonesia"]}
        self.clean(sn)
        self.blocks({**sn, "header": "Places"}, "header: must be one of")
        self.blocks({**sn, "header": "destinations"}, 'header: write "Destinations"')
        self.blocks({**sn, "values": ["Egypt", "Maldives"]}, "values: a snippet needs 3-10 (got 2)")
        self.blocks({**sn, "values": ["Egypt"] * 2 + ["x" * 26]}, "Snippet value 3: 26 chars > 25")
        self.blocks({**sn, "values": ["Egypt", "egypt", "Oman"]}, "listed twice")
        self.blocks({k: v for k, v in sn.items() if k != "header"}, "header is missing")

    def test_prices(self):
        item = {"header": "Egypt", "description": "Seven nights", "price": 1200, "final_url": "https://example.com/e"}
        pr = {"type": "price", "campaign": "C", "price_type": "Tours", "items": [dict(item, header=h) for h in ("Egypt", "Oman", "Sudan")]}
        self.clean(pr)
        self.blocks({**pr, "items": pr["items"][:2]}, "items: a price asset needs 3-8 (got 2)")
        self.blocks({**pr, "items": pr["items"][:2] + [dict(item, header="Sudan", price=0)]}, "Item 3 price: must be more than 0")
        self.blocks({**pr, "items": pr["items"][:2] + [{"header": "Sudan"}]}, "Item 3 description is missing")
        self.blocks({"type": "price", "edit": True, "campaign": "C", "prices": {"Egypt": -5}}, 'price of "Egypt": must be more than 0')

    def test_promotion(self):
        pm = {"type": "promotion", "campaign": "C", "target": "Red Sea trips", "percent": 10, "final_url": "https://example.com/p"}
        self.clean(pm)
        p, _ = self.check({**pm, "target": "x" * 21})
        self.assertIn("Item: 21 chars > 20 ('" + "x" * 21 + "')", p)
        self.assertFalse(any("> 25" in x for x in self.check({**pm, "target": "x" * 26})[0]))
        self.blocks({**pm, "percent": 0}, "percent: must be 1-100")
        self.blocks({**pm, "percent": 150}, "percent: must be 1-100")

    def test_malformed_change_is_readable(self):
        p, _ = self.check({"type": "snippet", "campaign": "C"})
        self.assertFalse(any("KeyError" in x or x.startswith("invalid change: '") for x in p), p)


class CampaignRules(Base):
    NEW = {"type": "campaign", "new": True, "campaign": "[15] New", "budget": 20, "locations": [{"id": 2826}]}

    def test_new_campaign(self):
        self.clean(self.NEW)
        self.blocks({**self.NEW, "campaign": " "}, "campaign is missing")
        self.blocks({k: v for k, v in self.NEW.items() if k != "budget"}, "budget is missing")
        self.blocks({**self.NEW, "budget": 0}, "budget: must be more than 0")
        self.blocks({**self.NEW, "budget": "abc"}, "budget: 'abc' is not a number")
        self.blocks({**self.NEW, "max_cpc_cap": -2}, "max_cpc_cap: must be more than 0")
        self.blocks({**self.NEW, "start_date": "2026-12-10", "end_date": "2026-12-01"}, "is before start_date")
        self.blocks({**self.NEW, "bid_strategy": "Target ROAS"}, "bid_strategy")
        self.warns({k: v for k, v in self.NEW.items() if k != "locations"}, "no locations")

    def test_duplicate_name_blocks(self):
        self.known = {"campaigns": [{"campaign": "[15] new", "status": "Paused", "budget": 10}]}
        self.blocks(self.NEW, "already exists", acct=A)
        self.known = {"campaigns": [{"campaign": "[15] New", "status": "Removed"}]}
        self.clean(self.NEW, acct=A)

    def test_edits(self):
        self.known = {"campaigns": [{"campaign": "Brand", "status": "Enabled", "budget": 10}],
                      "ad_groups": [{"campaign": "Brand", "ad_group": "Core", "max_cpc": 1.0, "status": "Enabled"}]}
        self.warns({"type": "campaign", "campaign": "Brand", "budget": 31}, "budget: 31.00 is more than 3x the current 10.00", acct=A)
        self.assertEqual(self.check({"type": "campaign", "campaign": "Brand", "budget": 25}, A), ([], []))
        self.blocks({"type": "campaign", "campaign": "Brand", "budget": -1}, "budget: must be more than 0")
        self.blocks({"type": "campaign", "campaign": "Brand", "status": "removed"}, "status: must be enabled or paused")
        self.warns({"type": "ad_group", "campaign": "Brand", "ad_group": "Core", "max_cpc": 5}, "max_cpc: 5.00 is more than 3x", acct=A)
        self.blocks({"type": "ad_group", "campaign": "Brand", "ad_group": "Core", "max_cpc": 0}, "max_cpc: must be more than 0")
        self.blocks({"type": "ad_group", "new": True, "campaign": "Brand", "ad_group": "core"}, "already has an ad group", acct=A)

    def test_amounts_that_cannot_be_sent(self):
        self.blocks({**self.NEW, "budget": 0.004}, "budget: 0.004 rounds to 0.00")
        self.blocks({"type": "ad_group", "campaign": "B", "ad_group": "G", "max_cpc": 0.001}, "max_cpc: 0.001 rounds to 0.00")
        self.clean({"type": "ad_group", "campaign": "B", "ad_group": "G", "max_cpc": 0.005})
        for bad in (float("inf"), float("nan"), "inf", "nan"):
            self.blocks({**self.NEW, "budget": bad}, "is not a usable amount")

    def test_huge_and_bool_amounts(self):
        self.blocks({**self.NEW, "budget": 1e30}, "budget: 1e+30 is too large")
        self.blocks({**self.NEW, "budget": 1e15}, "is too large")
        self.blocks({**self.NEW, "budget": True}, "budget: True is not a number")
        self.blocks({"type": "ad_group", "campaign": "B", "ad_group": "G", "max_cpc": False}, "max_cpc: False is not a number")

    def test_micros_round_to_cents_first(self):
        self.assertEqual(gae.micros(1.005), 1010000)
        self.assertEqual(gae.micros(0.1 + 0.2), 300000)
        self.assertEqual(gae.micros("12.3456"), 12350000)


class CopyRulesOnlyOnNewText(Base):
    """Copy rules (Bahamas, em dash, ...) judge text a change adds or rewrites, never the name of what it points at."""

    def test_status_and_bid_edits_on_existing_names_pass(self):
        kw = {"type": "keyword", "edit": True, "campaign": "Bahamas trips", "ad_group": f"Islands {EM} all",
              "match": "phrase", "keywords": ["bahamas liveaboard"]}
        self.clean({**kw, "status": "paused"})
        self.clean({**kw, "max_cpc": 1.1})
        self.clean({"type": "negative", "remove": True, "campaign": "Bahamas trips", "match": "phrase", "keywords": ["bahamas cheap"]})
        self.clean({"type": "campaign", "campaign": "Bahamas trips", "status": "paused"})
        self.clean({"type": "ad_group", "campaign": "Bahamas trips", "ad_group": "Bahamas", "max_cpc": 1.0})
        self.clean({"type": "rsa", "edit": True, "campaign": "Bahamas trips", "ad_group": "Bahamas", "local_id": 3, "status": "paused"})
        self.assertFalse(any("cabins" in w for w in self.check({**kw, "keywords": ["cabins"], "status": "paused"})[1]))

    def test_new_and_rewritten_text_is_still_judged(self):
        self.blocks({"type": "keyword", "campaign": "C", "ad_group": "G", "match": "phrase", "keywords": ["bahamas liveaboard"]},
                    "Keyword: mentions Bahamas")
        self.blocks({"type": "negative", "campaign": "C", "match": "phrase", "keywords": ["bahamas"]}, "Keyword: mentions Bahamas")
        self.blocks({"type": "campaign", "new": True, "campaign": "Bahamas 2027", "budget": 5, "locations": [{"id": 1}]},
                    "Campaign: mentions Bahamas")
        self.blocks({"type": "ad_group", "new": True, "campaign": "C", "ad_group": "Bahamas"}, "Ad Group: mentions Bahamas")
        self.blocks(rsa(campaign="Bahamas trips", edit=True, final_url=None, descriptions=["Sail the Bahamas.", "Two."]),
                    "Description 1: mentions Bahamas")
        self.blocks(rsa(final_url="https://example.com/bahamas"), "Final URL: mentions Bahamas")


class Messages(Base):
    """G8: the column stays in the message."""

    def test_field_named(self):
        p, _ = self.check(rsa(headlines=["x" * 31, "Two", "Three"]))
        self.assertIn("Headline 1: 31 chars > 30 ('" + "x" * 31 + "')", p)
        p, _ = self.check({"type": "pmax_asset", "campaign": "C", "asset_group": "AG", "field": "headline", "texts": ["y" * 31]})
        self.assertTrue(any(x.startswith("Headline 1: 31 chars > 30") for x in p), p)


class DraftFlow(Base):
    def test_empty_add_explains(self):  # G15
        with self.assertRaises(ValueError) as cm:
            core.add(A, [])
        self.assertEqual(str(cm.exception), "Nothing to add: there were no changes")
        shared = {"type": "negative", "remove": True, "campaign": "", "level": "Shared list", "list": "Brand negatives",
                  "match": "phrase", "keywords": ["free"]}
        with self.assertRaises(ValueError) as cm:
            core.add(A, [shared])
        self.assertIn("edit the shared list itself", str(cm.exception))
        self.assertEqual(core.draft(A)["items"], [])

    def test_mixed_selection_reports_skipped_shared_list_negatives(self):
        shared = {"type": "negative", "remove": True, "campaign": "", "level": "Shared list", "list": "Brand negatives",
                  "match": "phrase", "keywords": ["free"]}
        own = {"type": "negative", "remove": True, "campaign": "C", "match": "phrase", "keywords": ["cheap"]}
        items = core.add(A, [shared, own])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["change"], own)
        self.assertEqual(len(items[0]["skipped"]), 1)
        self.assertIn("'Brand negatives'", items[0]["skipped"][0])
        self.assertTrue(any(w.startswith("Not added: 1 negative in a shared list") for w in items[0]["warnings"]))
        stored = core.draft(A)["items"]
        self.assertEqual(len(stored), 1)
        self.assertNotIn("skipped", stored[0])
        self.assertFalse(any("Not added" in w for w in stored[0]["warnings"]))
        core.clear(A)

    def test_duplicate_new_names_in_one_draft_block(self):
        camp = {"type": "campaign", "new": True, "campaign": "[20] Twin", "budget": 5, "locations": [{"id": 2826}]}
        ag = {"type": "ad_group", "new": True, "campaign": "[20] Twin", "ad_group": "Core"}
        first = core.add(A, [camp, ag])
        self.assertEqual([i["problems"] for i in first], [[], []])
        second = core.add(A, [{**camp, "campaign": "[20] twin "}, {**ag, "ad_group": "core"}, {**ag, "ad_group": "Other"}])
        self.assertIn("already has a new campaign", second[0]["problems"][0])
        self.assertIn("already has a new ad group", second[1]["problems"][0])
        self.assertEqual(second[2]["problems"], [])
        self.assertEqual(core.draft_duplicates([camp, camp, ag, dict(ag, campaign="Other")]),
                         [[], ['the draft already has a new campaign called "[20] Twin"'], [], []])
        with mock.patch.object(live, "ops_for", lambda *a: []), mock.patch.object(live, "_plan_experiments", lambda *a: live.experiments.empty()):
            p = live.plan(A, items=core.draft(A)["items"], source="api")
        self.assertEqual(sum(1 for e in p["items"] if any("the draft already has" in x for x in e["problems"])), 2)
        core.clear(A)

    def test_validate_keeps_editor_problems_apart_and_live_plan_ignores_them(self):  # G2
        items = core.add(A, [{"type": "campaign", "campaign": "Brand", "status": "paused"}])
        self.assertEqual(items[0]["problems"], [])
        tmp = Path(_tf.mkdtemp())
        (tmp / f"ape_{A}.db").write_bytes(b"")
        with mock.patch.object(gae, "editor_version_dir", lambda: tmp), \
                mock.patch.object(gae, "apply_changes", side_effect=ValueError('campaign "Brand": not found in Editor')):
            res = core.validate(A)
        self.assertFalse(res[0]["ok"])
        self.assertNotIn("own_problems", res[0])
        it = core.draft(A)["items"][0]
        self.assertEqual(it["problems"], [])
        self.assertEqual(it["editor_problems"], ['campaign "Brand": not found in Editor'])
        self.assertIn('Send to Editor: campaign "Brand": not found in Editor', it["warnings"])

        seen = []

        def ops(ids, c, enable_new):
            seen.append(c)
            return []
        with mock.patch.object(live, "ops_for", ops), mock.patch.object(live, "_plan_experiments", lambda *a: live.experiments.empty()):
            p = live.plan(A, items=core.draft(A)["items"], source="api")
        self.assertEqual(p["items"][0]["problems"], [])
        self.assertEqual(len(seen), 1)
        core.clear(A)

    def test_live_plan_still_rechecks_with_core_check(self):
        bad = {"id": "x1", "change": {"type": "campaign", "campaign": "Brand", "budget": 0}, "by": "you", "summary": "s",
               "problems": [], "warnings": []}
        with mock.patch.object(live, "ops_for", lambda *a: []), mock.patch.object(live, "_plan_experiments", lambda *a: live.experiments.empty()):
            p = live.plan(A, items=[bad], source="api")
        self.assertIn("budget: must be more than 0 (got 0)", p["items"][0]["problems"])


class ApiGridRsaEdit(unittest.TestCase):
    """G2: an RSA edited from an API grid carries Google's ad id; Send to Editor maps it through remoteId."""

    def db(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.executescript("""
            create table GeoLocation (remoteId integer, locationShortName text, countryCode text);
            create table Campaign (localId integer primary key, name text, status integer, trialParentId integer);
            create table AdGroup (localId integer primary key, name text, parentId integer, status integer);
            create table ResponsiveSearchAd (localId integer primary key, parentId integer, status integer, remoteId integer,
                                             state integer, preAdvisoryState integer, status_revert integer);
            insert into Campaign values (1, 'Brand', 0, 0);
            insert into AdGroup values (5, 'Core', (2 << 32) | 1, 0);
            insert into ResponsiveSearchAd values (11, (4 << 32) | 5, 0, 777001, 0, null, null);
            insert into ResponsiveSearchAd values (12, (4 << 32) | 5, 0, 777002, 0, null, null);
        """)
        return db

    def test_ad_id_maps_via_remote_id(self):
        db = self.db()
        edits = []
        # local_id is the API row's id (= the Google ad id), which Editor does not know as a localId
        n = gae.apply_changes(db, [{"type": "rsa", "edit": True, "campaign": "Brand", "ad_group": "Core", "local_id": 777002,
                                    "ad_id": 777002, "status": "paused"}], "test", edits)
        self.assertEqual(n["rsa_edit"], 1)
        self.assertEqual(db.execute("select status from ResponsiveSearchAd where localId = 12").fetchone()[0], 1)
        self.assertEqual(db.execute("select status from ResponsiveSearchAd where localId = 11").fetchone()[0], 0)

    def test_unknown_ad_id_is_a_clear_error(self):
        with self.assertRaises(ValueError) as cm:
            gae.apply_changes(self.db(), [{"type": "rsa", "edit": True, "campaign": "Brand", "ad_group": "Core", "ad_id": 9,
                                           "status": "paused"}], "test", [])
        self.assertIn("ad id 9", str(cm.exception))

    def test_local_id_still_works(self):
        db = self.db()
        gae.apply_changes(db, [{"type": "rsa", "edit": True, "campaign": "Brand", "ad_group": "Core", "local_id": 11, "status": "paused"}],
                          "test", [])
        self.assertEqual(db.execute("select status from ResponsiveSearchAd where localId = 11").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
