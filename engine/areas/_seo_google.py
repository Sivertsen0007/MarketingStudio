"""SEO area: Google Search Console and GA4, read-only, with the dashboard's user OAuth token
(the person's Google sign-in via appconfig; scopes webmasters.readonly + analytics.readonly).
"""
import datetime as dt
import json
import sys
import threading
import time
import urllib.parse
from pathlib import Path

from . import _seo_common as c

SITE = "sc-domain:masterliveaboards.com"
GA4_PROPERTY = "264228970"
SELECT_CABIN = "select_cabin"  # GA4 event name, checked in GA4 on 10 November 2026 (select_cabin_all also exists)
ORGANIC = "Organic Search"
GSC_TTL, GA4_TTL = 60 * 60, 30 * 60
INSPECT_TTL = 24 * 3600
INSPECT_DAILY_CAP = 1900  # Google's limit is about 2,000 a day per property

_creds, _creds_at, _lock = None, 0, threading.Lock()


def _token():
    import appconfig  # noqa: PLC0415
    return appconfig.google_token("seo")


def scopes():
    import appconfig  # noqa: PLC0415
    if appconfig.get("GOOGLE_REFRESH_TOKEN"):
        return list(appconfig.GOOGLE_SCOPES)
    return []


def _post(url, body):
    st, _, res = c.http(url, "POST", body, headers={"Authorization": f"Bearer {_token()}"}, timeout=90)
    if st != 200:
        raise RuntimeError(f"Google answered {st}: {c.err_text(res)}")
    return res


def _get(url):
    st, _, res = c.http(url, headers={"Authorization": f"Bearer {_token()}"}, timeout=60)
    if st != 200:
        raise RuntimeError(f"Google answered {st}: {c.err_text(res)}")
    return res


# ---------- date ranges ----------

def date_range(q):
    """q: range=7|28|90 or start/end (YYYY-MM-DD). GSC data lags, so ranges end 2 days ago. Returns (start, end, prev_start, prev_end)."""
    today = dt.date.today()
    if q.get("start") and q.get("end"):
        s, e = dt.date.fromisoformat(q["start"]), dt.date.fromisoformat(q["end"])
        if e < s:
            s, e = e, s
    else:
        n = int(q.get("range") or 28)
        n = n if n in (7, 14, 28, 30, 90) else 28
        e = today - dt.timedelta(days=2)
        s = e - dt.timedelta(days=n - 1)
    days = (e - s).days + 1
    pe = s - dt.timedelta(days=1)
    ps = pe - dt.timedelta(days=days - 1)
    return s.isoformat(), e.isoformat(), ps.isoformat(), pe.isoformat()


# ---------- Search Console ----------

def _sa(dims, start, end, limit=25000, max_rows=100000, filters=None):
    url = f"https://www.googleapis.com/webmasters/v3/sites/{urllib.parse.quote(SITE, safe='')}/searchAnalytics/query"
    rows, start_row = [], 0
    while True:
        body = {"startDate": start, "endDate": end, "dimensions": dims, "rowLimit": limit, "startRow": start_row, "dataState": "final"}
        if filters:
            body["dimensionFilterGroups"] = [{"filters": filters}]
        got = _post(url, body).get("rows", [])
        rows += got
        if len(got) < limit or len(rows) >= max_rows:
            return rows
        start_row += limit


def _m(r):
    return {"clicks": int(r.get("clicks", 0)), "impressions": int(r.get("impressions", 0)),
            "ctr": round(float(r.get("ctr", 0)), 4), "position": round(float(r.get("position", 0)), 1)}


def gsc_pages(start, end, force=False):
    """{url key: {clicks, impressions, ctr, position, page}}"""
    def load():
        out = {}
        for r in _sa(["page"], start, end):
            k = c.url_key(r["keys"][0])
            if k and k not in out:  # http/https or www duplicates: keep the first (largest) one
                out[k] = {**_m(r), "page": r["keys"][0]}
        return out
    return c.cached(f"gsc_pages_{start}_{end}", GSC_TTL, load, force=force)


def gsc_query_page(start, end, force=False):
    """[[query, url key, clicks, impressions, ctr, position]] (compact, for opportunities and cannibalisation)."""
    def load():
        return [[r["keys"][0], c.url_key(r["keys"][1]), *_m(r).values()] for r in _sa(["query", "page"], start, end, max_rows=150000)]
    return c.cached(f"gsc_qp_{start}_{end}", GSC_TTL, load, force=force)


def gsc_totals(start, end):
    def load():
        rows = _sa([], start, end, limit=10)
        return _m(rows[0]) if rows else {"clicks": 0, "impressions": 0, "ctr": 0, "position": 0}
    return c.cached(f"gsc_tot_{start}_{end}", GSC_TTL, load)


def sitemaps():
    def load():
        return _get(f"https://www.googleapis.com/webmasters/v3/sites/{urllib.parse.quote(SITE, safe='')}/sitemaps").get("sitemap", [])
    return c.cached("gsc_sitemaps", 6 * 3600, load)


def inspection_link(url):
    return ("https://search.google.com/search-console/inspect?resource_id=" + urllib.parse.quote(SITE, safe="")
            + "&id=" + urllib.parse.quote(url, safe=""))


def _quota():
    return c.read_json("seo_inspect_quota.json", {"date": "", "n": 0})


def quota_left():
    q = _quota()
    return INSPECT_DAILY_CAP - (q["n"] if q.get("date") == dt.date.today().isoformat() else 0)


def inspected(url):
    """The cached URL Inspection result (no call)."""
    return c.read_json("seo_inspect.json", {}).get(url)


def inspect(url, force=False):
    """URL Inspection for one URL, cached 24 h, counted against the daily quota."""
    have = inspected(url)
    if have and not force and time.time() - have.get("at", 0) < INSPECT_TTL:
        return have
    if quota_left() <= 0:
        raise RuntimeError("URL Inspection daily quota used up (about 2,000 a day). Try again tomorrow.")
    res = _post("https://searchconsole.googleapis.com/v1/urlInspection/index:inspect", {"inspectionUrl": url, "siteUrl": SITE})
    ir = (res.get("inspectionResult") or {})
    ix = ir.get("indexStatusResult") or {}
    row = {"at": time.time(), "url": url, "verdict": ix.get("verdict"), "coverage": ix.get("coverageState"),
           "indexing": ix.get("indexingState"), "robots": ix.get("robotsTxtState"), "fetch": ix.get("pageFetchState"),
           "last_crawl": ix.get("lastCrawlTime"), "google_canonical": ix.get("googleCanonical"), "user_canonical": ix.get("userCanonical"),
           "link": ir.get("inspectionResultLink") or inspection_link(url)}
    today = dt.date.today().isoformat()
    with c.locked_json("seo_inspect_quota.json", {"date": today, "n": 0}) as q:
        if q.get("date") != today:
            q["date"], q["n"] = today, 0
        q["n"] += 1
    with c.locked_json("seo_inspect.json", {}) as d:
        d[url] = row
    return row


def indexed(row):
    return bool(row) and row.get("verdict") == "PASS"


# ---------- GA4 ----------

def _ga4(body):
    res = _post(f"https://analyticsdata.googleapis.com/v1beta/properties/{GA4_PROPERTY}:runReport", body)
    out = []
    for r in res.get("rows", []):
        out.append([d["value"] for d in r.get("dimensionValues", [])] + [float(m["value"]) for m in r.get("metricValues", [])])
    return out


def _organic(extra=None):
    f = [{"filter": {"fieldName": "sessionDefaultChannelGroup", "stringFilter": {"value": ORGANIC}}}]
    if extra:
        f.append({"filter": extra})
    return {"andGroup": {"expressions": f}} if len(f) > 1 else f[0]


def ga4_landing(start, end, force=False):
    """{url key: {"sessions", "select_cabin"}} for organic search sessions."""
    def load():
        out = {}
        for path, n in _ga4({"dateRanges": [{"startDate": start, "endDate": end}], "dimensions": [{"name": "landingPage"}],
                             "metrics": [{"name": "sessions"}], "dimensionFilter": _organic(), "limit": 100000}):
            k = c.url_key(path) if path.startswith("/") else ""
            if k:
                out.setdefault(k, {"sessions": 0, "select_cabin": 0})["sessions"] += int(n)
        for path, n in _ga4({"dateRanges": [{"startDate": start, "endDate": end}], "dimensions": [{"name": "landingPage"}],
                             "metrics": [{"name": "eventCount"}], "limit": 100000,
                             "dimensionFilter": _organic({"fieldName": "eventName", "stringFilter": {"value": SELECT_CABIN}})}):
            k = c.url_key(path) if path.startswith("/") else ""
            if k:
                out.setdefault(k, {"sessions": 0, "select_cabin": 0})["select_cabin"] += int(n)
        return out
    return c.cached(f"ga4_lp_{start}_{end}", GA4_TTL, load, force=force)


def ga4_organic_totals(start, end):
    def load():
        s = _ga4({"dateRanges": [{"startDate": start, "endDate": end}], "metrics": [{"name": "sessions"}], "dimensionFilter": _organic()})
        e = _ga4({"dateRanges": [{"startDate": start, "endDate": end}], "metrics": [{"name": "eventCount"}],
                  "dimensionFilter": _organic({"fieldName": "eventName", "stringFilter": {"value": SELECT_CABIN}})})
        return {"sessions": int(s[0][0]) if s else 0, "select_cabin": int(e[0][0]) if e else 0}
    return c.cached(f"ga4_tot_{start}_{end}", GA4_TTL, load)
