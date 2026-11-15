"""Team settings: values the whole team shares (copy rules, SEO sites, test email addresses, SoMe slots and UTM
defaults, calendar limits). See PROTOCOL.md, "Team settings".

One row per key, versioned. Everyone reads; owners and admins write. Every write bumps the key's version, adds a
`settings` event to the activity feed and sends the SSE event `settings` {key, version} to everyone.

The server only checks the key name, that the value is a JSON object and its size: what goes inside a value is
checked by the app (engine/team_settings.py), which also ignores anything it can't use.

Wiring (kept to two lines in app.py): TeamServer calls install(store) after the Store opens, and ROUTES gets routes().
"""
import json

from .store import MANAGERS, ApiError, iso, json_size, need_int

KEYS = ("copy_rules", "seo_sites", "email_test_addresses", "social", "calendar_limits")
MAX_VALUE_BYTES = 64 * 1024


def install(store):
    """Create the table (idempotent)."""
    with store.lock:
        store.db.executescript("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY, value TEXT, version INTEGER NOT NULL, updated_by INTEGER, updated_at REAL NOT NULL);
        """)


def _out(key, r):
    if r is None:
        return {"key": key, "value": None, "version": 0, "updated_by": None, "updated_at": None}
    return {"key": key, "value": json.loads(r["value"]) if r["value"] is not None else None, "version": r["version"],
            "updated_by": r["updated_by"], "updated_at": iso(r["updated_at"])}


def _need_key(key):
    if key not in KEYS:
        raise ApiError(404, "unknown setting %s (known: %s)" % (str(key)[:50], ", ".join(KEYS)))
    return key


def get_all(store):
    rows = {r["key"]: r for r in store.q("SELECT * FROM settings")}
    return {"settings": {k: _out(k, rows.get(k)) for k in KEYS}}


def get_one(store, key):
    _need_key(key)
    return _out(key, store.q1("SELECT * FROM settings WHERE key=?", (key,)))


def put(store, actor, key, body):
    """Set a key ({value, base_version?}). value null clears it (everyone falls back to their own setting).
    base_version, when given, must be the current version (else 409 with the current setting)."""
    _need_key(key)
    if actor["role"] not in MANAGERS:
        raise ApiError(403, "only an owner or admin can change team settings")
    if "value" not in body:
        raise ApiError(400, "value is required (null clears the team setting)")
    value = body.get("value")
    if value is not None and not isinstance(value, dict):
        raise ApiError(400, "value must be an object (or null)")
    if value is not None and json_size(value) > MAX_VALUE_BYTES:
        raise ApiError(400, "value is too large (max %d bytes)" % MAX_VALUE_BYTES)
    base = body.get("base_version")
    if base is not None:
        base = need_int(base, "base_version", 0)
    now = store.now()
    with store.tx() as db:
        r = db.execute("SELECT * FROM settings WHERE key=?", (key,)).fetchone()
        cur = r["version"] if r else 0
        if base is not None and base != cur:
            raise ApiError(409, "the team setting changed meanwhile", {"setting": _out(key, r)})
        vj = json.dumps(value, separators=(",", ":")) if value is not None else None
        db.execute("INSERT INTO settings(key, value, version, updated_by, updated_at) VALUES(?,?,?,?,?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value, version=excluded.version, "
                   "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                   (key, vj, cur + 1, actor["id"], now))
        summary = ("%s changed the team setting %s" if value is not None else "%s cleared the team setting %s") % (
            actor["name"], key)
        seq = store._add_event(db, actor["id"], "settings", "team", None, None, summary[:500],
                               {"key": key, "version": cur + 1})
        r = db.execute("SELECT * FROM settings WHERE key=?", (key,)).fetchone()
    store.emit("settings", {"key": key, "version": cur + 1})
    store.emit("event", {"seq": seq})
    return _out(key, r)


# ---------- HTTP ----------

def r_all(handler, query, body):
    handler.authed(query)
    return get_all(handler.team.store)


def r_get(handler, query, body, key):
    handler.authed(query)
    return get_one(handler.team.store, key)


def r_put(handler, query, body, key):
    ctx = handler.authed(query)
    return put(handler.team.store, ctx["member"], key, body)


def r_delete(handler, query, body, key):
    ctx = handler.authed(query)
    return put(handler.team.store, ctx["member"], key, {"value": None})


def routes(make):
    """make(method, path, fn) -> a ROUTES entry (app._r)."""
    return [make("GET", r"/api/v1/settings", r_all),
            make("GET", r"/api/v1/settings/([a-z_]{1,50})", r_get),
            make("PUT", r"/api/v1/settings/([a-z_]{1,50})", r_put),
            make("DELETE", r"/api/v1/settings/([a-z_]{1,50})", r_delete)]
