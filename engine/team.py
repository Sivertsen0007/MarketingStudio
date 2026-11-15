"""Team sync: join a team server and keep every area's drafts in sync with it, item by item (teamserver/PROTOCOL.md).

Where things live:
  config.json   TEAM_SERVER_URL (appconfig)
  Keychain      TEAM_DEVICE_TOKEN (appconfig hidden secret; secrets.json in tests)
  <data>/team/  state.json      member, team, device, cursor, last sync, auto-sync, remote changes waiting
                synced.json     per item ("coll\\tid"): the server version last seen (v) and the data as last synced (base)
                conflicts.json  per item: the server copy and the fields that changed differently on both sides
                changeset.json  my changesets {active, list}; draftstore tags every outbox entry with the active id

Sync (sync()): push, then pull, then push again when the pull merged something.
  push  every draftstore outbox entry becomes an op: put (base_version = v, 0 for a new item) or delete. Entries that
        only differ from base in derived fields (Ads "problems" / "warnings", which Validate rewrites) are trimmed
        without sending. ok -> v = the new version, base = what was sent, the entry is trimmed (if the item has not
        changed again since). conflict -> the server copy goes through the three-way merge below and the op is retried
        with the new base_version.
  pull  GET /items?since=cursor until more is false. An item with no pending local edit is written with
        draftstore.apply_remote (only if the local copy is still what was checked, so a pending edit is never lost).
        An item with a pending local edit is merged.
  merge three-way on the item's data keys (dicts one level down too, e.g. an email draft's "fields"): base = last
        synced copy, theirs = server, mine = local. Keys changed on one side only are taken; the same key changed
        differently on both sides is a conflict (recorded, neither side overwritten, the local item stays as mine and
        is not pushed until resolved). Remote delete vs local edit (and local delete vs remote edit) is a conflict on
        the whole item (field "*"). Bookkeeping keys (at, ts, by, edited_by, edited_at) never conflict: mine wins.
        Derived keys never conflict either.
resolve(): keep mine / take theirs / a custom value per field; once every field of an item has an answer the result
is written locally and pushed.

Live updates: a background thread reads /api/v1/stream (SSE). An `items` event sets "remote changes waiting: N", or
syncs when Auto-sync is on (off by default; when on, local writes also sync, debounced 3 s). 401 = this device was
removed from the team: the listener stops and status says so. 429 / 503 / network errors: reconnect with backoff.
Every network call has a timeout; offline is fine (the outbox waits).
"""
import contextlib
import copy
import datetime as dt
import fcntl
import json
import os
import shutil
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import appconfig
import draftstore

PROTOCOL = 1
TIMEOUT = 15
STREAM_TIMEOUT = 60  # the server pings every 20 s
DEBOUNCE = 3.0
DERIVED = ("problems", "warnings")  # Ads items: rewritten by Validate, never a change of their own
SOFT = ("at", "ts", "by", "edited_by", "edited_at")  # bookkeeping: on both sides -> mine, never a conflict
DEFAULT_CS = "Untitled changes"
ROOT = None  # tests may point this elsewhere; default core.STUDIO_DIR (where every area keeps its drafts)
_MISSING = object()


class TeamError(Exception):
    def __init__(self, message, status=0):
        super().__init__(message)
        self.status = status


class Offline(TeamError):
    pass


# ---------- files ----------

def _root():
    if ROOT is not None:
        return Path(ROOT)
    import core  # noqa: PLC0415 - Studio's data folder
    return Path(core.STUDIO_DIR)


def _dir():
    d = _root() / draftstore.TEAM_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load(name, default):
    try:
        v = json.loads((_dir() / name).read_text())
        return v if isinstance(v, type(default)) else copy.deepcopy(default)
    except (OSError, ValueError):
        return copy.deepcopy(default)


def _save(name, obj):
    d = _dir()
    fd, tmp = tempfile.mkstemp(dir=d, prefix=name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, ensure_ascii=False)
        os.replace(tmp, d / name)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


_lock = threading.RLock()  # state files within this process


def _state():
    return _load("state.json", {})


def _update_state(**kw):
    with _lock:
        st = _state()
        st.update(kw)
        _save("state.json", st)
        return st


@contextlib.contextmanager
def _flock():
    """One sync at a time across processes (the app and the MCP server)."""
    with open(_dir() / ".sync.lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _key(coll, iid):
    return f"{coll}\t{iid}"


# ---------- config ----------

def _server():
    return (appconfig.get("TEAM_SERVER_URL") or "").rstrip("/")


def _token():
    return appconfig.get("TEAM_DEVICE_TOKEN") or ""


def joined():
    return bool(_server() and _token() and _state().get("member"))


# ---------- HTTP ----------

def _http(method, path, body=None, server=None, token=None, timeout=TIMEOUT):
    server = (server or _server()).rstrip("/")
    if not server:
        raise TeamError("Not in a team")
    headers = {"Accept": "application/json", "User-Agent": "MarketingStudio-team/1"}
    data = None
    if body is not None:
        data = json.dumps(body, allow_nan=False).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(server + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read() or b"{}").get("error") or e.reason
        except (ValueError, AttributeError):
            msg = e.reason
        if e.code == 401 and token:
            _mark_removed()
            raise TeamError("This Mac was removed from the team", 401) from None
        if e.code in (429, 503):
            raise TeamError(f"The team server is busy ({e.code}): try again in a moment", e.code) from None
        raise TeamError(str(msg), e.code) from None
    except (urllib.error.URLError, OSError, ValueError) as e:  # ValueError: http.client InvalidURL etc.
        raise Offline(f"Can't reach the team server: {getattr(e, 'reason', e)}") from None
    try:
        return json.loads(raw or b"{}")
    except ValueError:
        raise TeamError("The team server sent something that is not JSON") from None


def _api(method, path, body=None, timeout=TIMEOUT):
    if _state().get("removed"):
        raise TeamError("This Mac was removed from the team", 401)
    return _http(method, path, body, token=_token(), timeout=timeout)


def _mark_removed():
    with _lock:
        if _state().get("member"):
            _update_state(removed=True, last_error="This Mac was removed from the team. Leave the team to clear it.")
    _LISTENER["stop"].set()


# ---------- join / leave ----------

def parse_link(link):
    """marketingstudio://join?server=..&invite=.. or https://host/join#<invite> -> (server, invite). A bare server
    URL -> (server, "")."""
    link = (link or "").strip()
    u = urllib.parse.urlparse(link)
    if u.scheme == "marketingstudio":
        q = urllib.parse.parse_qs(u.query)
        return (q.get("server") or [""])[0].rstrip("/"), (q.get("invite") or [""])[0]
    if u.scheme in ("http", "https") and u.fragment and u.path.rstrip("/").endswith("/join"):
        base = u.path.rstrip("/")[:-len("/join")]
        return f"{u.scheme}://{u.netloc}{base}".rstrip("/"), u.fragment
    return link.rstrip("/"), ""


def join(link=None, server=None, invite=None, device_name=None):
    """Join a team with an invite. -> status()."""
    if joined() and not _state().get("removed"):
        raise ValueError("This Mac is already in a team. Leave it first.")
    s2, i2 = parse_link(link) if link else ("", "")
    server = (server or s2 or "").strip().rstrip("/")
    invite = (invite or i2 or "").strip()
    if not server or urllib.parse.urlparse(server).scheme not in ("http", "https"):
        raise ValueError("Paste the invite link, or the team server's address (https://...) and the invite code.")
    if not invite:
        raise ValueError("The invite code is missing: paste the whole invite link.")
    h = _http("GET", "/api/v1/health", server=server)
    if h.get("protocol") != PROTOCOL:
        raise TeamError(f"This team server speaks protocol {h.get('protocol')}; this Studio speaks {PROTOCOL}. Update Studio or the server.")
    name = device_name or f"{socket.gethostname().split('.')[0]} (Marketing Studio)"
    r = _http("POST", "/api/v1/join", {"invite": invite, "device_name": name[:100]}, server=server)
    token = r.get("device_token")
    if not token:
        raise TeamError("The team server did not send a device token")
    me = _http("GET", "/api/v1/me", server=server, token=token)
    with _lock:
        stop_listener()
        with contextlib.suppress(OSError):
            shutil.rmtree(_dir())
        appconfig.set_secret("TEAM_DEVICE_TOKEN", token)
        appconfig.set_many({"TEAM_SERVER_URL": server})
        _save("state.json", {"member": r.get("member") or me.get("member"), "team": r.get("team") or h.get("team") or "",
                             "device": me.get("device") or {}, "cursor": 0, "auto_sync": False, "joined_at": _now(),
                             "remote_waiting": 0})
        _changesets()  # the default "Untitled changes"
    with contextlib.suppress(TeamError):
        _refresh_members()
    if _LISTENER["wanted"]:
        start_listener()
    return status()


def leave():
    """Revoke this device, forget the team (config, token, sync state). Local drafts stay."""
    st = _state()
    dev = (st.get("device") or {}).get("id")
    if dev is not None and _server() and _token() and not st.get("removed"):
        with contextlib.suppress(TeamError):
            _http("DELETE", f"/api/v1/devices/{int(dev)}", token=_token(), timeout=8)
    stop_listener()
    with _lock:
        appconfig.set_many({"TEAM_SERVER_URL": None})
        appconfig.set_secret("TEAM_DEVICE_TOKEN", None)
        with contextlib.suppress(OSError):
            shutil.rmtree(_root() / draftstore.TEAM_DIR)
    return status()


# ---------- members ----------

def _refresh_members():
    r = _api("GET", "/api/v1/members")
    m = {str(x["id"]): {"name": x.get("name") or x.get("email") or f"member {x['id']}", "colour": x.get("colour") or "",
                        "role": x.get("role") or "", "email": x.get("email") or ""} for x in r.get("members") or []}
    _update_state(members=m, members_at=_now())
    return m


def members():
    return _state().get("members") or {}


def _who(mid, ctx=None):
    if mid is None or mid == "":
        return ""
    m = members().get(str(mid))
    if not m and ctx is not None and not ctx.get("members_refreshed"):
        ctx["members_refreshed"] = True
        with contextlib.suppress(TeamError):
            m = _refresh_members().get(str(mid))
    return m["name"] if m else f"member {mid}"


# ---------- changesets ----------

def _changesets():
    with _lock:
        c = _load("changeset.json", {})
        if not c.get("active") or not c.get("list"):
            me = (_state().get("member") or {}).get("id", "x")
            cs = {"id": f"{me}-{uuid.uuid4().hex[:8]}", "name": DEFAULT_CS, "created": _now()}
            c = {"active": {"id": cs["id"], "name": cs["name"]}, "list": (c.get("list") or []) + [cs]}
            _save("changeset.json", c)
        return c


def changeset_get():
    c = _changesets()
    return {"active": c["active"], "list": c["list"]}


def changeset_set(cs_id=None, name=None):
    """Switch to one of my changesets (cs_id), or start a new one called name. New writes are tagged with it."""
    with _lock:
        c = _changesets()
        if cs_id:
            cs = next((x for x in c["list"] if x["id"] == cs_id), None)
            if not cs:
                raise ValueError("No such changeset")
        else:
            name = (name or "").strip()[:200]
            if not name:
                raise ValueError("Give the changeset a name")
            me = (_state().get("member") or {}).get("id", "x")
            cs = {"id": f"{me}-{uuid.uuid4().hex[:8]}", "name": name, "created": _now()}
            c["list"].append(cs)
        c["active"] = {"id": cs["id"], "name": cs["name"]}
        _save("changeset.json", c)
        return changeset_get()


def changeset_rename(cs_id, name):
    name = (name or "").strip()[:200]
    if not name:
        raise ValueError("Give the changeset a name")
    with _lock:
        c = _changesets()
        cs = next((x for x in c["list"] if x["id"] == (cs_id or c["active"]["id"])), None)
        if not cs:
            raise ValueError("No such changeset")
        cs["name"] = name
        if c["active"]["id"] == cs["id"]:
            c["active"]["name"] = name
        _save("changeset.json", c)
        return changeset_get()


def _cs_for(cs_id):
    c = _changesets()
    cs = next((x for x in c["list"] if x["id"] == cs_id), None) if cs_id else None
    cs = cs or c["active"]
    return {"id": str(cs["id"]), "name": cs.get("name") or ""}


# ---------- merging ----------

def _strip_derived(d):
    return {k: v for k, v in d.items() if k not in DERIVED} if isinstance(d, dict) else d


def same(a, b):
    """Equal apart from derived fields."""
    return _strip_derived(a) == _strip_derived(b)


def _eq(a, b):
    if a is _MISSING or b is _MISSING:
        return a is b
    return a == b


def merge3(base, theirs, mine, path=()):
    """Three-way merge of dicts. -> (merged, [(path, base, theirs, mine)]) with _MISSING for absent keys.
    On a conflict the merged value is mine."""
    merged, conflicts = {}, []
    keys = list(mine) + [k for k in theirs if k not in mine] + [k for k in base if k not in mine and k not in theirs]
    for k in keys:
        b, t, m = base.get(k, _MISSING), theirs.get(k, _MISSING), mine.get(k, _MISSING)
        if _eq(t, m):
            v = m
        elif not path and k in DERIVED:
            v = t if _eq(m, b) else m
        elif isinstance(b, dict) and isinstance(t, dict) and isinstance(m, dict):
            v, sub = merge3(b, t, m, path + (k,))
            conflicts += sub
        elif _eq(m, b):
            v = t
        elif _eq(t, b):
            v = m
        elif k in SOFT:
            v = m
        else:
            conflicts.append((path + (k,), b, t, m))
            v = m
        if v is not _MISSING:
            merged[k] = v
    return merged, conflicts


def _set_path(d, path, value):
    for k in path[:-1]:
        d = d.setdefault(k, {})
    if value is _MISSING:
        d.pop(path[-1], None)
    else:
        d[path[-1]] = value


def _get_path(d, path):
    for k in path:
        if not isinstance(d, dict) or k not in d:
            return _MISSING
        d = d[k]
    return d


def _plain(v):
    return None if v is _MISSING else v


# ---------- sync ----------

class _Changed(Exception):
    pass


_tls = threading.local()


def _local_write(coll, iid, data, expect, by="team sync"):
    """Write (or with None delete) one local item if it is still what was checked (hash expect). -> True if done."""
    try:
        with draftstore.tx(coll, _root(), by=by) as t:
            cur = t.get(iid)
            if draftstore.item_hash(cur["data"] if cur else None) != expect:
                raise _Changed
            if data is None:
                t.delete(iid, by)
            else:
                t.put(iid, copy.deepcopy(data), by)
        return True
    except _Changed:
        return False


def _snapshot():
    """peek() for many items: reads each collection once. -> fn(coll, id) -> (data or None, hash)."""
    cache, root = {}, _root()

    def get(coll, iid):
        if coll not in cache:
            cache[coll] = {r["id"]: (r["data"], draftstore.item_hash(r["data"])) for r in draftstore.items(coll, root)}
        return cache[coll].get(str(iid), (None, ""))
    return get


def _local_changed(s, mine):
    """Does the local copy differ from what was last synced (ignoring derived fields)?"""
    if not s:
        return mine is not None
    if s.get("deleted"):
        return mine is not None
    return mine is None or not same(s.get("base"), mine)


def _record_conflict(ctx, key, coll, iid, S, item, base, fields):
    ctx["conflicts"][key] = {"coll": coll, "id": iid, "theirs_item": item, "theirs_version": int(S["version"]),
                             "base": base, "who": item.get("updated_by") or "", "at": _now(),
                             "fields": [{"field": ".".join(p) if p else "*", "path": list(p), "base": _plain(b),
                                         "theirs": _plain(t), "mine": _plain(m), "base_missing": b is _MISSING,
                                         "theirs_missing": t is _MISSING, "mine_missing": m is _MISSING}
                                        for p, b, t, m in fields]}
    ctx["tally"]["conflicts"] += len(fields)


def _incoming(ctx, S):
    """One item from the server (pull, or the server copy in a push conflict)."""
    coll, iid = str(S["coll"]), str(S["id"])
    try:
        draftstore.layout(coll)
    except ValueError:
        return  # a collection this Studio does not know (a newer version's area)
    key, root, synced = _key(coll, iid), _root(), ctx["synced"]
    s = synced.get(key)
    if s and int(s.get("v", 0)) >= int(S["version"]):
        return  # what we sent, or older
    theirs = None if S.get("deleted") else (S.get("data") or {})
    item = {"id": iid, "data": theirs, "version": int(S["version"]), "author": _who(S.get("author"), ctx),
            "updated_by": _who(S.get("updated_by"), ctx), "updated_at": S.get("updated_at") or "",
            "deleted": bool(S.get("deleted")), "changeset": S.get("changeset")}
    new_s = {"v": int(S["version"]), "base": theirs, "deleted": theirs is None}
    mine, h = draftstore.peek(coll, iid, root)
    if key not in ctx["conflicts"] and not _local_changed(s, mine):
        if draftstore.apply_remote(coll, item, root, expect=h):
            synced[key] = new_s
            ctx["tally"]["received"] += 1
            return
        mine, h = draftstore.peek(coll, iid, root)  # edited just now: merge instead
    ctx["conflicts"].pop(key, None)
    base = s.get("base") if s and not s.get("deleted") else None
    if theirs is None and mine is None:
        draftstore.apply_remote(coll, item, root, expect=h)
        synced[key] = new_s
        return
    if theirs is None or mine is None:
        if mine is None and base is not None and same(base, theirs):
            synced[key] = new_s  # they only touched derived fields: my delete stands and is sent next
            return
        _record_conflict(ctx, key, coll, iid, S, item, base, [((), base if base is not None else _MISSING,
                                                                _MISSING if theirs is None else theirs,
                                                                _MISSING if mine is None else mine)])
        return
    merged, cf = merge3(base or {}, theirs, mine)
    if cf:
        _record_conflict(ctx, key, coll, iid, S, item, base, cf)
        return
    if merged == theirs:
        if not draftstore.apply_remote(coll, item, root, expect=h):
            return  # changed again under us: next sync
    elif merged != mine and not _local_write(coll, iid, merged, h):
        return
    synced[key] = new_s
    ctx["tally"]["received"] += 1
    if not same(merged, theirs):
        ctx["tally"]["merged"] += 1
        ctx["repush"] = True


def _push(ctx):
    root, synced = _root(), ctx["synced"]
    for _ in range(4):
        ops, sent, acks, peek = [], [], {}, _snapshot()
        for e in draftstore.outbox(root):
            coll, iid = e["coll"], str(e["id"])
            key = _key(coll, iid)
            if key in ctx["conflicts"]:
                continue
            try:
                data, h = peek(coll, iid)
            except ValueError:
                continue
            s = synced.get(key)
            if data is None:
                if not s or s.get("deleted"):
                    acks.setdefault(coll, []).append((iid, ""))  # never reached the server
                    continue
                op = {"op": "delete", "coll": coll, "id": iid, "base_version": int(s["v"])}
            else:
                if s and not s.get("deleted") and same(s.get("base"), data):
                    acks.setdefault(coll, []).append((iid, h))  # only derived fields changed
                    continue
                op = {"op": "put", "coll": coll, "id": iid, "base_version": int(s["v"]) if s else 0, "data": data}
            op["changeset"] = _cs_for(e.get("cs"))
            ops.append(op)
            sent.append((coll, iid, data, h))
        retry = False
        for n in range(0, len(ops), 200):
            chunk, meta = ops[n:n + 200], sent[n:n + 200]
            r = _api("POST", "/api/v1/items", {"ops": chunk})
            for res, (coll, iid, data, h) in zip(r.get("results") or [], meta):
                key = _key(coll, iid)
                if res.get("ok"):
                    synced[key] = {"v": int(res["version"]), "base": data, "deleted": data is None}
                    acks.setdefault(coll, []).append((iid, h))
                    ctx["tally"]["sent"] += 1
                elif res.get("conflict"):
                    S = res.get("server")
                    if S is None:  # the server does not have it (any more)
                        if data is None:
                            synced.pop(key, None)
                            acks.setdefault(coll, []).append((iid, ""))
                        else:
                            synced.pop(key, None)
                            retry = True
                    else:
                        _incoming(ctx, S)
                        retry = True
                else:  # e.g. delete of an item the server never had
                    synced.pop(key, None)
                    if data is None:
                        acks.setdefault(coll, []).append((iid, ""))
                    else:
                        ctx["errors"].append(f"{coll}/{iid}: {res.get('error') or 'refused'}")
            _save("synced.json", synced)
        for coll, a in acks.items():
            draftstore.outbox_ack(coll, a, root)
        _save("conflicts.json", ctx["conflicts"])
        if not retry:
            return


def _pull(ctx):
    cursor = int(_state().get("cursor") or 0)
    for _ in range(10000):
        r = _api("GET", f"/api/v1/items?since={cursor}&limit=1000")
        for S in r.get("items") or []:
            _incoming(ctx, S)
        cursor = int(r.get("cursor") or cursor)
        _save("synced.json", ctx["synced"])
        _save("conflicts.json", ctx["conflicts"])
        _update_state(cursor=cursor)
        if not r.get("more"):
            break


def _ctx():
    return {"synced": _load("synced.json", {}), "conflicts": _load("conflicts.json", {}), "errors": [], "repush": False,
            "tally": {"sent": 0, "received": 0, "merged": 0, "conflicts": 0}}


_sync_lock = threading.Lock()


def _summary(t):
    parts = [f"Sent {t['sent']}", f"received {t['received']}"]
    if t.get("merged"):
        parts.append(f"merged {t['merged']}")
    if t.get("conflicts"):
        parts.append(f"{t['conflicts']} conflict" + ("s" if t["conflicts"] != 1 else ""))
    return ", ".join(parts)


def sync():
    """Push my changes, pull everyone else's. -> {ok, sent, received, merged, conflicts, message, ...}."""
    if not joined():
        raise ValueError("Join a team first (Setup > Team)")
    if _state().get("removed"):
        return {"ok": False, "removed": True, "message": "This Mac was removed from the team", **status()}
    _tls.syncing = True
    try:
        with _sync_lock, _flock():
            ctx = _ctx()
            out = {"ok": True}
            try:
                _push(ctx)
                _pull(ctx)
                if ctx["repush"]:
                    _push(ctx)
                _update_state(last_sync=_now(), last_error="", remote_waiting=0, online=True)
            except Offline as e:
                out = {"ok": False, "offline": True, "error": str(e)}
                _update_state(last_error=str(e) + ". Your changes wait in the outbox.", online=False)
            except TeamError as e:
                out = {"ok": False, "error": str(e), "removed": e.status == 401}
                _update_state(last_error=str(e))
            finally:
                _save("synced.json", ctx["synced"])
                _save("conflicts.json", ctx["conflicts"])
            t = ctx["tally"]
            t["conflicts"] = sum(len(c["fields"]) for c in ctx["conflicts"].values()) if t["conflicts"] else 0
            msg = _summary(t) if out["ok"] else out["error"]
            if ctx["errors"]:
                msg += f" ({len(ctx['errors'])} refused: {ctx['errors'][0]})"
            out.update(t, message=msg, errors=ctx["errors"])
            _update_state(last_result={**t, "message": msg, "at": _now()})
    finally:
        _tls.syncing = False
    return {**out, "status": status()}


# ---------- conflicts ----------

def conflicts():
    """Every open conflict, one row per field."""
    rows = []
    for c in _load("conflicts.json", {}).values():
        for f in c["fields"]:
            rows.append({"coll": c["coll"], "id": c["id"], "field": f["field"], "base": f["base"], "theirs": f["theirs"],
                         "mine": f["mine"], "base_missing": f["base_missing"], "theirs_missing": f["theirs_missing"],
                         "mine_missing": f["mine_missing"], "who": c["who"], "at": c["at"], "choice": f.get("choice"),
                         "label": _label(c)})
    return {"conflicts": rows, "count": len(rows)}


def _label(c):
    """A short name for the item: its summary / title / subject where it has one."""
    for d in (c.get("theirs_item") or {}).get("data") or {}, c.get("base") or {}:
        if isinstance(d, dict):
            for k in ("summary", "title", "name", "subject"):
                if isinstance(d.get(k), str) and d[k].strip():
                    return d[k].strip()[:120]
            f = d.get("fields")
            if isinstance(f, dict) and isinstance(f.get("subject"), str):
                return f["subject"][:120]
    return f"{c['coll']} {c['id']}"


def resolve(coll, iid, field, choice, value=None):
    """Answer one conflicting field: choice "mine", "theirs" or "custom" (value). When every field of the item has an
    answer the result is written locally and pushed. -> {resolved, sync?, conflicts}."""
    if choice not in ("mine", "theirs", "custom"):
        raise ValueError("choice must be mine, theirs or custom")
    key, root = _key(coll, str(iid)), _root()
    with _sync_lock, _flock():
        ctx = _ctx()
        c = ctx["conflicts"].get(key)
        if not c:
            raise ValueError("No such conflict (already resolved?)")
        f = next((x for x in c["fields"] if x["field"] == field), None)
        if not f:
            raise ValueError("No such field in this conflict")
        if choice == "custom" and f["field"] == "*" and value is not None and not isinstance(value, dict):
            raise ValueError("A custom value for a whole item must be an object (or null to delete it)")
        f["choice"], f["value"] = choice, value
        done = all(x.get("choice") for x in c["fields"])
        if done:
            done = _finalise(ctx, key, c)
        _save("conflicts.json", ctx["conflicts"])
        _save("synced.json", ctx["synced"])
    out = {"resolved": done, "conflicts": conflicts()}
    if done:
        try:
            out["sync"] = sync()
        except (TeamError, ValueError) as e:
            out["sync"] = {"ok": False, "error": str(e)}
    return out


def _finalise(ctx, key, c):
    root, coll, iid = _root(), c["coll"], c["id"]
    item = c["theirs_item"]
    theirs = None if item.get("deleted") else item.get("data")
    mine, h = draftstore.peek(coll, iid, root)
    whole = next((x for x in c["fields"] if x["field"] == "*"), None)
    if whole:
        final = {"mine": mine, "theirs": theirs}.get(whole["choice"], whole.get("value"))
    else:
        if mine is None:  # deleted locally since: back to a whole-item question
            c["fields"] = [{"field": "*", "path": [], "base": c["base"], "theirs": theirs, "mine": None,
                            "base_missing": c["base"] is None, "theirs_missing": theirs is None, "mine_missing": True}]
            return False
        final, cf = merge3(c["base"] or {}, theirs, mine)
        answered = {tuple(x["path"]): x for x in c["fields"]}
        new = [p for p, *_ in cf if p not in answered]
        if new:  # edited further meanwhile: ask again about the new fields
            c["fields"] += [{"field": ".".join(p), "path": list(p), "base": _plain(b), "theirs": _plain(t), "mine": _plain(m),
                             "base_missing": b is _MISSING, "theirs_missing": t is _MISSING, "mine_missing": m is _MISSING}
                            for p, b, t, m in cf if p not in answered]
            return False
        for p, x in answered.items():
            if x["choice"] == "mine":
                v = _get_path(mine, p)
            elif x["choice"] == "theirs":
                v = _get_path(theirs, p)
            else:
                v = x.get("value")
            _set_path(final, p, v)
    ctx["synced"][key] = {"v": int(c["theirs_version"]), "base": theirs, "deleted": theirs is None}
    del ctx["conflicts"][key]
    if final == theirs:
        if not draftstore.apply_remote(coll, item, root, expect=h):
            ctx["conflicts"][key] = c
            return False
        return True
    if final != mine and not _local_write(coll, iid, final, h):
        ctx["conflicts"][key] = c
        return False
    draftstore.mark_pending(coll, iid, root)
    return True


# ---------- status and settings ----------

def pending_count():
    """Local changes waiting to be sent (derived-only edits do not count)."""
    synced, root, n, peek = _load("synced.json", {}), _root(), 0, _snapshot()
    for e in draftstore.outbox(root):
        try:
            data, _ = peek(e["coll"], e["id"])
        except ValueError:
            continue
        if _local_changed(synced.get(_key(e["coll"], str(e["id"]))), data):
            n += 1
    return n


def status():
    st = _state()
    server = _server()
    if not (server and _token() and st.get("member")):
        return {"joined": False, "removed": False, "server": "", "auto_sync": False}
    me = dict(st.get("member") or {})
    cf = _load("conflicts.json", {})
    try:
        pending = pending_count()
    except Exception:  # noqa: BLE001 - status must always answer
        pending = None
    return {"joined": True, "removed": bool(st.get("removed")), "server": server, "team": st.get("team") or "",
            "me": {k: me.get(k) for k in ("id", "name", "email", "role", "colour", "approver")},
            "device": st.get("device") or {}, "last_sync": st.get("last_sync"), "last_result": st.get("last_result"),
            "last_error": st.get("last_error") or "", "pending_out": pending,
            "remote_waiting": int(st.get("remote_waiting") or 0), "conflicts": sum(len(c["fields"]) for c in cf.values()),
            "auto_sync": bool(st.get("auto_sync")), "online": st.get("online"), "listening": _LISTENER["alive"],
            "changeset": _changesets()["active"], "members": members()}


def settings(auto_sync=None):
    if not joined():
        raise ValueError("Join a team first")
    if auto_sync is not None:
        _update_state(auto_sync=bool(auto_sync))
        if auto_sync:
            _schedule_sync(0.2)
    return status()


# ---------- live updates ----------

_LISTENER = {"thread": None, "stop": threading.Event(), "wanted": False, "alive": False, "timer": None}


def _bg_sync():
    try:
        if joined() and not _state().get("removed"):
            sync()
    except Exception:  # noqa: BLE001 - background: the status shows the error
        pass


def _schedule_sync(delay=DEBOUNCE):
    with _lock:
        t = _LISTENER.get("timer")
        if t:
            t.cancel()
        t = threading.Timer(delay, _bg_sync)
        t.daemon = True
        _LISTENER["timer"] = t
        t.start()


def _on_local_write(root):
    if getattr(_tls, "syncing", False):
        return
    try:
        if Path(root).resolve() != _root().resolve() or not joined() or not _state().get("auto_sync"):
            return
    except Exception:  # noqa: BLE001
        return
    _schedule_sync(DEBOUNCE)


def remote_waiting():
    """How many items changed on the server since my cursor that are not my own writes (one page)."""
    st = _state()
    r = _api("GET", f"/api/v1/items?since={int(st.get('cursor') or 0)}&limit=1000")
    synced = _load("synced.json", {})
    n = sum(1 for S in r.get("items") or [] if int((synced.get(_key(S["coll"], str(S["id"]))) or {}).get("v", 0)) < int(S["version"]))
    _update_state(remote_waiting=n)
    return n


def _on_event(kind, data):
    if kind == "items":
        try:
            cur = int(json.loads(data or "{}").get("cursor") or 0)
        except (ValueError, AttributeError):
            cur = 0
        if cur and cur <= int(_state().get("cursor") or 0):
            return
        if _state().get("auto_sync"):
            _schedule_sync(0.5)
        else:
            with contextlib.suppress(TeamError):
                remote_waiting()


def _listen(stop):
    backoff = 1
    _LISTENER["alive"] = True
    try:
        while not stop.is_set():
            if not joined() or _state().get("removed"):
                return
            wait = backoff
            try:
                req = urllib.request.Request(_server() + "/api/v1/stream", headers={
                    "Authorization": "Bearer " + _token(), "Accept": "text/event-stream", "User-Agent": "MarketingStudio-team/1"})
                with urllib.request.urlopen(req, timeout=STREAM_TIMEOUT) as r:
                    backoff = 1
                    _update_state(online=True)
                    with contextlib.suppress(TeamError):
                        remote_waiting()  # catch up on what happened while away
                    event, buf = None, []
                    for raw in r:
                        if stop.is_set():
                            return
                        line = raw.decode("utf-8", "replace").rstrip("\r\n")
                        if not line:
                            if buf or event:
                                _on_event(event or "message", "\n".join(buf))
                            event, buf = None, []
                        elif line.startswith(":"):
                            continue
                        else:
                            field, _, val = line.partition(":")
                            val = val[1:] if val.startswith(" ") else val
                            if field == "event":
                                event = val
                            elif field == "data":
                                buf.append(val)
                wait = 1  # the server closed the stream (restart, or this device revoked): reconnect soon
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    _mark_removed()
                    return
                wait = 30 if e.code in (429, 503) else backoff
            except Exception:  # noqa: BLE001 - offline, timeouts: retry with backoff
                _update_state(online=False)
                wait = backoff
            backoff = min(backoff * 2, 60)
            stop.wait(wait)
    finally:
        _LISTENER["alive"] = False


def start_listener():
    """Start the live-update thread (the app calls this at start; no-op when not in a team)."""
    _LISTENER["wanted"] = True
    if not getattr(start_listener, "_hooked", False):
        draftstore.on_local_write(_on_local_write)
        start_listener._hooked = True
    t = _LISTENER["thread"]
    if t and t.is_alive():
        return
    if not joined() or _state().get("removed"):
        return
    _LISTENER["stop"] = stop = threading.Event()
    t = threading.Thread(target=_listen, args=(stop,), name="team-stream", daemon=True)
    _LISTENER["thread"] = t
    t.start()


def stop_listener():
    _LISTENER["stop"].set()
    tm = _LISTENER.get("timer")
    if tm:
        tm.cancel()
