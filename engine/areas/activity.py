"""Activity area: everything that happened in a period, across channels. Read-only: this area has no write actions.

Sources (each loads on its own, so the page fills in as they arrive):
  google  Google Ads change history (change_event), every account Studio knows
  meta    Meta ad account activity log (act_<id>/activities)
  email   campaigns sent, from the Email area's adapter (demo data is shown, marked, and never counted)
  blog    masterliveaboards.com posts published and edited (public WordPress REST)
  social  Facebook and Instagram posts published (the SoMe area's read)
  studio  Studio's own logs: Push live and undo, Send to Editor, SEO publish/undo, SoMe publish/schedule

Google keeps change_event for 30 days, so Google, Meta, blog edits and social posts are also saved in a local archive
(<Studio data>/activity/archive.sqlite, de-duplicated by id). With datapolicy in manual mode (the default) Google and
Meta are synced into it only on a Refresh (the Activity Refresh button, or the Refresh menu's Activity or All), the first
time an account has nothing archived, and whenever snapshot() runs. sync_status() warns when the last Google sync is over
20 days old, because Google drops change history after about 30 days. In auto mode it syncs on open, every 15 minutes.

Read functions for MCP tools: activity_summary(period, ...) and activity_rows(period, filters, ...).
Personal data: rows carry who made a change ("who": Google user email, Meta actor name). Shown inside Studio only;
CSV exports leave it out unless include_who is set. See ADMIN.md section L.
"""
import concurrent.futures
import csv
import datetime as dt
import io
import threading
import time

from . import _activity_archive as archive
from . import _activity_google as gch
from . import _activity_local as loc
from . import _activity_meta as mch
from . import _activity_periods as per

LABEL = "Activity"
TTL = 15 * 60  # auto mode only; manual mode keeps results until a Refresh (datapolicy)
GOOGLE_KEEP_DAYS = 30
WARN_AFTER_DAYS = 20
RETENTION_DAYS = 730  # proposal: two years of archived activity, then pruned by snapshot(). Confirm with the director.
SOURCES = {"google": "Google Ads", "meta": "Meta", "email": "Email", "blog": "Blog", "social": "Social", "studio": "Studio"}
TILES = [("google", "Google Ads changes"), ("meta", "Meta changes"), ("email", "Emails sent"), ("blog_pub", "Blog posts published"),
         ("blog_edit", "Blog posts edited"), ("social", "Social posts published"), ("studio", "Studio pushes and undos")]
CSV_COLS = [("time", "Time (UK)"), ("channel", "Channel"), ("account", "Account"), ("type", "Change"), ("object_type", "Object type"),
            ("object", "Object"), ("where", "Where"), ("old", "Old"), ("new", "New"), ("by", "Made by"), ("who", "Who"), ("link", "Link")]

_cache = {}
_cache_lock = threading.Lock()
_src_locks = {s: threading.Lock() for s in SOURCES}
_synced = {}  # source -> time.time() of the last sync with Google / Meta
STUDIO_PUSH_MATCH = dt.timedelta(minutes=3)
META_OVERLAP = dt.timedelta(days=2)  # Meta's activity log can show an event hours late: re-read the last 2 days each time


# ---------------------------------------------------------------- helpers

def _now():
    return dt.datetime.now(dt.timezone.utc)


def _studio_dir():
    import core  # noqa: PLC0415
    return core.STUDIO_DIR


def _period(q):
    return per.period(q.get("period") or "last_30", tz=q.get("tz") or "Europe/London", start=q.get("from"), end=q.get("to"))


def _policy():
    try:
        import datapolicy  # noqa: PLC0415 - Studio's folder
        return datapolicy
    except ImportError:  # pragma: no cover
        return None


def _fresh(age, filled_at):
    dp = _policy()
    return dp.fresh("activity", age, TTL, filled_at) if dp else age < TTL


def _manual():
    dp = _policy()
    return bool(dp and dp.manual())


def _cached(key, fn, force=False):
    with _cache_lock:
        hit = _cache.get(key)
        if hit and not force and _fresh(time.time() - hit[0], hit[0]):
            return hit[1]
    val = fn()
    with _cache_lock:
        _cache[key] = (time.time(), val)
    return val


def counted(r):
    return r.get("count", True) and not r.get("system") and not r.get("demo")


def tile_key(r):
    ch = r.get("channel")
    if ch == "blog":
        return "blog_pub" if r.get("type") == "Post published" else "blog_edit"
    return ch


def _google_accounts():
    import gapi  # noqa: PLC0415
    return [{"id": str(a["id"]), "name": a.get("name") or str(a["id"])} for a in gapi.accounts_any()]


def _meta_accounts():
    import meta_ads  # noqa: PLC0415
    if not meta_ads.TOKEN:
        return []
    return [{"act": a["act"], "key": f"meta_{a['act'].replace('act_', '')}", "name": a["name"]} for a in meta_ads.accounts()]


def _names():
    out = {}
    try:
        out.update({a["id"]: a["name"] for a in _google_accounts()})
    except Exception:  # noqa: BLE001
        pass
    try:
        out.update({a["key"]: a["name"] for a in _meta_accounts()})
    except Exception:  # noqa: BLE001
        pass
    return out


# ---------------------------------------------------------------- syncing Google and Meta into the archive

def sync_google(force=False, query=None, accounts=None, now=None):
    """Read new change_event rows for every Google account into the archive (from the last read, or 30 days back).
    -> [{account, new, read, error}]"""
    if query is None:
        import live  # noqa: PLC0415
        query = live.query
    now = now or _now()
    out = []
    with _src_locks["google"]:
        if not force and not _manual() and time.time() - _synced.get("google", 0) < TTL:
            return out
        for a in accounts if accounts is not None else _google_accounts():
            cid = a["id"]
            try:
                last = archive.last_fetch("google", cid)
                if not force and _manual() and last:
                    continue  # manual mode: only a Refresh syncs an account that is already archived
                start = (last - dt.timedelta(hours=1)) if last else now - dt.timedelta(days=gch.KEEP_DAYS)
                win = gch.clamp(start, now, now)
                if not win:
                    continue
                tz = gch.account_tz(cid, query)
                raw = gch.fetch(cid, win[0], win[1], tz, query)
                rows = [{**gch.map_row(r, cid, a["name"], tz), "raw": r, "tz": str(tz)} for r in raw]
                out.append({"account": a["name"], "read": len(rows), "new": archive.save("google", cid, rows, win[0], win[1], now)})
            except Exception as ex:  # noqa: BLE001 - one account failing must not hide the others
                out.append({"account": a["name"], "read": 0, "new": 0, "error": _scrub(ex)})
        if not any(o.get("error") for o in out):
            _synced["google"] = time.time()
    return out


def sync_meta(force=False, get_all=None, accounts=None, now=None, days=30):
    """Read new Meta activities into the archive (from the last read, or `days` back). -> [{account, new, read, error}]"""
    if get_all is None:
        import meta_ads  # noqa: PLC0415
        get_all = meta_ads.get_all
    now = now or _now()
    out = []
    with _src_locks["meta"]:
        if not force and not _manual() and time.time() - _synced.get("meta", 0) < TTL:
            return out
        for a in accounts if accounts is not None else _meta_accounts():
            try:
                last = archive.last_fetch("meta", a["act"])
                if not force and _manual() and last:
                    continue
                start = (last - META_OVERLAP) if last else now - dt.timedelta(days=days)
                raw = mch.fetch(a["act"], start, now, get_all)
                rows = [{**mch.map_row(x, a["act"], a["name"]), "raw": x} for x in raw]
                out.append({"account": a["name"], "read": len(rows), "new": archive.save("meta", a["act"], rows, start, now, now)})
            except Exception as ex:  # noqa: BLE001
                out.append({"account": a["name"], "read": 0, "new": 0, "error": _scrub(ex)})
        if not any(o.get("error") for o in out):
            _synced["meta"] = time.time()
    return out


def backfill_meta(days=400, get_all=None):
    """One-off: read older Meta history into the archive (Meta keeps more than Google; 400 days took about 7 minutes
    on 10 November 2026). Not run from the page."""
    if get_all is None:
        import meta_ads  # noqa: PLC0415
        get_all = meta_ads.get_all
    now = _now()
    out = []
    for a in _meta_accounts():
        start = now - dt.timedelta(days=days)
        raw = mch.fetch(a["act"], start, now, get_all)
        rows = [{**mch.map_row(x, a["act"], a["name"]), "raw": x} for x in raw]
        out.append({"account": a["name"], "read": len(rows), "new": archive.save("meta", a["act"], rows, start, now, now)})
    return out


def snapshot():
    """Grow the archive: Google + Meta since the last read, blog edits and social posts of the last 30 days. Prunes rows
    older than RETENTION_DAYS. Safe to run daily (cron or the lead). -> what it did."""
    now = _now()
    res = {"google": sync_google(force=True), "meta": sync_meta(force=True)}
    p = per.period("last_30")
    for s in ("blog", "social"):
        try:
            res[s] = {"rows": len(load_source(s, p, force=True)["rows"])}
        except Exception as ex:  # noqa: BLE001
            res[s] = {"error": _scrub(ex)}
    res["pruned"] = archive.prune(now - dt.timedelta(days=RETENTION_DAYS))
    return res


def refresh(body=None):
    """The Refresh menu's Activity item and the Activity Refresh button: sync Google and Meta into the archive now and
    forget the kept results, so each source reads again (once) when shown. Reads only."""
    t0 = time.time()
    res = {"google": sync_google(force=True), "meta": sync_meta(force=True)}
    with _cache_lock:
        _cache.clear()
    dp = _policy()
    if dp:
        dp.invalidate("activity", at=t0)
        dp.mark("activity")
    return {**res, "sync": sync_status()}


def sync_status(now=None):
    """When Google and Meta were last synced into the archive, and a warning when Google's is over WARN_AFTER_DAYS old
    (the oldest account counts). -> {"google": iso|None, "meta": iso|None, "google_age_days", "days_left", "warning"}"""
    now = now or _now()
    out = {"google": None, "meta": None, "google_age_days": None, "days_left": None, "warning": None}
    try:
        g = [archive.last_fetch("google", a["id"]) for a in _google_accounts()]
    except Exception:  # noqa: BLE001
        g = []
    try:
        m = [archive.last_fetch("meta", a["act"]) for a in _meta_accounts()]
    except Exception:  # noqa: BLE001
        m = []
    if g and all(g):
        oldest = min(g)
        out["google"] = per.iso(oldest)
        age = (now - oldest).total_seconds() / 86400
        out["google_age_days"] = round(age, 1)
        left = max(0, int(GOOGLE_KEEP_DAYS - age))
        out["days_left"] = left
        if age > WARN_AFTER_DAYS:
            day = per.fmt_day(oldest.astimezone(per.LONDON).date())
            out["warning"] = (f"Refresh Activity within {left} days or Google history from {day} is lost." if left else
                              f"Refresh Activity now: Google history from {day} may already be lost.")
    if m and all(m):
        out["meta"] = per.iso(min(m))
    return out


def _scrub(ex):
    try:
        from health import _scrub as hs  # noqa: PLC0415
        return hs(str(ex))[:400]
    except Exception:  # noqa: BLE001
        return str(ex)[:400]


# ---------------------------------------------------------------- loading one source

def tag_studio_pushes(rows, studio_rows):
    """Mark API changes made within a few minutes of a Studio Push live / undo on the same account."""
    pushes = {}
    for s in studio_rows:
        if s.get("object_type") in ("Google Ads push", "Meta push"):
            pushes.setdefault(s["account_id"], []).append(per.parse(s["ts"]))
    for r in rows:
        if r.get("by_kind") not in ("GOOGLE_ADS_API", "api"):
            continue
        key = r.get("account_id", "")
        key = f"meta_{key.replace('act_', '')}" if r.get("channel") == "meta" else key
        t = per.parse(r["ts"])
        if any(abs(t - x) <= STUDIO_PUSH_MATCH for x in pushes.get(key, [])):
            r["by"] = f"{r['by']} (Studio push)"
            r["by_kind"] = "studio_api"
    return rows


def remap(r):
    """Archived rows keep the source's raw record, so better wording reaches old rows too. -> the row without raw."""
    raw = r.get("raw")
    try:
        if raw and r.get("channel") == "google":
            from zoneinfo import ZoneInfo  # noqa: PLC0415
            tz = ZoneInfo(r["tz"]) if r.get("tz") and r["tz"] != "UTC" else dt.timezone.utc
            r = gch.map_row(raw, r["account_id"], r["account"], tz)
        elif raw and r.get("channel") == "meta":
            r = mch.map_row(raw, r["account_id"], r["account"])
    except Exception:  # noqa: BLE001 - keep the stored wording
        pass
    return {k: v for k, v in r.items() if k not in ("raw", "tz")}


def _archive_source(src, p, force):
    sync = sync_google if src == "google" else sync_meta
    accounts = _google_accounts() if src == "google" else _meta_accounts()
    if not accounts:
        return {"status": "not_connected", "rows": [], "note": "No account connected." if src == "google" else "No Meta token or ad account."}
    res = sync(force=force)
    errs = [f"{r['account']}: {r['error']}" for r in res if r.get("error")]
    rows = [remap(r) for r in archive.rows(src, p["prev_start"], p["end"])]
    try:
        st = loc.studio_rows(_studio_dir(), p["prev_start"] - STUDIO_PUSH_MATCH, p["end"] + STUDIO_PUSH_MATCH, _names())
        tag_studio_pushes(rows, st)
    except Exception:  # noqa: BLE001 - the tag is a bonus
        pass
    cov = archive.coverage(src)
    keep = "Google keeps 30 days" if src == "google" else "Meta keeps longer, Studio reads 30 days back the first time"
    note = f"Archive holds {SOURCES[src]} history from {_day(cov['from'])}" if cov else "Nothing archived yet."
    if cov and cov["gaps"]:
        note += f" (gaps: {', '.join(_day(a) + ' to ' + _day(b) for a, b in cov['gaps'])})"
    note += f". {keep}."
    if cov and per.parse(cov["from"]) > p["start"]:
        note += f" This period starts before that, so it is incomplete."
    return {"status": "error" if errs and len(errs) == len(res) and not rows else "ok", "rows": rows, "note": note,
            "errors": errs, "coverage": cov, "covered_from": cov["from"] if cov else per.iso(_now())}


def _day(iso):
    return per.fmt_day(per.parse(iso).astimezone(per.LONDON).date())


def _key(src, p):
    return (src, p["name"], per.iso(p["prev_start"]), p["tz"], per.iso(p["start"]) if p["name"] == "custom" else "")


def _load_raw(src, p, force=False):
    if src in ("google", "meta"):
        return _archive_source(src, p, force)
    if src == "email":
        rows, demo = loc.email_rows(p["prev_start"], p["end"])
        note = "Demo data: the Email area is not connected to ML Suite. Demo campaigns are shown but never counted." if demo else ""
        return {"status": "demo" if demo else "ok", "rows": rows, "note": note}
    if src == "studio":
        return {"status": "ok", "rows": loc.studio_rows(_studio_dir(), p["prev_start"], p["end"], _names()),
                "note": "Studio's own logs: Push live and undo, Send to Editor, SEO publish, SoMe publish and schedule. SEO dry runs are shown, not counted."}
    if src == "blog":
        rows = loc.blog_rows(loc.blog_fetch(p["prev_start"], p["end"]), p["prev_start"], p["end"])
        archive.save("blog", "wp", [r for r in rows if r["type"] == "Post edited"])
        seen = {r["id"] for r in rows}
        rows += [r for r in archive.rows("blog", p["prev_start"], p["end"]) if r["id"] not in seen]
        return {"status": "ok", "rows": rows, "note": "WordPress keeps only each post's last edit time: a post edited twice shows once, "
                "unless Studio saw the earlier edit (the archive keeps it). Published posts only (no drafts)."}
    if src == "social":
        from . import _social_meta as sm  # noqa: PLC0415
        dp = _policy()
        pub = sm.published(dp.max_age("social", sm.CACHE_TTL) if dp else sm.CACHE_TTL)
        posts = pub.get("posts") or []
        rows = loc.social_rows(posts, p["prev_start"], p["end"])
        archive.save("social", "meta", rows)
        seen = {r["id"] for r in rows}
        rows += [r for r in archive.rows("social", p["prev_start"], p["end"]) if r["id"] not in seen]
        oldest = min((x["when"] for x in posts if x.get("when")), default=None)
        note = "The SoMe area reads the latest 100 posts per channel."
        if oldest:
            note += f" They reach back to {per.fmt_day(per.parse(oldest).date())}; older posts come from the archive."
        errs = pub.get("errors") or []
        capped = any(sum(1 for x in posts if x.get("channel") == ch) >= 100 for ch in ("facebook", "instagram"))
        return {"status": "error" if errs and not rows else "ok", "rows": rows, "note": note, "errors": errs,
                "covered_from": per.iso(per.parse(oldest)) if capped and oldest else None}
    raise KeyError(src)


def load_source(src, p, force=False):
    """-> {src, label, status (ok | error | not_connected | demo), note, rows (this period), prev_count, count, by_tile, prev_by_tile}"""
    if src not in SOURCES:
        raise KeyError(f"unknown source {src!r}")
    try:
        raw = _cached(_key(src, p), lambda: _load_raw(src, p, force), force=force)
    except Exception as ex:  # noqa: BLE001
        return {"src": src, "label": SOURCES[src], "status": "error", "note": "", "errors": [_scrub(ex)], "rows": [], "prev_count": 0,
                "count": 0, "by_tile": {}, "prev_by_tile": {}}
    cur, prev = split(raw["rows"], p)
    cf = per.parse(raw["covered_from"]) if raw.get("covered_from") else None
    return {"complete": not cf or cf <= p["start"], "prev_complete": not cf or cf <= p["prev_start"],"src": src, "label": SOURCES[src], "status": raw["status"], "note": raw.get("note", ""), "errors": raw.get("errors", []),
            "coverage": raw.get("coverage"), "rows": sorted(cur, key=lambda r: r["ts"], reverse=True),
            "count": sum(1 for r in cur if counted(r)), "prev_count": sum(1 for r in prev if counted(r)),
            "by_tile": _by_tile(cur), "prev_by_tile": _by_tile(prev)}


def split(rows, p):
    """Rows -> (this period, previous period)."""
    cur, prev = [], []
    for r in rows:
        t = per.parse(r["ts"])
        if p["start"] <= t < p["end"]:
            cur.append(r)
        elif p["prev_start"] <= t < p["prev_end"]:
            prev.append(r)
    return cur, prev


def _by_tile(rows):
    out = {}
    for r in rows:
        if counted(r):
            k = tile_key(r)
            out[k] = out.get(k, 0) + 1
    return out


# ---------------------------------------------------------------- combining, filters, CSV (also the MCP read functions)

def load_all(p, sources=None, force=False, timeout=None):
    """All sources in parallel. Sources not done within `timeout` seconds come back as status "loading"."""
    srcs = [s for s in (sources or SOURCES) if s in SOURCES]
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=len(srcs) or 1)
    futs = {s: ex.submit(load_source, s, p, force) for s in srcs}
    concurrent.futures.wait(futs.values(), timeout=timeout)
    ex.shutdown(wait=False)
    out = {}
    for s, f in futs.items():
        out[s] = f.result() if f.done() else {"src": s, "label": SOURCES[s], "status": "loading", "rows": [], "count": 0, "prev_count": 0,
                                                "by_tile": {}, "prev_by_tile": {}, "note": "Still loading.", "errors": []}
    return out


def summarise(results, p):
    """Source results -> tiles (count, previous, change), per-day totals and per-source status."""
    tiles = []
    for k, label in TILES:
        v = sum(r["by_tile"].get(k, 0) for r in results.values())
        pv = sum(r["prev_by_tile"].get(k, 0) for r in results.values())
        src = "blog" if k.startswith("blog") else k
        st = (results.get(src) or {}).get("status", "not_loaded")
        res = results.get(src) or {}
        tiles.append({"key": k, "label": label, "v": v, "prev": pv, "delta": v - pv, "status": st,
                      "complete": res.get("complete", True), "prev_complete": res.get("prev_complete", True)})
    days = {d: 0 for d in per.days(p["start"], p["end"], p["tz"])}
    for r in results.values():
        for row in r["rows"]:
            if counted(row):
                d = per.day_of(row["ts"], p["tz"])
                if d in days:
                    days[d] += 1
    return {"period": _period_out(p),
            "tiles": tiles, "days": [{"day": d, "n": n} for d, n in days.items()],
            "sources": {s: {"status": r["status"], "note": r.get("note", ""), "errors": r.get("errors", []), "count": r["count"],
                            "prev_count": r["prev_count"], "complete": r.get("complete", True), "prev_complete": r.get("prev_complete", True)}
                        for s, r in results.items()}}


def apply_filters(rows, f=None):
    """Same rules as the page: channel, account, type, by are exact matches (any of a comma list); q is a case-insensitive
    search over the text columns; sort = column, desc = 1 for newest/largest first (default: time, newest first)."""
    f = f or {}

    def any_of(key):
        v = f.get(key)
        return set(v.split(",")) if isinstance(v, str) and v else (set(v) if isinstance(v, (list, tuple)) and v else None)
    sets = {k: any_of(k) for k in ("channel", "account", "type", "by")}
    q = (f.get("q") or "").strip().lower()
    out = []
    for r in rows:
        if any(s is not None and str(r.get(k, "")) not in s for k, s in sets.items()):
            continue
        if q and q not in " ".join(str(r.get(k, "")) for k in ("account", "type", "object_type", "object", "where", "old", "new", "by", "who", "fields")).lower():
            continue
        out.append(r)
    key = f.get("sort") or "ts"
    desc = str(f.get("desc", "1" if key == "ts" else "0")) == "1"
    out.sort(key=lambda r: str(r.get(key, "")).lower(), reverse=desc)
    return out


def to_csv(rows, include_who=False, tz="Europe/London"):
    """CSV text of rows. "Who" (staff emails and names) only with include_who."""
    cols = [(k, lab) for k, lab in CSV_COLS if include_who or k != "who"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([lab for _, lab in cols])
    tzinfo = per._tz(tz)
    for r in rows:
        line = []
        for k, _ in cols:
            if k == "time":
                line.append(per.parse(r["ts"]).astimezone(tzinfo).strftime("%Y-%m-%d %H:%M"))
            elif k == "channel":
                line.append(SOURCES.get(r.get("channel"), r.get("channel", "")))
            else:
                line.append(r.get(k, "") or "")
        if r.get("demo"):
            line[cols.index(("account", "Account"))] += " [demo]" if "demo" not in line[cols.index(("account", "Account"))] else ""
        w.writerow(line)
    return buf.getvalue()


def activity_summary(period="last_30", start=None, end=None, tz="Europe/London", force=False):
    """MCP read: counts per channel for a period, compared with the previous period, plus per-day totals."""
    p = per.period(period, tz=tz, start=start, end=end)
    return summarise(load_all(p, force=force), p)


def activity_rows(period="last_30", filters=None, start=None, end=None, tz="Europe/London", include_who=False, limit=None):
    """MCP read: the combined timeline for a period, filtered. "who" is removed unless include_who."""
    p = per.period(period, tz=tz, start=start, end=end)
    res = load_all(p, sources=(filters or {}).get("channel", "").split(",") if (filters or {}).get("channel") else None)
    rows = apply_filters([r for s in res.values() for r in s["rows"]], filters)
    if not include_who:
        rows = [{k: v for k, v in r.items() if k != "who"} for r in rows]
    return rows[:limit] if limit else rows


# ---------------------------------------------------------------- HTTP

def g_config(q):
    return {"periods": per.PERIODS, "sources": SOURCES, "tiles": [{"key": k, "label": lab} for k, lab in TILES],
            "archive": {s: archive.coverage(s) for s in ("google", "meta", "blog", "social")}, "sync": sync_status()}


def _period_out(p):
    return {"name": p["name"], "label": p["label"], "tz": p["tz"], "start": per.iso(p["start"]), "end": per.iso(p["end"]),
            "prev_start": per.iso(p["prev_start"]), "prev_end": per.iso(p["prev_end"]), "days": per.days(p["start"], p["end"], p["tz"])}


def g_source(q):
    p = _period(q)
    out = {**load_source(q.get("src", ""), p, force=q.get("refresh") == "1"), "period": _period_out(p)}
    if q.get("src") == "google":
        out["sync"] = sync_status()
    return out


def g_summary(q):
    p = _period(q)
    return summarise(load_all(p, force=q.get("refresh") == "1", timeout=float(q.get("timeout") or 60)), p)


def g_rows(q):
    return {"rows": activity_rows(q.get("period") or "last_30", q, q.get("from"), q.get("to"), q.get("tz") or "Europe/London",
                                  include_who=q.get("who") == "1", limit=int(q["limit"]) if q.get("limit") else None)}


def g_csv(q):
    """CSV of the filtered view, for the browser to download. Who made it only when who=1."""
    p = _period(q)
    srcs = q.get("channel").split(",") if q.get("channel") else None
    res = load_all(p, sources=srcs)
    rows = apply_filters([r for s in res.values() for r in s["rows"]], q)
    who = q.get("who") == "1"
    name = f"activity-{p['start'].date().isoformat()}-to-{(p['end'] - dt.timedelta(seconds=1)).date().isoformat()}.csv"
    return {"filename": name, "csv": to_csv(rows, who, p["tz"]), "rows": len(rows), "include_who": who}


GET = {"config": g_config, "source": g_source, "summary": g_summary, "rows": g_rows, "csv": g_csv}
POST = {}  # read-only area (refresh() is called by datasources, the Refresh menu)


# ---------------------------------------------------------------- health and Overview

def health(today):
    from health import _row as hrow  # noqa: PLC0415
    ok, detail = archive.writable()
    rows = [hrow("Activity", "Archive", "Writable", ok, detail, "" if ok else "Check the Studio data folder permissions.")]
    try:
        import live  # noqa: PLC0415
        now = _now()
        for a in _google_accounts():
            try:
                tz = gch.account_tz(a["id"], live.query)
                live.query(a["id"], f"SELECT change_event.resource_name FROM change_event WHERE change_event.change_date_time >= "
                                    f"'{gch.gaql_time(now - dt.timedelta(days=1), tz)}' AND change_event.change_date_time <= "
                                    f"'{gch.gaql_time(now, tz)}' LIMIT 1")
                rows.append(hrow("Activity", a["name"], "Google change history", True, "change_event answered (last day, 1 row)."))
            except Exception as ex:  # noqa: BLE001
                rows.append(hrow("Activity", a["name"], "Google change history", False, _scrub(ex), "Check the Google Ads connection in Connections."))
    except Exception as ex:  # noqa: BLE001
        rows.append(hrow("Activity", "Google", "Google change history", False, _scrub(ex)))
    try:
        import meta_ads  # noqa: PLC0415
        accts = _meta_accounts()
        if not accts:
            rows.append(hrow("Activity", "Meta", "Meta activity log", None, "No Meta token or ad account."))
        for a in accts:
            try:
                meta_ads.get(f"{a['act']}/activities", fields="event_type,event_time", limit=1)
                rows.append(hrow("Activity", a["name"], "Meta activity log", True, "activities answered (1 row)."))
            except Exception as ex:  # noqa: BLE001
                rows.append(hrow("Activity", a["name"], "Meta activity log", False, _scrub(ex), "The token needs ads_read on the ad account."))
    except Exception as ex:  # noqa: BLE001
        rows.append(hrow("Activity", "Meta", "Meta activity log", False, _scrub(ex)))
    return rows


def overview(days="7"):
    n = int(days or 7)
    p = per.period(f"last_{n}") if n in (7, 30, 90) else per.period("custom", start=(_now() - dt.timedelta(days=n - 1)).date(), end=_now().date())
    s = summarise(load_all(p, timeout=12), p)
    tiles = [{"k": t["label"], "v": f"{t['v']:,}",
              "hi": (f"{'+' if t['delta'] > 0 else ''}{t['delta']:,} vs previous {n} days" if t["prev_complete"] else "no full history to compare yet")}
             for t in s["tiles"] if t["v"] or t["prev"]]
    loading = [SOURCES[k] for k, v in s["sources"].items() if v["status"] == "loading"]
    note = f"Activities in the last {n} days, per channel. Open the Activity tab for the full list."
    if loading:
        note += " Still loading: " + ", ".join(loading) + "."
    if s["sources"].get("email", {}).get("status") == "demo":
        note += " Email is demo data and is not counted."
    return {"title": "Activity", "tiles": tiles or [{"k": "Activities", "v": "0", "hi": ""}], "note": note}
