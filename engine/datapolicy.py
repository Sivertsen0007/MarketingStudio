"""When Marketing Studio may pull data again. One rule for every cache in Studio (Ads, Email, SEO, Activity, Overview, rates).

Mode (settings.json key "data_policy", default "manual"):
  manual  once something is cached it is kept until the user presses Refresh (or Push live / Undo refreshes that one
          account). No time-based expiry, no background re-fetch, no fetch on page load or tab switch. When nothing is
          cached yet (first time, or a date range never fetched), it is fetched once.
  auto    each cache's own time limit (ttl), with background re-fetch where the caller does that, and the scheduler's
          jobs that fetch data (scheduler.py refresh=True) run on their schedule. Chosen by the user in Settings >
          Background jobs ("Data refresh"); set_mode() saves it.

Every cache check asks fresh(source, age, ttl, filled_at). A Refresh calls invalidate(source), so in-memory copies made
before it (in this process or the MCP server's) are no longer fresh. mark(source) records when a source was last fetched
(for GET /api/data/status).

Sources are keys like "google:8931136258", "meta:meta", "email", "seo.gsc", "seo.ga4", "seo.wp", "social", "activity",
"overview", "fx". invalidate("google") also covers "google:<cid>"; invalidate("all") covers everything.

Debug: STUDIO_CALL_LOG=1 counts every outgoing HTTP call (host + path only, never the query string or headers) so a test
can prove nothing is pulled between refreshes. See install_call_log() and calls().
"""
import collections
import fcntl
import datetime as dt
import json
import os
import threading
import time
import urllib.request
from pathlib import Path

DIR = None  # tests set this; otherwise core.STUDIO_DIR
MODES = ("manual", "auto")
_lock = threading.RLock()
_mem = {"mtime": None, "data": None, "path": None}


def _dir():
    d = DIR or os.environ.get("STUDIO_DATA_DIR")
    if d is None:
        import core  # noqa: PLC0415 - lazy, so tests can run without Editor
        d = core.STUDIO_DIR
    p = Path(d)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _status_file():
    return _dir() / "data_status.json"


def mode():
    """'manual' (default) or 'auto', from settings.json "data_policy"."""
    try:
        m = json.loads((_dir() / "settings.json").read_text()).get("data_policy")
    except (OSError, ValueError, AttributeError):
        m = None
    return m if m in MODES else "manual"


def set_mode(m):
    """Save the data policy (the Settings switch). Only 'manual' or 'auto'. -> {data_policy}."""
    if m not in MODES:
        raise ValueError("data mode must be manual or auto")
    f = _dir() / "settings.json"
    with _lock, open(_dir() / ".settings_policy.lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            s = json.loads(f.read_text())
        except (OSError, ValueError):
            s = {}
        if not isinstance(s, dict):
            s = {}
        s["data_policy"] = m
        tmp = f.with_suffix(f".{os.getpid()}.policy.tmp")
        tmp.write_text(json.dumps(s, indent=1, ensure_ascii=False))
        tmp.replace(f)
    return {"data_policy": mode()}


def manual():
    return mode() == "manual"


def background_ok():
    """May a stale cache be re-fetched in a background thread? Only in auto mode."""
    return mode() == "auto"


# ---------- the status file: {"fetched": {source: iso}, "refreshed": {source: epoch}} ----------

def _read():
    f = _status_file()
    with _lock:
        try:
            mt = f.stat().st_mtime_ns
        except OSError:
            return {"fetched": {}, "refreshed": {}}
        if _mem["path"] == f and _mem["mtime"] == mt and _mem["data"] is not None:
            return _mem["data"]
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            d = {}
        d.setdefault("fetched", {})
        d.setdefault("refreshed", {})
        _mem.update(path=f, mtime=mt, data=d)
        return d


def _update(fn):
    f = _status_file()
    with _lock, open(_dir() / ".data_status.lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)  # the MCP server writes here too
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            d = {}
        d.setdefault("fetched", {})
        d.setdefault("refreshed", {})
        fn(d)
        tmp = f.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(d, indent=1))
        tmp.replace(f)
        _mem.update(path=None, mtime=None, data=None)


def _parents(source):
    """'google:8931136258' -> ['google:8931136258', 'google', 'all']; 'seo.gsc' -> ['seo.gsc', 'seo', 'all']."""
    out, s = [source], source
    for sep in (":", "."):
        if sep in s:
            s = s.split(sep, 1)[0]
            out.append(s)
    return out + ["all"]


def mark(source, at=None):
    """Record that `source` was just fetched (at: datetime, default now)."""
    stamp = (at or dt.datetime.now()).isoformat(timespec="seconds")
    _update(lambda d: d["fetched"].__setitem__(source, stamp))
    return stamp


def fetched(source):
    """When `source` was last fetched (ISO, local time) or None."""
    return _read()["fetched"].get(source)


def invalidate(source, at=None):
    """A Refresh: every copy of `source` (and of its children) made before `at` (epoch, default now) is no longer fresh.
    Pass the time the refresh started, so copies it just wrote stay fresh and older ones (other date ranges) do not."""
    now = time.time() if at is None else at
    _update(lambda d: d["refreshed"].__setitem__(source, max(now, d["refreshed"].get(source, 0))))
    return now


def refreshed_at(source):
    r = _read()["refreshed"]
    return max((r.get(s, 0) for s in _parents(source)), default=0)


def fresh(source, age, ttl=None, filled_at=None):
    """May the cached copy be used? age: seconds since it was fetched (None = there is no copy). ttl: the cache's own
    time limit, used only in auto mode (0 always means "fetch now"). filled_at: epoch when the copy was made, so a
    Refresh made after it (here or in another process) wins."""
    if age is None:
        return False
    if ttl == 0:
        return False
    if filled_at is not None and refreshed_at(source) > filled_at:
        return False
    if manual():
        return True
    return ttl is None or age < ttl


def max_age(source, ttl):
    """For functions that take a max_age in seconds: unlimited in manual mode, else ttl."""
    return float("inf") if manual() else ttl


# ---------- debug: count outgoing calls ----------

_calls = collections.Counter()
_log = []


def install_call_log():
    """Wrap urllib.request.urlopen so each outgoing call is counted by host + path. On only with STUDIO_CALL_LOG=1."""
    if not os.environ.get("STUDIO_CALL_LOG") or getattr(urllib.request.urlopen, "_studio_counted", False):
        return False
    real = urllib.request.urlopen

    def counted(req, *a, **kw):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        base = url.split("?", 1)[0].split("#", 1)[0]
        host = base.split("/")[2] if "://" in base else base
        with _lock:
            _calls[host] += 1
            _log.append({"t": dt.datetime.now().isoformat(timespec="seconds"), "host": host, "path": base.split(host, 1)[-1][:160]})
            del _log[:-500]
        return real(req, *a, **kw)

    counted._studio_counted = True
    urllib.request.urlopen = counted
    return True


def calls(reset=False):
    with _lock:
        out = {"total": sum(_calls.values()), "by_host": dict(_calls), "log": list(_log[-200:])}
        if reset:
            _calls.clear()
            _log.clear()
    return out
