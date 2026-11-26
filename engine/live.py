"""Ads Studio -> Google Ads API, straight from the Mac. Turns the draft (gae plan changes) into
googleAds:mutate operations, previews them with validateOnly (Google checks everything, applies nothing),
and only applies when the user presses Push live and confirms.

Safety built in:
- new campaigns are always created PAUSED; new ad groups / keywords / ads PAUSED unless the user ticks
  "create as enabled" in the push dialog
- anything that can raise spend (enable, budget or bid raise, bid strategy change, removing a negative)
  is flagged and needs an extra tick
- live values are read from the API before the push and stored, so a push can be undone; removals of
  keywords / ads / ad groups are permanent in Google Ads and are flagged as such
- one atomic request (partialFailure off): all of it goes live or none of it

IDs come from Editor's database (remoteId = the Google Ads id, verified against the API 2026-10-26) or straight
from the API (see Ids: the account's read source goes first, the other is the fallback). Rows Editor has never
posted (remoteId 0) can't be referenced - post them first or create them in the draft.
"""
import concurrent.futures, copy, datetime as dt, json, os, re, sys, threading, urllib.error, urllib.request, uuid
from pathlib import Path

import appconfig  # noqa: E402,F401  (vendor_hub on sys.path)
import gads  # noqa: E402  (auth + developer token, shared with the SEM agent)

import core
import demandgen  # noqa: E402
import experiments  # noqa: E402
import progress  # noqa: E402

gae = core.gae
STATUS = {"enabled": "ENABLED", "paused": "PAUSED"}
MATCH = {"exact": "EXACT", "phrase": "PHRASE", "broad": "BROAD"}
LANG = {"en": 1000, "de": 1001, "fr": 1002, "es": 1003, "it": 1004, "nl": 1010, "da": 1009, "sv": 1015, "no": 1013}
LABEL = {"status": "status", "cpcBidMicros": "max CPC"}
RESULT_KEY = {"campaignBudgetOperation": "campaignBudgetResult", "campaignOperation": "campaignResult", "adGroupOperation": "adGroupResult",
              "adGroupCriterionOperation": "adGroupCriterionResult", "campaignCriterionOperation": "campaignCriterionResult",
              "adGroupAdOperation": "adGroupAdResult", "adOperation": "adResult", "assetOperation": "assetResult",
              "campaignAssetOperation": "campaignAssetResult", "customerAssetOperation": "customerAssetResult",
              "campaignSharedSetOperation": "campaignSharedSetResult", "assetGroupAssetOperation": "assetGroupAssetResult",
              "userListOperation": "userListResult"}


# ---------- batched live reads (one plan = one read cache) ----------
# The preview / push plan reads live values for every item. Instead of one query per item, _prefetch reads them up
# front with one GAQL query per resource type (an IN list, CHUNK ids per query) and puts each row in the plan's cache
# under the exact per-item query; ops_for then asks the per-item query and gets the cached row. A batch only ever adds
# rows it got back: anything it did not return (or a batch that fails) is read per item, as before.
BATCH_READS = True
CHUNK = 500  # ids per IN list
POOL = 4  # parallel queries at most

# kind -> (FROM resource, fields): the per-item "before" reads of ops_for
_LIVE = {
    "campaign": ("campaign", "campaign.status, campaign.campaign_budget, campaign.bidding_strategy_type, "
                             "campaign_budget.amount_micros, campaign_budget.explicitly_shared"),
    "campaign_aud": ("campaign", "campaign.targeting_setting.target_restrictions"),
    "ad_group": ("ad_group", "ad_group.status, ad_group.cpc_bid_micros"),
    "keyword": ("ad_group_criterion", "ad_group_criterion.status, ad_group_criterion.cpc_bid_micros"),
    "rsa": ("ad_group_ad", "ad_group_ad.status, ad_group_ad.ad.final_urls, ad_group_ad.ad.responsive_search_ad.headlines, "
                           "ad_group_ad.ad.responsive_search_ad.descriptions, ad_group_ad.ad.responsive_search_ad.path1, "
                           "ad_group_ad.ad.responsive_search_ad.path2"),
    "sitelink": ("asset", "asset.final_urls, asset.sitelink_asset.link_text, asset.sitelink_asset.description1, "
                          "asset.sitelink_asset.description2"),
}
# kind -> (fields, FROM resource, parent id field, rest of the WHERE, extra fields a batch also reads, live kind they fill):
# the per-parent id lists Ids resolves names from (cache key = kind + parent id)
_LISTS = {
    "kw": ("ad_group_criterion.criterion_id, ad_group_criterion.keyword.text, ad_group_criterion.keyword.match_type", "ad_group_criterion",
           "ad_group.id", "ad_group_criterion.type = KEYWORD AND ad_group_criterion.negative = FALSE AND ad_group_criterion.status != 'REMOVED'",
           "ad_group_criterion.resource_name, ad_group_criterion.status, ad_group_criterion.cpc_bid_micros", "keyword"),
    "neg_g": ("ad_group_criterion.criterion_id, ad_group_criterion.keyword.text, ad_group_criterion.keyword.match_type", "ad_group_criterion",
              "ad_group.id", "ad_group_criterion.type = KEYWORD AND ad_group_criterion.negative = TRUE AND ad_group_criterion.status != 'REMOVED'",
              "", None),
    "neg_c": ("campaign_criterion.criterion_id, campaign_criterion.keyword.text, campaign_criterion.keyword.match_type", "campaign_criterion",
              "campaign.id", "campaign_criterion.type = KEYWORD AND campaign_criterion.negative = TRUE AND campaign_criterion.status != 'REMOVED'",
              "", None),
    "rsa": ("ad_group_ad.ad.id", "ad_group_ad", "ad_group.id",
            "ad_group_ad.ad.type = RESPONSIVE_SEARCH_AD AND ad_group_ad.status != 'REMOVED'",
            "ad_group_ad.resource_name, ad_group_ad.status, ad_group_ad.ad.final_urls, ad_group_ad.ad.responsive_search_ad.headlines, "
            "ad_group_ad.ad.responsive_search_ad.descriptions, ad_group_ad.ad.responsive_search_ad.path1, "
            "ad_group_ad.ad.responsive_search_ad.path2", "rsa"),
    "loc": ("campaign_criterion.location.geo_target_constant", "campaign_criterion", "campaign.id",
            "campaign_criterion.type = LOCATION AND campaign_criterion.negative = FALSE AND campaign_criterion.status != 'REMOVED'", "", None),
}
_ROW = {"campaign": "campaign", "ad_group": "adGroup", "ad_group_criterion": "adGroupCriterion", "ad_group_ad": "adGroupAd", "asset": "asset",
        "campaign_criterion": "campaignCriterion"}
CAMPAIGNS_GAQL = "SELECT campaign.id, campaign.name FROM campaign WHERE campaign.status != 'REMOVED'"
AD_GROUPS_GAQL = ("SELECT ad_group.id, ad_group.name, campaign.id FROM ad_group "
                  "WHERE ad_group.status != 'REMOVED' AND campaign.status != 'REMOVED'")


def _live_gaql(kind, res):
    frm, sel = _LIVE[kind]
    return f"SELECT {sel} FROM {frm} WHERE {frm}.resource_name = '{res}'"


def _list_gaql(kind, pid):
    sel, frm, par, tail = _LISTS[kind][:4]
    return f"SELECT {sel} FROM {frm} WHERE {par} = {pid} AND {tail}"


def _chunks(xs):
    xs = sorted(xs)
    return [xs[i:i + CHUNK] for i in range(0, len(xs), CHUNK)]


_tls = threading.local()  # the plan's Ids while plan() asks for experiments, so those reads land in its cache


def _log(acct):
    core.STUDIO_DIR.mkdir(parents=True, exist_ok=True)
    return core.STUDIO_DIR / f"live_{acct}.jsonl"


# ---------- API ----------

def _post(acct, path, body):
    url = f"https://googleads.googleapis.com/{gads.g.API_VERSION}/customers/{acct}/{path}"
    req = urllib.request.Request(url, json.dumps(body).encode(), gads.headers())
    try:
        return True, json.loads(urllib.request.urlopen(req, timeout=120).read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            body = json.loads(raw)
            if isinstance(body, list):  # searchStream answers errors as a one-element array
                body = body[0]
            err = body["error"]
            errs = (err.get("details") or [{}])[0].get("errors")
            if not errs:
                return False, [{"message": err.get("message") or raw[:1500], "index": None}]
            return False, [{"message": x.get("message"), "code": x.get("errorCode"), "index": _op_index(x.get("location"))} for x in errs]
        except (KeyError, IndexError, ValueError, TypeError, AttributeError):
            return False, [{"message": raw[:1500], "index": None}]


def _op_index(loc):
    for el in (loc or {}).get("fieldPathElements", []):
        if el.get("fieldName") == "mutate_operations":
            return el.get("index")
    return None


def query(acct, gaql):
    ok, res = _post(acct, "googleAds:searchStream", {"query": gaql})
    if not ok:
        raise RuntimeError("Google Ads API: " + "; ".join(e["message"] for e in res))
    return [r for b in res for r in b.get("results", [])]


def mutate(acct, ops, apply):
    return _post(acct, "googleAds:mutate", {"mutateOperations": ops, "validateOnly": not apply, "partialFailure": False})


# ---------- draft names -> Google ids ----------

class NotFound(ValueError):
    """This source doesn't know the name: try the other source."""


class NotPosted(NotFound):
    """Editor has it, but only locally (never posted)."""


class Ids:
    """Resolve the draft's campaign / ad group / keyword / ad names to Google Ads ids.

    Two sources: Editor's database (remoteId = the Google Ads id) and the Google Ads API itself. The account's
    read source (gapi.source: "api" by default, or "editor") is asked first; when it doesn't know a name (or
    Editor's copy was never posted / is missing), the other one is asked. Ambiguous names stop at once.
    So drafts built from API rows work without Editor, and drafts built from Editor rows work as before."""

    def __init__(self, acct, source=None):
        self.acct = acct
        try:
            self.db = core._db(acct)
        except (ValueError, SystemExit):  # no Editor, or this account isn't downloaded in it
            self.db = None
        if source is None:
            import gapi  # noqa: E402 - gapi imports live
            source = gapi.source(acct)
        order = ["api", "editor"] if source == "api" else ["editor", "api"]
        self.order = [b for b in order if b == "api" or self.db is not None]
        self.new = {}  # names created earlier in this same push -> temporary resource names
        self._tmp = 0
        self._cache = {}
        self._reads = {}  # GAQL -> ("ok", rows) or ("err", exception): each distinct read happens once per plan

    def tmp(self, kind):
        self._tmp -= 1
        return f"customers/{self.acct}/{kind}/{self._tmp}"

    # -- plumbing --
    def _first(self, what, **fns):
        errs = []
        for b in self.order:
            try:
                return fns[b]()
            except NotFound as ex:
                errs.append(ex)
        if errs:
            raise ValueError(str(next((e for e in errs if isinstance(e, NotPosted)), errs[0])))
        raise ValueError(f"{what}: not found")

    def _q(self, key, gaql):
        if key not in self._cache:
            self._cache[key] = self._read(gaql)
        return self._cache[key]

    # -- the plan's read cache --
    def _read(self, gaql):
        hit = self._reads.get(gaql)
        if hit is None:
            try:
                hit = ("ok", query(self.acct, gaql))
            except Exception as ex:  # noqa: BLE001 - kept, so the same read fails the same way without asking again
                hit = ("err", ex)
            self._reads[gaql] = hit
        if hit[0] == "err":
            raise hit[1]
        return hit[1]

    def read(self, gaql):
        """query(acct, gaql), once per plan; a copy, so callers can't change what the next caller gets."""
        return copy.deepcopy(self._read(gaql))

    def prime(self, gaql, rows):
        """Rows a batch got for a per-item read (never replaces what was read already)."""
        self._reads.setdefault(gaql, ("ok", rows))

    def ok(self, gaql):
        hit = self._reads.get(gaql)
        return hit[1] if hit and hit[0] == "ok" else None

    def warm(self, gaqls):
        """Run the reads not cached yet: the first on its own (it also fetches the access token), the rest in a small pool."""
        todo = [g for g in dict.fromkeys(gaqls) if g not in self._reads]
        if not todo:
            return

        def run(g):
            try:
                return g, ("ok", query(self.acct, g))
            except Exception as ex:  # noqa: BLE001 - kept per read; a failed batch just means per-item reads
                return g, ("err", ex)
        got = [run(todo[0])]
        if len(todo) > 1:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(POOL, len(todo) - 1)) as pool:
                got += list(pool.map(run, todo[1:]))
        for g, hit in got:
            self._reads.setdefault(g, hit)

    def _live_batches(self, kind, ress):
        frm, sel = _LIVE[kind]
        ress = {r for r in ress if _live_gaql(kind, r) not in self._reads}
        return [(f"SELECT {frm}.resource_name, {sel} FROM {frm} WHERE {frm}.resource_name IN ({', '.join(repr(r) for r in part)})",
                 kind, set(part)) for part in _chunks(ress)]

    def _list_batches(self, kind, pids):
        sel, frm, par, tail, extra, _ = _LISTS[kind]
        pids = {str(p) for p in pids if f"{kind}{p}" not in self._cache}
        fields = ", ".join(x for x in (par, sel, extra) if x)
        return [(f"SELECT {fields} FROM {frm} WHERE {par} IN ({', '.join(part)}) AND {tail}", kind, set(part)) for part in _chunks(pids)]

    def _spread(self, batches, lists=False):
        """Batched rows -> the per-item cache entries (only what came back; the rest is read per item)."""
        for gaql, kind, want in batches:
            rows = self.ok(gaql)
            if not rows:
                continue
            if not lists:
                key = _ROW[_LIVE[kind][0]]
                for r in rows:
                    rn = (r.get(key) or {}).get("resourceName")
                    if rn in want:
                        self.prime(_live_gaql(kind, rn), [r])
                continue
            _, frm, par, _, _, fills = _LISTS[kind]
            pkey = _ROW[par.split(".")[0]]
            by = {}
            for r in rows:
                pid = str((r.get(pkey) or {}).get("id", ""))
                if pid in want:
                    by.setdefault(pid, []).append(r)
            for pid, rs in by.items():
                self._cache.setdefault(f"{kind}{pid}", rs)
                if fills:
                    for r in rs:
                        rn = (r.get(_ROW[frm]) or {}).get("resourceName")
                        if rn:
                            self.prime(_live_gaql(fills, rn), [{_ROW[frm]: r[_ROW[frm]]}])

    def batch(self, live=None, lists=None):
        """Read many per-item values at once. live: {kind: resource names}, lists: {kind: parent ids}."""
        lb = [b for k, v in (live or {}).items() for b in self._live_batches(k, v)]
        sb = [b for k, v in (lists or {}).items() for b in self._list_batches(k, v)]
        self.warm([b[0] for b in lb + sb])
        self._spread(sb, lists=True)
        self._spread(lb)

    def batch_live(self, kind, ress):
        self.batch(live={kind: ress})

    def _one(self, rows, what):
        """Editor rows -> the one row (old name kept: callers outside may use it)."""
        if not rows:
            raise NotFound(f"{what}: not found in Editor")
        if len(rows) > 1:
            raise ValueError(f"{what}: ambiguous in Editor")
        if not rows[0]["remoteId"]:
            raise NotPosted(f"{what}: exists only in Editor (not posted yet) - post it in Editor or add it to the draft")
        return rows[0]

    @staticmethod
    def _api_one(rows, what):
        if not rows:
            raise NotFound(f"{what}: not found in Google Ads")
        if len(rows) > 1:
            raise ValueError(f"{what}: ambiguous in Google Ads")
        return rows[0]

    def _ed_campaign(self, name):
        return self._one(self.db.execute("select * from Campaign where name = ? and status != 2", (name,)).fetchall(), f'campaign "{name}"')

    def _ed_ad_group(self, camp, name):
        c = self._ed_campaign(camp)
        return self._one(self.db.execute("select * from AdGroup where name = ? and parentId = ? and status != 2",
                                         (name, (gae.PARENT_CAMPAIGN << 32) | c["localId"])).fetchall(), f'ad group "{name}"')

    def _api_campaign(self, name):
        rows = self._q("campaigns", CAMPAIGNS_GAQL)
        return self._api_one([r["campaign"]["id"] for r in rows if r["campaign"]["name"] == name], f'campaign "{name}"')

    def _api_ad_group(self, camp, name):
        cid = self._api_campaign(camp)
        rows = self._q("ad_groups", AD_GROUPS_GAQL)
        return self._api_one([r["adGroup"]["id"] for r in rows if r["campaign"]["id"] == cid and r["adGroup"]["name"] == name],
                             f'ad group "{name}"')

    # -- what ops_for asks for; each returns Google ids --
    def campaign(self, name):
        """-> (resource name, ref). ref is None when the campaign is created earlier in this same push."""
        if ("c", name) in self.new:
            return self.new[("c", name)], None
        rid = self._first(f'campaign "{name}"', editor=lambda: self._ed_campaign(name)["remoteId"], api=lambda: self._api_campaign(name))
        return f"customers/{self.acct}/campaigns/{rid}", {"id": str(rid)}

    def ad_group(self, camp, name):
        if ("g", camp, name) in self.new:
            return self.new[("g", camp, name)], None
        rid = self._first(f'ad group "{name}"', editor=lambda: self._ed_ad_group(camp, name)["remoteId"],
                          api=lambda: self._api_ad_group(camp, name))
        return f"customers/{self.acct}/adGroups/{rid}", {"id": str(rid)}

    def criterion(self, table, parent_local, text, match):
        """Editor-only lookup, kept for callers outside this module."""
        return self._one(self.db.execute(f"select * from {table} where parentId = ? and text = ? and criterionType = ? and status != 2",
                                         (parent_local, text, gae.MATCH_CODE[match.lower()])).fetchall(), f'"{text}" ({match})')

    def keyword(self, camp, ad_group, text, match):
        what = f'"{text}" ({match})'

        def ed():
            g = self._ed_ad_group(camp, ad_group)
            return self.criterion("Keyword", (gae.PARENT_ADGROUP << 32) | g["localId"], text, match)["remoteId"]

        def api():
            gid = self._api_ad_group(camp, ad_group)
            rows = self._q(f"kw{gid}", _list_gaql("kw", gid))
            return self._api_one([r["adGroupCriterion"]["criterionId"] for r in rows if r["adGroupCriterion"]["keyword"]["text"] == text
                                  and r["adGroupCriterion"]["keyword"]["matchType"] == MATCH[match.lower()]], what)
        return self._first(what, editor=ed, api=api)

    def negative(self, camp, ad_group, text, match):
        what = f'negative "{text}" ({match})'

        def ed():
            if ad_group:
                parent = (gae.PARENT_ADGROUP << 32) | self._ed_ad_group(camp, ad_group)["localId"]
            else:
                parent = (gae.PARENT_CAMPAIGN << 32) | self._ed_campaign(camp)["localId"]
            return self.criterion("KeywordNegative", parent, text, match)["remoteId"]

        def api():
            if ad_group:
                gid = self._api_ad_group(camp, ad_group)
                rows = [r["adGroupCriterion"] for r in self._q(f"neg_g{gid}", _list_gaql("neg_g", gid))]
            else:
                cid = self._api_campaign(camp)
                rows = [r["campaignCriterion"] for r in self._q(f"neg_c{cid}", _list_gaql("neg_c", cid))]
            return self._api_one([r["criterionId"] for r in rows if r["keyword"]["text"] == text and r["keyword"]["matchType"] == MATCH[match.lower()]], what)
        return self._first(what, editor=ed, api=api)

    def rsa_ads(self, camp, ad_group, local_id=None, ad_id=None):
        """Google ad ids of the RSAs an edit targets. ad_id = Google's ad id. local_id = the grid row's id: Editor's local
        ad id for Editor rows, Google's ad id for API rows (both are tried, in source order)."""
        if ad_id not in (None, ""):
            return [str(ad_id)]
        what = f'RSA in "{ad_group}"'

        def ed():
            g = self._ed_ad_group(camp, ad_group)
            q, args = "select * from ResponsiveSearchAd where parentId = ? and status != 2", [(gae.PARENT_ADGROUP << 32) | g["localId"]]
            if local_id not in (None, ""):
                q, args = q + " and localId = ?", args + [int(local_id)]
            ads = self.db.execute(q, args).fetchall()
            if not ads:
                raise NotFound(f"no RSA found in \"{ad_group}\"" + (f" with Editor id {local_id}" if local_id not in (None, "") else ""))
            for ad in ads:
                if not ad["remoteId"]:
                    raise NotPosted("this ad exists only in Editor (not posted yet)")
            return [str(ad["remoteId"]) for ad in ads]

        def api():
            gid = self._api_ad_group(camp, ad_group)
            rows = self._q(f"rsa{gid}", _list_gaql("rsa", gid))
            got = [r["adGroupAd"]["ad"]["id"] for r in rows]
            if local_id not in (None, ""):
                got = [x for x in got if x == str(local_id)]
            if not got:
                raise NotFound(f'no RSA found in "{ad_group}"' + (f" with ad id {local_id}" if local_id not in (None, "") else "") + " in Google Ads")
            return got
        return self._first(what, editor=ed, api=api)

    def sitelink(self, level, camp, text):
        what = f'sitelink "{text}"'
        account = level == "account"

        def ed():
            parent = gae.ACCOUNT_PARENT if account else (gae.PARENT_CAMPAIGN << 32) | self._ed_campaign(camp)["localId"]
            rows = self.db.execute("select a.* from SitelinkV2Edge e join SitelinkV2 a on a.localId = e.sourceId where e.parentId = ? "
                                   "and e.status != 2 and a.status != 2 and a.displayText = ?", (parent, text)).fetchall()
            return self._one(rows, what)["remoteId"]

        def api():
            if account:
                rows = self._q("sl_acct", "SELECT asset.id, asset.sitelink_asset.link_text FROM customer_asset "
                                          "WHERE customer_asset.field_type = SITELINK AND customer_asset.status != 'REMOVED'")
            else:
                cid = self._api_campaign(camp)
                rows = self._q(f"sl{cid}", f"SELECT campaign.id, asset.id, asset.sitelink_asset.link_text FROM campaign_asset WHERE campaign.id = {cid} "
                                           "AND campaign_asset.field_type = SITELINK AND campaign_asset.status != 'REMOVED'")
            return self._api_one([r["asset"]["id"] for r in rows if r["asset"].get("sitelinkAsset", {}).get("linkText") == text], what)
        return self._first(what, editor=ed, api=api)

    def callout(self, level, camp, text):
        what = f'callout "{text}"'
        account = level == "account"

        def ed():
            parent = gae.ACCOUNT_PARENT if account else (gae.PARENT_CAMPAIGN << 32) | self._ed_campaign(camp)["localId"]
            rows = self.db.execute("select a.* from CalloutV2Edge e join CalloutV2 a on a.localId = e.sourceId where e.parentId = ? "
                                   "and e.status != 2 and a.status != 2 and a.displayText = ?", (parent, text)).fetchall()
            return self._one(rows, what)["remoteId"]

        def api():
            if account:
                rows = self._q("co_acct", "SELECT asset.id, asset.callout_asset.callout_text FROM customer_asset "
                                          "WHERE customer_asset.field_type = CALLOUT AND customer_asset.status != 'REMOVED'")
            else:
                cid = self._api_campaign(camp)
                rows = self._q(f"co{cid}", f"SELECT campaign.id, asset.id, asset.callout_asset.callout_text FROM campaign_asset WHERE campaign.id = {cid} "
                                           "AND campaign_asset.field_type = CALLOUT AND campaign_asset.status != 'REMOVED'")
            return self._api_one([r["asset"]["id"] for r in rows if r["asset"].get("calloutAsset", {}).get("calloutText") == text], what)
        return self._first(what, editor=ed, api=api)

    def location_targeted(self, camp, loc_id):
        """True if either source says the location is targeted (positive) in the campaign: a safety check, so both are asked."""
        hit = False
        if self.db is not None:
            try:
                c = self._ed_campaign(camp)
                hit = bool(self.db.execute("select 1 from GeoTarget where parentId = ? and locationId = ? and status != 2",
                                           ((gae.PARENT_CAMPAIGN << 32) | c["localId"], int(loc_id))).fetchone())
            except NotFound:
                pass
        if not hit:
            try:
                cid = self._api_campaign(camp)
            except NotFound:
                return False
            rows = self._q(f"loc{cid}", _list_gaql("loc", cid))
            hit = any(_id(r["campaignCriterion"]["location"]["geoTargetConstant"]) == str(loc_id) for r in rows)
        return hit

    def shared_set(self, name):
        what = f'shared list "{name}"'

        def api():
            rows = self._q("shared_sets", "SELECT shared_set.id, shared_set.name FROM shared_set "
                                          "WHERE shared_set.type = NEGATIVE_KEYWORDS AND shared_set.status = ENABLED")
            return self._api_one([r["sharedSet"]["id"] for r in rows if r["sharedSet"]["name"] == name], what)
        return self._first(what, editor=lambda: self._one(self.db.execute("select * from NegativeKeywordList where name = ?", (name,)).fetchall(),
                                                          what)["remoteId"], api=api)

    def user_list(self, name):
        what = f'audience list "{name}"'
        if ("user_list", name) in self.new:  # [audiences] Google can't point at a list made in the same push (checked live)
            raise ValueError(f'{what} is new in this push: push it first, then combine or attach it in the next push')

        def ed():
            r = self.db.execute("select remoteId from UserList where name = ?", (name,)).fetchall()
            if not r:
                raise NotFound(f"{what} not found in Editor")
            if len(r) > 1:
                raise ValueError(f"{what}: ambiguous in Editor")
            return r[0][0]

        def api():
            rows = self._q("user_lists", "SELECT user_list.id, user_list.name FROM user_list")
            return self._api_one([r["userList"]["id"] for r in rows if r["userList"]["name"] == name], what)
        return str(self._first(what, editor=ed, api=api))

    def asset_group(self, camp, name):
        """Performance Max asset group -> resource name (API only: Editor holds no ids we write through)."""
        cid = self._api_campaign_any(camp)
        rows = self._q(f"ag{cid}", f"SELECT asset_group.id, asset_group.name FROM asset_group WHERE campaign.id = {cid} "
                                   "AND asset_group.status != 'REMOVED'")
        try:
            aid = self._api_one([r["assetGroup"]["id"] for r in rows if r["assetGroup"]["name"] == name], f'asset group "{name}"')
        except NotFound as ex:
            raise ValueError(str(ex))
        return f"customers/{self.acct}/assetGroups/{aid}"

    def _api_campaign_any(self, camp):
        try:
            return self._api_campaign(camp)
        except NotFound as ex:
            raise ValueError(str(ex))

    def campaign_user_lists(self, camp_res):
        """User lists on a campaign: {user list id: (criterion id, bid modifier or None)}."""
        rows = self._q(f"aud{camp_res}", "SELECT campaign_criterion.criterion_id, campaign_criterion.user_list.user_list, "
                                         "campaign_criterion.bid_modifier FROM campaign_criterion WHERE campaign.resource_name = "
                                         f"'{camp_res}' AND campaign_criterion.type = USER_LIST AND campaign_criterion.negative = FALSE "
                                         "AND campaign_criterion.status != 'REMOVED'")
        return {_id(r["campaignCriterion"]["userList"]["userList"]): (r["campaignCriterion"]["criterionId"], r["campaignCriterion"].get("bidModifier"))
                for r in rows}


def _id(res):
    return res.rsplit("/", 1)[1]


# ---------- live "before" values ----------

def _read(src, gaql):
    """A live read: through the plan's cache when src is an Ids, straight from the API when it is an account id."""
    return src.read(gaql) if hasattr(src, "read") else query(getattr(src, "acct", src), gaql)


def _live(src, gaql, what):
    rows = _read(src, gaql)
    if not rows:
        raise ValueError(f"{what} not found live in Google Ads (removed there?) - Get recent changes in Editor")
    return rows[0]


def _money(m):
    return f"{int(m) / 1e6:.2f}" if m not in (None, "") else "-"


# ---------- draft change -> operations ----------
# Each op dict: {"op": <MutateOperation>, "undo": <MutateOperation or None>, "undo_note": str, "line": str,
#                "spend": str|None, "permanent": bool, "created": <result key> if it creates something}

def _upd(kind, res, fields, mask, before_fields, before_line, after_line, spend=None):
    return {"op": {kind: {"update": {"resourceName": res, **fields}, "updateMask": mask}},
            "undo": {kind: {"update": {"resourceName": res, **before_fields}, "updateMask": mask}},
            "line": f"{before_line} -> {after_line}", "spend": spend}


def _create(kind, body, line, undoable=True):
    return {"op": {kind: {"create": body}}, "undo": "remove-created" if undoable else None, "line": line, "created": RESULT_KEY[kind],
            "undo_note": "" if undoable else "stays in the account's asset library (unlinked on undo)"}


def _remove(kind, res, line, undo_create=None):
    return {"op": {kind: {"remove": res}}, "undo": {kind: {"create": undo_create}} if undo_create else None, "line": line,
            "permanent": undo_create is None}


def ops_for(ids, c, enable_new):
    a, t = ids.acct, c["type"]
    new_status = "ENABLED" if enable_new else "PAUSED"
    out = []

    if t == "campaign" and c.get("new"):
        budget = ids.tmp("campaignBudgets")
        camp = ids.tmp("campaigns")
        out.append(_create("campaignBudgetOperation", {"resourceName": budget, "name": f"{c['campaign']} budget",
                                                       "amountMicros": str(gae.micros(c["budget"])),
                                                       "deliveryMethod": "STANDARD", "explicitlyShared": False},
                           f"new budget {float(c['budget']):.2f}/day", undoable=False))
        bid = c.get("bid_strategy", "Manual CPC").lower()
        scheme = {"manualCpc": {}} if bid == "manual cpc" else \
            {"targetSpend": {"cpcBidCeilingMicros": str(gae.micros(c["max_cpc_cap"]))} if c.get("max_cpc_cap") else {}}
        out.append(_create("campaignOperation", {"resourceName": camp, "name": c["campaign"], "status": "PAUSED",
                                                 "advertisingChannelType": "SEARCH", "campaignBudget": budget, **scheme,
                                                 "networkSettings": {"targetGoogleSearch": True, "targetSearchNetwork": False,
                                                                     "targetContentNetwork": False},
                                                 "containsEuPoliticalAdvertising": "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING"},
                           f'new campaign "{c["campaign"]}" (PAUSED, {c.get("bid_strategy", "Manual CPC")})'))
        ids.new[("c", c["campaign"])] = camp
        for lang in str(c.get("languages", "en")).split(","):
            out.append(_create("campaignCriterionOperation", {"campaign": camp, "language": {"languageConstant": f"languageConstants/{LANG[lang.strip()]}"}},
                               f"language {lang.strip()}"))
        for loc in c.get("locations", []):
            out.append(_create("campaignCriterionOperation", {"campaign": camp, "location": {"geoTargetConstant": f"geoTargetConstants/{loc['id']}"}},
                               f"location {loc.get('name', loc['id'])}"))
        return out

    if t == "campaign":
        camp, row = ids.campaign(c["campaign"])
        live = _live(ids, _live_gaql("campaign", camp), f'campaign "{c["campaign"]}"')
        lc, lb = live["campaign"], live.get("campaignBudget", {})
        if c.get("status"):
            new = STATUS[c["status"].lower()]
            out.append(_upd("campaignOperation", camp, {"status": new}, "status", {"status": lc["status"]},
                            f"campaign status {lc['status']}", new, spend="enables a campaign" if new == "ENABLED" and lc["status"] != "ENABLED" else None))
        if c.get("budget") is not None:
            if lb.get("explicitlyShared"):
                raise ValueError(f'"{c["campaign"]}" uses a shared budget - change it in Google Ads')
            old, new = int(lb.get("amountMicros", 0)), gae.micros(c["budget"])
            pct = f" (+{(new - old) / old * 100:.0f}%)" if old and new > old else ""
            out.append(_upd("campaignBudgetOperation", lc["campaignBudget"], {"amountMicros": str(new)}, "amountMicros",
                            {"amountMicros": str(old)}, f"budget {_money(old)}/day", f"{_money(new)}/day",
                            spend=f"budget raise{pct}" if new > old else None))
        if c.get("bid_strategy"):
            tis = {"location": "TOP_OF_PAGE", "locationFractionMicros": str(int(float(c["tis_percent"]) * 10000)),
                   "cpcBidCeilingMicros": str(gae.micros(c["max_cpc_cap"]))}
            op = {"op": {"campaignOperation": {"update": {"resourceName": camp, "targetImpressionShare": tis},
                                               "updateMask": "targetImpressionShare.location,targetImpressionShare.locationFractionMicros,targetImpressionShare.cpcBidCeilingMicros"}},
                  "undo": None, "undo_note": f"was {lc['biddingStrategyType']} - switch back by hand if needed",
                  "line": f"bid strategy {lc['biddingStrategyType']} -> Target impression share {c['tis_percent']}% top, cap {c['max_cpc_cap']}",
                  "spend": "bid strategy change"}
            out.append(op)
        return out

    if t == "ad_group" and c.get("new"):
        camp, _ = ids.campaign(c["campaign"])
        g = ids.tmp("adGroups")
        body = {"resourceName": g, "name": c["ad_group"], "campaign": camp, "status": new_status, "type": "SEARCH_STANDARD"}
        if c.get("max_cpc"):
            body["cpcBidMicros"] = str(gae.micros(c["max_cpc"]))
        out.append(_create("adGroupOperation", body, f'new ad group "{c["ad_group"]}" ({new_status})'))
        ids.new[("g", c["campaign"], c["ad_group"])] = g
        return out

    if t == "ad_group":
        g, _ = ids.ad_group(c["campaign"], c["ad_group"])
        live = _live(ids, _live_gaql("ad_group", g), f'ad group "{c["ad_group"]}"')["adGroup"]
        if str(c.get("status", "")).lower() == "removed":
            return [_remove("adGroupOperation", g, f'remove ad group "{c["ad_group"]}"')]
        fields, before, mask, spend = {}, {}, [], []
        if c.get("status"):
            fields["status"], before["status"] = STATUS[c["status"].lower()], live["status"]
            mask.append("status")
            if fields["status"] == "ENABLED" and live["status"] != "ENABLED":
                spend.append("enables an ad group")
        if c.get("max_cpc") is not None:
            new, old = gae.micros(c["max_cpc"]), int(live.get("cpcBidMicros", 0))
            fields["cpcBidMicros"], before["cpcBidMicros"] = str(new), str(old)
            mask.append("cpcBidMicros")
            if new > old:
                spend.append("default bid raise")
        return [_upd("adGroupOperation", g, fields, ",".join(mask), before,
                     ", ".join(f"{LABEL[k]} {before[k] if k == 'status' else _money(before[k])}" for k in before),
                     ", ".join(f"{LABEL[k]} {fields[k] if k == 'status' else _money(fields[k])}" for k in fields), spend="; ".join(spend) or None)]

    if t == "keyword":
        g, grow = ids.ad_group(c["campaign"], c["ad_group"])
        for kw in c.get("keywords", [c.get("keyword")]):
            if not c.get("edit"):
                body = {"adGroup": g, "status": new_status, "keyword": {"text": kw, "matchType": MATCH[c["match"].lower()]}}
                if c.get("max_cpc"):
                    body["cpcBidMicros"] = str(gae.micros(c["max_cpc"]))
                out.append(_create("adGroupCriterionOperation", body, f'new keyword "{kw}" ({c["match"]}, {new_status})'))
                continue
            if grow is None:
                raise ValueError("can't edit keywords of an ad group created in the same push")
            res = f"customers/{a}/adGroupCriteria/{_id(g)}~{ids.keyword(c['campaign'], c['ad_group'], kw, c['match'])}"
            live = _live(ids, _live_gaql("keyword", res), f'keyword "{kw}"')["adGroupCriterion"]
            if str(c.get("status", "")).lower() == "removed":
                out.append(_remove("adGroupCriterionOperation", res, f'remove keyword "{kw}"'))
                continue
            fields, before, mask, spend = {}, {}, [], []
            if c.get("status"):
                fields["status"], before["status"] = STATUS[c["status"].lower()], live["status"]
                mask.append("status")
                if fields["status"] == "ENABLED" and live["status"] != "ENABLED":
                    spend.append("enables a keyword")
            if c.get("max_cpc") is not None:
                new, old = gae.micros(c["max_cpc"]), int(live.get("cpcBidMicros", 0))
                fields["cpcBidMicros"], before["cpcBidMicros"] = str(new), str(old)
                mask.append("cpcBidMicros")
                if new > old:
                    spend.append("bid raise")
            out.append(_upd("adGroupCriterionOperation", res, fields, ",".join(mask), before,
                            f'"{kw}" ' + ", ".join(f"{LABEL[k]} {before[k] if k == 'status' else _money(before[k])}" for k in before),
                            ", ".join(f"{LABEL[k]} {fields[k] if k == 'status' else _money(fields[k])}" for k in fields), spend="; ".join(spend) or None))
        return out

    if t == "negative":
        match = c.get("match", "phrase")
        if c.get("ad_group"):
            parent, pref = ids.ad_group(c["campaign"], c["ad_group"])
            kind, parent_field = "adGroupCriterionOperation", "adGroup"
        else:
            parent, pref = ids.campaign(c["campaign"])
            kind, parent_field = "campaignCriterionOperation", "campaign"
        for kw in c.get("keywords", [c.get("keyword")]):
            body = {parent_field: parent, "negative": True, "keyword": {"text": kw, "matchType": MATCH[match.lower()]}}
            if not c.get("remove"):
                out.append(_create(kind, body, f'new negative "{kw}" ({match})'))
                continue
            if pref is None:
                raise ValueError("can't remove negatives from something created in the same push")
            nid = ids.negative(c["campaign"], c.get("ad_group"), kw, match)
            coll = "adGroupCriteria" if parent_field == "adGroup" else "campaignCriteria"
            res = f"customers/{a}/{coll}/{_id(parent)}~{nid}"
            op = _remove(kind, res, f'remove negative "{kw}" ({match})', undo_create=body)
            op["spend"] = "removes a negative keyword"
            out.append(op)
        return out

    if t == "rsa" and not c.get("edit"):
        g, _ = ids.ad_group(c["campaign"], c["ad_group"])
        out.append(_create("adGroupAdOperation", {"adGroup": g, "status": new_status, "ad": {"finalUrls": [c["final_url"]], "responsiveSearchAd": {
            "headlines": [_asset(h, "HEADLINE") for h in c["headlines"]], "descriptions": [_asset(d, "DESCRIPTION") for d in c["descriptions"]],
            **({"path1": c["path1"]} if c.get("path1") else {}), **({"path2": c["path2"]} if c.get("path2") else {})}}},
            f'new RSA in "{c["ad_group"]}" ({len(c["headlines"])} headlines, {new_status})'))
        return out

    if t == "rsa":
        g, grow = ids.ad_group(c["campaign"], c["ad_group"])
        if grow is None:
            raise ValueError("can't edit ads of an ad group created in the same push")
        ad_ids = ids.rsa_ads(c["campaign"], c["ad_group"], c.get("local_id"), c.get("ad_id"))
        if c.get("headlines") and len(ad_ids) != 1:
            raise ValueError(f'"{c["ad_group"]}" has {len(ad_ids)} RSAs - text edits need exactly one')
        for rid in ad_ids:
            ad = {"remoteId": rid}
            res = f"customers/{a}/adGroupAds/{_id(g)}~{rid}"
            live = _live(ids, _live_gaql("rsa", res), "ad")["adGroupAd"]
            if c.get("status"):
                if c["status"].lower() == "removed":
                    out.append(_remove("adGroupAdOperation", res, f'remove RSA {ad["remoteId"]}'))
                    continue
                new = STATUS[c["status"].lower()]
                out.append(_upd("adGroupAdOperation", res, {"status": new}, "status", {"status": live["status"]},
                                f"RSA {ad['remoteId']} status {live['status']}", new,
                                spend="enables an ad" if new == "ENABLED" and live["status"] != "ENABLED" else None))
            fields, before, mask = {}, {}, []
            if c.get("final_url"):
                fields["finalUrls"], before["finalUrls"] = [c["final_url"]], live["ad"].get("finalUrls", [])
                mask.append("finalUrls")
            if c.get("headlines"):
                rsa = live["ad"].get("responsiveSearchAd", {})
                fields["responsiveSearchAd"] = {"headlines": [_asset(h, "HEADLINE") for h in c["headlines"]],
                                                "descriptions": [_asset(d, "DESCRIPTION") for d in c["descriptions"]]}
                before["responsiveSearchAd"] = {"headlines": [_clean(h) for h in rsa.get("headlines", [])],
                                                "descriptions": [_clean(d) for d in rsa.get("descriptions", [])]}
                mask += ["responsiveSearchAd.headlines", "responsiveSearchAd.descriptions"]
            if "path1" in c or "path2" in c:  # G9: display paths (an empty value clears it)
                rsa = live["ad"].get("responsiveSearchAd", {})
                fields.setdefault("responsiveSearchAd", {})
                before.setdefault("responsiveSearchAd", {})
                for k in ("path1", "path2"):
                    if k in c and (c[k] or "") != rsa.get(k, ""):
                        fields["responsiveSearchAd"][k], before["responsiveSearchAd"][k] = c[k] or "", rsa.get(k, "")
                        mask.append(f"responsiveSearchAd.{k}")
                if not fields["responsiveSearchAd"]:
                    del fields["responsiveSearchAd"], before["responsiveSearchAd"]
            if fields:
                adres = f"customers/{a}/ads/{ad['remoteId']}"
                out.append(_upd("adOperation", adres, fields, ",".join(mask), before,
                                f"RSA {ad['remoteId']}: " + ", ".join(mask),
                                "new " + ("text" if c.get("headlines") else "final URL" if c.get("final_url") else "display path")))
        return out

    if t in ("sitelink", "callout") and c.get("remove"):  # G10: unlink from the campaign (or account); undo links it again
        field = t.upper()
        account = c.get("level") == "account"
        camp = None if account else ids.campaign(c["campaign"])[0]
        where = "the account" if account else f'"{c["campaign"]}"'
        for txt in c.get("texts", [c.get("text")]):
            aid = (ids.sitelink if t == "sitelink" else ids.callout)(c.get("level"), c.get("campaign"), txt)
            asset = f"customers/{a}/assets/{aid}"
            if account:
                res, kind, undo = f"customers/{a}/customerAssets/{aid}~{field}", "customerAssetOperation", {"asset": asset, "fieldType": field}
            else:
                res, kind, undo = f"customers/{a}/campaignAssets/{_id(camp)}~{aid}~{field}", "campaignAssetOperation", \
                    {"campaign": camp, "asset": asset, "fieldType": field}
            out.append(_remove(kind, res, f'unlink {t} "{txt}" from {where}', undo_create=undo))
        return out

    if t in ("sitelink", "callout", "snippet") and not c.get("edit"):
        texts = c.get("texts", [c.get("text")]) if t == "callout" else [None]
        for txt in texts:
            a_res = ids.tmp("assets")
            if t == "sitelink":
                body = {"resourceName": a_res, "finalUrls": [c["final_url"]], "sitelinkAsset": {
                    "linkText": c["text"], **({"description1": c["line1"]} if c.get("line1") else {}),
                    **({"description2": c["line2"]} if c.get("line2") else {}),
                    **({"startDate": c["start_date"]} if c.get("start_date") else {}), **({"endDate": c["end_date"]} if c.get("end_date") else {})}}
                label, field = f'sitelink "{c["text"]}"', "SITELINK"
            elif t == "callout":
                body, label, field = {"resourceName": a_res, "calloutAsset": {"calloutText": txt}}, f'callout "{txt}"', "CALLOUT"
            else:
                body = {"resourceName": a_res, "structuredSnippetAsset": {"header": c["header"], "values": c["values"]}}
                label, field = f'snippet {c["header"]}', "STRUCTURED_SNIPPET"
            out.append(_create("assetOperation", body, "new asset: " + label, undoable=False))
            if c.get("level") == "account":
                out.append(_create("customerAssetOperation", {"asset": a_res, "fieldType": field}, f"link {label} to the account"))
            else:
                camp, _ = ids.campaign(c["campaign"])
                out.append(_create("campaignAssetOperation", {"campaign": camp, "asset": a_res, "fieldType": field}, f"link {label} to the campaign"))
        return out

    if t == "sitelink":
        res = f"customers/{a}/assets/{ids.sitelink(c.get('level'), c.get('campaign'), c['text'])}"
        live = _live(ids, _live_gaql("sitelink", res), "sitelink")["asset"]
        ls = live.get("sitelinkAsset", {})
        fields, before, mask = {"sitelinkAsset": {}}, {"sitelinkAsset": {}}, []
        for k, api in (("new_text", "linkText"), ("line1", "description1"), ("line2", "description2")):
            if c.get(k) is not None:
                fields["sitelinkAsset"][api], before["sitelinkAsset"][api] = c[k], ls.get(api, "")
                mask.append(f"sitelinkAsset.{api}")
        if c.get("final_url"):
            fields["finalUrls"], before["finalUrls"] = [c["final_url"]], live.get("finalUrls", [])
            mask.append("finalUrls")
        out.append(_upd("assetOperation", res, fields, ",".join(mask), before, f'sitelink "{c["text"]}": ' + ", ".join(m.split(".")[-1] for m in mask), "new values"))
        return out

    if t == "excluded_location":
        camp, crow = ids.campaign(c["campaign"])
        for loc in c["locations"]:
            if crow and ids.location_targeted(c["campaign"], loc["id"]):
                raise ValueError(f"location {loc['id']} is targeted in this campaign - remove the targeting before excluding it")
            out.append(_create("campaignCriterionOperation", {"campaign": camp, "negative": True,
                                                              "location": {"geoTargetConstant": f"geoTargetConstants/{loc['id']}"}},
                               f"exclude location {loc.get('name', loc['id'])}"))
        return out

    if t == "shared_list":
        camp, _ = ids.campaign(c["campaign"])
        for name in c["lists"]:
            out.append(_create("campaignSharedSetOperation", {"campaign": camp, "sharedSet": f"customers/{a}/sharedSets/{ids.shared_set(name)}"},
                               f'attach shared negative list "{name}"'))
        return out

    if t == "audience":
        return _audience_ops(ids, c)

    if t == "user_list":  # [audiences] website / combined lists (audiences.py)
        return core.audiences.ops(ids, c)
    if t == "pmax_asset":
        return _pmax_asset_ops(ids, c, enable_new)

    if demandgen.is_dg(c):
        return demandgen.ops(ids, c, enable_new)

    raise ValueError(f"'{t}' can't be pushed live yet - use Send to Editor for it")



def _audience_mode(a, camp, c):
    """Switch the campaign's audience setting: observation (bid only) or targeting (narrows reach). a: the plan's Ids
    (or an account id)."""
    lc = _live(a, _live_gaql("campaign_aud", camp), f'campaign "{c["campaign"]}"')["campaign"]
    restr = lc.get("targetingSetting", {}).get("targetRestrictions", [])
    cur = next((r for r in restr if r.get("targetingDimension") == "AUDIENCE"), None)
    old = ("observation" if cur.get("bidOnly") else "targeting") if cur else "observation"
    if old == c["mode"]:
        raise ValueError(f'"{c["campaign"]}" already uses audiences for {old}')
    new = [r for r in restr if r.get("targetingDimension") != "AUDIENCE"] + [{"targetingDimension": "AUDIENCE", "bidOnly": c["mode"] == "observation"}]
    return _upd("campaignOperation", camp, {"targetingSetting": {"targetRestrictions": new}}, "targetingSetting.targetRestrictions",
                {"targetingSetting": {"targetRestrictions": restr}}, f"audiences: {old}", c["mode"],
                spend="widens who the campaign reaches (targeting -> observation)" if c["mode"] == "observation" else None)


def _audience_ops(ids, c):
    """User lists on a campaign: attach (observation follows the campaign setting), detach, bid adjustment, mode switch."""
    a = ids.acct
    camp, cref = ids.campaign(c["campaign"])
    out = []
    if c.get("mode"):
        if cref is None:
            raise ValueError("set the audience mode once the campaign exists (next push)")
        out.append(_audience_mode(ids, camp, c))
    lists = c.get("lists") or []
    if not lists:
        return out
    on = ids.campaign_user_lists(camp) if cref is not None else {}
    targeting = False
    if c.get("remove") and cref is not None:
        restr = _live(ids, _live_gaql("campaign_aud", camp), "campaign")["campaign"].get("targetingSetting", {}).get("targetRestrictions", [])
        targeting = any(r.get("targetingDimension") == "AUDIENCE" and not r.get("bidOnly") for r in restr)
    for name in lists:
        ul = ids.user_list(name)
        ul_res = f"customers/{a}/userLists/{ul}"
        if c.get("remove") or c.get("edit"):
            if ul not in on:
                raise ValueError(f'audience "{name}" is not on "{c["campaign"]}"')
            crit, bm = on[ul]
            res = f"customers/{a}/campaignCriteria/{_id(camp)}~{crit}"
            if c.get("remove"):
                undo = {"campaign": camp, "userList": {"userList": ul_res}, **({"bidModifier": bm} if bm not in (None, "") else {})}
                op = _remove("campaignCriterionOperation", res, f'detach audience "{name}"', undo_create=undo)
                op["permanent"] = True
                op["undo_note"] = "undo attaches the list again; its stats on this campaign start over"
                if targeting:
                    op["spend"] = "changes who a targeting campaign reaches (no lists left = everyone)"
                out.append(op)
            else:
                new, old = 1 + float(c["bid_modifier"]) / 100, float(bm) if bm not in (None, "") else 1.0
                out.append(_upd("campaignCriterionOperation", res, {"bidModifier": round(new, 4)}, "bidModifier", {"bidModifier": old},
                                f'audience "{name}" bid {round((old - 1) * 100):+d}%', f"{round((new - 1) * 100):+d}%",
                                spend="audience bid raise" if new > old else None))
            continue
        if ul in on:
            raise ValueError(f'audience "{name}" is already on "{c["campaign"]}"')
        out.append(_create("campaignCriterionOperation", {"campaign": camp, "userList": {"userList": ul_res}}, f'audience "{name}"'))
    return out


def _pmax_asset_ops(ids, c, enable_new):
    """Performance Max asset group text assets: add (new text asset + link, Paused unless enable_new) or remove the link."""
    a = ids.acct
    ftype, _, minimum, maximum = core.PMAX_FIELDS[c["field"].lower()]
    label = c["field"].lower().replace("_", " ")
    camp, cref = ids.campaign(c["campaign"])
    if cref is None:
        raise ValueError("asset groups of a campaign created in the same push can't be edited")
    ag = ids.asset_group(c["campaign"], c["asset_group"])
    cur = _read(ids, "SELECT asset_group_asset.status, asset_group_asset.source, asset.id, asset.text_asset.text FROM asset_group_asset "
                   f"WHERE asset_group.resource_name = '{ag}' AND asset_group_asset.field_type = {ftype} "
                   "AND asset_group_asset.status != 'REMOVED'")
    have = {}
    for r in cur:
        have.setdefault(r["asset"].get("textAsset", {}).get("text", ""), r)
    texts = c.get("texts", [c.get("text")])
    out = []
    # earlier pmax_asset items of this same push on this asset group and field (a replace is an add plus a remove):
    # the minimum and maximum are checked on what the asset group holds after them, not on what it holds now
    led = ids.__dict__.setdefault("_pmax", {}).setdefault((ag, ftype), {"added": 0, "added_on": 0, "removed": 0, "removed_on": 0})
    if c.get("remove"):
        enabled = sum(1 for r in cur if r["assetGroupAsset"]["status"] == "ENABLED" and r["assetGroupAsset"].get("source") != "AUTOMATICALLY_CREATED")
        gone = 0
        for txt in texts:
            r = have.get(txt)
            if not r:
                raise ValueError(f'{label} "{txt}" is not in asset group "{c["asset_group"]}"')
            aid = r["asset"]["id"]
            if r["assetGroupAsset"]["status"] == "ENABLED" and r["assetGroupAsset"].get("source") != "AUTOMATICALLY_CREATED":
                gone += 1
            op = _remove("assetGroupAssetOperation", f"customers/{a}/assetGroupAssets/{_id(ag)}~{aid}~{ftype}", f'remove {label} "{txt}"',
                         undo_create={"assetGroup": ag, "asset": f"customers/{a}/assets/{aid}", "fieldType": ftype})
            op["permanent"] = True
            op["undo_note"] = "undo links the same text again; its history in the asset group starts over"
            out.append(op)
        left = enabled - led["removed_on"] + led["added_on"] - gone
        if left < minimum:
            raise ValueError(f'"{c["asset_group"]}" would keep {left} enabled {label}s; Performance Max needs at least {minimum}'
                             + (" (new texts go in paused unless you switch new items on)" if led["added"] and not enable_new else ""))
        led["removed_on"] += gone
        led["removed"] += sum(1 for t in texts if have[t]["assetGroupAsset"].get("source") != "AUTOMATICALLY_CREATED")
        return out
    mine = sum(1 for r in cur if r["assetGroupAsset"].get("source") != "AUTOMATICALLY_CREATED")
    mine += led["added"] - led["removed"]
    if mine + len(texts) > maximum:
        raise ValueError(f'"{c["asset_group"]}" has {mine} {label}s; Performance Max allows {maximum} - remove some first')
    status = "ENABLED" if enable_new else "PAUSED"
    for txt in texts:
        if txt in have:
            raise ValueError(f'{label} "{txt}" is already in asset group "{c["asset_group"]}"')
        a_res = ids.tmp("assets")
        out.append(_create("assetOperation", {"resourceName": a_res, "textAsset": {"text": txt}}, f'new text asset "{txt}"', undoable=False))
        out.append(_create("assetGroupAssetOperation", {"assetGroup": ag, "asset": a_res, "fieldType": ftype, "status": status},
                           f'add {label} "{txt}" to "{c["asset_group"]}" ({status})'))
    led["added"] += len(texts)
    led["added_on"] += len(texts) if enable_new else 0
    return out

def _asset(x, kind):
    x = x if isinstance(x, dict) else {"text": x}
    out = {"text": x["text"]}
    if x.get("pin"):
        out["pinnedField"] = f"{kind}_{int(x['pin'])}"
    return out


def _clean(x):
    return {k: x[k] for k in ("text", "pinnedField") if k in x}


# ---------- preview / push / undo ----------

def plan(acct, enable_new=False, items=None, source=None):
    """Build operations for the whole draft (or the given draft items) and let Google validate them (validateOnly).
    Applies nothing."""
    items = core.draft(acct)["items"] if items is None else items
    items = [i for i in items if i["change"].get("type") != "video_upload"]  # uploads run from their own button only
    ids = Ids(acct, source)
    ops, per_item = [], []
    exps = _plan_experiments(acct, ids, items) if items else experiments.empty()
    dups = core.draft_duplicates([it["change"] for it in items])  # two new campaigns / ad groups with one name
    for it, dup in zip(items, dups):
        xp, xw = experiments.check(exps, it["change"])
        # G2: start from core.check only. Stored problems may hold what Editor's dry run (Send to Editor) refused, which
        # says nothing about a live push; and a synced or merged draft never went through core.add at all.
        entry = {"id": it["id"], "summary": it["summary"], "by": it["by"], "lines": [], "spend": [], "permanent": [],
                 "problems": list(xp), "warnings": xw}
        try:
            again = core.check(it["change"], acct)[0] + core.audiences.refusals(acct, [it["change"]])
        except Exception as ex:  # noqa: BLE001 - a broken change is a refusal, never a crash
            again = [f"invalid change: {ex}"]
        entry["problems"] += [p for p in again + dup if p not in entry["problems"]]
        per_item.append(entry)
    if BATCH_READS:
        try:  # only fills the read cache: what it misses is read per item below, exactly as without it
            _prefetch(ids, [it["change"] for it, e in zip(items, per_item) if not e["problems"]])
        except Exception:  # noqa: BLE001
            pass
    for it, entry in zip(items, per_item):
        if not entry["problems"]:
            try:
                for o in ops_for(ids, it["change"], enable_new):
                    o["item"] = it["id"]
                    o["index"] = len(ops)
                    ops.append(o)
                    entry["lines"].append(o["line"])
                    if o.get("spend"):
                        entry["spend"].append(o["spend"])
                    if o.get("permanent"):
                        entry["permanent"].append(o["line"])
            except Exception as ex:  # noqa: BLE001 - shown per item in the preview
                entry["problems"].append(str(ex))
    if ops and not any(e["problems"] for e in per_item):
        ok, res = mutate(acct, [o["op"] for o in ops], apply=False)
        if not ok:
            for err in res:
                i = err.get("index")
                item = ops[i]["item"] if i is not None and i < len(ops) else None
                for e in per_item:
                    if e["id"] == item or item is None:
                        e["problems"].append(f"Google: {err['message']}")
                        if item is None:
                            break
    return {"items": per_item, "ops": ops, "ok": bool(ops) and not any(e["problems"] for e in per_item),
            "spend": any(e["spend"] for e in per_item), "permanent": any(e["permanent"] for e in per_item)}


def _plan_experiments(acct, ids, items):
    """Experiments for a plan, read once through the plan's cache, together (in parallel) with the name lists the plan
    will need. The campaign list experiments reads has the same filter as Ids' campaign name list, so it serves both."""
    if BATCH_READS:
        api_first = ids.order[:1] == ["api"]
        ids.warm([experiments.CAMPAIGNS_GAQL, experiments.ARMS_GAQL]
                 + ([AD_GROUPS_GAQL] if api_first and any(it["change"].get("ad_group") for it in items) else []))
    _tls.ids = ids
    try:
        exps = _experiments(acct)
    finally:
        _tls.ids = None
    if BATCH_READS:
        rows = ids.ok(experiments.CAMPAIGNS_GAQL)
        if rows is not None:
            ids._cache.setdefault("campaigns", rows)
        if ids.ok(AD_GROUPS_GAQL) is not None:
            ids._cache.setdefault("ad_groups", ids.ok(AD_GROUPS_GAQL))
    return exps


def _exp_query(acct, gaql):
    ids = getattr(_tls, "ids", None)
    return ids.read(gaql) if BATCH_READS and ids is not None and ids.acct == acct else query(acct, gaql)


def _prefetch(ids, changes):
    """Read the live values the changes need, a few batched queries instead of one per item (see BATCH_READS)."""
    a = ids.acct
    made_c = {c.get("campaign") for c in changes if c.get("type") == "campaign" and c.get("new")}
    made_g = {(c.get("campaign"), c.get("ad_group")) for c in changes if c.get("type") == "ad_group" and c.get("new")}
    changes = [c for c in changes if c.get("campaign") not in made_c and (c.get("campaign"), c.get("ad_group")) not in made_g]
    api_first = ids.order[:1] == ["api"]

    def safe(fn):
        try:
            return fn()
        except Exception:  # noqa: BLE001 - the item itself reports it
            return None

    def group(c):
        return safe(lambda: ids.ad_group(c["campaign"], c["ad_group"])[0])

    live, lists, later = {k: set() for k in _LIVE}, {k: set() for k in _LISTS}, []
    for c in changes:
        t = c.get("type")
        if t == "campaign" and not c.get("new"):
            r = safe(lambda: ids.campaign(c["campaign"])[0])
            r and live["campaign"].add(r)
        elif t == "ad_group" and not c.get("new") and c.get("ad_group"):
            g = group(c)
            g and live["ad_group"].add(g)
        elif t == "keyword" and c.get("edit"):
            g = group(c)
            if g:
                api_first and lists["kw"].add(_id(g))
                later.append((t, c, g))
        elif t == "rsa" and c.get("edit"):
            g = group(c)
            if g:
                if c.get("ad_id") in (None, "") and api_first:
                    lists["rsa"].add(_id(g))
                later.append((t, c, g))
        elif t == "negative" and c.get("remove") and api_first:
            if c.get("ad_group"):
                g = group(c)
                g and lists["neg_g"].add(_id(g))
            else:
                r = safe(lambda: ids.campaign(c["campaign"])[0])
                r and lists["neg_c"].add(_id(r))
        elif t == "excluded_location" and ids.db is None:
            r = safe(lambda: ids.campaign(c["campaign"])[0])
            r and lists["loc"].add(_id(r))
        elif t == "sitelink" and c.get("edit"):
            aid = safe(lambda: ids.sitelink(c.get("level"), c.get("campaign"), c["text"]))
            aid and live["sitelink"].add(f"customers/{a}/assets/{aid}")
        elif t == "audience" and (c.get("mode") or (c.get("remove") and c.get("lists"))):
            r = safe(lambda: ids.campaign(c["campaign"])[0])
            r and live["campaign_aud"].add(r)
    ids.batch(live=live, lists=lists)
    # keywords and ads named by text / ad group: their ids are known now (the lists above), so read them in one go too
    live = {"keyword": set(), "rsa": set()}
    for t, c, g in later:
        if t == "keyword":
            for kw in c.get("keywords", [c.get("keyword")]):
                k = safe(lambda: ids.keyword(c["campaign"], c["ad_group"], kw, c["match"]))
                k and live["keyword"].add(f"customers/{a}/adGroupCriteria/{_id(g)}~{k}")
        else:
            for rid in safe(lambda: ids.rsa_ads(c["campaign"], c["ad_group"], c.get("local_id"), c.get("ad_id"))) or []:
                live["rsa"].add(f"customers/{a}/adGroupAds/{_id(g)}~{rid}")
    ids.batch(live=live)


def _experiments(acct):
    """Trial campaigns (refused) and bases with a running experiment (warned): asked from Google at push time, plus what
    Editor / the cache know. If Google can't be asked, the cached facts still apply (and the push itself needs Google)."""
    try:
        fresh = experiments.fetch(acct, _exp_query)
    except Exception:  # noqa: BLE001
        fresh = experiments.empty()
    return experiments.merge(fresh, experiments.known(acct))


_CAMP_RES = re.compile(r"campaign(?:s|Criteria|Assets|SharedSets)/(\d+)")
_GROUP_RES = re.compile(r"adGroup(?:s|Criteria|Ads|Assets)/(\d+)")


def _refuse_trial_undo(acct, rec):
    """An undo is a push too: refuse it, before anything is sent, when a logged change or any campaign / ad group the
    logged operations name is (now) an experiment's trial campaign or sits in one."""
    exps = _experiments(acct)
    bad = [p for it in rec.get("items", []) for p in experiments.check(exps, it.get("change") or {})[0]]
    blob = json.dumps([[o.get("op"), o.get("undo"), o.get("resource")] for o in rec.get("ops", [])])
    camps = sorted(set(_CAMP_RES.findall(blob)) - {""})
    groups = sorted(set(_GROUP_RES.findall(blob)))
    rows = []
    if camps:
        rows += query(acct, f"SELECT campaign.id, campaign.name, campaign.experiment_type FROM campaign WHERE campaign.id IN ({', '.join(camps)})")
    if groups:
        rows += query(acct, f"SELECT ad_group.id, campaign.id, campaign.name, campaign.experiment_type FROM ad_group "
                            f"WHERE ad_group.id IN ({', '.join(groups)})")
    for r in rows:
        c = r.get("campaign", {})
        if c.get("experimentType") not in experiments.NOT_TRIAL or experiments._n(c.get("name")) in exps["trials"]:
            bad.append(f'{experiments.MSG} ("{c.get("name", c.get("id"))}")')
    if bad:
        raise ValueError("Not undone: " + "; ".join(dict.fromkeys(bad)))


def _refuse_list_in_use(acct, rec):
    """[audiences] Undo removes a list Studio made in that push: first read live who uses it (as a remove does); refuse
    when anything does, or when that can't be read."""
    made = [o["resource"] for o in rec.get("ops", []) if o.get("undo") == "remove-created" and "userListOperation" in o.get("op", {})]
    if not made:
        return
    ids = type("Acct", (), {"acct": acct})()
    for res in made:
        try:
            users = core.audiences.live_users(ids, res)
        except Exception:  # noqa: BLE001
            raise ValueError("Not undone: Studio can't read whether the audience list it would remove is in use; try again")
        if users:
            raise ValueError(f"Not undone: the audience list this push made is now in use ({'; '.join(users)}). Detach it first")


def _only(acct, only):
    """The draft items in this push: all of them, or (team approvals / push by person) only the ids in `only`."""
    items = [i for i in core.draft(acct)["items"] if i["change"].get("type") != "video_upload"]
    if only is None:
        return items
    return [i for i in items if (only.admits(i) if hasattr(only, "admits") else i["id"] in only)]


def push(acct, enable_new=False, accept_spend=False, accept_permanent=False, only=None):
    progress.start(acct, "push", [(i["id"], i["summary"], 1) for i in _only(acct, only)], "checking", "Google is checking the changes")
    res = None
    try:
        res = _push(acct, enable_new, accept_spend, accept_permanent, only)
        return res
    finally:
        progress.finish(acct, bool(res and res.get("ok")), (res or {}).get("message", "Stopped with an error"))


def _push(acct, enable_new=False, accept_spend=False, accept_permanent=False, only=None):
    p = plan(acct, enable_new, None if only is None else _only(acct, only))
    progress.totals(acct, {e["id"]: len(e["lines"]) for e in p["items"]})
    for e in p["items"]:
        progress.checked(acct, e["id"], len(e["lines"])) if not e["problems"] else progress.mark(acct, e["id"], "failed")
    if not p["ok"]:
        return {"ok": False, "message": "Not pushed - fix the problems in the preview first.", "preview": _public(p)}
    if p["spend"] and not accept_spend:
        return {"ok": False, "message": "Not pushed - this push can raise spend; tick the box to accept.", "preview": _public(p)}
    if p["permanent"] and not accept_permanent:
        return {"ok": False, "message": "Not pushed - this push removes things for good; tick the box to accept.", "preview": _public(p)}
    ops = p["ops"]
    progress.phase(acct, "sending", "Sending to Google Ads: all changes go live together, or none do")
    ok, res = mutate(acct, [o["op"] for o in ops], apply=True)
    if ok:
        progress.sent(acct)
    stamp = dt.datetime.now()
    rec = {"id": f"{stamp:%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}", "ts": stamp.isoformat(timespec="seconds"), "ok": ok,
           "enable_new": enable_new, "items": [i for i in core.draft(acct)["items"] if i["id"] in {o["item"] for o in ops}],
           "ops": [], "result": res if not ok else None}
    if ok:
        responses = res.get("mutateOperationResponses", [])
        for o, r in zip(ops, responses):
            created = next(iter(r.values()), {}).get("resourceName") if r else None
            rec["ops"].append({"line": o["line"], "op": o["op"], "undo": o.get("undo"), "undo_note": o.get("undo_note", ""),
                               "resource": created, "permanent": o.get("permanent", False),
                               **({"undo_spend": o["undo_spend"]} if o.get("undo_spend") else {})})
        core.remove(acct, [i["id"] for i in rec["items"]])
    with open(_log(acct), "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return {"ok": ok, "push_id": rec["id"], "applied": len(rec["ops"]),
            "message": f"Live in Google Ads: {len(rec['ops'])} operation(s)." if ok else "Google refused the push - nothing changed.",
            "errors": None if ok else res}


def _public(p):
    return {k: v for k, v in p.items() if k != "ops"}


def preview(acct, enable_new=False, items=None, source=None, only=None):
    if only is not None and items is None:
        items = _only(acct, only)
    return _public(plan(acct, enable_new, items, source))


def history(acct, limit=20):
    f = _log(acct)
    if not f.exists():
        return []
    recs = [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
    undone = {r["undo_of"] for r in recs if r.get("undo_of")}
    out = []
    for r in reversed(recs):
        out.append({"id": r["id"], "ts": r["ts"], "ok": r["ok"], "undo_of": r.get("undo_of"), "undone": r["id"] in undone,
                    "summaries": [i["summary"] for i in r.get("items", [])], "lines": [o["line"] for o in r.get("ops", [])],
                    "can_undo": r["ok"] and not r.get("undo_of") and r["id"] not in undone and any(o.get("undo") for o in r.get("ops", []))})
    return out[:limit]


def undo(acct, push_id, accept_spend=False):
    res = None
    try:
        res = _undo(acct, push_id, accept_spend)
        return res
    finally:
        progress.finish(acct, bool(res and res.get("ok")), (res or {}).get("message", "Stopped with an error"))


def _undo(acct, push_id, accept_spend=False):
    recs = [json.loads(l) for l in _log(acct).read_text().splitlines() if l.strip()]
    rec = next((r for r in recs if r["id"] == push_id), None)
    if not rec or not rec["ok"]:
        raise ValueError("push not found")
    if any(r.get("undo_of") == push_id for r in recs):
        raise ValueError("already undone")
    on = [o["line"] for o in rec["ops"] if o.get("undo_spend")]  # [audiences] e.g. membership days put back
    if on and not accept_spend:  # nothing is sent: the UI asks, then calls again with accept_spend
        return {"ok": False, "needs_spend": True, "spend": on, "not_undone": [], "errors": None,
                "message": f"Not undone - this undo can raise spend ({len(on)} change(s)); accept to undo."}
    _refuse_trial_undo(acct, rec)
    _refuse_list_in_use(acct, rec)
    ops, notes = [], []
    for o in reversed(rec["ops"]):
        u = o.get("undo")
        if u == "remove-created":
            kind = next(iter(o["op"]))
            ops.append({kind: {"remove": o["resource"]}})
        elif u:
            ops.append(u)
        else:
            notes.append(o["line"] + (f" - {o['undo_note']}" if o.get("undo_note") else " - can't be undone"))
    if not ops:
        raise ValueError("nothing in this push can be undone automatically")
    lines = [o["line"] for o in reversed(rec["ops"]) if o.get("undo")]
    progress.start(acct, "undo", [(n, "Undo: " + l, 1) for n, l in enumerate(lines)], "checking", "Google is checking the undo")
    ok, res = mutate(acct, ops, apply=False)
    if ok:
        progress.checked(acct)
        progress.phase(acct, "sending", "Reversing in Google Ads: all together, or none")
        ok, res = mutate(acct, ops, apply=True)
        if ok:
            progress.sent(acct)
    stamp = dt.datetime.now()
    with open(_log(acct), "a") as f:
        f.write(json.dumps({"id": f"{stamp:%Y%m%d-%H%M%S}-undo", "ts": stamp.isoformat(timespec="seconds"), "ok": ok, "undo_of": push_id,
                            "items": [], "ops": [{"line": "undo", "op": op} for op in ops] if ok else [], "result": None if ok else res},
                           ensure_ascii=False) + "\n")
    return {"ok": ok, "message": f"Undone: {len(ops)} operation(s) reversed." if ok else "Google refused the undo - nothing changed.",
            "not_undone": notes, "errors": None if ok else res}


def last_push(acct):
    h = [r for r in history(acct, 200) if r["ok"]]
    return h[0]["ts"] if h else None
