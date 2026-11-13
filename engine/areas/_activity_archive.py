"""Activity area: the local archive (sqlite in <Studio data>/activity/archive.sqlite).

Google keeps change_event for about 30 days, so every read of Google (and Meta, and blog edits) is saved here,
de-duplicated by the source's own id. Longer periods then work going forward.

Tables:
  events(id PRIMARY KEY, source, account, ts, row JSON)      one plain activity row per change
  fetches(source, account, start, end, at, n)                 every window that was read, to show coverage

Personal data: rows hold who made a change (Google user_email, Meta actor name). This file stays on the Mac, inside
Studio's gitignored data folder. See the README.
"""
import json
import os
import sqlite3
import threading
from pathlib import Path

from . import _activity_periods as per

_lock = threading.RLock()
DIR = None  # tests set this; else ACTIVITY_DIR env, else core.STUDIO_DIR/activity


def folder():
    d = DIR or os.environ.get("ACTIVITY_DIR")
    if d:
        p = Path(d)
    else:
        import core  # noqa: PLC0415
        p = Path(core.STUDIO_DIR) / "activity"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _db():
    c = sqlite3.connect(str(folder() / "archive.sqlite"), timeout=20)
    c.execute("CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, source TEXT, account TEXT, ts TEXT, row TEXT)")
    c.execute("CREATE INDEX IF NOT EXISTS ev_ts ON events(source, ts)")
    c.execute("CREATE TABLE IF NOT EXISTS fetches(source TEXT, account TEXT, start TEXT, end TEXT, at TEXT, n INTEGER)")
    return c


def save(source, account, rows, start=None, end=None, at=None):
    """Insert rows (dicts with "id" and "ts"); existing ids are kept as they are. Records the window when given.
    -> number of new rows."""
    with _lock:
        c = _db()
        try:
            before = c.total_changes
            c.executemany("INSERT OR IGNORE INTO events(id, source, account, ts, row) VALUES (?,?,?,?,?)",
                          [(r["id"], source, account, r["ts"], json.dumps(r, ensure_ascii=False)) for r in rows])
            new = c.total_changes - before
            if start and end:
                c.execute("INSERT INTO fetches VALUES (?,?,?,?,?,?)", (source, account, per.iso(start), per.iso(end),
                                                                      per.iso(at or per.parse(end)), len(rows)))
            c.commit()
            return new
        finally:
            c.close()


def rows(source, start, end, account=None):
    """Archived rows of one source with start <= ts < end."""
    with _lock:
        c = _db()
        try:
            q = "SELECT row FROM events WHERE source=? AND ts>=? AND ts<?"
            args = [source, per.iso(start), per.iso(end)]
            if account:
                q += " AND account=?"
                args.append(account)
            return [json.loads(r[0]) for r in c.execute(q + " ORDER BY ts DESC", args)]
        finally:
            c.close()


def last_fetch(source, account):
    """End of the newest window read for this source and account (aware datetime), or None."""
    with _lock:
        c = _db()
        try:
            r = c.execute("SELECT MAX(end) FROM fetches WHERE source=? AND account=?", (source, account)).fetchone()
            return per.parse(r[0]) if r and r[0] else None
        finally:
            c.close()


def coverage(source, account=None):
    """Merged windows read so far -> {"from", "to", "gaps": [[from, to]], "events": n} (ISO UTC) or None."""
    with _lock:
        c = _db()
        try:
            q, args = "SELECT start, end FROM fetches WHERE source=?", [source]
            q2, args2 = "SELECT COUNT(*) FROM events WHERE source=?", [source]
            if account:
                q += " AND account=?"
                q2 += " AND account=?"
                args.append(account)
                args2.append(account)
            wins = sorted(c.execute(q, args).fetchall())
            n = c.execute(q2, args2).fetchone()[0]
        finally:
            c.close()
    if not wins:
        return None
    merged = [list(wins[0])]
    for s, e in wins[1:]:
        if s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return {"from": merged[0][0], "to": merged[-1][1], "gaps": [[merged[i][1], merged[i + 1][0]] for i in range(len(merged) - 1)], "events": n}


def prune(older_than):
    """Delete rows older than a datetime (retention). -> rows deleted."""
    with _lock:
        c = _db()
        try:
            n = c.execute("DELETE FROM events WHERE ts<?", (per.iso(older_than),)).rowcount
            c.execute("DELETE FROM fetches WHERE end<?", (per.iso(older_than),))
            c.commit()
            return n
        finally:
            c.close()


def writable():
    """-> (ok, detail) for the health check."""
    try:
        p = folder() / ".write-test"
        p.write_text("ok")
        p.unlink()
        c = _db()
        n = c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        c.close()
        return True, f"{folder()} is writable ({n:,} archived activities)."
    except Exception as ex:  # noqa: BLE001
        return False, f"Archive not writable: {ex}"
