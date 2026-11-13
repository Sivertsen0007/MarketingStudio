"""Live progress of a push or undo, per account, so the UI can show a list of what is going live (like Editor's Post window).

The push / undo request itself blocks until it is done; the UI polls GET /api/live/progress meanwhile.
Each line: {id, summary, total, checked, sent, state}. state: waiting, checking, sending, done, failed, reversed.
"""
import threading
import time

_lock = threading.Lock()
_runs = {}  # acct -> run dict


def start(acct, kind, lines, phase="checking", message=""):
    """lines: [(id, summary, steps)]. kind: push or undo."""
    with _lock:
        _runs[acct] = {"acct": acct, "kind": kind, "running": True, "phase": phase, "message": message, "started": time.time(),
                       "finished": None, "ok": None,
                       "items": [{"id": i, "summary": s, "total": max(int(n or 1), 1), "checked": 0, "sent": 0, "state": "waiting"}
                                 for i, s, n in lines]}


def _item(acct, item_id):
    r = _runs.get(acct)
    return next((x for x in r["items"] if x["id"] == item_id), None) if r else None


def phase(acct, name, message=""):
    with _lock:
        r = _runs.get(acct)
        if r:
            r["phase"], r["message"] = name, message
            for x in r["items"]:
                if x["state"] not in ("done", "failed", "reversed"):
                    x["state"] = "checking" if name == "checking" else "sending" if name == "sending" else x["state"]


def checked(acct, item_id=None, n=1):
    """One step of an item (or every item, when item_id is None) passed the check."""
    with _lock:
        r = _runs.get(acct)
        for x in (r["items"] if r else []):
            if item_id is None or x["id"] == item_id:
                x["checked"] = x["total"] if item_id is None else min(x["checked"] + n, x["total"])


def sent(acct, item_id=None, n=1):
    """One step of an item (or every item) is live."""
    with _lock:
        r = _runs.get(acct)
        for x in (r["items"] if r else []):
            if item_id is None or x["id"] == item_id:
                x["checked"] = x["total"]
                x["sent"] = x["total"] if item_id is None else min(x["sent"] + n, x["total"])
                x["state"] = "done" if x["sent"] >= x["total"] else "sending"


def mark(acct, item_id, state):
    with _lock:
        x = _item(acct, item_id)
        if x:
            x["state"] = state


def finish(acct, ok, message=""):
    with _lock:
        r = _runs.get(acct)
        if not r or not r["running"]:
            return
        r["running"], r["ok"], r["finished"], r["phase"] = False, bool(ok), time.time(), "done" if ok else "failed"
        r["message"] = message or r["message"]
        for x in r["items"]:
            if x["state"] not in ("done", "failed", "reversed"):
                x["state"] = "done" if ok else "failed"


def get(acct):
    with _lock:
        r = _runs.get(acct)
        if not r:
            return {"running": False, "items": []}
        return {**r, "items": [dict(x) for x in r["items"]], "elapsed": round((r["finished"] or time.time()) - r["started"], 1)}


def totals(acct, counts):
    """{item id: number of steps} once the plan is known."""
    with _lock:
        r = _runs.get(acct)
        for x in (r["items"] if r else []):
            if x["id"] in counts:
                x["total"] = max(int(counts[x["id"]] or 1), 1)
