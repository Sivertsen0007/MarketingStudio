"""Ads Studio <- Google Ads API (read side). Editor becomes optional.

Builds the same row dicts as core.grid / core.tree (same keys, so the UI, changeFor, ADD and the dialogs keep
working), straight from GAQL via live.query, and adds stats for a date window:
  cost, impressions, clicks, ctr (%), cpc, conversions, conv_value, cost_per_conv

IDs: rows carry Google's own ids. Campaign / ad group rows use the Google campaign / ad group id as `id`,
`campaign_id`, `ad_group_id` (ints, so the tree and grid scope work as with Editor's local ids). Ad rows use the
Google ad id as `id` (and `ad_id`), so the existing draft change {"type":"rsa","edit":true,"local_id":<row id>}
still works: live.Ids resolves `local_id` as a Google ad id first when the source is "api" (and falls back to
Editor's local id), see live.Ids.rsa_ads. Every other change refers to campaigns / ad groups by name, as before.

Two caches per account in core.STUDIO_DIR. When they are fresh is datapolicy's rule: in "manual" mode (the default)
a cache is kept until the user refreshes (refresh(), the Refresh menu, or Push live / Undo for that account); in "auto"
mode they are 15 minutes fresh, stale served at once and refreshed in a background thread:
  gapi_cache_<cid>_base.json        structure (all rows, no stats)
  gapi_cache_<cid>_<window>.json    stats for one window (LAST_7_DAYS, LAST_14_DAYS, LAST_30_DAYS or YYYY-MM-DD_YYYY-MM-DD)

Read only: GAQL searchStream. Nothing here writes to Google Ads.

Experiments: the base cache also holds "experiments" (see experiments.py: trial campaigns, and base campaigns with a
running experiment); grid and tree rows carry experiment = "trial" (read-only in the UI) or "base".

Fields Editor has that the API does not give (left empty): comment (Editor-only note), editor (Editor's unposted
state; always "" here, "draft" for draft rows). Asset performance labels (Best/Good/Low) are gone from
asset_group_asset in API v23; asset group asset rows carry primary_status plus real stats instead.
Formats that differ from Editor: ad_strength and approval are Google's words ("Excellent", "Approved") where
Editor stores codes; price_type / promotion occasion are Google's enum in title case; location paths are
Google's canonical names.
"""
import contextlib, datetime as dt, fcntl, json, re, threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import core
import datapolicy
import experiments
import live

gae = core.gae
FRESH = 15 * 60
SETTINGS = core.STUDIO_DIR / "settings.json"
STATUS = {"ENABLED": "Enabled", "PAUSED": "Paused", "REMOVED": "Removed"}
ST_CODE = {"Enabled": 0, "Paused": 1, "Removed": 2}
CHANNEL = {"SEARCH": "Search", "PERFORMANCE_MAX": "Performance Max", "DISPLAY": "Display", "DEMAND_GEN": "Demand Gen",
           "VIDEO": "Video", "SHOPPING": "Shopping", "MULTI_CHANNEL": "App"}
BIDDING = {"MANUAL_CPC": "Manual CPC", "TARGET_SPEND": "Maximize clicks", "MAXIMIZE_CONVERSIONS": "Maximize conversions",
           "MAXIMIZE_CONVERSION_VALUE": "Maximize conversion value", "TARGET_IMPRESSION_SHARE": "Target impression share",
           "TARGET_CPA": "Target CPA", "TARGET_ROAS": "Target ROAS", "MANUAL_CPM": "Manual CPM", "TARGET_CPM": "Target CPM"}
AD_TYPE = {"RESPONSIVE_SEARCH_AD": "Responsive search ad", "EXPANDED_DYNAMIC_SEARCH_AD": "Dynamic search ad"}
EXT_FIELDS = ("SITELINK", "CALLOUT", "STRUCTURED_SNIPPET", "PRICE", "PROMOTION")
EXT_KIND = {"SITELINK": "sitelinks", "CALLOUT": "callouts", "STRUCTURED_SNIPPET": "snippets", "PRICE": "prices", "PROMOTION": "promotions"}
AUD_TYPES = ("USER_LIST", "USER_INTEREST", "CUSTOM_AUDIENCE", "COMBINED_AUDIENCE", "LIFE_EVENT", "CUSTOM_AFFINITY")
METRIC_KEYS = ("cost", "impressions", "clicks", "ctr", "cpc", "conversions", "conv_value", "cost_per_conv", "select_cabin", "cost_per_select_cabin")
# ML's cabin-selection conversion (GA4 select_cabin), counted from all_conversions so it shows whether it is primary or secondary
SELECT_CABIN = "select_cabin"
SC = "segments.conversion_action_name, metrics.all_conversions"
M = "metrics.cost_micros, metrics.impressions, metrics.clicks, metrics.conversions, metrics.conversions_value"

KINDS = ("campaigns", "ad_groups", "keywords", "negatives", "ads", "asset_groups", "sitelinks", "callouts", "snippets", "prices",
         "promotions", "locations", "audiences", "shared_lists")
EXTRA_KINDS = ("asset_group_assets", "user_lists", "recommendations")  # API only, no Editor equivalent
NO_STATS = {"negatives", "shared_lists", "user_lists", "recommendations"}


# ---------- small helpers ----------

def _st(v):
    return STATUS.get(v, (v or "").title() or None)


def _title(v):
    return (v or "").replace("_", " ").capitalize()


def _words(v):
    return " ".join(w.capitalize() for w in (v or "").split("_"))


def _money(m):
    return round(int(m) / 1e6, 2) if m not in (None, "", "0", 0) else None


def _ymd(s):
    """'2023-05-24 14:48:28' or '2023-05-24' -> 20230524 (Editor's format)."""
    return int(s[:10].replace("-", "")) if s else ""


def _bm(v):
    """Google bid modifier (1.3) -> Editor's whole percent (30). Not set -> None."""
    return round((float(v) - 1) * 100) if v not in (None, "") else None


def _path(name):
    return re.sub(r",\s*", ", ", name or "")


def _tail(res):
    return res.rsplit("/", 1)[1] if res else ""


def _pin(x, kind):
    p = x.get("pinnedField") or ""
    return int(p.rsplit("_", 1)[1]) if p.startswith(kind + "_") else 0


def _cid(acct):
    return core.resolve(acct)


# ---------- source switch (settings.json, shared with meta_ads.settings) ----------

@contextlib.contextmanager
def _settings(write=False):
    core.STUDIO_DIR.mkdir(parents=True, exist_ok=True)
    with open(core.STUDIO_DIR / ".settings.lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX if write else fcntl.LOCK_SH)
        try:
            s = json.loads(SETTINGS.read_text())
        except (OSError, ValueError):
            s = {}
        yield s
        if write:
            tmp = SETTINGS.with_suffix(".gapi.tmp")
            tmp.write_text(json.dumps(s, indent=1, ensure_ascii=False))
            tmp.replace(SETTINGS)


def source(acct):
    """'api' (default) or 'editor': where Studio reads this Google account from."""
    with _settings() as s:
        return (s.get("google_source") or {}).get(_cid(acct), "api")


def set_source(acct, value):
    if value not in ("api", "editor"):
        raise ValueError("source must be 'api' or 'editor'")
    with _settings(write=True) as s:  # read-modify-write: other keys (meta_accounts, views) are kept
        s.setdefault("google_source", {})[_cid(acct)] = value
    return value


# ---------- date window ----------

def window(days=None, start=None, end=None):
    """-> (cache key, GAQL date condition). days: 7/14/30 (int or str), 'LAST_30_DAYS', (start, end), or start+end."""
    if isinstance(days, (tuple, list)):
        start, end = days
        days = None
    if start or end:
        for d in (start, end):
            if not d or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(d)):
                raise ValueError("custom range needs start and end as YYYY-MM-DD")
            dt.date.fromisoformat(str(d))
        if str(start) > str(end):
            raise ValueError("start is after end")
        return f"{start}_{end}", f"segments.date BETWEEN '{start}' AND '{end}'"
    if days in (None, ""):
        days = 7
    key = str(days).upper()
    if key.isdigit() and int(key) not in (7, 14, 30):
        # GAQL has no LAST_90_DAYS etc.: turn other day counts into a date range ending yesterday
        n = int(key)
        if not 1 <= n <= 730:
            raise ValueError("days must be between 1 and 730")
        e = dt.date.today() - dt.timedelta(days=1)
        return window(None, (e - dt.timedelta(days=n - 1)).isoformat(), e.isoformat())
    if key.isdigit():
        key = f"LAST_{int(key)}_DAYS"
    if key not in ("LAST_7_DAYS", "LAST_14_DAYS", "LAST_30_DAYS", "YESTERDAY", "TODAY", "THIS_MONTH", "LAST_MONTH"):
        raise ValueError(f"unsupported window {days!r}: use 7, 14, 30 or a start/end date")
    return key, f"segments.date DURING {key}"


# ---------- caches ----------

_lock = threading.Lock()
_bg = set()


def _file(cid, key):
    core.STUDIO_DIR.mkdir(parents=True, exist_ok=True)
    return core.STUDIO_DIR / f"gapi_cache_{cid}_{key}.json"


def _write(f, data):
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    tmp.replace(f)


def _age(d):
    return (dt.datetime.now() - dt.datetime.fromisoformat(d["fetched_at"])).total_seconds()


def _bg_run(tag, fn):
    try:
        fn()
    except Exception:  # noqa: BLE001 - the next load tries again
        pass
    finally:
        _bg.discard(tag)


def _filled(d):
    """Epoch when this cached copy was written."""
    try:
        return d.get("_at") or dt.datetime.fromisoformat(d["fetched_at"]).timestamp()
    except (KeyError, TypeError, ValueError):
        return 0


def _read(cid, key):
    f = _file(cid, key)
    try:
        return json.loads(f.read_text()) if f.exists() else None
    except ValueError:
        return None


def is_fresh(cid, key, max_age=FRESH):
    d = _read(cid, key)
    return d is not None and (max_age is None or datapolicy.fresh(f"google:{cid}", _age(d), max_age, _filled(d)))


def _load(cid, key, build, max_age=FRESH, wait=False):
    d = _read(cid, key)
    if d is not None:
        if max_age is None or datapolicy.fresh(f"google:{cid}", _age(d), max_age, _filled(d)):
            return d
        if not wait and datapolicy.background_ok() and _filled(d) >= datapolicy.refreshed_at(f"google:{cid}"):
            tag = (cid, key)  # auto mode only: serve the stale copy, re-fetch behind it
            if tag not in _bg:
                _bg.add(tag)
                threading.Thread(target=_bg_run, args=(tag, lambda: _save(cid, key, build)), daemon=True).start()
            return d
    return _save(cid, key, build)  # nothing cached (or refreshed since): fetch once


def _save(cid, key, build):
    with _lock if key == "base" else contextlib.nullcontext():
        d = build(cid)
    d["fetched_at"] = dt.datetime.now().isoformat(timespec="seconds")
    d["_at"] = dt.datetime.now().timestamp()
    _write(_file(cid, key), d)
    datapolicy.mark(f"google:{cid}" if key == "base" else f"google:{cid}:{key}")
    return d


def cached_rows(acct, kind):
    """Rows of one kind from the structure cache as it is on disk (core.google_rules' live context). Never fetches:
    None when nothing is cached."""
    d = _read(_cid(acct), "base")
    return None if d is None else list((d.get("rows") or {}).get(kind, []))


def base(acct, max_age=FRESH, wait=False):
    return _load(_cid(acct), "base", _build_base, max_age, wait)


def stats(acct, days=None, start=None, end=None, max_age=FRESH, wait=False):
    key, cond = window(days, start, end)
    return _load(_cid(acct), key, lambda cid: _build_stats(cid, cond, key), max_age, wait)


def refresh(acct, days=None, start=None, end=None):
    """Pull structure (and stats for the window) now: the Refresh menu, and after a live push or undo of this account.
    Stats for other date ranges cached before this refresh are fetched again (once) when next viewed. Read-only."""
    cid = _cid(acct)
    t0 = dt.datetime.now().timestamp()
    tag = (cid, "refresh")
    _bg.add(tag)
    try:
        b = _save(cid, "base", _build_base)
        key, cond = window(days, start, end)
        s = _save(cid, key, lambda c: _build_stats(c, cond, key))
    finally:
        _bg.discard(tag)
    datapolicy.invalidate(f"google:{cid}", at=t0)
    _ACCT_NAMES.pop(cid, None)
    return {"fetched_at": b["fetched_at"], "stats_fetched_at": s["fetched_at"], "window": key}


def info(acct, days=None, start=None, end=None):
    """For the header: where rows come from, how old they are, whether a refresh runs."""
    cid = _cid(acct)
    key, _ = window(days, start, end)
    out = {"source": source(cid), "window": key, "refreshing": any(t[0] == cid for t in _bg), "stats_cached": is_fresh(cid, key)}
    for k, name in (("base", "fetched_at"), (key, "stats_fetched_at")):
        f = _file(cid, k)
        try:
            out[name] = json.loads(f.read_text())["fetched_at"] if f.exists() else None
        except (ValueError, KeyError):
            out[name] = None
    return out


# ---------- the pull ----------

def _parallel(cid, queries):
    """{name: gaql} -> {name: rows}. A failed query gives [] and is listed under '_errors'."""
    live.gads.headers()  # warm the token once before threads ask for it
    out, errors = {}, {}

    def one(item):
        name, q = item
        try:
            return name, live.query(cid, q), None
        except Exception as ex:  # noqa: BLE001
            return name, [], str(ex)[:300]

    with ThreadPoolExecutor(max_workers=6) as pool:
        for name, rows, err in pool.map(one, queries.items()):
            out[name] = rows
            if err:
                errors[name] = err
    out["_errors"] = errors
    return out


def _build_base(cid):
    q = _parallel(cid, {
        "campaigns": "SELECT campaign.id, campaign.name, campaign.status, campaign.advertising_channel_type, campaign.bidding_strategy_type, "
                     "campaign.experiment_type, "
                     "campaign.start_date_time, campaign.end_date_time, campaign.target_spend.cpc_bid_ceiling_micros, "
                     "campaign.target_impression_share.location_fraction_micros, campaign.target_impression_share.cpc_bid_ceiling_micros, "
                     "campaign.maximize_conversions.target_cpa_micros, campaign.target_cpa.target_cpa_micros, "
                     "campaign.targeting_setting.target_restrictions, campaign_budget.amount_micros, campaign_budget.name, "
                     "campaign_budget.explicitly_shared FROM campaign",
        "languages": "SELECT campaign.id, campaign_criterion.language.language_constant FROM campaign_criterion "
                     "WHERE campaign_criterion.type = LANGUAGE AND campaign_criterion.status != REMOVED",
        "lang_codes": "SELECT language_constant.id, language_constant.code FROM language_constant",
        "experiment_arms": experiments.ARMS_GAQL,
        "ad_groups": "SELECT ad_group.id, ad_group.name, ad_group.status, ad_group.cpc_bid_micros, campaign.id FROM ad_group",
        "criteria": "SELECT ad_group.id, campaign.id, ad_group_criterion.criterion_id, ad_group_criterion.status, ad_group_criterion.negative, "
                    "ad_group_criterion.keyword.text, ad_group_criterion.keyword.match_type, ad_group_criterion.cpc_bid_micros, "
                    "ad_group_criterion.quality_info.quality_score, ad_group_criterion.position_estimates.first_page_cpc_micros, "
                    "ad_group_criterion.position_estimates.top_of_page_cpc_micros, ad_group_criterion.final_urls "
                    "FROM ad_group_criterion WHERE ad_group_criterion.type = KEYWORD",
        "camp_negatives": "SELECT campaign.id, campaign_criterion.criterion_id, campaign_criterion.status, campaign_criterion.keyword.text, "
                          "campaign_criterion.keyword.match_type FROM campaign_criterion "
                          "WHERE campaign_criterion.type = KEYWORD AND campaign_criterion.negative = TRUE",
        "shared_sets": "SELECT shared_set.id, shared_set.name, shared_set.status, shared_set.member_count FROM shared_set "
                       "WHERE shared_set.type = NEGATIVE_KEYWORDS",
        "shared_criteria": "SELECT shared_set.id, shared_criterion.criterion_id, shared_criterion.keyword.text, shared_criterion.keyword.match_type "
                           "FROM shared_criterion WHERE shared_criterion.type = KEYWORD AND shared_set.type = NEGATIVE_KEYWORDS",
        "campaign_shared_sets": "SELECT campaign.id, shared_set.id, campaign_shared_set.status FROM campaign_shared_set "
                                "WHERE shared_set.type = NEGATIVE_KEYWORDS",
        "ads": "SELECT ad_group.id, campaign.id, ad_group_ad.status, ad_group_ad.ad_strength, ad_group_ad.policy_summary.approval_status, "
               "ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.ad.final_urls, ad_group_ad.ad.responsive_search_ad.headlines, "
               "ad_group_ad.ad.responsive_search_ad.descriptions, ad_group_ad.ad.responsive_search_ad.path1, "
               "ad_group_ad.ad.responsive_search_ad.path2, ad_group_ad.ad.expanded_dynamic_search_ad.description, "
               "ad_group_ad.ad.expanded_dynamic_search_ad.description2 FROM ad_group_ad",
        "asset_groups": "SELECT asset_group.id, asset_group.name, asset_group.status, asset_group.final_urls, asset_group.ad_strength, "
                        "campaign.id FROM asset_group",
        "asset_group_assets": "SELECT asset_group.id, campaign.id, asset_group_asset.field_type, asset_group_asset.status, "
                              "asset_group_asset.source, asset_group_asset.primary_status, asset_group_asset.policy_summary.approval_status, "
                              "asset.id, asset.type, asset.name, asset.text_asset.text, asset.image_asset.full_size.url, "
                              "asset.youtube_video_asset.youtube_video_id, asset.youtube_video_asset.youtube_video_title "
                              "FROM asset_group_asset",
        "user_lists": "SELECT user_list.id, user_list.name, user_list.type, user_list.size_for_search, user_list.size_for_display, "
                      "user_list.membership_status, user_list.account_user_list_status, user_list.size_range_for_search, "
                      "user_list.size_range_for_display, user_list.membership_life_span, user_list.read_only, user_list.description, "
                      "user_list.rule_based_user_list.prepopulation_status, "
                      "user_list.rule_based_user_list.flexible_rule_user_list.inclusive_rule_operator, "
                      "user_list.rule_based_user_list.flexible_rule_user_list.inclusive_operands, "
                      "user_list.rule_based_user_list.flexible_rule_user_list.exclusive_operands, "
                      "user_list.logical_user_list.rules FROM user_list",  # [audiences] details for the Audience lists view
        "custom_audiences": "SELECT custom_audience.id, custom_audience.name FROM custom_audience",
        "combined_audiences": "SELECT combined_audience.id, combined_audience.name FROM combined_audience",
        "camp_audiences": "SELECT campaign.id, campaign_criterion.criterion_id, campaign_criterion.type, campaign_criterion.status, "
                          "campaign_criterion.negative, campaign_criterion.bid_modifier, campaign_criterion.user_list.user_list, "
                          "campaign_criterion.user_interest.user_interest_category, campaign_criterion.custom_audience.custom_audience, "
                          "campaign_criterion.combined_audience.combined_audience, campaign_criterion.life_event.life_event_id, "
                          "campaign_criterion.custom_affinity.custom_affinity FROM campaign_criterion "
                          f"WHERE campaign_criterion.type IN ({', '.join(AUD_TYPES)})",
        "group_audiences": "SELECT ad_group.id, campaign.id, ad_group_criterion.criterion_id, ad_group_criterion.type, ad_group_criterion.status, "
                           "ad_group_criterion.negative, ad_group_criterion.bid_modifier, ad_group_criterion.user_list.user_list, "
                           "ad_group_criterion.user_interest.user_interest_category, ad_group_criterion.custom_audience.custom_audience, "
                           "ad_group_criterion.combined_audience.combined_audience, ad_group_criterion.life_event.life_event_id, "
                           "ad_group_criterion.custom_affinity.custom_affinity FROM ad_group_criterion "
                           f"WHERE ad_group_criterion.type IN ({', '.join(AUD_TYPES)})",
        "locations": "SELECT campaign.id, campaign_criterion.criterion_id, campaign_criterion.status, campaign_criterion.negative, "
                     "campaign_criterion.bid_modifier, campaign_criterion.location.geo_target_constant FROM campaign_criterion "
                     "WHERE campaign_criterion.type = LOCATION",
        "group_locations": "SELECT ad_group.id, campaign.id, ad_group_criterion.criterion_id, ad_group_criterion.status, ad_group_criterion.negative, "
                           "ad_group_criterion.bid_modifier, ad_group_criterion.location.geo_target_constant FROM ad_group_criterion "
                           "WHERE ad_group_criterion.type = LOCATION",
        "recommendations": "SELECT recommendation.resource_name, recommendation.type, recommendation.campaign, recommendation.ad_group, "
                           "recommendation.dismissed, recommendation.impact FROM recommendation",
        **{f"{lvl}_assets": f"SELECT {sel}{lvl}_asset.field_type, {lvl}_asset.status, asset.id, asset.final_urls, "
                            "asset.sitelink_asset.link_text, asset.sitelink_asset.description1, asset.sitelink_asset.description2, "
                            "asset.sitelink_asset.start_date, asset.sitelink_asset.end_date, asset.callout_asset.callout_text, "
                            "asset.callout_asset.start_date, asset.callout_asset.end_date, asset.structured_snippet_asset.header, "
                            "asset.structured_snippet_asset.values, asset.price_asset.type, asset.price_asset.price_qualifier, "
                            "asset.price_asset.price_offerings, asset.promotion_asset.occasion, asset.promotion_asset.promotion_target, "
                            "asset.promotion_asset.percent_off, asset.promotion_asset.money_amount_off.amount_micros, "
                            "asset.promotion_asset.money_amount_off.currency_code, asset.promotion_asset.start_date, "
                            f"asset.promotion_asset.end_date FROM {lvl}_asset WHERE {lvl}_asset.field_type IN ({', '.join(EXT_FIELDS)})"
           for lvl, sel in (("customer", ""), ("campaign", "campaign.id, "), ("ad_group", "ad_group.id, campaign.id, "))},
    })
    # names that need a second lookup (ids seen above)
    interests = sorted({_tail(r["campaignCriterion"]["userInterest"]["userInterestCategory"]) for r in q["camp_audiences"]
                        if r["campaignCriterion"].get("userInterest")} |
                       {_tail(r["adGroupCriterion"]["userInterest"]["userInterestCategory"]) for r in q["group_audiences"]
                        if r["adGroupCriterion"].get("userInterest")})
    geos = sorted({_tail(r["campaignCriterion"].get("location", {}).get("geoTargetConstant")) for r in q["locations"]} |
                  {_tail(r["adGroupCriterion"].get("location", {}).get("geoTargetConstant")) for r in q["group_locations"]} - {""})
    extra = {}
    for i in range(0, len(geos), 500):
        extra[f"geo{i}"] = ("SELECT geo_target_constant.id, geo_target_constant.canonical_name, geo_target_constant.country_code "
                            f"FROM geo_target_constant WHERE geo_target_constant.id IN ({', '.join(geos[i:i + 500])})")
    if interests:
        extra["interests"] = ("SELECT user_interest.user_interest_id, user_interest.name FROM user_interest "
                              f"WHERE user_interest.user_interest_id IN ({', '.join(interests)})")
    extra["life_events"] = "SELECT life_event.id, life_event.name FROM life_event"
    q2 = _parallel(cid, extra)
    q["_errors"].update(q2.pop("_errors"))
    geo = {g["geoTargetConstant"]["id"]: g["geoTargetConstant"] for k, v in q2.items() if k.startswith("geo") for g in v}
    names = {
        "userInterests": {r["userInterest"]["userInterestId"]: r["userInterest"]["name"] for r in q2.get("interests", [])},
        "lifeEvents": {r["lifeEvent"]["id"]: r["lifeEvent"]["name"] for r in q2.get("life_events", [])},
        "userLists": {r["userList"]["id"]: r["userList"]["name"] for r in q["user_lists"]},
        "customAudiences": {r["customAudience"]["id"]: r["customAudience"]["name"] for r in q["custom_audiences"]},
        "combinedAudiences": {r["combinedAudience"]["id"]: r["combinedAudience"]["name"] for r in q["combined_audiences"]},
    }
    return _rows(q, geo, names)


def _rows(q, geo, names):
    camps = {r["campaign"]["id"]: r for r in q["campaigns"]}
    cname = {k: v["campaign"]["name"] for k, v in camps.items()}
    groups = {r["adGroup"]["id"]: r for r in q["ad_groups"]}
    gname = {k: v["adGroup"]["name"] for k, v in groups.items()}
    camp_status = {k: ST_CODE.get(_st(v["campaign"]["status"]), 0) for k, v in camps.items()}
    group_status = {k: ST_CODE.get(_st(v["adGroup"]["status"]), 0) for k, v in groups.items()}
    rows = {}

    def base(cid=None, gid=None, status=None, rid=None):
        level = "Ad group" if gid else "Campaign" if cid else "Account"
        return {"id": rid, "level": level, "campaign": cname.get(cid, "?") if cid else "", "ad_group": gname.get(gid, "?") if gid else "",
                "campaign_id": int(cid) if cid else None, "ad_group_id": int(gid) if gid else None, "status": status, "editor": ""}

    # keywords + ad group negatives
    kw, negs = [], []
    for r in q["criteria"]:
        c, gid, cid = r["adGroupCriterion"], r["adGroup"]["id"], r["campaign"]["id"]
        k = c.get("keyword", {})
        row = {**base(cid, gid, _st(c.get("status")), f"{gid}~{c['criterionId']}"), "keyword": k.get("text", ""),
               "match": _title(k.get("matchType")), "criterion_id": c["criterionId"]}
        if c.get("negative"):
            negs.append(row)
            continue
        pe = c.get("positionEstimates", {})
        row.update({"max_cpc": _money(c.get("cpcBidMicros")), "quality_score": c.get("qualityInfo", {}).get("qualityScore"),
                    "first_page_cpc": _money(pe.get("firstPageCpcMicros")), "top_of_page_cpc": _money(pe.get("topOfPageCpcMicros")),
                    "final_url": (c.get("finalUrls") or [""])[0], "_m": f"k:{gid}~{c['criterionId']}"})
        kw.append(row)
    rows["keywords"] = kw

    ads = []
    for r in q["ads"]:
        a, gid, cid = r["adGroupAd"], r["adGroup"]["id"], r["campaign"]["id"]
        ad, typ = a["ad"], a["ad"].get("type", "")
        rsa, dsa = ad.get("responsiveSearchAd", {}), ad.get("expandedDynamicSearchAd", {})
        if typ == "RESPONSIVE_SEARCH_AD":
            hl = [{"text": h.get("text", ""), "pin": _pin(h, "HEADLINE")} for h in rsa.get("headlines", [])]
            ds = [{"text": d.get("text", ""), "pin": _pin(d, "DESCRIPTION")} for d in rsa.get("descriptions", [])]
            head = hl[0]["text"] if hl else ""
        else:
            hl, ds = [], [{"text": t, "pin": 0} for t in (dsa.get("description"), dsa.get("description2")) if t]
            head = "(dynamic)" if typ == "EXPANDED_DYNAMIC_SEARCH_AD" else ""
        ads.append({**base(cid, gid, _st(a.get("status")), int(ad["id"])), "ad_id": int(ad["id"]), "remote_id": int(ad["id"]),
                    "ad_type": AD_TYPE.get(typ, _title(typ)), "headline": head, "headlines": hl, "descriptions": ds,
                    "path1": rsa.get("path1", ""), "path2": rsa.get("path2", ""), "final_url": (ad.get("finalUrls") or [""])[0],
                    "ad_strength": _title(a.get("adStrength")) if a.get("adStrength") not in (None, "UNSPECIFIED", "UNKNOWN") else "",
                    "approval": _title(a.get("policySummary", {}).get("approvalStatus")), "_m": f"a:{gid}~{ad['id']}"})
    rows["ads"] = ads

    live_kw = Counter(r["ad_group_id"] for r in kw if r["status"] != "Removed")
    live_ads = Counter(r["ad_group_id"] for r in ads if r["status"] != "Removed")
    rows["ad_groups"] = [{**base(r["campaign"]["id"], gid, _st(r["adGroup"]["status"]), int(gid)), "remote_id": int(gid),
                          "max_cpc": _money(r["adGroup"].get("cpcBidMicros")), "keywords": live_kw.get(int(gid), 0),
                          "ads": live_ads.get(int(gid), 0), "_m": f"g:{gid}"} for gid, r in groups.items()]

    langs = {r["languageConstant"]["id"]: r["languageConstant"]["code"] for r in q["lang_codes"]}
    camp_langs = {}
    for r in q["languages"]:
        lc = _tail(r["campaignCriterion"].get("language", {}).get("languageConstant"))
        camp_langs.setdefault(r["campaign"]["id"], []).append(langs.get(lc, lc))
    ag_n = Counter(r["campaign_id"] for r in rows["ad_groups"] if r["status"] != "Removed")
    kw_n = Counter(r["campaign_id"] for r in kw if r["status"] != "Removed")
    cr = []
    for cid, r in camps.items():
        c, b = r["campaign"], r.get("campaignBudget", {})
        bid = BIDDING.get(c.get("biddingStrategyType"), _title(c.get("biddingStrategyType")))
        tis = c.get("targetImpressionShare", {})
        if c.get("biddingStrategyType") == "TARGET_IMPRESSION_SHARE" and tis.get("locationFractionMicros"):
            bid = f"{bid} {round(int(tis['locationFractionMicros']) / 10000)}%"
        aud = next((t for t in c.get("targetingSetting", {}).get("targetRestrictions", []) if t.get("targetingDimension") == "AUDIENCE"), None)
        cr.append({"id": int(cid), "campaign_id": int(cid), "campaign": c["name"], "status": _st(c["status"]),
                   "type": CHANNEL.get(c.get("advertisingChannelType"), _title(c.get("advertisingChannelType"))),
                   "budget": _money(b.get("amountMicros")), "shared_budget": b.get("name", "") if b.get("explicitlyShared") else "",
                   "bidding": bid, "max_cpc_cap": _money(c.get("targetSpend", {}).get("cpcBidCeilingMicros") or tis.get("cpcBidCeilingMicros")),
                   "target_cpa": _money(c.get("maximizeConversions", {}).get("targetCpaMicros") or c.get("targetCpa", {}).get("targetCpaMicros")),
                   "ad_groups": ag_n.get(int(cid), 0), "keywords": kw_n.get(int(cid), 0),
                   "start": _ymd(c.get("startDateTime")), "end": _ymd(c.get("endDateTime")), "languages": ";".join(sorted(camp_langs.get(cid, []))),
                   "audience_mode": ("observation" if aud.get("bidOnly") else "targeting") if aud else "",
                   "editor": "", "comment": "", "remote_id": int(cid), "_m": f"c:{cid}"})
    rows["campaigns"] = cr

    # negatives: campaign level, ad group level (above), shared lists
    for r in q["camp_negatives"]:
        c, cid = r["campaignCriterion"], r["campaign"]["id"]
        negs.append({**base(cid, None, _st(c.get("status")), f"c{cid}~{c['criterionId']}"), "keyword": c.get("keyword", {}).get("text", ""),
                     "match": _title(c.get("keyword", {}).get("matchType")), "criterion_id": c["criterionId"]})
    sets = {r["sharedSet"]["id"]: r["sharedSet"] for r in q["shared_sets"]}
    for r in q["shared_criteria"]:
        s, c = sets.get(r["sharedSet"]["id"], {}), r["sharedCriterion"]
        negs.append({"id": f"s{r['sharedSet']['id']}~{c['criterionId']}", "level": "Shared list", "campaign": "", "ad_group": "",
                     "campaign_id": None, "ad_group_id": None, "list": s.get("name", ""), "keyword": c.get("keyword", {}).get("text", ""),
                     "match": _title(c.get("keyword", {}).get("matchType")), "status": _st(s.get("status", "ENABLED")), "editor": ""})
    rows["negatives"] = negs
    attached = {}
    for r in q["campaign_shared_sets"]:
        if r["campaignSharedSet"].get("status") != "REMOVED":
            attached.setdefault(r["sharedSet"]["id"], []).append(cname.get(r["campaign"]["id"], "?"))
    rows["shared_lists"] = [{"id": int(sid), "list": s["name"], "keywords": int(s.get("memberCount", 0)), "status": _st(s.get("status")),
                             "campaigns": len(attached.get(sid, [])), "attached_to": ", ".join(sorted(attached.get(sid, []))),
                             "editor": "", "campaign_id": None, "ad_group_id": None, "remote_id": int(sid)} for sid, s in sets.items()]

    # PMax asset groups + their assets
    aga = []
    counts = {}
    for r in q["asset_group_assets"]:
        l, a, agid = r["assetGroupAsset"], r.get("asset", {}), r["assetGroup"]["id"]
        if l.get("status") != "REMOVED" and l.get("source") != "AUTOMATICALLY_CREATED":
            counts.setdefault(agid, Counter())[l["fieldType"]] += 1
        aga.append({**base(r["campaign"]["id"], None, _st(l.get("status")), f"{agid}~{a.get('id')}~{l['fieldType']}"), "level": "Asset group",
                    "asset_group_id": int(agid), "field": _title(l["fieldType"]), "field_type": l["fieldType"],
                    "text": a.get("textAsset", {}).get("text", ""), "asset_id": int(a["id"]) if a.get("id") else None,
                    "asset_type": _title(a.get("type")), "asset_name": a.get("name", ""),
                    "image_url": a.get("imageAsset", {}).get("fullSize", {}).get("url", ""),
                    "video_id": a.get("youtubeVideoAsset", {}).get("youtubeVideoId", ""),
                    "video_title": a.get("youtubeVideoAsset", {}).get("youtubeVideoTitle", ""),
                    "source": _title(l.get("source")), "primary_status": _title(l.get("primaryStatus")),
                    "approval": _title(l.get("policySummary", {}).get("approvalStatus")), "_m": f"aga:{agid}~{a.get('id')}~{l['fieldType']}"})
    agn = {r["assetGroup"]["id"]: r["assetGroup"]["name"] for r in q["asset_groups"]}
    for row in aga:
        row["asset_group"] = agn.get(str(row["asset_group_id"]), "?")
    rows["asset_group_assets"] = aga
    rows["asset_groups"] = [{**base(r["campaign"]["id"], None, _st(r["assetGroup"].get("status")), int(r["assetGroup"]["id"])),
                             "asset_group": r["assetGroup"]["name"], "asset_group_id": int(r["assetGroup"]["id"]),
                             "headlines": counts.get(r["assetGroup"]["id"], {}).get("HEADLINE", 0),
                             "long_headlines": counts.get(r["assetGroup"]["id"], {}).get("LONG_HEADLINE", 0),
                             "descriptions": counts.get(r["assetGroup"]["id"], {}).get("DESCRIPTION", 0),
                             "final_url": (r["assetGroup"].get("finalUrls") or [""])[0],
                             "ad_strength": _title(r["assetGroup"].get("adStrength")) if r["assetGroup"].get("adStrength") not in (None, "UNSPECIFIED") else "",
                             "_m": f"ag:{r['assetGroup']['id']}"} for r in q["asset_groups"]]

    # extensions (assets linked to account / campaign / ad group)
    for k in EXT_KIND.values():
        rows[k] = []
    for lvl, key in (("customer", "customerAsset"), ("campaign", "campaignAsset"), ("ad_group", "adGroupAsset")):
        for r in q[f"{lvl}_assets"]:
            l, a = r[key], r["asset"]
            cid = r.get("campaign", {}).get("id")
            gid = r.get("adGroup", {}).get("id")
            ft = l["fieldType"]
            mk = {"customer": f"xa:{a['id']}~{ft}", "campaign": f"ca:{cid}~{a['id']}~{ft}", "ad_group": f"ga:{gid}~{a['id']}~{ft}"}[lvl]
            row = {**base(cid, gid, _st(l.get("status")), _tail(l["resourceName"])), "asset_id": int(a["id"]), "_m": mk}
            row.update(_ext_fields(ft, a))
            rows[EXT_KIND[ft]].append(row)

    locs = []
    for src, key in (("locations", "campaignCriterion"), ("group_locations", "adGroupCriterion")):
        for r in q[src]:
            c, cid, agid = r[key], r["campaign"]["id"], r.get("adGroup", {}).get("id")
            gid_ = _tail(c.get("location", {}).get("geoTargetConstant"))
            g = geo.get(gid_, {})
            locs.append({**base(cid, agid, _st(c.get("status")), f"{agid or cid}~{c['criterionId']}"),
                         "location": _path(g.get("canonicalName") or gid_), "location_id": int(gid_) if gid_.isdigit() else gid_,
                         "targeting": "Excluded" if c.get("negative") else "Targeted", "country": g.get("countryCode", ""),
                         "bid_modifier": _bm(c.get("bidModifier")), "_m": None if c.get("negative") or agid else f"l:{cid}~{c['criterionId']}"})
    rows["locations"] = locs

    auds = []
    for src, key in (("camp_audiences", "campaignCriterion"), ("group_audiences", "adGroupCriterion")):
        for r in q[src]:
            c, cid, gid = r[key], r["campaign"]["id"], r.get("adGroup", {}).get("id")
            name, ref = _aud_name(c, names)
            auds.append({**base(cid, gid, _st(c.get("status")), f"{gid or cid}~{c['criterionId']}"), "audience": name,
                         "audience_type": _title(c.get("type")), "user_list_id": int(ref) if c.get("userList") and ref.isdigit() else None,
                         "excluded": bool(c.get("negative")), "bid_modifier": _bm(c.get("bidModifier")),
                         "audience_mode": next((x["audience_mode"] for x in cr if x["id"] == int(cid)), ""),
                         "criterion_id": c["criterionId"],
                         "_m": None if c.get("negative") else (f"au:g:{gid}~{c['criterionId']}" if gid else f"au:c:{cid}~{c['criterionId']}")})
    rows["audiences"] = auds

    used = {}
    for a in auds:
        if a["user_list_id"] and a["status"] != "Removed":
            used.setdefault(a["user_list_id"], set()).add(a["campaign"])
    rows["user_lists"] = [{"id": int(u["id"]), "list": u["name"], "audience": u["name"], "type": _title(u.get("type")),
                           "size_search": int(u["sizeForSearch"]) if u.get("sizeForSearch") else None,
                           "size_display": int(u["sizeForDisplay"]) if u.get("sizeForDisplay") else None,
                           "membership": _title(u.get("membershipStatus")), "status": _st(u.get("accountUserListStatus")),
                           "campaigns": len(used.get(int(u["id"]), ())), "attached_to": ", ".join(sorted(used.get(int(u["id"]), ()))),
                           "campaign_id": None, "ad_group_id": None, "editor": "", "remote_id": int(u["id"]),
                           # [audiences] audiences.py: membership days, ranges, rules (read-only extras)
                           "lifespan": u.get("membershipLifeSpan"), "read_only": bool(u.get("readOnly")), "description": u.get("description", ""),
                           "size_range_search": u.get("sizeRangeForSearch"), "size_range_display": u.get("sizeRangeForDisplay"),
                           "_raw": {k: u[k] for k in ("type", "readOnly", "ruleBasedUserList", "logicalUserList") if k in u},
                           "_logical": (u.get("logicalUserList") or {}).get("rules") or []}
                          for u in (r["userList"] for r in q["user_lists"])]

    recs = []
    for r in q["recommendations"]:
        x = r["recommendation"]
        cid, gid = _tail(x.get("campaign")) or None, _tail(x.get("adGroup")) or None
        b, p = x.get("impact", {}).get("baseMetrics", {}), x.get("impact", {}).get("potentialMetrics", {})

        def delta(k, money=False):
            if k not in b and k not in p:
                return None
            d = float(p.get(k, 0) or 0) - float(b.get(k, 0) or 0)
            return round(d / 1e6, 2) if money else round(d, 2)
        recs.append({**base(cid, gid if gid in gname else None, None, _tail(x["resourceName"])), "type": _title(x.get("type")),
                     "recommendation": x.get("type", ""), "dismissed": bool(x.get("dismissed")),
                     "impact_impressions": delta("impressions"), "impact_clicks": delta("clicks"), "impact_cost": delta("costMicros", True),
                     "impact_conversions": delta("conversions"), "impact_conv_value": delta("conversionsValue"),
                     "resource_name": x["resourceName"]})
    rows["recommendations"] = recs

    tree = []
    for cid, r in camps.items():
        tree.append({"id": int(cid), "name": cname[cid], "status": _st(r["campaign"]["status"]), "_code": camp_status[cid],
                     "type": CHANNEL.get(r["campaign"].get("advertisingChannelType"), _title(r["campaign"].get("advertisingChannelType"))),
                     "editor": "", "ad_groups": sorted(({"id": int(gid), "name": gname[gid], "status": _st(g["adGroup"]["status"]), "_code": group_status[gid]}
                                                        for gid, g in groups.items() if g["campaign"]["id"] == cid), key=lambda g: g["name"].lower())})
    tree.sort(key=lambda c: (c["_code"], c["name"].lower()))
    exps = experiments.from_api(q["campaigns"], q.get("experiment_arms", []))
    return {"rows": rows, "tree": tree, "camp_status": {int(k): v for k, v in camp_status.items()},
            "group_status": {int(k): v for k, v in group_status.items()}, "errors": q["_errors"], "experiments": exps}


def _ext_fields(ft, a):
    urls = a.get("finalUrls") or [""]
    if ft == "SITELINK":
        s = a.get("sitelinkAsset", {})
        return {"text": s.get("linkText", ""), "line1": s.get("description1", ""), "line2": s.get("description2", ""),
                "final_url": urls[0], "start": _ymd(s.get("startDate")), "end": _ymd(s.get("endDate"))}
    if ft == "CALLOUT":
        s = a.get("calloutAsset", {})
        return {"text": s.get("calloutText", ""), "start": _ymd(s.get("startDate")), "end": _ymd(s.get("endDate"))}
    if ft == "STRUCTURED_SNIPPET":
        s = a.get("structuredSnippetAsset", {})
        return {"header": s.get("header", ""), "values": ", ".join(s.get("values", []))}
    if ft == "PRICE":
        s = a.get("priceAsset", {})
        items = [f"{o.get('header', '')} {o.get('price', {}).get('currencyCode', '')} {_money(o.get('price', {}).get('amountMicros'))}"
                 for o in s.get("priceOfferings", [])]
        return {"price_type": _words(s.get("type")), "qualifier": _words(s.get("priceQualifier")), "items": "; ".join(items)}
    s = a.get("promotionAsset", {})
    if s.get("percentOff"):
        disc = f"{int(s['percentOff']) / 1e4:g}%"
    else:
        m = s.get("moneyAmountOff", {})
        disc = f"{_money(m.get('amountMicros'))} {m.get('currencyCode', '')}".strip()
    return {"occasion": _words(s.get("occasion")) if s.get("occasion") not in (None, "NONE", "UNSPECIFIED", "UNKNOWN") else "",
            "target": s.get("promotionTarget", ""), "discount": disc, "final_url": urls[0],
            "start": _ymd(s.get("startDate")), "end": _ymd(s.get("endDate"))}


def _aud_name(c, names):
    for field, sub, table in (("userList", "userList", "userLists"), ("customAudience", "customAudience", "customAudiences"),
                              ("combinedAudience", "combinedAudience", "combinedAudiences"),
                              ("userInterest", "userInterestCategory", "userInterests")):
        if c.get(field):
            ref = _tail(c[field].get(sub))
            return names[table].get(ref) or f"{field} {ref}", ref
    if c.get("lifeEvent"):
        ref = str(c["lifeEvent"].get("lifeEventId", ""))
        return names["lifeEvents"].get(ref) or f"lifeEvent {ref}", ref
    if c.get("customAffinity"):
        ref = _tail(c["customAffinity"].get("customAffinity"))
        return f"customAffinity {ref}", ref
    return "?", ""


def _build_stats(cid, cond, key):
    q = _parallel(cid, {
        "c": f"SELECT campaign.id, {M} FROM campaign WHERE {cond}",
        "g": f"SELECT ad_group.id, {M} FROM ad_group WHERE {cond}",
        "k": f"SELECT ad_group.id, ad_group_criterion.criterion_id, {M} FROM keyword_view WHERE {cond}",
        "a": f"SELECT ad_group.id, ad_group_ad.ad.id, {M} FROM ad_group_ad WHERE {cond}",
        "ag": f"SELECT asset_group.id, {M} FROM asset_group WHERE {cond}",
        "aga": f"SELECT asset_group.id, asset.id, asset_group_asset.field_type, {M} FROM asset_group_asset WHERE {cond}",
        "xa": f"SELECT asset.id, customer_asset.field_type, {M} FROM customer_asset WHERE {cond}",
        "ca": f"SELECT campaign.id, asset.id, campaign_asset.field_type, {M} FROM campaign_asset WHERE {cond}",
        "ga": f"SELECT ad_group.id, asset.id, ad_group_asset.field_type, {M} FROM ad_group_asset WHERE {cond}",
        "l": f"SELECT campaign.id, campaign_criterion.criterion_id, {M} FROM location_view WHERE {cond}",
        "au:c": f"SELECT campaign.id, campaign_criterion.criterion_id, {M} FROM campaign_audience_view WHERE {cond}",
        "au:g": f"SELECT ad_group.id, ad_group_criterion.criterion_id, {M} FROM ad_group_audience_view WHERE {cond}",
        # select_cabin per campaign / ad group / keyword / ad / asset group (conversion segments can't sit with cost metrics)
        "sc:c": f"SELECT campaign.id, {SC} FROM campaign WHERE {cond}",
        "sc:g": f"SELECT ad_group.id, {SC} FROM ad_group WHERE {cond}",
        "sc:k": f"SELECT ad_group.id, ad_group_criterion.criterion_id, {SC} FROM keyword_view WHERE {cond}",
        "sc:a": f"SELECT ad_group.id, ad_group_ad.ad.id, {SC} FROM ad_group_ad WHERE {cond}",
        "sc:ag": f"SELECT asset_group.id, {SC} FROM asset_group WHERE {cond}",
    })
    keyf = {
        "c": lambda r: r["campaign"]["id"], "g": lambda r: r["adGroup"]["id"],
        "k": lambda r: f"{r['adGroup']['id']}~{r['adGroupCriterion']['criterionId']}",
        "a": lambda r: f"{r['adGroup']['id']}~{r['adGroupAd']['ad']['id']}", "ag": lambda r: r["assetGroup"]["id"],
        "aga": lambda r: f"{r['assetGroup']['id']}~{r['asset']['id']}~{r['assetGroupAsset']['fieldType']}",
        "xa": lambda r: f"{r['asset']['id']}~{r['customerAsset']['fieldType']}",
        "ca": lambda r: f"{r['campaign']['id']}~{r['asset']['id']}~{r['campaignAsset']['fieldType']}",
        "ga": lambda r: f"{r['adGroup']['id']}~{r['asset']['id']}~{r['adGroupAsset']['fieldType']}",
        "l": lambda r: f"{r['campaign']['id']}~{r['campaignCriterion']['criterionId']}",
        "au:c": lambda r: f"{r['campaign']['id']}~{r['campaignCriterion']['criterionId']}",
        "au:g": lambda r: f"{r['adGroup']['id']}~{r['adGroupCriterion']['criterionId']}",
    }
    out = {}
    for p, rows in q.items():
        if p == "_errors":
            continue
        if p.startswith("sc:"):
            base_p = p[3:]
            for r in rows:
                if SELECT_CABIN not in (r.get("segments", {}).get("conversionActionName") or "").lower():
                    continue
                k = f"{base_p}:{keyf[base_p](r)}"
                cur = out.setdefault(k, [0, 0, 0, 0.0, 0.0])
                while len(cur) < 6:
                    cur.append(0.0)
                cur[5] += float(r.get("metrics", {}).get("allConversions", 0))
            continue
        for r in rows:
            m = r.get("metrics", {})
            k = f"{p}:{keyf[p](r)}"
            cur = out.get(k, [0, 0, 0, 0.0, 0.0])
            out[k] = [cur[0] + int(m.get("costMicros", 0)), cur[1] + int(m.get("impressions", 0)), cur[2] + int(m.get("clicks", 0)),
                      cur[3] + float(m.get("conversions", 0)), cur[4] + float(m.get("conversionsValue", 0))] + list(cur[5:6])
    return {"window": key, "stats": out, "errors": q["_errors"]}


def _metrics(v):
    v = list(v or (0, 0, 0, 0.0, 0.0))
    cost_m, impr, clicks, conv, value = v[:5]
    sc = v[5] if len(v) > 5 else 0.0
    cost = round(cost_m / 1e6, 2)
    return {"cost": cost, "impressions": impr, "clicks": clicks, "ctr": round(clicks / impr * 100, 2) if impr else None,
            "cpc": round(cost / clicks, 2) if clicks else None, "conversions": round(conv, 2), "conv_value": round(value, 2),
            "cost_per_conv": round(cost / conv, 2) if conv else None,
            "select_cabin": round(sc, 1) if sc else None, "cost_per_select_cabin": round(cost / sc, 2) if sc else None}


# ---------- grid / tree (same contract as core.grid / core.tree) ----------

class _Names:
    """What core._overlay needs from core.Ctx: campaign / ad group names by id."""

    def __init__(self, d):
        self.camps = {c["id"]: {"name": c["name"]} for c in d["tree"]}
        self.groups = {g["id"]: {"name": g["name"]} for c in d["tree"] for g in c["ad_groups"]}


def grid(acct, kind, campaign_id=None, ad_group_id=None, status="active", search=None, limit=None,
         days=None, start=None, end=None, wait=False, with_stats=True):
    """Rows of one entity type from the API cache, plus stats for the window (days=7/14/30 or start+end YYYY-MM-DD)."""
    if kind not in KINDS + EXTRA_KINDS:
        raise ValueError(f"unknown kind {kind!r}; one of {', '.join(KINDS + EXTRA_KINDS)}")
    cid = _cid(acct)
    d = base(cid, wait=wait)
    rows = [{k: v for k, v in r.items() if k not in ("_raw", "_logical")} for r in d["rows"].get(kind, []) if core._scope_ok(r, campaign_id, ad_group_id)]
    cs = {int(k): v for k, v in d["camp_status"].items()}
    gs = {int(k): v for k, v in d["group_status"].items()}
    if status != "all":
        rows = [r for r in rows if r.get("status") != "Removed" and cs.get(r.get("campaign_id"), 0) != 2 and gs.get(r.get("ad_group_id"), 0) != 2]
    if status == "enabled":
        rows = [r for r in rows if r.get("status") in ("Enabled", None) and cs.get(r.get("campaign_id"), 0) == 0 and gs.get(r.get("ad_group_id"), 0) == 0]
    if search:
        n = search.lower()
        rows = [r for r in rows if core._matches(r, n)]
    st = stats(cid, days, start, end, wait=wait)["stats"] if with_stats and kind not in NO_STATS else None
    for r in rows:
        mk = r.pop("_m", None)
        if st is not None and kind not in NO_STATS:
            r.update(_metrics(st.get(mk)) if mk else dict.fromkeys(METRIC_KEYS))
    core._overlay(cid, kind, rows, _Names(d), campaign_id, ad_group_id)
    experiments.tag(rows, d.get("experiments") or experiments.empty())
    return rows[:limit] if limit else rows


def tree(acct, status="active", wait=False):
    d = base(_cid(acct), wait=wait)
    out = []
    for c in d["tree"]:
        if status != "all" and c["_code"] == 2:
            continue
        if status == "enabled" and c["_code"] != 0:
            continue
        kids = [{k: g[k] for k in ("id", "name", "status")} for g in c["ad_groups"] if status == "all" or g["_code"] != 2]
        out.append({**{k: c[k] for k in ("id", "name", "status", "type", "editor")}, "ad_groups": kids})
    return experiments.tag(out, d.get("experiments") or experiments.empty(), key="name")


def geo_search(acct, name):
    """Location lookup without Editor (same row shape as core.geo_search)."""
    n = re.sub(r"['\"\\%_]", " ", name or "").strip()
    if len(n) < 2:
        return []
    rows = live.query(_cid(acct), "SELECT geo_target_constant.id, geo_target_constant.canonical_name, geo_target_constant.country_code "
                                  f"FROM geo_target_constant WHERE geo_target_constant.canonical_name LIKE '%{n}%' "
                                  "AND geo_target_constant.status = ENABLED LIMIT 25")
    return [{"id": int(g["id"]), "location": _path(g.get("canonicalName", "")), "country": g.get("countryCode", "")}
            for g in (r["geoTargetConstant"] for r in rows)]


_ACCT_NAMES = {}  # cid -> name from the API (also kept in gapi_accounts.json); dropped by refresh()


def accounts():
    """Google accounts without Editor: the known accounts, named by the API (asked once, then kept until a refresh)."""
    f = core.STUDIO_DIR / "gapi_accounts.json"
    if not _ACCT_NAMES:
        try:
            _ACCT_NAMES.update(json.loads(f.read_text()))
        except (OSError, ValueError):
            pass
    out, new = [], False
    for key, cid in gae.ACCOUNTS.items():
        name = _ACCT_NAMES.get(cid)
        if not name:
            try:
                r = live.query(cid, "SELECT customer.descriptive_name FROM customer LIMIT 1")
                name = (r[0] if r else {}).get("customer", {}).get("descriptiveName", key)
                _ACCT_NAMES[cid], new = name, True
            except Exception:  # noqa: BLE001
                name = key
        out.append({"id": cid, "name": name, "key": key, "last_download": None})
    if new:
        try:
            core.STUDIO_DIR.mkdir(parents=True, exist_ok=True)
            _write(f, _ACCT_NAMES)
        except OSError:
            pass
    return out


# ---------- dispatch: the server calls these instead of core.grid / core.tree ----------

def _editor_ok(acct):
    try:
        core._db(_cid(acct))
        return True
    except (ValueError, SystemExit):
        return False


def use_api(acct):
    return source(acct) == "api" or not _editor_ok(acct)


def grid_any(acct, kind, campaign_id=None, ad_group_id=None, status="active", search=None, limit=None, days=None, start=None, end=None):
    if kind in EXTRA_KINDS or use_api(acct):
        return grid(acct, kind, campaign_id, ad_group_id, status, search, limit, days, start, end)
    return core.grid(_cid(acct), kind, campaign_id, ad_group_id, status, search, limit)


def tree_any(acct, status="active"):
    return tree(acct, status) if use_api(acct) else core.tree(_cid(acct), status)


def geo_any(acct, name):
    return geo_search(acct, name) if use_api(acct) else core.geo_search(_cid(acct), name)


def accounts_any():
    try:
        return core.accounts()
    except (SystemExit, Exception):  # noqa: BLE001 - no Editor on this machine
        return accounts()
