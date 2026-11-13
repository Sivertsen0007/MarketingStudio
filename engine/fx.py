"""Display-currency conversion for Marketing Studio (display only, never for drafts or pushes).

Latest ECB reference rates from the free Frankfurter API (no key), cached in core.STUDIO_DIR/fx.json. datapolicy decides
when to fetch again: in manual mode (the default) only on Refresh rates or the Refresh menu's All; in auto mode after 12 hours.
If the network fails: the last cached rates (marked stale), and only when there is no cache at all, rough fallback
constants (marked fallback, the UI says "rate may be out of date").

Money written to a draft or sent to Google or Meta is ALWAYS in the account's own currency. Nothing in this file
is called on that path: the server only uses it for Overview figures and the UI for what it shows.

  import fx
  fx.convert(10, "GBP", "USD")   # -> float, at the latest rate
  fx.info()                      # rates + date + source for /api/fx
"""
import datetime as dt
import json
import time
import urllib.request

import core
import datapolicy

TTL = 12 * 3600
HOSTS = ["https://api.frankfurter.dev/v1", "https://api.frankfurter.app"]
# Frankfurter answers 403 to urllib's default User-Agent; a real one is needed.
UA = {"User-Agent": "ml-marketing-studio-fx/1.0 (+https://masterliveaboards.com)", "Accept": "application/json"}
SOURCE = "ECB reference rate via Frankfurter"
# Rough late-2026 rates, units per 1 EUR. Used ONLY when Frankfurter is unreachable and nothing is cached.
FALLBACK = {"EUR": 1.0, "USD": 1.16, "GBP": 0.87, "DKK": 7.46, "AUD": 1.62, "THB": 37.5, "CAD": 1.58, "NOK": 11.6, "SEK": 11.0, "CHF": 0.93}
DISPLAY = ["GBP", "USD", "EUR", "DKK"]  # always offered in Settings, plus the account currencies


def _file():
    return core.STUDIO_DIR / "fx.json"


def _read_cache():
    try:
        d = json.loads(_file().read_text())
        return d if isinstance(d.get("rates"), dict) and d["rates"] else None
    except (OSError, ValueError):
        return None


def _write_cache(d):
    core.STUDIO_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _file().with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1))
    tmp.replace(_file())


def _fetch():
    """{base, date, rates} from the first Frankfurter host that answers, else None."""
    for host in HOSTS:
        try:
            raw = urllib.request.urlopen(urllib.request.Request(f"{host}/latest?base=EUR", headers=UA), timeout=15).read()
            data = json.loads(raw)
            rates = {k.upper(): float(v) for k, v in (data.get("rates") or {}).items()}
            if rates:
                rates[(data.get("base") or "EUR").upper()] = 1.0
                return {"base": (data.get("base") or "EUR").upper(), "date": data.get("date"), "rates": rates, "host": host}
        except Exception:  # noqa: BLE001 - try the next host, then the cache
            continue
    return None


def latest(force=False):
    """The rates to use now: {base, date, rates, source, fetched_at, stale, fallback}."""
    cached = _read_cache()
    if cached and not force and datapolicy.fresh("fx", time.time() - (cached.get("fetched_ts") or 0), TTL, cached.get("fetched_ts") or 0):
        return {**cached, "stale": False, "fallback": False}
    got = _fetch()
    if got:
        d = {**got, "source": SOURCE, "fetched_ts": time.time(), "fetched_at": dt.datetime.now().isoformat(timespec="minutes")}
        _write_cache(d)
        datapolicy.mark("fx")
        return {**d, "stale": False, "fallback": False}
    if cached:
        return {**cached, "stale": True, "fallback": False}
    return {"base": "EUR", "date": None, "rates": dict(FALLBACK), "source": "Built-in fallback rates (no network, no cached rates)",
            "fetched_ts": None, "fetched_at": None, "stale": True, "fallback": True}


def rate(frm, to, r=None):
    """Units of `to` per 1 `frm`, or None when a currency is unknown."""
    frm, to = (frm or "").upper(), (to or "").upper()
    if not frm or not to:
        return None
    if frm == to:
        return 1.0
    rates = (r or latest())["rates"]
    if frm not in rates or to not in rates or not rates[frm]:
        return None
    return rates[to] / rates[frm]


def convert(amount, frm, to, r=None):
    """amount in `frm` -> `to` at the latest rate. None in, None out; unknown currency raises ValueError."""
    if amount is None:
        return None
    x = rate(frm, to, r)
    if x is None:
        raise ValueError(f"no exchange rate for {frm} to {to}")
    return float(amount) * x


def nice_date(iso):
    try:
        d = dt.date.fromisoformat(iso)
        return f"{d.day} {d:%B %Y}"
    except (TypeError, ValueError):
        return "unknown date"


def note(r):
    """'ECB reference rate via Frankfurter, 9 November 2026: 1 GBP = 1.39 USD' (+ a warning when stale)."""
    usd = rate("GBP", "USD", r)
    out = f"{r.get('source', SOURCE)}, {nice_date(r.get('date'))}" + (f": 1 GBP = {usd:.2f} USD" if usd else "")
    return out + (" (rate may be out of date)" if r.get("stale") else "")


def info(force=False):
    """What /api/fx returns. `note` is the sentence the UI shows next to converted figures."""
    r = latest(force)
    return {"base": r["base"], "date": r["date"], "rates": r["rates"], "source": r["source"], "fetched_at": r.get("fetched_at"),
            "stale": r["stale"], "fallback": r["fallback"], "note": note(r)}
