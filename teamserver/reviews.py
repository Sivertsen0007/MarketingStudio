"""Reviews (approvals), the approval policy and owners for the team server (see PROTOCOL.md, "Reviews").

A review asks named reviewers to approve a set of items at given versions (a changeset, a selection from a draft, or a
whole post / page / campaign). An approval covers each item only while the item's content is what was asked about:
the server keeps a content hash per item (bookkeeping keys left out, see CONTENT_SKIP) and reports `stale: true` for an
item whose content changed since. Clients gate Push live / Publish / Send / Schedule on that.

Wiring: `install(store)` adds the tables (called by TeamServer), `ROUTES` holds the HTTP routes app.py appends to its
own. All writes go through store.tx() (one lock, one connection), events through store._add_event, live updates through
store.emit (SSE `review` to the reviewers and the requester, `policy` / `owners` to everyone).
"""
import hashlib
import json
import re

from .store import (ApiError, MANAGERS, MAX_KEY, MAX_NAME, MAX_NOTE, area_of, iso, need_bool, need_coll, need_int,
                    need_item_id, need_str)

AREAS = ("ads", "email", "seo", "social")
STATUSES = ("open", "approved", "changes_requested", "withdrawn")
MAX_ITEMS = 500
MAX_REVIEWERS = 20
MAX_TITLE = 200
MAX_LIST = 200
MAX_OWNERS = 2000
# Top-level keys that are bookkeeping, not content: changing only these does not make an approval stale.
CONTENT_SKIP = ("at", "ts", "by", "updated", "updated_at", "edited_at", "edited_by", "problems", "warnings",
                "approval", "approved_by", "approved_at")
SCOPE_RE = re.compile(r"[a-z_][^\x00-\x1f\x7f]{0,199}")
HASH_RE = re.compile(r"[0-9a-f]{64}")
CONTENT_COLLS = ("email",)  # items whose live content the server can't see (the platform campaign + the draft)

SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT, cs_id TEXT, cs_name TEXT, title TEXT, area TEXT, items TEXT NOT NULL,
    requested_by INTEGER NOT NULL, reviewers TEXT NOT NULL, status TEXT NOT NULL, note TEXT, decision_note TEXT,
    decided_by INTEGER, decided_at REAL, at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS reviews_status ON reviews(status);
CREATE TABLE IF NOT EXISTS review_policy (area TEXT PRIMARY KEY, require_approval INTEGER NOT NULL, at REAL, by INTEGER);
CREATE TABLE IF NOT EXISTS owners (scope TEXT PRIMARY KEY, member_id INTEGER NOT NULL, at REAL, by INTEGER);
"""


def content_hash(data):
    """sha256 of an item's data without bookkeeping keys; "" for no item (missing or deleted).
    Clients compute the same hash locally (engine/team_reviews.py) to tell whether their copy is the approved one."""
    if not isinstance(data, dict):
        return ""
    d = {k: v for k, v in data.items() if k not in CONTENT_SKIP}
    raw = json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def install(store):
    """Create the tables (idempotent)."""
    with store.lock:
        store.db.executescript(SCHEMA)


# ---------- helpers ----------

def _eligible(m):
    """May this member approve? Approver flag, or admin / owner."""
    return bool(m) and bool(m["active"]) and (bool(m["approver"]) or m["role"] in MANAGERS)


def _current(db, coll, iid):
    """(version, hash) of the item on the server now; (0, "") when it does not exist, (version, "") when deleted."""
    r = db.execute("SELECT version, data, deleted FROM items WHERE coll=? AND id=?", (coll, iid)).fetchone()
    if not r:
        return 0, ""
    if r["deleted"]:
        return r["version"], ""
    return r["version"], content_hash(json.loads(r["data"]))


def _decider_ok(db, r):
    """An approval counts only while the member who gave it may still approve."""
    if r["status"] != "approved" or not r["decided_by"]:
        return False
    m = db.execute("SELECT * FROM members WHERE id=?", (r["decided_by"],)).fetchone()
    return _eligible(m)


def _review_out(db, r):
    items = []
    for it in json.loads(r["items"]):
        version, h = _current(db, it["coll"], it["id"])
        ch = it.get("content_hash")
        # content_hash: the client hashed what a send would really send (e.g. an email campaign: the platform copy with
        # any draft on top). The server can't see that content, so only the client can tell whether it changed.
        items.append({"coll": it["coll"], "id": it["id"], "version": it["version"], "hash": it["hash"],
                      "content_hash": ch, "current_version": version, "stale": False if ch else h != it["hash"]})
    n = db.execute("SELECT COUNT(*) n FROM comments WHERE coll='review' AND item_id=?", (str(r["id"]),)).fetchone()["n"]
    return {"id": r["id"], "changeset": {"id": r["cs_id"], "name": r["cs_name"] or ""} if r["cs_id"] else None,
            "title": r["title"] or "", "area": r["area"] or "", "items": items, "requested_by": r["requested_by"],
            "reviewers": json.loads(r["reviewers"]), "status": r["status"], "note": r["note"] or "",
            "decision_note": r["decision_note"] or "", "decided_by": r["decided_by"], "decided_at": iso(r["decided_at"]),
            "at": iso(r["at"]), "updated_at": iso(r["updated_at"]), "comments": n,
            "stale": any(i["stale"] for i in items), "decider_ok": _decider_ok(db, r)}


def _changeset(body):
    cs = body.get("changeset")
    if cs is None:
        return None
    if not isinstance(cs, dict):
        raise ApiError(400, "changeset must be an object {id, name}")
    cid = cs.get("id")
    if isinstance(cid, int) and not isinstance(cid, bool):
        cid = str(cid)
    return {"id": need_str({"id": cid}, "id", MAX_KEY), "name": need_str(cs, "name", MAX_NAME, required=False, default="")}


def _items_of_changeset(db, actor, cs_id):
    """Every live item whose last write was tagged with this changeset by the requester."""
    out = []
    for r in db.execute("SELECT coll, id, version, data, changeset, updated_by FROM items"
                        " WHERE deleted=0 AND changeset IS NOT NULL AND updated_by=? ORDER BY seq", (actor["id"],)):
        try:
            cs = json.loads(r["changeset"]) or {}
        except ValueError:
            continue
        if str(cs.get("id")) == cs_id:
            out.append({"coll": r["coll"], "id": r["id"], "version": r["version"],
                        "hash": content_hash(json.loads(r["data"]))})
    return out


def _requested_items(db, raw):
    if not isinstance(raw, list) or not raw:
        raise ApiError(400, "items must be a non-empty list of {coll, id, version}")
    if len(raw) > MAX_ITEMS:
        raise ApiError(400, "too many items in one review (max %d)" % MAX_ITEMS)
    out, seen = [], set()
    for it in raw:
        if not isinstance(it, dict):
            raise ApiError(400, "each item must be an object {coll, id, version}")
        coll, iid = need_coll(it), need_item_id(it)
        version = need_int(it.get("version"), "version", 0)
        if (coll, iid) in seen:
            continue
        seen.add((coll, iid))
        ch = it.get("content_hash")
        if ch is not None and coll not in CONTENT_COLLS:
            raise ApiError(400, "content_hash is only for %s items (the server checks the others itself)"
                           % ", ".join(CONTENT_COLLS))
        if ch is not None and (not isinstance(ch, str) or not HASH_RE.fullmatch(ch)):
            raise ApiError(400, "content_hash must be 64 lower-case hex characters")
        cur, h = _current(db, coll, iid)
        if version != cur:
            raise ApiError(409, "%s/%s changed on the server (version %d, not %d): sync and ask again"
                           % (coll, iid, cur, version), {"coll": coll, "id": iid, "current_version": cur})
        if not h and not ch:
            raise ApiError(400, "%s/%s: there is nothing to review (no such item, and no content_hash)" % (coll, iid))
        entry = {"coll": coll, "id": iid, "version": version, "hash": h}
        if ch:
            entry["content_hash"] = ch
        out.append(entry)
    return out


def _authored_by(db, items, mid):
    """Did this member make or ever change any of these items (at any time, before or after a request)? They can't
    approve their own work."""
    for it in items:
        r = db.execute("SELECT author, updated_by FROM items WHERE coll=? AND id=?", (it["coll"], it["id"])).fetchone()
        if r and mid in (r["author"], r["updated_by"]):
            return True
    return _edited_since(db, items, mid, 0)


def _edited_since(db, items, mid, since):
    """Did this member write any of these items since `since`? Read from their edit_session events (a session's time is
    its last write), so an edit that was later put back still counts."""
    keys = {(it["coll"], it["id"]) for it in items}
    for e in db.execute("SELECT data FROM events WHERE kind='edit_session' AND actor=? AND at>=?", (mid, since)):
        try:
            got = json.loads(e["data"] or "{}").get("items") or []
        except ValueError:
            continue
        if any((x.get("coll"), x.get("id")) in keys for x in got if isinstance(x, dict)):
            return True
        d = json.loads(e["data"] or "{}")
        if d.get("truncated") and any(c in (d.get("colls") or {}) for c, _ in keys):
            return True  # a session too big to list every item: assume it touched them
    return False


def _reviewers(db, actor, raw, items=()):
    if not isinstance(raw, list) or not raw:
        raise ApiError(400, "pick at least one reviewer (reviewers: [member id])")
    if len(raw) > MAX_REVIEWERS:
        raise ApiError(400, "too many reviewers (max %d)" % MAX_REVIEWERS)
    out = []
    for v in raw:
        mid = need_int(v, "reviewers", 1)
        if mid == actor["id"]:
            raise ApiError(400, "you can't review your own request")
        m = db.execute("SELECT * FROM members WHERE id=?", (mid,)).fetchone()
        if not m or not m["active"]:
            raise ApiError(400, "reviewers: unknown member %d" % mid)
        if not _eligible(m):
            raise ApiError(400, "%s can't approve (not an approver, admin or owner)" % m["name"])
        if _authored_by(db, items, mid):
            raise ApiError(400, "%s made or changed items in this review, so can't approve it: pick another approver"
                           % m["name"])
        if mid not in out:
            out.append(mid)
    return out


def _notify(store, review_id, status, members):
    store.emit("review", {"id": review_id, "status": status}, set(members))


def _summary_of(items):
    colls = {}
    for it in items:
        colls[it["coll"]] = colls.get(it["coll"], 0) + 1
    n = len(items)
    return "%d item%s in %s" % (n, "" if n == 1 else "s", ", ".join(list(colls)[:3]) + (" and more" if len(colls) > 3 else ""))


# ---------- reviews ----------

def request_review(store, actor, body):
    """POST /reviews: editor+ asks named approvers to approve items (or every item of one of their changesets)."""
    if actor["role"] == "viewer":
        raise ApiError(403, "viewers cannot ask for reviews")
    note = need_str(body, "note", MAX_NOTE, required=False, default="")
    title = need_str(body, "title", MAX_TITLE, required=False, default="")
    area = need_str(body, "area", 50, required=False, default="")
    now = store.now()
    with store.tx() as db:
        actor = store._fresh_actor(db, actor)
        if actor["role"] == "viewer":
            raise ApiError(403, "viewers cannot ask for reviews")
        cs = _changeset(body)
        if body.get("items") is None and body.get("from_changeset") is True:
            if not cs:
                raise ApiError(400, "from_changeset needs a changeset {id, name}")
            items = _items_of_changeset(db, actor, cs["id"])
            if not items:
                raise ApiError(400, "that changeset has no items on the server yet: sync first")
            if len(items) > MAX_ITEMS:
                raise ApiError(400, "too many items in one review (max %d)" % MAX_ITEMS)
        else:
            items = _requested_items(db, body.get("items"))
        reviewers = _reviewers(db, actor, body.get("reviewers"), items)
        areas = {area_of(i["coll"]) for i in items}
        area = area or (areas.pop() if len(areas) == 1 else "multi")
        title = title or (cs["name"] if cs and cs["name"] else _summary_of(items))
        cur = db.execute("INSERT INTO reviews(cs_id, cs_name, title, area, items, requested_by, reviewers, status, note,"
                         " at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         (cs["id"] if cs else None, cs["name"] if cs else None, title, area, json.dumps(items),
                          actor["id"], json.dumps(reviewers), "open", note, now, now))
        rid = cur.lastrowid
        seq = store._add_event(db, actor["id"], "review_requested", area, "review", str(rid),
                               "%s asked for a review: %s" % (actor["name"], title[:200]),
                               {"review": rid, "reviewers": reviewers, "count": len(items)})
        out = _review_out(db, db.execute("SELECT * FROM reviews WHERE id=?", (rid,)).fetchone())
    store.emit("event", {"seq": seq})
    _notify(store, rid, "open", reviewers + [actor["id"]])
    return out


def list_reviews(store, actor, query):
    """GET /reviews?mine=1|to_me=1|status=open,approved|current=1&before=<id> (current: approved with at least one item
    not stale and a decider who may still approve). Newest first, MAX_LIST per page; `more` + `next_before` page on."""
    def flag(k):
        v = query.get(k)
        return bool(v) and v[0] in ("1", "true", "yes")
    status = (query.get("status") or [""])[0]
    wanted = [s for s in status.split(",") if s] if status else []
    for s in wanted:
        if s not in STATUSES:
            raise ApiError(400, "status must be one of %s" % ", ".join(STATUSES))
    sql, args = "SELECT * FROM reviews", []
    where = []
    if wanted:
        where.append("status IN (%s)" % ",".join("?" * len(wanted)))
        args += wanted
    if flag("mine"):
        where.append("requested_by=?")
        args.append(actor["id"])
    before = (query.get("before") or [""])[0]
    if before:
        if not before.isdigit() or len(before) > 12:
            raise ApiError(400, "before must be a review id")
        where.append("id<?")
        args.append(int(before))
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC"
    out, more = [], False
    with store.lock:
        for r in store.db.execute(sql, args).fetchall():
            if len(out) >= MAX_LIST:
                more = True
                break
            if flag("to_me") and actor["id"] not in json.loads(r["reviewers"]):
                continue
            rv = _review_out(store.db, r)
            if flag("current") and (all(i["stale"] for i in rv["items"])
                                    or (rv["status"] == "approved" and not rv["decider_ok"])):
                continue
            out.append(rv)
    return {"reviews": out, "more": more, "next_before": out[-1]["id"] if more else None}


def get_review(store, actor, rid):
    rid = need_int(rid, "review", 1)
    with store.lock:
        r = store.db.execute("SELECT * FROM reviews WHERE id=?", (rid,)).fetchone()
        if not r:
            raise ApiError(404, "review not found")
        return _review_out(store.db, r)


def decide(store, actor, rid, body):
    """POST /reviews/<id>/decide {decision: approve|request_changes, note?}: a listed reviewer who may approve."""
    rid = need_int(rid, "review", 1)
    decision = need_str(body, "decision", 30)
    if decision not in ("approve", "request_changes"):
        raise ApiError(400, "decision must be approve or request_changes")
    note = need_str(body, "note", MAX_NOTE, required=False, default="")
    now = store.now()
    with store.tx() as db:
        actor = store._fresh_actor(db, actor)
        r = db.execute("SELECT * FROM reviews WHERE id=?", (rid,)).fetchone()
        if not r:
            raise ApiError(404, "review not found")
        if r["requested_by"] == actor["id"]:
            raise ApiError(403, "you can't decide your own review")
        if actor["id"] not in json.loads(r["reviewers"]):
            raise ApiError(403, "only the reviewers asked can decide this review")
        if not _eligible(actor):
            raise ApiError(403, "only approvers, admins and owners can decide reviews")
        if r["status"] != "open":
            raise ApiError(409, "this review is %s, not open" % r["status"])
        items = json.loads(r["items"])
        if decision == "approve":
            if _authored_by(db, items, actor["id"]):
                raise ApiError(403, "you made or changed items in this review: another approver must decide it")
            if _edited_since(db, items, actor["id"], r["at"]):
                raise ApiError(403, "you changed items in this review after it was asked for: another approver must decide it")
            confirm = body.get("confirm") or []
            if not isinstance(confirm, list):
                raise ApiError(400, "confirm must be a list of {coll, id, content_hash}")
            seen = {(c.get("coll"), str(c.get("id"))): c.get("content_hash") for c in confirm if isinstance(c, dict)}
            for it in items:
                # Every item: the approver's Studio names the hash of exactly what its review screen showed. It must be
                # what was asked about, and (for items the server can see) still what the server holds.
                want = it.get("content_hash") or it["hash"]
                stale = not it.get("content_hash") and _current(db, it["coll"], it["id"])[1] != it["hash"]
                if stale or seen.get((it["coll"], it["id"])) != want:
                    raise ApiError(409, "%s/%s is not what was asked about any more (changed in Studio or on the"
                                        " platform, or your copy is not in sync): it has to be asked about again"
                                   % (it["coll"], it["id"]), {"coll": it["coll"], "id": it["id"]})
        status = "approved" if decision == "approve" else "changes_requested"
        db.execute("UPDATE reviews SET status=?, decision_note=?, decided_by=?, decided_at=?, updated_at=? WHERE id=?",
                   (status, note, actor["id"], now, now, rid))
        verb = "approved" if status == "approved" else "asked for changes to"
        seq = store._add_event(db, actor["id"], "review_decided", r["area"], "review", str(rid),
                               "%s %s %s" % (actor["name"], verb, (r["title"] or "a review")[:200]),
                               {"review": rid, "status": status, "note": note})
        out = _review_out(db, db.execute("SELECT * FROM reviews WHERE id=?", (rid,)).fetchone())
    store.emit("event", {"seq": seq})
    _notify(store, rid, status, json.loads(r["reviewers"]) + [r["requested_by"]])
    return out


def withdraw(store, actor, rid):
    """POST /reviews/<id>/withdraw: the requester (or an admin / owner) takes it back."""
    rid = need_int(rid, "review", 1)
    now = store.now()
    with store.tx() as db:
        actor = store._fresh_actor(db, actor)
        r = db.execute("SELECT * FROM reviews WHERE id=?", (rid,)).fetchone()
        if not r:
            raise ApiError(404, "review not found")
        if r["requested_by"] != actor["id"] and actor["role"] not in MANAGERS:
            raise ApiError(403, "only the requester or an admin or owner can withdraw a review")
        if r["status"] == "withdrawn":
            raise ApiError(409, "already withdrawn")
        db.execute("UPDATE reviews SET status='withdrawn', updated_at=? WHERE id=?", (now, rid))
        out = _review_out(db, db.execute("SELECT * FROM reviews WHERE id=?", (rid,)).fetchone())
    _notify(store, rid, "withdrawn", json.loads(r["reviewers"]) + [r["requested_by"]])
    return out


# ---------- policy ----------

def get_policy(store):
    with store.lock:
        rows = {r["area"]: bool(r["require_approval"]) for r in store.db.execute("SELECT * FROM review_policy")}
    return {"require_approval": {a: rows.get(a, False) for a in AREAS}}


def put_policy(store, actor, body):
    req = body.get("require_approval")
    if not isinstance(req, dict) or not req:
        raise ApiError(400, "require_approval must be an object {area: true|false}")
    for a, v in req.items():
        if a not in AREAS:
            raise ApiError(400, "area must be one of %s" % ", ".join(AREAS))
        need_bool(v, "require_approval." + a)
    now = store.now()
    with store.tx() as db:
        actor = store._fresh_actor(db, actor)
        if actor["role"] not in MANAGERS:
            raise ApiError(403, "only an admin or owner can change the approval policy")
        for a, v in req.items():
            db.execute("INSERT INTO review_policy(area, require_approval, at, by) VALUES(?,?,?,?) ON CONFLICT(area)"
                       " DO UPDATE SET require_approval=excluded.require_approval, at=excluded.at, by=excluded.by",
                       (a, 1 if v else 0, now, actor["id"]))
    out = get_policy(store)
    store.emit("policy", out)
    return out


# ---------- owners ----------

def get_owners(store):
    with store.lock:
        return {"owners": {r["scope"]: r["member_id"] for r in store.db.execute("SELECT * FROM owners ORDER BY scope")}}


def put_owners(store, actor, body):
    """PUT /owners {owners: {scope: member id | null}}: merged into the map (null removes a scope)."""
    req = body.get("owners")
    if not isinstance(req, dict) or not req:
        raise ApiError(400, "owners must be an object {scope: member id or null}")
    if len(req) > MAX_OWNERS:
        raise ApiError(400, "too many owners in one request (max %d)" % MAX_OWNERS)
    now = store.now()
    with store.tx() as db:
        actor = store._fresh_actor(db, actor)
        if actor["role"] not in MANAGERS:
            raise ApiError(403, "only an admin or owner can set owners")
        for scope, mid in req.items():
            if not isinstance(scope, str) or not SCOPE_RE.fullmatch(scope):
                raise ApiError(400, "owner scopes are 1-200 characters, start with a-z or _, no control characters")
            if mid is None:
                db.execute("DELETE FROM owners WHERE scope=?", (scope,))
                continue
            mid = need_int(mid, "owners." + scope[:40], 1)
            if not db.execute("SELECT 1 FROM members WHERE id=? AND active=1", (mid,)).fetchone():
                raise ApiError(400, "owners: unknown member %d" % mid)
            db.execute("INSERT INTO owners(scope, member_id, at, by) VALUES(?,?,?,?) ON CONFLICT(scope)"
                       " DO UPDATE SET member_id=excluded.member_id, at=excluded.at, by=excluded.by",
                       (scope, mid, now, actor["id"]))
        if db.execute("SELECT COUNT(*) n FROM owners").fetchone()["n"] > MAX_OWNERS:
            raise ApiError(400, "a team can have at most %d owner scopes" % MAX_OWNERS)
    out = get_owners(store)
    store.emit("owners", {})
    return out


# ---------- HTTP routes (appended to app.ROUTES) ----------

def r_reviews_get(h, query, body):
    ctx = h.authed(query)
    return list_reviews(h.team.store, ctx["member"], query)


def r_reviews_post(h, query, body):
    ctx = h.authed(query)
    return request_review(h.team.store, ctx["member"], body)


def r_review_get(h, query, body, rid):
    ctx = h.authed(query)
    return get_review(h.team.store, ctx["member"], int(rid))


def r_review_decide(h, query, body, rid):
    ctx = h.authed(query)
    return decide(h.team.store, ctx["member"], int(rid), body)


def r_review_withdraw(h, query, body, rid):
    ctx = h.authed(query)
    return withdraw(h.team.store, ctx["member"], int(rid))


def r_policy_get(h, query, body):
    h.authed(query)
    return get_policy(h.team.store)


def r_policy_put(h, query, body):
    ctx = h.authed(query)
    return put_policy(h.team.store, ctx["member"], body)


def r_owners_get(h, query, body):
    h.authed(query)
    return get_owners(h.team.store)


def r_owners_put(h, query, body):
    ctx = h.authed(query)
    return put_owners(h.team.store, ctx["member"], body)


ROUTES = [
    ("GET", r"/api/v1/reviews", r_reviews_get),
    ("POST", r"/api/v1/reviews", r_reviews_post),
    ("GET", r"/api/v1/reviews/(\d{1,12})", r_review_get),
    ("POST", r"/api/v1/reviews/(\d{1,12})/decide", r_review_decide),
    ("POST", r"/api/v1/reviews/(\d{1,12})/withdraw", r_review_withdraw),
    ("GET", r"/api/v1/policy", r_policy_get),
    ("PUT", r"/api/v1/policy", r_policy_put),
    ("GET", r"/api/v1/owners", r_owners_get),
    ("PUT", r"/api/v1/owners", r_owners_put),
]
