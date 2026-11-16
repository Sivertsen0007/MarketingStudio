"""Backups of the team database: an online copy while the server runs, and a restore.

`python3 -m teamserver backup --out FILE` copies <data>/team.db with SQLite's backup API in one step, so the copy is a
consistent snapshot even while the server writes (WAL mode: the server keeps serving during the copy). The copy is a
single self-contained file (rollback journal mode, no -wal / -shm next to it), checked with `PRAGMA integrity_check`,
written under a temporary name and renamed into place, mode 0600 (it holds the team's drafts and hashed device
tokens).

`python3 -m teamserver restore FILE` checks FILE the same way and copies it over <data>/team.db. Stop the server first:
the restore refuses while another process has the database open. The current database is kept next to it as
team.db.before-restore-<time> (itself a consistent backup).
"""
import os
import sqlite3
import time

DB = "team.db"
REQUIRED = ("meta", "members", "devices", "invites", "items", "events")


class BackupError(Exception):
    """The backup or restore can't be done. The message is safe to show and says what to do."""


def db_path(data_dir):
    return os.path.join(data_dir, DB)


def _connect(path):
    con = sqlite3.connect(path, timeout=30, isolation_level=None)
    con.execute("PRAGMA busy_timeout=30000")
    return con


def check(path):
    """Is `path` a sound team database? -> {team, members, items, events}; BackupError when not."""
    if not os.path.isfile(path):
        raise BackupError("%s does not exist" % path)
    with open(path, "rb") as f:
        if f.read(16) != b"SQLite format 3\x00":
            raise BackupError("%s is not an SQLite database" % path)
    con = _connect(path)
    try:
        ok = con.execute("PRAGMA integrity_check").fetchone()
        if not ok or ok[0] != "ok":
            raise BackupError("%s failed the integrity check (%s)" % (path, ok[0] if ok else "no answer"))
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        missing = [t for t in REQUIRED if t not in tables]
        if missing:
            raise BackupError("%s is not a team server database (no %s table)" % (path, ", ".join(missing)))
        row = con.execute("SELECT value FROM meta WHERE key = 'team_name'").fetchone()
        return {"team": (row[0] if row else "") or "Team",
                "members": con.execute("SELECT COUNT(*) FROM members").fetchone()[0],
                "items": con.execute("SELECT COUNT(*) FROM items").fetchone()[0],
                "events": con.execute("SELECT COALESCE(MAX(rowid), 0) FROM events").fetchone()[0]}
    except sqlite3.DatabaseError as e:
        raise BackupError("%s can't be read: %s" % (path, e)) from None
    finally:
        con.close()


def _remove(*paths):
    for p in paths:
        try:
            os.remove(p)
        except OSError:
            pass


def backup(data_dir, out, force=False):
    """Copy <data_dir>/team.db to `out` while the server may be running. -> check() of the copy."""
    src_path = db_path(data_dir)
    if not os.path.isfile(src_path):
        raise BackupError("There is no team database at %s" % src_path)
    out = os.path.abspath(out)
    if os.path.isdir(out):
        raise BackupError("%s is a folder: give a file name, for example %s" % (out, os.path.join(out, "team-backup.db")))
    if os.path.exists(out) and not force:
        raise BackupError("%s already exists: choose another name or pass --force to replace it" % out)
    if os.path.exists(out) and os.path.samefile(out, src_path):
        raise BackupError("The backup can't replace the live database")
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    tmp = "%s.part-%d" % (out, os.getpid())
    _remove(tmp, tmp + "-journal", tmp + "-wal", tmp + "-shm")
    os.close(os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))  # private before SQLite writes a byte
    try:
        src = _connect(src_path)
        dst = _connect(tmp)
        try:
            src.backup(dst)  # every page in one step: one read transaction, so one consistent snapshot
            dst.execute("PRAGMA journal_mode=DELETE")  # one self-contained file, no -wal / -shm
        finally:
            dst.close()
            src.close()
        _remove(tmp + "-wal", tmp + "-shm")  # left by the WAL header the copy briefly had; the file is in DELETE mode now
        info = check(tmp)
        os.chmod(tmp, 0o600)
        os.replace(tmp, out)
        return info
    except sqlite3.DatabaseError as e:
        raise BackupError("The backup failed: %s" % e) from None
    finally:
        _remove(tmp, tmp + "-journal", tmp + "-wal", tmp + "-shm")


def restore(data_dir, src, yes=False, now=None):
    """Replace <data_dir>/team.db with the backup `src`. The server must be stopped.
    -> {team, members, items, events, kept}: kept is where the replaced database was saved (None when there was none)."""
    info = check(src)
    os.makedirs(data_dir, exist_ok=True)
    dst_path = db_path(data_dir)
    if os.path.exists(dst_path) and os.path.samefile(src, dst_path):
        raise BackupError("That is the live database itself: restore from a backup file")
    kept = None
    if os.path.exists(dst_path):
        if not yes:
            raise BackupError("%s already has a team database. Stop the server, then run the restore again with --yes "
                              "(the current database is kept as %s.before-restore-<time>)" % (data_dir, DB))
        dst = _connect(dst_path)
        try:
            # Leaving WAL mode needs the only connection: a running server makes this fail or stay "wal".
            try:
                mode = dst.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
            except sqlite3.OperationalError:
                mode = "busy"
            if str(mode).lower() != "delete":
                raise BackupError("The team database is in use: stop the server first, then restore")
        finally:
            dst.close()
        kept = "%s.before-restore-%s" % (dst_path, time.strftime("%Y%m%d-%H%M%S", time.localtime(now)))
        backup(data_dir, kept)
    _remove(dst_path + "-wal", dst_path + "-shm")  # after a clean switch to DELETE mode these are gone or empty
    s = _connect(src)
    d = _connect(dst_path)
    try:
        d.execute("PRAGMA journal_mode=DELETE")
        s.backup(d)
        d.execute("PRAGMA journal_mode=WAL")  # what the server uses
    except sqlite3.DatabaseError as e:
        raise BackupError("The restore failed: %s%s" % (e, (". The previous database is in %s" % kept) if kept else ""))
    finally:
        d.close()
        s.close()
    os.chmod(dst_path, 0o600)
    out = check(dst_path)
    out["kept"] = kept
    return out
