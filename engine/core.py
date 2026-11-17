"""Ads Studio core - reads everything Google Ads Editor holds for an account, and keeps a shared draft
of changes that both the Studio UI and Claude (via MCP) edit. The draft is pushed into Editor with
`gae apply --restart`; Editor is then only the place where changes are reviewed and Posted.

Read side: Editor's database, read-only (immutable=1), re-opened per call so it is always current.
Draft: one JSON file per account under editor-changes/studio/ (draftstore collection ads:<acct>), guarded by a file lock.
"""
import contextlib, datetime as dt, fcntl, json, os, re, shutil, sqlite3, subprocess, sys, tempfile, uuid
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import appconfig  # noqa: E402,F401
import demandgen  # noqa: E402  (Demand Gen / YouTube video change types, live only)
import draftstore  # noqa: E402
import experiments  # noqa: E402
import gae  # noqa: E402
import audiences  # noqa: E402  [audiences] user_list change type (Push live only)

STUDIO_DIR = gae.OUT_DIR / "studio"
LOW32, P_ACCOUNT, P_CAMPAIGN, P_ADGROUP = gae.LOW32, 1, gae.PARENT_CAMPAIGN, gae.PARENT_ADGROUP
STATE = {0: "", 1: "edited", 2: "new"}  # Editor's own row state: unposted local changes


# ---------- reading ----------

def accounts():
    return [{"id": a["id"], "name": a["name"], "key": next((k for k, v in gae.ACCOUNTS.items() if v == a["id"]), a["id"]),
             "last_download": a["last_download"].isoformat(timespec="minutes") if a["last_download"] else None}
            for a in gae.accounts()]


def resolve(acct):
    return gae.resolve_account(acct or "ml")


def _db(acct):
    f = gae.editor_version_dir() / f"ape_{acct}.db"
    if not f.exists():
        raise ValueError(f"account {acct} is not downloaded in Editor")
    db = gae.ro(f)
    db.row_factory = sqlite3.Row
    return db


def qlist(blob):
    """Decode a Qt QStringList blob (see gae.qstringlist)."""
    if not blob:
        return []
    b, out, i = bytes(blob), [], 4
    for _ in range(int.from_bytes(b[:4], "big")):
        n = int.from_bytes(b[i:i + 4], "big")
        i += 4
        if n == 0xFFFFFFFF:
            continue
        out.append(b[i:i + n].decode("utf-16-be"))
        i += n
    return out


def _money(m):
    return round(m / 1e6, 2) if m else None


class Ctx:
    """Name lookups shared by every grid for one read."""

    def __init__(self, db):
        self.db = db
        self.camps = {r["localId"]: r for r in db.execute("select * from Campaign")}
        self.groups = {r["localId"]: r for r in db.execute("select localId, name, parentId, status from AdGroup")}

    def where(self, parent):
        """parentId -> (level, campaign name, ad group name, campaign localId, ad group localId)."""
        kind, lid = parent >> 32, parent & LOW32
        if kind == P_CAMPAIGN:
            c = self.camps.get(lid)
            return "Campaign", c["name"] if c else "?", "", lid, None
        if kind == P_ADGROUP:
            g = self.groups.get(lid)
            cid = g["parentId"] & LOW32 if g else None
            c = self.camps.get(cid)
            return "Ad group", c["name"] if c else "?", g["name"] if g else "?", cid, lid
        return "Account", "", "", None, None

    def base(self, r, parent=None):
        level, camp, ag, cid, gid = self.where(r["parentId"] if parent is None else parent)
        out = {"id": r["localId"], "level": level, "campaign": camp, "ad_group": ag, "campaign_id": cid, "ad_group_id": gid,
               "status": gae.STATUS.get(r["status"], r["status"]) if "status" in r.keys() else None,
               "editor": STATE.get(r["state"], r["state"])}
        if "comment" in r.keys() and r["comment"]:
            out["comment"] = r["comment"]
        return out


def _scope_ok(row, campaign_id, ad_group_id):
    if ad_group_id is not None:
        return row.get("ad_group_id") == ad_group_id
    if campaign_id is not None:
        return row.get("campaign_id") == campaign_id
    return True


def g_campaigns(x):
    ag = Counter(r["parentId"] & LOW32 for r in x.groups.values() if r["status"] != 2)
    kw = Counter()
    for (p,) in x.db.execute("select parentId from Keyword where status != 2"):
        g = x.groups.get(p & LOW32)
        if g:
            kw[g["parentId"] & LOW32] += 1
    budgets = {r["localId"]: (r["name"], r["budgetAmount"]) for r in x.db.execute("select * from Budget")}
    out = []
    for c in x.camps.values():
        shared = budgets.get(c["sharedBudgetLocalId"]) if c["sharedBudgetLocalId"] else None
        bid = gae.BIDDING.get(c["biddingStrategyType"], c["biddingStrategyType"])
        if c["biddingStrategyType"] == 15 and c["targetImpressionShare"]:
            bid = f"{bid} {round(c['targetImpressionShare'] * 100)}%"
        out.append({"id": c["localId"], "campaign_id": c["localId"], "campaign": c["name"], "status": gae.STATUS.get(c["status"]),
                    "type": gae.CAMPAIGN_TYPE.get(c["campaignType"], str(c["campaignType"])),
                    "budget": _money(shared[1] if shared else c["budgetAmount"]), "shared_budget": shared[0] if shared else "",
                    "bidding": bid, "max_cpc_cap": _money(c["maxCpcBidCeiling"]), "target_cpa": _money(c["maxCpa"]),
                    "ad_groups": ag.get(c["localId"], 0), "keywords": kw.get(c["localId"], 0),
                    "start": c["startDate"] or "", "end": c["endDate"] or "", "languages": c["languages"] or "",
                    "editor": STATE.get(c["state"], c["state"]), "comment": c["comment"] or "", "remote_id": c["remoteId"]})
    return out


def g_ad_groups(x):
    kw = Counter(p & LOW32 for (p,) in x.db.execute("select parentId from Keyword where status != 2"))
    ads = Counter(p & LOW32 for (p,) in x.db.execute("select parentId from ResponsiveSearchAd where status != 2"))
    out = []
    for r in x.db.execute("select * from AdGroup"):
        out.append({**x.base(r), "level": "Ad group", "ad_group": r["name"], "ad_group_id": r["localId"], "remote_id": r["remoteId"],
                    "max_cpc": _money(r["maxCpc"]), "keywords": kw.get(r["localId"], 0), "ads": ads.get(r["localId"], 0)})
    return out


def g_keywords(x):
    out = []
    for r in x.db.execute("select * from Keyword"):
        urls = qlist(r["finalUrls"])
        out.append({**x.base(r), "keyword": r["text"], "match": gae.MATCH.get(r["criterionType"]), "max_cpc": _money(r["maxCpc"]),
                    "quality_score": r["qualityScore"] or None, "first_page_cpc": _money(r["firstPageCpc"]),
                    "top_of_page_cpc": _money(r["topOfPageCpc"]), "final_url": urls[0] if urls else ""})
    return out


def g_negatives(x):
    out = []
    for r in x.db.execute("select * from KeywordNegative where parentId >> 32 in (?, ?)", (P_CAMPAIGN, P_ADGROUP)):
        out.append({**x.base(r), "keyword": r["text"], "match": gae.MATCH.get(r["criterionType"])})
    lists = {r["localId"]: r["name"] for r in x.db.execute("select localId, name from NegativeKeywordList")}
    for r in x.db.execute("select * from KeywordNegative where parentId >> 32 not in (?, ?)", (P_CAMPAIGN, P_ADGROUP)):
        out.append({"id": r["localId"], "level": "Shared list", "campaign": "", "ad_group": "", "campaign_id": None, "ad_group_id": None,
                    "list": lists.get(r["parentId"] & LOW32, ""), "keyword": r["text"], "match": gae.MATCH.get(r["criterionType"]),
                    "status": gae.STATUS.get(r["status"]), "editor": STATE.get(r["state"], r["state"])})
    return out


def g_ads(x):
    out = []
    for r in x.db.execute("select * from ResponsiveSearchAd"):
        hl = [{"text": r[f"headline{i}"], "pin": r[f"headline{i}Pos"] or 0} for i in range(1, 16) if r[f"headline{i}"]]
        ds = [{"text": r[f"description{i}"], "pin": r[f"description{i}Pos"] or 0} for i in range(1, 5) if r[f"description{i}"]]
        urls = qlist(r["finalUrls"])
        out.append({**x.base(r), "ad_type": "Responsive search ad", "headline": hl[0]["text"] if hl else "",
                    "headlines": hl, "descriptions": ds, "path1": r["path1"] or "", "path2": r["path2"] or "",
                    "final_url": urls[0] if urls else "", "ad_strength": r["adStrengthServer"] or r["adStrengthLocal"] or "",
                    "approval": r["approvalStatus"]})
    for r in x.db.execute("select * from ExpandedDynamicSearchAd"):
        ds = [{"text": t, "pin": 0} for t in (r["description1"], r["description2"]) if t]
        out.append({**x.base(r), "ad_type": "Dynamic search ad", "headline": "(dynamic)", "headlines": [], "descriptions": ds,
                    "path1": "", "path2": "", "final_url": "", "ad_strength": "", "approval": r["approvalStatus"]})
    return out


def g_asset_groups(x):
    out = []
    for r in x.db.execute("select * from AssetGroup"):
        urls = qlist(r["finalUrls"])
        out.append({**x.base(r), "asset_group": r["name"], "headlines": sum(1 for i in range(1, 16) if r[f"headline{i}"]),
                    "long_headlines": sum(1 for i in range(1, 6) if r[f"longHeadline{i}"]),
                    "descriptions": sum(1 for i in range(1, 6) if r[f"description{i}"]),
                    "final_url": urls[0] if urls else "", "ad_strength": r["adStrengthServer"] or ""})
    return out


def _linked(x, table, fields):
    """Assets are a library table plus <table>Edge links to account/campaign/ad group; one row per link."""
    assets = {r["localId"]: r for r in x.db.execute(f"select * from {table}")}
    out = []
    for e in x.db.execute(f"select * from {table}Edge"):
        a = assets.get(e["sourceId"])
        if a is None:
            continue
        row = {**x.base(e), "asset_id": a["localId"]}
        if a["status"] == 2:
            row["status"] = "Removed"
        row.update(fields(a))
        out.append(row)
    return out


def g_sitelinks(x):
    return _linked(x, "SitelinkV2", lambda a: {"text": a["displayText"], "line1": a["description1"] or "", "line2": a["description2"] or "",
                                              "final_url": (qlist(a["finalUrls"]) or [""])[0],
                                              "start": a["startDate"] or "", "end": a["endDate"] or ""})


def g_callouts(x):
    return _linked(x, "CalloutV2", lambda a: {"text": a["displayText"], "start": a["startDate"] or "", "end": a["endDate"] or ""})


def g_snippets(x):
    return _linked(x, "StructuredSnippetV2", lambda a: {"header": a["header"], "values": ", ".join(qlist(a["snippetValues"]))})


def g_prices(x):
    def f(a):
        items = [f"{a[f'header{i}']} {a['currencyCode'] or ''} {_money(a[f'price{i}'])}" for i in range(1, 9) if a[f"header{i}"]]
        return {"price_type": a["type"], "qualifier": a["priceQualifier"] or "", "items": "; ".join(items)}
    return _linked(x, "PriceExtensionV2", f)


def g_promotions(x):
    return _linked(x, "PromotionExtensionV2", lambda a: {"occasion": a["occasion"] or "", "target": a["promotionTarget"] or "",
                                                        "discount": f"{a['percentDiscount'] / 1e4:g}%" if a["percentDiscount"] else
                                                        f"{_money(a['monetaryDiscount'])} {a['currencyCode'] or ''}",
                                                        "final_url": (qlist(a["finalUrls"]) or [""])[0]})


def g_locations(x):
    geo = {r["remoteId"]: r for r in x.db.execute("select * from GeoLocation")}
    out = []
    for table, kind in (("GeoTarget", "Targeted"), ("GeoTargetNegative", "Excluded")):
        for r in x.db.execute(f"select * from {table}"):
            g = geo.get(r["locationId"])
            out.append({**x.base(r), "location": g["locationPath"] if g else (r["customLocationPath"] if "customLocationPath" in r.keys() else "") or str(r["locationId"]),
                        "location_id": r["locationId"], "targeting": kind, "country": g["countryCode"] if g else "",
                        "bid_modifier": r["bidModifier"] if "bidModifier" in r.keys() and r["bidModifier"] not in (None, -2147483647) else None})
    return out


def g_audiences(x):
    ul = {r["remoteId"]: r["name"] for r in x.db.execute("select remoteId, name from UserList")}
    ca = {r["remoteId"]: r["name"] for r in x.db.execute("select remoteId, name from CustomAffinity")}
    out = []
    for r in x.db.execute("select * from Audience"):
        name = ul.get(r["userListId"]) or ca.get(r["customAffinityId"]) or \
            next((f"{k} {r[k]}" for k in ("userInterestId", "lifeEventId", "demographicsId", "combinedAudienceId") if r[k]), "?")
        out.append({**x.base(r), "audience": name, "bid_modifier": r["bidModifier"] if r["bidModifier"] != -2147483647 else None})
    return out


def g_shared_lists(x):
    attached = {}
    for e in x.db.execute("select * from NegativeKeywordListEdge where status != 2"):
        attached.setdefault(e["sourceId"], []).append(x.where(e["parentId"])[1])
    return [{"id": r["localId"], "list": r["name"], "keywords": r["keywordCount"], "status": gae.STATUS.get(r["status"]),
             "campaigns": len(attached.get(r["localId"], [])), "attached_to": ", ".join(sorted(attached.get(r["localId"], []))),
             "editor": STATE.get(r["state"], r["state"]), "campaign_id": None, "ad_group_id": None}
            for r in x.db.execute("select * from NegativeKeywordList")]


GRIDS = {"campaigns": g_campaigns, "ad_groups": g_ad_groups, "keywords": g_keywords, "negatives": g_negatives, "ads": g_ads,
         "asset_groups": g_asset_groups, "sitelinks": g_sitelinks, "callouts": g_callouts, "snippets": g_snippets,
         "prices": g_prices, "promotions": g_promotions, "locations": g_locations, "audiences": g_audiences,
         "shared_lists": g_shared_lists}


def _matches(row, needle):
    return any(needle in str(v).lower() for k, v in row.items() if isinstance(v, (str, int, float)) and not k.endswith("_id"))


def grid(acct, kind, campaign_id=None, ad_group_id=None, status="active", search=None, limit=None):
    """Rows of one entity type. status: 'active' (not removed, the Editor default view), 'enabled', or 'all'."""
    if kind not in GRIDS:
        raise ValueError(f"unknown kind {kind!r}; one of {', '.join(GRIDS)}")
    db = _db(acct)
    x = Ctx(db)
    rows = [r for r in GRIDS[kind](x) if _scope_ok(r, campaign_id, ad_group_id)]
    camp_status = {c["localId"]: c["status"] for c in x.camps.values()}
    group_status = {g["localId"]: g["status"] for g in x.groups.values()}
    if status != "all":
        # like Editor: a removed campaign hides everything under it
        rows = [r for r in rows if r.get("status") != "Removed" and camp_status.get(r.get("campaign_id"), 0) != 2
                and group_status.get(r.get("ad_group_id"), 0) != 2]
    if status == "enabled":
        rows = [r for r in rows if r.get("status") in ("Enabled", None) and camp_status.get(r.get("campaign_id"), 0) == 0
                and group_status.get(r.get("ad_group_id"), 0) == 0]
    if search:
        n = search.lower()
        rows = [r for r in rows if _matches(r, n)]
    _overlay(acct, kind, rows, x, campaign_id, ad_group_id)
    experiments.tag(rows, experiments.from_editor(db))
    return rows[:limit] if limit else rows


def tree(acct, status="active"):
    db = _db(acct)
    x = Ctx(db)
    groups = {}
    for g in x.groups.values():
        groups.setdefault(g["parentId"] & LOW32, []).append(g)
    out = []
    for c in sorted(x.camps.values(), key=lambda c: (c["status"], c["name"].lower())):
        if status != "all" and c["status"] == 2:
            continue
        if status == "enabled" and c["status"] != 0:
            continue
        kids = [{"id": g["localId"], "name": g["name"], "status": gae.STATUS.get(g["status"])}
                for g in sorted(groups.get(c["localId"], []), key=lambda g: g["name"].lower())
                if status == "all" or g["status"] != 2]
        out.append({"id": c["localId"], "name": c["name"], "status": gae.STATUS.get(c["status"]),
                    "type": gae.CAMPAIGN_TYPE.get(c["campaignType"], str(c["campaignType"])),
                    "editor": STATE.get(c["state"], c["state"]), "ad_groups": kids})
    return experiments.tag(out, experiments.from_editor(db), key="name")


def editor_pending(acct):
    db = _db(acct)
    out = {}
    for t, col in gae.PENDING_TABLES.items():
        try:
            n = db.execute(f"select count(*) from {t} where state != 0").fetchone()[0]
        except sqlite3.OperationalError:
            continue
        if n:
            out[t] = n
    return out


def geo_search(acct, name):
    return [{"id": r["remoteId"], "location": r["locationPath"], "country": r["countryCode"]}
            for r in _db(acct).execute("select * from GeoLocation where locationPath like ? order by reach desc limit 25", (f"%{name}%",))]


# ---------- the shared draft ----------

def _draft_path(acct):
    STUDIO_DIR.mkdir(parents=True, exist_ok=True)
    return STUDIO_DIR / f"draft_{acct}.json"


def _coll(acct):
    return f"ads:{acct}"


@contextlib.contextmanager
def _locked(acct, write=True):
    """The draft {version, items} under its lock (draftstore collection ads:<acct>). write: saved and version + 1."""
    if not write:
        yield draftstore.read(_coll(acct), STUDIO_DIR)
        return
    with draftstore.doc(_coll(acct), STUDIO_DIR, bump=True) as d:
        yield d


def draft(acct):
    return draftstore.read(_coll(acct), STUDIO_DIR)


# Copy rules: team_settings (key copy_rules, area "google_ads"): "block" rules run in gae.lint (with the length limits),
# "warn" rules here. A new install has none; an older one keeps the rules it had built in (_legacy_copy_rules.py).
def copy_warn_rules():
    """[(compiled pattern, message)] of the Ads "warn" copy rules."""
    try:
        import team_settings  # noqa: PLC0415
        return [(rx, msg) for lv, rx, msg in team_settings.copy_rules("google_ads") if lv == "warn"]
    except Exception:  # noqa: BLE001
        return []


# Change types that only the Google Ads API can carry out (Push live). Send to Editor refuses them.
PMAX_FIELDS = {"headline": ("HEADLINE", 30, 3, 15), "long_headline": ("LONG_HEADLINE", 90, 1, 5),
               "description": ("DESCRIPTION", 90, 2, 5)}  # field -> (API field type, max chars, min, max per asset group)
AUDIENCE_MODES = ("observation", "targeting")


def live_only(change):
    t = change.get("type")
    return t in ("pmax_asset", audiences.TYPE) or demandgen.is_dg(change) or (t == "audience" and bool(change.get("remove") or change.get("mode")))


def _warn(rows):
    out, rules = [], copy_warn_rules()
    for r in rows:
        for k, v in r.items():
            for pat, msg in rules:
                if v and isinstance(v, str) and pat.search(v) and k not in ("Campaign", "Ad Group", "Final URL"):
                    out.append(f"{k}: {msg}")
    return sorted(set(out))


def _check_live_only(c, acct=None):
    if demandgen.is_dg(c):
        return demandgen.check(c, acct)
    if c["type"] == audiences.TYPE:  # [audiences] website / combined lists: audiences.py
        return audiences.check(c)
    problems = [f"{k} is missing" for k in ("campaign",) if not c.get(k)]
    if c["type"] == "pmax_asset":
        if not c.get("asset_group"):
            problems.append("asset_group is missing")
        f = PMAX_FIELDS.get(str(c.get("field", "")).lower())
        texts = c.get("texts", [c.get("text")] if c.get("text") else [])
        if not f:
            problems.append(f"field must be one of {', '.join(PMAX_FIELDS)}")
        if not texts or not all(isinstance(t, str) and t.strip() for t in texts):
            problems.append("texts: give one or more non-empty texts")
        if problems:
            return problems, []
        label = c["field"].replace("_", " ").capitalize()
        rows = [{f"{label} {i}": t for i, t in enumerate(texts, 1)}]
        if not c.get("remove"):
            if f[0] == "LONG_HEADLINE":  # gae.lint already knows the Headline / Description limits
                problems += [f"{label} {i}: {gae.width(t)} chars > {f[1]}" for i, t in enumerate(texts, 1) if gae.width(t) > f[1]]
            problems += [p.split(": ", 1)[1] if p.startswith("row ") else p for p in gae.lint(rows)]
            if len(set(texts)) != len(texts):
                problems.append("the same text is listed twice")
            return problems, _warn(rows)
        return problems, []
    # audience: detach (remove) or observation/targeting switch
    if c.get("mode") and c["mode"] not in AUDIENCE_MODES:
        problems.append(f"mode must be {' or '.join(AUDIENCE_MODES)}")
    if c.get("remove") and not c.get("lists"):
        problems.append("lists: name the audience lists to detach")
    if c.get("remove") and c.get("mode"):
        problems.append("detach and mode switch are separate changes")
    return problems, []


def check(change, acct=None):
    """-> (problems that block, warnings). Uses gae's own lint so Studio and gae never disagree."""
    if live_only(change):
        return _check_live_only(change, acct)
    try:
        rows = gae.plan_rows(change)
    except (KeyError, ValueError) as ex:
        return [f"invalid change: {ex}"], []
    if change.get("type") not in gae.SUPPORTED:
        return [f"type {change.get('type')!r} cannot be written into Editor yet"], []
    problems = [p.split(": ", 1)[1] if p.startswith("row ") else p for p in gae.lint(rows)]
    if change.get("type") == "sitelink" and change.get("edit"):  # gae.plan_rows skips edits, so check the new text here
        for k, lim in (("new_text", 25), ("line1", 35), ("line2", 35)):
            if gae.width(change.get(k) or "") > lim:
                problems.append(f"{k}: {gae.width(change[k])} chars > {lim}")
        rows = [{k: change[k] for k in ("new_text", "line1", "line2") if change.get(k)}]
        problems += [p.split(": ", 1)[1] for p in gae.lint(rows)]
    return problems, _warn(rows)


def add(acct, changes, by="you", note=None):
    """Add plan changes (gae plan format) to the draft. Returns the new items, each with its problems/warnings.
    Refuses (ValueError, nothing added) when a change touches an experiment's trial campaign (experiments.py)."""
    if isinstance(changes, dict):
        changes = [changes]
    if by == "claude" and any(isinstance(c, dict) and c.get("type") == "video_upload" for c in changes):
        raise ValueError("Not added to the draft. Uploading a video to YouTube is the user's own Upload button in Studio's Videos tab")
    exps = experiments.known(acct)
    refused = [f"{_summary(c)}: {p}" for c in changes for p in experiments.check(exps, c)[0]]
    refused += audiences.refusals(acct, changes)  # [audiences] a list in use can't be removed
    if refused:
        raise ValueError("Not added to the draft. " + "; ".join(refused))
    items = []
    for c in changes:
        problems, warnings = check(c, acct)
        warnings = warnings + experiments.check(exps, c)[1]
        items.append({"id": uuid.uuid4().hex[:8], "change": c, "by": by, "note": note or "",
                      "ts": dt.datetime.now().isoformat(timespec="seconds"), "summary": _summary(c),
                      "problems": problems, "warnings": warnings})
    with draftstore.tx(_coll(acct), STUDIO_DIR, by=by) as t:
        for it in items:
            t.put(it["id"], it, by)
    return items


def _summary(c):
    t = c.get("type")
    if demandgen.is_dg(c):
        return demandgen.summary(c)
    if t == audiences.TYPE:
        return audiences.summary(c)
    if t == "pmax_asset":
        texts = c.get("texts", [c.get("text")])
        return (f"{'Remove' if c.get('remove') else 'Add'} {str(c.get('field', '')).replace('_', ' ')}s in asset group "
                f"{c.get('campaign')} / {c.get('asset_group')}: " + ", ".join(str(x) for x in texts))
    if t == "audience" and c.get("remove"):
        return f"Detach audiences from {c.get('campaign')}: " + ", ".join(c.get("lists", []))
    if t == "audience" and c.get("mode") and not c.get("lists"):
        return f"Audiences on {c.get('campaign')}: switch to {c['mode']}"
    if t == "audience" and c.get("mode"):
        return f"Audiences ({c['mode']}) on {c.get('campaign')}: " + ", ".join(c.get("lists", []))
    try:
        return gae.summarise(c)
    except (KeyError, TypeError):
        return json.dumps(c, ensure_ascii=False)


def remove(acct, ids):
    with draftstore.tx(_coll(acct), STUDIO_DIR) as t:
        gone = [r["data"] for r in (t.get(i) for i in set(ids)) if r]
        n = sum(1 for i in set(ids) if t.delete(i))
        left = [r["data"] for r in t.items()]
    _release_staged(acct, gone, left)
    return n


def clear(acct):
    with draftstore.tx(_coll(acct), STUDIO_DIR) as t:
        gone = [r["data"] for r in t.items()]
        n = sum(1 for i in t.ids() if t.delete(i))
    _release_staged(acct, gone, [])
    return n


def _release_staged(acct, gone, left):
    """A video_upload item left the draft: delete the copy Studio staged for it (youtube_upload.release)."""
    if not any(isinstance(i, dict) and (i.get("change") or {}).get("type") == "video_upload" for i in gone):
        return
    try:
        import youtube_upload  # noqa: PLC0415 - it imports core
        youtube_upload.release(acct, gone, left)
    except Exception:  # noqa: BLE001 - removing from the draft never fails because of a staging file
        pass


def validate(acct):
    """Dry run: apply every draft item, in order, to a throwaway copy of Editor's database."""
    items = draft(acct)["items"]
    vdir = gae.editor_version_dir()
    with tempfile.TemporaryDirectory() as tmp:
        shutil.copy2(vdir / f"ape_{acct}.db", Path(tmp) / "a.db")
        db = sqlite3.connect(str(Path(tmp) / "a.db"), isolation_level=None)  # manual transactions: savepoint per item
        db.row_factory = sqlite3.Row
        db.execute("begin")
        results = []
        exps = experiments.merge(experiments.known(acct), experiments.from_editor(db))
        for it in items:
            if it["change"].get("type") == "video_upload":
                continue  # runs from its own Upload button; never part of Send to Editor
            problems, warnings = check(it["change"], acct)
            xp, xw = experiments.check(exps, it["change"])
            problems, warnings = xp + problems, warnings + xw
            if live_only(it["change"]):
                results.append({"id": it["id"], "summary": it["summary"], "ok": False, "live_only": True, "warnings": warnings,
                                "problems": problems + ["live only: Editor can't hold this change - use Push live, or remove it before Send to Editor"]})
                continue
            if not problems:
                try:
                    db.execute("savepoint s")
                    gae.apply_changes(db, [it["change"]], "Studio validate", [])
                    db.execute("release s")
                except Exception as ex:  # noqa: BLE001 - every failure is reported per item
                    db.execute("rollback to s")
                    db.execute("release s")
                    problems = [str(ex)]
            results.append({"id": it["id"], "summary": it["summary"], "ok": not problems, "problems": problems, "warnings": warnings})
        db.close()
    with _locked(acct) as d:
        by_id = {r["id"]: r for r in results if not r.get("live_only")}
        for it in d["items"]:
            if it["id"] in by_id:
                it["problems"], it["warnings"] = by_id[it["id"]]["problems"], by_id[it["id"]]["warnings"]
    return results


def push(acct, title=None, why=None, only=None):
    """Write the whole draft into Editor as pending changes (gae apply --restart), then archive it.
    only: (team approvals / push by person) just these draft items; see team_reviews.Only."""
    results = validate(acct)
    d = draft(acct)
    d["items"] = [i for i in d["items"] if i["change"].get("type") != "video_upload"]
    if only is not None:
        keep = {i["id"] for i in d["items"] if (only.admits(i) if hasattr(only, "admits") else i["id"] in only)}
        results = [r for r in results if r["id"] in keep]
        d["items"] = [i for i in d["items"] if i["id"] in keep]
    bad = [r for r in results if not r["ok"]]
    if not results:
        return {"ok": False, "output": "The draft is empty." if only is None else "Nothing in the draft can go to Editor."}
    if bad:
        return {"ok": False, "output": "Fix these first:\n" + "\n".join(f"- {r['summary']}: {'; '.join(r['problems'])}" for r in bad)}
    stamp = dt.datetime.now()
    plan = {"title": title or f"Ads Studio {stamp:%d %b %H:%M}", "why": why or "; ".join(sorted({i['note'] for i in d['items'] if i['note']})) or None,
            "changes": [i["change"] for i in d["items"]]}
    pfile = STUDIO_DIR / f"plan-{stamp:%Y%m%d-%H%M%S}.json"
    pfile.write_text(json.dumps(plan, ensure_ascii=False, indent=1))
    key = next((k for k, v in gae.ACCOUNTS.items() if v == acct), acct)
    p = subprocess.run([sys.executable, str(Path(gae.__file__)), "--account", key, "apply", str(pfile), "--restart"],
                       capture_output=True, text=True, timeout=300)
    out = (p.stdout + p.stderr).strip()
    if p.returncode == 0:
        with _locked(acct) as cur:
            pushed = [i for i in cur["items"] if i["id"] in {x["id"] for x in d["items"]}]
            cur["items"] = [i for i in cur["items"] if i not in pushed]
        with open(STUDIO_DIR / f"pushed_{acct}.jsonl", "a") as f:
            f.write(json.dumps({"ts": stamp.isoformat(timespec="seconds"), "plan": str(pfile.name), "items": pushed}, ensure_ascii=False) + "\n")
    return {"ok": p.returncode == 0, "output": out, "plan": str(pfile)}


# ---------- draft overlay: show draft values and draft rows inside the grids ----------

def _n(s):
    return (s or "").strip().lower()


def _overlay(acct, kind, rows, x, campaign_id, ad_group_id):
    items = draft(acct)["items"]
    if not items:
        return
    camp_name = x.camps[campaign_id]["name"] if campaign_id in x.camps else None
    group_name = x.groups[ad_group_id]["name"] if ad_group_id in x.groups else None

    def in_scope(c):
        return (camp_name is None or _n(c.get("campaign")) == _n(camp_name)) and \
               (group_name is None or not c.get("ad_group") or _n(c.get("ad_group")) == _n(group_name))

    def find(**kv):
        return [r for r in rows if all(_n(str(r.get(k))) == _n(str(v)) for k, v in kv.items())]

    for it in items:
        c, t = it["change"], it["change"].get("type")
        mark = {"draft_id": it["id"], "by": it["by"]}
        status = c.get("status")
        if kind == "campaigns" and t == "campaign":
            if c.get("new"):
                if in_scope(c):
                    rows.insert(0, {"id": f"d-{it['id']}", "campaign": c["campaign"], "status": (status or "paused").title(),
                                    "type": c.get("campaign_type", "Search"), "budget": c.get("budget"), "bidding": c.get("bid_strategy", "Manual CPC"),
                                    "editor": "draft", "_new": mark})
                continue
            for r in find(campaign=c["campaign"]):
                r.setdefault("_draft", {})
                if status:
                    r["_draft"]["status"] = {**mark, "value": status.title()}
                if c.get("budget") is not None:
                    r["_draft"]["budget"] = {**mark, "value": float(c["budget"])}
                if c.get("bid_strategy"):
                    r["_draft"]["bidding"] = {**mark, "value": f"{c['bid_strategy']} {c.get('tis_percent', '')}%".strip()}
        elif kind == "ad_groups" and t == "ad_group":
            if c.get("new"):
                if in_scope(c):
                    rows.insert(0, {"id": f"d-{it['id']}", "campaign": c["campaign"], "ad_group": c["ad_group"], "level": "Ad group",
                                    "status": (status or "enabled").title(), "max_cpc": c.get("max_cpc"), "editor": "draft", "_new": mark})
                continue
            for r in find(campaign=c["campaign"], ad_group=c["ad_group"]):
                r.setdefault("_draft", {})
                if status:
                    r["_draft"]["status"] = {**mark, "value": status.title()}
                if c.get("max_cpc") is not None:
                    r["_draft"]["max_cpc"] = {**mark, "value": float(c["max_cpc"])}
        elif kind == "keywords" and t == "keyword":
            for kw in c.get("keywords", [c.get("keyword")]):
                if c.get("edit"):
                    for r in find(campaign=c["campaign"], ad_group=c["ad_group"], keyword=kw, match=c["match"].title()):
                        r.setdefault("_draft", {})
                        if status:
                            r["_draft"]["status"] = {**mark, "value": status.title()}
                        if c.get("max_cpc") is not None:
                            r["_draft"]["max_cpc"] = {**mark, "value": float(c["max_cpc"])}
                elif in_scope(c):
                    rows.insert(0, {"id": f"d-{it['id']}-{kw}", "campaign": c["campaign"], "ad_group": c["ad_group"], "level": "Ad group",
                                    "keyword": kw, "match": c["match"].title(), "status": "Enabled", "max_cpc": c.get("max_cpc"),
                                    "editor": "draft", "_new": mark})
        elif kind == "negatives" and t == "negative":
            for kw in c.get("keywords", [c.get("keyword")]):
                match = c.get("match", "phrase").title()
                if c.get("remove"):
                    for r in find(campaign=c["campaign"], keyword=kw, match=match):
                        if _n(r.get("ad_group")) == _n(c.get("ad_group")):
                            r["_draft"] = {"status": {**mark, "value": "Removed"}}
                elif in_scope(c):
                    rows.insert(0, {"id": f"d-{it['id']}-{kw}", "campaign": c["campaign"], "ad_group": c.get("ad_group", ""),
                                    "level": "Ad group" if c.get("ad_group") else "Campaign", "keyword": kw, "match": match,
                                    "status": "Enabled", "editor": "draft", "_new": mark})
        elif kind == "ads" and t == "rsa":
            if c.get("edit"):
                for r in find(campaign=c["campaign"], ad_group=c["ad_group"]):
                    if c.get("local_id") is not None and r["id"] != int(c["local_id"]):
                        continue
                    r.setdefault("_draft", {})
                    if status:
                        r["_draft"]["status"] = {**mark, "value": status.title()}
                    if c.get("final_url"):
                        r["_draft"]["final_url"] = {**mark, "value": c["final_url"]}
                    if c.get("headlines"):
                        h0 = c["headlines"][0]
                        r["_draft"]["headline"] = {**mark, "value": h0["text"] if isinstance(h0, dict) else h0}
            elif in_scope(c):
                hl = [h if isinstance(h, dict) else {"text": h} for h in c.get("headlines", [])]
                ds = [d if isinstance(d, dict) else {"text": d} for d in c.get("descriptions", [])]
                rows.insert(0, {"id": f"d-{it['id']}", "campaign": c["campaign"], "ad_group": c["ad_group"], "level": "Ad group",
                                "ad_type": "Responsive search ad", "headline": hl[0]["text"] if hl else "", "headlines": hl,
                                "descriptions": ds, "final_url": c.get("final_url", ""), "path1": c.get("path1", ""), "path2": c.get("path2", ""),
                                "status": (status or "paused").title(), "editor": "draft", "_new": mark})
        elif kind in ("sitelinks", "callouts") and t == kind[:-1]:
            if c.get("edit"):
                for r in find(campaign="" if c.get("level") == "account" else c["campaign"], text=c["text"]):
                    r.setdefault("_draft", {})
                    for k, col in (("new_text", "text"), ("final_url", "final_url"), ("line1", "line1"), ("line2", "line2")):
                        if c.get(k) is not None:
                            r["_draft"][col] = {**mark, "value": c[k]}
            elif in_scope(c):
                for txt in c.get("texts", [c.get("text")]):
                    rows.insert(0, {"id": f"d-{it['id']}-{txt}", "campaign": c.get("campaign", ""), "level": "Account" if c.get("level") == "account" else "Campaign",
                                    "text": txt, "line1": c.get("line1", ""), "line2": c.get("line2", ""), "final_url": c.get("final_url", ""),
                                    "status": "Enabled", "editor": "draft", "_new": mark})


CHANGE_REFERENCE = """Draft changes use gae's plan format (one JSON object per change). Names must match Editor exactly.
New:
  {"type":"campaign","new":true,"campaign":"[15] X","budget":20,"bid_strategy":"Manual CPC"|"Maximize clicks","max_cpc_cap":2,
   "languages":"en","locations":[{"id":2826}]}            (new campaigns default Paused)
  {"type":"ad_group","new":true,"campaign":"..","ad_group":"..","max_cpc":1.2}
  {"type":"keyword","campaign":"..","ad_group":"..","match":"exact|phrase|broad","keywords":["..",".."],"max_cpc":1.5}
  {"type":"negative","campaign":"..","ad_group":"(optional)","match":"phrase","keywords":[".."]}
  {"type":"rsa","campaign":"..","ad_group":"..","final_url":"https://..","path1":"","path2":"",
   "headlines":["..", {"text":"..","pin":1}],"descriptions":[".."]}   (3-15 headlines <=30 chars, 2-4 descriptions <=90; default Paused)
  {"type":"sitelink","campaign":"..","text":"<=25","line1":"<=35","line2":"<=35","final_url":"https://.."}  ("level":"account" for account level)
  {"type":"callout","campaign":"..","texts":["<=25"]}
  {"type":"snippet","campaign":"..","header":"Destinations","values":["..",".."]}
  {"type":"excluded_location","campaign":"..","locations":[{"id":2356}]}
  {"type":"shared_list","campaign":"..","lists":["<shared negative list name>"]}
  {"type":"audience","campaign":"..","lists":["<audience list name>"]}
  {"type":"pmax_asset","campaign":"..","asset_group":"..","field":"headline|long_headline|description","texts":[".."]}
   (Performance Max text assets; headline <=30, long headline <=90, description <=90; live only, added Paused)
  {"type":"audience","campaign":"..","lists":[".."],"mode":"observation|targeting"}   (mode optional; with mode = live only)
Edit existing:
  {"type":"campaign","campaign":"..","status":"enabled|paused","budget":25}
  {"type":"campaign","campaign":"..","bid_strategy":"Target impression share","tis_percent":80,"max_cpc_cap":3}
  {"type":"ad_group","campaign":"..","ad_group":"..","status":"enabled|paused|removed","max_cpc":1.1}
  {"type":"keyword","edit":true,"campaign":"..","ad_group":"..","match":"exact","keywords":[".."],"max_cpc":1.4,"status":"paused"}
  {"type":"negative","remove":true,"campaign":"..","ad_group":"(optional)","match":"phrase","keywords":[".."]}
  {"type":"rsa","edit":true,"campaign":"..","ad_group":"..","local_id":123,"status":"paused","final_url":"..",
   "headlines":[..],"descriptions":[..]}   (text edits replace all headlines+descriptions of that one ad)
  {"type":"sitelink","edit":true,"campaign":"..","text":"<current link text>","new_text":"..","final_url":"..","line1":"..","line2":".."}
  {"type":"pmax_asset","remove":true,"campaign":"..","asset_group":"..","field":"headline","texts":["<current text>"]}   (live only)
  {"type":"audience","remove":true,"campaign":"..","lists":[".."]}   (detach audience lists; live only)
  {"type":"audience","edit":true,"campaign":"..","lists":[".."],"bid_modifier":30}   (bid adjustment, whole percent)
  {"type":"audience","campaign":"..","mode":"observation|targeting"}   (switch the campaign's audience setting; live only)
Optional on any change: nothing else. Hard checks: em dash, character limits, Bahamas. Warnings: cabins, yacht, guarantee, Expeditions, emojis."""
