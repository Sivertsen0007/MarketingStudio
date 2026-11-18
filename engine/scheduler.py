"""Studio's background scheduler: named periodic jobs on one thread, started once from server.py main().

Never started on import (the MCP server imports Studio's modules too): only start() starts the thread.

    import scheduler
    scheduler.register("alerts", check_alerts, every=900, enabled_default=False)      # every 15 minutes
    scheduler.register("digest", send_digest, at="07:30", tz="Europe/London")         # daily at 07:30 in that zone
    scheduler.start()          # server.py main(), once
    scheduler.status()         # for the Settings row (GET /api/scheduler)
    scheduler.set_enabled("alerts", True)

Rules
- One thread wakes every second, looks at the jobs that are due and starts each in its own worker thread. A job never
  overlaps itself: while a run is still going, the job is not started again, even after its timeout.
- timeout (seconds): a run that takes longer is marked "timeout" in status() and the scheduler moves on. Python can't
  stop a thread, so the run goes on in the background; the job waits for it before running again, and its result is
  still recorded when it ends.
- An exception in a job is caught, logged (stderr) and shown in status(); the job runs again at its next time.
- every: seconds (or a timedelta) between starts. The first run is one interval after start (as Studio's old SoMe
  thread did). at: "HH:MM", daily, in tz (an IANA name, a tzinfo, or a function returning either). tz=None means the
  account's time zone: the Meta ad account's time zone from Studio's cache (no API call), else the Mac's.
- Missed runs (the Mac was asleep or Studio was closed) run once, not once per missed slot. resume=False: after a
  restart the job starts over (first run one interval after start) instead of using the saved next run.
- A run that was still going when Studio stopped (closed, crashed, Mac restarted) shows as "interrupted".
- refresh=True marks a job that pulls data from Google, Meta, an email platform etc. It runs only when the data policy
  (datapolicy.py) is "auto"; in "manual" it is held (shown as such) and nothing is pulled. Jobs that only work on what
  is already on the Mac (alerts on cached data, the SoMe publisher the user turned on) leave refresh False.
- Each job can be switched off in Settings (set_enabled); enabled_default is used until the user has chosen.
  can_disable=False for a job whose own area setting is the switch (the SoMe publisher).
- Last run, result, error and next run are kept in <data dir>/scheduler.json, so status() survives a restart and a
  daily job knows it already ran today.
"""
import datetime as dt
import json
import os
import sys
import threading
import time
import traceback
from zoneinfo import ZoneInfo

import datapolicy

TICK = 1.0  # seconds between looks at the job list
TRACE_EVERY = 3600  # a job failing the same way every round prints its full traceback at most once an hour
DEFAULT_TIMEOUT = 300
STATE_FILE = "scheduler.json"

_lock = threading.RLock()
_jobs = {}  # name -> dict (the registration + run-time fields)
_loop = {"thread": None, "stop": None, "started_at": None, "error": ""}
_disk = {"loaded": False, "data": None}


# ---------- persistence (<data dir>/scheduler.json) ----------

def _file():
    return datapolicy._dir() / STATE_FILE


def _read():
    with _lock:
        if not _disk["loaded"]:
            try:
                d = json.loads(_file().read_text())
            except (OSError, ValueError):
                d = {}
            if not isinstance(d, dict):
                d = {}
            d.setdefault("enabled", {})
            d.setdefault("jobs", {})
            _disk.update(loaded=True, data=d)
        return _disk["data"]


def _write():
    with _lock:
        d = _read()
        f = _file()
        tmp = f.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            tmp.write_text(json.dumps(d, indent=1))
            tmp.replace(f)
        except OSError:
            traceback.print_exc()


def _save_job(j):
    """Copy a job's run fields to disk."""
    with _lock:
        _read()["jobs"][j["name"]] = {k: j.get(k) for k in ("spec", "last_run", "last_end", "last_status", "last_error",
                                                             "duration", "next_run", "runs", "failures")}
        _write()


# ---------- time ----------

def _now():
    return time.time()


def _iso(ts, tz=None):
    if ts is None:
        return None
    return dt.datetime.fromtimestamp(ts, tz or dt.timezone.utc).astimezone(tz).isoformat(timespec="seconds")


def account_tz():
    """The Meta ad account's time zone from Studio's cache (meta_cache_*.json, account.timezone_name), else the Mac's.
    Never calls an API."""
    try:
        for f in sorted(datapolicy._dir().glob("meta_cache_*.json")):
            try:
                name = (json.loads(f.read_text()).get("account") or {}).get("timezone_name")
            except (OSError, ValueError, AttributeError):
                continue
            if name:
                return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - unknown zone name, unreadable folder: the Mac's zone
        pass
    return local_tz()


def local_tz():
    """The Mac's own time zone as a real zone (DST included): TZ, else /etc/localtime's target, else a zone that asks
    time.localtime for each moment."""
    for name in (os.environ.get("TZ", "").lstrip(":"), _localtime_name()):
        if name:
            try:
                return ZoneInfo(name)
            except Exception:  # noqa: BLE001 - not an IANA name
                pass
    return _SystemTZ()


def _localtime_name():
    try:
        target = os.path.realpath("/etc/localtime")
    except OSError:
        return ""
    return target.split("zoneinfo/", 1)[1] if "zoneinfo/" in target else ""


class _SystemTZ(dt.tzinfo):
    """The C library's local time, asked per moment (so DST is right on both sides of a change)."""

    def _lt(self, d):
        if d is None:
            return time.localtime()
        try:
            return time.localtime(time.mktime((d.year, d.month, d.day, d.hour, d.minute, d.second, 0, 0, -1)))
        except (OverflowError, ValueError):
            return time.localtime()

    def utcoffset(self, d):
        return dt.timedelta(seconds=self._lt(d).tm_gmtoff)

    def dst(self, d):
        return dt.timedelta(hours=1) if self._lt(d).tm_isdst > 0 else dt.timedelta(0)

    def tzname(self, d):
        return self._lt(d).tm_zone

    def fromutc(self, d):
        lt = time.localtime(d.replace(tzinfo=dt.timezone.utc).timestamp())
        return (d + dt.timedelta(seconds=lt.tm_gmtoff)).replace(tzinfo=self)

    def __repr__(self):
        return "local"


def _tz(j):
    tz = j.get("tz")
    if callable(tz) and not isinstance(tz, dt.tzinfo):
        tz = tz()
    if tz is None:
        return account_tz()
    return tz if isinstance(tz, dt.tzinfo) else ZoneInfo(str(tz))


def _parse_at(at):
    try:
        h, m = (int(x) for x in str(at).strip().split(":"))
        if 0 <= h < 24 and 0 <= m < 60:
            return h, m
    except ValueError:
        pass
    raise ValueError(f'at must be "HH:MM" (24 hours), not {at!r}')


def _next_daily(j, after):
    """The first HH:MM in the job's time zone strictly after `after` (epoch). DST-safe (wall-clock time)."""
    tz = _tz(j)
    h, m = j["at"]
    day = dt.datetime.fromtimestamp(after, tz).date()
    for i in range(3):
        d = day + dt.timedelta(days=i)
        t = dt.datetime(d.year, d.month, d.day, h, m, tzinfo=tz).timestamp()
        if t > after:
            return t
    return after + 86400  # not reached


def _next_after(j, ts):
    return ts + j["every"] if j.get("every") else _next_daily(j, ts)


# ---------- registration ----------

def register(name, fn, every=None, at=None, enabled_default=False, timeout=DEFAULT_TIMEOUT, tz=None, refresh=False,
             label=None, can_disable=True, note="", resume=True):
    """Add (or replace) a named job. Exactly one of every (seconds or timedelta) or at ("HH:MM", daily).
    Safe before or after start(). Returns the job's status row."""
    if not name or not callable(fn):
        raise ValueError("a job needs a name and a function")
    if (every is None) == (at is None):
        raise ValueError("give exactly one of every= or at=")
    if isinstance(every, dt.timedelta):
        every = every.total_seconds()
    if every is not None and float(every) <= 0:
        raise ValueError("every must be more than 0 seconds")
    spec = f"every {float(every):g}s" if every is not None else f"daily {at}"
    j = {"name": name, "fn": fn, "every": float(every) if every is not None else None, "at": _parse_at(at) if at else None,
         "at_text": str(at).strip() if at else None, "enabled_default": bool(enabled_default),
         "timeout": float(timeout or DEFAULT_TIMEOUT), "tz": tz, "refresh": bool(refresh), "label": label or name,
         "can_disable": bool(can_disable), "note": note, "spec": spec, "worker": None, "started": None, "timed_out": False,
         "last_run": None, "last_end": None, "last_status": None, "last_error": "", "duration": None, "next_run": None,
         "runs": 0, "failures": 0}
    with _lock:
        old = _jobs.get(name)
        if old and old.get("worker") and old["worker"].is_alive():  # keep the running one visible: no overlap
            j.update(worker=old["worker"], started=old["started"], timed_out=old["timed_out"])
        saved = _read()["jobs"].get(name) or {}
        for k in ("last_run", "last_end", "last_status", "last_error", "duration", "runs", "failures"):
            if saved.get(k) is not None:
                j[k] = saved[k]
        if j["last_status"] == "running" and not j["worker"]:  # Studio stopped during that run
            j["last_status"] = "interrupted"
            j["last_error"] = "Studio stopped while this was running (closed, crashed or the Mac restarted)"
            j["failures"] = int(j.get("failures") or 0) + 1
        now = _now()
        if resume and saved.get("spec") == spec and saved.get("next_run"):
            j["next_run"] = float(saved["next_run"])  # in the past: it runs once at the next look (a missed run)
        else:
            j["next_run"] = _next_after(j, now)
        _jobs[name] = j
        _save_job(j)
    return _row(j)


def unregister(name):
    with _lock:
        return _jobs.pop(name, None) is not None


def enabled(name):
    with _lock:
        j = _jobs.get(name)
        if not j:
            return False
        if not j["can_disable"]:
            return True
        v = _read()["enabled"].get(name)
        return j["enabled_default"] if v is None else bool(v)


def set_enabled(name, on):
    """Settings switch for one job."""
    with _lock:
        j = _jobs.get(name)
        if not j:
            raise ValueError(f"no background job called {name!r}")
        if not j["can_disable"]:
            raise ValueError(f"{j['label']} is switched on and off in its own area's settings")
        _read()["enabled"][name] = bool(on)
        _write()
        if on and j["next_run"] is not None and j["next_run"] < _now():
            j["next_run"] = _next_after(j, _now())  # switched back on: no instant catch-up run
            _save_job(j)
        return _row(j)


# ---------- running ----------

def _held(j):
    return j["refresh"] and not datapolicy.background_ok()


def _worker(j, started):
    status, err = "ok", ""
    try:
        j["fn"]()
    except Exception as ex:  # noqa: BLE001 - one job's failure never stops the others
        status, err = "error", f"{ex.__class__.__name__}: {str(ex)[:300]}"
        _report(j, err)
    end = _now()
    with _lock:
        if j.get("timed_out"):
            err = (err + "; " if err else "") + f"took {end - started:.0f}s (timeout {j['timeout']:g}s)"
            if status == "ok":
                status = "timeout"
        j.update(last_end=end, last_status=status, last_error=err, duration=round(end - started, 3), worker=None,
                 started=None, timed_out=False)
        if status != "ok":
            j["failures"] = int(j.get("failures") or 0) + 1
        _save_job(j)


def _report(j, err):
    """Log a job's failure: the full traceback when it is new (or once an hour), else one short line."""
    t = time.time()
    if err != j.get("_tb_err") or t - (j.get("_tb_at") or 0) >= TRACE_EVERY:
        j.update(_tb_err=err, _tb_at=t)
        print(f"[scheduler] job {j['name']} failed:", file=sys.stderr)
        traceback.print_exc()
    else:
        print(f"[scheduler] job {j['name']} failed again: {err[:120]}", file=sys.stderr)


def _start_run(j, now):
    j.update(last_run=now, runs=int(j.get("runs") or 0) + 1, started=now, timed_out=False, last_status="running",
             next_run=_next_after(j, now))
    t = threading.Thread(target=_worker, args=(j, now), name=f"job-{j['name']}", daemon=True)
    j["worker"] = t
    _save_job(j)
    t.start()


def run_due(now=None):
    """One look at the job list (the thread calls this every TICK). Tests call it with a fake now. -> names started."""
    now = _now() if now is None else now
    started = []
    with _lock:
        for j in list(_jobs.values()):
            w = j.get("worker")
            if w is not None and w.is_alive():
                if not j["timed_out"] and now - j["started"] > j["timeout"]:
                    j["timed_out"] = True
                    j["last_status"] = "timeout"
                    j["last_error"] = f"still running after {j['timeout']:g}s; the next run waits for it"
                    j["failures"] = int(j.get("failures") or 0) + 1
                    print(f"[scheduler] job {j['name']} timed out after {j['timeout']:g}s", file=sys.stderr)
                    _save_job(j)
                continue  # never overlap
            if not enabled(j["name"]) or j["next_run"] is None or now < j["next_run"]:
                continue
            if _held(j):
                j.update(next_run=_next_after(j, now), last_status="held",
                         last_error="not run: the data policy is manual (data is only fetched when you press Refresh)")
                _save_job(j)
                continue
            _start_run(j, now)
            started.append(j["name"])
    return started


def run_now(name):
    """Start one job now (a Settings button, tests). Refuses while it is running. -> True when started."""
    with _lock:
        j = _jobs.get(name)
        if not j:
            raise ValueError(f"no background job called {name!r}")
        if j.get("worker") is not None and j["worker"].is_alive():
            return False
        _start_run(j, _now())
        return True


def _loop_body(stop, tick):
    while not stop.wait(tick):
        try:
            run_due()
            _loop["error"] = ""
        except Exception as ex:  # noqa: BLE001 - the loop itself never dies
            _loop["error"] = f"{ex.__class__.__name__}: {str(ex)[:300]}"
            traceback.print_exc()


def start(tick=TICK):
    """Start the scheduler thread (daemon). Called from server.py main(). Safe to call twice. -> True if it started."""
    with _lock:
        if running():
            return False
        stop = threading.Event()
        t = threading.Thread(target=_loop_body, args=(stop, tick), name="studio-scheduler", daemon=True)
        _loop.update(thread=t, stop=stop, started_at=_now(), error="")
        t.start()
        return True


def stop(wait=2.0):
    """Stop the thread (tests, shutdown). Running jobs finish on their own."""
    with _lock:
        t, ev = _loop.get("thread"), _loop.get("stop")
        _loop.update(thread=None, stop=None, started_at=None)
    if ev:
        ev.set()
    if t and wait:
        t.join(wait)


def running():
    t = _loop.get("thread")
    return bool(t and t.is_alive())


# ---------- status (Settings) ----------

def _schedule_text(j):
    if j["every"]:
        s = j["every"]
        if s % 86400 == 0:
            return f"every {s / 86400:g} day" + ("" if s == 86400 else "s")
        if s % 3600 == 0:
            return f"every {s / 3600:g} hour" + ("" if s == 3600 else "s")
        if s % 60 == 0:
            return f"every {s / 60:g} minute" + ("" if s == 60 else "s")
        return f"every {s:g} seconds"
    try:
        z = _tz(j)
        zn = getattr(z, "key", None) or str(z)
    except Exception:  # noqa: BLE001
        zn = "?"
    return f"daily at {j['at_text']} ({zn})"


def _row(j):
    try:
        tz = _tz(j) if j["at"] else None
    except Exception:  # noqa: BLE001
        tz = None
    on = enabled(j["name"])
    busy = bool(j.get("worker") is not None and j["worker"].is_alive())
    return {"name": j["name"], "label": j["label"], "schedule": _schedule_text(j), "enabled": on, "can_disable": j["can_disable"],
            "refresh": j["refresh"], "held": bool(on and _held(j)), "running": busy, "timeout": j["timeout"], "note": j["note"],
            "last_run": _iso(j.get("last_run"), tz), "last_status": j.get("last_status"), "last_error": j.get("last_error") or "",
            "duration": j.get("duration"), "next_run": _iso(j.get("next_run"), tz) if on else None,
            "runs": j.get("runs") or 0, "failures": j.get("failures") or 0}


def status():
    """{running, started_at, error, jobs: [...]} for GET /api/scheduler and the Settings row."""
    with _lock:
        return {"running": running(), "started_at": _iso(_loop.get("started_at")), "error": _loop.get("error") or "",
                "policy": datapolicy.mode(), "jobs": [_row(j) for j in sorted(_jobs.values(), key=lambda x: x["label"].lower())]}


def _reset_for_tests():
    """Forget jobs and the cached state file (tests)."""
    stop(wait=1.0)
    with _lock:
        _jobs.clear()
        _disk.update(loaded=False, data=None)
        _loop["error"] = ""
