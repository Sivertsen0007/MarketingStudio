"""SoMe storage: posts (social_draft.json), the scheduling queue (social_queue.json) and SoMe settings
(social_settings.json), all in Studio's gitignored data folder, each guarded by a file lock.

The data folder is core.STUDIO_DIR, or the SOCIAL_DIR environment variable (tests, the test server and the
VPS publisher job use their own folder). This module has no Studio imports at load time, so the VPS job can use it.

Post (one per idea, ticked for one or more channels):
  {id, title, channels: [facebook, instagram, linkedin], when (ISO with offset, or ""), caption, captions: {channel: text},
   first_comment, hashtags, link, alt_text, location, media: [{media_id, kind, name}], format (text|image|carousel|video|reel),
   guest_photos, consent_confirmed, move: {when, at} (a drag in the calendar, not confirmed yet),
   results: {channel: {status, post_id, url, error, at, via, scheduled_for}}, by, created, updated}
Personal data: nothing about other people is stored here (no comments, no profiles).
"""
import contextlib
import datetime as dt
import fcntl
import json
import os
import re
import uuid
from pathlib import Path

CHANNELS = ("facebook", "instagram", "linkedin")
CHANNEL_LABEL = {"facebook": "Facebook", "instagram": "Instagram", "linkedin": "LinkedIn"}
POST_FIELDS = ("title", "channels", "when", "caption", "captions", "first_comment", "hashtags", "link", "alt_text", "location",
               "media", "format", "guest_photos", "consent_confirmed",
               "approval", "note", "utm", "autolist")
DEFAULT_SETTINGS = {"vps_job": False, "vps_host": "", "vps_dir": "", "public_host": "r2",
                    # Studio's own scheduler on this Mac (publishes queue items the user scheduled, while Studio runs)
                    "local_scheduler": False, "late_minutes": 0,
                    # posting slots per channel: ["Tue 09:00", "Thu 18:00"]
                    "slots": {"facebook": ["Tue 09:00", "Thu 18:00"], "instagram": ["Tue 09:00", "Thu 18:00"], "linkedin": ["Wed 10:00"]},
                    # trackable links: add utm_* to links on these channels (never to links that already have UTMs)
                    "utm": {"facebook": True, "linkedin": True}, "utm_campaign": "organic_social",
                    "competitors": []}
APPROVAL = ("", "needed", "approved")


def data_dir():
    d = os.environ.get("SOCIAL_DIR")
    if d:
        p = Path(d)
    else:
        import core  # Studio's data folder (only imported when there is no SOCIAL_DIR, e.g. not on the VPS)
        p = core.STUDIO_DIR
    p.mkdir(parents=True, exist_ok=True)
    return p


def now():
    return dt.datetime.now(dt.timezone.utc).astimezone()


def iso(t):
    return t.isoformat(timespec="seconds") if t else ""


def parse_when(v):
    """ISO text (with or without offset; without = this Mac's local time) -> aware datetime, or None."""
    if not v:
        return None
    if isinstance(v, (int, float)):
        return dt.datetime.fromtimestamp(v, dt.timezone.utc).astimezone()
    v = re.sub(r"([+-]\d\d)(\d\d)$", r"\1:\2", str(v).strip().replace("Z", "+00:00"))  # Meta's +0000 (Python 3.9 needs +00:00)
    t = dt.datetime.fromisoformat(v)
    return t if t.tzinfo else t.astimezone()


@contextlib.contextmanager
def _locked(name, default):
    """Read-modify-write one JSON file under an exclusive lock. Yields the data; it is saved on exit."""
    d = data_dir()
    path = d / name
    with open(d / f".{name}.lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            data = json.loads(json.dumps(default))
        yield data
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False))
        tmp.replace(path)


def _read(name, default):
    try:
        return json.loads((data_dir() / name).read_text())
    except (OSError, ValueError):
        return json.loads(json.dumps(default))


# ---------- settings ----------

def settings():
    return {**DEFAULT_SETTINGS, **_read("social_settings.json", {})}


def save_settings(changes):
    allowed = {k: v for k, v in (changes or {}).items() if k in DEFAULT_SETTINGS}
    with _locked("social_settings.json", {}) as s:
        s.update(allowed)
        return {**DEFAULT_SETTINGS, **s}


# ---------- posts ----------

def posts():
    return _read("social_draft.json", {"posts": []})["posts"]


def get(post_id):
    p = next((x for x in posts() if x["id"] == post_id), None)
    if not p:
        raise ValueError("post not found")
    return p


def blank(by="you"):
    t = iso(now())
    return {"id": uuid.uuid4().hex[:10], "title": "", "channels": ["facebook", "instagram"], "when": "", "caption": "", "captions": {},
            "first_comment": "", "hashtags": "", "link": "", "alt_text": "", "location": "", "media": [], "format": "text",
            "guest_photos": False, "consent_confirmed": False, "approval": "", "note": "", "utm": True, "autolist": "", "move": None, "results": {}, "by": by, "created": t, "updated": t}


def clean(fields):
    """Only the editable fields, in the right shape."""
    out = {}
    for k in POST_FIELDS:
        if k not in fields:
            continue
        v = fields[k]
        if k == "channels":
            v = [c for c in CHANNELS if c in (v or [])]
        elif k == "captions":
            v = {c: str(t) for c, t in (v or {}).items() if c in CHANNELS and t is not None}
        elif k == "media":
            v = [{"media_id": str(m["media_id"]), "kind": m.get("kind", "image"), "name": m.get("name", "")} for m in (v or []) if m.get("media_id")]
        elif k in ("guest_photos", "consent_confirmed", "utm"):
            v = bool(v)
        elif k == "approval":
            v = v if v in APPROVAL else ""
        elif k == "when":
            v = iso(parse_when(v)) if v else ""
        else:
            v = "" if v is None else str(v)
        out[k] = v
    if "media" in out and "format" not in fields:
        m = out["media"]
        out["format"] = "text" if not m else "carousel" if len(m) > 1 else ("video" if m[0]["kind"] == "video" else "image")
    return out


OPEN = ("scheduled", "publishing", "needs_checking")  # results that are not final: the post can't be edited meanwhile


def edit_block(p):
    """Why this post can't be edited now ("" = it can): a channel (ticked or not) is scheduled, publishing or needs checking."""
    busy = [CHANNEL_LABEL.get(c, c) for c, r in (p.get("results") or {}).items() if r.get("status") in OPEN]
    return ("this post is scheduled, being posted or needs checking on " + ", ".join(busy) + ": cancel or check it first") if busy else ""


def save(fields, by="you"):
    """Create (no id) or update a post. Published channels are history: editing never changes them on the channel.
    Second guard: a post that isn't editable (see edit_block) is refused here too, whichever caller."""
    with _locked("social_draft.json", {"posts": []}) as d:
        pid = fields.get("id")
        p = next((x for x in d["posts"] if x["id"] == pid), None) if pid else None
        if p and edit_block(p):
            raise ValueError(edit_block(p))
        if not p:
            p = blank(by)
            d["posts"].append(p)
        p.update(clean(fields))
        p["updated"] = iso(now())
        return dict(p)


def update(post_id, fn):
    """fn(post) changes the post in place under the lock. -> the post."""
    with _locked("social_draft.json", {"posts": []}) as d:
        p = next((x for x in d["posts"] if x["id"] == post_id), None)
        if not p:
            raise ValueError("post not found")
        fn(p)
        p["updated"] = iso(now())
        return json.loads(json.dumps(p))


def remove(post_id):
    with _locked("social_draft.json", {"posts": []}) as d:
        p = next((x for x in d["posts"] if x["id"] == post_id), None)
        if not p:
            raise ValueError("post not found")
        live = [c for c, r in (p.get("results") or {}).items() if r.get("status") in ("scheduled", "published", "publishing", "needs_checking")]
        if live:
            raise ValueError("this post is scheduled or published on " + ", ".join(CHANNEL_LABEL[c] for c in live)
                             + ": cancel or delete it there first")
        d["posts"] = [x for x in d["posts"] if x["id"] != post_id]
        return True


def status(p):
    """Overall status of a post from its channel results: failed > publishing > scheduled > published > draft."""
    res = p.get("results") or {}
    st = [(res.get(c) or {}).get("status") for c in p.get("channels") or []]
    st += [r.get("status") for c, r in res.items() if c not in (p.get("channels") or []) and r.get("status") in OPEN]  # unticked but open
    for s in ("failed", "needs_checking", "missed", "not_scheduled", "publishing", "scheduled"):
        if s in st:
            return s
    if st and all(s == "published" for s in st):
        return "published"
    if "published" in st:
        return "partly published"
    return "draft"


def caption_for(p, channel):
    """The text that goes out on one channel: its own caption (or the shared one) plus hashtags."""
    text = (p.get("captions") or {}).get(channel) or p.get("caption") or ""
    tags = " ".join(t if t.startswith("#") else "#" + t for t in str(p.get("hashtags") or "").replace(",", " ").split() if t.strip("#"))
    if tags and tags not in text:
        text = (text.rstrip() + "\n\n" + tags).strip()
    return text


# ---------- library: templates, hashtag sets, evergreen autolists ----------

LIB_DEFAULT = {"templates": [], "hashtag_sets": [], "autolists": []}


def library():
    return {**LIB_DEFAULT, **_read("social_library.json", {})}


def library_save(kind, item):
    """Add or replace one item ({id?, name, ...}) in templates / hashtag_sets / autolists."""
    if kind not in LIB_DEFAULT:
        raise ValueError("unknown library list")
    with _locked("social_library.json", {}) as lib:
        items = lib.setdefault(kind, [])
        item = {**item, "id": item.get("id") or uuid.uuid4().hex[:8]}
        lib[kind] = [x for x in items if x["id"] != item["id"]] + [item]
        return item


def library_remove(kind, item_id):
    with _locked("social_library.json", {}) as lib:
        lib[kind] = [x for x in lib.get(kind, []) if x["id"] != item_id]
    return True


# ---------- queue (scheduled items for the publisher: Studio's own scheduler or the VPS job) ----------

def queue():
    return _read("social_queue.json", {"items": []})["items"]


def queue_new(item):
    """A queue item (not saved yet). item: {post_id, channel, due (ISO), payload, runner, max_late}."""
    return {"id": uuid.uuid4().hex[:10], "status": "scheduled", "created": iso(now()), "result": None, **item}


def queue_replace(old_id, new_item):
    """A move, in one step under the queue lock: add the new item and cancel the old one, only if the old one is still
    scheduled (the publisher's claim takes the same lock). -> (new item, "") or (None, the old item's status / "missing")."""
    with _locked("social_queue.json", {"items": []}) as q:
        old = next((x for x in q["items"] if x["id"] == old_id), None)
        if not old or old["status"] != "scheduled":
            return None, old["status"] if old else "missing"
        old["status"], old["result"] = "cancelled", {"at": iso(now()), "moved_to": new_item["id"]}
        q["items"].append(dict(new_item))
        return dict(new_item), ""


def queue_add(item):
    """item: {post_id, channel, due (ISO), payload}. -> the queue item (status scheduled)."""
    it = queue_new(item)
    with _locked("social_queue.json", {"items": []}) as q:
        q["items"] = [x for x in q["items"] if not (x["post_id"] == it["post_id"] and x["channel"] == it["channel"] and x["status"] == "scheduled")]
        q["items"].append(it)
    return it


def queue_set(item_id, **changes):
    with _locked("social_queue.json", {"items": []}) as q:
        it = next((x for x in q["items"] if x["id"] == item_id), None)
        if not it:
            raise ValueError("queue item not found")
        it.update(changes)
        return dict(it)


def queue_cancel(post_id, channel):
    """Cancel the scheduled items of one post + channel under the queue lock, so it can't race the publisher's claim.
    -> {"cancelled": n, "busy": [ids of items already being published: too late to cancel those]}"""
    out = {"cancelled": 0, "busy": []}
    with _locked("social_queue.json", {"items": []}) as q:
        for x in q["items"]:
            if x["post_id"] != post_id or x["channel"] != channel:
                continue
            if x["status"] == "scheduled":
                x["status"], x["result"] = "cancelled", {"at": iso(now())}
                out["cancelled"] += 1
            elif x["status"] == "publishing":
                out["busy"].append(x["id"])
    return out


def queue_cancel_id(item_id):
    """Cancel one item by id (the VPS job's --cancel). -> "cancelled", "publishing" (too late), or its final status."""
    with _locked("social_queue.json", {"items": []}) as q:
        it = next((x for x in q["items"] if x["id"] == item_id), None)
        if not it:
            return "missing"
        if it["status"] == "scheduled":
            it["status"], it["result"] = "cancelled", {"at": iso(now())}
            return "cancelled"
        return it["status"]


def queue_claim(item_id, due_iso, to="publishing"):
    """Atomically take an item for publishing (or mark it missed): only if it is still scheduled and still due at the
    time the publisher read. A cancel or a move that got the lock first wins. -> the item, or None."""
    with _locked("social_queue.json", {"items": []}) as q:
        it = next((x for x in q["items"] if x["id"] == item_id), None)
        if not it or it["status"] != "scheduled" or it.get("due") != due_iso:
            return None
        it["status"], it["claimed_at"] = to, iso(now())
        return dict(it)


def due(items, at=None, max_late_min=None, default_late=2):
    """Split queue items into (due now, missed). Only status scheduled counts. Missed = more minutes overdue than the
    grace: max_late_min when given (an explicit override), else the item's own max_late (set from Studio's late-posting
    setting when the user pressed Schedule), else default_late."""
    at = at or now()
    ready, missed = [], []
    for x in items:
        if x.get("status") != "scheduled":
            continue
        t = parse_when(x.get("due"))
        if not t or t > at:
            continue
        grace = max_late_min if max_late_min is not None else x.get("max_late", default_late)
        (missed if (at - t).total_seconds() > float(grace) * 60 else ready).append(x)
    return ready, missed
