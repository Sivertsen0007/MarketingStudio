"""One view of all our ads: Google (every account Editor holds) and Meta, spend and results per campaign,
grouped by destination. Read-only. Currencies are kept apart (Google GBP, Meta USD) - never summed across, unless
the user picked a display currency in Settings: then convert() adds converted figures (latest ECB rate, fx.py) next to
the native ones and an "All channels" total. Native amounts always stay in the output.

Cached per days value (overview_cache_<days>.json): opening Overview again pulls nothing. A refresh of Google or Meta
(any account), the Refresh menu's All, or Overview's own Refresh button makes it build again (datapolicy source "overview").
"""
import copy
import datetime as dt
import json
import re
import time

import core
import datapolicy
import fx
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


def _google(days):
    out, errors = [], []
    during = WINDOWS[days][0]
    for a in core.accounts():
        try:
            rows = live.query(a["id"], "SELECT campaign.name, campaign.status, customer.currency_code, metrics.cost_micros, metrics.clicks, "
                                       f"metrics.impressions, metrics.conversions FROM campaign WHERE segments.date DURING {during} "
                                       "AND metrics.impressions > 0")
        except Exception as ex:  # noqa: BLE001 - one account failing should not hide the rest
            errors.append(f"Google {a['name']}: {ex}")
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


def cached(days="7"):
    """The cached raw overview for `days` if it may be used, else None. No API calls."""
    days = days if days in WINDOWS else "7"
    try:
        d = json.loads(_cache_file(days).read_text())
    except (OSError, ValueError):
        return None
    if not datapolicy.fresh("overview", time.time() - d.get("_at", 0), None, d.get("_at", 0)):
        return None
    return d


def build(days="7", display="own", force=False):
    """The overview, from its cache unless force (or nothing cached / refreshed since). Display conversion is applied
    to a copy each time, so changing the display currency pulls nothing."""
    days = str(days) if str(days) in WINDOWS else "7"
    raw = None if force else cached(days)
    if raw is None:
        t0 = time.time()
        raw = _build(days)
        raw["fetched_at"] = dt.datetime.now().isoformat(timespec="seconds")
        raw["_at"] = t0
        try:
            core.STUDIO_DIR.mkdir(parents=True, exist_ok=True)
            tmp = _cache_file(days).with_suffix(".tmp")
            tmp.write_text(json.dumps(raw, ensure_ascii=False))
            tmp.replace(_cache_file(days))
            datapolicy.mark(f"overview:{days}")
        except OSError:
            pass
    out = copy.deepcopy(raw)
    built = out.pop("_at", 0)
    # sources refreshed after this Overview was built: shown as a hint, never a rebuild on their own
    try:
        out["newer_sources"] = sorted(k for k, v in datapolicy._read()["refreshed"].items()
                                      if v > built and not k.startswith("overview") and k != "all")
    except Exception:  # noqa: BLE001
        out["newer_sources"] = []
    return convert(out, display)


def _build(days):
    g, ge = _google(days)
    m, me = _meta(days)
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
    blocks = []
    try:
        import areas  # Email, SEO, SoMe add their own block: overview(days) -> {"title", "tiles": [{"k", "v", "hi"}], "note"}
        for name, mod in areas.AREAS.items():
            fn = getattr(mod, "overview", None)
            if fn:
                try:
                    b = fn(days)
                    if b:
                        blocks.append(b)
                except Exception as ex:  # noqa: BLE001
                    ge.append(f"{getattr(mod, 'LABEL', name)}: {ex}")
    except Exception:  # noqa: BLE001
        pass
    return ({"days": int(days), "totals": list(totals.values()), "areas": blocks, "destinations": sorted(by_dest.values(), key=lambda d: d["destination"]),
            "campaigns": sorted(rows, key=lambda r: -r["spend"]), "errors": ge + me,
            "note": "Select Cabin: Google's GA4 select_cabin conversion and Meta's SelectCabin pixel event (each counted its own way). Results: Google conversions vs Meta pixel purchases/leads/custom events - not the same thing, compare with care."})
