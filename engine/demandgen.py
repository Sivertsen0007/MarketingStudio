"""Demand Gen and YouTube video ads: draft change types, their checks, the Google Ads API operations Push live sends,
and the reads behind the Videos tab.

Why Demand Gen: Video campaigns are read only in the Google Ads API (MUTATE_NOT_ALLOWED), so video ads on YouTube are
made as Demand Gen video responsive ads (DemandGenVideoResponsiveAdInfo). Channel controls can keep a Demand Gen ad
group on YouTube only (in-stream, in-feed, Shorts).

Change types (all live only: Google Ads Editor can't hold them, Send to Editor leaves them out):
  dg_campaign   new: budget (daily, not shared), bidding (maximize_conversions, maximize_clicks, target_cpa), first ad
                group with locations, languages and channel controls. Always created Paused.
                edit: status, budget, target_cpa of an existing Demand Gen campaign.
  dg_ad_group   new: in an existing (or same-push) Demand Gen campaign, with locations, languages, channels.
                edit: status, channels.
  dg_video_ad   new: Demand Gen video responsive ad (YouTube videos by id, headlines, long headlines, descriptions,
                business name, square logo, final URL, call to action). edit (edit + ad_id): replace any of those.
  dg_ad         status (paused / enabled) of any existing Demand Gen ad (video, image, carousel ...).
  video_asset   add YouTube videos (by id or URL) to the account's asset library.
  video_upload  a file on this Mac to upload to YouTube (youtube_upload.py). Never part of Push live or Send to
                Editor: it only runs from its own Upload button (gated like a push), never from MCP.

Everything goes through live.py: the preview is googleAds:mutate with validateOnly, Push live sends the same
operations in one atomic request, Undo reverses what can be reversed (new assets stay in the account's library).
Reads here are GAQL only (googleAds:searchStream).
"""
import base64, datetime as dt, json, re, struct
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import core

TYPES = ("dg_campaign", "dg_ad_group", "dg_video_ad", "dg_ad", "video_asset", "video_upload")
PUSH_TYPES = TYPES[:-1]  # video_upload never goes in a push
BIDDING = {"maximize_conversions": "Maximise conversions", "maximize_clicks": "Maximise clicks", "target_cpa": "Target CPA"}
CHANNELS = ("youtube_in_stream", "youtube_in_feed", "youtube_shorts", "discover", "gmail", "display", "maps")
CHANNEL_LABEL = {"youtube_in_stream": "YouTube in-stream", "youtube_in_feed": "YouTube in-feed", "youtube_shorts": "YouTube Shorts",
                 "discover": "Discover", "gmail": "Gmail", "display": "Display Network", "maps": "Maps"}
PRESETS = {"all": "All channels", "google_owned": "Google-owned channels only", "youtube": "YouTube only"}
YOUTUBE = ("youtube_in_stream", "youtube_in_feed", "youtube_shorts")
CTA = ("learn_more", "book_now", "get_quote", "sign_up", "contact_us", "subscribe", "download", "shop_now", "buy_now",
       "apply_now", "order_now", "see_more", "start_now", "visit_site", "watch_now")
# Google's limits for a Demand Gen video responsive ad: (min, max, max chars). Fewer than 3 texts is allowed but Google
# recommends 3 or more of each, so it is a warning.
LIMITS = {"videos": (1, 5, None), "headlines": (1, 5, 40), "long_headlines": (1, 5, 90), "descriptions": (1, 5, 90)}
BUSINESS_NAME = 25
BREADCRUMB = 15
RECOMMENDED = 3
# Demand Gen's minimum daily budget per currency (Google's currency_constant.demand_gen_info, read 2026-11-17). The
# draft checks these; Push live reads the live minimum again for the account's currency.
MIN_BUDGET = {"GBP": 4.0, "USD": 5.0, "EUR": 5.0}
LANG = {"en": 1000, "de": 1001, "fr": 1002, "es": 1003, "it": 1004, "nl": 1010, "da": 1009, "sv": 1015, "no": 1013}
UPLOAD_EXT = (".mp4", ".mov", ".m4v", ".avi", ".wmv", ".flv", ".mpg", ".mpeg", ".webm", ".3gp", ".mkv")
MAX_UPLOAD = 256 * 1024 ** 3
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
LOGO_EXT = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".gif": "GIF"}
MAX_LOGO = 5 * 1024 * 1024


def is_dg(change):
    return (change or {}).get("type") in TYPES


# ---------- small helpers ----------

def video_id(v):
    """A YouTube video id from an id or a YouTube URL (watch, youtu.be, shorts, embed, live). ValueError if it isn't one."""
    s = str(v or "").strip()
    if VIDEO_ID.fullmatch(s):
        return s
    try:
        u = urlparse(s if "://" in s else "https://" + s)
    except ValueError:
        u = None
    host = (u.hostname or "").lower() if u else ""
    if host.startswith("www."):
        host = host[4:]
    if host.startswith("m."):
        host = host[2:]
    cand = ""
    if host == "youtu.be":
        cand = u.path.strip("/").split("/")[0]
    elif host in ("youtube.com", "music.youtube.com", "youtube-nocookie.com"):
        if u.path == "/watch":
            cand = (parse_qs(u.query).get("v") or [""])[0]
        else:
            parts = [p for p in u.path.split("/") if p]
            if len(parts) >= 2 and parts[0] in ("shorts", "embed", "live", "v"):
                cand = parts[1]
    if VIDEO_ID.fullmatch(cand or ""):
        return cand
    raise ValueError(f"not a YouTube video id or URL: {s[:80]!r}")


def _videos(c):
    out, bad = [], []
    for v in c.get("videos") or []:
        raw = v.get("id") if isinstance(v, dict) else v
        try:
            out.append(video_id(raw))
        except ValueError as ex:
            bad.append(str(ex))
    return out, bad


def _texts(v):
    if v is None:
        return None
    if isinstance(v, str):
        return [v]
    return [x.get("text") if isinstance(x, dict) else x for x in v]


def _status(v):
    s = str(v or "").strip().lower()
    return {"enabled": "ENABLED", "paused": "PAUSED"}.get(s)


def _langs(c):
    raw = c.get("languages") or []
    items = raw if isinstance(raw, list) else str(raw).split(",")
    return [x.strip().lower() for x in items if str(x).strip()]


def channels(c):
    """The channel controls a change asks for -> (API dict, label). None when the change doesn't set them."""
    ch = c.get("channels")
    if ch in (None, "", []):
        return None, ""
    if isinstance(ch, str):
        if ch == "all":
            return {"channelStrategy": "ALL_CHANNELS"}, PRESETS["all"]
        if ch == "google_owned":
            return {"channelStrategy": "ALL_OWNED_AND_OPERATED_CHANNELS"}, PRESETS["google_owned"]
        if ch == "youtube":
            ch = list(YOUTUBE)
        else:
            ch = [x.strip() for x in ch.split(",")]
    on = [str(x).strip() for x in ch if str(x).strip() in CHANNELS]
    sel = {_camel(k): k in on for k in CHANNELS}
    label = PRESETS["youtube"] if set(on) == set(YOUTUBE) else ", ".join(CHANNEL_LABEL[k] for k in CHANNELS if k in on)
    return {"selectedChannels": sel}, label


def _camel(k):
    a, *b = k.split("_")
    return a + "".join(x.capitalize() for x in b)


def currency(acct):
    """The account's currency as Studio cached it (account_currency.json), or ''. Never asks Google."""
    try:
        return (json.loads((core.STUDIO_DIR / "account_currency.json").read_text()).get(str(acct)) or "").upper()
    except (OSError, ValueError, AttributeError):
        return ""


def _num(v, what):
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{what} must be a number")
    if x != x or x <= 0:
        raise ValueError(f"{what} must be more than 0")
    return x


def image_size(data):
    """(format, width, height) of PNG, JPEG or GIF bytes; ValueError otherwise."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        w, h = struct.unpack(">II", data[16:24])
        return "PNG", w, h
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        w, h = struct.unpack("<HH", data[6:10])
        return "GIF", w, h
    if data[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            m = data[i + 1]
            if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:
                i += 2
                continue
            seg = struct.unpack(">H", data[i + 2:i + 4])[0]
            if m in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return "JPEG", w, h
            i += 2 + seg
    raise ValueError("the logo must be a PNG, JPEG or GIF image")


def _logo_file_problems(path):
    p = Path(str(path or "")).expanduser()
    if not p.is_absolute():
        return ["logo_file must be a full path on this Mac"]
    if p.suffix.lower() not in LOGO_EXT:
        return ["logo_file must be a .png, .jpg or .gif"]
    if not p.is_file():
        return [f"logo file not found on this Mac: {p.name}"]
    if p.stat().st_size > MAX_LOGO:
        return ["logo file is over 5 MB"]
    try:
        _, w, h = image_size(p.read_bytes()[:65536] if p.stat().st_size > 65536 else p.read_bytes())
    except ValueError as ex:
        return [str(ex)]
    if w < 128 or h < 128 or abs(w / h - 1) > 0.01:
        return [f"the logo must be square (1:1) and at least 128x128; this one is {w}x{h}"]
    return []


# ---------- checks (draft time; no Google calls) ----------

def check(c, acct=None):
    """-> (problems that block, warnings)."""
    t = c.get("type")
    try:
        p, w = {"dg_campaign": _check_campaign, "dg_ad_group": _check_ad_group, "dg_video_ad": _check_ad, "dg_ad": _check_status_ad,
                "video_asset": _check_video_asset, "video_upload": _check_upload}[t](c, acct)
    except (TypeError, ValueError, AttributeError) as ex:
        return [f"invalid change: {ex}"], []
    return list(dict.fromkeys(p)), list(dict.fromkeys(w))


def _need(c, *keys):
    return [f"{k} is missing" for k in keys if not str(c.get(k) or "").strip()]


def _check_targeting(c):
    p = []
    for loc in c.get("locations") or []:
        lid = loc.get("id") if isinstance(loc, dict) else loc
        if not str(lid or "").isdigit():
            p.append(f"location id must be a number (got {lid!r})")
    for lang in _langs(c):
        if lang not in LANG:
            p.append(f"language {lang!r} not known: use {', '.join(LANG)}")
    ch = c.get("channels")
    if ch not in (None, "", []):
        if isinstance(ch, str) and ch in PRESETS:
            pass
        else:
            items = [str(x).strip() for x in (ch if isinstance(ch, list) else str(ch).split(",")) if str(x).strip()]
            bad = [x for x in items if x not in CHANNELS]
            if bad:
                p.append(f"channels: unknown {', '.join(bad)}; use all, google_owned, youtube or a list of {', '.join(CHANNELS)}")
            elif not items:
                p.append("channels: pick at least one channel")
    return p


def _check_campaign(c, acct):
    p, w = _need(c, "campaign"), []
    if c.get("new"):
        p += _need(c, "ad_group")
        try:
            b = _num(c.get("budget"), "budget")
            cur = currency(acct) if acct else ""
            if cur in MIN_BUDGET and b < MIN_BUDGET[cur]:
                p.append(f"budget {b:.2f} {cur}/day is under Demand Gen's minimum of {MIN_BUDGET[cur]:.2f} {cur}/day")
            elif cur not in MIN_BUDGET:
                w.append("Demand Gen has a minimum daily budget (about 5 USD); the live preview checks it")
        except ValueError as ex:
            p.append(str(ex))
        bid = str(c.get("bidding") or "maximize_conversions")
        if bid not in BIDDING:
            p.append(f"bidding must be one of {', '.join(BIDDING)}")
        if bid == "target_cpa":
            try:
                _num(c.get("target_cpa"), "target_cpa")
            except ValueError as ex:
                p.append(str(ex))
        elif c.get("target_cpa") not in (None, ""):
            if bid == "maximize_clicks":
                p.append("target_cpa only goes with maximize_conversions or target_cpa bidding")
            else:
                try:
                    _num(c.get("target_cpa"), "target_cpa")
                except ValueError as ex:
                    p.append(str(ex))
        if c.get("status") and _status(c["status"]) != "PAUSED":
            w.append("new campaigns are always created Paused: enable it after checking it in Google Ads")
        if not c.get("locations"):
            w.append("no locations: the ad group targets every country")
        if not _langs(c):
            w.append("no languages: the ad group targets every language")
        for k in ("start_date", "end_date"):
            if c.get(k) and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(c[k])):
                p.append(f"{k} must be YYYY-MM-DD")
        p += _check_targeting(c)
        return p, w
    if not any(c.get(k) not in (None, "") for k in ("status", "budget", "target_cpa")):
        p.append("nothing to change: give status, budget or target_cpa")
    if c.get("status") and not _status(c["status"]):
        p.append("status must be enabled or paused")
    for k in ("budget", "target_cpa"):
        if c.get(k) not in (None, ""):
            try:
                v = _num(c[k], k)
                cur = currency(acct) if acct else ""
                if k == "budget" and cur in MIN_BUDGET and v < MIN_BUDGET[cur]:
                    p.append(f"budget {v:.2f} {cur}/day is under Demand Gen's minimum of {MIN_BUDGET[cur]:.2f} {cur}/day")
            except ValueError as ex:
                p.append(str(ex))
    return p, w


def _check_ad_group(c, acct):
    p, w = _need(c, "campaign", "ad_group"), []
    if c.get("status") and not _status(c["status"]):
        p.append("status must be enabled or paused")
    if c.get("new"):
        p += _check_targeting(c)
        if not c.get("locations") and not _langs(c):
            w.append("no locations or languages: the ad group uses the campaign's, or every country and language")
    else:
        if c.get("locations") or c.get("languages"):
            p.append("locations and languages of an existing ad group are changed in Google Ads")
        if not (c.get("status") or c.get("channels")):
            p.append("nothing to change: give status or channels")
        p += _check_targeting({k: v for k, v in c.items() if k == "channels"})
    return p, w


def _check_texts(c, rows, new):
    p, w = [], []
    for k, (lo, hi, chars) in LIMITS.items():
        if k == "videos":
            continue
        vals = _texts(c.get(k))
        if vals is None:
            if new:
                p.append(f"{k.replace('_', ' ')}: give {lo} to {hi}")
            continue
        if not all(isinstance(x, str) and x.strip() for x in vals):
            p.append(f"{k.replace('_', ' ')}: every text must be non-empty")
            continue
        if not lo <= len(vals) <= hi:
            p.append(f"{k.replace('_', ' ')}: {len(vals)} given, Google allows {lo} to {hi}")
        if len(set(x.strip().lower() for x in vals)) != len(vals):
            p.append(f"{k.replace('_', ' ')}: the same text is listed twice")
        if len(vals) < RECOMMENDED:
            w.append(f"{k.replace('_', ' ')}: Google recommends at least {RECOMMENDED}")
        label = {"headlines": "Short headline", "long_headlines": "Long headline", "descriptions": "Video description"}[k]
        for i, x in enumerate(vals, 1):
            if len(x) > chars:
                p.append(f"{label} {i}: {len(x)} chars > {chars}")
            rows.append({f"{label} {i}": x})
    if c.get("business_name") is not None or new:
        bn = str(c.get("business_name") or "")
        if not bn.strip():
            p.append("business_name is missing")
        elif len(bn) > BUSINESS_NAME:
            p.append(f"business name: {len(bn)} chars > {BUSINESS_NAME}")
        rows.append({"Business name": bn})
    for k in ("breadcrumb1", "breadcrumb2"):
        if c.get(k) and len(str(c[k])) > BREADCRUMB:
            p.append(f"{k}: {len(str(c[k]))} chars > {BREADCRUMB}")
    return p, w


def _check_ad(c, acct):
    p, w = _need(c, "campaign", "ad_group"), []
    new = not c.get("edit")
    if not new and not str(c.get("ad_id") or "").isdigit():
        p.append("ad_id (the Google ad id) is needed to edit an ad")
    if not new and not any(c.get(k) not in (None, "", []) for k in
                           ("videos", "headlines", "long_headlines", "descriptions", "business_name", "logo_asset_id", "logo_file",
                            "final_url", "call_to_action", "status", "breadcrumb1", "breadcrumb2")):
        p.append("nothing to change")
    vids, bad = _videos(c)
    p += bad
    if c.get("videos") is not None or new:
        lo, hi, _ = LIMITS["videos"]
        if not lo <= len(vids) + len(bad) <= hi:
            p.append(f"videos: {len(vids) + len(bad)} given, Google allows {lo} to {hi}")
        if len(set(vids)) != len(vids):
            p.append("videos: the same video is listed twice")
    rows = []
    tp, tw = _check_texts(c, rows, new)
    p, w = p + tp, w + tw
    if new or c.get("logo_asset_id") or c.get("logo_file"):
        if c.get("logo_asset_id") and c.get("logo_file"):
            p.append("give logo_asset_id or logo_file, not both")
        elif c.get("logo_asset_id"):
            if not str(c["logo_asset_id"]).isdigit():
                p.append("logo_asset_id must be the image asset's id (a number)")
        elif c.get("logo_file"):
            p += _logo_file_problems(c["logo_file"])
        else:
            p.append("logo: pick a square logo image (logo_asset_id or logo_file); Google requires one")
    fu = c.get("final_url")
    if new or fu is not None:
        if not re.match(r"https?://[^\s/]+\.[^\s]+", str(fu or "")):
            p.append("final_url must be a full https:// address")
        rows.append({"Final URL": fu})
    cta = c.get("call_to_action")
    if cta not in (None, "") and str(cta).lower() not in CTA:
        p.append(f"call_to_action must be one of {', '.join(CTA)}")
    if c.get("status") and not _status(c["status"]):
        p.append("status must be enabled or paused")
    if rows:
        import gae  # noqa: PLC0415
        p += [x.split(": ", 1)[1] if x.startswith("row ") else x for x in gae.lint(rows)]
        w += core._warn(rows)
    return p, w


def _check_status_ad(c, acct):
    p = _need(c, "campaign", "ad_group")
    if not str(c.get("ad_id") or "").isdigit():
        p.append("ad_id (the Google ad id) is missing")
    if not _status(c.get("status")):
        p.append("status must be enabled or paused")
    return p, []


def _check_video_asset(c, acct):
    vids, bad = _videos(c)
    p = bad + ([] if vids or bad else ["videos: give one or more YouTube video ids or URLs"])
    if len(set(vids)) != len(vids):
        p.append("the same video is listed twice")
    names = [v.get("name") for v in c.get("videos") or [] if isinstance(v, dict) and v.get("name")]
    p += [f"name over 100 chars: {n[:30]}..." for n in names if len(str(n)) > 100]
    return p, []


def upload_problems(c, on_this_mac=True):
    p = []
    title, desc = str(c.get("title") or ""), str(c.get("description") or "")
    if not title.strip():
        p.append("title is missing")
    if len(title) > 100:
        p.append(f"title: {len(title)} chars > 100")
    if len(desc) > 5000:
        p.append(f"description: {len(desc)} chars > 5000")
    if re.search(r"[<>]", title + desc):
        p.append("title and description can't contain < or >")
    if c.get("ai_disclosure") not in (None, "", "non_generative", "generative"):
        p.append("ai_disclosure must be non_generative or generative")
    if c.get("channel_id"):
        p.append("uploads to your own brand channel need that channel's own sign-in, which Studio doesn't do: leave channel_id "
                 "out to upload to the account's Google-managed channel (unlisted)")
    if c.get("privacy") not in (None, "", "unlisted"):
        p.append("the Google-managed channel only takes unlisted videos")
    f = Path(str(c.get("file") or "")).expanduser()
    if not c.get("file"):
        p.append("file is missing")
    elif not f.is_absolute():
        p.append("file must be a full path on this Mac")
    elif f.suffix.lower() not in UPLOAD_EXT:
        p.append(f"file must be a video ({', '.join(UPLOAD_EXT)})")
    elif on_this_mac:
        if not f.is_file():
            p.append(f"file not found on this Mac: {f.name}")
        elif f.stat().st_size == 0:
            p.append("the file is empty")
        elif f.stat().st_size > MAX_UPLOAD:
            p.append("the file is over YouTube's 256 GB limit")
    return p


def _check_upload(c, acct):
    p = upload_problems(c, on_this_mac=False)
    f = Path(str(c.get("file") or "")).expanduser()
    w = []
    if not p and not f.is_file():
        w.append(f"file not on this Mac ({f.name}): only the Mac that has it can upload it")
    elif not p and f.stat().st_size == 0:
        p.append("the file is empty")
    return p, w


# ---------- summaries ----------

def summary(c):
    t = c.get("type")
    if t == "dg_campaign" and c.get("new"):
        ch = channels(c)[1]
        return (f"New Demand Gen campaign {c.get('campaign')} ({BIDDING.get(c.get('bidding') or 'maximize_conversions', c.get('bidding'))}, "
                f"{c.get('budget')}/day, Paused) with ad group {c.get('ad_group')}" + (f", {ch}" if ch else ""))
    if t == "dg_campaign":
        bits = [f"status {c['status']}" if c.get("status") else "", f"budget {c['budget']}/day" if c.get("budget") not in (None, "") else "",
                f"target CPA {c['target_cpa']}" if c.get("target_cpa") not in (None, "") else ""]
        return f"Demand Gen campaign {c.get('campaign')}: " + ", ".join(b for b in bits if b)
    if t == "dg_ad_group":
        ch = channels(c)[1]
        if c.get("new"):
            return f"New Demand Gen ad group {c.get('campaign')} / {c.get('ad_group')}" + (f" ({ch})" if ch else "")
        bits = [f"status {c['status']}" if c.get("status") else "", f"channels: {ch}" if ch else ""]
        return f"Demand Gen ad group {c.get('campaign')} / {c.get('ad_group')}: " + ", ".join(b for b in bits if b)
    if t == "dg_video_ad":
        vids, _ = _videos(c)
        if c.get("edit"):
            keys = [k.replace("_", " ") for k in ("videos", "headlines", "long_headlines", "descriptions", "business_name", "final_url",
                                                   "call_to_action", "status") if c.get(k) not in (None, "", [])]
            if c.get("logo_asset_id") or c.get("logo_file"):
                keys.append("logo")
            return f"Edit Demand Gen video ad {c.get('ad_id')} in {c.get('campaign')} / {c.get('ad_group')}: " + ", ".join(keys)
        h = _texts(c.get("headlines")) or [""]
        return f"New Demand Gen video ad in {c.get('campaign')} / {c.get('ad_group')}: {h[0]} ({len(vids)} video{'s' if len(vids) != 1 else ''})"
    if t == "dg_ad":
        return f"Demand Gen ad {c.get('ad_id')} in {c.get('campaign')} / {c.get('ad_group')}: {c.get('status')}"
    if t == "video_asset":
        vids, _ = _videos(c)
        return "Add YouTube video" + ("s" if len(vids) != 1 else "") + " to the asset library: " + ", ".join(vids)
    if t == "video_upload":
        return f"Upload to YouTube (Google-managed channel, unlisted): {c.get('title')} ({c.get('file_name') or Path(str(c.get('file') or '')).name})"
    return json.dumps(c, ensure_ascii=False)


# ---------- push: draft change -> Google Ads operations ----------
# Uses live.py's op helpers; each op: {op, undo, line, spend, permanent, created}.

def _live_mod():
    import live  # noqa: PLC0415 - live imports core, which imports this module
    return live


def _q(ids, key, gaql):
    return ids._q(key, gaql)


def _min_budget(ids):
    """The account's live Demand Gen minimum (currency, amount) from GAQL; (None, None) if Google doesn't say."""
    rows = _q(ids, "dg_currency", "SELECT customer.currency_code FROM customer LIMIT 1")
    cur = (rows[0] if rows else {}).get("customer", {}).get("currencyCode", "")
    if not re.fullmatch(r"[A-Z]{3}", cur or ""):
        return None, None
    rows = _q(ids, f"dg_min_{cur}", "SELECT currency_constant.code, currency_constant.demand_gen_info.minimum_budget_per_day_micros "
                                    f"FROM currency_constant WHERE currency_constant.code = '{cur}'")
    m = (rows[0] if rows else {}).get("currencyConstant", {}).get("demandGenInfo", {}).get("minimumBudgetPerDayMicros")
    return cur, (int(m) if m not in (None, "") else None)


def _check_min(ids, amount):
    cur, m = _min_budget(ids)
    if m is not None and int(round(amount * 1e6)) < m:
        raise ValueError(f"budget {amount:.2f} {cur}/day is under Demand Gen's minimum of {m / 1e6:.2f} {cur}/day")


def _dg_campaign(ids, name):
    """An existing campaign that must be Demand Gen -> (resource, row)."""
    camp, ref = ids.campaign(name)
    if ref is None:
        return camp, None
    rows = _q(ids, f"dgc{camp}", "SELECT campaign.advertising_channel_type, campaign.status, campaign.campaign_budget, "
                                 "campaign.bidding_strategy_type, campaign.target_cpa.target_cpa_micros, "
                                 "campaign.maximize_conversions.target_cpa_micros, campaign_budget.amount_micros, "
                                 f"campaign_budget.explicitly_shared FROM campaign WHERE campaign.resource_name = '{camp}'")
    if not rows:
        raise ValueError(f'campaign "{name}" not found live in Google Ads')
    if rows[0]["campaign"].get("advertisingChannelType") != "DEMAND_GEN":
        raise ValueError(f'"{name}" is not a Demand Gen campaign')
    return camp, rows[0]


def _targeting_ops(ids, g, c, live):
    out = []
    for lang in _langs(c):
        out.append(live._create("adGroupCriterionOperation", {"adGroup": g, "language": {"languageConstant": f"languageConstants/{LANG[lang]}"}},
                                f"language {lang}"))
    for loc in c.get("locations") or []:
        lid = loc.get("id") if isinstance(loc, dict) else loc
        name = loc.get("name", lid) if isinstance(loc, dict) else lid
        out.append(live._create("adGroupCriterionOperation", {"adGroup": g, "location": {"geoTargetConstant": f"geoTargetConstants/{int(lid)}"}},
                                f"location {name}"))
    return out


def _new_ad_group(ids, camp, c, status, live):
    g = ids.tmp("adGroups")
    body = {"resourceName": g, "name": c["ad_group"], "campaign": camp, "status": status}
    ctl, label = channels(c)
    if ctl:
        body["demandGenAdGroupSettings"] = {"channelControls": ctl}
    out = [live._create("adGroupOperation", body, f'new Demand Gen ad group "{c["ad_group"]}" ({status}' + (f", {label})" if label else ")"))]
    ids.new[("g", c["campaign"], c["ad_group"])] = g
    return out + _targeting_ops(ids, g, c, live)


def ops(ids, c, enable_new):
    live = _live_mod()
    t, a = c["type"], ids.acct
    new_status = "ENABLED" if enable_new else "PAUSED"
    out = []

    if t == "dg_campaign" and c.get("new"):
        amount = float(c["budget"])
        _check_min(ids, amount)
        budget, camp = ids.tmp("campaignBudgets"), ids.tmp("campaigns")
        out.append(live._create("campaignBudgetOperation", {"resourceName": budget, "name": f"{c['campaign']} budget",
                                                            "amountMicros": str(int(round(amount * 1e6))), "deliveryMethod": "STANDARD",
                                                            "explicitlyShared": False}, f"new budget {amount:.2f}/day", undoable=False))
        bid = c.get("bidding") or "maximize_conversions"
        cpa = {"targetCpaMicros": str(int(round(float(c["target_cpa"]) * 1e6)))} if c.get("target_cpa") not in (None, "") else {}
        scheme = {"maximize_conversions": {"maximizeConversions": cpa}, "target_cpa": {"targetCpa": cpa},
                  "maximize_clicks": {"targetSpend": {}}}[bid]
        body = {"resourceName": camp, "name": c["campaign"], "status": "PAUSED", "advertisingChannelType": "DEMAND_GEN",
                "campaignBudget": budget, **scheme, "containsEuPoliticalAdvertising": "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING"}
        if c.get("start_date"):
            body["startDate"] = c["start_date"]
        if c.get("end_date"):
            body["endDate"] = c["end_date"]
        out.append(live._create("campaignOperation", body, f'new Demand Gen campaign "{c["campaign"]}" (PAUSED, {BIDDING[bid]})'))
        ids.new[("c", c["campaign"])] = camp
        return out + _new_ad_group(ids, camp, c, new_status, live)

    if t == "dg_campaign":
        camp, row = _dg_campaign(ids, c["campaign"])
        if row is None:
            raise ValueError("a campaign created in the same push can't be edited: push first")
        lc, lb = row["campaign"], row.get("campaignBudget", {})
        if c.get("status"):
            new = _status(c["status"])
            out.append(live._upd("campaignOperation", camp, {"status": new}, "status", {"status": lc["status"]},
                                 f"campaign status {lc['status']}", new,
                                 spend="enables a campaign" if new == "ENABLED" and lc["status"] != "ENABLED" else None))
        if c.get("budget") not in (None, ""):
            if lb.get("explicitlyShared"):
                raise ValueError(f'"{c["campaign"]}" uses a shared budget - change it in Google Ads')
            new_b = float(c["budget"])
            _check_min(ids, new_b)
            old, new = int(lb.get("amountMicros", 0)), int(round(new_b * 1e6))
            pct = f" (+{(new - old) / old * 100:.0f}%)" if old and new > old else ""
            out.append(live._upd("campaignBudgetOperation", lc["campaignBudget"], {"amountMicros": str(new)}, "amountMicros",
                                 {"amountMicros": str(old)}, f"budget {live._money(old)}/day", f"{live._money(new)}/day",
                                 spend=f"budget raise{pct}" if new > old else None))
        if c.get("target_cpa") not in (None, ""):
            st = lc.get("biddingStrategyType")
            path = {"TARGET_CPA": "targetCpa", "MAXIMIZE_CONVERSIONS": "maximizeConversions"}.get(st)
            if not path:
                raise ValueError(f'"{c["campaign"]}" bids with {st}: a target CPA needs Target CPA or Maximise conversions bidding')
            old = int(lc.get(path, {}).get("targetCpaMicros", 0) or 0)
            new = int(round(float(c["target_cpa"]) * 1e6))
            out.append(live._upd("campaignOperation", camp, {path: {"targetCpaMicros": str(new)}}, f"{path}.targetCpaMicros",
                                 {path: {"targetCpaMicros": str(old)}}, f"target CPA {live._money(old) if old else 'none'}",
                                 live._money(new), spend="target CPA raise" if new > old else None))
        return out

    if t == "dg_ad_group" and c.get("new"):
        camp, row = _dg_campaign(ids, c["campaign"])
        if row is not None and (c.get("locations") or _langs(c)):
            have = _q(ids, f"dgct{camp}", "SELECT campaign_criterion.type FROM campaign_criterion WHERE campaign.resource_name = "
                                          f"'{camp}' AND campaign_criterion.type IN (LOCATION, LANGUAGE) AND campaign_criterion.status != 'REMOVED'")
            if have:
                raise ValueError(f'"{c["campaign"]}" sets locations and languages for the whole campaign, so its ad groups can\'t have '
                                 "their own: leave locations and languages out (the new ad group uses the campaign's)")
        return _new_ad_group(ids, camp, c, new_status, live)

    if t == "dg_ad_group":
        _dg_campaign(ids, c["campaign"])
        g, gref = ids.ad_group(c["campaign"], c["ad_group"])
        if gref is None:
            raise ValueError("an ad group created in the same push can't be edited: push first")
        rows = _q(ids, f"dgg{g}", "SELECT ad_group.status, ad_group.demand_gen_ad_group_settings.channel_controls.channel_strategy, "
                                  + ", ".join(f"ad_group.demand_gen_ad_group_settings.channel_controls.selected_channels.{k}" for k in CHANNELS)
                                  + f" FROM ad_group WHERE ad_group.resource_name = '{g}'")
        if not rows:
            raise ValueError(f'ad group "{c["ad_group"]}" not found live in Google Ads')
        lg = rows[0]["adGroup"]
        fields, before, mask, undo_mask, was, now, spend = {}, {}, [], [], [], [], None
        if c.get("status"):
            new = _status(c["status"])
            fields["status"], before["status"] = new, lg["status"]
            mask.append("status")
            undo_mask.append("status")
            was.append(f"status {lg['status']}")
            now.append(new)
            if new == "ENABLED" and lg["status"] != "ENABLED":
                spend = "enables an ad group"
        ctl, label = channels(c)
        if ctl:
            cur = lg.get("demandGenAdGroupSettings", {}).get("channelControls", {})
            if cur.get("selectedChannels"):
                old = {"selectedChannels": {_camel(k): bool(cur["selectedChannels"].get(_camel(k))) for k in CHANNELS}}
            else:
                old = {"channelStrategy": cur.get("channelStrategy") or "ALL_CHANNELS"}
            fields["demandGenAdGroupSettings"] = {"channelControls": ctl}
            before["demandGenAdGroupSettings"] = {"channelControls": old}
            mask.append(_ch_mask(ctl))
            undo_mask.append(_ch_mask(old))
            was.append("channels " + _ch_label(old))
            now.append(label)
        op = live._upd("adGroupOperation", g, fields, ",".join(mask), before, f'ad group "{c["ad_group"]}" ' + ", ".join(was),
                       ", ".join(now), spend=spend)
        op["undo"]["adGroupOperation"]["updateMask"] = ",".join(undo_mask)
        return [op]

    if t == "dg_video_ad" and not c.get("edit"):
        g, _ = ids.ad_group(c["campaign"], c["ad_group"])
        _dg_campaign(ids, c["campaign"])
        ad = {"name": str(c.get("name") or f"{c['ad_group']} video ad {dt.date.today():%Y-%m-%d}")[:255], "finalUrls": [c["final_url"]],
              "demandGenVideoResponsiveAd": _ad_body(ids, c, out, live)}
        out.append(live._create("adGroupAdOperation", {"adGroup": g, "status": new_status, "ad": ad},
                                f'new Demand Gen video ad in "{c["ad_group"]}" ({new_status})'))
        return out

    if t in ("dg_video_ad", "dg_ad"):
        g, gref = ids.ad_group(c["campaign"], c["ad_group"])
        if gref is None:
            raise ValueError("ads of an ad group created in the same push can't be edited")
        _dg_campaign(ids, c["campaign"])
        res = f"customers/{a}/adGroupAds/{live._id(g)}~{int(c['ad_id'])}"
        sel = "ad_group_ad.status, ad_group_ad.ad.type, ad_group_ad.ad.final_urls"
        if t == "dg_video_ad":
            sel += ", " + ", ".join(f"ad_group_ad.ad.demand_gen_video_responsive_ad.{k}" for k in
                                    ("headlines", "long_headlines", "descriptions", "business_name", "videos", "logo_images",
                                     "call_to_actions", "breadcrumb1", "breadcrumb2"))
        rows = live.query(a, f"SELECT {sel} FROM ad_group_ad WHERE ad_group_ad.resource_name = '{res}'")
        if not rows:
            raise ValueError(f'ad {c["ad_id"]} not found live in "{c["ad_group"]}"')
        lad = rows[0]["adGroupAd"]
        typ = lad["ad"].get("type", "")
        if not typ.startswith("DEMAND_GEN_"):
            raise ValueError(f"ad {c['ad_id']} is not a Demand Gen ad ({typ})")
        if t == "dg_video_ad" and typ != "DEMAND_GEN_VIDEO_RESPONSIVE_AD":
            raise ValueError(f"ad {c['ad_id']} is a {typ.replace('_', ' ').lower()}: only video ads can be edited here (pause works for all)")
        if c.get("status"):
            new = _status(c["status"])
            out.append(live._upd("adGroupAdOperation", res, {"status": new}, "status", {"status": lad["status"]},
                                 f"ad {c['ad_id']} status {lad['status']}", new,
                                 spend="enables an ad" if new == "ENABLED" and lad["status"] != "ENABLED" else None))
        if t == "dg_ad":
            return out
        cur = lad["ad"].get("demandGenVideoResponsiveAd", {})
        fields, before, mask = {}, {}, []
        body = _ad_body(ids, c, out, live, edit=True)
        for k, v in body.items():
            fields.setdefault("demandGenVideoResponsiveAd", {})[k] = v
            before.setdefault("demandGenVideoResponsiveAd", {})[k] = _clean_link(cur.get(k), k)
            mask.append(f"demandGenVideoResponsiveAd.{k}")
        if c.get("final_url"):
            fields["finalUrls"], before["finalUrls"] = [c["final_url"]], lad["ad"].get("finalUrls", [])
            mask.append("finalUrls")
        if fields:
            out.append(live._upd("adOperation", f"customers/{a}/ads/{int(c['ad_id'])}", fields, ",".join(mask), before,
                                 f"Demand Gen video ad {c['ad_id']}: " + ", ".join(m.split(".")[-1] for m in mask), "new values"))
        return out

    if t == "video_asset":
        vids, _ = _videos(c)
        names = {video_id(v.get("id")): v.get("name") for v in c.get("videos") or [] if isinstance(v, dict) and v.get("name")}
        have = _existing_videos(ids, vids)
        for v in vids:
            if v in have:
                raise ValueError(f"video {v} is already in the account's asset library")
            body = {"resourceName": ids.tmp("assets"), "youtubeVideoAsset": {"youtubeVideoId": v}}
            if names.get(v):
                body["name"] = names[v]
            out.append(live._create("assetOperation", body, f"new YouTube video asset {v}", undoable=False))
        return out

    if t == "video_upload":
        raise ValueError("an upload runs from its own Upload button, never from Push live")
    raise ValueError(f"unknown Demand Gen change type {t!r}")


def _ch_mask(ctl):
    if "channelStrategy" in ctl:
        return "demandGenAdGroupSettings.channelControls.channelStrategy"
    return ",".join(f"demandGenAdGroupSettings.channelControls.selectedChannels.{x}" for x in ctl["selectedChannels"])


def _ch_label(ctl):
    if ctl.get("channelStrategy"):
        return {"ALL_CHANNELS": PRESETS["all"], "ALL_OWNED_AND_OPERATED_CHANNELS": PRESETS["google_owned"]}.get(ctl["channelStrategy"], ctl["channelStrategy"])
    on = [k for k in CHANNELS if ctl.get("selectedChannels", {}).get(_camel(k))]
    return PRESETS["youtube"] if set(on) == set(YOUTUBE) else ", ".join(CHANNEL_LABEL[k] for k in on) or "none"


def _clean_link(v, k):
    if v is None and k in ("videos", "headlines", "longHeadlines", "descriptions", "logoImages", "callToActions"):
        return []
    if isinstance(v, list):
        return [{x: y for x, y in i.items() if x in ("text", "asset")} for i in v]
    if isinstance(v, dict):
        return {x: y for x, y in v.items() if x in ("text", "asset")}
    return v if v is not None else ("" if k.startswith("breadcrumb") else v)


def _existing_videos(ids, vids):
    """{youtube id: asset resource name} for videos already in the account's asset library."""
    if not vids:
        return {}
    quoted = ", ".join(f"'{v}'" for v in sorted(set(vids)))
    rows = _q(ids, f"yt{quoted}", "SELECT asset.resource_name, asset.youtube_video_asset.youtube_video_id FROM asset "
                                   f"WHERE asset.type = YOUTUBE_VIDEO AND asset.youtube_video_asset.youtube_video_id IN ({quoted})")
    out = {}
    for r in rows:
        out.setdefault(r["asset"]["youtubeVideoAsset"]["youtubeVideoId"], r["asset"]["resourceName"])
    return out


def _video_assets(ids, vids, out, live):
    have = _existing_videos(ids, vids)
    links = []
    for v in vids:
        res = have.get(v)
        if not res:
            res = ids.tmp("assets")
            out.append(live._create("assetOperation", {"resourceName": res, "youtubeVideoAsset": {"youtubeVideoId": v}},
                                    f"new YouTube video asset {v}", undoable=False))
            have[v] = res
        links.append({"asset": res})
    return links


def _logo(ids, c, out, live):
    if c.get("logo_asset_id"):
        aid = int(c["logo_asset_id"])
        rows = _q(ids, f"logo{aid}", "SELECT asset.resource_name, asset.type, asset.image_asset.full_size.width_pixels, "
                                     f"asset.image_asset.full_size.height_pixels FROM asset WHERE asset.id = {aid}")
        if not rows or rows[0]["asset"].get("type") != "IMAGE":
            raise ValueError(f"logo asset {aid} is not an image asset in this account")
        fs = rows[0]["asset"].get("imageAsset", {}).get("fullSize", {})
        w, h = int(fs.get("widthPixels") or 0), int(fs.get("heightPixels") or 0)
        if w and h and (w < 128 or h < 128 or abs(w / h - 1) > 0.01):
            raise ValueError(f"logo asset {aid} is {w}x{h}: the logo must be square (1:1) and at least 128x128")
        return rows[0]["asset"]["resourceName"]
    p = Path(str(c["logo_file"])).expanduser()
    probs = _logo_file_problems(p)
    if probs:
        raise ValueError(probs[0])
    data = p.read_bytes()
    res = ids.tmp("assets")
    out.append(live._create("assetOperation", {"resourceName": res, "name": f"{p.stem[:60]} logo {dt.date.today():%Y-%m-%d}",
                                               "type": "IMAGE", "imageAsset": {"data": base64.b64encode(data).decode()}},
                            f"new logo image asset {p.name}", undoable=False))
    return res


def _cta(ids, c, out, live):
    want = str(c["call_to_action"]).upper()
    rows = _q(ids, "cta_assets", "SELECT asset.resource_name, asset.call_to_action_asset.call_to_action FROM asset WHERE asset.type = CALL_TO_ACTION")
    for r in rows:
        if r["asset"].get("callToActionAsset", {}).get("callToAction") == want:
            return r["asset"]["resourceName"]
    res = ids.tmp("assets")
    out.append(live._create("assetOperation", {"resourceName": res, "callToActionAsset": {"callToAction": want}},
                            f"new call to action asset {want.replace('_', ' ').lower()}", undoable=False))
    return res


def _ad_body(ids, c, out, live, edit=False):
    """The demandGenVideoResponsiveAd fields this change sets (all of them for a new ad). Asset ops go into out."""
    body = {}
    if c.get("videos") or not edit:
        body["videos"] = _video_assets(ids, _videos(c)[0], out, live)
    for k, api in (("headlines", "headlines"), ("long_headlines", "longHeadlines"), ("descriptions", "descriptions")):
        if c.get(k) or not edit:
            body[api] = [{"text": x} for x in _texts(c[k])]
    if c.get("business_name") or not edit:
        body["businessName"] = {"text": c["business_name"]}
    if c.get("logo_asset_id") or c.get("logo_file") or not edit:
        body["logoImages"] = [{"asset": _logo(ids, c, out, live)}]
    if c.get("call_to_action"):
        body["callToActions"] = [{"asset": _cta(ids, c, out, live)}]
    for k in ("breadcrumb1", "breadcrumb2"):
        if c.get(k):
            body[k] = c[k]
    return body


# ---------- reads for the Videos tab (GAQL only, cached until a refresh) ----------

def _cache(cid):
    core.STUDIO_DIR.mkdir(parents=True, exist_ok=True)
    return core.STUDIO_DIR / f"gapi_videos_{cid}.json"


def library(acct, refresh=False):
    """Video assets, YouTube uploads, Demand Gen campaigns / ad groups / ads and square logo images of one account.
    Read once and kept until refresh=True (read-only, GAQL)."""
    cid = core.resolve(acct)
    f = _cache(cid)
    if not refresh:
        try:
            return json.loads(f.read_text())
        except (OSError, ValueError):
            pass
    live = _live_mod()
    q = lambda g: live.query(cid, g)  # noqa: E731
    errors = {}

    def safe(name, gaql):
        try:
            return q(gaql)
        except Exception as ex:  # noqa: BLE001 - shown in the tab, the rest still loads
            errors[name] = str(ex)[:300]
            return []
    assets = safe("videos", "SELECT asset.id, asset.name, asset.youtube_video_asset.youtube_video_id, asset.youtube_video_asset.youtube_video_title "
                            "FROM asset WHERE asset.type = YOUTUBE_VIDEO")
    meta = safe("video_details", "SELECT video.id, video.title, video.duration_millis, video.channel_id FROM video")
    uploads = safe("uploads", "SELECT you_tube_video_upload.resource_name, you_tube_video_upload.video_upload_id, you_tube_video_upload.video_id, "
                              "you_tube_video_upload.state, you_tube_video_upload.channel_id, you_tube_video_upload.video_privacy "
                              "FROM you_tube_video_upload")
    camps = safe("campaigns", "SELECT campaign.id, campaign.name, campaign.status, campaign.bidding_strategy_type, campaign.experiment_type, "
                              "campaign_budget.amount_micros FROM campaign WHERE campaign.advertising_channel_type = DEMAND_GEN "
                              "AND campaign.status != 'REMOVED'")
    groups = safe("ad_groups", "SELECT ad_group.id, ad_group.name, ad_group.status, campaign.id, "
                               "ad_group.demand_gen_ad_group_settings.channel_controls.channel_strategy, "
                               + ", ".join(f"ad_group.demand_gen_ad_group_settings.channel_controls.selected_channels.{k}" for k in CHANNELS)
                               + " FROM ad_group WHERE campaign.advertising_channel_type = DEMAND_GEN AND ad_group.status != 'REMOVED'")
    ads = safe("ads", "SELECT ad_group_ad.status, ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.ad.name, ad_group_ad.ad.final_urls, "
                      "ad_group_ad.policy_summary.approval_status, ad_group.id, campaign.id, "
                      + ", ".join(f"ad_group_ad.ad.demand_gen_video_responsive_ad.{k}" for k in
                                  ("headlines", "long_headlines", "descriptions", "business_name", "videos", "logo_images", "call_to_actions"))
                      + " FROM ad_group_ad WHERE campaign.advertising_channel_type = DEMAND_GEN AND ad_group_ad.status != 'REMOVED'")
    logos = safe("logos", "SELECT asset.id, asset.name, asset.image_asset.full_size.width_pixels, asset.image_asset.full_size.height_pixels, "
                          "asset.image_asset.full_size.url FROM asset WHERE asset.type = IMAGE")
    ctas = safe("ctas", "SELECT asset.id, asset.resource_name, asset.call_to_action_asset.call_to_action FROM asset WHERE asset.type = CALL_TO_ACTION")
    details = {m["video"]["id"]: m["video"] for m in meta if m.get("video", {}).get("id")}
    by_res = {}
    vids = []
    for r in assets:
        x = r["asset"]
        yid = x.get("youtubeVideoAsset", {}).get("youtubeVideoId", "")
        d = details.get(yid, {})
        by_res[x.get("resourceName") or f"customers/{cid}/assets/{x['id']}"] = yid
        vids.append({"asset_id": x["id"], "video_id": yid, "name": x.get("name", ""),
                     "title": x.get("youtubeVideoAsset", {}).get("youtubeVideoTitle") or d.get("title", ""),
                     "duration_s": round(int(d["durationMillis"]) / 1000) if d.get("durationMillis") else None})
    cname = {r["campaign"]["id"]: r["campaign"]["name"] for r in camps}
    gname = {r["adGroup"]["id"]: r["adGroup"]["name"] for r in groups}
    out_ads = []
    for r in ads:
        ad = r["adGroupAd"]["ad"]
        v = ad.get("demandGenVideoResponsiveAd", {})
        out_ads.append({"ad_id": ad["id"], "type": ad.get("type", "").replace("DEMAND_GEN_", "").replace("_", " ").capitalize(),
                        "video": ad.get("type") == "DEMAND_GEN_VIDEO_RESPONSIVE_AD", "status": r["adGroupAd"]["status"].title(),
                        "approval": (r["adGroupAd"].get("policySummary", {}).get("approvalStatus") or "").replace("_", " ").capitalize(),
                        "campaign": cname.get(r["campaign"]["id"], r["campaign"]["id"]), "ad_group": gname.get(r["adGroup"]["id"], r["adGroup"]["id"]),
                        "name": ad.get("name", ""), "final_url": (ad.get("finalUrls") or [""])[0],
                        "headlines": [h.get("text", "") for h in v.get("headlines", [])],
                        "long_headlines": [h.get("text", "") for h in v.get("longHeadlines", [])],
                        "descriptions": [h.get("text", "") for h in v.get("descriptions", [])],
                        "business_name": v.get("businessName", {}).get("text", ""),
                        "videos": [by_res.get(x.get("asset"), x.get("asset", "").rsplit("/", 1)[-1]) for x in v.get("videos", [])],
                        "logo_asset_id": next((x.get("asset", "").rsplit("/", 1)[-1] for x in v.get("logoImages", [])), "")})
    out = {"account": cid, "fetched_at": dt.datetime.now().isoformat(timespec="seconds"), "errors": errors,
           "videos": sorted(vids, key=lambda v: (v["title"] or v["name"] or v["video_id"]).lower()),
           "uploads": [{"upload_id": u["youTubeVideoUpload"].get("videoUploadId"), "resource": u["youTubeVideoUpload"].get("resourceName"),
                        "video_id": u["youTubeVideoUpload"].get("videoId", ""), "state": u["youTubeVideoUpload"].get("state", ""),
                        "privacy": u["youTubeVideoUpload"].get("videoPrivacy", ""), "brand_channel": bool(u["youTubeVideoUpload"].get("channelId"))}
                       for u in uploads],
           "campaigns": [{"id": r["campaign"]["id"], "campaign": r["campaign"]["name"], "status": r["campaign"]["status"].title(),
                          "bidding": r["campaign"].get("biddingStrategyType", ""),
                          "trial": r["campaign"].get("experimentType") not in (None, "", "BASE", "UNSPECIFIED", "UNKNOWN"),
                          "budget": round(int(r.get("campaignBudget", {}).get("amountMicros", 0)) / 1e6, 2)} for r in camps],
           "ad_groups": [{"id": r["adGroup"]["id"], "ad_group": r["adGroup"]["name"], "campaign": cname.get(r["campaign"]["id"], ""),
                          "status": r["adGroup"]["status"].title(),
                          "channels": _ch_label(r["adGroup"].get("demandGenAdGroupSettings", {}).get("channelControls", {}) or {"channelStrategy": "ALL_CHANNELS"})}
                         for r in groups],
           "ads": out_ads,
           "logos": sorted([{"asset_id": x["asset"]["id"], "name": x["asset"].get("name", ""),
                             "size": f"{x['asset']['imageAsset']['fullSize'].get('widthPixels')}x{x['asset']['imageAsset']['fullSize'].get('heightPixels')}",
                             "url": x["asset"]["imageAsset"]["fullSize"].get("url", "")}
                            for x in logos if _square(x)], key=lambda x: x["name"].lower())[:300],
           "ctas": sorted({r["asset"].get("callToActionAsset", {}).get("callToAction", "").lower() for r in ctas} - {""})}
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False))
    tmp.replace(f)
    return out


def _square(x):
    fs = x.get("asset", {}).get("imageAsset", {}).get("fullSize", {})
    try:
        w, h = int(fs.get("widthPixels")), int(fs.get("heightPixels"))
    except (TypeError, ValueError):
        return False
    return w >= 128 and h >= 128 and abs(w / h - 1) <= 0.01


def video_info(acct, raw):
    """Check a YouTube id or URL and say what Google Ads already knows about it (title, duration, in the asset library).
    Read-only GAQL; a video Google Ads has never seen comes back with the id only."""
    vid = video_id(raw)
    cid = core.resolve(acct)
    live = _live_mod()
    out = {"video_id": vid, "title": "", "duration_s": None, "in_library": False, "asset_id": None, "known": False}
    try:
        rows = live.query(cid, f"SELECT video.id, video.title, video.duration_millis FROM video WHERE video.id = '{vid}' LIMIT 1")
        if rows:
            v = rows[0]["video"]
            out.update(title=v.get("title", ""), known=True,
                       duration_s=round(int(v["durationMillis"]) / 1000) if v.get("durationMillis") else None)
        rows = live.query(cid, "SELECT asset.id, asset.youtube_video_asset.youtube_video_title FROM asset WHERE asset.type = YOUTUBE_VIDEO "
                               f"AND asset.youtube_video_asset.youtube_video_id = '{vid}' LIMIT 1")
        if rows:
            out.update(in_library=True, asset_id=rows[0]["asset"]["id"], known=True,
                       title=out["title"] or rows[0]["asset"].get("youtubeVideoAsset", {}).get("youtubeVideoTitle", ""))
    except Exception as ex:  # noqa: BLE001 - the id itself is still fine
        out["note"] = f"Couldn't ask Google Ads: {str(ex)[:200]}"
    return out


CHANGE_REFERENCE = """Demand Gen / YouTube video ads (Google Ads API only: Push live, never Send to Editor). Video campaigns can't be
created through the API, so YouTube video ads are Demand Gen video ads; channels "youtube" keeps them on YouTube only.
  {"type":"dg_campaign","new":true,"campaign":"..","budget":20,"bidding":"maximize_conversions|maximize_clicks|target_cpa",
   "target_cpa":12,"ad_group":"..","locations":[{"id":2826}],"languages":"en","channels":"youtube|google_owned|all|[youtube_in_stream,
   youtube_in_feed,youtube_shorts,discover,gmail,display,maps]","start_date":"YYYY-MM-DD","end_date":"YYYY-MM-DD"}   (always Paused;
   daily budget, Demand Gen minimum about 5 USD / 4 GBP a day)
  {"type":"dg_campaign","campaign":"..","status":"enabled|paused","budget":25,"target_cpa":15}
  {"type":"dg_ad_group","new":true,"campaign":"..","ad_group":"..","locations":[..],"languages":"en","channels":"youtube"}
  {"type":"dg_ad_group","campaign":"..","ad_group":"..","status":"paused","channels":"youtube"}
  {"type":"dg_video_ad","campaign":"..","ad_group":"..","videos":["<YouTube id or URL>"],"headlines":["<=40"],"long_headlines":["<=90"],
   "descriptions":["<=90"],"business_name":"<=25","logo_asset_id":"<square image asset id>" or "logo_file":"/full/path.png",
   "final_url":"https://..","call_to_action":"learn_more|book_now|...","breadcrumb1":"<=15"}   (1-5 videos, 1-5 of each text,
   3+ recommended; added Paused)
  {"type":"dg_video_ad","edit":true,"campaign":"..","ad_group":"..","ad_id":"<Google ad id>", any of the fields above, "status":"paused"}
   (a list given replaces that whole list)
  {"type":"dg_ad","campaign":"..","ad_group":"..","ad_id":"..","status":"paused|enabled"}   (any Demand Gen ad)
  {"type":"video_asset","videos":["<YouTube id or URL>", {"id":"..","name":".."}]}   (add videos to the asset library)
Uploading a video file to YouTube is the user's own Upload button in Studio's Videos tab: never through MCP."""
