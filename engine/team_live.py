"""Team live: who is here, what they changed, claims, comments and mentions (teamserver/PROTOCOL.md). Only active when
this Studio is in a team (team.joined()); a solo user never calls any of this.

Talks to the team server through team.py's HTTP helper and device token (team._api). What it keeps in <data>/team/:
  received.json  per item ("coll\\tid") that arrived from a teammate: the local data it replaced (before), the hash of
                 the data it became (h), who and when. A hover can then say "Alex changed Headline 2: old -> new".
  feed.json      the activity feed cache {seq, events} (newest last, edit_session events replaced by data.session).

Live: team.py's stream listener passes every server event to on_event(). It keeps the last presence list it was sent
and bumps a counter per kind (event, presence, comment, mention, items), which the page polls (ping) without any
network call, and fetches what changed.

Marks (tints): marks() lists every draft item whose last writer is another member (draftstore's updated_by, as
team.py writes it from the server), with who, when, the fields that changed (from received.json, or from the item's
own shape) and parent names so the UI can put a dot on campaigns, ad groups and map hubs.
"""
import contextlib
import copy
import json
import re
import threading
import time
import urllib.parse

import draftstore
import team

FEED_KEEP = 500
RECEIVED_KEEP = 2000
SKIP_KEYS = {"id", "problems", "warnings", "at", "ts", "by", "edited_by", "edited_at", "summary", "type", "issues"}
LOCAL_WRITERS = {"", "you", "claude", "team sync"}
THINGS = {"seo:map_nodes", "seo:new_posts", "seo:map_proposals", "social"}  # items that are the thing itself, not a change to it
LABEL_KEYS = ("summary", "title", "name", "subject", "post_title", "campaign", "ad_set", "ad")
CLAUDE_PREFIX = "Claude: "

_lock = threading.RLock()
LIVE = {"seq": 0, "kinds": {}, "presence": None, "presence_at": 0}


# ---------- plumbing ----------

def _need_team():
    if not team.joined():
        raise ValueError("Join a team first (Setup > Team)")


def _api(method, path, body=None):
    _need_team()
    return team._api(method, path, body)


def _q(**kw):
    return urllib.parse.urlencode({k: v for k, v in kw.items() if v is not None})


def _me():
    return (team._state().get("member") or {})


def _members():
    m = team.members()
    if not m:
        with contextlib.suppress(team.TeamError):
            m = team._refresh_members()
    return m or {}


_REFRESHED = {"at": None}


def _member(mid):
    m = _members().get(str(mid)) if mid is not None else None
    if not m and mid is not None and (_REFRESHED["at"] is None or time.monotonic() - _REFRESHED["at"] > 20):  # someone who joined since: ask once
        _REFRESHED["at"] = time.monotonic()
        with contextlib.suppress(team.TeamError):
            m = team._refresh_members().get(str(mid))
    if not m:
        return {"id": mid, "name": f"member {mid}" if mid is not None else "", "colour": "#888888"}
    return {"id": int(mid) if str(mid).isdigit() else mid, "name": m["name"], "colour": m["colour"], "role": m.get("role", "")}


def _bump(kind):
    with _lock:
        LIVE["seq"] += 1
        LIVE["kinds"][kind] = LIVE["seq"]


def on_event(kind, data):
    """team.py's stream listener: every SSE event from the server."""
    if kind == "presence":
        try:
            p = json.loads(data or "{}").get("presence")
            if isinstance(p, list):
                LIVE["presence"] = p
        except ValueError:
            pass
    if kind in ("event", "presence", "comment", "mention", "items"):
        _bump(kind)


def ping():
    """What changed since the page last looked (local only, no network): {seq, kinds: {kind: seq}}."""
    with _lock:
        return {"seq": LIVE["seq"], "kinds": dict(LIVE["kinds"]), "joined": team.joined()}


# ---------- received changes (for hovers) ----------

def on_received(entries):
    """team.py, end of a sync: items that arrived from teammates."""
    root, cache = team._root(), {}

    def now_hash(coll, iid):
        if coll not in cache:  # each collection read once per sync, not once per item
            try:
                cache[coll] = {r["id"]: draftstore.item_hash(r["data"]) for r in draftstore.items(coll, root)}
            except ValueError:
                cache[coll] = {}
        return cache[coll].get(str(iid), "")
    with _lock:
        rec = team._load("received.json", {})
        for e in entries:
            S = e.get("item") or {}
            h = now_hash(e["coll"], e["id"])
            rec.pop(team._key(e["coll"], e["id"]), None)
            rec[team._key(e["coll"], e["id"])] = {"before": e.get("before"), "h": h, "by": S.get("updated_by"),
                                                   "at": S.get("updated_at") or team._now()}
        if len(rec) > RECEIVED_KEEP:
            rec = dict(list(rec.items())[-RECEIVED_KEEP:])
        team._save("received.json", rec)
    _bump("items")


team.EVENT_HOOKS.append(on_event)
team.RECEIVE_HOOKS.append(on_received)
draftstore.on_local_write(lambda root: _bump("items"))  # my own edit can clear a tint: the page re-reads marks


# ---------- field changes ----------

def _label_key(k):
    s = str(k).replace("_", " ").strip()
    return s[:1].upper() + s[1:]


ISO_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _empty(v):
    return v is None or v == "" or v == [] or v == {}


def _short(v, n=80):
    if v is None:
        return ""
    if isinstance(v, str):
        m = ISO_RE.match(v)
        if m and 1 <= int(m.group(2)) <= 12:  # a date and time: as people write it
            return f"{int(m.group(3))} {MONTHS[int(m.group(2)) - 1]} {m.group(1)} {m.group(4)}:{m.group(5)}"
    if isinstance(v, bool):
        return "Yes" if v else "No"
    if isinstance(v, (list, tuple)):
        s = ", ".join(_short(x, n) for x in v)
    elif isinstance(v, dict):
        s = json.dumps(v, ensure_ascii=False)
    else:
        s = str(v)
    s = " ".join(s.split())
    return s if len(s) <= n else s[:n - 3] + "..."


def _diff(before, after, prefix=""):
    """Changed keys between two item datas (one level into dicts). -> [{field, old, new}]."""
    out = []
    before = before if isinstance(before, dict) else {}
    after = after if isinstance(after, dict) else {}
    for k in list(after) + [k for k in before if k not in after]:
        if k in SKIP_KEYS:
            continue
        a, b = before.get(k), after.get(k)
        if a == b or (_empty(a) and _empty(b)):
            continue
        if isinstance(a, dict) and isinstance(b, dict) or (a is None and isinstance(b, dict) and k in ("change", "fields")):
            out += _diff(a or {}, b, prefix)
            continue
        out.append({"field": _label_key(k), "key": prefix + str(k), "old": _short(a) if k in before else None,
                    "new": _short(b) if k in after else None})
    return out


def _own_fields(coll, data):
    """Fields an item changes, from its own shape (when nothing was recorded on arrival)."""
    if not isinstance(data, dict):
        return []
    if coll == "seo:changes" and data.get("field"):
        return [{"field": data.get("label") or _label_key(data["field"]), "key": data["field"],
                 "old": _short(data.get("old")), "new": _short(data.get("new") if data.get("kind") != "todo" else data.get("note"))}]
    if coll.startswith("ads:") and isinstance(data.get("change"), dict):
        c = data["change"]
        return [{"field": _label_key(k), "key": k, "old": None, "new": _short(v)} for k, v in c.items()
                if k not in SKIP_KEYS | {"campaign", "ad_group", "campaign_id", "adset_id", "new", "ref"} and not _empty(v)]
    if coll == "email" and isinstance(data.get("fields"), dict):
        return [{"field": _label_key(k), "key": k, "old": None, "new": _short(v)} for k, v in data["fields"].items() if not _empty(v)]
    return [{"field": _label_key(k), "key": k, "old": None, "new": _short(v)} for k, v in data.items()
            if k not in SKIP_KEYS and not isinstance(v, (dict, list)) and not _empty(v) and v is not False][:6]


def label(coll, iid, data=None):
    """A short readable name for an item (draft data where there is some)."""
    if data is None and coll and iid:
        with contextlib.suppress(Exception):
            data = draftstore.peek(coll, iid, team._root())[0]
    if isinstance(data, dict):
        for d in (data, data.get("change") or {}, data.get("fields") or {}):
            if isinstance(d, dict):
                for k in LABEL_KEYS:
                    if isinstance(d.get(k), str) and d[k].strip():
                        return d[k].strip()[:120]
    if iid and ":" in str(iid):
        kind, _, rest = str(iid).partition(":")
        return f"{_label_key(kind)} {rest}"
    return f"{coll} {iid}" if iid else str(coll or "")


def _parents(coll, data):
    """Names / ids of what an item sits under, so the UI can dot the tree."""
    if not isinstance(data, dict):
        return {}
    if coll.startswith("ads:"):
        c = data.get("change") if isinstance(data.get("change"), dict) else {}
        return {k: str(c[k]) for k in ("campaign", "ad_group", "campaign_id", "adset_id") if c.get(k) not in (None, "")}
    if coll == "seo:changes":
        return {"post_id": str(data.get("post_id") or "")}
    if coll == "seo:new_posts":
        return {"node_id": str(data.get("node_id") or "")}
    if coll == "seo:map_nodes":
        return {"parent": str(data.get("parent") or "")}
    return {}


def _colls():
    out = []
    for lay in draftstore._files(team._root()):
        out += list(lay.colls)
    return out


def marks(coll=None):
    """Draft items whose last writer is another member: {coll: {id: {who, at, fields, label, parents}}}."""
    if not team.joined():
        return {"joined": False, "marks": {}}
    me = _me()
    mine = {str(me.get("name") or "").strip().lower()} | LOCAL_WRITERS
    by_name = {}
    for mid, m in _members().items():
        by_name.setdefault(m["name"].strip().lower(), mid)
    rec = team._load("received.json", {})
    root = team._root()
    out = {}
    for c in ([coll] if coll else _colls()):
        try:
            recs = draftstore.items(c, root)
        except ValueError:
            continue
        got = {}
        anc = {}
        if c == "seo:map_nodes":
            anc = {r["id"]: str((r["data"] or {}).get("parent") or "") for r in recs}
        for r in recs:
            who = str(r.get("updated_by") or "").strip()
            if who.lower() in mine:
                continue
            data = r["data"]
            k = team._key(c, r["id"])
            x = rec.get(k)
            mid = by_name.get(who.lower())
            fields, added = None, False
            at = r.get("updated_at") or ""
            if x and x.get("h") == draftstore.item_hash(data):
                fields = _diff(x["before"], data) if x.get("before") is not None else None
                added = x.get("before") is None and c in THINGS
                mid = x.get("by") if x.get("by") is not None else mid
                at = x.get("at") or at
            if not fields:
                fields = _own_fields(c, data)
            m = _member(mid) if mid is not None else {"id": None, "name": who, "colour": "#888888"}
            if mid is not None and str(mid) == str(me.get("id")):
                continue
            mark = {"who": m, "at": at, "fields": fields[:12], "added": added, "label": label(c, r["id"], data),
                    "parents": _parents(c, data)}
            if anc:
                chain, p, seen = [], anc.get(r["id"]), set()
                while p and p not in seen:
                    seen.add(p)
                    chain.append(p)
                    p = anc.get(p)
                mark["ancestors"] = chain
            got[r["id"]] = mark
        if got:
            out[c] = got
    return {"joined": True, "marks": out}


# ---------- presence ----------

def _presence_out(lst):
    me = str(_me().get("id"))
    out = []
    for p in lst or []:
        if not isinstance(p, dict):
            continue
        m = _member(p.get("member"))
        out.append({**{k: p.get(k) for k in ("area", "coll", "id", "editing", "at")}, "member": m,
                    "me": str(p.get("member")) == me,
                    "label": label(p.get("coll"), p.get("id")) if p.get("coll") and p.get("id") else ""})
    return out


def presence_beat(area, coll=None, iid=None, editing=False):
    """Heartbeat: where I am and what I have open. -> everyone here."""
    body = {"area": str(area or "")[:40] or "ads", "editing": bool(editing)}
    if coll and iid and team.COLL_RE.match(str(coll)) and team.ID_RE.match(str(iid)):
        body["coll"], body["id"] = str(coll), str(iid)
    r = _api("POST", "/api/v1/presence", body)
    LIVE["presence"] = r.get("presence") or []
    return {"presence": _presence_out(LIVE["presence"])}


def presence():
    if LIVE["presence"] is None:
        LIVE["presence"] = _api("GET", "/api/v1/presence").get("presence") or []
    return {"presence": _presence_out(LIVE["presence"])}


# ---------- claims ----------

def _claim_out(c):
    return {"coll": c.get("coll"), "id": c.get("id"), "note": c.get("note") or "", "at": c.get("at"),
            "member": _member(c.get("member")), "mine": str(c.get("member")) == str(_me().get("id"))}


def claims():
    return {"claims": [_claim_out(c) for c in _api("GET", "/api/v1/claims").get("claims") or []]}


def claim(coll, iid, note=None):
    """Claim an item ("Alex is writing this"). Someone else holding it -> {ok: false, held_by, message}."""
    try:
        c = _api("POST", "/api/v1/claims", {"coll": coll, "id": str(iid), **({"note": note} if note else {})})
    except team.TeamError as e:
        if e.status != 409:
            raise
        held = next((x for x in _api("GET", "/api/v1/claims").get("claims") or []
                     if x.get("coll") == coll and str(x.get("id")) == str(iid)), None)
        h = _claim_out(held) if held else None
        who = h["member"]["name"] if h else "another member"
        return {"ok": False, "conflict": True, "held_by": h, "message": f"{who} is already writing this"}
    return {"ok": True, "claim": _claim_out(c)}


def release(coll, iid):
    _api("DELETE", "/api/v1/claims", {"coll": coll, "id": str(iid)})
    return {"ok": True}


# ---------- comments and mentions ----------

def _comment_out(c):
    return {**c, "author": _member(c.get("author")), "mine": str(c.get("author")) == str(_me().get("id")),
            "mentions": [_member(m) for m in c.get("mentions") or []]}


def comments(coll, iid):
    r = _api("GET", "/api/v1/comments?" + _q(coll=coll, id=str(iid)))
    return {"comments": [_comment_out(c) for c in r.get("comments") or []], "label": label(coll, iid)}


def counts(colls):
    """{coll: {item_id: {open, total, latest}}} for several collections."""
    if isinstance(colls, str):
        colls = [c for c in colls.split(",") if c]
    out = {}
    for c in (colls or [])[:20]:
        if team.COLL_RE.match(c):
            out[c] = _api("GET", "/api/v1/comments/counts?" + _q(coll=c))
    return {"counts": out}


def comment_add(coll, iid, body, parent=None, mentions=None, by_claude=False):
    body = str(body or "").strip()
    if not body:
        raise ValueError("Write something first")
    if by_claude and not body.startswith(CLAUDE_PREFIX):
        body = CLAUDE_PREFIX + body  # written by Claude for the user: say so
    req = {"coll": coll, "id": str(iid), "body": body}
    if parent:
        req["parent"] = int(parent)
    if mentions:
        req["mentions"] = [int(m) for m in mentions]
    c = _api("POST", "/api/v1/comments", req)
    _bump("comment")
    return {"comment": _comment_out(c)}


def comment_edit(cid, body=None, resolved=None):
    req = {}
    if body is not None:
        req["body"] = str(body).strip()
    if resolved is not None:
        req["resolved"] = bool(resolved)
    c = _api("PATCH", f"/api/v1/comments/{int(cid)}", req)
    _bump("comment")
    return {"comment": _comment_out(c)}


def mentions(unread=False):
    r = _api("GET", "/api/v1/mentions" + ("?unread=1" if unread else ""))
    out = [{"read": bool(x.get("read")), "comment": _comment_out(x.get("comment") or {}),
            "label": label((x.get("comment") or {}).get("coll"), (x.get("comment") or {}).get("item_id"))}
           for x in r.get("mentions") or []]
    out.sort(key=lambda x: (x["read"], -(x["comment"].get("id") or 0)))
    return {"mentions": out, "unread": sum(1 for x in out if not x["read"])}


def mentions_read(ids):
    r = _api("POST", "/api/v1/mentions/read", {"ids": [int(i) for i in ids or []]})
    _bump("mention")
    return r


# ---------- activity feed ----------

def _merge_events(events, new):
    """Add new events; an edit_session that grew replaces its earlier copy (same data.session)."""
    for e in new:
        if not isinstance(e, dict):
            continue
        sess = (e.get("data") or {}).get("session") if e.get("kind") == "edit_session" else None
        if sess is not None:
            events[:] = [x for x in events if not (x.get("kind") == "edit_session" and (x.get("data") or {}).get("session") == sess)]
        events[:] = [x for x in events if x.get("seq") != e.get("seq")]
        events.append(e)
    events.sort(key=lambda x: x.get("seq") or 0)
    del events[:-FEED_KEEP]


def _refresh_feed():
    with _lock:
        f = team._load("feed.json", {})
    seq, events = int(f.get("seq") or 0), list(f.get("events") or [])
    start = seq
    # the network calls run outside the lock
    for _ in range(200):
        r = _api("GET", f"/api/v1/events?since={seq}&limit=1000")
        if r.get("reset") and seq:
            seq, events = 0, []
            continue
        got = r.get("events") or []
        _merge_events(events, got)
        nseq = r.get("seq")
        seq = nseq if isinstance(nseq, int) and not isinstance(nseq, bool) and nseq >= seq else seq
        if len(got) < 1000:
            break
    with _lock:
        cur = team._load("feed.json", {})
        if int(cur.get("seq") or 0) <= seq or int(cur.get("seq") or 0) == start:  # never step back over a newer save
            team._save("feed.json", {"seq": seq, "events": events})
    return events


def _labeller():
    """label(coll, id) that reads each draft collection once."""
    cache, root = {}, team._root()

    def get(coll, iid):
        if coll not in cache:
            try:
                cache[coll] = {r["id"]: r["data"] for r in draftstore.items(coll, root)}
            except ValueError:
                cache[coll] = {}
        return label(coll, iid, cache[coll].get(str(iid)) or {"_": None})
    return get


def feed(person=None, area=None, limit=200):
    """The team's activity, newest first: edits (one row per edit session), pushes, publishes, comments, claims, joins."""
    events = _refresh_feed()
    lab = _labeller()
    out = []
    for e in reversed(events):
        if person not in (None, "", "all") and str(e.get("actor")) != str(person):
            continue
        if area not in (None, "", "all") and e.get("area") != area and not (area == "multi" and e.get("area") == "multi"):
            continue
        x = copy.deepcopy(e)
        x["who"] = _member(e.get("actor"))
        name = x["who"]["name"]
        if name and str(x.get("summary") or "").startswith(name + " "):
            x["summary"] = x["summary"][len(name) + 1:]  # the row already says who
        if e.get("coll") and e.get("id"):
            x["label"] = lab(e["coll"], e["id"])
        for it in (x.get("data") or {}).get("items") or []:
            if isinstance(it, dict) and it.get("coll") and it.get("id"):
                it["label"] = lab(it["coll"], it["id"])
        out.append(x)
        if len(out) >= int(limit or 200):
            break
    return {"events": out, "areas": sorted({e.get("area") for e in events if e.get("area")})}


def report(kind, area, summary, coll=None, iid=None, data=None):
    """Tell the team about a push / publish / undo (client-reported event)."""
    body = {"kind": kind, "area": area, "summary": str(summary)[:300]}
    if coll:
        body["coll"] = coll
    if iid:
        body["id"] = str(iid)
    if data:
        body["data"] = data
    return _api("POST", "/api/v1/events", body)


# what the user's own live buttons did, told to the team (POST path -> kind, area, what happened). One table: drop a
# line when another part of Studio reports that action itself (or have it return "team_event": true).
# Areas mark results that changed nothing real: live false (dry run), demo true, test_mode true.
ACTIONS = {
    "/api/live/push": ("push", "ads", "pushed Ads changes live"),
    "/api/live/undo": ("undo", "ads", "undid a live Ads push"),
    "/api/area/seo/publish": ("publish", "seo", "published SEO changes to WordPress"),
    "/api/area/seo/undo": ("undo", "seo", "undid an SEO publish"),
    "/api/area/email/send_now": ("publish", "email", "sent an email campaign"),
    "/api/area/email/schedule": ("publish", "email", "scheduled an email campaign"),
    "/api/area/social/post_now": ("publish", "social", "posted on social media"),
    "/api/area/social/schedule": ("publish", "social", "scheduled a social post"),
}


def after_post(path, body, result):
    """server.py, after a POST answered: a push / publish / send that went through becomes a team event (in the
    background; never slows or breaks the button)."""
    a = ACTIONS.get(str(path).split("?")[0])
    if not a or not isinstance(result, dict) or result.get("ok") is False or result.get("error") or result.get("problems"):
        return
    if result.get("live") is False or result.get("demo") or result.get("test_mode") or result.get("needs_approval") \
            or result.get("team_event"):
        return  # a dry run, demo data, nothing really happened, or the event was already posted (approvals)
    if not team.joined() or team._state().get("removed"):
        return
    kind, area, summary = a
    body = body if isinstance(body, dict) else {}
    coll, iid = None, None
    if area == "ads" and body.get("acct"):
        with contextlib.suppress(Exception):
            import core  # noqa: PLC0415
            import meta_ads  # noqa: PLC0415
            acct = body["acct"]
            coll = "ads:" + (acct if meta_ads.is_meta(acct) and str(acct).startswith("meta_") else str(core.resolve(acct)))
    elif area in ("email", "social") and body.get("id"):
        coll, iid = area, str(body["id"])
    if coll and not team.COLL_RE.match(coll):
        coll = None
    if iid and not team.ID_RE.match(iid):
        iid = None

    def go():
        with contextlib.suppress(Exception):
            report(kind, area, summary, coll, iid if coll else None)
    threading.Thread(target=go, daemon=True).start()


# ---------- one call for the page ----------

def state():
    """Everything the page's team layer needs at start: me, members, presence, claims, unread mentions."""
    st = team.status()
    if not st.get("joined") or st.get("removed"):
        return {"joined": bool(st.get("joined")), "removed": bool(st.get("removed"))}
    me = st["me"]
    mem = st.get("members") or {}
    with contextlib.suppress(team.TeamError):
        mem = team._refresh_members()  # someone may have joined since
    out = {"joined": True, "removed": False, "me": me, "team": st.get("team"), "ping": ping(),
           "members": [{"id": int(k) if str(k).isdigit() else k, **v} for k, v in mem.items()]}
    for k, fn in (("presence", lambda: presence()["presence"]), ("claims", lambda: claims()["claims"]),
                  ("unread", _bell_unread)):
        try:
            out[k] = fn()
        except (team.TeamError, ValueError) as e:
            out[k] = [] if k != "unread" else 0
            out["error"] = str(e)
    return out


def _bell_unread():
    """The top bar's number: unread notifications (team_notify.py), or unread @mentions on an older server."""
    import team_notify  # noqa: PLC0415
    return team_notify.unread()


# ---------- HTTP routes (server.py: /api/team/live/<name>) ----------

def _name(path):
    return re.sub(r"[?#].*$", "", path).rstrip("/").rsplit("/", 1)[-1]


def get(path, q):
    n = _name(path)
    routes = {
        "state": state, "ping": ping, "marks": lambda: marks(q.get("coll") or None),
        "presence": presence, "claims": claims, "feed": lambda: feed(q.get("person"), q.get("area"), int(q.get("limit") or 200)),
        "comments": lambda: comments(q["coll"], q["id"]), "counts": lambda: counts(q.get("colls") or ""),
        "mentions": lambda: mentions(q.get("unread") in ("1", "true")),
    }
    if n not in routes:
        raise ValueError("no such team route")
    return routes[n]()


def post(path, body):
    n = _name(path)
    routes = {
        "presence": lambda: presence_beat(body.get("area"), body.get("coll"), body.get("id"), body.get("editing")),
        "claim": lambda: claim(body["coll"], body["id"], body.get("note")),
        "release": lambda: release(body["coll"], body["id"]),
        "comment": lambda: comment_add(body["coll"], body["id"], body.get("body"), body.get("parent"), body.get("mentions")),
        "comment_edit": lambda: comment_edit(body["cid"], body.get("body"), body.get("resolved")),
        "mentions_read": lambda: mentions_read(body.get("ids")),
        "report": lambda: report(body["kind"], body.get("area") or "ads", body.get("summary") or "", body.get("coll"), body.get("id")),
    }
    if n not in routes:
        raise ValueError("no such team route")
    return routes[n]()
