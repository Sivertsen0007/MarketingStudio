"""What Marketing Studio has fetched, and refreshing one source at a time (the header's Refresh menu, the per-area
Refresh buttons, GET /api/data/status, POST /api/data/refresh and the MCP tools data_status / refresh_data).

Refreshing only reads: Google Ads (GAQL search), Meta (Graph GET), ML Suite (Email reads), Search Console, GA4,
WordPress (public/REST GET), Frankfurter rates. Nothing is pushed, sent, scheduled or published from here.

Sources:  google (account: ml, mluk or a customer id; none = every Google account)
          meta   (account: a Meta key from the account list, e.g. meta; none = every Meta account)
          email, seo (account / parts: gsc, ga4, wp; none = all three), social, activity, fx, overview (days), all
"""
import datetime as dt
import json
import time

import core
import datapolicy
import fx
import gapi
import meta_ads as meta
import overview

SEO_PARTS = {"gsc": "Search Console", "ga4": "GA4", "wp": "WordPress posts"}


def _area(name):
    import areas  # noqa: PLC0415
    return areas.AREAS.get(name)


def _google_accounts():
    return [{"key": a.get("key") or a["id"], "id": str(a["id"]), "name": a.get("name") or a["id"]} for a in gapi.accounts_any()]


def _iso_of(epoch):
    return dt.datetime.fromtimestamp(epoch).isoformat(timespec="seconds") if epoch else None


def _local(iso):
    """An ISO time with a zone (the archive keeps UTC) -> this Mac's local time without zone, like every other row."""
    if not iso:
        return None
    try:
        return dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone().replace(tzinfo=None).isoformat(timespec="seconds")
    except ValueError:
        return iso


def _social_status():
    """SoMe's own cache file (read here, never written): when Facebook + Instagram posts were last read."""
    so = _area("social")
    out = {"available": bool(so and hasattr(so, "_published")), "fetched_at": None}
    try:
        from areas import _social_store as store  # noqa: PLC0415
        c = json.loads((store.data_dir() / "social_cache_meta.json").read_text())
        out["fetched_at"] = _iso_of(c.get("ts"))
    except Exception:  # noqa: BLE001 - not read yet
        pass
    return out


def status():
    """Last fetched per source and account. Reads local files only: no API calls."""
    rows = []
    for a in _google_accounts():
        info = {}
        try:
            info = gapi.info(a["id"])
        except Exception:  # noqa: BLE001
            pass
        rows.append({"source": "google", "account": a["key"], "label": a["name"], "fetched_at": info.get("fetched_at"),
                     "refreshing": bool(info.get("refreshing"))})
    for a in meta.accounts():
        rows.append({"source": "meta", "account": a["key"], "label": a["name"], "fetched_at": a.get("last_download"),
                     "refreshing": a["act"] in meta._bg})
    rows.append({"source": "email", "account": None, "label": "Email", "fetched_at": datapolicy.fetched("email")})
    try:
        from areas import _seo_common as sc  # noqa: PLC0415
        prefixes = {"gsc": ("gsc_",), "ga4": ("ga4_",), "wp": ("posts", "categories")}
        for k, label in SEO_PARTS.items():
            rows.append({"source": "seo", "account": k, "label": label, "fetched_at": _iso_of(sc.newest(prefixes[k]))})
    except Exception:  # noqa: BLE001
        pass
    so = _social_status()
    rows.append({"source": "social", "account": None, "label": "SoMe", "fetched_at": so["fetched_at"], "available": so["available"]})
    ac = _area("activity")
    sync = ac.sync_status() if ac else {}
    rows.append({"source": "activity", "account": None, "label": "Activity", "fetched_at": _local(sync.get("google")) or datapolicy.fetched("activity"),
                 "google_synced": _local(sync.get("google")), "meta_synced": _local(sync.get("meta")), "warning": sync.get("warning")})
    try:
        rows.append({"source": "fx", "account": None, "label": "Exchange rates", "fetched_at": (fx._read_cache() or {}).get("fetched_at")})
    except Exception:  # noqa: BLE001
        pass
    return {"mode": datapolicy.mode(), "sources": rows, "activity_warning": sync.get("warning")}


def _one_google(acct, ctx):
    return {"source": "google", "account": acct, **gapi.refresh(acct, ctx.get("days"), ctx.get("start"), ctx.get("end"))}


def _one_meta(key):
    meta.use(key)
    d = meta.refresh_account()
    return {"source": "meta", "account": key, "fetched_at": d["fetched_at"]}


def refresh(source, account=None, **ctx):
    """Refresh one source (or one account of it) now and return what was fetched. Read-only towards every service.
    ctx: days / start / end (the stats window on screen) for Google and Meta, days for overview."""
    source = (source or "").lower()
    out = []
    if source == "all":
        t0 = time.time()
        for s in ("google", "meta", "email", "seo", "social", "activity", "fx"):
            try:
                out += refresh(s, None, **ctx)["results"]
            except Exception as ex:  # noqa: BLE001 - one source failing must not stop the rest
                out.append({"source": s, "error": str(ex)[:300]})
        datapolicy.invalidate("overview", at=t0)
        return {"results": out, "status": status()}
    if source == "google":
        keys = [account] if account else [a["key"] for a in _google_accounts()]
        for k in keys:
            try:
                out.append(_one_google(core.resolve(k), ctx))
            except Exception as ex:  # noqa: BLE001
                out.append({"source": "google", "account": k, "error": str(ex)[:300]})
    elif source == "meta":
        keys = [account] if account else [a["key"] for a in meta.accounts()]
        for k in keys:
            try:
                out.append(_one_meta(k))
            except Exception as ex:  # noqa: BLE001
                out.append({"source": "meta", "account": k, "error": str(ex)[:300]})
    elif source == "email":
        em = _area("email")
        if not em:
            raise ValueError("the Email area didn't load")
        out.append({"source": "email", **em.refresh({})})
    elif source == "seo":
        seo = _area("seo")
        if not seo:
            raise ValueError("the SEO area didn't load")
        parts = [account] if account else ctx.get("parts") or list(SEO_PARTS)
        bad = [p for p in parts if p not in SEO_PARTS]
        if bad:
            raise ValueError(f"SEO part must be one of {', '.join(SEO_PARTS)}")
        out.append({"source": "seo", **seo.refresh({"parts": parts})})
    elif source == "social":
        so = _area("social")
        if not so or not hasattr(so, "_published"):
            raise ValueError("SoMe refresh is coming")
        r = so.refresh({})  # the SoMe area's own read (Facebook + Instagram posts), forced
        out.append({"source": "social", "fetched_at": r.get("fetched_at"), "errors": r.get("errors", [])})
    elif source == "activity":
        ac = _area("activity")
        if not ac:
            raise ValueError("the Activity area didn't load")
        r = ac.refresh({})
        errs = [f"{x['account']}: {x['error']}" for k in ("google", "meta") for x in r.get(k, []) if x.get("error")]
        out.append({"source": "activity", "google": r.get("google"), "meta": r.get("meta"), "warning": r["sync"].get("warning"), "errors": errs})
    elif source == "fx":
        out.append({"source": "fx", "fetched_at": fx.info(force=True).get("fetched_at")})
    elif source == "overview":
        days = str(ctx.get("days") or "7")
        o = overview.build(days, "own", force=True)
        out.append({"source": "overview", "days": o["days"], "errors": o.get("errors", [])})
    else:
        raise ValueError("source must be all, google, meta, email, seo, social, activity, fx or overview")
    if source in ("email", "seo", "social"):
        datapolicy.invalidate("overview")
    return {"results": out}
