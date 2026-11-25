"""Ads Studio <-> Meta (Facebook + Instagram) Marketing API. The Meta side of Studio: no Editor in between,
so Studio reads Meta straight from the API (cached), keeps its own draft, previews with Meta's validate_only
and pushes live only when the user presses Push live and confirms.

Safety built in:
- new campaigns, ad sets and ads are always created PAUSED; nothing that existed before a push is ever deleted or
  archived. Only objects Studio created in a push are set to DELETED, and only when that push is rolled back or undone
- anything that can raise spend (activate, budget up, wider targeting) is flagged and needs an extra tick
- live values are read right before the push and stored, so every push can be undone
- Meta has no all-or-nothing batch: changes go one at a time and a failure rolls back the ones already done
- ad copy can't be edited in place on Meta: a new creative is made and the ad is pointed at it; undo points
  it back. The ad goes through Meta review again.

Token: META_ACCESS_TOKEN (system user, ads_management) and META_AD_ACCOUNT_ID from Setup (appconfig: Keychain / config.json).
"""
import base64, collections, datetime as dt, hashlib, html, json, re, sys, threading, time, urllib.error, urllib.parse, urllib.request, uuid
from pathlib import Path

import core
import datapolicy
import meta_audiences  # [audiences] Audiences view + meta_audience change type
import progress

import appconfig  # noqa: E402  (keys + vendor_hub on sys.path)
import meta_api  # noqa: E402  (shared Meta connection: one GRAPH_VERSION for Studio, dashboard and scripts)

API = meta_api.API
KEY = "meta"
CACHE_MAX_AGE = 15 * 60
STATUS = {"active": "ACTIVE", "paused": "PAUSED"}
CTAS = ["LEARN_MORE", "BOOK_TRAVEL", "SHOP_NOW", "SIGN_UP", "CONTACT_US", "GET_OFFER", "SEE_MORE", "DOWNLOAD", "APPLY_NOW", "SUBSCRIBE"]
_cache_lock = threading.Lock()


TOKEN = meta_api.TOKEN
DEFAULT_ACT = appconfig.get("META_AD_ACCOUNT_ID")
PAGE_ID = appconfig.get("META_PAGE_ID")


def _refresh():
    global TOKEN, DEFAULT_ACT, PAGE_ID
    meta_api._refresh()
    TOKEN, DEFAULT_ACT, PAGE_ID = meta_api.TOKEN, appconfig.get("META_AD_ACCOUNT_ID"), appconfig.get("META_PAGE_ID")


appconfig.on_change(_refresh)
SETTINGS = core.STUDIO_DIR / "settings.json"
_cur = threading.local()  # the Meta ad account this request/thread works on (set by use())


def settings():
    try:
        s = json.loads(SETTINGS.read_text())
    except (OSError, ValueError):
        s = {}
    s.setdefault("meta_accounts", [{"act": DEFAULT_ACT, "name": "Meta (Facebook + Instagram)"}] if DEFAULT_ACT else [])
    s.setdefault("views", [])
    return s


def save_settings(s):
    core.STUDIO_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=1, ensure_ascii=False))
    tmp.replace(SETTINGS)


def key_for(act_id):
    """UI/MCP key of an ad account. The .env account keeps the old key "meta" so drafts and logs carry over."""
    return KEY if act_id == DEFAULT_ACT else f"meta_{act_id.replace('act_', '')}"


def use(key):
    """Point this thread at the ad account behind a UI key ("meta" or "meta_<id>")."""
    key = key or KEY
    for a in settings()["meta_accounts"]:
        if key_for(a["act"]) == key:
            _cur.act = a["act"]
            return a["act"]
    raise ValueError(f"unknown Meta ad account {key!r}: add it in Studio Settings")


def act():
    return getattr(_cur, "act", None) or DEFAULT_ACT


def acct():
    """Draft / log file key of the current ad account."""
    return f"meta_{act().replace('act_', '')}"


def is_meta(acct_key):
    return str(acct_key or "").startswith("meta")


def accounts():
    if not TOKEN:
        return []
    out = []
    for a in settings()["meta_accounts"]:
        c = core.STUDIO_DIR / f"meta_cache_meta_{a['act'].replace('act_', '')}.json"
        try:
            fetched = json.loads(c.read_text()).get("fetched_at") if c.exists() else None
        except ValueError:
            fetched = None
        out.append({"id": f"meta_{a['act'].replace('act_', '')}", "key": key_for(a["act"]), "act": a["act"], "name": a["name"],
                    "channel": "meta", "last_download": fetched})
    return out


# ---------- API ----------
# The connection itself (version, token, retries, usage headers) lives in shared/dashboard/connectors/meta_api.py.
# These names stay here because other Studio files use meta_ads.get/post/get_all/MetaError.

MetaError = meta_api.MetaError


def get(path, **params):
    return meta_api.get(path, **params)


def post(path, params, validate=False, retry_rate=True):
    """POST with one patient retry when Meta rate-limits (a refused call changed nothing)."""
    return meta_api.post(path, params, validate=validate, retry_rate=retry_rate)


def get_all(path, **params):
    return meta_api.get_all(path, **params)


def delete(path):
    """DELETE one object (used only for audiences: undo of one Studio made, or a delete the user pushed)."""
    return meta_api.call("DELETE", path)


# ---------- reading (cached) ----------

F_CAMP = "id,name,status,effective_status,objective,daily_budget,lifetime_budget,bid_strategy,start_time,stop_time,updated_time"
F_ADSET = ("id,name,campaign_id,status,effective_status,daily_budget,lifetime_budget,optimization_goal,bid_strategy,bid_amount,"
           "start_time,end_time,targeting,updated_time")
F_AD = ("id,name,adset_id,campaign_id,status,effective_status,updated_time,"
        "creative{id,name,object_type,object_story_spec,asset_feed_spec,object_story_id,thumbnail_url,image_url,url_tags,degrees_of_freedom_spec,"
        "body,title,link_url,call_to_action_type,effective_object_story_id}")
# effective_status filters for the list reads: everything except DELETED and ARCHIVED (Meta's own values per object),
# so a long-running account's archive isn't paged through on every Refresh.
LIVE_CAMP = ["ACTIVE", "PAUSED", "IN_PROCESS", "WITH_ISSUES"]
LIVE_ADSET = LIVE_CAMP + ["CAMPAIGN_PAUSED"]
LIVE_AD = LIVE_ADSET + ["ADSET_PAUSED", "PENDING_REVIEW", "DISAPPROVED", "PREAPPROVED", "PENDING_BILLING_INFO"]
F_AUD = ("id,name,subtype,approximate_count_lower_bound,approximate_count_upper_bound,time_updated,operation_status,"
         "description,retention_days,lookalike_spec,data_source")  # [audiences] rule is read per audience (too big for the list)
F_INS = "spend,impressions,reach,frequency,clicks,ctr,cpc,cpm,actions,conversions"
# Custom pixel events Meta reports under "conversions" (offsite_conversion.fb_pixel_custom.<Event>): our column key -> event name.
# SelectCabin is the website's cabin-selection event (one of ML's main conversions; it is a pixel event, not a saved custom conversion).
CUSTOM_EVENTS = {"select_cabin": "SelectCabin"}
RESULT_ACTIONS = ("offsite_conversion.fb_pixel_purchase", "offsite_conversion.fb_pixel_custom", "lead", "onsite_conversion.lead_grouped")


def _cache_file():
    core.STUDIO_DIR.mkdir(parents=True, exist_ok=True)
    return core.STUDIO_DIR / f"meta_cache_{acct()}.json"


def refresh():
    """Pull the whole account (5-8 paged calls) and cache it."""
    data = {"account": get(act(), fields="name,currency,timezone_name,account_status"),
            "campaigns": get_all(f"{act()}/campaigns", fields=F_CAMP, effective_status=LIVE_CAMP),
            "adsets": get_all(f"{act()}/adsets", fields=F_ADSET, effective_status=LIVE_ADSET),
            "ads": get_all(f"{act()}/ads", fields=F_AD, effective_status=LIVE_AD, limit=100),  # 500 with full creatives (carousels) is refused as too much data
            "audiences": get_all(f"{act()}/customaudiences", fields=F_AUD, limit=100),  # [audiences] Meta refuses big pages with these fields
            "insights": {}}
    for level in ("campaign", "adset", "ad"):
        rows = get_all(f"{act()}/insights", level=level, date_preset="last_7d", fields=f"{level}_id," + F_INS)
        data["insights"][level] = {r[f"{level}_id"]: r for r in rows}
    data["fetched_at"] = dt.datetime.now().isoformat(timespec="minutes")
    data["_at"] = time.time()
    f = _cache_file()
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    tmp.replace(f)
    datapolicy.mark(_src())
    return data


def _src():
    """datapolicy source key of the current ad account."""
    return f"meta:{acct()}"


def refresh_account():
    """The Refresh menu, and after Push live / Undo: pull this ad account again and drop its insights and lead form
    copies made before now (each date range is fetched again, once, when next viewed). Read-only."""
    t0 = time.time()
    _bg.add(act())
    try:
        with _cache_lock:
            data = refresh()
    finally:
        _bg.discard(act())
    for k in [k for k in _ins_mem if k[0] == acct()]:
        _ins_mem.pop(k, None)
    _lf_mem.clear()
    datapolicy.invalidate(_src(), at=t0)
    return data


def _filled(d):
    try:
        return d.get("_at") or dt.datetime.fromisoformat(d["fetched_at"]).timestamp()
    except (KeyError, TypeError, ValueError):
        return 0


_bg = set()  # ad accounts with a background refresh running


def _bg_refresh(act_id):
    _cur.act = act_id
    try:
        with _cache_lock:
            refresh()
    except Exception:  # noqa: BLE001 - the next load tries again
        pass
    finally:
        _bg.discard(act_id)


def _cached_copy(max_age):
    """(data, usable) from the cache file; (None, False) when there is none."""
    f = _cache_file()
    if not f.exists():
        return None, False
    try:
        d = json.loads(f.read_text())
    except ValueError:
        return None, False
    age = (dt.datetime.now() - dt.datetime.fromisoformat(d["fetched_at"])).total_seconds()
    return d, max_age is None or datapolicy.fresh(_src(), age, max_age, _filled(d))


def load(max_age=CACHE_MAX_AGE, wait=False):
    """Cached account data. datapolicy decides when it is stale: in manual mode (the default) never, until the user
    refreshes. In auto mode a stale cache is served at once and refreshed in the background (a full pull takes ~20s).
    max_age=0 always pulls now."""
    d, ok = _cached_copy(max_age)
    if ok:
        return d
    if d is not None and not wait and datapolicy.background_ok() and _filled(d) >= datapolicy.refreshed_at(_src()):
        if act() not in _bg:
            _bg.add(act())
            threading.Thread(target=_bg_refresh, args=(act(),), daemon=True).start()
        return d
    with _cache_lock:
        if max_age != 0:  # another request may have pulled it while this one waited for the lock
            d, ok = _cached_copy(max_age)
            if ok:
                return d
        return refresh()


def refreshing():
    return act() in _bg


def _cur_factor(d):
    # budgets are in the currency's minor unit; every currency we use has 2 decimals
    return 100


def _money(v, d):
    return round(int(v) / _cur_factor(d), 2) if v not in (None, "", "0") else None


EXTRA = ("select_cabin", "cost_per_select_cabin", "purchases", "cost_per_purchase", "leads", "cost_per_lead")
INS_KEYS = ("spend_7d", "impr_7d", "reach_7d", "frequency_7d", "clicks_7d", "ctr_7d", "cpc_7d", "cpm_7d", "results_7d", "cost_per_result_7d") + EXTRA


INS_GENERIC = ("spend", "impressions", "reach", "frequency", "clicks", "ctr", "cpc", "cpm", "results", "cost_per_result") + EXTRA


def _ins_vals(r):
    """One insights row -> {spend, impressions, reach, frequency, clicks, ctr, cpc, cpm, results, cost_per_result}."""
    if not r:
        return dict.fromkeys(INS_GENERIC)
    results = sum(float(a["value"]) for a in r.get("actions", []) if a["action_type"] in RESULT_ACTIONS)
    spend = round(float(r.get("spend", 0)), 2)
    f = lambda k, n=2: round(float(r[k]), n) if r.get(k) not in (None, "") else None  # noqa: E731
    return {"spend": spend, "impressions": int(r.get("impressions", 0)), "reach": int(r["reach"]) if r.get("reach") else None,
            "frequency": f("frequency"), "clicks": int(r.get("clicks", 0)), "ctr": f("ctr"), "cpc": f("cpc"), "cpm": f("cpm"),
            "results": round(results) if results else None, "cost_per_result": round(spend / results, 2) if results else None,
            **_conv_vals(r, spend)}


def _conv_vals(r, spend):
    """Select Cabin (custom pixel event), purchases and leads, each with a cost per."""
    conv = {a["action_type"]: float(a["value"]) for a in r.get("conversions") or []}
    act_ = {a["action_type"]: float(a["value"]) for a in r.get("actions") or []}
    n = {k: conv.get(f"offsite_conversion.fb_pixel_custom.{ev}") for k, ev in CUSTOM_EVENTS.items()}
    n["purchases"] = act_.get("offsite_conversion.fb_pixel_purchase")
    n["leads"] = act_.get("lead")
    out = {}
    for k, v in n.items():
        out[k] = round(v) if v else None
        out["cost_per_" + k[:-1] if k in ("purchases", "leads") else "cost_per_" + k] = round(spend / v, 2) if v else None
    return out


def _ins(d, level, oid):
    v = _ins_vals(d["insights"].get(level, {}).get(oid))
    return dict(zip(INS_KEYS, (v[k] for k in INS_GENERIC)))


WINDOWS = {7: "last_7d", 14: "last_14d", 30: "last_30d", 90: "last_90d"}
_ins_mem = {}  # (acct, level, window key) -> (fetched epoch, data); also kept on disk


def _window(window=None, since=None, until=None):
    """-> (cache key, Meta params, label). window: 7, 14, 30, 90 (or "7d" etc.), or since/until YYYY-MM-DD."""
    if since or until:
        try:
            a, b = dt.date.fromisoformat(str(since)), dt.date.fromisoformat(str(until or dt.date.today()))
        except ValueError:
            raise ValueError("since and until must be YYYY-MM-DD") from None
        if a > b:
            raise ValueError("since must be on or before until")
        if b > dt.date.today():
            raise ValueError("until can't be in the future")
        if a < dt.date.today() - dt.timedelta(days=37 * 30):
            raise ValueError("Meta keeps insights for about 37 months")
        return f"{a}_{b}", {"time_range": {"since": str(a), "until": str(b)}}, f"{a:%d %b %Y} to {b:%d %b %Y}"
    w = int(str(window or 7).lower().rstrip("d"))
    if w not in WINDOWS:
        raise ValueError(f"window must be one of {', '.join(map(str, WINDOWS))} days, or since/until")
    return f"{w}d", {"date_preset": WINDOWS[w]}, f"last {w} days"


def insights_cached(level, window=7, since=None, until=None):
    """Is this window's insights already cached (so showing it pulls nothing)?"""
    key, _, _ = _window(window, since, until)
    hit = _ins_mem.get((acct(), level, key))
    if hit and datapolicy.fresh(_src(), time.time() - hit[0], CACHE_MAX_AGE, hit[0]):
        return True
    try:
        disk = json.loads((core.STUDIO_DIR / f"meta_ins_{acct()}_{level}_{key}.json").read_text())
        return datapolicy.fresh(_src(), time.time() - disk["at"], CACHE_MAX_AGE, disk["at"])
    except (OSError, ValueError, KeyError):
        return False


def insights(level, window=7, since=None, until=None):
    """{id: {spend, impressions, reach, frequency, clicks, ctr, cpc, cpm, results, cost_per_result}} for campaigns, ad sets
    or ads over a window. Cached per account + window (datapolicy: until a refresh in manual mode, 15 minutes in auto). Read-only."""
    if level not in LEVELS:
        raise ValueError("level must be campaign, adset or ad")
    key, params, _ = _window(window, since, until)
    mk = (acct(), level, key)
    hit = _ins_mem.get(mk)
    if hit and datapolicy.fresh(_src(), time.time() - hit[0], CACHE_MAX_AGE, hit[0]):
        return hit[1]
    f = core.STUDIO_DIR / f"meta_ins_{acct()}_{level}_{key}.json"
    try:
        disk = json.loads(f.read_text())
        if datapolicy.fresh(_src(), time.time() - disk["at"], CACHE_MAX_AGE, disk["at"]):
            _ins_mem[mk] = (disk["at"], disk["data"])
            return disk["data"]
    except (OSError, ValueError, KeyError):
        pass
    got = get_all(f"{act()}/insights", level=level, fields=f"{level}_id," + F_INS, **params)
    data = {r[f"{level}_id"]: _ins_vals(r) for r in got}
    now = time.time()
    _ins_mem[mk] = (now, data)
    core.STUDIO_DIR.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps({"at": now, "data": data}))
    tmp.replace(f)
    return data


def _st(s):
    return (s or "").replace("_", " ").title()


def creative_text(cr):
    """Primary text, headline, description, link, CTA and format of a creative, plus whether Studio can edit it."""
    cr = cr or {}
    oss, feed = cr.get("object_story_spec") or {}, cr.get("asset_feed_spec")
    ld, vd = oss.get("link_data") or {}, oss.get("video_data") or {}
    cta = ld.get("call_to_action") or vd.get("call_to_action") or {}
    out = {"primary_text": ld.get("message") or vd.get("message") or "", "headline": ld.get("name") or vd.get("title") or "",
           "description": ld.get("description") or vd.get("link_description") or "",
           "link": ld.get("link") or (cta.get("value") or {}).get("link") or "", "cta": cta.get("type") or "",
           "format": "Video" if vd else "Carousel" if ld.get("child_attachments") else "Image" if ld else (cr.get("object_type") or "").title(),
           "editable": bool(ld or vd) and not feed and not cr.get("object_story_id") and not ld.get("child_attachments"),
           "why_not": ""}
    for k, src in (("primary_text", "body"), ("headline", "title"), ("link", "link_url"), ("cta", "call_to_action_type")):
        out[k] = out[k] or cr.get(src) or ""
    # edit_type: the draft change type that edits this ad's copy ("" = none). "editable" stays the meta_copy flag.
    out["edit_type"] = "meta_copy" if out["editable"] else ""
    if feed:
        out["why_not"] = "Flexible / Advantage+ creative with several texts: edit the text lists (meta_feed_copy)"
        out["primary_text"] = out["primary_text"] or ((feed.get("bodies") or [{}])[0].get("text") or "")
        out["headline"] = out["headline"] or ((feed.get("titles") or [{}])[0].get("text") or "")
        out["link"] = out["link"] or ((feed.get("link_urls") or [{}])[0].get("website_url") or "")
        out.update(_feed_lists(feed))
        if cr.get("object_story_id") or not oss:
            out["why_not"] = "Flexible / Advantage+ creative made from a page post: edit in Ads Manager"
        elif feed.get("asset_customization_rules"):
            out["why_not"] = "Advantage+ creative with per-placement rules: edit in Ads Manager"
        else:
            out["edit_type"] = "meta_feed_copy"
    elif cr.get("object_story_id"):
        out["why_not"] = "Uses an existing page post: edit the post itself"
    elif ld.get("child_attachments"):
        out["cards"] = _cards(ld)
        out["why_not"] = f"Carousel ({len(ld['child_attachments'])} cards): edit the cards (meta_carousel_copy)"
        out["edit_type"] = "meta_carousel_copy"
    elif not (ld or vd):
        out["why_not"] = "Made from an existing Facebook or Instagram post: edit the post itself" if cr.get("effective_object_story_id") \
            else "This creative type can't be edited from Studio"
    return out


def _cards(ld):
    return [{"index": i, "link": ch.get("link", ""), "name": ch.get("name", ""), "description": ch.get("description", ""),
             "picture": ch.get("picture", ""), "image_hash": ch.get("image_hash", ""), "video_id": ch.get("video_id", ""),
             "cta": (ch.get("call_to_action") or {}).get("type", "")}
            for i, ch in enumerate(ld.get("child_attachments") or [])]


def _feed_lists(feed):
    return {"bodies": [b.get("text", "") for b in feed.get("bodies") or []],
            "titles": [b.get("text", "") for b in feed.get("titles") or []],
            "descriptions": [b.get("text", "") for b in feed.get("descriptions") or []],
            "link_urls": [b.get("website_url", "") for b in feed.get("link_urls") or []]}


PLACEMENT_KEYS = ("publisher_platforms", "facebook_positions", "instagram_positions", "audience_network_positions",
                  "messenger_positions", "threads_positions")
PLATFORMS = ("facebook", "instagram", "audience_network", "messenger", "threads")
FB_POS = ("feed", "right_hand_column", "marketplace", "video_feeds", "story", "search", "instream_video", "facebook_reels",
          "facebook_reels_overlay", "profile_feed", "notification", "biz_disco_feed")
IG_POS = ("stream", "story", "explore", "explore_home", "reels", "profile_feed", "ig_search", "profile_reels")


def _placements(t):
    t = t or {}
    if not t.get("publisher_platforms"):
        return "Advantage+ (automatic)"
    bits = []
    for p in t["publisher_platforms"]:
        pos = t.get(f"{p}_positions")
        bits.append(f"{p}: {', '.join(pos)}" if pos else f"{p}: all")
    return "; ".join(bits)


def _targeting(t):
    t = t or {}
    geo = t.get("geo_locations") or {}
    places = (geo.get("countries") or []) + [r.get("name", "") for r in geo.get("regions", [])] + [c.get("name", "") for c in geo.get("cities", [])]
    adv = bool((t.get("targeting_automation") or {}).get("advantage_audience"))
    age = (f"{t['age_range'][0]}-{t['age_range'][1]} (suggestion)" if adv and isinstance(t.get("age_range"), list) and len(t["age_range"]) == 2
           else f"{t.get('age_min', '')}-{t.get('age_max', '')}")
    return {"countries": ", ".join(places), "age": age,
            "audiences": ", ".join(a.get("name", a.get("id", "")) for a in t.get("custom_audiences", [])),
            "excluded": ", ".join(a.get("name", a.get("id", "")) for a in t.get("excluded_custom_audiences", [])),
            "advantage_audience": bool((t.get("targeting_automation") or {}).get("advantage_audience")),
            "placements": _placements(t), "advantage_placements": not t.get("publisher_platforms")}


def rows(d, kind):
    camps = {c["id"]: c for c in d["campaigns"]}
    sets = {s["id"]: s for s in d["adsets"]}
    if kind == "campaigns":
        n_sets, n_ads = {}, {}
        for s in d["adsets"]:
            n_sets[s["campaign_id"]] = n_sets.get(s["campaign_id"], 0) + 1
        for a in d["ads"]:
            n_ads[a["campaign_id"]] = n_ads.get(a["campaign_id"], 0) + 1
        return [{"id": c["id"], "campaign_id": c["id"], "ad_group_id": None, "campaign": c["name"], "status": _st(c["status"]),
                 "delivery": _st(c["effective_status"]), "objective": _st(c.get("objective", "").replace("OUTCOME_", "")),
                 "budget": _money(c.get("daily_budget"), d), "lifetime_budget": _money(c.get("lifetime_budget"), d),
                 "budget_level": "Campaign (CBO)" if c.get("daily_budget") or c.get("lifetime_budget") else "Ad sets",
                 "bidding": _st(c.get("bid_strategy", "")), "ad_sets": n_sets.get(c["id"], 0), "ads": n_ads.get(c["id"], 0),
                 "updated": (c.get("updated_time") or "")[:10], **_ins(d, "campaign", c["id"])} for c in d["campaigns"]]
    if kind == "adsets":
        out = []
        for s in d["adsets"]:
            c = camps.get(s["campaign_id"], {})
            cbo = bool(c.get("daily_budget") or c.get("lifetime_budget"))
            out.append({"id": s["id"], "campaign_id": s["campaign_id"], "ad_group_id": s["id"], "campaign": c.get("name", "?"),
                        "ad_set": s["name"], "status": _st(s["status"]), "delivery": _st(s["effective_status"]),
                        "budget": None if cbo else _money(s.get("daily_budget"), d), "lifetime_budget": _money(s.get("lifetime_budget"), d),
                        "cbo": cbo, "optimisation": _st(s.get("optimization_goal", "")), "end": (s.get("end_time") or "")[:10],
                        **_targeting(s.get("targeting")), "updated": (s.get("updated_time") or "")[:10], **_ins(d, "adset", s["id"])})
        return out
    if kind == "ads":
        out = []
        for a in d["ads"]:
            s = sets.get(a["adset_id"], {})
            ct = creative_text(a.get("creative"))
            out.append({"id": a["id"], "campaign_id": a["campaign_id"], "ad_group_id": a["adset_id"],
                        "campaign": camps.get(a["campaign_id"], {}).get("name", "?"), "ad_set": s.get("name", "?"), "ad": a["name"],
                        "status": _st(a["status"]), "delivery": _st(a["effective_status"]), **ct,
                        "creative_id": (a.get("creative") or {}).get("id"), "thumbnail": (a.get("creative") or {}).get("thumbnail_url", ""),
                        "image": (a.get("creative") or {}).get("image_url") or (a.get("creative") or {}).get("thumbnail_url", ""),
                        "updated": (a.get("updated_time") or "")[:10], **_ins(d, "ad", a["id"])})
        return out
    if kind == "audiences":  # [audiences] honest sizes, retention, where used (meta_audiences.py)
        used = meta_audiences.users(d)
        return [{"id": a["id"], "campaign_id": None, "ad_group_id": None, "audience": a["name"],
                 "type": meta_audiences.SUBTYPE.get(a.get("subtype", ""), _st(a.get("subtype", ""))), "size": meta_audiences.size_text(a),
                 "retention": f"{a['retention_days']} days" if a.get("retention_days") else "",
                 "used_by": "; ".join(used.get(str(a["id"]), [])),
                 "status": (a.get("operation_status") or {}).get("description", ""),
                 "updated": dt.datetime.fromtimestamp(a["time_updated"]).strftime("%Y-%m-%d") if a.get("time_updated") else "",
                 "note": meta_audiences.NO_CUSTOMER_DATA if a.get("subtype") == "CUSTOM" else GDPR_NOTE}
                for a in d["audiences"]]
    if kind == "leadforms":
        return [{"id": f["id"], "campaign_id": None, "ad_group_id": None, "lead_form": f.get("name", ""), "status": _st(f.get("status", "")),
                 "leads": f.get("leads_count"), "created": (f.get("created_time") or "")[:10], "note": GDPR_NOTE}
                for f in lead_forms()]
    raise ValueError(f"unknown Meta kind {kind!r}; one of {', '.join(GRIDS)}")


GRIDS = ("campaigns", "adsets", "ads", "audiences", "leadforms")
GONE = ("Archived", "Deleted")
GDPR_NOTE = "Personal data: UK GDPR check needed before any upload/download. Studio reads names and counts only."
_lf_mem = {}


def _page_for_account(d=None):
    """The Facebook Page behind the current ad account: META_PAGE_ID for the .env account, else the page most ads use."""
    if act() == DEFAULT_ACT and PAGE_ID:
        return PAGE_ID
    pages = collections.Counter(((a.get("creative") or {}).get("object_story_spec") or {}).get("page_id")
                                for a in (d or load())["ads"])
    pages.pop(None, None)
    return pages.most_common(1)[0][0] if pages else PAGE_ID


def lead_forms():
    """Lead forms on the account's Page: id, name, status, leads count, created. READ ONLY and never the leads
    themselves (personal data). Needs a Page token, fetched in memory from the system user token and never stored."""
    page = _page_for_account()
    if not page:
        return []
    hit = _lf_mem.get(page)
    if hit and datapolicy.fresh(_src(), time.time() - hit[0], CACHE_MAX_AGE, hit[0]):
        return hit[1]
    ptoken = get(page, fields="access_token").get("access_token")
    if not ptoken:
        raise ValueError("the token can't act for the Page, so lead forms can't be listed")
    out, page_data = [], meta_api.call("GET", f"{page}/leadgen_forms", {"fields": "id,name,status,leads_count,created_time", "limit": 200},
                                       token=ptoken)
    while True:
        out += page_data.get("data", [])
        nxt = (page_data.get("paging") or {}).get("next")
        if not nxt:
            break
        page_data = meta_api.call("GET", nxt, token=ptoken)
    _lf_mem[page] = (time.time(), out)
    return out


def grid(kind, campaign_id=None, adset_id=None, status="active", search=None, limit=None, window=None, since=None, until=None):
    """Rows of one kind. window (7, 14, 30, 90) or since/until (YYYY-MM-DD) overlays that period's stats onto the generic keys
    spend, impressions, reach, frequency, clicks, ctr, cpc, cpm, results, cost_per_result (the *_7d keys stay as they are)."""
    d = load()
    out = rows(d, kind)
    if (window or since or until) and kind in ("campaigns", "adsets", "ads"):
        _, _, label = _window(window, since, until)
        ins = insights({"campaigns": "campaign", "adsets": "adset", "ads": "ad"}[kind], window, since, until)
        for r in out:
            r.update(ins.get(r["id"]) or dict.fromkeys(INS_GENERIC))
            r["window"] = label
    if adset_id:
        out = [r for r in out if r.get("ad_group_id") == str(adset_id)]
    elif campaign_id:
        out = [r for r in out if r.get("campaign_id") == str(campaign_id)]
    if status != "all":
        out = [r for r in out if r.get("status") not in GONE]
    if status == "enabled":
        out = [r for r in out if r.get("delivery") in ("Active", "") or kind == "audiences"]
    if search:
        n = search.lower()
        out = [r for r in out if any(n in str(v).lower() for k, v in r.items() if isinstance(v, str) and not k.endswith("_id"))]
    _overlay(kind, out, campaign_id, adset_id)
    return out[:limit] if limit else out


def tree(status="active"):
    d = load()
    sets = {}
    for s in d["adsets"]:
        sets.setdefault(s["campaign_id"], []).append(s)
    order = {"ACTIVE": 0, "PAUSED": 1}
    out = []
    for c in sorted(d["campaigns"], key=lambda c: (order.get(c["status"], 2), c["name"].lower())):
        if status != "all" and _st(c["status"]) in GONE:
            continue
        if status == "enabled" and c["effective_status"] != "ACTIVE":
            continue
        kids = [{"id": s["id"], "name": s["name"], "status": "Enabled" if s["effective_status"] == "ACTIVE" else "Paused"}
                for s in sorted(sets.get(c["id"], []), key=lambda s: s["name"].lower()) if status == "all" or _st(s["status"]) not in GONE]
        out.append({"id": c["id"], "name": c["name"], "status": "Enabled" if c["effective_status"] == "ACTIVE" else "Paused",
                    "type": _st(c.get("objective", "").replace("OUTCOME_", "")), "editor": "", "ad_groups": kids})
    if status != "enabled":  # new campaigns / ad sets in the draft (always Paused), id "d-<draft item id>"
        by_id = {c["id"]: c for c in out}
        news = []
        for it in core.draft(acct())["items"]:
            c = it["change"]
            if c.get("type") == "meta_new_campaign":
                n = {"id": f"d-{it['id']}", "name": c.get("name", ""), "status": "Paused", "draft": True,
                     "type": _st(str(c.get("objective", "")).replace("OUTCOME_", "")), "editor": "", "ad_groups": []}
                by_id[n["id"]] = n
                news.append(n)
        for it in core.draft(acct())["items"]:
            c = it["change"]
            if c.get("type") == "meta_new_adset" and str(c.get("campaign_id")) in by_id:
                by_id[str(c["campaign_id"])]["ad_groups"].append({"id": f"d-{it['id']}", "name": c.get("name", ""), "status": "Paused", "draft": True})
        out = news + out
    return out


def state():
    d = core.draft(acct())
    f = _cache_file()
    fetched = json.loads(f.read_text()).get("fetched_at") if f.exists() else None
    return {"draft": d, "editor_pending": {}, "last_download": fetched, "editor_running": False, "channel": "meta",
            "pushing": False, "refreshing": refreshing(), "last_live_push": last_push()}


# ---------- draft: Meta change types ----------

LEVELS = ("campaign", "adset", "ad")


def _find(d, level, oid):
    coll = {"campaign": "campaigns", "adset": "adsets", "ad": "ads"}[level]
    o = next((x for x in d[coll] if x["id"] == str(oid)), None)
    if not o:
        raise ValueError(f"{level} {oid} not found in Meta (refresh if it is new)")
    return o


def _name(d, level, oid):
    try:
        return _find(d, level, oid)["name"]
    except ValueError:
        return str(oid)


TYPES = ("meta_status", "meta_budget", "meta_end", "meta_rename", "meta_targeting", "meta_placements", "meta_copy",
         "meta_carousel_copy", "meta_feed_copy", "meta_new_campaign", "meta_new_adset", "meta_new_ad", "meta_boost_post",
         "meta_test_split", "meta_test_creative", "meta_test_end",  # tests: meta_tests.py
         "meta_audience")  # [audiences] meta_audiences.py
TEST_TYPES = ("meta_test_split", "meta_test_creative", "meta_test_end")
NEW_TYPES = ("meta_new_campaign", "meta_new_adset", "meta_new_ad")
OBJECTIVES = {"traffic": "OUTCOME_TRAFFIC", "awareness": "OUTCOME_AWARENESS", "engagement": "OUTCOME_ENGAGEMENT",
              "leads": "OUTCOME_LEADS", "sales": "OUTCOME_SALES"}
# objective -> (default optimisation goal, other goals Studio allows). Billing is always IMPRESSIONS, bidding lowest cost.
GOALS = {"OUTCOME_TRAFFIC": ("LANDING_PAGE_VIEWS", ("LINK_CLICKS", "REACH", "IMPRESSIONS")),
         "OUTCOME_AWARENESS": ("REACH", ("IMPRESSIONS", "THRUPLAY")),
         "OUTCOME_ENGAGEMENT": ("POST_ENGAGEMENT", ("THRUPLAY",)),
         "OUTCOME_LEADS": ("OFFSITE_CONVERSIONS", ("LEAD_GENERATION", "LANDING_PAGE_VIEWS")),
         "OUTCOME_SALES": ("OFFSITE_CONVERSIONS", ("LANDING_PAGE_VIEWS", "LINK_CLICKS"))}
DEFAULT_EVENT = {"OUTCOME_LEADS": "LEAD", "OUTCOME_SALES": "PURCHASE"}
STD_EVENTS = ("PURCHASE", "LEAD", "COMPLETE_REGISTRATION", "ADD_TO_CART", "INITIATED_CHECKOUT", "CONTENT_VIEW", "SEARCH",
              "CONTACT", "SUBMIT_APPLICATION", "SCHEDULE", "ADD_PAYMENT_INFO", "ADD_TO_WISHLIST", "START_TRIAL", "SUBSCRIBE")
SPECIAL_CATS = ("EMPLOYMENT", "HOUSING", "FINANCIAL_PRODUCTS_SERVICES", "ISSUES_ELECTIONS_POLITICS", "ONLINE_GAMBLING_AND_GAMING")


def _ref(v):
    """'d-<draft item id>' (a new campaign / ad set in this draft) -> '<draft item id>', else None."""
    v = str(v or "")
    return v[2:] if v.startswith("d-") else None


def _items(extra=None):
    """Draft items by id (plus items being added right now), for references between new objects."""
    out = {it["id"]: it for it in core.draft(acct())["items"]}
    for it in extra or []:
        out[it["id"]] = it
    return out


def _camp_facts(cid, d, items):
    """-> {name, objective, cbo, new} for a real campaign id or a 'd-<id>' draft campaign."""
    r = _ref(cid)
    if r:
        it = items.get(r)
        if not it or it["change"].get("type") != "meta_new_campaign":
            raise ValueError(f"campaign {cid} is not a new campaign in this draft")
        c = it["change"]
        return {"name": c.get("name", ""), "objective": c.get("objective"), "cbo": bool(c.get("daily_budget")), "new": True}
    o = _find(d, "campaign", cid)
    return {"name": o["name"], "objective": o.get("objective"), "cbo": bool(o.get("daily_budget") or o.get("lifetime_budget")), "new": False}


def _adset_facts(sid, d, items):
    r = _ref(sid)
    if r:
        it = items.get(r)
        if not it or it["change"].get("type") != "meta_new_adset":
            raise ValueError(f"ad set {sid} is not a new ad set in this draft")
        c = it["change"]
        return {"name": c.get("name", ""), "campaign_id": str(c.get("campaign_id")), "new": True}
    o = _find(d, "adset", sid)
    return {"name": o["name"], "campaign_id": o["campaign_id"], "new": False}


def _pl_text(pl):
    pl = pl or {}
    if pl.get("advantage", not pl.get("publisher_platforms")):
        return "Advantage+ placements"
    return _placements(pl)


def summarise(c, d=None, items=None):
    d = d or load()
    t = c.get("type")
    lvl = c.get("level", "ad")
    if t in TEST_TYPES:
        import meta_tests  # noqa: PLC0415
        return meta_tests.summarise(c, d, items if items is not None else _items())
    if t == meta_audiences.TYPE:
        return meta_audiences.summarise(c, d)
    if t == "meta_new_campaign":
        b = f"campaign budget {float(c['daily_budget']):.2f}/day" if c.get("daily_budget") else "ad set budgets"
        return f'Meta new campaign "{c["name"]}" ({_st(str(c.get("objective", "")).replace("OUTCOME_", ""))}, {b}, Paused)'
    if t == "meta_new_adset":
        items = items if items is not None else _items()
        try:
            camp = _camp_facts(c["campaign_id"], d, items)["name"]
        except (KeyError, ValueError):
            camp = str(c.get("campaign_id"))
        b = f"daily {float(c['daily_budget']):.2f}" if c.get("daily_budget") else "campaign budget"
        age = f"age {c.get('age_min', 18)}-{c.get('age_max', 65)}"
        return (f'Meta new ad set "{c["name"]}" in campaign "{camp}" ({b}; {", ".join(c.get("countries") or [])}; {age}; '
                f'{_pl_text(c.get("placements"))}; Paused)')
    if t == "meta_new_ad":
        items = items if items is not None else _items()
        try:
            where = _adset_facts(c["adset_id"], d, items)["name"]
        except (KeyError, ValueError):
            where = str(c.get("adset_id"))
        src = []
        if c.get("cards"):
            src.append(f'carousel of {len(c["cards"])} uploaded images')
        if c.get("media_id"):
            m = _media_index().get(c["media_id"]) or {}
            src.append(f'uploaded {m.get("kind", "file")} "{m.get("name", c["media_id"][:8])}"')
        if c.get("from_ad"):
            src.append(f'copy of "{_name(d, "ad", c["from_ad"])}"')
        return f'Meta new ad "{c["name"]}" in ad set "{where}" ({", ".join(src)}, Paused)'
    if t == "meta_boost_post":
        items = items if items is not None else _items()
        try:
            where = _adset_facts(c["adset_id"], d, items)["name"]
        except (KeyError, ValueError):
            where = str(c.get("adset_id"))
        src = "Instagram" if c.get("channel") == "instagram" else "Facebook"
        return f'Meta new ad "{c.get("name")}" in ad set "{where}" from {src} post {c.get("post_id")} (boost, Paused)'
    who = f'{lvl} "{c.get("name") or _name(d, lvl, c.get("id"))}"'
    if t == "meta_status":
        return f"Meta {who}: {c['status']}"
    if t == "meta_budget":
        kind = "lifetime" if c.get("lifetime") else "daily"
        return f"Meta {who}: {kind} budget {c['budget']:.2f}"
    if t == "meta_end":
        return f"Meta {who}: end date {c['end_date'] or 'none'}"
    if t == "meta_rename":
        return f"Meta {who}: rename to \"{c['new_name']}\""
    if t == "meta_targeting":
        bits = [f"countries {', '.join(c['countries'])}" if c.get("countries") else "",
                f"age {c.get('age_min', '')}-{c.get('age_max', '')}" if c.get("age_min") or c.get("age_max") else "",
                meta_audiences.targeting_words(c, d)]
        return f"Meta {who}: " + "; ".join(b for b in bits if b)
    if t == "meta_placements":
        return f"Meta {who}: placements {_pl_text(c)}"
    if t == "meta_copy":
        fields = [k for k in ("primary_text", "headline", "description", "link", "cta") if c.get(k) is not None]
        return f"Meta {who}: new copy ({', '.join(fields)})"
    if t == "meta_carousel_copy":
        bits = [k for k in ("primary_text", "link") if c.get(k) is not None]
        if c.get("cards"):
            bits.append("cards " + ", ".join(str(int(x["index"]) + 1) for x in c["cards"]))
        return f"Meta {who}: new carousel copy ({'; '.join(bits)})"
    if t == "meta_feed_copy":
        bits = [f"{k} ({len(c[k])})" for k in ("bodies", "titles", "descriptions", "link_urls") if c.get(k) is not None]
        return f"Meta {who}: new text options ({', '.join(bits)})"
    return json.dumps(c, ensure_ascii=False)


# Taken out of the tests step (a04): Studio never creates anything Active through a test. Refused on every change type.
NOT_YET = {"from_adset": "copying an ad set (Duplicate with one change) is not available yet",
           "activate_with_test": "creating objects Active for a test is not available yet: new objects are always created Paused"}


def _not_yet(c):
    """NOT_YET keys set on a change, or on anything nested in it (e.g. a test's cells)."""
    found = [k for k in NOT_YET if c.get(k)]
    for x in c.get("cells") or []:
        if isinstance(x, dict):
            found += [k for k in NOT_YET if x.get(k) and k not in found]
    return found


PRICE = re.compile(r"(?:[$£€]\s?\d(?:[\d,.]*\d)?|\d(?:[\d,.]*\d)?\s?(?:USD|GBP|EUR))")
PCT = re.compile(r"\d{1,2}\s?%")


def _page_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Macintosh) AdsStudio/1.0"})
    raw = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "replace")
    raw = re.sub(r"(?s)<(script|style)[^>]*>.*?</\1>", " ", raw)
    return html.unescape(re.sub(r"<[^>]+>", " ", raw))


def link_problem(url):
    """A link that answers 404/410 would send every click to a missing page."""
    if not url or "{{" in url:
        return ""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Macintosh) AdsStudio/1.0"})
        urllib.request.urlopen(req, timeout=15).close()
        return ""
    except urllib.error.HTTPError as ex:
        return f"the link answers {ex.code}: {url}" if ex.code in (404, 410) else ""
    except Exception:  # noqa: BLE001 - network trouble is not a dead link
        return ""


def page_check(text, url):
    """Prices and percentages in the copy that are not on the linked page (ML ads must match the live page)."""
    found = PRICE.findall(text or "") + PCT.findall(text or "")
    if not found or not url:
        return []
    try:
        page = _page_text(url.split("?")[0] if "{{" in url else url)
    except Exception as ex:  # noqa: BLE001 - a dead link is reported by link_problem
        return [] if isinstance(ex, urllib.error.HTTPError) else [f"couldn't open the linked page to check prices ({ex})"]
    flat = re.sub(r"[\s,]", "", page)
    return [f'"{f.strip()}" is not on the linked page - check it is still true' for f in sorted(set(found))
            if re.sub(r"[\s,]", "", f) not in flat]


def _future_date(v, what, problems, after=None):
    try:
        x = dt.date.fromisoformat(str(v))
    except ValueError:
        problems.append(f"{what} must be YYYY-MM-DD")
        return None
    if x < dt.date.today():
        problems.append(f"{what} can't be in the past")
    if after and x <= after:
        problems.append(f"{what} must be after the start date")
    return x


def _placement_problems(pl):
    pl = pl or {}
    if pl.get("advantage", not pl.get("publisher_platforms")):
        return []
    out = []
    pubs = pl.get("publisher_platforms") or []
    if not pubs:
        out.append("placements: pick at least one platform, or set advantage true")
    bad = [p for p in pubs if p not in PLATFORMS]
    if bad:
        out.append(f"placements: unknown platform {', '.join(bad)} (one of {', '.join(PLATFORMS)})")
    for key, allowed, plat in (("facebook_positions", FB_POS, "facebook"), ("instagram_positions", IG_POS, "instagram")):
        bad = [p for p in pl.get(key) or [] if p not in allowed]
        if bad:
            out.append(f"{key}: unknown {', '.join(bad)} (one of {', '.join(allowed)})")
        if pl.get(key) and plat not in pubs:
            out.append(f"{key} given but {plat} is not in publisher_platforms")
    return out


def _texts(c):
    """(field label, text) pairs of everything written in a change, for the copy rules."""
    out = [(k, c[k]) for k in ("primary_text", "headline", "description", "new_name", "name") if c.get(k)]
    for x in c.get("cards") or []:
        out += [(f"card {int(x.get('index', 0)) + 1} {k}", x[k]) for k in ("name", "description") if x.get(k)]
    for k in ("bodies", "titles", "descriptions"):
        out += [(f"{k} {i + 1}", v) for i, v in enumerate(c.get(k) or []) if isinstance(v, str) and v]
    return out


COPY_LABEL = re.compile(r"^(primary_text|headline|description|card \d+ (name|description)|(bodies|titles|descriptions) \d+)$")


def check(c, d=None, items=None):
    """-> (problems that block, warnings). items: draft items by id (for 'd-<id>' references to new objects)."""
    d = d or load()
    t = c.get("type")
    problems, warnings = [], []
    links = []  # (link, copy text) pairs to check for dead pages and prices
    try:
        if t not in TYPES:
            return [f"unknown Meta change type {t!r}"], []
        items = items if items is not None else _items()
        if t in TEST_TYPES:
            import meta_tests  # noqa: PLC0415
            p_, w_ = meta_tests.check(c, d, items)
            problems += p_
            warnings += w_
        elif t == meta_audiences.TYPE:
            p_, w_ = meta_audiences.check(c, d)
            problems += p_
            warnings += w_
        elif t == "meta_new_campaign":
            if not c.get("name"):
                problems.append("the new campaign needs a name")
            if c.get("objective") not in OBJECTIVES.values():
                problems.append(f"objective must be one of {', '.join(OBJECTIVES)}")
            if c.get("daily_budget") is not None and float(c["daily_budget"]) < 1:
                problems.append("budget must be at least 1.00")
            bad = [x for x in c.get("special_ad_categories") or [] if x not in SPECIAL_CATS]
            if bad:
                problems.append(f"special_ad_categories: unknown {', '.join(bad)} (one of {', '.join(SPECIAL_CATS)})")
            if c.get("objective") == "OUTCOME_LEADS" and not c.get("special_ad_categories") and re.search(r"\b(hiring|job|career|recruit)", c.get("name", ""), re.I):
                warnings.append('job ads must use special_ad_categories ["EMPLOYMENT"]')
        elif t == "meta_new_adset":
            if not c.get("name"):
                problems.append("the new ad set needs a name")
            camp = _camp_facts(c.get("campaign_id"), d, items)
            if camp["cbo"]:
                if c.get("daily_budget") is not None:
                    problems.append("the campaign has a campaign budget (CBO): leave the ad set budget out")
            elif c.get("daily_budget") is None or float(c["daily_budget"]) < 1:
                problems.append("the ad set needs a daily budget of at least 1.00 (the campaign has no campaign budget)")
            obj = camp["objective"]
            if obj in GOALS and c.get("optimization_goal") and c["optimization_goal"] != GOALS[obj][0] and c["optimization_goal"] not in GOALS[obj][1]:
                problems.append(f"optimization_goal for {obj}: one of {', '.join((GOALS[obj][0],) + GOALS[obj][1])}")
            if not c.get("countries"):
                problems.append("the new ad set needs at least one country")
            elif any(not re.fullmatch(r"[A-Za-z]{2}", str(x)) for x in c["countries"]):
                problems.append("countries are 2-letter codes, e.g. GB, DE")
            lo, hi = int(c.get("age_min") or 18), int(c.get("age_max") or 65)
            if lo < 18 or hi > 65 or lo > hi:
                problems.append("age must be 18-65 with min <= max")
            start = _future_date(c["start_date"], "start date", problems) if c.get("start_date") else None
            if c.get("end_date"):
                _future_date(c["end_date"], "end date", problems, after=start)
            problems += _placement_problems(c.get("placements"))
        elif t == "meta_new_ad":
            _adset_facts(c.get("adset_id"), d, items)
            if not c.get("name"):
                problems.append("the new ad needs a name")
            if c.get("cards"):  # a new carousel: every card is an uploaded image (Studio can't copy carousels)
                problems += _carousel_problems(c)
            elif not c.get("from_ad") and not c.get("media_id"):
                problems.append("a new ad needs from_ad (copy an ad's image/video), media_id (an uploaded file) or cards (a carousel)")
            src = None
            if c.get("from_ad"):
                src = _find(d, "ad", c["from_ad"])
                ct = creative_text(src.get("creative"))
                if not ct["editable"]:
                    problems.append(f"can't copy that ad: {ct['why_not']}")
            if c.get("media_id"):
                if c["media_id"] not in _media_index():
                    problems.append("media_id not found: upload the file again")
                if not src:
                    for k in ("link", "primary_text", "headline"):
                        if not c.get(k):
                            problems.append(f"a new ad from an uploaded file needs {k}")
            link = c.get("link") or (creative_text(src.get("creative"))["link"] if src else "")
            links.append((link, c.get("primary_text")))
            for x in c.get("cards") or []:
                if isinstance(x, dict) and x.get("link"):
                    links.append((x["link"], " ".join(x.get(k) or "" for k in ("name", "description"))))
        elif t == "meta_boost_post":
            _adset_facts(c.get("adset_id"), d, items)
            if not c.get("name"):
                problems.append("the boost ad needs a name")
            if c.get("channel") not in ("facebook", "instagram"):
                problems.append("channel must be facebook or instagram")
            elif c["channel"] == "facebook" and not re.fullmatch(r"\d+_\d+", str(c.get("post_id") or "")):
                problems.append("post_id must be a Facebook Page post id (<page id>_<post id>)")
            elif c["channel"] == "instagram" and not re.fullmatch(r"\d+", str(c.get("post_id") or "")):
                problems.append("post_id must be an Instagram media id")
        else:
            if c.get("level") not in LEVELS:
                return ["level must be campaign, adset or ad"], []
            if _ref(c.get("id")):
                return ["this is a new object in the draft: edit that draft item instead"], []
            o = _find(d, c["level"], c["id"])
            if t == "meta_status" and c.get("status") not in STATUS:
                problems.append("status must be active or paused (Studio never deletes on Meta)")
            if t == "meta_budget":
                if c["level"] == "ad":
                    problems.append("ads have no budget")
                elif float(c.get("budget") or 0) < 1:
                    problems.append("budget must be at least 1.00")
                elif c["level"] == "adset":
                    camp = _find(d, "campaign", o["campaign_id"])
                    if camp.get("daily_budget") or camp.get("lifetime_budget"):
                        problems.append("this campaign uses a campaign budget (CBO): change the campaign's budget instead")
                elif c["level"] == "campaign" and not (o.get("daily_budget") or o.get("lifetime_budget")):
                    problems.append("this campaign has ad set budgets: change the ad sets instead")
            if t in ("meta_end", "meta_targeting", "meta_placements") and c["level"] != "adset":
                problems.append("only ad sets have an end date, targeting and placements")
            if t == "meta_targeting":  # [audiences] attach / detach audiences on the ad set
                problems += meta_audiences.targeting_problems(c, d)
                if not any(c.get(k) for k in ("countries", "age_min", "age_max") + meta_audiences.AUD_KEYS):
                    problems.append("nothing to change: give countries, age_min / age_max or audiences_add / _remove, excluded_add / _remove")
            if t == "meta_end" and c.get("end_date"):
                try:
                    if dt.date.fromisoformat(c["end_date"]) <= dt.date.today():
                        problems.append("end date must be in the future")
                except ValueError:
                    problems.append("end date must be YYYY-MM-DD")
            if t == "meta_rename" and not c.get("new_name"):
                problems.append("new_name is empty")
            if t == "meta_placements":
                problems += _placement_problems(c)
            if t in ("meta_copy", "meta_carousel_copy", "meta_feed_copy"):
                if c["level"] != "ad":
                    problems.append("copy changes are per ad")
                else:
                    ct = creative_text(o.get("creative"))
                    if ct["edit_type"] != t:
                        problems.append(ct["why_not"] or f"this ad's copy is edited with {ct['edit_type']}, not {t}")
                    elif t == "meta_copy":
                        links.append((c.get("link") or ct["link"], c.get("primary_text")))
                    elif t == "meta_carousel_copy":
                        n = len(ct["cards"])
                        links.append((c.get("link") or ct["link"], c.get("primary_text")))
                        if not c.get("cards") and c.get("primary_text") is None and c.get("link") is None:
                            problems.append("nothing to change: give primary_text, link or cards")
                        for x in c.get("cards") or []:
                            if not isinstance(x.get("index"), int) or not 0 <= x["index"] < n:
                                problems.append(f"card index must be 0-{n - 1}")
                                continue
                            for k in ("name", "link"):
                                if k in x and not x[k]:
                                    problems.append(f"card {x['index'] + 1}: {k} can't be empty")
                            links.append((x.get("link") or ct["cards"][x["index"]]["link"],
                                          " ".join(x.get(k) or "" for k in ("name", "description"))))
                    else:
                        lists = [k for k in ("bodies", "titles", "descriptions", "link_urls") if c.get(k) is not None]
                        if not lists:
                            problems.append("nothing to change: give bodies, titles, descriptions or link_urls")
                        for k in lists:
                            if not isinstance(c[k], list) or not c[k] or any(not isinstance(v, str) or not v.strip() for v in c[k]):
                                problems.append(f"{k}: a list of non-empty texts")
                            elif len(c[k]) > 5:
                                warnings.append(f"{k}: {len(c[k])} options - Meta allows up to 5 for most formats")
                        urls = c.get("link_urls") or ct["link_urls"] or [ct["link"]]
                        for i, u in enumerate(urls):
                            links.append((u, " ".join((c.get("bodies") or [])[:1] if i == 0 else [])))
        if c.get("cta") and c["cta"] not in CTAS:
            problems.append(f"cta must be one of {', '.join(CTAS)}")
        for x in c.get("cards") or []:
            if x.get("cta") and x["cta"] not in CTAS:
                problems.append(f"card cta must be one of {', '.join(CTAS)}")
    except (KeyError, TypeError, ValueError) as ex:
        problems.append(f"invalid change: {ex}")
    problems += [f"{k}: {NOT_YET[k]}" for k in _not_yet(c)]
    if c.get("with_end") and (t != "meta_status" or c.get("status") != "paused"):
        problems.append("with_end is only for pausing (meta_status paused) together with End test")
    texts = _texts(c)
    try:
        import team_settings  # noqa: PLC0415 - copy rules (key copy_rules, area "meta_ads")
        rules = team_settings.copy_rules("meta_ads")
    except Exception:  # noqa: BLE001
        rules = []
    for k, v in texts:
        for level, rx, msg in rules:
            if level == "block" and rx.search(v):
                problems.append(f"{k}: {msg}")
            elif level == "warn" and COPY_LABEL.match(k) and rx.search(v):
                warnings.append(f"{k}: {msg}")
    if links and not problems:
        bodies = [("primary_text", c.get("primary_text"))] + [(f"bodies {i + 1}", v) for i, v in enumerate(c.get("bodies") or [])]
        main = next((u for u, _ in links if u), "")
        for k, pt in bodies:
            if pt is not None and main and not pt.rstrip().endswith(main.split("?")[0].rstrip("/")) and not pt.rstrip().endswith(main):
                warnings.append(f"{k}: end with a blank line and the link (drives more link clicks)")
            if pt and re.search(r"[$£€]\d{1,3},\d{3}", pt):
                warnings.append(f"{k}: remove the comma in prices ($1155 not $1,155) - Meta misreads it")
        for k in ["headline"] + [f"titles {i + 1}" for i in range(len(c.get("titles") or []))]:
            v = c.get("headline") if k == "headline" else c["titles"][int(k.split()[1]) - 1]
            if len(v or "") > 40:
                warnings.append(f"{k}: {len(v)} chars - Meta cuts it after about 40")
        copy = " ".join(v for k, v in texts if COPY_LABEL.match(k))
        for link in sorted({u for u, _ in links if u}):
            dead = link_problem(link)
            if dead:
                problems.append(dead)
            else:
                warnings += page_check(copy, link)
    return problems, sorted(set(warnings))


def _normalise(c):
    c = dict(c)
    if c.get("type") in TEST_TYPES:
        import meta_tests  # noqa: PLC0415
        c = meta_tests.normalise(c)
    if c.get("type") == meta_audiences.TYPE:
        c = meta_audiences.normalise(c)
    for k in meta_audiences.AUD_KEYS:
        if c.get(k):
            c[k] = [str(x) for x in c[k]]
    if c.get("status"):
        c["status"] = c["status"].lower().replace("enabled", "active")
    if c.get("type") == "meta_new_campaign":
        o = str(c.get("objective") or "").strip()
        c["objective"] = OBJECTIVES.get(o.lower().replace("outcome_", ""), o.upper())
        c.setdefault("special_ad_categories", [])
    if c.get("type") == "meta_new_adset":
        c["countries"] = [str(x).upper() for x in c.get("countries") or []]
        if c.get("campaign_draft"):
            c["campaign_id"] = "d-" + str(c.pop("campaign_draft"))
    if c.get("type") == "meta_new_ad" and isinstance(c.get("cards"), list):
        c["cards"] = [{**x, "index": i} if isinstance(x, dict) else x for i, x in enumerate(c["cards"])]
    if c.get("type") in ("meta_new_ad", "meta_boost_post") and c.get("adset_draft"):
        c["adset_id"] = "d-" + str(c.pop("adset_draft"))
    if c.get("type") == "meta_boost_post":
        c["channel"] = str(c.get("channel") or "facebook").lower()
        c["post_id"] = str(c.get("post_id") or "")
    for k in ("campaign_id", "adset_id", "from_ad", "id"):
        if c.get(k) is not None and not isinstance(c[k], str):
            c[k] = str(c[k])
    return c


def add(changes, by="you", note=None):
    """Add changes. New objects can point at each other in one call: give a change "ref":"x" and refer to it
    with "campaign_id":"d-x" / "adset_id":"d-x" (or campaign_draft / adset_draft = "x" or an existing draft item id)."""
    if isinstance(changes, dict):
        changes = [changes]
    d = load()
    refused = meta_audiences.refusals([_normalise(c) for c in changes], d)  # [audiences] an audience in use can't be removed
    if refused:
        raise ValueError("Not added to the draft. " + "; ".join(refused))
    items = []
    for c in changes:
        c = _normalise(c)
        items.append({"id": uuid.uuid4().hex[:8], "change": c, "by": by, "note": note or "",
                      "ts": dt.datetime.now().isoformat(timespec="seconds")})
    refs = {it["change"].pop("ref"): it["id"] for it in items if it["change"].get("ref")}
    for it in items:
        for k in ("campaign_id", "adset_id"):
            r = _ref(it["change"].get(k))
            if r in refs:
                it["change"][k] = "d-" + refs[r]
        ch = it["change"]
        for cell in ch.get("cells") or [] if ch.get("type") in TEST_TYPES else []:
            cell["ids"] = ["d-" + refs[_ref(o)] if _ref(o) in refs else o for o in cell.get("ids") or []]
        if ch.get("type") in TEST_TYPES and ch.get("ads"):
            ch["ads"] = ["d-" + refs[_ref(o)] if _ref(o) in refs else o for o in ch["ads"]]
    pool = _items(items)
    for it in items:
        c = it["change"]
        it["problems"], it["warnings"] = check(c, d, pool)
        try:
            it["summary"] = summarise(c, d, pool)
        except (KeyError, TypeError, ValueError):
            it["summary"] = json.dumps(c, ensure_ascii=False)
    with core._locked(acct()) as dr:
        dr["items"].extend(items)
    return items


def validate():
    """Re-check every draft item against the cached Meta data (the local checks; Meta's own check, on live data, runs in
    the live preview). In auto mode the cache is first re-pulled when older than a minute."""
    d = load(max_age=60, wait=True)
    results = []
    with core._locked(acct()) as dr:
        pool = {it["id"]: it for it in dr["items"]}
        for it in dr["items"]:
            it["problems"], it["warnings"] = check(it["change"], d, pool)
            results.append({"id": it["id"], "summary": it["summary"], "ok": not it["problems"], "problems": it["problems"], "warnings": it["warnings"]})
    return results


def _overlay(kind, out, campaign_id, adset_id):
    items = core.draft(acct())["items"]
    if not items:
        return
    by_id = {r["id"]: r for r in out}
    col_level = {"campaigns": "campaign", "adsets": "adset", "ads": "ad"}.get(kind)
    pool = {it["id"]: it for it in items}
    d = load()
    ad_rows = None
    for it in items:
        c, t = it["change"], it["change"].get("type")
        mark = {"draft_id": it["id"], "by": it["by"]}
        try:
            if t == "meta_new_campaign":
                if kind == "campaigns" and not campaign_id and not adset_id:
                    out.insert(0, {"id": f"d-{it['id']}", "campaign_id": f"d-{it['id']}", "ad_group_id": None, "campaign": c.get("name"),
                                   "status": "Paused", "delivery": "draft", "objective": _st(str(c.get("objective", "")).replace("OUTCOME_", "")),
                                   "budget": float(c["daily_budget"]) if c.get("daily_budget") else None, "lifetime_budget": None,
                                   "budget_level": "Campaign (CBO)" if c.get("daily_budget") else "Ad sets", "bidding": "Lowest Cost Without Cap",
                                   "ad_sets": sum(1 for x in items if x["change"].get("campaign_id") == f"d-{it['id']}"), "ads": 0,
                                   "_new": mark, **dict.fromkeys(INS_KEYS)})
                continue
            if t == "meta_new_adset":
                if kind == "adsets" and (not campaign_id or str(campaign_id) == c.get("campaign_id")) and not adset_id:
                    camp = _camp_facts(c["campaign_id"], d, pool)
                    tg = _apply_placements({"geo_locations": {"countries": c.get("countries") or []}, "age_min": c.get("age_min", 18),
                                            "age_max": c.get("age_max", 65)}, c.get("placements") or {})
                    out.insert(0, {"id": f"d-{it['id']}", "campaign_id": c["campaign_id"], "ad_group_id": f"d-{it['id']}",
                                   "campaign": camp["name"], "ad_set": c.get("name"), "status": "Paused", "delivery": "draft",
                                   "budget": None if camp["cbo"] else c.get("daily_budget"), "lifetime_budget": None, "cbo": camp["cbo"],
                                   "optimisation": _st(c.get("optimization_goal") or GOALS.get(camp["objective"], ("",))[0]),
                                   "end": c.get("end_date") or "", **_targeting(tg), "advantage_audience": bool(c.get("advantage_audience")),
                                   "_new": mark, **dict.fromkeys(INS_KEYS)})
                continue
            if t == "meta_new_ad":
                if kind == "ads" and (not adset_id or str(adset_id) == c["adset_id"]):
                    if ad_rows is None:
                        ad_rows = {r["id"]: r for r in rows(d, "ads")}
                    src = ad_rows.get(str(c.get("from_ad")), {})
                    aset = _adset_facts(c["adset_id"], d, pool)
                    camp_id = aset["campaign_id"]
                    if campaign_id and camp_id != str(campaign_id):
                        continue
                    m = _media_index().get(c.get("media_id") or "") or {}
                    row = {**{k: src.get(k) for k in ("format", "cta", "link", "headline", "description", "primary_text", "image", "thumbnail")},
                           "campaign": _camp_facts(camp_id, d, pool)["name"], "ad_set": aset["name"], "campaign_id": camp_id,
                           "ad_group_id": c["adset_id"], "id": f"d-{it['id']}", "ad": c["name"], "status": "Paused", "delivery": "draft",
                           "_new": mark, **{k: c[k] for k in ("primary_text", "headline", "description", "link", "cta") if c.get(k) is not None}}
                    if m:
                        row.update({"format": m["kind"].title(), "media_id": c["media_id"], "image": "", "thumbnail": ""})
                    if c.get("cards"):
                        row.update({"format": "Carousel", "media_id": c["cards"][0].get("media_id"), "image": "", "thumbnail": "",
                                    "cards": [{"index": i, **{k: x.get(k, "") for k in ("name", "description", "link", "media_id")}}
                                              for i, x in enumerate(c["cards"])]})
                    out.insert(0, row)
                continue
        except (KeyError, ValueError):
            continue
        if c.get("level") != col_level or str(c.get("id")) not in by_id:
            continue
        r = by_id[str(c["id"])]
        r.setdefault("_draft", {})
        if t == "meta_status":
            r["_draft"]["status"] = {**mark, "value": c["status"].title()}
        elif t == "meta_budget":
            r["_draft"]["lifetime_budget" if c.get("lifetime") else "budget"] = {**mark, "value": float(c["budget"])}
        elif t == "meta_end":
            r["_draft"]["end"] = {**mark, "value": c["end_date"] or ""}
        elif t == "meta_rename":
            r["_draft"][{"campaign": "campaign", "adset": "ad_set", "ad": "ad"}[c["level"]]] = {**mark, "value": c["new_name"]}
        elif t == "meta_targeting":
            if any(c.get(k) for k in meta_audiences.AUD_KEYS):
                r["_draft"]["audiences"] = {**mark, "value": meta_audiences.targeting_words(c, d)}
            if c.get("countries"):
                r["_draft"]["countries"] = {**mark, "value": ", ".join(c["countries"])}
            if c.get("age_min") or c.get("age_max"):
                r["_draft"]["age"] = {**mark, "value": f"{c.get('age_min', '')}-{c.get('age_max', '')}"}
        elif t == "meta_placements":
            r["_draft"]["placements"] = {**mark, "value": _pl_text(c)}
        elif t == "meta_copy":
            for k in ("primary_text", "headline", "description", "link", "cta"):
                if c.get(k) is not None:
                    r["_draft"][k] = {**mark, "value": c[k]}
        elif t == "meta_carousel_copy":
            for k in ("primary_text", "link"):
                if c.get(k) is not None:
                    r["_draft"][k] = {**mark, "value": c[k]}
            if c.get("cards"):
                cards = json.loads(json.dumps(r.get("cards") or []))
                for x in c["cards"]:
                    if 0 <= int(x["index"]) < len(cards):
                        cards[x["index"]].update({k: v for k, v in x.items() if k in ("name", "description", "link", "cta")})
                r["_draft"]["cards"] = {**mark, "value": cards}
        elif t == "meta_feed_copy":
            for k in ("bodies", "titles", "descriptions", "link_urls"):
                if c.get(k) is not None:
                    r["_draft"][k] = {**mark, "value": c[k]}


# ---------- live: plan (validate_only) / push / undo ----------

def _live(level, oid):
    fields = {"campaign": "name,status,objective,daily_budget,lifetime_budget",
              "adset": "name,status,daily_budget,lifetime_budget,end_time,targeting,campaign_id",
              "ad": "name,status,adset_id,creative{id,object_story_spec,asset_feed_spec,object_story_id,url_tags,degrees_of_freedom_spec}"}[level]
    return get(str(oid), fields=fields)


def _money_txt(v):
    return f"{int(v) / 100:.2f}" if v not in (None, "") else "none"


def _clean_oss(oss):
    """Drop read-back fields Meta refuses when they are sent next to the field they were derived from."""
    ld, vd = oss.get("link_data"), oss.get("video_data")
    if ld:
        if ld.get("image_hash"):
            ld.pop("picture", None)  # picture is the read-back URL of image_hash; sending both is refused
        for ch in ld.get("child_attachments") or []:
            if ch.get("image_hash"):
                ch.pop("picture", None)
    if vd and vd.get("image_hash"):
        vd.pop("image_url", None)
    return oss


def _new_spec(cr, c):
    """A new object_story_spec from an existing creative with the draft's copy applied."""
    oss = json.loads(json.dumps(cr["object_story_spec"]))
    if "link_data" in oss:
        ld = oss["link_data"]
        for k, src in (("message", "primary_text"), ("name", "headline"), ("description", "description"), ("link", "link")):
            if c.get(src) is not None:
                ld[k] = c[src]
        if c.get("cta") or c.get("link"):
            cta = ld.setdefault("call_to_action", {"type": "LEARN_MORE"})
            if c.get("cta"):
                cta["type"] = c["cta"]
            if c.get("link") and "value" in cta:
                cta["value"]["link"] = c["link"]
        for x in c.get("cards") or []:  # carousel cards (meta_carousel_copy)
            ch = ld["child_attachments"][int(x["index"])]
            for k in ("name", "description", "link"):
                if x.get(k) is not None:
                    ch[k] = x[k]
            if x.get("cta") or x.get("link"):
                cta = ch.setdefault("call_to_action", {"type": (ld.get("call_to_action") or {}).get("type", "LEARN_MORE")})
                if x.get("cta"):
                    cta["type"] = x["cta"]
                if x.get("link"):
                    cta.setdefault("value", {})["link"] = x["link"]
    else:
        vd = oss["video_data"]
        for k, src in (("message", "primary_text"), ("title", "headline"), ("link_description", "description")):
            if c.get(src) is not None:
                vd[k] = c[src]
        cta = vd.setdefault("call_to_action", {"type": "LEARN_MORE", "value": {}})
        if c.get("cta"):
            cta["type"] = c["cta"]
        if c.get("link"):
            cta.setdefault("value", {})["link"] = c["link"]
    return _clean_oss(oss)


def _new_feed(feed, c):
    """asset_feed_spec with the draft's text lists (meta_feed_copy). Extra fields on each entry (e.g. adlabels) are kept."""
    f = json.loads(json.dumps(feed))
    for k, field in (("bodies", "text"), ("titles", "text"), ("descriptions", "text"), ("link_urls", "website_url")):
        if c.get(k) is None:
            continue
        old = f.get(k) or []
        f[k] = [{**(old[i] if i < len(old) else {}), field: v} for i, v in enumerate(c[k])]
    if f.get("images"):
        f["images"] = [{k: v for k, v in im.items() if k in ("hash", "adlabels")} for im in f["images"]]
    if f.get("videos"):
        f["videos"] = [{k: v for k, v in vi.items() if k in ("video_id", "thumbnail_hash", "thumbnail_url", "adlabels")}
                       for vi in f["videos"]]
    return f


# ---------- Marketing API v27: say every new default out loud, so nothing turns on without the user ----------
# v27.0 (Meta, 4 November 2026) turns two things on when a request leaves them out. Studio always sends them:
#  - ad sets: "Reach more people across other mobile devices" (targeting_automation.individual_setting.device) turns on
#    for eligible ad sets that leave it out. Studio sends 0, or keeps what the ad set already has.
#  - creatives: Meta may switch Product showcase on (product_extensions) for a creative that leaves it out. Studio sends
#    OPT_OUT, or keeps what the source creative already has.
# advantage_audience is still always set on new ad sets (0 or 1, from the draft).

def _pin_device(tg):
    """A copy of targeting with targeting_automation.individual_setting.device and targeting_automation
    .advantage_audience set: each kept as it is, else 0 (off)."""
    tg = json.loads(json.dumps(tg or {}))
    ta = tg.get("targeting_automation") if isinstance(tg.get("targeting_automation"), dict) else {}
    ind = ta.get("individual_setting") if isinstance(ta.get("individual_setting"), dict) else {}
    if ind.get("device") is None:
        ind["device"] = 0
    tg["targeting_automation"] = {**ta, "advantage_audience": ta.get("advantage_audience") or 0, "individual_setting": ind}
    return tg


def _pin_showcase(p):
    """Creative params with degrees_of_freedom_spec.creative_features_spec.product_extensions set: kept as it is,
    else OPT_OUT (Product showcase off, as before v27). Returns p (changed in place on a deep copy of the spec)."""
    dof = json.loads(json.dumps(p.get("degrees_of_freedom_spec") or {}))
    cfs = dof.get("creative_features_spec") if isinstance(dof.get("creative_features_spec"), dict) else {}
    if not cfs.get("product_extensions"):
        cfs["product_extensions"] = {"enroll_status": "OPT_OUT"}
    dof["creative_features_spec"] = cfs
    p["degrees_of_freedom_spec"] = dof
    return p


def _creative_params(cr, c, name):
    if cr.get("asset_feed_spec"):  # the texts live in the feed spec; the story spec only carries page, media and link
        p = {"name": name[:100], "object_story_spec": _clean_oss(json.loads(json.dumps(cr["object_story_spec"]))),
             "asset_feed_spec": _new_feed(cr["asset_feed_spec"], c)}
    else:
        p = {"name": name[:100], "object_story_spec": _new_spec(cr, c)}
    if cr.get("url_tags"):
        p["url_tags"] = cr["url_tags"]
    if cr.get("degrees_of_freedom_spec"):
        p["degrees_of_freedom_spec"] = cr["degrees_of_freedom_spec"]
    return _pin_showcase(p)


def _identity(src_cr=None, need_ig=False):
    """page_id (+ instagram_user_id) for a new creative: from the source ad, else the pair most ads in the account use.
    need_ig: only pairs with an Instagram account count (an Instagram boost), even when most ads have none."""
    if src_cr and src_cr.get("object_story_spec", {}).get("page_id"):
        o = src_cr["object_story_spec"]
        return {k: o[k] for k in ("page_id", "instagram_user_id") if o.get(k)}
    pairs = collections.Counter()
    for a in load()["ads"]:
        o = (a.get("creative") or {}).get("object_story_spec") or {}
        if o.get("page_id") and (o.get("instagram_user_id") or not need_ig):
            pairs[(o["page_id"], o.get("instagram_user_id"))] += 1
    if pairs:
        page, ig = pairs.most_common(1)[0][0]
        return {"page_id": page, **({"instagram_user_id": ig} if ig else {})}
    return {"page_id": _page_for_account()}


def _set_media(p, kind, ref):
    oss = p["object_story_spec"]
    if kind == "image":
        oss["link_data"]["image_hash"] = ref["hash"]
    else:
        vd = oss["video_data"]
        vd["video_id"] = ref["video_id"]
        vd.pop("image_hash", None)
        vd.pop("image_url", None)
        if ref.get("image_hash"):
            vd["image_hash"] = ref["image_hash"]
        elif ref.get("image_url"):
            vd["image_url"] = ref["image_url"]
    return p


def _media_creative(c, src_cr, kind, name):
    """Creative params for a new ad built from an uploaded file (media filled in by _set_media)."""
    base = creative_text(src_cr) if src_cr else {}
    txt = {k: c[k] if c.get(k) is not None else base.get(k, "") for k in ("primary_text", "headline", "description", "link", "cta")}
    cta = {"type": txt["cta"] or "LEARN_MORE", "value": {"link": txt["link"]}}
    oss = _identity(src_cr)
    if kind == "image":
        oss["link_data"] = {k: v for k, v in (("link", txt["link"]), ("message", txt["primary_text"]), ("name", txt["headline"]),
                                              ("description", txt["description"]), ("call_to_action", cta)) if v}
    else:
        oss["video_data"] = {k: v for k, v in (("message", txt["primary_text"]), ("title", txt["headline"]),
                                               ("link_description", txt["description"]), ("call_to_action", cta)) if v}
    p = {"name": name[:100], "object_story_spec": oss}
    for k in ("url_tags", "degrees_of_freedom_spec"):
        if src_cr and src_cr.get(k):
            p[k] = src_cr[k]
    return _pin_showcase(p)


CAROUSEL_CARDS = (2, 10)


def _carousel_problems(c):
    """A new carousel (meta_new_ad with cards): 2-10 cards, each an uploaded image with a headline and a link."""
    out = []
    cards = c.get("cards")
    if not isinstance(cards, list) or not CAROUSEL_CARDS[0] <= len(cards) <= CAROUSEL_CARDS[1]:
        return [f"a carousel needs {CAROUSEL_CARDS[0]}-{CAROUSEL_CARDS[1]} cards"]
    if c.get("from_ad") or c.get("media_id"):
        out.append("a carousel takes its images from cards: leave out from_ad and media_id")
    for k in ("link", "primary_text"):
        if not c.get(k):
            out.append(f"a new carousel needs {k} (the ad's text and its 'see more' link)")
    idx = _media_index()
    for i, x in enumerate(cards):
        if not isinstance(x, dict):
            out.append(f"card {i + 1}: must be an object")
            continue
        m = idx.get(x.get("media_id") or "")
        if not m:
            out.append(f"card {i + 1}: media_id not found: upload the file again")
        elif m.get("kind") != "image":
            out.append(f"card {i + 1}: carousel cards are images only (videos not available yet)")
        for k in ("name", "link"):
            if not x.get(k):
                out.append(f"card {i + 1}: needs {k}")
    return out


def _carousel_creative(c, name):
    """Creative params for a new carousel; each card's image_hash is filled in at push (or by a stand-in to check).
    The cards stay in the order given (multi_share_optimized off) and Meta adds no end card."""
    cta_type = c.get("cta") or "LEARN_MORE"
    kids = []
    for x in c["cards"]:
        ch = {"link": x["link"], "name": x["name"],
              "call_to_action": {"type": x.get("cta") or cta_type, "value": {"link": x["link"]}}}
        if x.get("description"):
            ch["description"] = x["description"]
        kids.append(ch)
    oss = _identity()
    oss["link_data"] = {"link": c["link"], "message": c["primary_text"], "child_attachments": kids,
                        "multi_share_optimized": False, "multi_share_end_card": False,
                        "call_to_action": {"type": cta_type, "value": {"link": c["link"]}}}
    return _pin_showcase({"name": name[:100], "object_story_spec": oss})


def _stand_in_media(kind):
    """An image hash / video already in the account, used only to let Meta validate a creative before the upload."""
    for a in load()["ads"]:
        o = (a.get("creative") or {}).get("object_story_spec") or {}
        ld, vd = o.get("link_data") or {}, o.get("video_data") or {}
        if kind == "image" and ld.get("image_hash") and not ld.get("child_attachments"):
            return {"hash": ld["image_hash"]}
        if kind == "video" and vd.get("video_id") and (vd.get("image_hash") or vd.get("image_url")):
            return {"video_id": vd["video_id"], "image_hash": vd.get("image_hash"), "image_url": vd.get("image_url")}
    return None


def _any_creative(adset_id):
    """A creative id already used in the ad set (or the account), only for Meta's validate_only check of a new ad."""
    ads = load()["ads"]
    for a in [x for x in ads if x.get("adset_id") == str(adset_id)] + ads:
        if (a.get("creative") or {}).get("id"):
            return a["creative"]["id"]
    return None


_pixel_mem = {}


def _pixel():
    if act() not in _pixel_mem:
        px = get_all(f"{act()}/adspixels", fields="id,name")
        if not px:
            raise ValueError("this ad account has no Meta pixel: pick a non-conversion optimisation goal")
        _pixel_mem[act()] = px[0]["id"]
    return _pixel_mem[act()]


def _apply_placements(tg, pl):
    """Merge placement settings into a targeting object. Advantage+ placements = no platform/position lists."""
    tg = json.loads(json.dumps(tg or {}))
    if pl.get("advantage", not pl.get("publisher_platforms")):
        for k in PLACEMENT_KEYS + ("device_platforms",):
            tg.pop(k, None)
        return tg
    for k in PLACEMENT_KEYS:
        tg.pop(k, None)
    tg["publisher_platforms"] = list(pl["publisher_platforms"])
    for k in PLACEMENT_KEYS[1:]:
        if pl.get(k):
            tg[k] = list(pl[k])
    return tg


def _wider(old_tg, new_tg):
    """Do the new placements reach places the old ones did not?"""
    if not new_tg.get("publisher_platforms"):
        return bool(old_tg.get("publisher_platforms"))
    if not old_tg.get("publisher_platforms"):
        return False
    if set(new_tg["publisher_platforms"]) - set(old_tg["publisher_platforms"]):
        return True
    for k in PLACEMENT_KEYS[1:]:
        if old_tg.get(k) and (not new_tg.get(k) or set(new_tg[k]) - set(old_tg[k])):
            return True
    return False


RECHECK = ("meta_audience", "meta_targeting", "meta_placements")  # [audiences] re-checked at preview / push
PHASE = {"audience": -1, "new_campaign": 0, "new_adset": 1, "new_ad": 2, "study": 4, "study_end": 5, "audience_delete": 6}  # edits: 3


def ops_for(c, items=None):
    """Draft change -> list of steps. Each step: {line, kind, ...} with what to do, how to check it and how to undo it.
    Steps that create objects carry "refs" ({param: draft item id}) that the push fills with ids made earlier in it."""
    t = c["type"]
    for k in _not_yet(c):  # never Active through a test, never a copy: refused here too, whatever the draft says
        raise ValueError(f"{k}: {NOT_YET[k]}")
    if t in TEST_TYPES:
        import meta_tests  # noqa: PLC0415
        steps = meta_tests.ops_for(c, items if items is not None else _items())
        for s in steps:
            s["needs"] = meta_tests.refs(c)
        return steps
    if t == meta_audiences.TYPE:  # [audiences] reads only here; nothing reaches Meta before Push live
        return meta_audiences.ops_for(c, sys.modules[__name__])
    if t == "meta_new_campaign":
        p = {"name": c["name"], "objective": c["objective"], "status": "PAUSED", "special_ad_categories": c.get("special_ad_categories") or []}
        if c.get("daily_budget"):
            p.update(daily_budget=int(round(float(c["daily_budget"]) * 100)), bid_strategy="LOWEST_COST_WITHOUT_CAP")
            b = f"campaign budget {float(c['daily_budget']):.2f}/day"
        else:
            p["is_adset_budget_sharing_enabled"] = False
            b = "budgets on the ad sets"
        return [{"kind": "create", "phase": "new_campaign", "edge": "campaigns", "params": p, "refs": {},
                 "line": f'new campaign "{c["name"]}" ({_st(c["objective"].replace("OUTCOME_", ""))}, {b}), created Paused',
                 "spend": f"new campaign budget {float(c['daily_budget']):.2f}/day (Paused until you switch it on)" if c.get("daily_budget") else ""}]
    if t == "meta_new_adset":
        items = items if items is not None else _items()
        d = load()
        cref = _ref(c["campaign_id"])
        if cref:
            camp = _camp_facts(c["campaign_id"], d, items)
        else:
            lc = _live("campaign", c["campaign_id"])
            camp = {"name": lc["name"], "objective": lc.get("objective"), "cbo": bool(lc.get("daily_budget") or lc.get("lifetime_budget"))}
        obj = camp["objective"]
        goal = c.get("optimization_goal") or GOALS.get(obj, ("LINK_CLICKS",))[0]
        tg = {"geo_locations": {"countries": c["countries"]}, "age_min": int(c.get("age_min", 18)), "age_max": int(c.get("age_max", 65)),
              "targeting_automation": {"advantage_audience": 1 if c.get("advantage_audience") else 0}}
        tg = _pin_device(_apply_placements(tg, c.get("placements") or {}))
        p = {"name": c["name"], "status": "PAUSED", "billing_event": "IMPRESSIONS", "optimization_goal": goal, "targeting": tg}
        if not cref:
            p["campaign_id"] = str(c["campaign_id"])
        if goal == "OFFSITE_CONVERSIONS":
            ev = c.get("conversion_event") or DEFAULT_EVENT.get(obj, "PURCHASE")
            px = c.get("pixel_id") or _pixel()
            p["promoted_object"] = ({"pixel_id": px, "custom_event_type": ev.upper()} if ev.upper() in STD_EVENTS
                                    else {"pixel_id": px, "custom_event_type": "OTHER", "custom_event_str": ev})
        elif goal == "LEAD_GENERATION":
            p["promoted_object"] = {"page_id": _page_for_account(d)}
        if c.get("start_date") and dt.date.fromisoformat(c["start_date"]) > dt.date.today():
            p["start_time"] = f"{c['start_date']}T00:00:00+0000"
        if c.get("end_date"):
            p["end_time"] = f"{c['end_date']}T23:59:00+0000"
        spend = ""
        if not camp["cbo"]:
            p.update(daily_budget=int(round(float(c["daily_budget"]) * 100)), bid_strategy="LOWEST_COST_WITHOUT_CAP")
            spend = f"new ad set budget {float(c['daily_budget']):.2f}/day (Paused until you switch it on)"
        line = (f'new ad set "{c["name"]}" in campaign "{camp["name"]}" ({_st(goal)}, '
                f'{"campaign budget" if camp["cbo"] else "daily " + format(float(c["daily_budget"]), ".2f")}, '
                f'{", ".join(c["countries"])}, age {tg["age_min"]}-{tg["age_max"]}, {_pl_text(c.get("placements"))}), created Paused')
        step = {"kind": "create", "phase": "new_adset", "edge": "adsets", "params": p, "refs": {"campaign_id": cref} if cref else {},
                "line": line, "spend": spend}
        if cref:  # Meta can only check an ad set against a campaign that exists: borrow a similar one for the check
            cats = sorted(items[cref]["change"].get("special_ad_categories") or [])
            live_camps = get_all(f"{act()}/campaigns", fields="id,name,objective,daily_budget,lifetime_budget,special_ad_categories",
                                 effective_status=["ACTIVE", "PAUSED"])
            twin = next((x for x in live_camps if x.get("objective") == obj and sorted(x.get("special_ad_categories") or []) == cats
                         and bool(x.get("daily_budget") or x.get("lifetime_budget")) == camp["cbo"]), None)
            if twin:
                step["check_params"] = {**p, "campaign_id": twin["id"]}
                step["line"] += f' (Meta checked it against campaign "{twin["name"]}" as a stand-in, as the new campaign does not exist yet)'
            else:
                step["check_params"] = None
                step["line"] += " (Meta can only check it once the new campaign exists, so at push)"
        return [step]
    if t == "meta_new_ad":
        src_cr = _live("ad", c["from_ad"])["creative"] if c.get("from_ad") else None
        aref = _ref(c["adset_id"])
        if aref:
            where = f'new ad set "{(items if items is not None else _items()).get(aref, {}).get("change", {}).get("name", c["adset_id"])}"'
        else:
            where = f"ad set {c['adset_id']}"
        base = {"kind": "new_ad", "phase": "new_ad", "adset": None if aref else str(c["adset_id"]), "adset_ref": aref, "name": c["name"]}
        if c.get("cards"):
            cr = _carousel_creative(c, c["name"] + " (Studio)")
            mids = [x["media_id"] for x in c["cards"]]
            have = [(media_info(m).get("uploads") or {}).get(act()) for m in mids]
            line = f'new carousel ad "{c["name"]}" (Paused) in {where} with {len(mids)} cards'
            check_creative = True
            if all(h and h.get("hash") for h in have):
                for ch, h in zip(cr["object_story_spec"]["link_data"]["child_attachments"], have):
                    ch["image_hash"] = h["hash"]
                line += " (images already in the ad account's media library)"
            else:
                stand_in = _stand_in_media("image")
                if stand_in:
                    for ch, h in zip(cr["object_story_spec"]["link_data"]["child_attachments"], have):
                        ch["image_hash"] = h["hash"] if h and h.get("hash") else stand_in["hash"]
                    line += ("; the images are uploaded to the ad account's media library at push (an upload can't be test-run, "
                             "so Meta checked the carousel with a stand-in image from the account)")
                else:
                    check_creative = False
                    line += "; the images are uploaded at push and Meta checks the creative then"
            return [{**base, "line": line, "creative": cr, "card_media": mids, "check_creative": check_creative,
                     "source_creative": None if aref else _any_creative(c["adset_id"])}]
        if not c.get("media_id"):
            return [{**base, "line": f'new ad "{c["name"]}" (Paused) in {where}, copy of ad {c["from_ad"]}',
                     "creative": _creative_params(src_cr, c, c["name"] + " (Studio)"), "source_creative": src_cr["id"]}]
        m = media_info(c["media_id"])
        have = (m.get("uploads") or {}).get(act())
        cr = _media_creative(c, src_cr, m["kind"], c["name"] + " (Studio)")
        slot = have if have and (m["kind"] == "image" or have.get("image_url") or have.get("image_hash")) else None
        line = f'new ad "{c["name"]}" (Paused) in {where} with {m["kind"]} "{m["name"]}"'
        check_creative = True
        if slot:
            _set_media(cr, m["kind"], slot)
            line += " (already in the ad account's media library)"
        else:
            stand_in = _stand_in_media(m["kind"])
            if stand_in:
                _set_media(cr, m["kind"], stand_in)
                line += (f"; the {m['kind']} is uploaded to the ad account's media library at push (an upload can't be test-run, "
                         f"so Meta checked the ad with a stand-in {m['kind']} from the account)")
            else:
                check_creative = False
                line += f"; the {m['kind']} is uploaded at push and Meta checks the creative then"
        if m["kind"] == "video" and not slot:
            line += ". Video: Studio waits for Meta to finish processing it (up to 10 minutes)"
        return [{**base, "line": line, "creative": cr, "media_id": c["media_id"], "media_kind": m["kind"], "check_creative": check_creative,
                 "source_creative": (src_cr or {}).get("id") or (None if aref else _any_creative(c["adset_id"]))}]
    if t == "meta_boost_post":
        aref = _ref(c["adset_id"])
        if aref:
            where = f'new ad set "{(items if items is not None else _items()).get(aref, {}).get("change", {}).get("name", c["adset_id"])}"'
        else:
            where = f"ad set {c['adset_id']}"
        if c["channel"] == "instagram":  # an existing Instagram post as the ad (Meta: source_instagram_media_id)
            ident = _identity(need_ig=True)
            if not ident.get("instagram_user_id"):
                raise ValueError("no Instagram account found on the ads in this ad account")
            cr = _pin_showcase({"name": c["name"][:100] + " (Studio boost)", "object_id": ident["page_id"],
                                "instagram_user_id": ident["instagram_user_id"], "source_instagram_media_id": c["post_id"]})
        else:  # an existing Page post as the ad (object_story_id): likes and comments stay on the post
            cr = _pin_showcase({"name": c["name"][:100] + " (Studio boost)", "object_story_id": c["post_id"]})
        src = "Instagram" if c["channel"] == "instagram" else "Facebook"
        return [{"kind": "new_ad", "phase": "new_ad", "adset": None if aref else str(c["adset_id"]), "adset_ref": aref, "name": c["name"],
                 "line": f'new ad "{c["name"]}" (Paused) in {where}, boosting {src} post {c["post_id"]}', "creative": cr,
                 "check_creative": True, "source_creative": None}]
    lvl, oid = c["level"], str(c["id"])
    cur = _live(lvl, oid)
    nm = cur.get("name", oid)
    if t == "meta_status":
        new = STATUS[c["status"]]
        if c.get("with_end"):  # paused together with End test: the test is over, so undo never switches it back on
            return [{"kind": "update", "id": oid, "params": {"status": new}, "undo": None, "permanent": True,
                     "final": "the test has ended; switch it on by hand if you want",
                     "line": f'{lvl} "{nm}": status {cur["status"].title()} -> {new.title()} (with End test; not undone by undo: '
                             "the test has ended, switch it on by hand if you want)"}]
        return [{"kind": "update", "id": oid, "params": {"status": new}, "undo": {"status": cur["status"]},
                 "line": f'{lvl} "{nm}": status {cur["status"].title()} -> {new.title()}',
                 "spend": f"activates {lvl}" if new == "ACTIVE" and cur["status"] != "ACTIVE" else ""}]
    if t == "meta_budget":
        key = "lifetime_budget" if c.get("lifetime") else "daily_budget"
        new = int(round(float(c["budget"]) * 100))
        old = cur.get(key)
        if not old:
            raise ValueError(f"{lvl} has no {key.replace('_', ' ')} to change")
        pct = (new - int(old)) / int(old) * 100
        return [{"kind": "update", "id": oid, "params": {key: new}, "undo": {key: int(old)}, "budget": True,
                 "line": f'{lvl} "{nm}": {key.replace("_", " ")} {_money_txt(old)} -> {_money_txt(new)} ({pct:+.0f}%)',
                 "spend": f"budget up {pct:.0f}%" if new > int(old) else ""}]
    if t == "meta_end":
        new = f"{c['end_date']}T23:59:00+0000" if c.get("end_date") else 0
        return [{"kind": "update", "id": oid, "params": {"end_time": new}, "undo": {"end_time": cur.get("end_time") or 0},
                 "line": f'ad set "{nm}": end {(cur.get("end_time") or "none")[:10]} -> {c.get("end_date") or "none"}',
                 "spend": "runs longer" if not c.get("end_date") or (cur.get("end_time") and c["end_date"] > cur["end_time"][:10]) else ""}]
    if t == "meta_rename":
        return [{"kind": "update", "id": oid, "params": {"name": c["new_name"]}, "undo": {"name": nm},
                 "line": f'{lvl} "{nm}": rename -> "{c["new_name"]}"'}]
    if t == "meta_targeting":
        tg = json.loads(json.dumps(cur.get("targeting") or {}))
        before = _targeting(tg)
        spend = []
        if c.get("countries"):
            geo = tg.setdefault("geo_locations", {})
            old = set(geo.get("countries") or [])
            geo["countries"] = [x.upper() for x in c["countries"]]
            if set(geo["countries"]) - old:
                spend.append("more countries")
        for k in ("age_min", "age_max"):
            if c.get(k):
                if (k == "age_min" and int(c[k]) < int(tg.get(k, 18))) or (k == "age_max" and int(c[k]) > int(tg.get(k, 65))):
                    spend.append("wider age")
                tg[k] = int(c[k])
        undo_wider = []
        if c.get("countries") and set((cur.get("targeting") or {}).get("geo_locations", {}).get("countries") or []) - {x.upper() for x in c["countries"]}:
            undo_wider.append("brings back countries")
        if (c.get("age_min") and int(c["age_min"]) > int((cur.get("targeting") or {}).get("age_min", 18))) or \
                (c.get("age_max") and int(c["age_max"]) < int((cur.get("targeting") or {}).get("age_max", 65))):
            undo_wider.append("widens the age range again")
        if any(c.get(k) for k in meta_audiences.AUD_KEYS):  # [audiences] include / exclude custom audiences
            tg, wider, narrower = meta_audiences.apply_targeting(tg, c, load())
            if wider:
                spend.append("wider audience (more people can see the ads)")
            if narrower:
                undo_wider.append("widens the audience again")
        after = _targeting(tg)
        aud = (f'; audiences {before["audiences"] or "none"} -> {after["audiences"] or "none"}; excluded {before["excluded"] or "none"} -> '
               f'{after["excluded"] or "none"}') if any(c.get(k) for k in meta_audiences.AUD_KEYS) else ""
        return [{"kind": "update", "id": oid, "params": {"targeting": _pin_device(tg)}, "undo": {"targeting": _pin_device(cur.get("targeting"))},
                 "line": f'ad set "{nm}": countries {before["countries"]} -> {after["countries"]}; age {before["age"]} -> {after["age"]}' + aud,
                 # conservative: undoing any targeting change puts the old targeting back, which can reach more people
                 "spend": ", ".join(spend), "undo_spend": ", ".join(undo_wider) or "puts the ad set's old targeting back (can reach more people)"}]
    if t == "meta_placements":
        old_tg = cur.get("targeting") or {}
        tg = _apply_placements(old_tg, c)
        return [{"kind": "update", "id": oid, "params": {"targeting": _pin_device(tg)}, "undo": {"targeting": _pin_device(old_tg)},
                 "line": f'ad set "{nm}": placements {_placements(old_tg)} -> {_placements(tg)}',
                 "spend": "more placements" if _wider(old_tg, tg) else "",
                 "undo_spend": "puts the ad set's old placements back (can reach more people)"}]
    if t in ("meta_copy", "meta_carousel_copy", "meta_feed_copy"):
        cr = cur["creative"]
        ct = creative_text(cr)
        if ct["edit_type"] != t or not cr.get("object_story_spec"):
            raise ValueError(ct["why_not"] or "this ad's creative can't be edited from Studio")
        if t == "meta_copy":
            diff = [f"{k}: {ct[k]!r} -> {c[k]!r}" for k in ("primary_text", "headline", "description", "link", "cta")
                    if c.get(k) is not None and c[k] != ct[k]]
        elif t == "meta_carousel_copy":
            diff = [f"{k}: {ct[k]!r} -> {c[k]!r}" for k in ("primary_text", "link") if c.get(k) is not None and c[k] != ct[k]]
            for x in c.get("cards") or []:
                old = ct["cards"][int(x["index"])]
                diff += [f"card {int(x['index']) + 1} {k}: {old[k]!r} -> {x[k]!r}" for k in ("name", "description", "link", "cta")
                         if x.get(k) is not None and x[k] != old[k]]
        else:
            diff = [f"{k}: {len(ct[k])} -> {len(c[k])} options" + ("" if ct[k] != c[k] else " (unchanged)")
                    for k in ("bodies", "titles", "descriptions", "link_urls") if c.get(k) is not None]
        return [{"kind": "copy", "id": oid, "creative": _creative_params(cr, c, f"{nm} (Studio {dt.date.today():%d %b})"),
                 "old_creative": cr["id"], "line": f'ad "{nm}": new copy, goes back to Meta review. ' + "; ".join(diff)}]
    raise ValueError(f"'{t}' can't be pushed live")


def _check_step(s):
    """Meta's own validate_only for one step. Returns an error string or ''."""
    try:
        if s["kind"] in ("study", "study_end"):  # reads only: /ad_studies has no validate_only (meta_tests.py)
            import meta_tests  # noqa: PLC0415
            return meta_tests.check_step(s)
        if s["kind"] in meta_audiences.STEP_KINDS:  # [audiences] no validate_only POST for audiences (not proven on Meta)
            return meta_audiences.check_step(s)
        if s["kind"] == "update":
            post(s["id"], s["params"], validate=True, retry_rate=False)
        elif s["kind"] == "create":
            params = s.get("check_params") if s.get("refs") else s["params"]
            if params:
                post(f"{act()}/{s['edge']}", params, validate=True, retry_rate=False)
        elif s["kind"] in ("copy", "new_ad"):
            if s.get("check_creative", True):
                post(f"{act()}/adcreatives", s["creative"], validate=True, retry_rate=False)
            if s["kind"] == "new_ad" and s.get("adset") and s.get("source_creative"):
                post(f"{act()}/ads", {"name": s["name"], "adset_id": s["adset"], "status": "PAUSED",
                                    "creative": {"creative_id": s["source_creative"]}}, validate=True, retry_rate=False)
        return ""
    except MetaError as ex:
        if ex.rate_limited:
            return ""  # Meta allows few budget calls per ad set; a busy answer is not a no - the push waits and retries
        return f"Meta: {ex}"


def _only(only):
    """The draft items in this push: all, or (team approvals / push by person) only the ids in `only`."""
    items = core.draft(acct())["items"]
    if only is None:
        return items
    return [i for i in items if (only.admits(i) if hasattr(only, "admits") else i["id"] in only)]


def _activate(s):
    """enable_new: a new ad is created Active (its ad set / campaign decide whether it delivers). Raises spend."""
    s["status"] = "ACTIVE"
    s["line"] = s["line"].replace("(Paused)", "(Active)", 1)
    s["spend"] = f'new ad "{s["name"]}" is created Active and starts delivering once Meta approves it'


def plan(tick=False, only=None, enable_new=False):
    items = _only(only)
    pool = {it["id"]: it for it in items}
    steps, per_item, touched = [], [], {}
    for it in items:
        entry = {"id": it["id"], "summary": it["summary"], "by": it["by"], "lines": [], "spend": [], "permanent": [], "problems": list(it["problems"])}
        rechecked = it["change"].get("type") in RECHECK
        if rechecked:  # [audiences] never trust stored problems: a synced or merged draft never went through add()
            try:
                c0 = _normalise(it["change"])
                entry["problems"] += [p for p in check(c0, load(), pool)[0] + meta_audiences.refusals([c0], load())
                                      if p not in entry["problems"]]
            except Exception as ex:  # noqa: BLE001 - a broken change is a refusal, never a crash
                entry["problems"].append(f"invalid change: {ex}")
        if not entry["problems"]:
            try:
                its = list(ops_for(it["change"], pool))
                if tick:
                    progress.totals(acct(), {it["id"]: len(its)})
                    progress.mark(acct(), it["id"], "checking")
                for s in its:
                    if enable_new and s.get("kind") == "new_ad":
                        _activate(s)
                    key = (s.get("id"), tuple(sorted(s.get("params", {}))))
                    if s["kind"] == "update" and key in touched:
                        raise ValueError(f"the same field is changed twice in this draft (also in: {touched[key]})")
                    if s["kind"] == "update":
                        touched[key] = it["summary"]
                    for ref in list((s.get("refs") or {}).values()) + ([s["adset_ref"]] if s.get("adset_ref") else []) + list(s.get("needs") or []):
                        if ref not in pool:
                            raise ValueError("it belongs to a new campaign / ad set that is no longer in the draft (or not in this push)")
                    err = _check_step(s)
                    if err:
                        entry["problems"].append(err)
                    if tick:
                        progress.checked(acct(), it["id"])
                    s["item"] = it["id"]
                    steps.append(s)
                    entry["lines"].append(s["line"])
                    if s.get("spend"):
                        entry["spend"].append(s["spend"])
                    if s.get("undo_spend"):  # its undo, or a rollback after a failed push, can widen targeting again
                        entry["spend"].append(f"undo or rollback: {s['undo_spend']}")
                    if s.get("permanent"):
                        entry["permanent"].append(s["line"])
            except (MetaError, ValueError, KeyError) as ex:
                entry["problems"].append(str(ex))
            except Exception as ex:  # noqa: BLE001
                if not rechecked:
                    raise
                entry["problems"].append(f"invalid change: {ex}")
        per_item.append(entry)
    steps.sort(key=lambda s: PHASE.get(s.get("phase"), 3))  # campaign -> ad set -> ad -> edits; stable within a phase
    return {"items": per_item, "steps": steps, "ok": bool(steps) and not any(e["problems"] for e in per_item),
            "spend": any(e["spend"] for e in per_item), "permanent": any(e["permanent"] for e in per_item), "channel": "meta"}


def preview(enable_new=False, only=None):
    p = plan(only=only, enable_new=enable_new)
    return {k: v for k, v in p.items() if k != "steps"}


def _log():
    core.STUDIO_DIR.mkdir(parents=True, exist_ok=True)
    return core.STUDIO_DIR / f"live_{acct()}.jsonl"


def _do(s, ctx=None):
    """Apply one step for real. Returns the record needed to undo it. ctx: {draft item id: Meta id} made in this push."""
    ctx = {} if ctx is None else ctx
    if s["kind"] == "update":
        post(s["id"], s["params"])
        return {"line": s["line"], "id": s["id"], "undo": s["undo"], **({"final": s["final"]} if s.get("final") else {}),
                **({"undo_spend": s["undo_spend"]} if s.get("undo_spend") else {})}
    if s["kind"] == "copy":
        new = post(f"{act()}/adcreatives", s["creative"])["id"]
        post(s["id"], {"creative": {"creative_id": new}})
        return {"line": s["line"], "id": s["id"], "undo": {"creative": {"creative_id": s["old_creative"]}}, "created_creative": new}
    if s["kind"] == "create":
        params = dict(s["params"])
        for field, ref in (s.get("refs") or {}).items():
            params[field] = ctx[ref]
        new = post(f"{act()}/{s['edge']}", params)["id"]
        ctx[s["item"]] = new
        # created by Studio in this push: undo / rollback sets it to DELETED (never done to anything that existed before)
        return {"line": s["line"], "id": new, "undo": {"status": "DELETED"}, "created": s["edge"][:-1],
                "parent": params.get("campaign_id")}
    if s["kind"] == "new_ad":
        creative = json.loads(json.dumps(s["creative"]))
        rec = {}
        if s.get("media_id"):
            fresh = act() not in (media_info(s["media_id"]).get("uploads") or {})
            ref = upload_media(s["media_id"])
            ctx["__uploaded"] = ctx.get("__uploaded") or fresh
            _set_media(creative, s["media_kind"], ref)
            rec["uploaded"] = {"media_id": s["media_id"], **ref}
        if s.get("card_media"):
            kids = creative["object_story_spec"]["link_data"]["child_attachments"]
            ups = []
            for ch, mid in zip(kids, s["card_media"]):
                fresh = act() not in (media_info(mid).get("uploads") or {})
                ref = upload_media(mid)
                ctx["__uploaded"] = ctx.get("__uploaded") or fresh
                ch["image_hash"] = ref["hash"]
                ch.pop("picture", None)
                ups.append({"media_id": mid, **ref})
            rec["uploaded_cards"] = ups
        adset = ctx[s["adset_ref"]] if s.get("adset_ref") else s["adset"]
        new_cr = post(f"{act()}/adcreatives", creative)["id"]
        ad = post(f"{act()}/ads", {"name": s["name"], "adset_id": adset, "status": s.get("status", "PAUSED"),
                                   "creative": {"creative_id": new_cr}})["id"]
        if s.get("item"):
            ctx[s["item"]] = ad  # a creative test in this push can point at it
        return {"line": s["line"], "id": ad, "undo": {"status": "DELETED"}, "created": "ad", "created_ad": ad, "created_creative": new_cr,
                "parent": adset, **rec}
    if s["kind"] in ("study", "study_end"):
        import meta_tests  # noqa: PLC0415
        return meta_tests.do(s, ctx)
    if s["kind"] in meta_audiences.STEP_KINDS:
        return meta_audiences.do(s, sys.modules[__name__])
    raise ValueError(s["kind"])


def _keep(done):
    """After a refusal: (kept, to reverse). A test made in this push can't be removed, so it stays, and so does every
    object it uses: its cells' ad sets / campaigns / ads, the new campaign above a used ad set, the new ads inside a used
    ad set or campaign. Everything else made in the push is reversed as usual."""
    used = {str(x) for r in done if r.get("created") == "study" for x in r.get("uses") or []}
    made = {str(r["id"]) for r in done if r.get("created")}
    keep = {id(r) for r in done if r.get("undo") is None}  # tests made or ended: final
    grew = True
    while grew:
        grew = False
        for r in done:
            if id(r) in keep or not r.get("created"):
                continue
            rid, parent = str(r["id"]), str(r.get("parent") or "")
            if rid in used or parent in used:
                keep.add(id(r))
                for x in (rid, parent if parent in made else ""):  # an existing parent does not pull in its other children
                    if x and x not in used:
                        used.add(x)
                grew = True
    return [r for r in done if id(r) in keep], [r for r in done if id(r) not in keep]


def _reverse(done, tick=None):
    """tick(record, ok) after each reversal (for the progress list)."""
    errors = []
    for r in reversed(done):
        if r.get("undo") is None:  # a test made or ended on Meta: nothing can reverse it (meta_tests.py)
            errors.append(f"{r['line']}: {r.get('final') or 'this can not be undone'}")
            if tick:
                tick(r, False)
            continue
        try:
            if r["undo"] == "delete-audience":  # [audiences] an audience Studio made in that push
                delete(r["id"])
            else:
                post(r["id"], r["undo"])
            ok = True
        except MetaError as ex:
            ok = False
            if r["undo"] == {"status": "DELETED"}:  # can't delete it: at least stop it spending
                try:
                    post(r["id"], {"status": "PAUSED"})
                    errors.append(f"{r['line']}: could not delete it ({ex}), so it was set to Paused instead; delete it in Ads Manager")
                except MetaError as ex2:
                    errors.append(f"{r['line']}: could not delete it ({ex}) or pause it ({ex2}); check it in Ads Manager now")
            else:
                errors.append(f"{r['line']}: {ex}")
        if tick:
            tick(r, ok)
    return errors


def push(enable_new=False, accept_spend=False, accept_permanent=False, only=None):
    a = acct()
    progress.start(a, "push", [(i["id"], i["summary"], 1) for i in _only(only)], "checking", "Meta is checking each change")
    res = None
    try:
        res = _push(enable_new, accept_spend, accept_permanent, only)
        return res
    finally:
        progress.finish(a, bool(res and res.get("ok")), (res or {}).get("message", "Stopped with an error"))


def _push(enable_new=False, accept_spend=False, accept_permanent=False, only=None):
    p = plan(tick=True, only=only, enable_new=enable_new)
    for e in p["items"]:
        if e["problems"]:
            progress.mark(acct(), e["id"], "failed")
    pub = {k: v for k, v in p.items() if k != "steps"}
    if not p["ok"]:
        return {"ok": False, "message": "Not pushed - fix the problems in the preview first.", "preview": pub}
    if p["spend"] and not accept_spend:
        return {"ok": False, "message": "Not pushed - this push can raise spend; tick the box to accept.", "preview": pub}
    if p["permanent"] and not accept_permanent:
        return {"ok": False, "message": "Not pushed - this push has changes that can't be undone (tests); tick the box to accept.",
                "preview": pub}
    done, error, ctx = [], None, {}
    progress.phase(acct(), "sending", "Sending to Meta, one change at a time")
    for s in p["steps"]:
        if s.get("media_id") or s.get("card_media"):
            progress.phase(acct(), "sending", "Uploading to Meta. A video can take several minutes while Meta processes it")
        try:
            rec_ = _do(s, ctx)
            rec_["item"] = s["item"]
            done.append(rec_)
            progress.sent(acct(), s["item"])
        except (MetaError, KeyError, ValueError, OSError) as ex:
            error = f"{s['line']}: {ex}"
            progress.mark(acct(), s["item"], "failed")
            break
    stamp = dt.datetime.now()
    items = [i for i in core.draft(acct())["items"] if i["id"] in {s["item"] for s in p["steps"]}]
    kept, back = _keep(done) if error else (done, [])
    if error and back:
        progress.phase(acct(), "reversing", "Meta refused a change: reversing the ones already made")
    rolled = _reverse(back, lambda r, ok: progress.mark(acct(), r.get("item"), "reversed")) if error else []
    kept_items = {r.get("item") for r in kept}
    rec = {"id": f"{stamp:%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}", "ts": stamp.isoformat(timespec="seconds"), "ok": not error,
           "partial": bool(error and kept), "items": [i for i in items if i["id"] in kept_items] if error else items,
           "ops": kept, "error": error, "rolled_back": [d["line"] for d in back], "rollback_errors": rolled}
    with open(_log(), "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    if not error:
        core.remove(acct(), [i["id"] for i in items])
    elif kept:  # what is live now is no longer a draft (pushing it again would make it twice)
        core.remove(acct(), [i for i in kept_items if i])
    try:
        refresh_account()  # only this ad account
    except MetaError:
        pass
    uploaded = ctx.get("__uploaded")
    lib = " Uploaded images and videos stay in the ad account's media library (also after an undo)." if uploaded else ""
    if error and kept:
        msg = (f"Meta refused a change: {error}. Kept on Meta: {len(kept)} change(s), because a test made in this push "
               f"can't be removed and keeps what it uses: {'; '.join(r['line'] for r in kept)}.")
        if back:
            msg += f" Reversed: {len(back) - len(rolled)} of {len(back)} other change(s)" + \
                (f"; these could not be reversed: {'; '.join(rolled)}" if rolled else "") + "."
        return {"ok": False, "partial": True, "push_id": rec["id"], "kept": len(kept), "message": msg + lib, "errors": None}
    if error:
        msg = (f"Meta refused a change, so nothing was kept: {error}" if not rolled
               else f"Meta refused a change: {error}. Not everything made before it could be removed")
        if done:
            msg += f" ({len(done) - len(rolled)} change(s) already made were reversed" + (f"; these could not be reversed: {'; '.join(rolled)}" if rolled else "") + ")"
        return {"ok": False, "push_id": rec["id"], "message": msg + lib, "errors": None}
    return {"ok": True, "push_id": rec["id"], "applied": len(done), "message": f"Live on Meta: {len(done)} change(s).{lib}", "errors": None}


def history(limit=20):
    f = _log()
    if not f.exists():
        return []
    recs = [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
    undone = {r["undo_of"] for r in recs if r.get("undo_of")}
    out = []
    for r in reversed(recs):
        out.append({"id": r["id"], "ts": r["ts"], "ok": r["ok"], "undo_of": r.get("undo_of"), "undone": r["id"] in undone,
                    "summaries": [i["summary"] for i in r.get("items", [])] + ([f"Failed: {r['error']}"] if r.get("error") else [])
                    + ([f"Kept on Meta: {len(r['ops'])} change(s) a test uses"] if r.get("partial") else []),
                    "lines": [o["line"] for o in r.get("ops", [])], "partial": bool(r.get("partial")),
                    "undo_spend": undo_spend(r.get("ops") or []),
                    "can_undo": (r["ok"] or bool(r.get("partial"))) and not r.get("undo_of") and r["id"] not in undone
                    and any(o.get("undo") is not None for o in r.get("ops") or [])})
    return out[:limit]


def undo_spend(ops):
    """Undo steps that would switch something back on (status ACTIVE) or widen targeting again: they can raise spend, like Push live."""
    return [o["line"] for o in ops if (isinstance(o.get("undo"), dict) and str(o["undo"].get("status") or "").upper() == "ACTIVE")
            or o.get("undo_spend")]


def undo(push_id, accept_spend=False):
    recs = [json.loads(l) for l in _log().read_text().splitlines() if l.strip()]
    rec = next((r for r in recs if r["id"] == push_id), None)
    if not rec or not (rec["ok"] or rec.get("partial")):
        raise ValueError("push not found")
    if any(r.get("undo_of") == push_id for r in recs):
        raise ValueError("already undone")
    a = acct()
    ops = rec["ops"]
    on = undo_spend(ops)
    if on and not accept_spend:  # nothing is sent: the UI asks, then calls again with accept_spend
        return {"ok": False, "needs_spend": True, "spend": on, "not_undone": [], "errors": None,
                "message": f"Not undone - this undo can raise spend ({len(on)} change(s): switched back on or wider targeting); accept to undo."}
    refused = meta_audiences.delete_refusal(ops, sys.modules[__name__])  # [audiences] never delete an audience an ad set now uses
    if refused:
        return {"ok": False, "message": refused, "not_undone": [refused], "errors": None}
    progress.start(a, "undo", [(n, "Undo: " + o["line"], 1) for n, o in enumerate(ops)], "sending", "Reversing on Meta, one change at a time")
    idx = {id(o): n for n, o in enumerate(ops)}
    try:
        errors = _reverse(ops, lambda r, ok: progress.sent(a, idx[id(r)]) if ok else progress.mark(a, idx[id(r)], "failed"))
    except Exception:
        progress.finish(a, False, "Stopped with an error")
        raise
    progress.finish(a, not errors, "Undone" if not errors else "Partly undone")
    stamp = dt.datetime.now()
    with open(_log(), "a") as f:
        f.write(json.dumps({"id": f"{stamp:%Y%m%d-%H%M%S}-undo", "ts": stamp.isoformat(timespec="seconds"), "ok": not errors,
                            "undo_of": push_id, "items": [], "ops": [], "error": "; ".join(errors) or None}, ensure_ascii=False) + "\n")
    try:
        refresh_account()  # only this ad account
    except MetaError:
        pass
    n = len(rec["ops"]) - len(errors)
    return {"ok": not errors, "message": f"Undone: {n} change(s) reversed." if not errors else f"Partly undone ({n} of {len(rec['ops'])}).",
            "not_undone": errors, "errors": None}


def last_push():
    h = [r for r in history(200) if r["ok"]]
    return h[0]["ts"] if h else None


CHANGE_REFERENCE = """Meta (account "meta") draft changes. ids are Meta ids from list_entities (kinds campaigns, adsets, ads, audiences, leadforms).
  {"type":"meta_status","level":"campaign|adset|ad","id":"120..","status":"active|paused"}
  {"type":"meta_budget","level":"campaign|adset","id":"..","budget":40.0}          (account currency, daily; "lifetime":true for lifetime)
     CBO campaigns: change the campaign; otherwise the ad set.
  {"type":"meta_end","level":"adset","id":"..","end_date":"2026-12-31"}             (null = no end)
  {"type":"meta_rename","level":"campaign|adset|ad","id":"..","new_name":".."}
  {"type":"meta_targeting","level":"adset","id":"..","countries":["GB","DE"],"age_min":25,"age_max":65}  (replaces the country list)
  {"type":"meta_placements","level":"adset","id":"..","advantage":true}             (Advantage+ placements: Meta picks)
  {"type":"meta_placements","level":"adset","id":"..","advantage":false,"publisher_platforms":["facebook","instagram"],
   "facebook_positions":["feed","story"],"instagram_positions":["stream","story","reels"]}   (merged into the ad set's targeting)
  {"type":"meta_copy","level":"ad","id":"..","primary_text":"..","headline":"..","description":"..","link":"https://..","cta":"LEARN_MORE"}
     (only the fields given change; single image/video ads (edit_type meta_copy); the ad goes back to Meta review)
  {"type":"meta_carousel_copy","level":"ad","id":"..","primary_text":"..","link":"..",
   "cards":[{"index":0,"name":"..","description":"..","link":"..","cta":".."}]}   (carousel ads, edit_type meta_carousel_copy;
     index is 0-based from the ad's "cards"; only the cards and fields given change; images stay)
  {"type":"meta_feed_copy","level":"ad","id":"..","bodies":[".."],"titles":[".."],"descriptions":[".."],"link_urls":["https://.."]}
     (flexible / Advantage+ creative, edit_type meta_feed_copy; each list given replaces that list; up to 5 options)
  {"type":"meta_new_campaign","ref":"c1","name":"..","objective":"traffic|awareness|engagement|leads|sales",
   "daily_budget":50.0,"special_ad_categories":[]}     (daily_budget = campaign budget (CBO), leave it out for ad set budgets;
     job ads need ["EMPLOYMENT"]. Always created Paused, lowest-cost bidding)
  {"type":"meta_new_adset","ref":"s1","campaign_id":"<id> or d-c1","name":"..","daily_budget":20.0,"countries":["GB"],
   "age_min":25,"age_max":65,"start_date":"2026-12-01","end_date":"2027-01-31","optimization_goal":"(optional)",
   "conversion_event":"PURCHASE|LEAD|<custom event>","advantage_audience":false,"placements":{"advantage":true}}
     (daily_budget only when the campaign has no campaign budget. Defaults per objective: traffic LANDING_PAGE_VIEWS,
      awareness REACH, engagement POST_ENGAGEMENT, leads OFFSITE_CONVERSIONS (pixel, LEAD), sales OFFSITE_CONVERSIONS
      (pixel, PURCHASE); billing IMPRESSIONS. Always created Paused)
  {"type":"meta_new_ad","adset_id":"<id> or d-s1","name":"..","from_ad":"<ad id to copy image/video and texts from>",
   "media_id":"<from an upload, instead of or as well as from_ad>","primary_text":"..","headline":"..","description":"..",
   "link":"..","cta":".."}   (always created Paused. With media_id and no from_ad, link, primary_text and headline are needed.
     The file is uploaded to the ad account's media library only at Push live; undo deletes the ad, the library file stays)
  {"type":"meta_new_ad","adset_id":"<id> or d-s1","name":"..","primary_text":"..","link":"<see more link>","cta":"..",
   "cards":[{"media_id":"<uploaded image>","name":"<card headline>","description":"..","link":"..","cta":"(optional)"}, ..]}
     (a new carousel: 2-10 cards, each an uploaded image; no from_ad / media_id. Cards keep their order and Meta adds
      no end card. Images are uploaded at Push live. Always created Paused)
  "ref" names a new campaign / ad set inside one add_to_draft call; "d-<ref or draft item id>" points at it.
  Push order: new campaigns, then new ad sets, then new ads, then edits. Rollback and undo set to DELETED only what that
  push created; anything that existed before only gets its old values back.
  {"type":"meta_targeting","level":"adset","id":"..","audiences_add":["<audience id>"],"audiences_remove":[..],
   "excluded_add":[..],"excluded_remove":[..]}   (include / exclude custom audiences on an ad set; leaving one out widens reach)
Audiences (meta_audience; created / changed on Meta only at Push live, never checked on Meta before it; see audience_add_to_draft):
  {"type":"meta_audience","action":"create","kind":"website","name":"..","retention_days":30 (1-180),
   "rules":[{"field":"url","op":"contains|equals|not_contains","value":"/egypt"},{"field":"event","value":"SelectCabin"}],
   "match":"any|all","exclude":[..],"prefill":true}
  {"type":"meta_audience","action":"create","kind":"engagement","name":"..","source":"page|instagram|lead_form",
   "event":"page: engaged|visited|messaged|cta|saved|post; instagram: engaged|all|visited|messaged;
   lead_form: opened|not_submitted|submitted","retention_days":365 (page / instagram 1-730, lead_form 1-90),
   "form_ids":[..] (lead_form: lead form ids)}   (video viewer audiences: not available yet)
  {"type":"meta_audience","action":"create","kind":"lookalike","name":"..","seed_id":"<audience id>","ratio":1 (1-10 %)}
     or "from":1,"to":3 for a 1-3% band. Never a country: the ad set's location decides. Seed needs 100+ people.
  {"type":"meta_audience","action":"edit","id":"..","name":"..","description":".."}   (name and description only: rules,
     retention and sources can't be changed on an existing audience; create a new one)
  {"type":"meta_audience","action":"remove","id":".."}   (final; refused while an ad set or lookalike uses it)
Customer lists (subtype CUSTOM) and lead forms are read only. Personal data: UK GDPR check needed before any upload/download.
  Studio never uploads customer lists and never downloads leads.
Rules: primary text ends with a blank line + the link; no commas in prices; no em dash, Bahamas, cabins, yacht,
guarantees, Expeditions, emojis; prices and % in the copy must be on the linked page; dead links block."""


PREVIEW_FORMATS = {"facebook": "MOBILE_FEED_STANDARD", "instagram": "INSTAGRAM_STANDARD", "stories": "INSTAGRAM_STORY",
                   "desktop": "DESKTOP_FEED_STANDARD", "reels": "INSTAGRAM_REELS"}


# Previews are cached on disk per ad + creative + placement: a new creative (copy change) gets a new key, so a cached
# preview never shows old copy. Meta's iframe links carry a short-lived token, so entries expire after PREVIEW_TTL.
PREVIEW_TTL = 6 * 3600
PREVIEW_FILE = core.STUDIO_DIR / "meta_preview_cache.json"
_pv_lock = threading.Lock()
_pv = None


def _pv_load():
    global _pv
    if _pv is None:
        try:
            _pv = json.loads(PREVIEW_FILE.read_text())
        except (OSError, ValueError):
            _pv = {}
    return _pv


def ad_preview(ad_id, placement="facebook", creative_id=None):
    """Meta's own rendering of the ad (an iframe from Meta), as people see it. Read-only. Cached (see PREVIEW_TTL)."""
    fmt = PREVIEW_FORMATS.get(placement, "MOBILE_FEED_STANDARD")
    key = f"{ad_id}|{creative_id or ''}|{fmt}"
    now = time.time()
    with _pv_lock:
        hit = _pv_load().get(key)
    if hit and now - hit["at"] < PREVIEW_TTL:
        return {"placement": placement, "html": hit["html"], "cached": True}
    data = get(f"{ad_id}/previews", ad_format=fmt).get("data") or []
    out = data[0]["body"] if data else ""
    if out:
        with _pv_lock:
            pv = _pv_load()
            pv[key] = {"at": now, "html": out}
            for k in [k for k, v in pv.items() if now - v["at"] >= PREVIEW_TTL]:
                del pv[k]
            try:
                core.STUDIO_DIR.mkdir(parents=True, exist_ok=True)
                tmp = PREVIEW_FILE.with_suffix(".tmp")
                tmp.write_text(json.dumps(pv))
                tmp.replace(PREVIEW_FILE)
            except OSError:
                pass
    return {"placement": placement, "html": out}


# ---------- media: stored locally first, uploaded to Meta only by Push live ----------

MEDIA_DIR = core.STUDIO_DIR / "meta_media"
MEDIA_TYPES = {"jpg": ("image", "image/jpeg"), "png": ("image", "image/png"), "mp4": ("video", "video/mp4"), "mov": ("video", "video/quicktime")}
MEDIA_MAX = {"image": 30 * 1024 * 1024, "video": 250 * 1024 * 1024}
VIDEO_WAIT = 600  # seconds Studio waits for Meta to finish processing an uploaded video
_media_lock = threading.Lock()


def _media_index():
    try:
        return json.loads((MEDIA_DIR / "index.json").read_text())
    except (OSError, ValueError):
        return {}


def _media_save(fn):
    """Read-modify-write the media index under a lock (server and MCP are separate processes)."""
    import fcntl
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    with _media_lock, open(MEDIA_DIR / ".index.lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        idx = _media_index()
        out = fn(idx)
        tmp = MEDIA_DIR / "index.tmp"
        tmp.write_text(json.dumps(idx, indent=1, ensure_ascii=False))
        tmp.replace(MEDIA_DIR / "index.json")
        return out


def _sniff(ext, data):
    if ext == "jpg":
        return data[:3] == b"\xff\xd8\xff"
    if ext == "png":
        return data[:8] == b"\x89PNG\r\n\x1a\n"
    return data[4:8] in (b"ftyp", b"moov", b"wide", b"mdat", b"free", b"skip")


def store_media(filename, data):
    """Keep an image (jpg/png, up to 30 MB) or video (mp4/mov, up to 250 MB) on this Mac for a new ad. Nothing goes to
    Meta here: the file is uploaded to the ad account by Push live. data: bytes, or a base64 string.
    -> {media_id, kind, name, size}. Use media_id in a meta_new_ad change."""
    if isinstance(data, str):
        try:
            data = base64.b64decode(data.split(",", 1)[1] if data.startswith("data:") else data, validate=True)
        except ValueError:
            raise ValueError("the file is not valid base64") from None
    name = Path(str(filename or "upload")).name[:120]
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    ext = {"jpeg": "jpg"}.get(ext, ext)
    if ext not in MEDIA_TYPES:
        raise ValueError("only .jpg, .png, .mp4 and .mov files")
    kind = MEDIA_TYPES[ext][0]
    if not data:
        raise ValueError("the file is empty")
    if len(data) > MEDIA_MAX[kind]:
        raise ValueError(f"{kind}s can be up to {MEDIA_MAX[kind] // (1024 * 1024)} MB")
    if not _sniff(ext, data):
        raise ValueError(f"the file does not look like a .{ext}")
    sha = hashlib.sha256(data).hexdigest()
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    path = MEDIA_DIR / f"{sha}.{ext}"
    if not path.exists():
        tmp = MEDIA_DIR / f"{sha}.part"
        tmp.write_bytes(data)
        tmp.replace(path)

    def upd(idx):
        e = idx.setdefault(sha, {"uploads": {}, "stored": dt.datetime.now().isoformat(timespec="seconds")})
        e.update(name=name, ext=ext, kind=kind, size=len(data), file=path.name)
        return e
    _media_save(upd)
    return {"media_id": sha, "kind": kind, "name": name, "size": len(data)}


def media_info(media_id):
    if not re.fullmatch(r"[0-9a-f]{64}", str(media_id or "")):
        raise ValueError("media_id is not valid")
    m = _media_index().get(media_id)
    if not m or not (MEDIA_DIR / m["file"]).exists():
        raise ValueError("media_id not found: upload the file again")
    return m


def media_file(media_id):
    """-> (path, content type) of a stored file, so the UI can show it before it is on Meta."""
    m = media_info(media_id)
    return MEDIA_DIR / m["file"], MEDIA_TYPES[m["ext"]][1]


def list_media():
    return [{"media_id": k, "name": v["name"], "kind": v["kind"], "size": v["size"], "stored": v.get("stored"),
             "on_meta": act() in (v.get("uploads") or {})} for k, v in sorted(_media_index().items(), key=lambda kv: kv[1].get("stored", ""), reverse=True) if v.get("source") != "social"]


def _post_file(edge, field, path, params=None, timeout=900):
    """multipart/form-data POST of one file (Meta's advideos upload). Never retried: the upload may have gone through."""
    boundary = uuid.uuid4().hex
    parts = []
    for k, v in {**(params or {}), "access_token": meta_api.TOKEN}.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{Path(path).name}"\r\n'
                 f'Content-Type: application/octet-stream\r\n\r\n'.encode() + Path(path).read_bytes() + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(f"{meta_api.API}/{edge}", data=b"".join(parts), method="POST",
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read())["error"]
        except (ValueError, KeyError, TypeError, OSError):
            err = {"message": f"HTTP {e.code}"}
        raise MetaError(err, status=e.code) from None
    except (urllib.error.URLError, OSError) as e:
        raise MetaError({"message": f"no answer from Meta ({getattr(e, 'reason', None) or e.__class__.__name__}); "
                                    "the upload may or may not have gone through"}) from None


def _wait_video(video_id, limit=None):
    """Wait (bounded) until Meta has processed an uploaded video. -> a thumbnail URL for the creative."""
    limit = VIDEO_WAIT if limit is None else limit
    t0 = time.time()
    while True:
        st = (get(str(video_id), fields="status").get("status") or {})
        vs = st.get("video_status")
        if vs == "ready":
            break
        if vs == "error":
            raise MetaError({"message": f"Meta could not process the video ({(st.get('processing_progress') or '')})".replace(" ()", "")})
        if time.time() - t0 > limit:
            raise MetaError({"message": f"the video was still processing after {limit // 60} minutes: nothing was kept, "
                                        "push again in a while (the upload is reused)"})
        time.sleep(10)
    th = get(f"{video_id}/thumbnails", fields="uri,is_preferred").get("data") or []
    pick = next((x for x in th if x.get("is_preferred")), th[0] if th else None)
    if not pick:
        raise MetaError({"message": "Meta made no thumbnail for the video"})
    return pick["uri"]


def upload_media(media_id):
    """Push-time only: put a stored file into the ad account's media library. -> {"hash"} or {"video_id", "image_url"}.
    An earlier upload to the same ad account is reused. Library media are not deleted by undo."""
    m = media_info(media_id)
    ref = dict((m.get("uploads") or {}).get(act()) or {})
    if ref.get("hash") or (ref.get("video_id") and ref.get("image_url")):
        return ref
    path = MEDIA_DIR / m["file"]
    if m["kind"] == "image":
        r = post(f"{act()}/adimages", {"bytes": base64.b64encode(path.read_bytes()).decode(), "name": m["name"]})
        img = next(iter((r.get("images") or {}).values()), None)
        if not img or not img.get("hash"):
            raise MetaError({"message": "Meta did not return an image hash for the upload"})
        ref = {"hash": img["hash"]}
    else:
        if not ref.get("video_id"):
            ref = {"video_id": _post_file(f"{act()}/advideos", "source", path, {"name": m["name"]})["id"]}
            _remember_upload(media_id, ref)  # keep the id even if processing times out, so the next push reuses it
        ref["image_url"] = _wait_video(ref["video_id"])
    _remember_upload(media_id, ref)
    return ref


def _remember_upload(media_id, ref):
    a = act()

    def upd(idx):
        idx[media_id].setdefault("uploads", {})[a] = ref
    _media_save(upd)


# ---------- preview of drafted copy (Meta renders it, nothing is created) ----------

def draft_preview(ad_id=None, placement="facebook", overrides=None, draft_id=None):
    """Meta's rendering of DRAFTED copy, via act_<id>/generatepreviews (read-only: nothing is created on Meta).
    - ad_id + overrides: an existing ad with these fields changed (meta_copy fields, "cards" for a carousel,
      bodies/titles/descriptions/link_urls for flexible creative). overrides may carry "type".
    - draft_id (or ad_id "d-<draft item id>"): a meta_copy / meta_carousel_copy / meta_feed_copy / meta_new_ad in the draft."""
    fmt = PREVIEW_FORMATS.get(placement, "MOBILE_FEED_STANDARD")
    if not draft_id and _ref(ad_id):
        draft_id = _ref(ad_id)
    if draft_id:
        it = _items().get(draft_id)
        if not it:
            raise ValueError("draft item not found")
        c = dict(it["change"])
    else:
        ov = dict(overrides or {})
        o = _find(load(), "ad", ad_id)
        c = {"type": ov.pop("type", None) or creative_text(o.get("creative"))["edit_type"] or "meta_copy", "level": "ad", "id": str(ad_id), **ov}
    t = c.get("type")
    if t not in ("meta_copy", "meta_carousel_copy", "meta_feed_copy", "meta_new_ad"):
        raise ValueError("only copy changes and new ads have a draft preview")
    step = ops_for(c)[0]
    out = {"placement": placement, "draft": True, "html": "",
           "note": "Meta's rendering of the drafted copy. Nothing is created and nothing is live until you push."}
    if t == "meta_new_ad" and c.get("cards"):
        pending = [x["media_id"] for x in c["cards"] if act() not in (media_info(x["media_id"]).get("uploads") or {})]
        if pending:
            return {**out, "media_id": c["cards"][0]["media_id"], "media_kind": "image", "cards": c["cards"],
                    "note": "The card images are only uploaded to Meta at push, so Meta can't render this carousel yet. "
                            "Showing the text with the first card's image from this Mac.",
                    **{k: c.get(k) for k in ("primary_text", "headline", "description", "link", "cta")}}
    if t == "meta_new_ad" and c.get("media_id"):
        m = media_info(c["media_id"])
        if act() not in (m.get("uploads") or {}):
            return {**out, "media_id": c["media_id"], "media_kind": m["kind"],
                    "note": f"The {m['kind']} is only uploaded to Meta at push, so Meta can't render this ad yet. "
                            "Showing the text with the file from this Mac.",
                    **{k: c.get(k) for k in ("primary_text", "headline", "description", "link", "cta")}}
    spec = {k: v for k, v in step["creative"].items() if k in ("object_story_spec", "asset_feed_spec", "url_tags")}
    data = get(f"{act()}/generatepreviews", creative=spec, ad_format=fmt).get("data") or []
    return {**out, "html": data[0]["body"] if data else ""}


def visible_accounts():
    """Ad accounts the token can reach, so Settings can offer them."""
    have = {a["act"] for a in settings()["meta_accounts"]}
    return [{"act": a["id"], "name": a.get("name", ""), "currency": a.get("currency", ""), "added": a["id"] in have,
             "active": a.get("account_status") == 1}
            for a in get_all("me/adaccounts", fields="name,account_id,currency,account_status")]


def add_account(act_id, name=None):
    act_id = str(act_id).strip()
    if not act_id.startswith("act_"):
        act_id = "act_" + re.sub(r"\D", "", act_id)
    info = get(act_id, fields="name,currency,account_status")  # proves the token can reach it
    s = settings()
    if any(a["act"] == act_id for a in s["meta_accounts"]):
        raise ValueError(f"{act_id} is already in Studio")
    s["meta_accounts"].append({"act": act_id, "name": (name or "").strip() or f"{info.get('name', act_id)} (Meta)"})
    save_settings(s)
    return {"act": act_id, "key": key_for(act_id), "name": s["meta_accounts"][-1]["name"], "currency": info.get("currency")}


def remove_account(act_id):
    s = settings()
    s["meta_accounts"] = [a for a in s["meta_accounts"] if a["act"] != act_id]
    save_settings(s)
    return {"removed": act_id}


def rename_account(act_id, name):
    s = settings()
    for a in s["meta_accounts"]:
        if a["act"] == act_id:
            a["name"] = name.strip() or a["name"]
    save_settings(s)
    return {"ok": True}
