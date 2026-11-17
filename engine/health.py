#!/usr/bin/env python3
"""Ads Studio connection health. Read-only checks of the Google Ads API and the Meta Marketing API.

  python3 health.py          human summary
  python3 health.py --json   full result as JSON
  Exit code 1 when any check is red.

Each check is {"channel", "account", "check", "ok", "detail", "fix"}:
  ok True = green, None = warning (amber), False = red.

From server.py:
  health.check_all()        run every check now (a few seconds), saves health.json, returns the list
  health.cached()           last saved result {"checked_at", "results"} or None (no API calls)
  health.summary(data=None) {"status": green|amber|red|unknown, "red", "warn", "ok", "checked_at", "age_hours", "stale"}

Nothing here writes to Google Ads or Meta. Tokens and secrets are never printed: every detail
string goes through _scrub().
"""
import datetime as dt, json, os, sys, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import appconfig  # noqa: E402  (vendor_hub on sys.path)

try:
    import core
    STUDIO_DIR = core.STUDIO_DIR
except Exception:  # noqa: BLE001 - e.g. a box without the gae module
    core = None
    STUDIO_DIR = appconfig.DATA / "studio"
HEALTH_FILE = STUDIO_DIR / "health.json"
STALE_HOURS = 36
WARN_DAYS = 60

# ---------- version schedules (update when the vendors publish new versions) ----------

# Google Ads API sunset dates. Google gives a month only, so the 1st of that month is used (worst case).
# Source: https://developers.google.com/google-ads/api/docs/sunset-dates (read 10 November 2026).
# Minor releases (v23.1 ...) share the major version's URL path and sunset.
GOOGLE_SOURCE = "https://developers.google.com/google-ads/api/docs/sunset-dates"
GOOGLE_READ = "10 November 2026"
GOOGLE_SUNSET = {
    "v23": "2027-02-01",
    "v24": "2027-05-01",
    "v25": "2027-08-01",
    "v26": "2027-11-01",
    "v27": "2028-02-01",  # projected
}

# Meta Marketing API expiration dates (stricter than the Graph API dates for the same version, and the
# ads endpoints are what Studio uses). None = "TBD" on Meta's page.
# Source: https://developers.facebook.com/docs/marketing-api/marketing-api-changelog/versions (read 17 November 2026).
# After expiry Meta may auto-upgrade a call to the next version or fail it (endpoints that changed fail):
# https://developers.facebook.com/docs/marketing-api/overview/versioning
META_SOURCE = "https://developers.facebook.com/docs/marketing-api/marketing-api-changelog/versions"
META_READ = "17 November 2026"
META_EXPIRY = {
    "v18.0": "2024-08-13",
    "v19.0": "2025-02-04",
    "v20.0": "2025-05-06",
    "v21.0": "2025-09-09",
    "v22.0": "2026-02-19",
    "v23.0": "2026-06-09",
    "v24.0": "2026-11-10",
    "v25.0": "2027-02-23",
    "v26.0": None,
    "v27.0": None,
}
META_REQUIRED_SCOPES = ("ads_management", "ads_read")
META_APP_REVIEW = "Meta App Review: apply for Full access to the Marketing API"

_SECRETS = []


def _scrub(s):
    s = str(s)
    for sec in _SECRETS:
        if sec and len(sec) > 8:
            s = s.replace(sec, "***")
    return s[:400]


def _d(x):
    return f"{x.day} {x:%B %Y}"


def _row(channel, account, check, ok, detail, fix=""):
    return {"channel": channel, "account": account, "check": check, "ok": ok, "detail": _scrub(detail), "fix": fix}


def _version_row(channel, version, table, source, read_on, today, fix_where):
    d = table.get(version, "missing") if version else "missing"
    if d == "missing":
        return _row(channel, "", "API version", False, f"{version or 'unknown'} is not in Studio's schedule (source {source}, read {read_on}).",
                    f"Check {source} and add the version to health.py, or bump {fix_where} to a listed version.")
    if d is None:
        return _row(channel, "", "API version", True, f"{version}: no end date announced yet (source {source}, read {read_on}).")
    end = dt.date.fromisoformat(d)
    left = (end - today).days
    if left <= 0:
        return _row(channel, "", "API version", False, f"{version} reached end of life on {_d(end)} ({-left} days ago). Source {source}, read {read_on}.",
                    f"Bump {fix_where} to a supported version and test. See the README (Setup).")
    ok = True if left >= WARN_DAYS else None
    return _row(channel, "", "API version", ok, f"{version} supported until {_d(end)} ({left} days left). Source {source}, read {read_on}.",
                "" if ok else f"Plan the bump of {fix_where} before {_d(end)}. See the README (Setup).")


# ---------- Google ----------

def _google_accounts():
    try:
        return [(a["id"], a["name"]) for a in core.accounts()]
    except Exception:  # noqa: BLE001 - no Editor on this box (VPS): fall back to the known ids
        try:
            import gae
            return [(v, k.upper()) for k, v in gae.ACCOUNTS.items()]
        except Exception:  # noqa: BLE001
            return []


def google_checks(today):
    out = []
    try:
        import google_ads_api as g
    except Exception as e:  # noqa: BLE001
        return [_row("google", "", "Connection module", False, f"Could not load google_ads_api.py: {e}", "Reinstall Marketing Studio.")]
    for k in ("GADS_DEVELOPER_TOKEN",):
        try:
            _SECRETS.append(g._env(k, ""))
        except Exception:  # noqa: BLE001
            pass
    try:
        tok = g._token()
        _SECRETS.append(tok)
        out.append(_row("google", "", "Service account token", True, "Token obtained (adwords scope)."))
    except Exception as e:  # noqa: BLE001
        out.append(_row("google", "", "Service account token", False, f"No token: {e}",
                        "The service account key may be missing or deleted. See the README (Setup)."))
        tok = None
    out.append(_version_row("google", getattr(g, "API_VERSION", None), GOOGLE_SUNSET, GOOGLE_SOURCE, GOOGLE_READ, today,
                            "API_VERSION in engine/vendor_hub/google_ads_api.py"))
    if not tok:
        return out
    try:
        import live
    except Exception as e:  # noqa: BLE001
        out.append(_row("google", "", "Accounts", False, f"Could not load live.py: {e}", ""))
        return out
    accts = _google_accounts()
    if not accts:
        out.append(_row("google", "", "Accounts", None, "No Google Ads accounts found to test.", "See the README (Setup)."))
    for cid, name in accts:
        try:
            rows = live.query(cid, "SELECT customer.descriptive_name, customer.currency_code FROM customer LIMIT 1")
            c = (rows[0] if rows else {}).get("customer", {})
            out.append(_row("google", name, "Account answers", True, f"{c.get('descriptiveName', '?')} ({cid}), {c.get('currencyCode', '?')}"))
        except Exception as e:  # noqa: BLE001
            out.append(_row("google", name, "Account answers", False, f"{cid}: {e}",
                            "Check the account is still linked to the MCC and the service account has access. See the README."))
    return out


# ---------- Meta ----------

def _meta_version(meta_ads):
    try:
        from meta_api import GRAPH_VERSION  # the shared connection module, once it exists
        v, where = GRAPH_VERSION, "GRAPH_VERSION in shared/dashboard/connectors/meta_api.py"
    except Exception:  # noqa: BLE001
        v, where = meta_ads.API.rstrip("/").rsplit("/", 1)[-1], "API in shared/gae/studio/meta_ads.py"
    return (v if v.startswith("v") else "v" + v), where


def _meta_get(version, path, token, **params):
    """GET with the response headers (the usage and version headers live there)."""
    q = dict(params, access_token=token, locale="en_GB")
    url = f"https://graph.facebook.com/{version}/{path}?" + urllib.parse.urlencode(q)
    try:
        r = urllib.request.urlopen(url, timeout=45)
        return json.loads(r.read() or b"{}"), {k.lower(): v for k, v in r.headers.items()}
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read())["error"]
            msg = err.get("error_user_msg") or err.get("message") or f"HTTP {e.code}"
            raise RuntimeError(f"{msg} (code {err.get('code')})") from None
        except (ValueError, KeyError):
            raise RuntimeError(f"HTTP {e.code}") from None


def _debug_token(version, token):
    try:
        import meta_api
        d = meta_api.debug_token()
        return d.get("data", d)
    except Exception:  # noqa: BLE001 - module missing or different shape: ask Meta directly
        d, _ = _meta_get(version, "debug_token", token, input_token=token)
        return d.get("data", {})


def _usage(headers):
    """Highest % from Meta's usage headers, plus the access tier."""
    pct, tier = [], None
    for h in ("x-business-use-case-usage", "x-ad-account-usage", "x-app-usage"):
        try:
            v = json.loads(headers.get(h) or "null")
        except ValueError:
            continue
        items = [x for lst in v.values() for x in lst] if isinstance(v, dict) and h == "x-business-use-case-usage" else [v]
        for x in items:
            if not isinstance(x, dict):
                continue
            tier = x.get("ads_api_access_tier") or tier
            for k in ("call_count", "total_cputime", "total_time", "acc_id_util_pct"):
                if isinstance(x.get(k), (int, float)):
                    pct.append(x[k])
    return (max(pct) if pct else None), tier


def meta_checks(today):
    out = []
    try:
        import meta_ads
    except Exception as e:  # noqa: BLE001
        return [_row("meta", "", "Connection module", False, f"Could not load meta_ads.py: {e}", "")]
    token = meta_ads.TOKEN
    _SECRETS.append(token)
    version, where = _meta_version(meta_ads)
    out.append(_version_row("meta", version, META_EXPIRY, META_SOURCE, META_READ, today, where))
    if not token:
        return out + [_row("meta", "", "Token", False, "No Meta access token yet.", "See the README (Setup).")]

    try:
        d = _debug_token(version, token)
        if not d.get("is_valid"):
            out.append(_row("meta", "", "Token", False, f"Token is not valid: {(d.get('error') or {}).get('message', 'no reason given')}",
                            "Generate a new system user token. See the README (Setup)."))
        else:
            exp = d.get("expires_at") or 0
            exp_txt = "never expires" if not exp else f"expires {_d(dt.datetime.fromtimestamp(exp))}"
            missing = [s for s in META_REQUIRED_SCOPES if s not in (d.get("scopes") or [])]
            days = (dt.datetime.fromtimestamp(exp).date() - today).days if exp else None
            ok = False if missing else (None if days is not None and days < 14 else True)
            out.append(_row("meta", "", "Token", ok,
                            f"{d.get('type', '?')} token" + (f" for app \"{d['application']}\"" if d.get("application") else "") + f", {exp_txt}"
                            + (f", missing scopes: {', '.join(missing)}" if missing else ", has ads_management + ads_read"),
                            "" if ok else "Generate a new system user token with ads_management and ads_read. See the README (Setup)."))
    except Exception as e:  # noqa: BLE001
        out.append(_row("meta", "", "Token", False, f"debug_token failed: {e}", "See the README (Setup)."))

    worst, tier, upgraded = None, None, None
    for a in meta_ads.settings().get("meta_accounts", []):
        try:
            r, h = _meta_get(version, a["act"], token, fields="name,currency,account_status")
            st = r.get("account_status")
            ok = True if st == 1 else None
            out.append(_row("meta", a["name"], "Account answers", ok,
                            f"{r.get('name', '?')} ({a['act']}), {r.get('currency', '?')}" + ("" if ok else f", account_status {st} (1 = active)"),
                            "" if ok else "The ad account is not active in Meta. Check Ads Manager > Account overview."))
            p, t = _usage(h)
            if p is not None:
                worst = p if worst is None else max(worst, p)
            tier = t or tier
            upgraded = h.get("x-ad-api-version-warning") or upgraded
        except Exception as e:  # noqa: BLE001
            out.append(_row("meta", a["name"], "Account answers", False, f"{a['act']}: {e}",
                            "The system user may have lost access to this ad account. See the README."))
    if worst is not None:
        out.append(_row("meta", "", "API usage", True if worst <= 75 else None, f"Highest usage {worst}% of the current limit window.",
                        "" if worst <= 75 else "Slow down bulk edits; usage resets within the hour. Full access raises the limit ."))
    if tier:
        dev = tier == "development_access"
        out.append(_row("meta", "", "Access tier", None if dev else True,
                        f"{tier}" + (" (Meta's Limited access: heavily rate-limited, meant for development)" if dev else ""),
                        f"Apply for Full access. See {META_APP_REVIEW}." if dev else ""))
    if upgraded:
        out.append(_row("meta", "", "Version auto-upgrade", None, f"Meta says: {upgraded}",
                        "Meta is answering on a newer version than Studio asks for. Bump the version so calls don't start failing. See the README (Setup)."))
    return out


# ---------- run, cache, summary ----------

def check_all(save=True):
    today = dt.date.today()
    results = []
    try:
        import areas  # Email, SEO, SoMe connections
        area_fn = [areas.health_checks]
    except Exception:  # noqa: BLE001
        area_fn = []
    for fn in (google_checks, meta_checks, *area_fn):
        try:
            results += fn(today)
        except Exception as e:  # noqa: BLE001 - one channel crashing must not hide the other
            results.append(_row(fn.__name__.split("_")[0], "", "Health check", False, f"Check crashed: {e}", "Run python3 health.py on the box to see why."))
    if save:
        try:
            STUDIO_DIR.mkdir(parents=True, exist_ok=True)
            tmp = HEALTH_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps({"checked_at": dt.datetime.now().isoformat(timespec="seconds"), "results": results}, indent=1))
            tmp.replace(HEALTH_FILE)
        except OSError:
            pass
    return results


def cached():
    try:
        return json.loads(HEALTH_FILE.read_text())
    except (OSError, ValueError):
        return None


def summary(data=None):
    """data: a cached() dict or a results list. None = read the cache."""
    if data is None:
        data = cached()
    if isinstance(data, list):
        data = {"checked_at": dt.datetime.now().isoformat(timespec="seconds"), "results": data}
    if not data:
        return {"status": "unknown", "red": 0, "warn": 0, "ok": 0, "checked_at": None, "age_hours": None, "stale": True}
    res = data.get("results", [])
    red = sum(1 for r in res if r["ok"] is False)
    warn = sum(1 for r in res if r["ok"] is None)
    age = None
    try:
        age = round((dt.datetime.now() - dt.datetime.fromisoformat(data["checked_at"])).total_seconds() / 3600, 1)
    except (KeyError, TypeError, ValueError):
        pass
    return {"status": "red" if red else "amber" if warn else "green", "red": red, "warn": warn, "ok": len(res) - red - warn,
            "checked_at": data.get("checked_at"), "age_hours": age, "stale": age is None or age > STALE_HOURS}


def main(argv):
    res = check_all()
    s = summary(res)
    if "--json" in argv:
        print(json.dumps({"summary": s, "results": res}, indent=1))
    else:
        mark = {True: "OK  ", None: "WARN", False: "RED "}
        for r in res:
            print(f"{mark[r['ok']]} {r['channel']:6} {(r['account'] or '-')[:32]:32} {r['check']:22} {r['detail']}")
            if r["fix"]:
                print(f"{'':45}fix: {r['fix']}")
        print(f"\n{s['status'].upper()}: {s['ok']} ok, {s['warn']} warning, {s['red']} red")
    return 1 if s["red"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
