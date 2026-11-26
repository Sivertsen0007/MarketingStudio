"""Ads Studio core - reads everything Google Ads Editor holds for an account, and keeps a shared draft
of changes that both the Studio UI and Claude (via MCP) edit. The draft is pushed into Editor with
`gae apply --restart`; Editor is then only the place where changes are reviewed and Posted.

Read side: Editor's database, read-only (immutable=1), re-opened per call so it is always current.
Draft: one JSON file per account under editor-changes/studio/ (draftstore collection ads:<acct>), guarded by a file lock.
"""
import contextlib, datetime as dt, fcntl, json, os, re, shutil, sqlite3, subprocess, sys, tempfile, time, uuid
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
                    "approval": r["approvalStatus"], "remote_id": r["remoteId"] or None})
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


# ---------- Google's own rules (T01 option B: block what Google refuses, warn on style) ----------
# Hard Google limits and clearly invalid input are problems (they block Send to Editor and Push live). Editorial /
# style points (CAPS, "!!", double spaces, a duplicate of a live row, a big budget jump, no locations) are warnings.
# The brand's own copy rules (em dash, banned words, ...) stay in team_settings (gae.lint / _warn).

KW_MAX_CHARS, KW_MAX_WORDS = 80, 10
KW_BAD_CHARS = set("!@%^*()={};~`<>?\\|,")  # Google Ads: "invalid characters" in keyword text
MATCHES = ("exact", "phrase", "broad")
STATUSES = {"campaign": ("enabled", "paused"), "ad_group": ("enabled", "paused", "removed"), "keyword": ("enabled", "paused", "removed"),
            "rsa": ("enabled", "paused", "removed")}
# Structured snippet headers Google accepts (English), https://support.google.com/google-ads/answer/6280012
SNIPPET_HEADERS = ("Amenities", "Brands", "Courses", "Degree programs", "Destinations", "Featured hotels", "Insurance coverage",
                   "Models", "Neighborhoods", "Service catalog", "Shows", "Styles", "Types")
AD_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿️‍]")
URL_RX = re.compile(r"^https?://[^\s/?#]+\.[^\s/?#]{2,}(?:[/?#]\S*)?$", re.I)
BIG_JUMP = 3  # a budget / bid more than this many times the live value is a warning


def _txt(x):
    return (x.get("text") if isinstance(x, dict) else x) if x is not None else None


def _norm(s):
    return " ".join(str(s or "").lower().split())


def _num(c, key, problems, label=None):
    """c[key] as a float, or None when absent. Not a number, or <= 0: a problem."""
    v = c.get(key)
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        problems.append(f"{label or key}: {v!r} is not a number")
        return None
    if f != f or f <= 0:  # NaN too
        problems.append(f"{label or key}: must be more than 0 (got {v})")
        return None
    return f


def _url_ok(u):
    return bool(u) and isinstance(u, str) and bool(URL_RX.match(u.strip())) and u == u.strip()


def _need(c, keys, problems):
    for k in keys:
        v = c.get(k)
        if v is None or (isinstance(v, str) and not v.strip()):
            problems.append(f"{k} is missing")


def _style(field, text, warnings, headline=False):
    """Editorial points Google may flag: warnings, never blocks."""
    if not isinstance(text, str) or not text:
        return
    if re.search(r"\b[A-Z]{4,}\b", text):
        warnings.append(f"{field}: written in CAPITALS (Google may disapprove excessive capitals)")
    if re.search(r"([!?])\1", text):
        warnings.append(f"{field}: repeated punctuation ({'!!' if '!!' in text else '??'}) is likely to be disapproved")
    elif headline and "!" in text:
        warnings.append(f"{field}: Google does not allow an exclamation mark in headlines")
    if "  " in text:
        warnings.append(f"{field}: double space")
    if text != text.strip():
        warnings.append(f"{field}: starts or ends with a space")


def _ad_text(field, text, limit, problems, warnings, headline=False):
    """One piece of ad text: empty and emoji block (length is gae.lint's), the rest is style."""
    if not isinstance(text, str) or not text.strip():
        problems.append(f"{field}: is empty")
        return
    if AD_EMOJI.search(text):
        problems.append(f"{field}: emoji are not allowed in Google ads")
    _style(field, text, warnings, headline)


def _status_ok(c, problems):
    t, st = c.get("type"), c.get("status")
    if st is None or t not in STATUSES:
        return
    if str(st).lower() not in STATUSES[t]:
        problems.append(f"status: must be {' or '.join(STATUSES[t])} (got {st!r})")


def _match_ok(c, problems, default=None):
    m = c.get("match", default)
    if m is None:
        problems.append("match is missing")
        return None
    if str(m).lower() not in MATCHES:
        problems.append(f"match: must be exact, phrase or broad (got {m!r})")
        return None
    return str(m).lower()


def _keyword_texts(c, problems, label="Keyword"):
    kws = c.get("keywords", [c.get("keyword")] if "keyword" in c else None)
    if not isinstance(kws, list) or not kws:
        problems.append("keywords: give one or more keywords")
        return []
    seen, out = set(), []
    for kw in kws:
        if not isinstance(kw, str) or not kw.strip():
            problems.append(f"{label}: an empty keyword")
            continue
        name = f'{label} "{kw}"'
        if kw.strip()[:1] in '["' or kw.strip()[-1:] in ']"':
            problems.append(f"{name}: match type syntax ([ ] or \" \") typed in the text - pick the match type instead")
        elif kw.strip().startswith(("-", "+")):
            problems.append(f"{name}: remove the leading {kw.strip()[0]!r} (use the match type and the negative list instead)")
        bad = sorted({ch for ch in kw if ch in KW_BAD_CHARS})
        if bad:
            problems.append(f"{name}: Google does not allow {' '.join(bad)} in keywords")
        if gae.width(kw) > KW_MAX_CHARS:
            problems.append(f"{name}: {gae.width(kw)} chars > {KW_MAX_CHARS}")
        if len(kw.split()) > KW_MAX_WORDS:
            problems.append(f"{name}: {len(kw.split())} words > {KW_MAX_WORDS}")
        if AD_EMOJI.search(kw):
            problems.append(f"{name}: emoji are not allowed")
        if _norm(kw) in seen:
            problems.append(f"{name}: listed twice")
        seen.add(_norm(kw))
        out.append(kw)
    return out


_KNOWN, KNOWN_TTL = {}, 20  # (acct, kind) -> (time, rows): one read serves a whole bulk add


def _known_rows(acct, kind):
    """What the account has now (for duplicate / jump warnings): the Google Ads API cache when that is the account's
    source, else Editor's database. Never fetches; anything unreadable is simply no rows."""
    if not acct:
        return []
    hit = _KNOWN.get((acct, kind))
    if hit and time.monotonic() - hit[0] < KNOWN_TTL:
        return hit[1]
    rows = _read_known(acct, kind)
    _KNOWN[(acct, kind)] = (time.monotonic(), rows)
    return rows


def _read_known(acct, kind):
    try:
        import gapi  # noqa: PLC0415 - gapi imports core
        if gapi.source(acct) == "api":
            rows = gapi.cached_rows(acct, kind)
            if rows is not None:
                return rows
        db = _db(acct)
        try:
            return GRIDS[kind](Ctx(db))
        finally:
            db.close()
    except (Exception, SystemExit):  # noqa: BLE001 - no Editor, no cache: no live context, no warning
        return []


def _live(rows):
    return [r for r in rows if r.get("status") != "Removed"]


def _blocks(neg, match, kw):
    """Does negative keyword `neg` (match type) block search keyword text `kw`?"""
    n, k = _norm(neg).split(), _norm(kw).split()
    if not n:
        return False
    if match == "exact":
        return n == k
    if match == "phrase":
        return any(k[i:i + len(n)] == n for i in range(len(k) - len(n) + 1))
    return set(n) <= set(k)


def _jump(field, new, old, warnings):
    if new and old and new > old * BIG_JUMP:
        warnings.append(f"{field}: {new:.2f} is more than {BIG_JUMP}x the current {old:.2f}")


def _same_scope(r, c):
    return _norm(r.get("campaign")) == _norm(c.get("campaign")) and \
        (not c.get("ad_group") or _norm(r.get("ad_group")) == _norm(c.get("ad_group")))


def _r_campaign(c, acct, problems, warnings):
    _need(c, ["campaign"], problems)
    _status_ok(c, problems)
    budget = _num(c, "budget", problems)
    for k in ("max_cpc_cap", "target_cpa"):
        _num(c, k, problems)
    if c.get("tis_percent") is not None:
        try:
            if not 0 < float(c["tis_percent"]) <= 100:
                problems.append(f"tis_percent: must be 1-100 (got {c['tis_percent']})")
        except (TypeError, ValueError):
            problems.append(f"tis_percent: {c['tis_percent']!r} is not a number")
    if c.get("bid_strategy") and str(c["bid_strategy"]).lower() == "target impression share":
        _need(c, ["tis_percent", "max_cpc_cap"], problems)
    s, e = c.get("start_date"), c.get("end_date")
    for k in ("start_date", "end_date"):
        if c.get(k) and not re.fullmatch(r"\d{4}-?\d{2}-?\d{2}", str(c[k])):
            problems.append(f"{k}: use YYYY-MM-DD (got {c[k]!r})")
    if s and e and str(e).replace("-", "") < str(s).replace("-", ""):
        problems.append(f"end_date {e} is before start_date {s}")
    camps = _known_rows(acct, "campaigns")
    if c.get("new"):
        if c.get("budget") in (None, ""):
            problems.append("budget is missing (a daily budget more than 0)")
        bid = str(c.get("bid_strategy", "Manual CPC")).lower()
        if bid not in gae.BID_CODE:
            problems.append(f"bid_strategy: a new campaign takes Manual CPC or Maximize clicks (got {c['bid_strategy']!r})")
        if c.get("campaign") and any(_norm(r.get("campaign")) == _norm(c["campaign"]) for r in _live(camps)):
            problems.append(f'a campaign called "{c["campaign"]}" already exists')
        if not c.get("locations"):
            warnings.append("no locations: the campaign will show everywhere in the world")
        for loc in c.get("locations") or []:
            if not isinstance(loc, dict) or not str(loc.get("id", "")).isdigit():
                problems.append(f"locations: {loc!r} needs a numeric id")
        return
    if budget is not None:
        old = next((r.get("budget") for r in camps if _norm(r.get("campaign")) == _norm(c.get("campaign"))), None)
        _jump("budget", budget, old, warnings)


def _r_ad_group(c, acct, problems, warnings):
    _need(c, ["campaign", "ad_group"], problems)
    _status_ok(c, problems)
    cpc = _num(c, "max_cpc", problems)
    groups = [r for r in _known_rows(acct, "ad_groups") if _norm(r.get("campaign")) == _norm(c.get("campaign"))]
    mine = [r for r in groups if _norm(r.get("ad_group")) == _norm(c.get("ad_group"))]
    if c.get("new"):
        if c.get("ad_group") and _live(mine):
            problems.append(f'"{c["campaign"]}" already has an ad group called "{c["ad_group"]}"')
    elif cpc is not None and mine:
        _jump("max_cpc", cpc, mine[0].get("max_cpc"), warnings)


def _r_keyword(c, acct, problems, warnings):
    _need(c, ["campaign", "ad_group"], problems)
    _status_ok(c, problems)
    match = _match_ok(c, problems)
    cpc = _num(c, "max_cpc", problems)
    kws = _keyword_texts(c, problems)
    if c.get("final_url") and not _url_ok(c["final_url"]):
        problems.append(f"final_url: {c['final_url']!r} is not a valid web address (https://...)")
    if not match or not kws:
        return
    live = [r for r in _live(_known_rows(acct, "keywords")) if _same_scope(r, c)]
    for kw in kws:
        hits = [r for r in live if _norm(r.get("keyword")) == _norm(kw) and str(r.get("match", "")).lower() == match]
        if not c.get("edit") and hits:
            warnings.append(f'Keyword "{kw}" ({match}) is already in {c.get("ad_group")}')
        elif c.get("edit") and cpc is not None and hits:
            _jump(f'max_cpc of "{kw}"', cpc, hits[0].get("max_cpc"), warnings)


def _r_negative(c, acct, problems, warnings):
    _need(c, ["campaign"], problems)
    match = _match_ok(c, problems, "phrase")
    kws = _keyword_texts(c, problems, "Negative")
    if not match or not kws or c.get("remove"):
        return
    negs = [r for r in _live(_known_rows(acct, "negatives")) if r.get("level") != "Shared list" and
            _norm(r.get("campaign")) == _norm(c.get("campaign")) and _norm(r.get("ad_group")) == _norm(c.get("ad_group"))]
    pos = [r for r in _live(_known_rows(acct, "keywords")) if _same_scope(r, c)]
    for kw in kws:
        if any(_norm(r.get("keyword")) == _norm(kw) and str(r.get("match", "")).lower() == match for r in negs):
            warnings.append(f'Negative "{kw}" ({match}) is already there (it is skipped)')
        blocked = sorted({r["keyword"] for r in pos if r.get("keyword") and _blocks(kw, match, r["keyword"])})
        if blocked:
            more = f" and {len(blocked) - 3} more" if len(blocked) > 3 else ""
            warnings.append(f'Negative "{kw}" ({match}) blocks live keywords: ' + ", ".join(blocked[:3]) + more)


def _r_rsa(c, acct, problems, warnings):
    _need(c, ["campaign", "ad_group"], problems)
    _status_ok(c, problems)
    if c.get("final_url") not in (None, "") or not c.get("edit"):
        if not _url_ok(c.get("final_url")):
            problems.append(f"final_url: {c.get('final_url')!r} is not a valid web address (https://...)" if c.get("final_url")
                            else "final_url is missing")
    if c.get("path2") and not c.get("path1"):
        problems.append("Path 2 needs Path 1")
    for k in ("path1", "path2"):
        v = c.get(k)
        if v and (" " in v.strip() or AD_EMOJI.search(v)):
            problems.append(f"{k.replace('path', 'Path ')}: no spaces or emoji in a display path")
    hl, ds = c.get("headlines"), c.get("descriptions")
    if c.get("edit") and not hl and not ds:
        return
    if c.get("edit") and bool(hl) != bool(ds):
        problems.append("a text edit gives both headlines and descriptions (they replace the ad's text)")
    if not isinstance(hl, list) or not 3 <= len(hl) <= 15:
        problems.append(f"headlines: an RSA needs 3-15 (got {len(hl) if isinstance(hl, list) else 0})")
    if not isinstance(ds, list) or not 2 <= len(ds) <= 4:
        problems.append(f"descriptions: an RSA needs 2-4 (got {len(ds) if isinstance(ds, list) else 0})")
    for items, label, limit, pins in ((hl, "Headline", 30, 3), (ds, "Description", 90, 2)):
        seen = set()
        for i, x in enumerate(items if isinstance(items, list) else [], 1):
            field, text = f"{label} {i}", _txt(x)
            _ad_text(field, text, limit, problems, warnings, headline=label == "Headline")
            pin = x.get("pin") if isinstance(x, dict) else None
            if pin not in (None, "", 0, "0"):
                try:
                    ok = 1 <= int(pin) <= pins
                except (TypeError, ValueError):
                    ok = False
                if not ok:
                    problems.append(f"{field} position: pin must be 1-{pins} (got {pin!r})")
            if isinstance(text, str) and text.strip():
                if _norm(text) in seen:
                    problems.append(f"{field}: the same text is used twice")
                seen.add(_norm(text))


def _r_sitelink(c, acct, problems, warnings):
    if c.get("level") != "account":
        _need(c, ["campaign"], problems)
    if c.get("remove"):
        if not c.get("texts") and not c.get("text"):
            problems.append("texts: name the sitelinks to remove")
        return
    if c.get("edit"):
        _need(c, ["text"], problems)
        if c.get("final_url") and not _url_ok(c["final_url"]):
            problems.append(f"final_url: {c['final_url']!r} is not a valid web address (https://...)")
        if c.get("new_text") is not None:
            _ad_text("Link Text", c["new_text"], 25, problems, warnings)
        return
    _ad_text("Link Text", c.get("text"), 25, problems, warnings)
    if not _url_ok(c.get("final_url")):
        problems.append(f"final_url: {c.get('final_url')!r} is not a valid web address (https://...)" if c.get("final_url")
                        else "final_url is missing")
    if bool(c.get("line1")) != bool(c.get("line2")):
        problems.append("Description line 1 and 2: give both or neither")
    for k, col in (("line1", "Description line 1"), ("line2", "Description line 2")):
        if c.get(k):
            _ad_text(col, c[k], 35, problems, warnings)


def _r_callout(c, acct, problems, warnings):
    _need(c, ["campaign"] if c.get("level") != "account" else [], problems)
    texts = c.get("texts", [c.get("text")] if "text" in c else None)
    if not isinstance(texts, list) or not texts:
        problems.append("texts: give one or more callouts")
        return
    if c.get("remove"):
        return
    seen = set()
    for i, t in enumerate(texts, 1):
        _ad_text(f"Callout text {i}", t, 25, problems, warnings)
        if isinstance(t, str) and t.strip():
            if _norm(t) in seen:
                problems.append(f'Callout text {i}: "{t}" is listed twice')
            seen.add(_norm(t))


def _r_snippet(c, acct, problems, warnings):
    _need(c, ["campaign", "header"], problems)
    if c.get("header") and c["header"] not in SNIPPET_HEADERS:
        hit = next((h for h in SNIPPET_HEADERS if h.lower() == str(c["header"]).strip().lower()), None)
        problems.append(f'header: write "{hit}"' if hit else f"header: must be one of {', '.join(SNIPPET_HEADERS)} (got {c['header']!r})")
    vals = c.get("values")
    if not isinstance(vals, list) or not 3 <= len(vals) <= 10:
        problems.append(f"values: a snippet needs 3-10 (got {len(vals) if isinstance(vals, list) else 0})")
        vals = vals if isinstance(vals, list) else []
    seen = set()
    for i, v in enumerate(vals, 1):
        _ad_text(f"Snippet value {i}", v, 25, problems, warnings)
        if isinstance(v, str) and v.strip():
            if gae.width(v) > 25:
                problems.append(f"Snippet value {i}: {gae.width(v)} chars > 25 ({v!r})")
            if _norm(v) in seen:
                problems.append(f'Snippet value {i}: "{v}" is listed twice')
            seen.add(_norm(v))


def _r_price(c, acct, problems, warnings):
    _need(c, ["campaign"], problems)
    if c.get("edit"):
        prices = c.get("prices")
        if not isinstance(prices, dict) or not prices:
            problems.append('prices: give {"<item header>": <new price>, ...}')
            return
        for h, v in prices.items():
            _num({"p": v}, "p", problems, f'price of "{h}"')
        return
    _need(c, ["price_type"], problems)
    items = c.get("items")
    if not isinstance(items, list) or not 3 <= len(items) <= 8:
        problems.append(f"items: a price asset needs 3-8 (got {len(items) if isinstance(items, list) else 0})")
        items = items if isinstance(items, list) else []
    seen = set()
    for i, it in enumerate(items, 1):
        if not isinstance(it, dict):
            problems.append(f"Item {i}: give header, description, price and final_url")
            continue
        for k, col in (("header", "header"), ("description", "description")):
            if not str(it.get(k) or "").strip():
                problems.append(f"Item {i} {col} is missing")
        _num(it, "price", problems, f"Item {i} price")
        if it.get("price") in (None, ""):
            problems.append(f"Item {i} price is missing")
        if not _url_ok(it.get("final_url")):
            problems.append(f"Item {i} final_url: {it.get('final_url')!r} is not a valid web address (https://...)"
                            if it.get("final_url") else f"Item {i} final_url is missing")
        if it.get("header"):
            if _norm(it["header"]) in seen:
                problems.append(f'Item {i} header: "{it["header"]}" is used twice')
            seen.add(_norm(it["header"]))


def _r_promotion(c, acct, problems, warnings):
    _need(c, ["campaign", "target", "percent"], problems)
    if c.get("target"):
        _ad_text("Item", c["target"], 20, problems, warnings)
        if gae.width(c["target"]) > 20:
            problems.append(f"Item: {gae.width(c['target'])} chars > 20 ({c['target']!r})")
    if c.get("percent") not in (None, ""):
        try:
            if not 0 < float(c["percent"]) <= 100:
                problems.append(f"percent: must be 1-100 (got {c['percent']})")
        except (TypeError, ValueError):
            problems.append(f"percent: {c['percent']!r} is not a number")
    if not _url_ok(c.get("final_url")):
        problems.append(f"final_url: {c.get('final_url')!r} is not a valid web address (https://...)" if c.get("final_url")
                        else "final_url is missing")
    s, e = c.get("start_date"), c.get("end_date")
    if s and e and str(e).replace("-", "") < str(s).replace("-", ""):
        problems.append(f"end_date {e} is before start_date {s}")


def _r_locations(c, acct, problems, warnings):
    _need(c, ["campaign"], problems)
    locs = c.get("locations")
    if not isinstance(locs, list) or not locs:
        problems.append("locations: give one or more {\"id\": <location id>}")
        return
    for loc in locs:
        if not isinstance(loc, dict) or not str(loc.get("id", "")).isdigit():
            problems.append(f"locations: {loc!r} needs a numeric id")


def _r_lists(c, acct, problems, warnings):
    _need(c, ["campaign"], problems)
    if not isinstance(c.get("lists"), list) or not c["lists"] or not all(isinstance(x, str) and x.strip() for x in c["lists"]):
        problems.append("lists: name one or more lists")
    if c.get("type") == "audience" and c.get("edit"):
        try:
            int(c.get("bid_modifier"))
        except (TypeError, ValueError):
            problems.append("bid_modifier: a whole percent, e.g. 30 or -20")


RULES = {"campaign": _r_campaign, "ad_group": _r_ad_group, "keyword": _r_keyword, "negative": _r_negative, "rsa": _r_rsa,
         "sitelink": _r_sitelink, "callout": _r_callout, "snippet": _r_snippet, "price": _r_price, "promotion": _r_promotion,
         "location": _r_locations, "excluded_location": _r_locations, "shared_list": _r_lists, "audience": _r_lists}


def google_rules(change, acct=None):
    """-> (problems, warnings) from Google's own rules for one Editor-type change. acct (optional) adds what needs the
    account's current rows: duplicate names, duplicates of live rows, negatives that block live keywords, >3x jumps."""
    problems, warnings = [], []
    fn = RULES.get(change.get("type"))
    if fn:
        try:
            fn(change, acct, problems, warnings)
        except Exception as ex:  # noqa: BLE001 - a malformed change is a readable problem, never a crash
            problems.append(f"invalid change: {_readable(ex)}")
    return list(dict.fromkeys(problems)), list(dict.fromkeys(warnings))


def _readable(ex):
    if isinstance(ex, KeyError):
        return f"{ex.args[0]} is missing"
    return str(ex)


def _unrow(p):
    """gae.lint says "row 1 Headline 2: 31 chars > 30"; keep the field name, drop only the row number (G8)."""
    return re.sub(r"^row \d+ ", "", p)


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
            problems += [_unrow(p) for p in gae.lint(rows)]
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
    """-> (problems that block, warnings). Uses gae's own lint so Studio and gae never disagree, plus Google's own rules
    (google_rules). acct (optional) adds the checks that need the account's current rows."""
    if not isinstance(change, dict):
        return ["a change must be a JSON object"], []
    if live_only(change):
        return _check_live_only(change, acct)
    t = change.get("type")
    if t not in gae.SUPPORTED:
        return [f"type {t!r} cannot be written into Editor yet"], []
    problems, warnings = google_rules(change, acct)
    try:
        rows = gae.plan_rows(change)
    except (KeyError, ValueError, TypeError, AttributeError) as ex:
        rows = []
        if not problems:  # google_rules names the field already; this is only the fallback
            problems.append(f"invalid change: {_readable(ex)}")
    lint = [_unrow(p) for p in gae.lint(rows)]
    if t == "promotion":  # Google's promotion item limit is 20 (google_rules), not the Editor column's 25
        lint = [p for p in lint if not p.startswith("Item:")]
    problems += lint
    if t == "sitelink" and change.get("edit"):  # gae.plan_rows skips edits, so check the new text here
        for k, lim in (("new_text", 25), ("line1", 35), ("line2", 35)):
            if gae.width(change.get(k) or "") > lim:
                problems.append(f"{k}: {gae.width(change[k])} chars > {lim}")
        rows = [{k: change[k] for k in ("new_text", "line1", "line2") if change.get(k)}]
        problems += [_unrow(p) for p in gae.lint(rows)]
    return list(dict.fromkeys(problems)), list(dict.fromkeys(warnings + _warn(rows)))


def add(acct, changes, by="you", note=None):
    """Add plan changes (gae plan format) to the draft. Returns the new items, each with its problems/warnings.
    Refuses (ValueError, nothing added) when a change touches an experiment's trial campaign (experiments.py)."""
    if isinstance(changes, dict):
        changes = [changes]
    if not changes:  # G15: e.g. a bulk Remove where every selected negative sits in a shared list
        raise ValueError("Nothing was added to the draft: there were no changes to add. Negatives in a shared list "
                         "can't be removed one by one here; edit the shared list itself")
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
            own = list(problems)
            if not problems:
                try:
                    db.execute("savepoint s")
                    gae.apply_changes(db, [it["change"]], "Studio validate", [])
                    db.execute("release s")
                except Exception as ex:  # noqa: BLE001 - every failure is reported per item
                    db.execute("rollback to s")
                    db.execute("release s")
                    problems = [_readable(ex)]
            results.append({"id": it["id"], "summary": it["summary"], "ok": not problems, "problems": problems, "warnings": warnings,
                            "own_problems": own})
        db.close()
    with _locked(acct) as d:
        # G2: what only Editor's dry run refused is kept apart (editor_problems): it blocks Send to Editor (push() reads
        # these results), never Push live. The item's own problems stay core.check's; the Editor note shows as a warning.
        by_id = {r["id"]: r for r in results if not r.get("live_only")}
        for it in d["items"]:
            r = by_id.get(it["id"])
            if r is None:
                continue
            own = r.get("own_problems", r["problems"])
            ed = [p for p in r["problems"] if p not in own]
            it["problems"], it["editor_problems"] = own, ed
            it["warnings"] = r["warnings"] + [f"Send to Editor: {p}" for p in ed]
    for r in results:
        r.pop("own_problems", None)
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
                    if c.get("ad_id") not in (None, ""):  # Google's ad id (API grids); Editor rows carry it as remote_id
                        if str(c["ad_id"]) not in (str(r.get("ad_id")), str(r.get("remote_id"))):
                            continue
                    elif c.get("local_id") is not None and str(r["id"]) != str(c["local_id"]):
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
Optional on any change: nothing else.
Hard checks (block): the copy rules set to block (e.g. em dash, Bahamas), character limits, Google's rules: empty or
duplicate texts, 3-15 headlines / 2-4 descriptions, pins 1-3 / 1-2, valid final URL, Path 2 needs Path 1, emoji in ad
text, keywords (<=80 chars, <=10 words, no ! @ % ^ * ( ) = { } ; ~ ` < > ? \\ | , and no [ ] or " " match syntax),
snippet header from Google's list with 3-10 values <=25, price 3-8 items, promotion item <=20, sitelink lines both or
neither, budget / bids > 0, a new campaign's name not already used, end date not before start.
Warnings: the copy rules set to warn, CAPITALS, "!!", double spaces, duplicates of live rows, negatives that block live
keywords, budget / bid more than 3x the current, a new campaign with no locations."""
