"""Background jobs for live preview and live push, so the page never waits on a long request.

server.py starts a job when a live preview / push route gets "background": true in its JSON body and answers
{"job": id} at once; the page then polls GET /api/jobs/<id>. Jobs live in memory only (a restart forgets them); the
last ~50 are kept. Only one push job per account runs at a time (start() raises Busy with the running job's id).

Job record:
  {id, kind: preview|push, source: google|meta, acct, state: running|done|error, started, finished,
   progress: {done, total, label}, items: [{key, label, status, detail, link}], result, error}

Progress comes from the engine's progress keyword when it has one (fn(done, total, label, item_key, status)), else
from progress.py (the per-account live list the synchronous push already fills), read when the job is fetched.
"""
import copy
import inspect
import secrets
import threading
import time
import traceback

import progress

KEEP = 50
_lock = threading.Lock()
_jobs = {}  # id -> record (insertion order = start order)
_fallback = {}  # id -> progress.py key, for jobs whose engine has no progress keyword


class Busy(Exception):
    """A push job is already running for this account."""

    def __init__(self, job_id):
        super().__init__(f"a push is already running for this account (job {job_id})")
        self.job_id = job_id


def accepts(fn, name):
    """True if fn takes the keyword `name` (or **kwargs)."""
    try:
        ps = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(p.name == name or p.kind == p.VAR_KEYWORD for p in ps)


def _prune():
    done = [i for i, j in _jobs.items() if j["state"] != "running"]
    while len(_jobs) > KEEP and done:
        i = done.pop(0)
        _jobs.pop(i, None)
        _fallback.pop(i, None)


def running_push(acct):
    with _lock:
        return next((j["id"] for j in _jobs.values() if j["kind"] == "push" and j["acct"] == acct and j["state"] == "running"), None)


_STATUS = {"waiting": "waiting", "checking": "running", "sending": "running", "running": "running", "done": "done",
           "ok": "done", "failed": "error", "error": "error", "reversed": "reversed", "skipped": "skipped"}


def _status(s):
    return _STATUS.get(str(s or "").lower(), str(s or "waiting"))


class Reporter:
    """Handed to the job function: report(done, total, label, item_key=None, status=None)."""

    def __init__(self, job_id):
        self.job_id = job_id

    def __call__(self, done=None, total=None, label=None, item_key=None, status=None):
        with _lock:
            j = _jobs.get(self.job_id)
            if not j:
                return
            _fallback.pop(self.job_id, None)  # the engine reports itself
            p = j["progress"]
            if done is not None:
                p["done"] = int(done)
            if total is not None:
                p["total"] = int(total)
            if label is not None:
                p["label"] = str(label)
            if item_key is not None:
                key = str(item_key)
                it = next((x for x in j["items"] if x["key"] == key), None)
                if it is None:
                    it = {"key": key, "label": str(label or key), "status": "waiting", "detail": None, "link": None}
                    j["items"].append(it)
                if status is not None:
                    it["status"] = _status(status)

    def use_progress_py(self, key):
        """No progress keyword in the engine: read progress.py's list for `key` when the job is fetched."""
        with _lock:
            _fallback[self.job_id] = key


def _items_from_result(res):
    out = []
    for x in (res.get("items") if isinstance(res, dict) else None) or []:
        if not isinstance(x, dict) or "key" not in x and "id" not in x:
            continue
        err = x.get("error") or ("; ".join(map(str, x["problems"])) if x.get("problems") else None)
        out.append({"key": str(x.get("key", x.get("id"))), "label": str(x.get("label") or x.get("summary") or x.get("key") or ""),
                    "status": _status(x.get("status") or ("error" if err else "done")), "detail": err or x.get("detail"),
                    "link": x.get("link"), **({"ids": x["ids"]} if x.get("ids") is not None else {})})
    return out


def start(kind, source, acct, fn, after=None):
    """Run fn(report) in a thread. kind: preview or push. after(job_copy) runs once the job is marked done or error
    (never raises into the job). Raises Busy for a second push on the same account."""
    if kind not in ("preview", "push") or source not in ("google", "meta"):
        raise ValueError("kind must be preview or push, source google or meta")
    jid = secrets.token_urlsafe(9)
    with _lock:
        if kind == "push":
            busy = next((j["id"] for j in _jobs.values() if j["kind"] == "push" and j["acct"] == acct and j["state"] == "running"), None)
            if busy:
                raise Busy(busy)
        _jobs[jid] = {"id": jid, "kind": kind, "source": source, "acct": acct, "state": "running", "started": time.time(),
                      "finished": None, "progress": {"done": 0, "total": 0, "label": "Starting"}, "items": [],
                      "result": None, "error": None}
        _prune()
    rep = Reporter(jid)

    def run():
        res, err = None, None
        try:
            res = fn(rep)
        except Exception as ex:  # noqa: BLE001 - the job records every error
            traceback.print_exc()
            err = str(ex) or ex.__class__.__name__
        with _lock:
            j = _jobs.get(jid)
            if j is not None:
                fb = _fallback.pop(jid, None)
                if fb is not None:
                    _merge_progress(j, fb)
                mapped = _items_from_result(res)
                if mapped:
                    j["items"] = mapped
                j["result"], j["error"] = res, err
                j["state"] = "error" if err else "done"
                j["finished"] = time.time()
                p = j["progress"]
                if not err:
                    p["total"] = p["total"] or len(j["items"])
                    p["done"] = p["total"]
                p["label"] = "Failed" if err else (res.get("message") if isinstance(res, dict) and res.get("message") else "Done")
                snap = copy.deepcopy(j)
            else:
                snap = None
        if after and snap is not None:
            try:
                after(snap)
            except Exception:  # noqa: BLE001
                traceback.print_exc()

    threading.Thread(target=run, name=f"job-{kind}-{jid}", daemon=True).start()
    return jid


def _merge_progress(j, key):
    """Copy progress.py's live list for `key` into the job (caller holds _lock)."""
    try:
        r = progress.get(key)
    except Exception:  # noqa: BLE001
        return
    items = r.get("items") or []
    if not items or (r.get("started") or 0) < j["started"] - 1:
        return  # nothing yet, or a run from before this job
    j["items"] = [{"key": str(x.get("id")), "label": x.get("summary") or "", "status": _status(x.get("state")),
                   "detail": None, "link": None} for x in items]
    j["progress"] = {"done": sum(1 for x in items if x.get("state") in ("done", "failed", "reversed")),
                     "total": len(items), "label": r.get("message") or j["progress"]["label"]}


def set_result(jid, **kv):
    """Merge keys into a finished job's result (e.g. refreshing=False once the background refresh ended)."""
    with _lock:
        j = _jobs.get(jid)
        if j is not None:
            if not isinstance(j["result"], dict):
                j["result"] = {} if j["result"] is None else {"value": j["result"]}
            j["result"].update(kv)


def get(jid):
    with _lock:
        j = _jobs.get(jid)
        if j is None:
            return None
        if j["state"] == "running" and jid in _fallback:
            _merge_progress(j, _fallback[jid])
        return copy.deepcopy(j)


def latest(acct=None, limit=20):
    with _lock:
        ids = [i for i, j in _jobs.items() if acct is None or j["acct"] == acct]
    out = [get(i) for i in reversed(ids)]
    return [j for j in out if j][: max(int(limit or 20), 1)]


def _reset():
    """Tests only."""
    with _lock:
        _jobs.clear()
        _fallback.clear()
