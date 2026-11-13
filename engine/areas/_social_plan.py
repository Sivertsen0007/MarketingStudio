"""SoMe planning helpers, all pure (unit tested without the network): posting slots and the next free slot, best time
to post (weekday x hour heatmap), quick time picks, bulk CSV import preview, evergreen autolists, trackable links (UTM),
hashtag ranking from our own posts, account analytics + an HTML report, and competitor facts.

Times: Studio works in this Mac's local time zone. Instagram's online_followers hours are read as Pacific time
(Meta's insights days end at 08:00 UTC, i.e. midnight Pacific) and converted to local time (confidence: medium).
"""
import csv
import datetime as dt
import html
import io
import re
import urllib.parse
from collections import defaultdict

try:
    from . import _social_store as store
except ImportError:
    import _social_store as store

DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
SLOT = re.compile(r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\w*\s+([01]?\d|2[0-3]):([0-5]\d)$", re.I)
PACIFIC = "America/Los_Angeles"


# ---------- posting slots ----------

def parse_slots(items):
    """["Tue 09:00", ...] or "Tue 09:00, Thu 18:00" -> sorted [(weekday 0-6, hour, minute)]. Raises on a bad entry."""
    if isinstance(items, str):
        items = [x for x in re.split(r"[,;\n]+", items) if x.strip()]
    out = set()
    for x in items or []:
        m = SLOT.match(x.strip())
        if not m:
            raise ValueError(f'"{x.strip()}" is not a slot: use e.g. "Tue 09:00"')
        out.add((DAYS.index(m.group(1)[:3].title()), int(m.group(2)), int(m.group(3))))
    return sorted(out)


def fmt_slots(slots):
    return [f"{DAYS[d]} {h:02d}:{m:02d}" for d, h, m in slots]


def taken_times(posts, channels):
    """Minute-rounded local times already used by posts (any status except cancelled/deleted) on these channels."""
    out = set()
    for p in posts:
        w = (p.get("move") or {}).get("when") or p.get("when")
        if not w or not set(p.get("channels") or []) & set(channels):
            continue
        out.add(store.parse_when(w).astimezone().replace(second=0, microsecond=0))
    return out


def next_free_slot(slot_map, channels, posts, after, day=None, horizon=28):
    """The first slot (of any of the channels) after `after` that no post on those channels uses.
    day: a date to look on only that day. -> aware datetime or None."""
    slots = sorted({s for c in channels for s in parse_slots(slot_map.get(c) or [])})
    if not slots:
        return None
    used = taken_times(posts, channels)
    after = after.astimezone()
    start = dt.datetime.combine(day, dt.time(), after.tzinfo) if day else after
    for i in range(1 if day else horizon + 1):
        d = (start + dt.timedelta(days=i)).date()
        for wd, h, m in slots:
            if d.weekday() != wd:
                continue
            t = dt.datetime(d.year, d.month, d.day, h, m, tzinfo=after.tzinfo).astimezone()
            if t > after and t not in used:
                return t
    return None


# ---------- best time to post ----------

def _norm(grid):
    hi = max((v for row in grid for v in row), default=0)
    return [[round(v / hi, 3) if hi else 0 for v in row] for row in grid]


def heatmap(posts, channel, online=None, tz=None):
    """posts: published rows ({channel, when, engagement, reach}). online: [(day end datetime, {hour: followers})] for
    Instagram. -> {"cells": 7x24 scores 0-1 (Mon first, local hours), "top": [{day, hour, score}], "posts": n, "basis"}.
    Score: Instagram = half follower activity, half our posts' engagement rate in that hour; other channels = engagement."""
    tz = tz or dt.datetime.now().astimezone().tzinfo
    eng, cnt = [[0.0] * 24 for _ in range(7)], [[0] * 24 for _ in range(7)]
    n = 0
    for r in posts:
        if r.get("channel") != channel or not r.get("when"):
            continue
        t = store.parse_when(r["when"]).astimezone(tz)
        score = (r.get("engagement") or 0) / max(r.get("reach") or 0, 1) if r.get("reach") else (r.get("engagement") or 0) / 1000
        for dh in (-1, 0, 1):  # spread a little over the neighbouring hours: few posts per hour otherwise
            h = (t.hour + dh) % 24
            w = 1.0 if dh == 0 else 0.5
            eng[t.weekday()][h] += score * w
            cnt[t.weekday()][h] += w
        n += 1
    avg = [[eng[d][h] / (cnt[d][h] + 1) for h in range(24)] for d in range(7)]  # +1: one lucky post counts less
    e = _norm(avg)
    basis = [f"engagement of {n} of our posts"] if n else []
    o = None
    if online:
        try:
            from zoneinfo import ZoneInfo
            pac = ZoneInfo(PACIFIC)
        except Exception:  # noqa: BLE001
            pac = dt.timezone(dt.timedelta(hours=-8))
        tot, k = [[0.0] * 24 for _ in range(7)], [[0] * 24 for _ in range(7)]
        for end, hours in online:
            day = (end.astimezone(pac) - dt.timedelta(days=1)).date()
            for hk, v in (hours or {}).items():
                t = dt.datetime(day.year, day.month, day.day, int(hk), tzinfo=pac).astimezone(tz)
                tot[t.weekday()][t.hour] += v or 0
                k[t.weekday()][t.hour] += 1
        o = _norm([[tot[d][h] / k[d][h] if k[d][h] else 0 for h in range(24)] for d in range(7)])
        basis.append(f"Instagram follower activity ({len(online)} days)")
    cells = [[round((e[d][h] + o[d][h]) / 2 if o and n else (o[d][h] if o else e[d][h]), 3) for h in range(24)] for d in range(7)]
    flat = sorted(((cells[d][h], d, h) for d in range(7) for h in range(24) if 6 <= h <= 22), reverse=True)
    top, seen_days = [], defaultdict(int)
    for sc, d, h in flat:  # at most two of the top three on one day, and not next to each other
        if sc <= 0 or seen_days[d] >= 2 or any(t["day"] == d and abs(t["hour"] - h) < 3 for t in top):
            continue
        top.append({"day": d, "hour": h, "score": sc, "label": f"{DAYS[d]} {h:02d}:00"})
        seen_days[d] += 1
        if len(top) == 3:
            break
    return {"cells": cells, "top": top, "posts": n, "basis": basis}


def next_best(top, after, used=()):
    """Next datetime of the best slots after `after`, in after's time zone (same as the heatmap's). -> [aware datetimes]."""
    after = after if after.tzinfo else after.astimezone()
    out = []
    for t in top:
        for i in range(8):
            d = (after + dt.timedelta(days=i)).date()
            if d.weekday() != t["day"]:
                continue
            x = dt.datetime(d.year, d.month, d.day, t["hour"], 0, tzinfo=after.tzinfo)
            if x > after and x not in used:
                out.append(x)
                break
    return sorted(out)


def quick_times(now, slot_time=None, best=None):
    """The quick picks under the date field. -> [{label, when}]"""
    now = now.astimezone()
    day = lambda n, h, m=0: (now + dt.timedelta(days=n)).replace(hour=h, minute=m, second=0, microsecond=0)  # noqa: E731
    picks = []
    if day(0, 18) > now + dt.timedelta(minutes=15):
        picks.append(("Today 18:00", day(0, 18)))
    picks += [("Tomorrow 09:00", day(1, 9)), ("Tomorrow 12:00", day(1, 12)), ("Tomorrow 18:00", day(1, 18))]
    if slot_time:
        picks.append((f"Next queue slot ({slot_time:%a %H:%M})", slot_time))
    for b in (best or [])[:1]:
        picks.append((f"Best time ({b:%a %H:%M})", b))
    return [{"label": label, "when": store.iso(t)} for label, t in picks]


# ---------- trackable links ----------

def add_utm(url, source, campaign, medium="social"):
    """Add utm_source / utm_medium / utm_campaign. Never touches a link that already has any utm_ parameter."""
    if not url or not url.startswith(("http://", "https://")):
        return url
    u = urllib.parse.urlsplit(url)
    q = urllib.parse.parse_qsl(u.query, keep_blank_values=True)
    if any(k.lower().startswith("utm_") for k, _ in q):
        return url
    q += [("utm_source", source), ("utm_medium", medium), ("utm_campaign", campaign or "organic_social")]
    return urllib.parse.urlunsplit((u.scheme, u.netloc, u.path, urllib.parse.urlencode(q), u.fragment))


def campaign_slug(text):
    s = re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")
    return s[:40] or "organic_social"


URL = re.compile(r"https?://[^\s<>\"')]+")


def tracked_text(text, source, campaign):
    """Caption with UTMs added to the links in it (Facebook / LinkedIn)."""
    def sub(m):
        u = m.group(0)
        core = u.rstrip(".,;:!")
        return add_utm(core, source, campaign) + u[len(core):]
    return URL.sub(sub, text or "")


# ---------- bulk CSV import ----------

CSV_COLS = ("date", "time", "channels", "caption", "link", "first_comment", "hashtags", "media")


def parse_csv(text, tz=None):
    """-> [{"row", "fields" (post fields), "media": [paths/urls], "problems"}]. Dates: YYYY-MM-DD or DD/MM/YYYY; time HH:MM."""
    tz = tz or dt.datetime.now().astimezone().tzinfo
    rows = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
    out = []
    for i, r in enumerate(rows, 2):
        r = {(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
        probs, when = [], ""
        d, t = r.get("date", ""), r.get("time", "") or "09:00"
        if d:
            try:
                day = dt.date.fromisoformat(d) if "-" in d else dt.datetime.strptime(d, "%d/%m/%Y").date()
                hh, mm = (int(x) for x in t.split(":")[:2])
                when = store.iso(dt.datetime(day.year, day.month, day.day, hh, mm, tzinfo=tz))
            except ValueError:
                probs.append(f"date/time not understood: {d} {t} (use 2026-11-25 and 09:00)")
        chans = [c.strip().lower() for c in re.split(r"[,;/ ]+", r.get("channels", "")) if c.strip()]
        alias = {"fb": "facebook", "ig": "instagram", "li": "linkedin"}
        chans = [alias.get(c, c) for c in chans]
        bad = [c for c in chans if c not in store.CHANNELS]
        if bad:
            probs.append(f"unknown channel(s): {', '.join(bad)}")
        if not [c for c in chans if c in store.CHANNELS]:
            probs.append("no channels (facebook, instagram, linkedin)")
        fields = {"when": when, "channels": [c for c in chans if c in store.CHANNELS], "caption": r.get("caption", ""), "link": r.get("link", ""),
                  "first_comment": r.get("first_comment", ""), "hashtags": r.get("hashtags", ""), "title": (r.get("caption", "")[:50] or f"CSV row {i}")}
        media = [m.strip() for m in re.split(r"[|;]+", r.get("media", "")) if m.strip()]
        out.append({"row": i, "fields": fields, "media": media, "problems": probs})
    return out


# ---------- evergreen autolists ----------

def autolist_fill(lst, posts_by_id, free_times):
    """Copies of the list's posts, in turn, for each free slot time. -> ([new post field dicts], next index).
    They are DRAFTS: the user still presses Schedule."""
    ids = [i for i in lst.get("post_ids") or [] if i in posts_by_id]
    if not ids:
        return [], lst.get("next", 0)
    k = int(lst.get("next", 0)) % len(ids)
    out = []
    for t in free_times:
        src = posts_by_id[ids[k]]
        f = {x: src.get(x) for x in store.POST_FIELDS if x in src and x not in ("when", "approval", "autolist")}
        f.update(when=store.iso(t), title=f"Evergreen: {src.get('title') or (src.get('caption') or '')[:40]}", autolist=lst["id"])
        if src.get("approval") in ("needed", "approved"):  # a copy is a new post: it needs its own approval, never inherits one
            f["approval"] = "needed"
        if lst.get("channels"):
            f["channels"] = lst["channels"]
        out.append(f)
        k = (k + 1) % len(ids)
    return out, k


# ---------- hashtags from our own posts ----------

TAG = re.compile(r"(?<!\w)#(\w+)")


def hashtag_rank(rows, min_posts=2):
    """Our own hashtags ranked by the average engagement of the posts that used them. -> [{tag, posts, avg_engagement}]"""
    by = defaultdict(list)
    for r in rows:
        for t in {x.lower() for x in TAG.findall(r.get("text") or "")}:
            by[t].append(r.get("engagement") or 0)
    out = [{"tag": "#" + t, "posts": len(v), "avg_engagement": round(sum(v) / len(v), 1)} for t, v in by.items() if len(v) >= min_posts]
    return sorted(out, key=lambda x: (-x["avg_engagement"], -x["posts"]))


# ---------- analytics + report ----------

def analytics(rows, start, end, accounts=None):
    """rows: published posts with insights. accounts: {channel: {followers, growth}}. -> dict for the UI and the report."""
    rs = [r for r in rows if r.get("when") and start <= store.parse_when(r["when"]) < end]
    out = {"start": store.iso(start), "end": store.iso(end), "channels": {}, "top": [], "by_type": [], "by_day": []}
    for ch in ("facebook", "instagram"):
        c = [r for r in rs if r["channel"] == ch]
        reach = sum(r.get("reach") or 0 for r in c)
        eng = sum(r.get("engagement") or 0 for r in c)
        out["channels"][ch] = {"posts": len(c), "reach": reach, "engagement": eng,
                               "engagement_rate": round(100 * eng / reach, 2) if reach else None,
                               "link_clicks": sum(r.get("link_clicks") or 0 for r in c) if ch == "facebook" else None,
                               **((accounts or {}).get(ch) or {})}
    by_t = defaultdict(lambda: {"posts": 0, "reach": 0, "engagement": 0})
    by_d = defaultdict(lambda: {"posts": 0, "reach": 0, "engagement": 0})
    for r in rs:
        for key, agg in (((r["channel"], r.get("format") or "Post"), by_t), ((store.parse_when(r["when"]).astimezone().weekday(),), by_d)):
            a = agg[key]
            a["posts"] += 1
            a["reach"] += r.get("reach") or 0
            a["engagement"] += r.get("engagement") or 0
    rate = lambda a: round(100 * a["engagement"] / a["reach"], 2) if a["reach"] else None  # noqa: E731
    out["by_type"] = sorted([{"channel": k[0], "format": k[1], **v, "engagement_rate": rate(v)} for k, v in by_t.items()],
                            key=lambda x: (x["channel"], -x["posts"]))
    out["by_day"] = [{"day": DAYS[k[0]], **by_d[k], "engagement_rate": rate(by_d[k])} for k in sorted(by_d)]
    out["top"] = sorted(rs, key=lambda r: -(r.get("engagement") or 0))[:10]
    return out


def report_html(a, title="SoMe report: Master Liveaboards"):
    e = lambda v: html.escape(str(v if v is not None else ""))  # noqa: E731
    n = lambda v: "" if v is None else f"{v:,}" if isinstance(v, int) else f"{v}"  # noqa: E731
    d = lambda s: f"{store.parse_when(s):%d %B %Y}"  # noqa: E731
    ch = "".join(f"<tr><td>{e(k.title())}</td><td>{n(v.get('followers'))}</td><td>{n(v.get('growth'))}</td><td>{n(v['posts'])}</td>"
                 f"<td>{n(v['reach'])}</td><td>{n(v['engagement'])}</td><td>{n(v['engagement_rate'])}</td></tr>" for k, v in a["channels"].items())
    ty = "".join(f"<tr><td>{e(x['channel'].title())}</td><td>{e(x['format'])}</td><td>{n(x['posts'])}</td><td>{n(x['reach'])}</td>"
                 f"<td>{n(x['engagement'])}</td><td>{n(x['engagement_rate'])}</td></tr>" for x in a["by_type"])
    dy = "".join(f"<tr><td>{e(x['day'])}</td><td>{n(x['posts'])}</td><td>{n(x['reach'])}</td><td>{n(x['engagement'])}</td><td>{n(x['engagement_rate'])}</td></tr>"
                 for x in a["by_day"])
    tp = "".join(f"<tr><td>{d(x['when'])}</td><td>{e(x['channel'].title())}</td><td>{e(x.get('format'))}</td><td>{e((x.get('text') or '')[:90])}</td>"
                 f"<td>{n(x.get('reach'))}</td><td>{n(x.get('engagement'))}</td><td><a href=\"{e(x.get('url'))}\">open</a></td></tr>" for x in a["top"])
    th = lambda *c: "<tr>" + "".join(f"<th>{x}</th>" for x in c) + "</tr>"  # noqa: E731
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{e(title)}</title>
<style>body{{font:14px/1.45 Arial,sans-serif;margin:24px;color:#202124}}table{{border-collapse:collapse;margin:8px 0 22px}}
th,td{{border:1px solid #dadce0;padding:5px 9px;text-align:left}}th{{background:#f1f3f4}}h1{{font-weight:400}}.n{{color:#5f6368}}</style></head><body>
<h1>{e(title)}</h1><p class="n">{d(a['start'])} to {d(a['end'])}. Organic posts only. Engagement rate = engagement / reach of the same posts.
Reach is added up per post, so one person can count more than once. Source: Meta Graph API, read-only.</p>
<h2>Channels</h2><table>{th('Channel', 'Followers', 'New followers', 'Posts', 'Reach', 'Engagement', 'Engagement rate %')}{ch}</table>
<h2>By post type</h2><table>{th('Channel', 'Type', 'Posts', 'Reach', 'Engagement', 'Engagement rate %')}{ty}</table>
<h2>By day of the week</h2><table>{th('Day', 'Posts', 'Reach', 'Engagement', 'Engagement rate %')}{dy}</table>
<h2>Top posts</h2><table>{th('Date', 'Channel', 'Type', 'Text', 'Reach', 'Engagement', '')}{tp}</table></body></html>"""


# ---------- competitors (facts only) ----------

def competitor_facts(username, bd):
    """bd: the business_discovery object. -> {username, followers, media_count, avg_likes, avg_comments, posts_per_week, recent}"""
    media = (bd.get("media") or {}).get("data") or []
    likes = [m.get("like_count") or 0 for m in media]
    comments = [m.get("comments_count") or 0 for m in media]
    times = sorted(store.parse_when(m["timestamp"]) for m in media if m.get("timestamp"))
    span = (times[-1] - times[0]).days if len(times) > 1 else 0
    return {"username": username, "followers": bd.get("followers_count"), "media_count": bd.get("media_count"),
            "avg_likes": round(sum(likes) / len(likes)) if likes else None, "avg_comments": round(sum(comments) / len(comments), 1) if comments else None,
            "posts_per_week": round(7 * (len(times) - 1) / span, 1) if span else None, "recent": len(media)}
