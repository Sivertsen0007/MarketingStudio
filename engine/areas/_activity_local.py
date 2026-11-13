"""Activity area: Studio's own logs, emails sent, blog posts and social posts. All read-only, all defensive:
the files belong to other areas and may change shape, so a row that can't be read is skipped, never fatal.

Each loader takes (start, end) as aware datetimes and returns plain activity rows:
  {id, ts (ISO UTC), channel, account, type, object_type, object, where, old, new, fields, by, by_kind, who, link, source,
   count (False = shown but not counted, e.g. dry runs and demo data), demo}
"""
import datetime as dt
import json
import re
from pathlib import Path

from . import _activity_periods as per

WP = "https://masterliveaboards.com"
LOCAL = dt.datetime.now().astimezone().tzinfo  # Studio writes its own log times in the Mac's local time


def _local_tz():
    return dt.datetime.now().astimezone().tzinfo


def _in(ts, start, end):
    try:
        t = per.parse(ts)
    except (TypeError, ValueError):
        return False
    return start <= t < end


def _jsonl(path):
    out = []
    try:
        for line in Path(path).read_text().splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return out


def _short_list(items, n=3):
    items = [i for i in items if i]
    if len(items) <= n:
        return "; ".join(items)
    return "; ".join(items[:n]) + f" and {len(items) - n} more"


def _by(items):
    bys = sorted({(i or {}).get("by") or "you" for i in items}) or ["you"]
    return "Studio (" + " + ".join(bys) + ")", "+".join(bys)


# ---------------------------------------------------------------- Studio logs

def studio_rows(folder, start, end, account_names=None):
    """Live pushes and undos (live_*.jsonl), Send to Editor (pushed_*.jsonl), SEO publish history and dry runs,
    SoMe publish/schedule results. account_names: {file key: display name}."""
    folder = Path(folder)
    names = account_names or {}
    tz = _local_tz()
    out = []
    for f in sorted(folder.glob("live_*.jsonl")):
        key = f.stem[len("live_"):]
        meta = key.startswith("meta")
        acct = names.get(key) or (("Meta " if meta else "Google ") + key.replace("meta_", ""))
        for r in _jsonl(f):
            try:
                ts = per.parse(r.get("ts"), assume=tz)
            except (TypeError, ValueError):
                continue
            if not start <= ts < end:
                continue
            items = r.get("items") or []
            undo = bool(r.get("undo_of"))
            by, who = _by(items)
            lines = [o.get("line", "") for o in r.get("ops") or [] if o.get("line") and o.get("line") != "undo"]
            typ = ("Undo" if undo else "Push live") + ("" if r.get("ok") else " (failed)")
            out.append({"id": f"st:live:{key}:{r.get('id')}", "ts": per.iso(ts), "channel": "studio", "account": acct, "account_id": key,
                        "type": typ, "object_type": "Meta push" if meta else "Google Ads push",
                        "object": _short_list([i.get("summary", "") for i in items]) or (f"undo of push {r.get('undo_of')}" if undo else ""),
                        "where": "Meta" if meta else "Google Ads", "old": "", "new": _short_list(lines, 4),
                        "fields": f"{len(r.get('ops') or [])} operation(s)", "by": by if not undo else "Studio (you)",
                        "by_kind": "studio", "who": "you" if undo else who, "link": "", "source": "Studio live log", "count": True})
    for f in sorted(folder.glob("pushed_*.jsonl")):
        key = f.stem[len("pushed_"):]
        acct = names.get(key) or f"Google {key}"
        for r in _jsonl(f):
            try:
                ts = per.parse(r.get("ts"), assume=tz)
            except (TypeError, ValueError):
                continue
            if not start <= ts < end:
                continue
            items = r.get("items") or []
            by, who = _by(items)
            out.append({"id": f"st:editor:{key}:{r.get('ts')}:{r.get('plan')}", "ts": per.iso(ts), "channel": "studio", "account": acct,
                        "account_id": key, "type": "Sent to Editor", "object_type": "Editor plan",
                        "object": _short_list([i.get("summary", "") for i in items]) or str(r.get("plan", "")), "where": "Google Ads Editor",
                        "old": "", "new": "", "fields": f"{len(items)} change(s)", "by": by, "by_kind": "studio", "who": who,
                        "link": "", "source": "Studio Send to Editor log", "count": True})
    for name, dry in (("seo_history.jsonl", False), ("seo_dryrun.jsonl", True)):
        for r in _jsonl(folder / name):
            try:
                ts = per.parse(r.get("at"), assume=tz)
            except (TypeError, ValueError):
                continue
            if not start <= ts < end:
                continue
            act = r.get("action") or "publish"
            if dry:
                typ = f"SEO {act} (dry run, nothing sent)"
            else:
                typ = {"publish": "SEO publish", "undo": "SEO undo"}.get(act, f"SEO {act}")
            fields = sorted((r.get("after") or {}).keys()) or ([r["field"]] if r.get("field") else [])
            out.append({"id": f"st:seo:{name}:{r.get('push_id', '')}:{r.get('post_id', '')}:{r.get('at')}:{act}", "ts": per.iso(ts),
                        "channel": "studio", "account": "masterliveaboards.com" + (f" ({r.get('target')})" if r.get("target") else ""),
                        "account_id": "wp", "type": typ, "object_type": "Blog post", "object": r.get("title") or str(r.get("post_id", "")),
                        "where": "SEO", "old": "", "new": "", "fields": ", ".join(fields), "by": "Studio (you)", "by_kind": "studio",
                        "who": "you", "link": r.get("url", ""), "source": "Studio SEO history", "count": not dry})
    try:
        posts = json.loads((folder / "social_draft.json").read_text())
        posts = posts.get("posts", posts) if isinstance(posts, dict) else posts
    except (OSError, ValueError):
        posts = []
    for p in posts if isinstance(posts, list) else []:
        for ch, r in ((p or {}).get("results") or {}).items():
            if not isinstance(r, dict) or not r.get("at") or not _in(r["at"], start, end):
                continue
            st = r.get("status") or "?"
            label = {"published": "SoMe post published", "scheduled": "SoMe post scheduled", "cancelled": "SoMe schedule cancelled",
                     "deleted": "SoMe post deleted", "failed": "SoMe post failed"}.get(st, f"SoMe {st}")
            out.append({"id": f"st:social:{p.get('id')}:{ch}:{st}:{r['at']}", "ts": per.iso(per.parse(r["at"])), "channel": "studio",
                        "account": ch.capitalize(), "account_id": ch, "type": label, "object_type": "Social post",
                        "object": p.get("title") or (p.get("caption") or "")[:80], "where": "SoMe", "old": "",
                        "new": f"for {r['scheduled_for']}" if r.get("scheduled_for") else "", "fields": "",
                        "by": f"Studio ({p.get('by') or 'you'})", "by_kind": "studio", "who": "you", "link": r.get("url", ""),
                        "source": "Studio SoMe results", "count": st != "failed"})
    return out


# ---------------------------------------------------------------- emails

def email_rows(start, end, adapter=None):
    """Campaigns sent in the window, from the Email area's adapter. -> (rows, demo)."""
    if adapter is None:
        from . import email  # noqa: PLC0415
        adapter = email.adapter()
    demo = bool(getattr(adapter, "demo", False))
    out = []
    for c in adapter.list_campaigns(status="sent") or []:
        sent = c.get("sent_at")
        if not sent or not _in(sent, start, end):
            continue
        n = (c.get("stats") or {}).get("sent")
        aud = c.get("segment_name") or c.get("list_name") or ""
        out.append({"id": f"e:{'demo:' if demo else ''}{c.get('id')}", "ts": per.iso(per.parse(sent)), "channel": "email",
                    "account": "ML Suite" + (" (demo data)" if demo else ""), "account_id": "mls", "type": "Campaign sent",
                    "object_type": "Email campaign", "object": c.get("name") or c.get("subject") or str(c.get("id")),
                    "where": aud, "old": "", "new": f"{n:,} emails" if isinstance(n, int) else "", "fields": "",
                    "by": "Demo data" if demo else "ML Suite", "by_kind": "demo" if demo else "mls", "who": "",
                    "link": c.get("url") or "", "source": "Email area" + (" (demo)" if demo else ""), "count": not demo, "demo": demo})
    return out, demo


# ---------------------------------------------------------------- blog posts

EDIT_GAP = dt.timedelta(minutes=15)  # a "modified" this close to the publish time is the publish itself


def _wp_pages(path, params, http):
    out, page = [], 1
    while page <= 30:
        q = "&".join(f"{k}={v}" for k, v in {**params, "page": page}.items())
        st, headers, body = http(f"{WP}{path}?{q}")
        if st == 400 and page > 1:
            break
        if st != 200 or not isinstance(body, list):
            raise RuntimeError(f"WordPress answered {st}")
        out += body
        total = int({k.lower(): v for k, v in (headers or {}).items()}.get("x-wp-totalpages", 1) or 1)
        if page >= total:
            break
        page += 1
    return out


def blog_fetch(start, end, http=None):
    """Public REST: posts published in the window (after/before) and posts modified in it (modified_after/modified_before).
    A day of margin each side, because WordPress compares in the site's time zone; the exact cut uses date_gmt."""
    if http is None:
        from . import _seo_common as c  # noqa: PLC0415
        http = c.http
    a = (start - dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
    b = (end + dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
    f = {"_fields": "id,date_gmt,modified_gmt,title,link,status", "per_page": 100, "orderby": "date"}
    pub = _wp_pages("/wp-json/wp/v2/posts", {**f, "after": a, "before": b}, http)
    mod = _wp_pages("/wp-json/wp/v2/posts", {**f, "modified_after": a, "modified_before": b}, http)
    by = {p["id"]: p for p in pub + mod if isinstance(p, dict) and "id" in p}
    return list(by.values())


def _title(p):
    t = p.get("title")
    t = t.get("rendered", "") if isinstance(t, dict) else (t or "")
    import html  # noqa: PLC0415
    return html.unescape(re.sub(r"<[^>]+>", "", t))


def blog_rows(posts, start, end):
    """WordPress posts -> Published rows (date in window) and Edited rows (modified in window, not the publish itself).
    WordPress keeps only the last modified time, so one post edited three times shows once (the archive keeps
    the earlier edits it has seen)."""
    out = []
    for p in posts:
        try:
            d = per.parse(p.get("date_gmt") or p.get("date"))
            m = per.parse(p.get("modified_gmt") or p.get("modified") or p.get("date_gmt"))
        except (TypeError, ValueError):
            continue
        base = {"channel": "blog", "account": "masterliveaboards.com", "account_id": "wp", "object_type": "Blog post",
                "object": _title(p), "where": "", "old": "", "new": "", "fields": "", "by": "WordPress", "by_kind": "wp", "who": "",
                "link": p.get("link", ""), "source": "WordPress REST", "count": True}
        if start <= d < end:
            out.append({**base, "id": f"b:pub:{p['id']}", "ts": per.iso(d), "type": "Post published"})
        if start <= m < end and m - d > EDIT_GAP:
            out.append({**base, "id": f"b:edit:{p['id']}:{per.iso(m)}", "ts": per.iso(m), "type": "Post edited"})
    return out


# ---------------------------------------------------------------- social posts

def social_rows(posts, start, end):
    """Published Facebook / Instagram posts (the SoMe area's read) in the window."""
    out = []
    for x in posts or []:
        if not x.get("when") or not _in(x["when"], start, end):
            continue
        ch = x.get("channel", "")
        out.append({"id": f"s:{x.get('id')}", "ts": per.iso(per.parse(x["when"])), "channel": "social",
                    "account": {"facebook": "Facebook Page", "instagram": "Instagram"}.get(ch, ch), "account_id": ch,
                    "type": f"{(x.get('format') or 'Post')} published", "object_type": "Social post",
                    "object": (x.get("text") or "").split("\n")[0][:100] or x.get("format") or str(x.get("id")),
                    "where": "", "old": "", "new": "", "fields": "", "by": ch.capitalize() or "Meta", "by_kind": ch, "who": "",
                    "link": x.get("url", ""), "source": "SoMe area (Meta)", "count": True})
    return out
