"""Creative and asset performance for Google Ads and Meta (Ads > Creatives, MCP creative_performance).

Read-only towards both platforms. Rows from the user can go into the Ads DRAFT, and only with change types the draft
already has (core.add / meta_ads.add): nothing here pushes, pauses or edits anything live.

Views
  Google  "ads"     every asset in ads (Search RSA headlines and descriptions, Demand Gen images, logos, headlines,
                    descriptions and videos, video ads): ad_group_ad_asset_view, one GAQL search.
          "pmax"    Performance Max asset group assets: asset_group_asset, one GAQL search.
  Meta    "assets"  per text, image, video, call to action and link of ads that use several options (flexible /
                    dynamic / Advantage+ creative, i.e. an asset_feed_spec): insights level=ad with one asset
                    breakdown per call (Meta allows only one), and only for those ads, filtered by ad id.
          "ads"     ad by ad for every ad that delivered: thumbnail, primary text, headline, format, with the same
                    metrics and the video metrics (plays, ThruPlays, 25/50/75/100% watched, average watch time).

Rows are grouped by what the asset is (the same headline text, image or video in many ads adds up), or one row per
ad and asset with group=False. Metrics: impressions, clicks, CTR, cost, conversions, CPA, value and ROAS (when the
platform reports a conversion value). Rows under min_impressions (default 500) are flagged as low volume: too little
data to judge. Google's asset performance labels are mostly "Pending" / "Not applicable" and are not used.

Reads (per account and date range, cached in <data>/creatives_<account>_<range>.json, datapolicy source
"creatives:<account id>": fetched once, kept until the user presses Refresh; no background or timed pulls in manual
mode):
  Google  2 GAQL searches (the two views; the account currency comes along in the rows).
  Meta    1 GET ad insights (with video metrics); the ads that delivered with their creatives come from the Ads area's
          Meta cache when it has them, else 1 GET of only those ads (filtered by id); then up to 7 GETs of asset
          breakdowns (body, title, description, image, video, call to action, link) for the asset-feed ads that
          delivered. Every call is a single GET (meta_api.get_once, no retries); before each one Meta's usage headers
          are checked and the read stops early (the rest marked as not fetched) when Studio is near the limit;
          fetch_rest() later reads only the missing breakdowns.

Draft actions (draft(); the user's Add to draft buttons, never automatic):
  Google Search RSA asset   replace_text / remove_text -> {"type":"rsa","edit":true,"local_id":<ad id>,
                            "headlines":[..],"descriptions":[..]} (the ad's current text is read fresh, one GAQL
                            search, so an edit only swaps the one text); pause_ad -> rsa edit status paused
  Google PMax text asset    remove_text -> pmax_asset remove; replace_text -> pmax_asset remove + pmax_asset add
  Meta asset-feed text      replace_text / remove_text -> meta_feed_copy (the ad's text lists with that one changed)
  Meta ad                   pause_ad -> meta_status paused; replace_text (primary text or headline) -> meta_copy
  Everything else (images, videos, Demand Gen, call to action, links) is read-only here.
"""
import datetime as dt
import hashlib
import json
import re
import threading
import time

import core
import datapolicy

SOURCE = "creatives"
LOW_IMPRESSIONS = 500
DAYS = (7, 14, 30, 90)
DEFAULT_DAYS = 30
NOTE = "Creatives:"
PAGES = 10  # Meta: at most this many next pages per read
_locks = {}
_locks_lock = threading.Lock()

G_ADS = ("SELECT customer.currency_code, campaign.id, campaign.name, campaign.status, campaign.advertising_channel_type, "
         "ad_group.id, ad_group.name, ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status, "
         "ad_group_ad_asset_view.field_type, ad_group_ad_asset_view.pinned_field, ad_group_ad_asset_view.enabled, "
         "asset.id, asset.type, asset.name, asset.text_asset.text, asset.image_asset.full_size.url, "
         "asset.youtube_video_asset.youtube_video_id, asset.youtube_video_asset.youtube_video_title, "
         "metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions, metrics.conversions_value, "
         "metrics.video_trueview_views FROM ad_group_ad_asset_view WHERE {cond} AND metrics.impressions > 0")
G_PMAX = ("SELECT customer.currency_code, campaign.id, campaign.name, campaign.status, asset_group.id, asset_group.name, "
          "asset_group.status, asset_group_asset.field_type, asset_group_asset.status, asset.id, asset.type, asset.name, "
          "asset.text_asset.text, asset.image_asset.full_size.url, asset.youtube_video_asset.youtube_video_id, "
          "asset.youtube_video_asset.youtube_video_title, metrics.impressions, metrics.clicks, metrics.cost_micros, "
          "metrics.conversions, metrics.conversions_value FROM asset_group_asset WHERE {cond} AND metrics.impressions > 0")
G_RSA = ("SELECT ad_group_ad.ad.id, ad_group_ad.ad.responsive_search_ad.headlines, ad_group_ad.ad.responsive_search_ad.descriptions "
         "FROM ad_group_ad WHERE ad_group_ad.ad.id IN ({ids})")
RSA_TEXT = {"HEADLINE": ("headlines", 30, 3), "DESCRIPTION": ("descriptions", 90, 2)}  # field -> (list, max chars, min left)
PMAX_TEXT = {"HEADLINE": "headline", "LONG_HEADLINE": "long_headline", "DESCRIPTION": "description"}
PMAX_MAX = {"HEADLINE": 30, "LONG_HEADLINE": 90, "DESCRIPTION": 90}

M_ADS = ("id,name,campaign_id,adset_id,effective_status,campaign{name},adset{name},"
         "creative{id,object_type,object_story_spec,asset_feed_spec,object_story_id,effective_object_story_id,thumbnail_url,"
         "image_url,body,title,link_url,call_to_action_type}")
M_VIDEO = ("video_play_actions,video_thruplay_watched_actions,video_p25_watched_actions,video_p50_watched_actions,"
           "video_p75_watched_actions,video_p100_watched_actions,video_avg_time_watched_actions")
M_INS = "ad_id,ad_name,adset_name,campaign_name,account_currency,spend,impressions,clicks,actions,action_values," + M_VIDEO
M_BD_INS = "ad_id,spend,impressions,clicks,actions,action_values"
BREAKDOWNS = ("body_asset", "title_asset", "description_asset", "image_asset", "video_asset", "call_to_action_asset", "link_url_asset")
M_FIELD = {"body_asset": "Primary text", "title_asset": "Headline", "description_asset": "Description", "image_asset": "Image",
           "video_asset": "Video", "call_to_action_asset": "Call to action", "link_url_asset": "Link"}
M_FEED_LIST = {"body_asset": "bodies", "title_asset": "titles", "description_asset": "descriptions"}
VIDEO_KEYS = ("plays", "thruplays", "p25", "p50", "p75", "p100", "avg_watch")
SUMS = ("impressions", "clicks", "cost", "conversions", "value", "views") + VIDEO_KEYS[:-1]

VIEWS = {"google": [{"id": "ads", "label": "Ad assets (Search, Demand Gen, Video)"}, {"id": "pmax", "label": "Performance Max assets"}],
         "meta": [{"id": "assets", "label": "Assets of flexible / Advantage+ ads"}, {"id": "ads", "label": "Ad by ad"}]}
CHANNEL = {"SEARCH": "Search", "DEMAND_GEN": "Demand Gen", "VIDEO": "Video", "DISPLAY": "Display", "PERFORMANCE_MAX": "Performance Max"}

METHOD = ("Metrics are the platform's own for each asset in each ad over the date range, added up per asset (same text, "
          "image or video) across ads. CTR = clicks / impressions; CPA = cost / conversions; ROAS = conversion value / cost. "
          "Meta conversions are its result actions (purchases, leads, custom pixel events); Meta asset breakdowns exist only "
          "for ads with several text or media options, so other ads are compared ad by ad. Low volume = fewer impressions "
          "than the threshold: too little data to judge.")


# ---------------------------------------------------------------- small helpers

def _lock(key):
    with _locks_lock:
        return _locks.setdefault(key, threading.Lock())


def _r(v, n=2):
    return None if v is None else round(float(v) + 0.0, n)


def _int(v):
    try:
        return int(float(v or 0))
    except (TypeError, ValueError):
        return 0


def _f(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _title(v):
    return (v or "").replace("_", " ").capitalize()


def _scrub(ex):
    try:
        from health import _scrub as hs  # noqa: PLC0415
        return hs(str(ex))[:400]
    except Exception:  # noqa: BLE001
        return str(ex)[:400]


def _norm(text):
    """Grouping key for a text: whitespace collapsed, case kept (a capital letter is a different headline)."""
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _key(*parts):
    return hashlib.sha1(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()[:14]


def _https(url):
    """Only https links go to the page (thumbnails, previews)."""
    u = str(url or "")
    return u if u.startswith("https://") else ""


def window(days=None, start=None, end=None, today=None):
    """-> {key, label, g_cond, meta} for a date range: days (7, 14, 30, 90) or start + end (YYYY-MM-DD).
    Ranges end yesterday (complete days), like the platforms' own "last N days"."""
    today = today or dt.date.today()
    if start or end:
        try:
            a, b = dt.date.fromisoformat(str(start)), dt.date.fromisoformat(str(end or today))
        except ValueError:
            raise ValueError("start and end must be YYYY-MM-DD") from None
        if a > b:
            raise ValueError("start is after end")
        if b > today:
            raise ValueError("end can't be in the future")
        if a < today - dt.timedelta(days=37 * 30):
            raise ValueError("the platforms keep asset data for about 3 years")
        return {"key": f"{a}_{b}", "label": f"{a:%d %b %Y} to {b:%d %b %Y}", "days": None,
                "g_cond": f"segments.date BETWEEN '{a}' AND '{b}'", "meta": {"time_range": {"since": str(a), "until": str(b)}}}
    try:
        n = int(str(days or DEFAULT_DAYS).lower().rstrip("d"))
    except ValueError:
        raise ValueError(f"days must be one of {', '.join(map(str, DAYS))}") from None
    if n not in DAYS:
        raise ValueError(f"days must be one of {', '.join(map(str, DAYS))}, or give start and end")
    if n in (7, 14, 30):
        cond = f"segments.date DURING LAST_{n}_DAYS"
    else:
        e = today - dt.timedelta(days=1)
        cond = f"segments.date BETWEEN '{e - dt.timedelta(days=n - 1)}' AND '{e}'"
    return {"key": f"{n}d", "label": f"last {n} days", "days": n, "g_cond": cond, "meta": {"date_preset": f"last_{n}d"}}


def finish(m):
    """Totals -> the derived numbers (CTR %, CPC, CPA, ROAS, view rate)."""
    imp, clk, cost, conv, val = m.get("impressions") or 0, m.get("clicks") or 0, m.get("cost") or 0.0, m.get("conversions") or 0.0, m.get("value") or 0.0
    m["ctr"] = _r(clk / imp * 100) if imp else None
    m["cpc"] = _r(cost / clk) if clk else None
    m["cpa"] = _r(cost / conv) if conv else None
    m["roas"] = _r(val / cost) if val and cost else None
    m["cost"], m["value"], m["conversions"] = _r(cost), _r(val), _r(conv)
    if m.get("plays"):
        m["thruplay_rate"] = _r((m.get("thruplays") or 0) / m["plays"] * 100, 1)
    return m


# ---------------------------------------------------------------- reading Google

def _g_asset(a):
    t = a.get("type") or ""
    kind = "text" if t == "TEXT" else "image" if t == "IMAGE" else "video" if t == "YOUTUBE_VIDEO" else "other"
    yt = a.get("youtubeVideoAsset") or {}
    return {"asset_id": str(a.get("id") or ""), "asset_type": t, "kind": kind,
            "text": (a.get("textAsset") or {}).get("text") or (a.get("name") if kind == "other" else "") or "",
            "name": a.get("name") or "", "image_url": _https(((a.get("imageAsset") or {}).get("fullSize") or {}).get("url")),
            "video_id": yt.get("youtubeVideoId") or "", "video_title": yt.get("youtubeVideoTitle") or ""}


def _g_metrics(m):
    return {"impressions": _int(m.get("impressions")), "clicks": _int(m.get("clicks")), "cost": _int(m.get("costMicros")) / 1e6,
            "conversions": _f(m.get("conversions")), "value": _f(m.get("conversionsValue")), "views": _int(m.get("videoTrueviewViews"))}


def fetch_google(cid, win, query=None):
    """One Google account's asset rows for a date range: two read-only GAQL searches."""
    if query is None:
        import live  # noqa: PLC0415
        query = live.query
    cur = ""
    ads = []
    for r in query(cid, G_ADS.format(cond=win["g_cond"])):
        c, g, aa, v = r.get("campaign", {}), r.get("adGroup", {}), r.get("adGroupAd", {}), r.get("adGroupAdAssetView", {})
        cur = cur or (r.get("customer") or {}).get("currencyCode", "")
        ad = aa.get("ad") or {}
        ads.append({"campaign_id": str(c.get("id")), "campaign": c.get("name", ""), "campaign_status": _title(c.get("status")),
                    "channel": CHANNEL.get(c.get("advertisingChannelType"), _title(c.get("advertisingChannelType"))),
                    "ad_group_id": str(g.get("id")), "ad_group": g.get("name", ""), "ad_id": str(ad.get("id")),
                    "ad_type": ad.get("type") or "", "ad_status": _title(aa.get("status")), "field": v.get("fieldType") or "",
                    "pinned": v.get("pinnedField") or "", "enabled": v.get("enabled", True) is not False,
                    **_g_asset(r.get("asset") or {}), **_g_metrics(r.get("metrics") or {})})
    pmax = []
    for r in query(cid, G_PMAX.format(cond=win["g_cond"])):
        c, g, l = r.get("campaign", {}), r.get("assetGroup", {}), r.get("assetGroupAsset", {})
        cur = cur or (r.get("customer") or {}).get("currencyCode", "")
        pmax.append({"campaign_id": str(c.get("id")), "campaign": c.get("name", ""), "campaign_status": _title(c.get("status")),
                     "channel": "Performance Max", "asset_group_id": str(g.get("id")), "asset_group": g.get("name", ""),
                     "asset_group_status": _title(g.get("status")), "field": l.get("fieldType") or "", "status": _title(l.get("status")),
                     **_g_asset(r.get("asset") or {}), **_g_metrics(r.get("metrics") or {})})
    return {"platform": "google", "account_id": str(cid), "currency": cur, "window": win["key"], "label": win["label"],
            "fetched_at": dt.datetime.now().isoformat(timespec="seconds"), "_at": time.time(), "calls": 2,
            "rows": {"ads": ads, "pmax": pmax}, "missing": [], "warnings": []}


# ---------------------------------------------------------------- reading Meta

class MetaBusy(Exception):
    """Meta's usage headers say Studio is close to its limit: stop reading (what was read is kept)."""


def _usage_for(u, kind):
    """Meta's usage headers -> only the parts that limit this kind of read: "insights" (the ads_insights use case and the
    insights throttle) or "management" (the ads_management use case). Account and app usage count for both."""
    out = {h: u[h] for h in ("x-ad-account-usage", "x-app-usage") if h in (u or {})}
    if kind == "insights" and "x-fb-ads-insights-throttle" in u:
        out["x-fb-ads-insights-throttle"] = u["x-fb-ads-insights-throttle"]
    buc = (u or {}).get("x-business-use-case-usage")
    if isinstance(buc, dict):
        want = "ads_insights" if kind == "insights" else "ads_management"
        out["x-business-use-case-usage"] = {k: [e for e in v if isinstance(e, dict) and e.get("type") in (want, None)]
                                            for k, v in buc.items() if isinstance(v, list)}
    return out


def _meta_busy(kind=None):
    """Why to stop before the next Meta read of this kind ("insights" or "management"), or None. The back-off line and
    wait are Alerts' (alerts.meta_pressure / _backoff_minutes) on the headers that limit that kind of read."""
    try:
        import alerts  # noqa: PLC0415
        import meta_api  # noqa: PLC0415
        u = meta_api.usage()
        pct, regain = alerts.meta_pressure(_usage_for(u, kind) if kind else u)
        mins = alerts._backoff_minutes(pct, regain)
        return f"Meta usage is at {pct:.0f}%: try again in about {mins:.0f} minutes" if mins else None
    except Exception:  # noqa: BLE001 - no usage known: carry on
        return None


def _acts(lst, types=None):
    """Sum of an actions list (all entries, or only action_type in types)."""
    return sum(_f(a.get("value")) for a in lst or [] if types is None or a.get("action_type") in types)


def _m_metrics(r, video=False):
    from meta_ads import RESULT_ACTIONS  # noqa: PLC0415
    out = {"impressions": _int(r.get("impressions")), "clicks": _int(r.get("clicks")), "cost": _f(r.get("spend")),
           "conversions": _acts(r.get("actions"), RESULT_ACTIONS),
           "value": _acts(r.get("action_values"), ("offsite_conversion.fb_pixel_purchase",)), "views": 0}
    if video:
        out.update(plays=_acts(r.get("video_play_actions")), thruplays=_acts(r.get("video_thruplay_watched_actions")),
                   p25=_acts(r.get("video_p25_watched_actions")), p50=_acts(r.get("video_p50_watched_actions")),
                   p75=_acts(r.get("video_p75_watched_actions")), p100=_acts(r.get("video_p100_watched_actions")),
                   avg_watch=_acts(r.get("video_avg_time_watched_actions")))
    return out


def _bd_asset(bd, a):
    a = a or {}
    if bd in ("body_asset", "title_asset", "description_asset"):
        return {"kind": "text", "text": a.get("text") or ""}
    if bd == "image_asset":
        return {"kind": "image", "hash": a.get("hash") or "", "image_url": _https(a.get("url")), "text": a.get("name") or ""}
    if bd == "video_asset":
        return {"kind": "video", "video_id": str(a.get("video_id") or ""), "image_url": _https(a.get("thumbnail_url")),
                "video_url": _https(a.get("url")), "text": a.get("video_name") or a.get("name") or ""}
    if bd == "call_to_action_asset":
        return {"kind": "text", "text": _title(a.get("name") or a.get("type") or "")}
    return {"kind": "text", "text": a.get("website_url") or a.get("display_url") or ""}


def _reader(get, busy, calls):
    """read(path, _pages=PAGES, **params) -> every row of a list (at most _pages next pages); checks busy(kind) before
    each GET (kind: "insights" for insights reads, else "management")."""
    def read(path, _pages=PAGES, **params):
        kind = "insights" if path.endswith("/insights") else "management"
        why = busy(kind) if busy else None
        if why:
            raise MetaBusy(why)
        page = get(path, **params)
        calls[0] += 1
        out = list(page.get("data") or [])
        nxt, n = (page.get("paging") or {}).get("next"), 0
        while nxt and n < _pages:
            why = busy(kind) if busy else None
            if why:
                raise MetaBusy(why)
            page = get(nxt)
            calls[0] += 1
            out += page.get("data") or []
            nxt, n = (page.get("paging") or {}).get("next"), n + 1
        return out
    return read


def _breakdowns(act, win, feed_ids, which, read):
    """Asset breakdowns (one per call) for these ads. -> (rows per breakdown, missing, warnings). Stops at Meta's
    back-off line or a rate limit: what was read is kept, the rest listed in missing."""
    import meta_ads  # noqa: PLC0415
    bds, missing, warnings = {}, [], []
    for i, bd in enumerate(which if feed_ids else ()):
        try:
            rows = []
            for k in range(0, len(feed_ids), 100):
                rows += read(f"{act}/insights", level="ad", breakdowns=bd, limit=500, **win["meta"],
                             filtering=[{"field": "ad.id", "operator": "IN", "value": feed_ids[k:k + 100]}],
                             fields=M_BD_INS + ("," + M_VIDEO if bd == "video_asset" else ""))
        except (MetaBusy, meta_ads.MetaError) as ex:
            missing = list(which[i:])
            warnings.append(_stopped(ex, missing))
            break
        bds[bd] = [{"ad_id": str(r.get("ad_id")), "asset_id": str((r.get(bd) or {}).get("id") or ""), **_bd_asset(bd, r.get(bd)),
                    **_m_metrics(r, video=bd == "video_asset")} for r in rows]
    return bds, missing, warnings


def _meta_known(act):
    """Ads (with their creatives) from the Ads area's own Meta cache when it has this account: no extra call. Campaign
    and ad set names are added from the same cache. {} when there is none."""
    try:
        import meta_ads  # noqa: PLC0415
        if meta_ads.act() != act:
            return {}
        d, _ = meta_ads._cached_copy(None)
    except Exception:  # noqa: BLE001
        return {}
    if not d:
        return {}
    camps = {str(c.get("id")): c.get("name", "") for c in d.get("campaigns") or []}
    sets = {str(x.get("id")): x.get("name", "") for x in d.get("adsets") or []}
    return {str(a["id"]): {**a, "campaign": {"name": camps.get(str(a.get("campaign_id")), "")},
                           "adset": {"name": sets.get(str(a.get("adset_id")), "")}} for a in d.get("ads") or [] if a.get("id")}


def _ad_stub(i, r):
    """An ad that delivered, from its insights row only (before its creative is read)."""
    return {"id": i, "name": r.get("ad_name") or f"Ad {i}", "status": "", "active": False, "campaign_id": "",
            "campaign": r.get("campaign_name") or "", "adset_id": "", "adset": r.get("adset_name") or "", "thumbnail": "",
            "format": "", "primary_text": "", "headline": "", "description": "", "edit_type": "",
            "why_not": "Its creative is not read yet: Fetch the rest", "feed": False, "lists": {}, "read": False}


def _ad_entry(a, stub):
    import meta_ads  # noqa: PLC0415
    cr = a.get("creative") or {}
    ct = meta_ads.creative_text(cr)
    feed = cr.get("asset_feed_spec") or {}
    return {**stub, "name": a.get("name") or stub["name"], "status": _title(a.get("effective_status")),
            "active": a.get("effective_status") == "ACTIVE", "campaign_id": str(a.get("campaign_id") or ""),
            "campaign": (a.get("campaign") or {}).get("name") or stub["campaign"], "adset_id": str(a.get("adset_id") or ""),
            "adset": (a.get("adset") or {}).get("name") or stub["adset"], "thumbnail": _https(cr.get("thumbnail_url") or cr.get("image_url")),
            "format": ct.get("format") or "", "primary_text": ct.get("primary_text") or "", "headline": ct.get("headline") or "",
            "description": ct.get("description") or "", "edit_type": ct.get("edit_type") or "", "why_not": ct.get("why_not") or "",
            "feed": bool(feed), "lists": {k: [b.get("text", "") for b in feed.get(k) or []] for k in ("bodies", "titles", "descriptions")},
            "read": True}


def _read_ads(act, ads, known, read):
    """Fill in the creatives of the ads not read yet: from the Ads area's Meta cache (no call), else GETs of only those
    ads (filtered by id, 100 per call). Each answer is kept at once, so a stop between calls loses nothing. Ads Meta no
    longer returns (deleted) are marked read."""
    todo = [i for i, a in ads.items() if not a.get("read")]
    for i in todo:
        if i in (known or {}):
            ads[i] = _ad_entry(known[i], ads[i])
    need = sorted(i for i in todo if not ads[i]["read"])
    for k in range(0, len(need), 100):
        chunk = need[k:k + 100]
        # one page holds every ad the id filter can return: no next page (Meta often sends an empty one)
        for a in read(f"{act}/ads", _pages=0, fields=M_ADS, limit=100, filtering=[{"field": "id", "operator": "IN", "value": chunk}]):
            i = str(a.get("id"))
            if i in ads:
                ads[i] = _ad_entry(a, ads[i])
        for i in chunk:
            if not ads[i]["read"]:
                ads[i].update(read=True, why_not="Meta no longer returns this ad (deleted)")


def _stopped(ex, missing):
    import meta_ads  # noqa: PLC0415
    if isinstance(ex, meta_ads.MetaError) and not ex.rate_limited:
        raise ex
    what = ", ".join("the ads' creatives" if b == "ads" else M_FIELD[b].lower() for b in missing)
    return f"Stopped before {what}: {ex}. Press Fetch the rest later (only those are read)."


def fetch_meta(act, win, get=None, busy=_meta_busy, known=None):
    """One Meta ad account's creative rows for a date range. Single GETs only (no retries); stops early when Meta's
    usage headers say Studio is near the limit or Meta rate-limits (what was read is kept, the rest listed in
    "missing" for fill_missing / Fetch the rest)."""
    if get is None:
        import meta_api  # noqa: PLC0415
        get = meta_api.get_once
    import meta_ads  # noqa: PLC0415
    calls = [0]
    read = _reader(get, busy, calls)
    ins = read(f"{act}/insights", level="ad", fields=M_INS, limit=500, **win["meta"])
    delivered = {str(r.get("ad_id")): r for r in ins}
    cur = next((r.get("account_currency") for r in ins if r.get("account_currency")), "")
    ads = {i: _ad_stub(i, r) for i, r in delivered.items()}
    snap = {"platform": "meta", "account_id": "meta_" + act.replace("act_", ""), "act": act, "currency": cur, "window": win["key"],
            "label": win["label"], "fetched_at": dt.datetime.now().isoformat(timespec="seconds"), "_at": time.time(),
            "calls": 0, "ads": ads, "ad_metrics": {i: _m_metrics(r, video=True) for i, r in delivered.items()},
            "breakdowns": {}, "missing": [], "warnings": []}
    try:
        _read_ads(act, ads, _meta_known(act) if known is None else known, read)
    except (MetaBusy, meta_ads.MetaError) as ex:
        snap["missing"] = ["ads"] + list(BREAKDOWNS)
        snap["warnings"] = [_stopped(ex, ["ads"])]
        snap["calls"] = calls[0]
        return snap
    feed_ids = sorted(i for i, a in ads.items() if a["feed"])
    snap["breakdowns"], snap["missing"], snap["warnings"] = _breakdowns(act, win, feed_ids, BREAKDOWNS, read)
    snap["calls"] = calls[0]
    return snap


def fill_missing(snap, win, get=None, busy=_meta_busy, known=None):
    """A Meta read that stopped early: read only what it is missing (the ads' creatives, then the asset breakdowns;
    same range) into it."""
    if get is None:
        import meta_api  # noqa: PLC0415
        get = meta_api.get_once
    import meta_ads  # noqa: PLC0415
    calls = [0]
    read = _reader(get, busy, calls)
    missing = list(snap.get("missing") or [])
    try:
        if "ads" in missing:
            _read_ads(snap["act"], snap["ads"], _meta_known(snap["act"]) if known is None else known, read)
            missing.remove("ads")
    except (MetaBusy, meta_ads.MetaError) as ex:
        snap.update(missing=missing, warnings=[_stopped(ex, ["ads"])], calls=int(snap.get("calls") or 0) + calls[0])
        return snap
    feed_ids = sorted(i for i, a in snap["ads"].items() if a["feed"])
    which = [b for b in BREAKDOWNS if b in missing]
    bds, missing, warnings = _breakdowns(snap["act"], win, feed_ids, which, read)
    snap["breakdowns"].update(bds)
    snap.update(missing=missing, warnings=warnings, calls=int(snap.get("calls") or 0) + calls[0])
    return snap


# ---------------------------------------------------------------- accounts, cache

def accounts():
    import pacing  # noqa: PLC0415 - the same account list as Pacing
    return pacing.accounts()


def find_account(ref, accts=None):
    import pacing  # noqa: PLC0415
    return pacing.find_account(ref, accts)


def _file(acct_id, wkey):
    return core.STUDIO_DIR / f"creatives_{re.sub(r'[^A-Za-z0-9_]', '_', acct_id)}_{re.sub(r'[^A-Za-z0-9_-]', '_', wkey)}.json"


def _src(acct_id):
    return f"{SOURCE}:{acct_id}"


def cached(acct_id, wkey):
    try:
        return json.loads(_file(acct_id, wkey).read_text())
    except (OSError, ValueError):
        return None


def _fetch(a, win):
    if a["platform"] == "google":
        return fetch_google(a["id"], win)
    import meta_ads  # noqa: PLC0415
    meta_ads.use(a["key"])
    return fetch_meta(a["act"], win)


def snapshot(a, win, force=False, fetch=None):
    """The cached read for this account and date range, or a new one: when there is none yet, after a Refresh
    (datapolicy), or force. Never on a timer."""
    with _lock(a["id"]):
        d = cached(a["id"], win["key"])
        if d and not force and datapolicy.fresh(_src(a["id"]), time.time() - float(d.get("_at") or 0), None, d.get("_at")):
            return d
        d = (fetch or _fetch)(a, win)
        d["name"] = a["name"]
        f = _file(a["id"], win["key"])
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False))
        tmp.replace(f)
        datapolicy.mark(_src(a["id"]))
        return d


def refresh(account=None, days=None, start=None, end=None):
    """The Refresh button / Refresh menu: read one account (or every account) again now for a date range. Read-only."""
    win = window(days, start, end)
    t0 = time.time()
    accts = accounts()
    if account:
        accts = [find_account(account, accts)]
    out, errs = [], []
    for a in accts:
        datapolicy.invalidate(_src(a["id"]), at=t0)
        try:
            d = snapshot(a, win, force=True)
            out.append({"account": a["id"], "fetched_at": d["fetched_at"], "calls": d.get("calls"), "missing": d.get("missing", [])})
        except Exception as ex:  # noqa: BLE001 - one account failing must not stop the rest
            errs.append({"account": a["id"], "name": a["name"], "error": _scrub(ex)})
    return {"accounts": out, "errors": errs, "window": win["label"]}


def fetch_rest(account, days=None, start=None, end=None, get=None):
    """Meta: read only the asset breakdowns the cached read is missing (it stopped at Meta's back-off line)."""
    a = find_account(account)
    if a["platform"] != "meta":
        raise ValueError("only Meta reads can stop early")
    win = window(days, start, end)
    with _lock(a["id"]):
        snap = cached(a["id"], win["key"])
        if not snap:
            raise ValueError("nothing cached for this account and date range yet")
        if snap.get("missing"):
            import meta_ads  # noqa: PLC0415
            meta_ads.use(a["key"])
            fill_missing(snap, win, get)
            f = _file(a["id"], win["key"])
            tmp = f.with_suffix(".tmp")
            tmp.write_text(json.dumps(snap, ensure_ascii=False))
            tmp.replace(f)
    return {"account": a["id"], "missing": snap.get("missing", []), "warnings": snap.get("warnings", [])}


def fetched_at():
    """Oldest last fetch over every account (None when any account has never been read): the Refresh menu's line."""
    try:
        f = [datapolicy.fetched(_src(a["id"])) for a in accounts()]
    except Exception:  # noqa: BLE001
        return None
    return min(f) if f and all(f) else None


# ---------------------------------------------------------------- building a view (no API calls)

def _actions_google(view, m):
    """Draft actions one Google member row allows (an asset in one ad / asset group)."""
    if view == "ads":
        if m["ad_type"] != "RESPONSIVE_SEARCH_AD" or m["ad_status"] == "Removed":
            return []
        out = ["pause_ad"] if m["ad_status"] == "Enabled" else []
        if m["field"] in RSA_TEXT and m["enabled"] and m["kind"] == "text":
            out += ["replace_text", "remove_text"]
        return out
    if m["field"] in PMAX_TEXT and m["kind"] == "text" and m["status"] != "Removed":
        return ["replace_text", "remove_text"]
    return []


def _actions_meta(view, ad, bd=None):
    if not ad:
        return []
    out = ["pause_ad"] if ad["active"] else []
    if view == "assets":
        if bd in M_FEED_LIST and ad["edit_type"] == "meta_feed_copy":
            out += ["replace_text", "remove_text"]
    elif ad["edit_type"] == "meta_copy":
        out.append("replace_text")
    return out


def _members(snap, view):
    """Per ad (or asset group) and asset rows for a view: [{gkey, field, label, ...display, metrics, actions}]."""
    out = []
    if snap["platform"] == "google":
        for m in snap["rows"].get(view, []):
            content = _norm(m["text"]) if m["kind"] in ("text", "other") else m["asset_id"]
            out.append({**m, "field_label": _title(m["field"]), "gkey": ("g", view, m["field"], m["kind"], content),
                        "where": " / ".join(x for x in (m["campaign"], m.get("ad_group") or m.get("asset_group")) if x),
                        "owner_id": m.get("ad_id") or m.get("asset_group_id"),
                        "status": m.get("ad_status") or m.get("status") or "", "actions": _actions_google(view, m)})
        return out
    ads = snap["ads"]
    if view == "ads":
        for i, ad in ads.items():
            met = snap["ad_metrics"].get(i) or {}
            text = ad["primary_text"] or ad["headline"] or ("" if ad.get("read") and ad["feed"] else ad["name"])
            label = ("Flexible " + ad["format"].lower()).strip() if ad["feed"] else ad["format"] or "Ad"
            out.append({"gkey": ("m", "ads", i), "field": label.lower().replace(" ", "_"), "field_label": label, "kind": "ad", "text": text,
                        "headline": ad["headline"], "primary_text": ad["primary_text"], "image_url": ad["thumbnail"],
                        "ad_id": i, "owner_id": i, "ad_name": ad["name"], "campaign": ad["campaign"], "adset": ad["adset"],
                        "where": " / ".join(x for x in (ad["campaign"], ad["adset"]) if x), "status": ad["status"],
                        "flexible": ad["feed"], "why_not": ad["why_not"], **met, "actions": _actions_meta("ads", ad)})
        return out
    for bd, rows in snap["breakdowns"].items():
        for r in rows:
            ad = ads.get(r["ad_id"])
            content = r.get("hash") or r.get("video_id") or _norm(r["text"])
            out.append({**r, "field": bd, "field_label": M_FIELD[bd], "gkey": ("m", "assets", bd, content), "owner_id": r["ad_id"],
                        "ad_name": (ad or {}).get("name", ""), "campaign": (ad or {}).get("campaign", ""), "adset": (ad or {}).get("adset", ""),
                        "where": " / ".join(x for x in ((ad or {}).get("campaign"), (ad or {}).get("adset")) if x),
                        "status": (ad or {}).get("status", ""), "actions": _actions_meta("assets", ad, bd)})
    return out


def _group(members, grouped, min_imp):
    groups = {}
    for m in members:
        k = _key(*m["gkey"]) if grouped else _key(*m["gkey"], m["owner_id"], m.get("asset_id"))
        g = groups.get(k)
        if g is None:
            g = groups[k] = {"key": k, "field": m["field"], "field_label": m["field_label"], "kind": m["kind"], "text": m.get("text", ""),
                             "image_url": m.get("image_url", ""), "video_id": m.get("video_id", ""), "video_title": m.get("video_title", ""),
                             "video_url": m.get("video_url", ""), "headline": m.get("headline", ""), "primary_text": m.get("primary_text", ""),
                             "why_not": m.get("why_not", ""), "flexible": m.get("flexible", False), "channels": [], "campaigns": [],
                             "members": [], "actions": [], **dict.fromkeys(SUMS, 0)}
            if "avg_watch" in m:
                g["_watch"] = 0.0
        g["members"].append({k2: m.get(k2) for k2 in ("owner_id", "ad_id", "asset_id", "ad_name", "where", "status", "channel",
                                                      "campaign", "ad_group", "asset_group", "adset", "pinned", "enabled", "actions",
                                                      "impressions", "clicks", "cost", "conversions", "value") if m.get(k2) not in (None, "")})
        for s in SUMS:
            g[s] += m.get(s) or 0
        if "avg_watch" in m:
            g["_watch"] += (m.get("avg_watch") or 0) * (m.get("plays") or 0)
        for lst, v in (("channels", m.get("channel")), ("campaigns", m.get("campaign"))):
            if v and v not in g[lst]:
                g[lst].append(v)
        for a in m["actions"]:
            if a not in g["actions"]:
                g["actions"].append(a)
    out = []
    for g in groups.values():
        w = g.pop("_watch", None)
        if w is not None:
            g["avg_watch"] = _r(w / g["plays"], 1) if g["plays"] else None
        if not any(g.get(k) for k in VIDEO_KEYS[:-1]):
            for k in VIDEO_KEYS:
                g.pop(k, None)
        g["ads"] = len({m.get("owner_id") for m in g["members"]})
        g["low_volume"] = g["impressions"] < min_imp
        for m in g["members"]:
            m["cost"] = _r(m.get("cost") or 0)
        out.append(finish(g))
    return out


SORTS = ("cost", "impressions", "clicks", "ctr", "conversions", "cpa", "roas", "value", "plays", "thruplays")


def account_view(a, snap, view=None, grouped=True, min_impressions=LOW_IMPRESSIONS, field=None, search=None, sort="cost", limit=None):
    views = VIEWS[snap["platform"]]
    view = view or views[0]["id"]
    if view not in [v["id"] for v in views]:
        raise ValueError(f"view must be one of {', '.join(v['id'] for v in views)} for {snap['platform']}")
    members = _members(snap, view)
    fields = sorted({(m["field"], m["field_label"]) for m in members}, key=lambda x: x[1])
    if field:
        want = str(field).lower().replace(" ", "_")
        members = [m for m in members if want in (str(m["field"]).lower(), m["field_label"].lower().replace(" ", "_"))]
    if search:
        s = str(search).lower()
        members = [m for m in members if s in " ".join(str(m.get(k) or "") for k in ("text", "headline", "where", "ad_name", "video_title")).lower()]
    rows = _group(members, grouped, float(min_impressions if min_impressions is not None else LOW_IMPRESSIONS))
    sort = sort if sort in SORTS else "cost"
    rows.sort(key=lambda r: (r.get(sort) is None, -(r.get(sort) or 0) if sort != "cpa" else (r.get(sort) or 0), r["text"]))
    total = finish({s: sum(r.get(s) or 0 for r in rows) for s in SUMS[:6]})
    notes = []
    if snap["platform"] == "meta" and view == "assets":
        n = sum(1 for x in snap["ads"].values() if x["feed"])
        notes.append(f"{n} of {len(snap['ads'])} ads that delivered use several text or media options; the rest are in Ad by ad.")
    if snap["platform"] == "google" and view == "ads" and any(m.get("channel") == "Demand Gen" for m in members):
        notes.append("Demand Gen and video assets are read-only here: change them in Google Ads.")
    return {"account": a["id"], "key": a["key"], "name": a["name"], "platform": snap["platform"], "currency": snap.get("currency", ""),
            "window": snap.get("label"), "fetched_at": snap.get("fetched_at"), "view": view, "views": views, "grouped": bool(grouped),
            "fields": [{"id": f, "label": lbl} for f, lbl in fields], "min_impressions": min_impressions, "totals": total,
            "count": len(rows), "rows": rows[:int(limit)] if limit else rows, "missing": snap.get("missing", []),
            "warnings": snap.get("warnings", []) + notes, "method": METHOD}


def status(account=None, view=None, days=None, start=None, end=None, refresh=False, grouped=True,
           min_impressions=LOW_IMPRESSIONS, field=None, search=None, sort="cost", limit=None, fetch=None):
    """One account's creative view for a date range (cached; read once when nothing is cached, again on refresh)."""
    accts = accounts()
    a = find_account(account, accts) if account else (accts[0] if accts else None)
    if not a:
        raise ValueError("no ad accounts yet: connect Google Ads or Meta in Setup")
    win = window(days, start, end)
    if refresh:
        datapolicy.invalidate(_src(a["id"]))
    snap = snapshot(a, win, force=refresh, fetch=fetch)
    out = account_view(a, snap, view, grouped, min_impressions, field, search, sort, limit)
    out["accounts"] = [{"id": x["id"], "key": x["key"], "name": x["name"], "platform": x["platform"]} for x in accts]
    out["days"] = win["days"]
    return out


# ---------------------------------------------------------------- rows -> the draft

def _rsa_lists(cid, ad_ids, query=None):
    """The current headlines / descriptions (with pins) of these RSAs, read now (one GAQL search)."""
    if query is None:
        import live  # noqa: PLC0415
        query = live.query
    ids = [str(int(i)) for i in ad_ids]
    out = {}
    for r in query(cid, G_RSA.format(ids=", ".join(ids))):
        ad = (r.get("adGroupAd") or {}).get("ad") or {}
        rsa = ad.get("responsiveSearchAd") or {}

        def pin(x, kind):
            p = x.get("pinnedField") or ""
            return int(p.rsplit("_", 1)[1]) if p.startswith(kind + "_") else 0
        out[str(ad.get("id"))] = {"headlines": [{"text": h.get("text", ""), "pin": pin(h, "HEADLINE")} for h in rsa.get("headlines", [])],
                                  "descriptions": [{"text": d.get("text", ""), "pin": pin(d, "DESCRIPTION")} for d in rsa.get("descriptions", [])]}
    return out


def _check_text(new, limit, what):
    new = str(new or "").strip()
    if not new:
        raise ValueError("give the new text")
    if limit and len(new) > limit:
        raise ValueError(f"{what}: {len(new)} characters, the limit is {limit}")
    return new


def _find_row(v, key):
    row = next((r for r in v["rows"] if r["key"] == key), None)
    if not row:
        raise ValueError("that row is no longer in the data: refresh the view")
    return row


def _google_changes(row, action, new, members, view, cid, query):
    changes, skipped = [], []
    old = _norm(row["text"])
    if view == "ads":
        if action == "pause_ad":
            seen = set()
            for m in members:
                if m["ad_id"] not in seen:
                    seen.add(m["ad_id"])
                    changes.append(({"type": "rsa", "edit": True, "campaign": m["campaign"], "ad_group": m["ad_group"],
                                     "local_id": int(m["ad_id"]), "status": "paused"}, m))
            return changes, skipped
        lst, limit, least = RSA_TEXT[row["field"]]
        if action == "replace_text":
            new = _check_text(new, limit, row["field_label"])
        live_lists = _rsa_lists(cid, sorted({m["ad_id"] for m in members}), query)
        once = {}
        for m in members:  # one change per ad (an ad can hold the same text twice, e.g. with other spacing)
            once.setdefault(m["ad_id"], m)
        for m in once.values():
            cur = live_lists.get(m["ad_id"])
            if not cur:
                skipped.append({"name": m["where"], "why": "the ad was not found in Google Ads (removed?)"})
                continue
            items = cur[lst]
            hit = [i for i, x in enumerate(items) if _norm(x["text"]) == old]
            if not hit:
                skipped.append({"name": m["where"], "why": "the ad no longer has this text"})
                continue
            if action == "remove_text":
                if len(items) - len(hit) < least:
                    skipped.append({"name": m["where"], "why": f"the ad would have fewer than {least} {lst}"})
                    continue
                items = [x for i, x in enumerate(items) if i not in hit]
            else:
                if any(_norm(x["text"]) == _norm(new) for x in items):
                    skipped.append({"name": m["where"], "why": "the ad already has the new text"})
                    continue
                items = [dict(x, text=new) if i in hit else x for i, x in enumerate(items)]
            c = {"type": "rsa", "edit": True, "campaign": m["campaign"], "ad_group": m["ad_group"], "local_id": int(m["ad_id"]),
                 "headlines": cur["headlines"], "descriptions": cur["descriptions"]}
            c[lst] = items
            changes.append((c, m))
        return changes, skipped
    f = PMAX_TEXT[row["field"]]
    if action == "replace_text":
        new = _check_text(new, PMAX_MAX[row["field"]], row["field_label"])
        if _norm(new) == old:
            raise ValueError("the new text is the same as the old one")
    seen = set()
    for m in members:
        if m["asset_group_id"] in seen:
            continue
        seen.add(m["asset_group_id"])
        base = {"type": "pmax_asset", "campaign": m["campaign"], "asset_group": m["asset_group"], "field": f}
        changes.append(({**base, "remove": True, "texts": [row["text"]]}, m))
        if action == "replace_text":
            changes.append(({**base, "texts": [new]}, m))
    return changes, skipped


def _meta_changes(row, action, new, members, view, snap, text_field=None):
    changes, skipped = [], []
    if action == "pause_ad":
        for i in sorted({m["owner_id"] for m in members}):
            changes.append(({"type": "meta_status", "level": "ad", "id": i, "status": "paused"}, {"ad_id": i}))
        return changes, skipped
    if view == "ads":
        tf = text_field or "primary_text"
        if tf not in ("primary_text", "headline"):
            raise ValueError("text_field must be primary_text or headline")
        new = _check_text(new, None, tf)
        for m in members:
            changes.append(({"type": "meta_copy", "level": "ad", "id": m["owner_id"], tf: new}, m))
        return changes, skipped
    lst = M_FEED_LIST.get(row["field"])
    if not lst:
        raise ValueError("only text options (primary text, headline, description) can be changed from here")
    old = _norm(row["text"])
    if action == "replace_text":
        new = _check_text(new, None, row["field_label"])
    for i in sorted({m["owner_id"] for m in members}):
        ad = snap["ads"].get(i) or {}
        items = list((ad.get("lists") or {}).get(lst) or [])
        hit = [k for k, x in enumerate(items) if _norm(x) == old]
        if not hit:
            skipped.append({"name": ad.get("name", i), "why": "the ad no longer has this text"})
            continue
        if action == "remove_text":
            items = [x for k, x in enumerate(items) if k not in hit]
            if not items:
                skipped.append({"name": ad.get("name", i), "why": f"it is the ad's only {row['field_label'].lower()}"})
                continue
        else:
            if any(_norm(x) == _norm(new) for x in items):
                skipped.append({"name": ad.get("name", i), "why": "the ad already has the new text"})
                continue
            items = [new if k in hit else x for k, x in enumerate(items)]
        changes.append(({"type": "meta_feed_copy", "level": "ad", "id": i, lst: items}, {"ad_id": i}))
    return changes, skipped


def _clash(platform, items, c):
    """Is there already a draft item that this change would fight with (same ad / same text list)?"""
    for it in items:
        o = it.get("change") or {}
        if o.get("type") != c["type"]:
            continue
        if platform == "google" and c["type"] == "rsa" and str(o.get("local_id")) == str(c["local_id"]) \
                and bool(o.get("headlines")) == bool(c.get("headlines")):
            return True
        if platform == "google" and c["type"] == "pmax_asset" and o.get("asset_group") == c["asset_group"] \
                and o.get("field") == c["field"] and bool(o.get("remove")) == bool(c.get("remove")) and o.get("texts") == c.get("texts"):
            return True
        if platform == "meta" and str(o.get("id")) == str(c.get("id")):
            return True
    return False


ACTIONS = ("pause_ad", "replace_text", "remove_text")


def add_to_draft(account, view, key, action, new_text=None, ad_ids=None, days=None, start=None, end=None, grouped=True,
                 text_field=None, by="you", note=None, query=None):
    """One row of the Creatives view -> changes in the account's Ads draft (never pushed). ad_ids: only these ads /
    asset groups of the row (default every one that allows the action). -> {added, skipped, draft_account}"""
    if action not in ACTIONS:
        raise ValueError(f"action must be one of {', '.join(ACTIONS)}")
    a = find_account(account)
    win = window(days, start, end)
    snap = cached(a["id"], win["key"])
    if not snap:
        raise ValueError("no creative data for this account and date range: open the view first")
    v = account_view(a, snap, view, grouped, 0)
    row = _find_row(v, key)
    want = {str(i) for i in ad_ids} if ad_ids else None
    members = [m for m in _members(snap, v["view"]) if (_key(*m["gkey"]) if grouped else _key(*m["gkey"], m["owner_id"], m.get("asset_id"))) == key
               and action in m["actions"] and (want is None or str(m["owner_id"]) in want)]
    if not members:
        raise ValueError(f"{action.replace('_', ' ')} isn't possible for this row" + (f": {row['why_not']}" if row.get("why_not") else ""))
    if a["platform"] == "google":
        changes, skipped = _google_changes(row, action, new_text, members, v["view"], a["id"], query)
        dkey = core.resolve(a["key"])
    else:
        import meta_ads  # noqa: PLC0415
        meta_ads.use(a["key"])
        changes, skipped = _meta_changes(row, action, new_text, members, v["view"], snap, text_field)
        dkey = meta_ads.acct()
    items = core.draft(dkey)["items"]
    keep = []
    for c, m in changes:
        if _clash(a["platform"], items, c):
            skipped.append({"name": m.get("where") or m.get("ad_id") or "", "why": "the draft already has a change like this for it"})
        else:
            keep.append(c)
    if not keep:
        return {"added": [], "skipped": skipped, "draft_account": a["key"], "message": "Nothing added."}
    what = {"pause_ad": "pause the ad", "replace_text": "replace text", "remove_text": "remove text"}[action]
    stats = f"{row['impressions']:,} impressions, CTR {row['ctr'] if row['ctr'] is not None else '-'}%, {row['conversions'] or 0:g} conversions"
    text = f"{NOTE} {what} ({row['field_label'].lower()} \"{row['text'][:60]}\"; {v['window']}: {stats})." + (f" {note}" if note else "")
    if a["platform"] == "meta":
        got = meta_ads.add(keep, by=by, note=text)
    else:
        got = []
        for c in keep:  # one at a time: a change Studio refuses (an experiment's trial campaign) must not stop the rest
            try:
                got += core.add(dkey, [c], by=by, note=text)
            except ValueError as ex:
                skipped.append({"name": " / ".join(x for x in (c.get("campaign"), c.get("ad_group") or c.get("asset_group")) if x),
                                "why": str(ex).replace("Not added to the draft. ", "")[:300]})
        if not got:
            return {"added": [], "skipped": skipped, "draft_account": a["key"], "message": "Nothing added."}
    added =[{"id": it["id"], "summary": it.get("summary"), "problems": it.get("problems"), "warnings": it.get("warnings")} for it in got]
    return {"added": added, "skipped": skipped, "draft_account": a["key"]}
