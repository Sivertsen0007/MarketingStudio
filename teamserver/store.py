"""SQLite store and team logic for the team server (protocol v1, see PROTOCOL.md).

One Store per server process. All writes go through one connection guarded by a lock, so the HTTP threads
never race. The clock is injectable so tests can move time (edit sessions, presence, invite expiry).
Every change that clients should hear about is passed to `on_emit(kind, payload, to)`, which the HTTP layer
wires to the SSE broker (`to` = a set of member ids, or None for everyone).
"""
import hashlib
import json
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone

ROLES = ("owner", "admin", "editor", "viewer")
MANAGERS = ("owner", "admin")
# Ten colours that stay apart from each other on light and dark backgrounds.
PALETTE = ("#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4",
           "#42d4f4", "#f032e6", "#9a6324", "#469990", "#808000")

INVITE_TTL = 7 * 86400
SESSION_IDLE = 600
PRESENCE_TTL = 120
CLIENT_EVENT_KINDS = ("push", "publish", "undo", "custom")

MAX_OPS = 500
MAX_ITEM_BYTES = 512 * 1024
MAX_KEY = 200          # coll and item id
MAX_NAME = 100
MAX_EMAIL = 254
MAX_SUMMARY = 500
MAX_BODY = 10000       # comment body
MAX_NOTE = 500
MAX_AREA = 50
MAX_DATA_BYTES = 64 * 1024   # event data
SESSION_ITEMS_SHOWN = 200

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+$")
MENTION_RE = re.compile(r"(?<![\w.+-])@([A-Za-z0-9][A-Za-z0-9._+-]*)")


class ApiError(Exception):
    """An error that is safe to show the client: HTTP status plus a short message."""

    def __init__(self, status, message, extra=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra or {}


def iso(t):
    """Epoch seconds -> ISO 8601 UTC string (or None)."""
    if t is None:
        return None
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hash_token(token):
    """sha256 hex of a token; only hashes are ever stored."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def area_of(coll):
    """The app area a collection belongs to: 'ads:123' -> 'ads', 'email' -> 'email'."""
    return (coll or "").split(":", 1)[0]


# ---------- input checks ----------

def need_str(d, key, maxlen, required=True, default=None):
    """Return d[key] as a stripped string, enforcing presence and length."""
    v = d.get(key)
    if v is None or v == "":
        if required:
            raise ApiError(400, "%s is required" % key)
        return default
    if not isinstance(v, str):
        raise ApiError(400, "%s must be a string" % key)
    v = v.strip()
    if required and not v:
        raise ApiError(400, "%s is required" % key)
    if len(v) > maxlen:
        raise ApiError(400, "%s is too long (max %d)" % (key, maxlen))
    return v


MAX_INT = 2 ** 63 - 1


def need_int(v, key, minimum=0):
    """A real JSON integer (no bools, floats or strings) between `minimum` and 2**63-1."""
    if not isinstance(v, int) or isinstance(v, bool):
        raise ApiError(400, "%s must be an integer" % key)
    if v < minimum or v > MAX_INT:
        raise ApiError(400, "%s must be an integer between %d and %d" % (key, minimum, MAX_INT))
    return v


def need_bool(v, key):
    if not isinstance(v, bool):
        raise ApiError(400, "%s must be true or false" % key)
    return v


def need_email(d, key="email"):
    e = need_str(d, key, MAX_EMAIL).lower()
    if not EMAIL_RE.match(e):
        raise ApiError(400, "%s is not a valid email address" % key)
    return e


def need_role(d, key="role"):
    r = need_str(d, key, 20).lower()
    if r not in ROLES:
        raise ApiError(400, "role must be one of %s" % ", ".join(ROLES))
    return r


def json_size(obj):
    return len(json.dumps(obj, separators=(",", ":")).encode("utf-8"))


class Store:
    """The team's database plus all protocol rules (roles, versions, sessions, mentions)."""

    def __init__(self, path, clock=None):
        self.clock = clock or time.time
        self.lock = threading.RLock()
        self.on_emit = None
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=10000")
        self.db.execute("PRAGMA foreign_keys=ON")
        self._schema()
        self.presence = {}  # member_id -> {member, area, coll, id, editing, at}

    # ---------- plumbing ----------

    def close(self):
        with self.lock:
            self.db.close()

    def now(self):
        return float(self.clock())

    def emit(self, kind, payload, to=None):
        """Hand a live update to the SSE layer (never raises into the caller)."""
        if self.on_emit:
            try:
                self.on_emit(kind, payload, to)
            except Exception:
                pass

    @contextmanager
    def tx(self):
        """One write transaction under the lock."""
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            else:
                self.db.execute("COMMIT")

    def q(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def q1(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args).fetchone()

    def _schema(self):
        with self.lock:
            self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS members (
                id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
                role TEXT NOT NULL, approver INTEGER NOT NULL DEFAULT 0, colour TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS devices (
                id INTEGER PRIMARY KEY AUTOINCREMENT, member_id INTEGER NOT NULL REFERENCES members(id),
                token_hash TEXT UNIQUE NOT NULL, name TEXT NOT NULL, created_at REAL NOT NULL,
                last_seen REAL, revoked_at REAL);
            CREATE TABLE IF NOT EXISTS invites (
                id INTEGER PRIMARY KEY AUTOINCREMENT, token_hash TEXT UNIQUE NOT NULL, email TEXT NOT NULL,
                name TEXT, role TEXT NOT NULL, created_by INTEGER, created_at REAL NOT NULL,
                expires_at REAL NOT NULL, used_at REAL, used_by_device INTEGER, member_id INTEGER);
            CREATE TABLE IF NOT EXISTS items (
                coll TEXT NOT NULL, id TEXT NOT NULL, version INTEGER NOT NULL, data TEXT NOT NULL,
                deleted INTEGER NOT NULL DEFAULT 0, author INTEGER, updated_by INTEGER, updated_at REAL NOT NULL,
                changeset TEXT, seq INTEGER NOT NULL, PRIMARY KEY (coll, id));
            CREATE INDEX IF NOT EXISTS items_seq ON items(seq);
            CREATE TABLE IF NOT EXISTS events (
                rowid_ INTEGER PRIMARY KEY AUTOINCREMENT, seq INTEGER UNIQUE NOT NULL, at REAL NOT NULL,
                actor INTEGER, kind TEXT NOT NULL, area TEXT, coll TEXT, item_id TEXT, summary TEXT, data TEXT);
            CREATE TABLE IF NOT EXISTS edit_sessions (
                member_id INTEGER PRIMARY KEY, event_rowid INTEGER NOT NULL, started_at REAL NOT NULL,
                last_at REAL NOT NULL, items TEXT NOT NULL, writes INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT, coll TEXT NOT NULL, item_id TEXT NOT NULL, parent INTEGER,
                author INTEGER NOT NULL, body TEXT NOT NULL, mentions TEXT NOT NULL, resolved INTEGER NOT NULL DEFAULT 0,
                at REAL NOT NULL, edited_at REAL);
            CREATE INDEX IF NOT EXISTS comments_item ON comments(coll, item_id);
            CREATE TABLE IF NOT EXISTS mentions (
                comment_id INTEGER NOT NULL, member_id INTEGER NOT NULL, read INTEGER NOT NULL DEFAULT 0,
                at REAL NOT NULL, PRIMARY KEY (comment_id, member_id));
            CREATE TABLE IF NOT EXISTS claims (
                coll TEXT NOT NULL, id TEXT NOT NULL, member INTEGER NOT NULL, note TEXT, at REAL NOT NULL,
                PRIMARY KEY (coll, id));
            """)
            cols = [r[1] for r in self.db.execute("PRAGMA table_info(invites)")]
            if "member_id" not in cols:
                self.db.execute("ALTER TABLE invites ADD COLUMN member_id INTEGER")

    def get_meta(self, key, default=None):
        r = self.q1("SELECT value FROM meta WHERE key=?", (key,))
        return r["value"] if r else default

    def set_meta(self, key, value):
        with self.lock:
            self.db.execute("INSERT INTO meta(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (key, str(value)))

    def _next(self, db, name):
        """Next value of a named counter (inside a transaction)."""
        r = db.execute("SELECT value FROM meta WHERE key=?", (name,)).fetchone()
        n = int(r["value"]) + 1 if r else 1
        db.execute("INSERT INTO meta(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (name, str(n)))
        return n

    def team_name(self):
        return self.get_meta("team_name", "Team")

    def item_cursor(self):
        return int(self.get_meta("item_seq", "0"))

    def event_seq(self):
        return int(self.get_meta("event_seq", "0"))

    # ---------- members, invites, devices ----------

    @staticmethod
    def member_out(r):
        return {"id": r["id"], "email": r["email"], "name": r["name"], "role": r["role"],
                "approver": bool(r["approver"]), "colour": r["colour"], "active": bool(r["active"])}

    def member(self, mid):
        r = self.q1("SELECT * FROM members WHERE id=?", (mid,))
        if not r:
            raise ApiError(404, "member not found")
        return self.member_out(r)

    def _pick_colour(self, db):
        """The palette colour used by the fewest members (first one on a tie), so colours stay distinct."""
        used = {}
        for r in db.execute("SELECT colour, COUNT(*) n FROM members GROUP BY colour"):
            used[r["colour"]] = r["n"]
        return min(PALETTE, key=lambda c: (used.get(c, 0), PALETTE.index(c)))

    def create_invite(self, email, role, name=None, by=None):
        """Make a single-use invite and return (token, expires_at). `by` is the inviting member dict or None (CLI)."""
        email = need_email({"email": email})
        role = need_role({"role": role})
        if name is not None:
            name = need_str({"name": name}, "name", MAX_NAME, required=False)
        token = secrets.token_urlsafe(32)
        now = self.now()
        with self.tx() as db:
            existing = db.execute("SELECT * FROM members WHERE email=?", (email,)).fetchone()
            if by is not None:
                if existing is not None:
                    # A second device for an existing member: only that member or an owner may ask for one,
                    # so nobody can take over someone else's identity.
                    if existing["id"] != by["id"] and by["role"] != "owner":
                        raise ApiError(403, "that email already belongs to a member; only they or an owner can"
                                            " invite another device for it")
                else:
                    if by["role"] not in MANAGERS:
                        raise ApiError(403, "only an admin or owner can invite")
                    if role == "owner" and by["role"] != "owner":
                        raise ApiError(403, "only an owner can invite an owner")
            if existing is not None:
                role = existing["role"]  # a device invite never changes the member's role
            db.execute("INSERT INTO invites(token_hash, email, name, role, created_by, created_at, expires_at,"
                       " member_id) VALUES(?,?,?,?,?,?,?,?)",
                       (hash_token(token), email, name, role, by["id"] if by else None, now, now + INVITE_TTL,
                        existing["id"] if existing is not None else None))
        return token, now + INVITE_TTL

    def has_owner(self):
        return self.q1("SELECT 1 FROM members WHERE role='owner' AND active=1") is not None

    def join(self, invite, device_name):
        """Redeem an invite: create (or reuse) the member, add a device, return (device_token, member)."""
        invite = need_str({"invite": invite}, "invite", 200)
        device_name = need_str({"device_name": device_name}, "device_name", MAX_NAME, required=False, default="Device")
        now = self.now()
        token = secrets.token_urlsafe(32)
        with self.tx() as db:
            inv = db.execute("SELECT * FROM invites WHERE token_hash=?", (hash_token(invite),)).fetchone()
            if not inv:
                raise ApiError(401, "invite not valid")
            if inv["used_at"] is not None:
                raise ApiError(401, "invite already used")
            if inv["expires_at"] <= now:
                raise ApiError(401, "invite expired")
            m = db.execute("SELECT * FROM members WHERE email=?", (inv["email"],)).fetchone()
            if m and not m["active"]:
                raise ApiError(403, "member is deactivated")
            if m is not None and inv["member_id"] != m["id"]:
                # Made for a new member, but someone with that email joined since: never hand over the identity.
                raise ApiError(403, "this invite was for a new member and that email is already taken;"
                                    " ask for a new invite")
            if m is None and inv["member_id"] is not None:
                raise ApiError(401, "invite not valid")
            new_member = m is None
            if new_member:
                name = inv["name"] or inv["email"].split("@")[0]
                cur = db.execute("INSERT INTO members(email, name, role, approver, colour, active, created_at)"
                                 " VALUES(?,?,?,0,?,1,?)",
                                 (inv["email"], name, inv["role"], self._pick_colour(db), now))
                mid = cur.lastrowid
            else:
                mid = m["id"]
            cur = db.execute("INSERT INTO devices(member_id, token_hash, name, created_at, last_seen) VALUES(?,?,?,?,?)",
                             (mid, hash_token(token), device_name, now, now))
            db.execute("UPDATE invites SET used_at=?, used_by_device=? WHERE id=?", (now, cur.lastrowid, inv["id"]))
            mrow = db.execute("SELECT * FROM members WHERE id=?", (mid,)).fetchone()
            seq = None
            if new_member:
                seq = self._add_event(db, mid, "join", "team", None, None, "%s joined the team" % mrow["name"], {})
        if seq:
            self.emit("event", {"seq": seq})
        return token, self.member_out(mrow)

    def auth(self, token):
        """Resolve a bearer token to {'member': dict, 'device_id': int}; raise 401 if unknown, revoked or deactivated."""
        if not token or len(token) > 200:
            raise ApiError(401, "missing or bad token")
        r = self.q1("SELECT d.id did, d.name dname, d.last_seen, d.revoked_at, m.* FROM devices d"
                    " JOIN members m ON m.id=d.member_id WHERE d.token_hash=?", (hash_token(token),))
        if not r or r["revoked_at"] is not None or not r["active"]:
            raise ApiError(401, "missing or bad token")
        now = self.now()
        if r["last_seen"] is None or now - r["last_seen"] > 60:
            with self.lock:
                self.db.execute("UPDATE devices SET last_seen=? WHERE id=?", (now, r["did"]))
        return {"member": self.member_out(r), "device_id": r["did"], "device_name": r["dname"]}

    def device_ok(self, did):
        """True while the device is not revoked and its member is active."""
        r = self.q1("SELECT d.revoked_at, m.active FROM devices d JOIN members m ON m.id=d.member_id WHERE d.id=?",
                    (did,))
        return bool(r) and r["revoked_at"] is None and bool(r["active"])

    def devices_of(self, mid):
        return [{"id": r["id"], "name": r["name"], "created_at": iso(r["created_at"]),
                 "last_seen": iso(r["last_seen"]), "revoked": r["revoked_at"] is not None}
                for r in self.q("SELECT * FROM devices WHERE member_id=? ORDER BY id", (mid,))]

    def list_members(self, with_devices=False):
        out = []
        for r in self.q("SELECT * FROM members ORDER BY id"):
            m = self.member_out(r)
            if with_devices:
                m["devices"] = self.devices_of(r["id"])
            out.append(m)
        return out

    def pending_invites(self):
        now = self.now()
        return [{"email": r["email"], "name": r["name"], "role": r["role"], "expires_at": iso(r["expires_at"])}
                for r in self.q("SELECT * FROM invites WHERE used_at IS NULL AND expires_at>? ORDER BY id", (now,))]

    def update_member(self, actor, mid, body):
        """Admin/owner change of role, approver flag or active state."""
        if actor["role"] not in MANAGERS:
            raise ApiError(403, "only an admin or owner can change members")
        fields = {}
        if "role" in body:
            fields["role"] = need_role(body)
        if "approver" in body:
            fields["approver"] = 1 if need_bool(body["approver"], "approver") else 0
        if "active" in body:
            fields["active"] = 1 if need_bool(body["active"], "active") else 0
        if not fields:
            raise ApiError(400, "nothing to change (role, approver, active)")
        with self.tx() as db:
            t = db.execute("SELECT * FROM members WHERE id=?", (mid,)).fetchone()
            if not t:
                raise ApiError(404, "member not found")
            if actor["role"] != "owner" and (t["role"] == "owner" or fields.get("role") == "owner"):
                raise ApiError(403, "only an owner can change an owner or make one")
            losing_owner = t["role"] == "owner" and t["active"] and (
                fields.get("role", "owner") != "owner" or fields.get("active", 1) == 0)
            if losing_owner:
                n = db.execute("SELECT COUNT(*) n FROM members WHERE role='owner' AND active=1").fetchone()["n"]
                if n <= 1:
                    raise ApiError(409, "the team needs at least one active owner")
            sets = ", ".join("%s=?" % k for k in fields)
            db.execute("UPDATE members SET %s WHERE id=?" % sets, list(fields.values()) + [mid])
            if fields.get("active") == 0:
                # Reactivating later must not bring old tokens back: the member joins again with a new invite.
                db.execute("UPDATE devices SET revoked_at=? WHERE member_id=? AND revoked_at IS NULL",
                           (self.now(), mid))
            r = db.execute("SELECT * FROM members WHERE id=?", (mid,)).fetchone()
        if fields.get("active") == 0:
            self.presence.pop(mid, None)
            self.emit("_close_member", {"member": mid})
        return self.member_out(r)

    def revoke_device(self, actor, did):
        """Revoke one device token. Admin/owner for anyone's device, any member for their own. actor None = CLI."""
        did = need_int(did, "device", 1)
        with self.tx() as db:
            d = db.execute("SELECT * FROM devices WHERE id=?", (did,)).fetchone()
            if not d:
                raise ApiError(404, "device not found")
            if actor is not None and d["member_id"] != actor["id"]:
                if actor["role"] not in MANAGERS:
                    raise ApiError(403, "only an admin or owner can revoke another member's device")
                target = db.execute("SELECT role FROM members WHERE id=?", (d["member_id"],)).fetchone()
                if target and target["role"] == "owner" and actor["role"] != "owner":
                    raise ApiError(403, "only an owner can revoke an owner's device")
            if d["revoked_at"] is None:
                db.execute("UPDATE devices SET revoked_at=? WHERE id=?", (self.now(), did))
        self.emit("_close_device", {"device": did})
        return {"ok": True, "device": did}

    # ---------- items ----------

    @staticmethod
    def item_out(r):
        if r is None:
            return None
        return {"coll": r["coll"], "id": r["id"], "version": r["version"], "data": json.loads(r["data"]),
                "deleted": bool(r["deleted"]), "author": r["author"], "updated_by": r["updated_by"],
                "updated_at": iso(r["updated_at"]),
                "changeset": json.loads(r["changeset"]) if r["changeset"] else None}

    def items_since(self, since, limit=1000):
        """Items changed after the cursor, oldest change first. `more` is true when the page was full."""
        with self.lock:
            rows = self.db.execute("SELECT * FROM items WHERE seq>? ORDER BY seq LIMIT ?", (since, limit + 1)).fetchall()
            more = len(rows) > limit
            rows = rows[:limit]
            cursor = rows[-1]["seq"] if more else max(self.item_cursor(), since)
        return {"items": [self.item_out(r) for r in rows], "cursor": cursor, "more": more}

    def _check_op(self, op):
        if not isinstance(op, dict):
            raise ApiError(400, "each op must be an object")
        kind = op.get("op")
        if kind not in ("put", "delete"):
            raise ApiError(400, "op must be put or delete")
        coll = need_str(op, "coll", MAX_KEY)
        iid = op.get("id")
        if isinstance(iid, int) and not isinstance(iid, bool):
            iid = str(iid)
        iid = need_str({"id": iid}, "id", MAX_KEY)
        base = need_int(op.get("base_version", None), "base_version", 0)
        data = None
        if kind == "put":
            data = op.get("data")
            if not isinstance(data, dict):
                raise ApiError(400, "data must be an object for put")
            if json_size(data) > MAX_ITEM_BYTES:
                raise ApiError(400, "item data is too large (max %d bytes)" % MAX_ITEM_BYTES)
        cs = op.get("changeset")
        if cs is not None:
            if not isinstance(cs, dict):
                raise ApiError(400, "changeset must be an object {id, name}")
            cs_id = cs.get("id")
            if isinstance(cs_id, int) and not isinstance(cs_id, bool):
                cs_id = str(cs_id)
            cs = {"id": need_str({"id": cs_id}, "id", MAX_KEY),
                  "name": need_str(cs, "name", MAX_NAME, required=False, default="")}
        return kind, coll, iid, base, data, cs

    def write_items(self, actor, ops):
        """Apply put/delete ops with optimistic versions. Returns {results, cursor}."""
        if actor["role"] == "viewer":
            raise ApiError(403, "viewers cannot change items")
        if not isinstance(ops, list) or not ops:
            raise ApiError(400, "ops must be a non-empty list")
        if len(ops) > MAX_OPS:
            raise ApiError(400, "too many ops in one request (max %d)" % MAX_OPS)
        checked = [self._check_op(op) for op in ops]
        now = self.now()
        results, touched = [], []
        session_seq = None
        with self.tx() as db:
            for kind, coll, iid, base, data, cs in checked:
                row = db.execute("SELECT * FROM items WHERE coll=? AND id=?", (coll, iid)).fetchone()
                cur = row["version"] if row else 0
                res = {"coll": coll, "id": iid}
                if base != cur:
                    res.update(ok=False, conflict=True, server=self.item_out(row))
                elif kind == "put":
                    seq = self._next(db, "item_seq")
                    csj = json.dumps(cs) if cs else None
                    dj = json.dumps(data, separators=(",", ":"))
                    if row:
                        db.execute("UPDATE items SET version=?, data=?, deleted=0, updated_by=?, updated_at=?,"
                                   " changeset=?, seq=? WHERE coll=? AND id=?",
                                   (cur + 1, dj, actor["id"], now, csj, seq, coll, iid))
                    else:
                        db.execute("INSERT INTO items(coll, id, version, data, deleted, author, updated_by, updated_at,"
                                   " changeset, seq) VALUES(?,?,1,?,0,?,?,?,?,?)",
                                   (coll, iid, dj, actor["id"], actor["id"], now, csj, seq))
                    res.update(ok=True, version=cur + 1)
                    touched.append((coll, iid))
                else:  # delete
                    if not row:
                        res.update(ok=False, error="not found")
                    elif row["deleted"]:
                        res.update(ok=True, version=cur)
                    else:
                        seq = self._next(db, "item_seq")
                        db.execute("UPDATE items SET version=?, deleted=1, updated_by=?, updated_at=?, seq=?,"
                                   " changeset=COALESCE(?, changeset) WHERE coll=? AND id=?",
                                   (cur + 1, actor["id"], now, seq, json.dumps(cs) if cs else None, coll, iid))
                        res.update(ok=True, version=cur + 1)
                        touched.append((coll, iid))
                results.append(res)
            if touched:
                session_seq = self._touch_session(db, actor, touched, now)
            cursor = self.item_cursor()
        if touched:
            self.emit("items", {"cursor": cursor})
        if session_seq:
            self.emit("event", {"seq": session_seq})
        return {"results": results, "cursor": cursor}

    # ---------- events ----------

    @staticmethod
    def event_out(r):
        return {"seq": r["seq"], "at": iso(r["at"]), "actor": r["actor"], "kind": r["kind"], "area": r["area"],
                "coll": r["coll"], "id": r["item_id"], "summary": r["summary"],
                "data": json.loads(r["data"]) if r["data"] else {}}

    def _add_event(self, db, actor_id, kind, area, coll, iid, summary, data):
        """Insert an event inside a transaction and return its seq."""
        seq = self._next(db, "event_seq")
        db.execute("INSERT INTO events(seq, at, actor, kind, area, coll, item_id, summary, data) VALUES(?,?,?,?,?,?,?,?,?)",
                   (seq, self.now(), actor_id, kind, area, coll, iid, summary, json.dumps(data or {})))
        return seq

    def _touch_session(self, db, actor, touched, now):
        """Fold item writes into the member's open edit_session event, or start a new one. Returns the event seq.

        The event keeps one row per session; each update moves it to a fresh seq so `events?since=` picks it up.
        data.session is the stable session id clients use to replace the earlier copy.
        """
        s = db.execute("SELECT * FROM edit_sessions WHERE member_id=?", (actor["id"],)).fetchone()
        seq = self._next(db, "event_seq")
        if s and now - s["last_at"] < SESSION_IDLE:
            keys = [tuple(k) for k in json.loads(s["items"])]
            seen = set(keys)
            for k in touched:
                if k not in seen:
                    keys.append(k)
                    seen.add(k)
            writes = s["writes"] + len(touched)
            started, rowid = s["started_at"], s["event_rowid"]
            fields = self._session_fields(actor, keys, writes, started, rowid)
            db.execute("UPDATE events SET seq=?, at=?, area=?, coll=?, item_id=?, summary=?, data=? WHERE rowid_=?",
                       (seq, now) + fields + (rowid,))
            db.execute("UPDATE edit_sessions SET last_at=?, items=?, writes=? WHERE member_id=?",
                       (now, json.dumps(keys), writes, actor["id"]))
        else:
            keys = []
            for k in touched:
                if k not in keys:
                    keys.append(k)
            cur = db.execute("INSERT INTO events(seq, at, actor, kind) VALUES(?,?,?,'edit_session')",
                             (seq, now, actor["id"]))
            rowid = cur.lastrowid
            fields = self._session_fields(actor, keys, len(touched), now, rowid)
            db.execute("UPDATE events SET area=?, coll=?, item_id=?, summary=?, data=? WHERE rowid_=?", fields + (rowid,))
            db.execute("INSERT INTO edit_sessions(member_id, event_rowid, started_at, last_at, items, writes)"
                       " VALUES(?,?,?,?,?,?) ON CONFLICT(member_id) DO UPDATE SET event_rowid=excluded.event_rowid,"
                       " started_at=excluded.started_at, last_at=excluded.last_at, items=excluded.items,"
                       " writes=excluded.writes",
                       (actor["id"], rowid, now, now, json.dumps(keys), len(touched)))
        return seq

    @staticmethod
    def _session_fields(actor, keys, writes, started, rowid):
        """(area, coll, item_id, summary, data) for an edit_session event."""
        colls = {}
        for c, _ in keys:
            colls[c] = colls.get(c, 0) + 1
        names = list(colls)
        n = len(keys)
        where = ", ".join(names[:3]) + (" and %d more" % (len(names) - 3) if len(names) > 3 else "")
        summary = "%d change%s in %s" % (n, "" if n == 1 else "s", where)
        areas = {area_of(c) for c in names}
        area = areas.pop() if len(areas) == 1 else "multi"
        coll = names[0] if len(names) == 1 else None
        iid = keys[0][1] if n == 1 else None
        data = {"session": rowid, "count": n, "writes": writes, "colls": colls, "started_at": iso(started),
                "items": [{"coll": c, "id": i} for c, i in keys[:SESSION_ITEMS_SHOWN]],
                "truncated": n > SESSION_ITEMS_SHOWN}
        return (area, coll, iid, summary, json.dumps(data))

    def events_since(self, since, limit=200):
        """Events after `since`, newest last; `seq` is where the next call should start."""
        with self.lock:
            rows = self.db.execute("SELECT * FROM events WHERE seq>? ORDER BY seq LIMIT ?", (since, limit)).fetchall()
            seq = rows[-1]["seq"] if len(rows) == limit else max(self.event_seq(), since)
        return {"events": [self.event_out(r) for r in rows], "seq": seq}

    def post_event(self, actor, body):
        """A client-reported event (push / publish / undo / custom)."""
        if actor["role"] == "viewer":
            raise ApiError(403, "viewers cannot post events")
        kind = need_str(body, "kind", 30)
        if kind not in CLIENT_EVENT_KINDS:
            raise ApiError(400, "kind must be one of %s" % ", ".join(CLIENT_EVENT_KINDS))
        area = need_str(body, "area", MAX_AREA)
        coll = need_str(body, "coll", MAX_KEY, required=False)
        iid = body.get("id")
        if isinstance(iid, int) and not isinstance(iid, bool):
            iid = str(iid)
        iid = need_str({"id": iid}, "id", MAX_KEY, required=False)
        summary = need_str(body, "summary", MAX_SUMMARY)
        data = body.get("data") or {}
        if not isinstance(data, dict):
            raise ApiError(400, "data must be an object")
        if json_size(data) > MAX_DATA_BYTES:
            raise ApiError(400, "data is too large")
        with self.tx() as db:
            seq = self._add_event(db, actor["id"], kind, area, coll, iid, summary, data)
            r = db.execute("SELECT * FROM events WHERE seq=?", (seq,)).fetchone()
        self.emit("event", {"seq": seq})
        return self.event_out(r)

    # ---------- comments and mentions ----------

    @staticmethod
    def comment_out(r):
        return {"id": r["id"], "coll": r["coll"], "item_id": r["item_id"], "parent": r["parent"],
                "author": r["author"], "body": r["body"], "mentions": json.loads(r["mentions"]),
                "resolved": bool(r["resolved"]), "at": iso(r["at"]), "edited_at": iso(r["edited_at"])}

    def parse_mentions(self, body, db=None):
        """Member ids mentioned as @name (spaces removed) or @email-local-part. Case-insensitive; a token that
        matches more than one active member mentions nobody."""
        db = db or self.db
        keys = {}
        for r in db.execute("SELECT id, name, email FROM members WHERE active=1"):
            for k in {r["name"].replace(" ", "").lower(), r["email"].split("@")[0].lower()}:
                if k:
                    keys.setdefault(k, set()).add(r["id"])
        out = []
        for m in MENTION_RE.finditer(body):
            tok = m.group(1).lower()
            for cand in (tok, tok.rstrip("._+-")):
                ids = keys.get(cand)
                if ids:
                    if len(ids) == 1:
                        mid = next(iter(ids))
                        if mid not in out:
                            out.append(mid)
                    break
        return out

    def _explicit_mentions(self, db, value):
        if value is None:
            return []
        if not isinstance(value, list) or len(value) > 50:
            raise ApiError(400, "mentions must be a list of member ids")
        out = []
        for v in value:
            mid = need_int(v, "mentions", 1)
            if not db.execute("SELECT 1 FROM members WHERE id=? AND active=1", (mid,)).fetchone():
                raise ApiError(400, "mentions: unknown member %d" % mid)
            if mid not in out:
                out.append(mid)
        return out

    def comments_for(self, coll, iid):
        coll = need_str({"coll": coll}, "coll", MAX_KEY)
        iid = need_str({"id": iid}, "id", MAX_KEY)
        return [self.comment_out(r) for r in
                self.q("SELECT * FROM comments WHERE coll=? AND item_id=? ORDER BY id", (coll, iid))]

    def comment_counts(self, coll):
        """{item_id: {open, total, latest}}: open = unresolved threads (top-level comments), total = all comments."""
        coll = need_str({"coll": coll}, "coll", MAX_KEY)
        out = {}
        for r in self.q("SELECT item_id, COUNT(*) total,"
                        " SUM(CASE WHEN parent IS NULL AND resolved=0 THEN 1 ELSE 0 END) open_, MAX(at) latest"
                        " FROM comments WHERE coll=? GROUP BY item_id", (coll,)):
            out[r["item_id"]] = {"open": r["open_"], "total": r["total"], "latest": iso(r["latest"])}
        return out

    def add_comment(self, actor, body):
        """New comment or reply. Any active member (viewers too) may comment."""
        coll = need_str(body, "coll", MAX_KEY)
        iid = body.get("id")
        if isinstance(iid, int) and not isinstance(iid, bool):
            iid = str(iid)
        iid = need_str({"id": iid}, "id", MAX_KEY)
        text = need_str(body, "body", MAX_BODY)
        parent = body.get("parent")
        now = self.now()
        with self.tx() as db:
            if parent is not None:
                parent = need_int(parent, "parent", 1)
                p = db.execute("SELECT coll, item_id FROM comments WHERE id=?", (parent,)).fetchone()
                if not p:
                    raise ApiError(404, "parent comment not found")
                if (p["coll"], p["item_id"]) != (coll, iid):
                    raise ApiError(400, "parent belongs to another item")
            mentions = self.parse_mentions(text, db)
            for mid in self._explicit_mentions(db, body.get("mentions")):
                if mid not in mentions:
                    mentions.append(mid)
            mentions = [m for m in mentions if m != actor["id"]]
            cur = db.execute("INSERT INTO comments(coll, item_id, parent, author, body, mentions, resolved, at)"
                             " VALUES(?,?,?,?,?,?,0,?)", (coll, iid, parent, actor["id"], text, json.dumps(mentions), now))
            cid = cur.lastrowid
            for mid in mentions:
                db.execute("INSERT OR IGNORE INTO mentions(comment_id, member_id, read, at) VALUES(?,?,0,?)", (cid, mid, now))
            short = text if len(text) <= 80 else text[:77] + "..."
            seq = self._add_event(db, actor["id"], "comment", area_of(coll), coll, iid,
                                  "%s commented: %s" % (actor["name"], short),
                                  {"comment_id": cid, "parent": parent, "mentions": mentions})
            r = db.execute("SELECT * FROM comments WHERE id=?", (cid,)).fetchone()
        self.emit("comment", {"coll": coll, "id": iid})
        self.emit("event", {"seq": seq})
        for mid in mentions:
            self.emit("mention", {"comment_id": cid}, {mid})
        return self.comment_out(r)

    def patch_comment(self, actor, cid, body):
        """Author edits the body; any non-viewer resolves or reopens."""
        cid = need_int(cid, "comment", 1)
        if "body" not in body and "resolved" not in body:
            raise ApiError(400, "nothing to change (body, resolved)")
        now = self.now()
        new_mentions = []
        with self.tx() as db:
            r = db.execute("SELECT * FROM comments WHERE id=?", (cid,)).fetchone()
            if not r:
                raise ApiError(404, "comment not found")
            if "body" in body:
                if r["author"] != actor["id"]:
                    raise ApiError(403, "only the author can edit a comment")
                text = need_str(body, "body", MAX_BODY)
                old = json.loads(r["mentions"])
                mentions = [m for m in self.parse_mentions(text, db) if m != actor["id"]]
                for mid in self._explicit_mentions(db, body.get("mentions")):
                    if mid not in mentions and mid != actor["id"]:
                        mentions.append(mid)
                new_mentions = [m for m in mentions if m not in old]
                gone = [m for m in old if m not in mentions]
                db.execute("UPDATE comments SET body=?, mentions=?, edited_at=? WHERE id=?",
                           (text, json.dumps(mentions), now, cid))
                for mid in new_mentions:
                    db.execute("INSERT OR IGNORE INTO mentions(comment_id, member_id, read, at) VALUES(?,?,0,?)",
                               (cid, mid, now))
                for mid in gone:
                    db.execute("DELETE FROM mentions WHERE comment_id=? AND member_id=?", (cid, mid))
            if "resolved" in body:
                if actor["role"] == "viewer":
                    raise ApiError(403, "viewers cannot resolve comments")
                db.execute("UPDATE comments SET resolved=? WHERE id=?",
                           (1 if need_bool(body["resolved"], "resolved") else 0, cid))
            r = db.execute("SELECT * FROM comments WHERE id=?", (cid,)).fetchone()
        self.emit("comment", {"coll": r["coll"], "id": r["item_id"]})
        for mid in new_mentions:
            self.emit("mention", {"comment_id": cid}, {mid})
        return self.comment_out(r)

    def mentions_for(self, member_id, unread_only=False, limit=200):
        sql = ("SELECT c.*, mn.read mread FROM mentions mn JOIN comments c ON c.id=mn.comment_id"
               " WHERE mn.member_id=?" + (" AND mn.read=0" if unread_only else "") + " ORDER BY c.id DESC LIMIT ?")
        return [{"comment": self.comment_out(r), "read": bool(r["mread"])} for r in self.q(sql, (member_id, limit))]

    def mark_read(self, member_id, ids):
        if not isinstance(ids, list) or len(ids) > 1000:
            raise ApiError(400, "ids must be a list of comment ids")
        ids = [need_int(i, "ids", 1) for i in ids]
        with self.tx() as db:
            n = 0
            for cid in ids:
                n += db.execute("UPDATE mentions SET read=1 WHERE comment_id=? AND member_id=?", (cid, member_id)).rowcount
        return {"ok": True, "updated": n}

    # ---------- presence ----------

    def presence_beat(self, actor, body):
        area = need_str(body, "area", MAX_AREA)
        coll = need_str(body, "coll", MAX_KEY, required=False)
        iid = body.get("id")
        if isinstance(iid, int) and not isinstance(iid, bool):
            iid = str(iid)
        iid = need_str({"id": iid}, "id", MAX_KEY, required=False)
        editing = body.get("editing", False)
        editing = need_bool(editing, "editing")
        now = self.now()
        with self.lock:
            old = self.presence.get(actor["id"])
            changed = (old is None or now - old["_t"] >= PRESENCE_TTL
                       or (old["area"], old["coll"], old["id"], old["editing"]) != (area, coll, iid, editing))
            self.presence[actor["id"]] = {"member": actor["id"], "area": area, "coll": coll, "id": iid,
                                          "editing": editing, "_t": now}
        if changed:
            self.emit("presence", {"presence": self.presence_list()})
        return {"presence": self.presence_list()}

    def presence_list(self):
        now = self.now()
        with self.lock:
            return [{"member": p["member"], "area": p["area"], "coll": p["coll"], "id": p["id"],
                     "editing": p["editing"], "at": iso(p["_t"])}
                    for p in self.presence.values() if now - p["_t"] < PRESENCE_TTL]

    def expire_presence(self):
        """Drop entries older than PRESENCE_TTL; tell clients if any went. Returns how many expired."""
        now = self.now()
        with self.lock:
            gone = [k for k, p in self.presence.items() if now - p["_t"] >= PRESENCE_TTL]
            for k in gone:
                del self.presence[k]
        if gone:
            self.emit("presence", {"presence": self.presence_list()})
        return len(gone)

    # ---------- claims ----------

    @staticmethod
    def claim_out(r):
        return {"coll": r["coll"], "id": r["id"], "member": r["member"], "note": r["note"], "at": iso(r["at"])}

    def _claim_key(self, body):
        coll = need_str(body, "coll", MAX_KEY)
        iid = body.get("id")
        if isinstance(iid, int) and not isinstance(iid, bool):
            iid = str(iid)
        return coll, need_str({"id": iid}, "id", MAX_KEY)

    def claim(self, actor, body):
        if actor["role"] == "viewer":
            raise ApiError(403, "viewers cannot claim items")
        coll, iid = self._claim_key(body)
        note = need_str(body, "note", MAX_NOTE, required=False)
        now = self.now()
        with self.tx() as db:
            r = db.execute("SELECT * FROM claims WHERE coll=? AND id=?", (coll, iid)).fetchone()
            if r and r["member"] != actor["id"]:
                raise ApiError(409, "already claimed by another member", {"claim": self.claim_out(r)})
            db.execute("INSERT INTO claims(coll, id, member, note, at) VALUES(?,?,?,?,?) ON CONFLICT(coll, id)"
                       " DO UPDATE SET note=excluded.note, at=excluded.at", (coll, iid, actor["id"], note, now))
            seq = self._add_event(db, actor["id"], "claim", area_of(coll), coll, iid,
                                  "%s claimed %s/%s" % (actor["name"], coll, iid), {"claimed": True, "note": note})
            r = db.execute("SELECT * FROM claims WHERE coll=? AND id=?", (coll, iid)).fetchone()
        self.emit("event", {"seq": seq})
        return self.claim_out(r)

    def release(self, actor, body):
        coll, iid = self._claim_key(body)
        with self.tx() as db:
            r = db.execute("SELECT * FROM claims WHERE coll=? AND id=?", (coll, iid)).fetchone()
            if not r:
                raise ApiError(404, "no claim on that item")
            if r["member"] != actor["id"] and actor["role"] not in MANAGERS:
                raise ApiError(403, "only the claimant or an admin can release a claim")
            db.execute("DELETE FROM claims WHERE coll=? AND id=?", (coll, iid))
            seq = self._add_event(db, actor["id"], "claim", area_of(coll), coll, iid,
                                  "%s released %s/%s" % (actor["name"], coll, iid), {"claimed": False})
        self.emit("event", {"seq": seq})
        return {"ok": True}

    def claims(self):
        return [self.claim_out(r) for r in self.q("SELECT * FROM claims ORDER BY at")]
