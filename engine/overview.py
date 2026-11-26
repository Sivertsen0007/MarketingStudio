"""One view of all our ads: Google (every account Editor holds) and Meta, spend and results per campaign,
grouped by destination. Read-only. Currencies are kept apart (Google GBP, Meta USD) - never summed across, unless
the user picked a display currency in Settings: then convert() adds converted figures (latest ECB rate, fx.py) next to
the native ones and an "All channels" total. Native amounts always stay in the output.

Cached per days value (overview_cache_<days>.json): opening Overview again pulls nothing. The Refresh menu's All or
Overview's own Refresh button makes it build again (datapolicy source "overview"); a Google or Meta refresh is shown
as a hint. A refresh of Email, SEO or SoMe rebuilds only that area's block (from the area's fresh cache) the next time
Overview opens: the Google and Meta figures are left alone. When a rebuild can't reach Google, Meta or an area, the
previous figures for that part are kept and marked stale instead of being wiped.
"""
import copy
import datetime as dt
import json
import re
import time

import core
import datapolicy
import fx
import gapi
import live
import meta_ads as meta

WINDOWS = {"7": ("LAST_7_DAYS", "last_7d"), "14": ("LAST_14_DAYS", "last_14d"), "30": ("LAST_30_DAYS", "last_30d")}
# name fragment -> destination (product-reference slugs, plus how campaigns actually name them)
DEST = [("red sea", "Egypt / Red Sea"), ("egypt", "Egypt / Red Sea"), ("saudi", "Saudi Arabia"), ("maldives", "Maldives"),
        ("thailand", "Thailand"), ("similan", "Thailand"), ("myanmar", "Myanmar"), ("mergui", "Myanmar"), ("galapagos", "Galapagos"),
        ("indonesia", "Indonesia"), ("raja ampat", "Indonesia"), ("komodo", "Indonesia"), ("banda", "Indonesia"), ("palau", "Palau"),
        ("philippines", "Philippines"), ("tubbataha", "Philippines"), ("solomon", "Solomon Islands"), ("truk", "Truk Lagoon"),
        ("chuuk", "Truk Lagoon"), ("bikini", "Bikini Atoll")]


def destination(name):
    n = (name or "").lower()
    hits = sorted({d for k, d in DEST if k in n})
    return " + ".join(hits) if hits else "General / brand"


def _google(days, failed=None):
    """-> (rows, errors). failed (a list) collects the names of accounts whose campaigns could not be read."""
    out, errors = [], []
    during = WINDOWS[days][0]
    try:
        accts = gapi.accounts_any()  # the API account list works without Google Ads Editor
    except Exception as ex:  # noqa: BLE001
        return [], [f"Google: {ex}"]
    for a in accts:
        try:
            rows = live.query(a["id"], "SELECT campaign.name, campaign.status, customer.currency_code, metrics.cost_micros, metrics.clicks, "
                                       f"metrics.impressions, metrics.conversions FROM campaign WHERE segments.date DURING {during} "
                                       "AND metrics.impressions > 0")
        except Exception as ex:  # noqa: BLE001 - one account failing should not hide the rest
            errors.append(f"Google {a['name']}: {ex}")
            if failed is not None:
                failed.append(a["name"])
            continue
        sc = {}
        try:  # ML's select_cabin conversion per campaign (conversion segments need their own query)
            for x in live.query(a["id"], "SELECT campaign.name, segments.conversion_action_name, metrics.all_conversions FROM campaign "
                                         f"WHERE segments.date DURING {during}"):
                if "select_cabin" in (x.get("segments", {}).get("conversionActionName") or "").lower():
                    n = x["campaign"]["name"]
                    sc[n] = sc.get(n, 0) + float(x.get("metrics", {}).get("allConversions", 0))
        except Exception as ex:  # noqa: BLE001
            errors.append(f"Google {a['name']} Select Cabin: {ex}")
        for r in rows:
            m, c = r.get("metrics", {}), r["campaign"]
            out.append({"channel": "Google", "account": a["name"], "campaign": c["name"], "status": c["status"].title(),
                        "currency": r.get("customer", {}).get("currencyCode", ""), "spend": round(int(m.get("costMicros", 0)) / 1e6, 2),
                        "clicks": int(m.get("clicks", 0)), "impressions": int(m.get("impressions", 0)),
                        "results": round(float(m.get("conversions", 0)), 1), "select_cabin": round(sc.get(c["name"], 0), 1)})
    return out, errors


def _meta(days):
    if not meta.TOKEN:
        return [], []
    try:
        cur = meta.load()["account"]["currency"]
        rows = meta.get_all(f"{meta.act()}/insights", level="campaign", date_preset=WINDOWS[days][1],
                            fields="campaign_name,campaign_id," + meta.F_INS)
    except Exception as ex:  # noqa: BLE001
        return [], [f"Meta: {ex}"]
    status = {c["id"]: c["status"].title() for c in meta.load()["campaigns"]}
    return [{"channel": "Meta", "account": "ML Meta", "campaign": r["campaign_name"], "status": status.get(r["campaign_id"], ""),
             "currency": cur, "spend": round(float(r.get("spend", 0)), 2), "clicks": int(r.get("clicks", 0)),
             "impressions": int(r.get("impressions", 0)),
             "results": round(sum(float(x["value"]) for x in r.get("actions", []) if x["action_type"] in meta.RESULT_ACTIONS), 1),
             "select_cabin": meta._conv_vals(r, float(r.get("spend", 0)))["select_cabin"] or 0}
            for r in rows], []


def convert(out, display, rates=None):
    """Add display-currency figures to a build() result (in place) and return it. display 'own' or None: unchanged."""
    if not display or display == "own":
        out["display"] = None
        return out
    r = rates or fx.latest()
    cv = lambda v, cur: None if v is None or fx.rate(cur, display, r) is None else round(fx.convert(v, cur, display, r), 2)  # noqa: E731
    skipped = set()
    for x in out["campaigns"]:
        x["spend_display"] = cv(x["spend"], x["currency"])
        if x["spend_display"] is None:
            skipped.add(x["currency"] or "unknown")
    for t in out["totals"]:
        t["spend_display"] = cv(t["spend"], t["currency"])
        t["cost_per_select_cabin_display"] = cv(t["cost_per_select_cabin"], t["currency"])
    for d in out["destinations"]:
        d["spend_display"] = round(sum(x["spend_display"] for x in out["campaigns"] if x["destination"] == d["destination"] and x["spend_display"] is not None), 2)
    ok = [t for t in out["totals"] if t["spend_display"] is not None]
    a = {"currency": display, "spend": round(sum(t["spend_display"] for t in ok), 2), "clicks": sum(t["clicks"] for t in ok),
         "results": round(sum(t["results"] for t in ok), 1), "select_cabin": round(sum(t["select_cabin"] for t in ok), 1),
         "campaigns": sum(t["campaigns"] for t in ok), "channels": [t["channel"] for t in ok]}
    a["cost_per_select_cabin"] = round(a["spend"] / a["select_cabin"], 2) if a["select_cabin"] else None
    out["all_channels"] = a
    out["display"] = {"currency": display, "note": f"Converted to {display} at the latest ECB rate ({fx.note(r)}). Period stats use the latest rate, not each day's rate."
                      + (f" Not converted (no rate): {', '.join(sorted(skipped))}." if skipped else "")}
    return out


def _cache_file(days):
    return core.STUDIO_DIR / f"overview_cache_{days}.json"


def _read_cache(days):
    try:
        return json.loads(_cache_file(days).read_text())
    except (OSError, ValueError):
        return None


def _write_cache(days, raw):
    try:
        core.STUDIO_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _cache_file(days).with_suffix(".tmp")
        tmp.write_text(json.dumps(raw, ensure_ascii=False))
        tmp.replace(_cache_file(days))
        return True
    except OSError:
        return False


def cached(days="7"):
    """The cached raw overview for `days` if it may be used, else None. No API calls."""
    days = days if days in WINDOWS else "7"
    d = _read_cache(days)
    if d is None:
        return None
    if not datapolicy.fresh("overview", time.time() - d.get("_at", 0), None, d.get("_at", 0)):
        return None
    return d


# ---------- the Email / SEO / SoMe / Activity blocks ----------

AREA_SOURCES = ("email", "seo", "social")  # refreshing one of these rebuilds only its own block


def _root(source):
    """datapolicy source -> its root: 'seo.gsc' -> 'seo', 'google:123' -> 'google'."""
    return str(source).split(":", 1)[0].split(".", 1)[0]


def _refreshed():
    try:
        return datapolicy._read()["refreshed"]
    except Exception:  # noqa: BLE001
        return {}


def tile(t):
    """One area tile in the contract the page uses: hi (bool, highlighted) and title (str, the hover breakdown).
    Areas that passed their breakdown text in hi (older contract) get it moved to title."""
    t = dict(t)
    hi = t.get("hi")
    if isinstance(hi, str):
        if hi.strip() and not t.get("title"):
            t["title"] = hi
        t["hi"] = False
    else:
        t["hi"] = bool(hi)
    return t


def _area_label(name, mod):
    return getattr(mod, "LABEL", name)


def _area_blocks(days, only=None, old=None, errors=None):
    """Area blocks in area order. only: rebuild just these area names and keep the others from old. An area whose
    overview fails keeps its block from old, marked stale (errors gets one line)."""
    try:
        import areas  # noqa: PLC0415 - Email, SEO, SoMe add their own block: overview(days) -> {"title", "tiles", "note"}
    except Exception:  # noqa: BLE001
        return list(old or [])
    prev = {}
    for b in old or []:
        prev[b.get("area") or b.get("title")] = b
    out = []
    for name, mod in areas.AREAS.items():
        fn = getattr(mod, "overview", None)
        if not fn:
            continue
        before = prev.get(name) or prev.get(_area_label(name, mod))
        if only is not None and name not in only:
            if before:
                out.append(before)
            continue
        try:
            b = fn(days)
            if b:
                out.append({**b, "area": name, "built_at": time.time()})
        except Exception as ex:  # noqa: BLE001
            if errors is not None:
                errors.append(f"{_area_label(name, mod)}: {ex}" + (" Showing the previous figures." if before else ""))
            if before:
                out.append({**before, "area": name, "stale": True})
    return out


def _refresh_areas(days, raw):
    """Rebuild in place the area blocks whose source was refreshed after the block was built; True when anything changed."""
    ref, built = _refreshed(), raw.get("_area_at") or {}
    due = [n for n in AREA_SOURCES
           if max((v for k, v in ref.items() if _root(k) == n), default=0) > built.get(n, raw.get("_at", 0))]
    if not due:
        return False
    try:
        import areas  # noqa: PLC0415
        labels = {_area_label(n, areas.AREAS[n]) for n in due if n in areas.AREAS}
    except Exception:  # noqa: BLE001
        labels = set()
    errs = []
    raw["areas"] = _area_blocks(days, only=set(due), old=raw.get("areas"), errors=errs)
    raw["errors"] = [e for e in raw.get("errors") or [] if e.split(":", 1)[0] not in labels] + errs
    now = time.time()
    raw["_area_at"] = {**built, **{n: now for n in due}}
    return True


def build(days="7", display="own", force=False):
    """The overview, from its cache unless force (or nothing cached / refreshed since). Display conversion is applied
    to a copy each time, so changing the display currency pulls nothing."""
    days = str(days) if str(days) in WINDOWS else "7"
    raw = None if force else cached(days)
    if raw is None:
        t0 = time.time()
        raw = _build(days, _read_cache(days))
        raw["fetched_at"] = dt.datetime.now().isoformat(timespec="seconds")
        raw["_at"] = t0
        if _write_cache(days, raw):
            datapolicy.mark(f"overview:{days}")
    elif _refresh_areas(days, raw):  # Email / SEO / SoMe refreshed since: only their blocks, Google and Meta untouched
        _write_cache(days, raw)
    out = copy.deepcopy(raw)
    built = out.pop("_at", 0)
    area_at = out.pop("_area_at", None) or {}
    out["areas"] = [{**b, "tiles": [tile(t) for t in b.get("tiles") or []]} for b in out.get("areas") or []]
    # sources refreshed after this Overview was built: shown as a hint, never a rebuild on their own
    # (an area whose block was rebuilt after its refresh is up to date)
    out["newer_sources"] = sorted(k for k, v in _refreshed().items()
                                  if v > max(built, area_at.get(_root(k), 0)) and not k.startswith("overview") and k != "all")
    return convert(out, display)


def _build(days, old=None):
    failed = []
    g, ge = _google(days, failed)
    m, me = _meta(days)
    # a channel (or Google account) that could not be read keeps its rows from the previous Overview, marked stale
    prev = (old or {}).get("campaigns") or []
    when = (old or {}).get("fetched_at")
    kept = [r for r in prev if r.get("channel") == "Google" and r.get("account") in failed]
    if me and not m:
        kept += [r for r in prev if r.get("channel") == "Meta"]
    for r in kept:
        r["stale"] = True
    for name in sorted({r["account"] for r in kept}):
        ge.append(f"{name}: could not be read now, showing the figures fetched {when or 'earlier'}.")
    g += [r for r in kept if r.get("channel") == "Google"]
    m += [r for r in kept if r.get("channel") == "Meta"]
    rows = g + m
    for r in rows:
        r["destination"] = destination(r["campaign"])
    totals = {}
    for r in rows:
        k = (r["channel"], r["currency"])
        t = totals.setdefault(k, {"channel": r["channel"], "currency": r["currency"], "spend": 0, "clicks": 0, "results": 0, "select_cabin": 0, "campaigns": 0})
        t["spend"] += r["spend"]
        t["clicks"] += r["clicks"]
        t["results"] += r["results"]
        t["select_cabin"] += r["select_cabin"]
        t["campaigns"] += 1
    by_dest = {}
    for r in rows:
        d = by_dest.setdefault(r["destination"], {"destination": r["destination"], "spend": {}, "clicks": 0, "results": 0, "select_cabin": 0})
        key = f"{r['channel']} {r['currency']}"
        d["spend"][key] = round(d["spend"].get(key, 0) + r["spend"], 2)
        d["clicks"] += r["clicks"]
        d["results"] = round(d["results"] + r["results"], 1)
        d["select_cabin"] = round(d["select_cabin"] + r["select_cabin"], 1)
    for t in totals.values():
        t["spend"], t["results"], t["select_cabin"] = round(t["spend"], 2), round(t["results"], 1), round(t["select_cabin"], 1)
        t["cost_per_select_cabin"] = round(t["spend"] / t["select_cabin"], 2) if t["select_cabin"] else None
    # Email, SEO, SoMe, Activity add their own block: overview(days) -> {"title", "tiles": [{"k", "v", "hi", "title"}], "note"}
    blocks = _area_blocks(days, old=(old or {}).get("areas"), errors=ge)
    return ({"days": int(days), "totals": list(totals.values()), "areas": blocks, "destinations": sorted(by_dest.values(), key=lambda d: d["destination"]),
            "campaigns": sorted(rows, key=lambda r: -r["spend"]), "errors": ge + me, "_area_at": {n: time.time() for n in AREA_SOURCES},
            "note": "Select Cabin: Google's GA4 select_cabin conversion and Meta's SelectCabin pixel event (each counted its own way). Results: Google conversions vs Meta pixel purchases/leads/custom events - not the same thing, compare with care."})
