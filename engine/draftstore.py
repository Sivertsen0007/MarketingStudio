"""Draft store: every area's drafts as collections of items, ready to sync with a team server item by item.

A collection (`coll`) is a named set of items. Each item is {id, data, version, author, updated_by, updated_at, deleted}:
data is the item's JSON (a dict) exactly as the area keeps it, version is a local counter per item, author is who made
it (you, claude, ...), updated_by the last writer. The collections, and the file that holds each:

  ads:<acct>          draft_<acct>.json       "items" list, id = the item's 8-hex id (Google account ids and meta_<id>)
  email               email_draft.json        "drafts" dict, id = the platform campaign id (as text)
  seo:changes         seo_draft.json          "changes" list, id = the change's 10-hex id
  seo:new_posts       seo_draft.json          "new_posts" list, id = "n" + 9 hex
  seo:map_nodes       seo_topical_map.json    "nodes" dict, id = "n_" + 8 hex
  seo:map_site        seo_topical_map.json    "site" dict, one item, id = "site"
  seo:map_proposals   seo_topical_map.json    "proposals" list, id = "p_" + 8 hex
  social              social_draft.json       "posts" list, id = the post's 10-hex id (the publish queue is not a draft)

Storage: the store keeps each area's own file (same name, same folder, same JSON shape) and adds one block to it,
"_draftstore": {format, colls: {coll: {version, items: {id: meta}}}, outbox}. So every reader of the old shape keeps
working, and an older Studio that rewrites the file loses nothing: on the next write the store notices what changed
(each item's meta holds a short hash of its data) and records it. A file without the block is migrated on its first
write: a byte copy is kept as <name>.pre-draftstore.json (once; never overwritten), and every item it holds is
recorded as a local put. Readers get the file without the block.

Writes: `with tx(coll, root) as t: t.get(id) / t.put(id, data, by) / t.delete(id, by) / t.items()`, or `doc(coll, root)`
for code that edits the whole file shape in place. Both hold the area's file lock (the same lock file names as before)
and save with an atomic replace. On commit the store diffs every collection in the file: each new or changed item
gets version + 1 and its meta, each removed one becomes a tombstone, and each of those is added to the outbox
({coll, id, op, at}; one pending entry per item, the newest). The file's own "version" (what the UI watches) bumps
by one per tx when bump is on, as the areas did before. apply_remote() writes an item from the team server without
touching the outbox.
"""
import contextlib
import copy
import datetime as dt
import fcntl
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

FORMAT = 1
META = "_draftstore"
BACKUP_SUFFIX = ".pre-draftstore.json"
MAX_TOMBSTONES = 500  # per collection; the oldest go first
SINGLE_ID = "site"


class Layout:
    """One area file: its name, lock file, the collections inside it ({coll: (key, kind)}) and its version key.
    kind: "list" (a list of dicts with "id"), "dict" ({id: data}) or "single" (one dict, id SINGLE_ID)."""

    def __init__(self, file, lock, colls, version_key, default):
        self.file, self.lock, self.colls, self.version_key, self._default = file, lock, colls, version_key, default

    def default(self):
        return copy.deepcopy(self._default)


_MAP_EMPTY = {"site": {"central_entity": "", "source_context": "", "central_search_intent": "", "exclude": []},
              "nodes": {}, "proposals": [], "version": 0}
EMAIL = Layout("email_draft.json", ".email_draft.json.lock", {"email": ("drafts", "dict")}, "version", {"version": 0, "drafts": {}})
SEO = Layout("seo_draft.json", ".seo_draft.json.lock", {"seo:changes": ("changes", "list"), "seo:new_posts": ("new_posts", "list")},
             "version", {"version": 0, "changes": []})
SEO_MAP = Layout("seo_topical_map.json", ".seo_topical_map.json.lock",
                 {"seo:map_nodes": ("nodes", "dict"), "seo:map_site": ("site", "single"), "seo:map_proposals": ("proposals", "list")},
                 "version", _MAP_EMPTY)
SOCIAL = Layout("social_draft.json", ".social_draft.json.lock", {"social": ("posts", "list")}, None, {"posts": []})
FIXED = (EMAIL, SEO, SEO_MAP, SOCIAL)


def ads_layout(acct):
    return Layout(f"draft_{acct}.json", f".draft_{acct}.lock", {f"ads:{acct}": ("items", "list")}, "version", {"version": 0, "items": []})


def layout(coll):
    """The Layout that holds a collection (or a Layout passed through)."""
    if isinstance(coll, Layout):
        return coll
    if coll.startswith("ads:") and len(coll) > 4:
        return ads_layout(coll[4:])
    for lay in FIXED:
        if coll in lay.colls:
            return lay
    raise ValueError(f"unknown draft collection {coll!r}")


def _root(root):
    if root is None:
        import core  # noqa: PLC0415 - Studio's data folder, only when the caller gives none
        root = core.STUDIO_DIR
    p = Path(root)
    p.mkdir(parents=True, exist_ok=True)
    return p


def now():
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def _hash(data):
    return hashlib.sha1(json.dumps(data, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:16]


# ---------- reading and writing one file ----------

def _load(path, lay, default, salvage):
    """-> (doc, existed). A file that isn't JSON: salvage copies it aside (write) or the default is used (read)."""
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return (copy.deepcopy(default) if default is not None else lay.default()), False
    try:
        d = json.loads(raw)
        if not isinstance(d, dict):
            raise ValueError("not an object")
        return d, True
    except ValueError:
        if salvage:
            shutil.copy2(path, path.with_name(f"{path.stem}.corrupt-{dt.datetime.now():%Y%m%d-%H%M%S}.json"))
        return (copy.deepcopy(default) if default is not None else lay.default()), False


def _save(path, d):
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _entries(d, key, kind):
    """The items of one collection in the doc, in order: [(id, data)]."""
    v = d.get(key)
    if kind == "single":
        return [(SINGLE_ID, v)] if isinstance(v, dict) else []
    if kind == "dict":
        return [(str(k), x) for k, x in v.items()] if isinstance(v, dict) else []
    return [(str(x["id"]), x) for x in v if isinstance(x, dict) and x.get("id") is not None] if isinstance(v, list) else []


def _data_by(data):
    return (data.get("edited_by") or data.get("by")) if isinstance(data, dict) else None


def _reconcile(d, meta, lay, by=None, by_item=None, at=None, record=True, cs=None):
    """Bring meta in line with the doc: new / changed items get a put, missing ones a tombstone. -> outbox entries.
    cs: the active changeset id (team sync), kept on each outbox entry so the op can name it."""
    at, by_item, out = at or now(), by_item or {}, []
    colls = meta.setdefault("colls", {})
    for coll, (key, kind) in lay.colls.items():
        cm = colls.setdefault(coll, {"version": 0, "items": {}})
        items, changed, seen = cm.setdefault("items", {}), False, set()
        for iid, data in _entries(d, key, kind):
            seen.add(iid)
            h, m = _hash(data), items.get(iid)
            who = by_item.get(iid) or by or _data_by(data) or "you"
            if m is None or m.get("deleted"):
                items.pop(iid, None)  # re-insert so live items keep the doc's order after tombstones
                items[iid] = {"version": (m or {}).get("version", 0) + 1, "author": by_item.get(iid) or _data_by(data) or by or "you",
                              "updated_by": who, "updated_at": at, "h": h}
            elif m.get("h") != h:
                m.update(version=m.get("version", 0) + 1, updated_by=who, updated_at=at, h=h)
            else:
                continue
            changed = True
            out.append({"coll": coll, "id": iid, "op": "put", "at": at})
            if cs:
                out[-1]["cs"] = cs
        for iid, m in list(items.items()):
            if iid not in seen and not m.get("deleted"):
                m.update(version=m.get("version", 0) + 1, updated_by=by_item.get(iid) or by or "you", updated_at=at, deleted=True)
                m.pop("h", None)
                changed = True
                out.append({"coll": coll, "id": iid, "op": "delete", "at": at})
                if cs:
                    out[-1]["cs"] = cs
        dead = [k for k, m in items.items() if m.get("deleted")]
        for k in dead[:max(0, len(dead) - MAX_TOMBSTONES)]:
            del items[k]
        if changed:
            cm["version"] = cm.get("version", 0) + 1
    if record:
        _outbox_add(meta, out)
    return out


def _outbox_add(meta, entries):
    box = meta.setdefault("outbox", [])
    for e in entries:
        box[:] = [x for x in box if (x["coll"], x["id"]) != (e["coll"], e["id"])]
        box.append(e)


TEAM_DIR = "team"  # inside the data folder; engine/team.py keeps its sync state there
_listeners = []


def on_local_write(fn):
    """fn(root) runs after a commit that added local changes to the outbox (team auto-sync)."""
    _listeners.append(fn)


def _active_cs(root):
    """The active changeset id that team.py keeps in <root>/team/changeset.json, or None (not in a team)."""
    try:
        with open(Path(root) / TEAM_DIR / "changeset.json") as f:
            a = (json.load(f) or {}).get("active") or {}
        return str(a["id"]) if a.get("id") else None
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


def _strip(d):
    d = dict(d)
    d.pop(META, None)
    return d


@contextlib.contextmanager
def _open(lay, root, default=None):
    """Lock the file, load it, migrate it if it has no store block. Yields (path, doc, meta); the caller saves."""
    r = _root(root)
    path = r / lay.file
    with open(r / lay.lock, "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        d, existed = _load(path, lay, default, salvage=True)
        meta = d.pop(META, None)
        if not isinstance(meta, dict):
            if existed:
                backup = path.with_name(path.stem + BACKUP_SUFFIX)
                if not backup.exists():
                    shutil.copy2(path, backup)
            meta = {"format": FORMAT, "colls": {}, "outbox": []}
        yield path, d, meta


def _commit(path, d, meta, lay, bump, by=None, by_item=None):
    out = _reconcile(d, meta, lay, by=by, by_item=by_item, cs=_active_cs(path.parent))
    if bump and lay.version_key:
        d[lay.version_key] = d.get(lay.version_key, 0) + 1
    d[META] = meta
    _save(path, d)
    del d[META]
    if out:
        for fn in list(_listeners):
            try:
                fn(path.parent)
            except Exception:  # noqa: BLE001 - a listener must never break a save
                pass


# ---------- the public API ----------

@contextlib.contextmanager
def doc(coll, root=None, by=None, bump=False, default=None):
    """Read-modify-write the area's whole file shape (without the store block) under its lock. Saved on a clean exit.
    bump: add 1 to the file's version (callers that bump it themselves leave this off)."""
    lay = layout(coll)
    with _open(lay, root, default) as (path, d, meta):
        _reconcile(d, meta, lay, cs=_active_cs(path.parent))  # an older writer's changes are recorded before this edit
        yield d
        _commit(path, d, meta, lay, bump, by=by)


class Tx:
    """Item access to one collection inside a locked file (see tx())."""

    def __init__(self, coll, lay, d, meta, by):
        self.coll, self.lay, self.doc, self._meta, self.by, self._by_item = coll, lay, d, meta, by, {}
        self.key, self.kind = lay.colls[coll]

    @property
    def version(self):
        """The file's version (the number the UI watches), or the collection's own counter when the file has none."""
        if self.lay.version_key:
            return self.doc.get(self.lay.version_key, 0)
        return self._meta.get("colls", {}).get(self.coll, {}).get("version", 0)

    def _record(self, iid, data):
        m = dict(self._meta.get("colls", {}).get(self.coll, {}).get("items", {}).get(iid) or {})
        m.pop("h", None)
        return {"id": iid, "data": data, "version": m.get("version", 0), "author": m.get("author") or _data_by(data) or "you",
                "updated_by": m.get("updated_by") or _data_by(data) or "you", "updated_at": m.get("updated_at", ""), "deleted": False}

    def ids(self):
        return [i for i, _ in _entries(self.doc, self.key, self.kind)]

    def items(self):
        """Live items in order, as records {id, data, version, author, updated_by, updated_at, deleted}."""
        return [self._record(i, x) for i, x in _entries(self.doc, self.key, self.kind)]

    def get(self, iid):
        """The record for id, or None. record["data"] is the live dict: changing it changes the draft."""
        iid = str(iid)
        return next((self._record(i, x) for i, x in _entries(self.doc, self.key, self.kind) if i == iid), None)

    def put(self, iid, data, by=None):
        """Create or replace an item. A new id goes last; an existing one keeps its place."""
        iid = str(iid)
        if not isinstance(data, dict):
            raise ValueError("an item's data must be a dict")
        if by:
            self._by_item[iid] = by
        if self.kind == "single":
            self.doc[self.key] = data
        elif self.kind == "dict":
            self.doc.setdefault(self.key, {})[iid] = data
        else:
            lst = self.doc.setdefault(self.key, [])
            data = data if str(data.get("id")) == iid else {**data, "id": data.get("id", iid)}
            for n, x in enumerate(lst):
                if isinstance(x, dict) and str(x.get("id")) == iid:
                    lst[n] = data
                    break
            else:
                lst.append(data)
        return data

    def delete(self, iid, by=None):
        """Remove an item. -> True when it was there."""
        iid = str(iid)
        if iid not in self.ids():
            return False
        if by:
            self._by_item[iid] = by
        if self.kind == "single":
            self.doc.pop(self.key, None)
        elif self.kind == "dict":
            self.doc[self.key].pop(iid, None)
        else:
            self.doc[self.key] = [x for x in self.doc[self.key] if not (isinstance(x, dict) and str(x.get("id")) == iid)]
        return True


@contextlib.contextmanager
def tx(coll, root=None, by=None, bump=True, default=None):
    """A transaction on one collection: `with tx("email", root) as t: t.put(id, data, "you")`. Saved on a clean
    exit (nothing is saved when the block raises). bump: the file's version + 1 on every commit, as before."""
    lay = layout(coll)
    with _open(lay, root, default) as (path, d, meta):
        _reconcile(d, meta, lay, cs=_active_cs(path.parent))
        t = Tx(coll, lay, d, meta, by)
        yield t
        _commit(path, d, meta, lay, bump, by=by, by_item=t._by_item)


def read(coll, root=None, default=None):
    """The area's file shape without the store block (no lock: writes are atomic replaces). Missing or broken file:
    a copy of default (the layout's default when None)."""
    lay = layout(coll)
    d, _ = _load(_root(root) / lay.file, lay, default, salvage=False)
    return _strip(d) if isinstance(d, dict) else d


def items(coll, root=None):
    """Live items of one collection as records (read only)."""
    lay = layout(coll)
    d, _ = _load(_root(root) / lay.file, lay, None, salvage=False)
    meta = d.pop(META, None)
    meta = meta if isinstance(meta, dict) else {"colls": {}}
    _reconcile(d, meta, lay, record=False)
    return Tx(coll, lay, d, meta, None).items()


def apply_remote(coll, item, root=None, expect=None):
    """Write one item from the team server ({id, data, version, author, updated_by, updated_at, deleted}).
    Its version and authors are taken as given and nothing is added to the outbox. The file's version bumps so the UI
    reloads. Local edits made by an older writer since the last commit are recorded first, as usual.
    expect: None writes whatever is there. Otherwise the local item must still be what the caller saw (its hash from
    peek(), or "" for no live item); when it changed meanwhile nothing is written and the result is False, so a
    pending local edit is never overwritten. When written, the item's outbox entry goes (local now equals the server)."""
    lay = layout(coll)
    iid = str(item["id"])
    with _open(lay, root) as (path, d, meta):
        _reconcile(d, meta, lay, cs=_active_cs(path.parent))
        t = Tx(coll, lay, d, meta, None)
        if expect is not None:
            cur = t.get(iid)
            if (_hash(cur["data"]) if cur else "") != expect:
                d[META] = meta
                _save(path, d)  # keep what reconcile recorded
                return False
            box = meta.setdefault("outbox", [])
            box[:] = [x for x in box if (x["coll"], x["id"]) != (coll, iid)]
        if item.get("deleted"):
            t.delete(iid)
        else:
            t.put(iid, item.get("data") or {})
        cm = meta["colls"].setdefault(coll, {"version": 0, "items": {}})
        m = {"version": int(item.get("version") or 0), "author": item.get("author") or "", "updated_by": item.get("updated_by") or "",
             "updated_at": item.get("updated_at") or now()}
        if item.get("deleted"):
            m["deleted"] = True
        else:
            m["h"] = _hash(t.get(iid)["data"])
        cm["items"][iid] = m
        cm["version"] = cm.get("version", 0) + 1
        if lay.version_key:
            d[lay.version_key] = d.get(lay.version_key, 0) + 1
        d[META] = meta
        _save(path, d)
    return True


def peek(coll, iid, root=None):
    """One item as it is on disk now: (data or None when there is no live item, its hash or "")."""
    for r in items(coll, root):
        if r["id"] == str(iid):
            return r["data"], _hash(r["data"])
    return None, ""


def item_hash(data):
    return _hash(data) if data is not None else ""


def outbox_ack(coll, acks, root=None):
    """Drop outbox entries the team server has acknowledged. acks: [(id, hash)] with the hash of what was sent
    ("" for a delete). An entry whose item changed again since then stays. -> how many went."""
    lay = layout(coll)
    want = {str(i): h for i, h in acks}
    with _open(lay, root) as (path, d, meta):
        _reconcile(d, meta, lay, cs=_active_cs(path.parent))
        t = Tx(coll, lay, d, meta, None)
        live = {r["id"]: _hash(r["data"]) for r in t.items()}
        box = meta.setdefault("outbox", [])
        keep = [x for x in box if not (x["coll"] == coll and x["id"] in want and live.get(x["id"], "") == want[x["id"]])]
        gone = len(box) - len(keep)
        box[:] = keep
        d[META] = meta
        _save(path, d)
    return gone


def mark_pending(coll, iid, root=None):
    """Make sure the item has an outbox entry (put when it is live, else delete), e.g. after a conflict was resolved
    in favour of the local copy, which then has to be sent."""
    lay = layout(coll)
    iid = str(iid)
    with _open(lay, root) as (path, d, meta):
        cs = _active_cs(path.parent)
        _reconcile(d, meta, lay, cs=cs)
        live = Tx(coll, lay, d, meta, None).get(iid) is not None
        e = {"coll": coll, "id": iid, "op": "put" if live else "delete", "at": now()}
        if cs:
            e["cs"] = cs
        _outbox_add(meta, [e])
        d[META] = meta
        _save(path, d)


def _files(root):
    r = _root(root)
    out = [lay for lay in FIXED if (r / lay.file).exists()]
    for f in sorted(r.glob("draft_*.json")):
        if not f.name.endswith(BACKUP_SUFFIX) and ".corrupt-" not in f.name:
            out.append(ads_layout(f.name[len("draft_"):-len(".json")]))
    return out


def outbox(root=None):
    """Every pending local change in the data folder, oldest first: [{coll, id, op, at}]."""
    out = []
    for lay in _files(root):
        d, _ = _load(_root(root) / lay.file, lay, None, salvage=False)
        meta = d.get(META)
        if isinstance(meta, dict):
            out += meta.get("outbox", [])
    return sorted(out, key=lambda e: e["at"])


def migrate(root=None):
    """Migrate every draft file in the data folder now (idempotent). -> the files that were migrated."""
    done = []
    for lay in _files(root):
        with _open(lay, root) as (path, d, meta):
            if meta.get("colls"):
                continue
            _commit(path, d, meta, lay, bump=False)
            done.append(lay.file)
    return done
