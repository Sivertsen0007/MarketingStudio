"""Team reviews on the Studio side: approvals, push gating, push by person, the why note and owners
(teamserver/PROTOCOL.md, "Reviews"). Only active when this Mac is in a team; solo users see no change.

  Send for approval   request(): my changeset (the server collects its items), a selection of draft items, or a whole
                      post / page / campaign (group). Studio syncs first, so the versions asked about are the server's.
  Review              view(): the items with the same old / new lines the area's Push or Publish preview shows, per-item
                      and review comments, Approve / Request changes (decide()).
  Gating              plan(area, entries, scope): when the team's policy requires approval for the area, only items whose
                      local copy hashes the same as a current (not stale) approval are allowed; the rest are listed as
                      "needs approval". scope "mine" keeps only my own changes ("all" = everything allowed). Offline with
                      the policy on: nothing is allowed (fail closed).
  Why note            every push in a team needs a one-line why (require_why()); report_push() posts it as a `push` event.
  Owners              owner_note(scopes): "Owned by Alex" when a scope I edit is someone else's. Advice only.

Each area keeps its own Push / Publish / Send / Schedule; it calls into here through small hooks
(gated_preview / gated_push for lists, single_action for one item).
"""
import contextlib
import hashlib
import json
import urllib.parse

import draftstore
import team

AREAS = ("ads", "email", "seo", "social")
CONTENT_SKIP = ("at", "ts", "by", "updated", "updated_at", "edited_at", "edited_by", "problems", "warnings",
                "approval", "approved_by", "approved_at")  # the server's list (teamserver/reviews.py)
AREA_LABEL = {"ads": "Ads", "email": "Email", "seo": "SEO", "social": "SoMe"}
MAX_WHY = 200


def content_hash(data):
    """The server's content hash (teamserver/reviews.py): bookkeeping keys left out; "" for no item."""
    if not isinstance(data, dict):
        return ""
    d = {k: v for k, v in data.items() if k not in CONTENT_SKIP}
    raw = json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def joined():
    try:
        return team.joined() and not team._state().get("removed")
    except Exception:  # noqa: BLE001 - a broken team folder must never stop a push for a solo user
        return False


def _api(method, path, body=None):
    return team._api(method, path, body)


def _root():
    return team._root()


def _me():
    return team._state().get("member") or {}


def _names(ids=()):
    mem = team.members()
    if any(str(i) not in mem for i in ids if i is not None):
        with contextlib.suppress(team.TeamError):
            mem = team._refresh_members()
    return {k: v.get("name") or f"member {k}" for k, v in mem.items()}


def area_of(coll):
    return str(coll or "").split(":", 1)[0]


# ---------- policy, approvals, owners ----------

def policy():
    """{require_approval: {area: bool}, offline}. The last answer is kept, so an offline Mac still knows the rules."""
    if not joined():
        return {"require_approval": {a: False for a in AREAS}, "offline": False}
    try:
        p = _api("GET", "/api/v1/policy").get("require_approval") or {}
        p = {a: bool(p.get(a)) for a in AREAS}
        team._update_state(review_policy=p)
        return {"require_approval": p, "offline": False}
    except team.TeamError as e:
        cached = team._state().get("review_policy") or {}
        return {"require_approval": {a: bool(cached.get(a)) for a in AREAS}, "offline": True, "error": str(e)}


def set_policy(body):
    req = body.get("require_approval")
    if not isinstance(req, dict):
        raise ValueError("require_approval must be {area: true|false}")
    r = _api("PUT", "/api/v1/policy", {"require_approval": {a: bool(v) for a, v in req.items()}})
    team._update_state(review_policy=r.get("require_approval") or {})
    return r


def approvals():
    """{"coll\\tid": [{review, hash, title}]} from approved reviews whose items are not stale on the server."""
    out = {}
    for rv in _api("GET", "/api/v1/reviews?status=approved&current=1").get("reviews") or []:
        for it in rv.get("items") or []:
            if not it.get("stale"):
                out.setdefault(team._key(it["coll"], it["id"]), []).append(
                    {"review": rv["id"], "hash": it.get("hash") or "", "title": rv.get("title") or ""})
    return out


def owners():
    """{scope: {id, name, colour, me}} (empty when not in a team or offline)."""
    if not joined():
        return {}
    try:
        raw = _api("GET", "/api/v1/owners").get("owners") or {}
    except team.TeamError:
        return {}
    mem, me = team.members(), _me().get("id")
    if any(str(m) not in mem for m in raw.values()):
        with contextlib.suppress(team.TeamError):
            mem = team._refresh_members()
    return {s: {"id": mid, "name": (mem.get(str(mid)) or {}).get("name") or f"member {mid}",
                "colour": (mem.get(str(mid)) or {}).get("colour") or "#888888", "me": mid == me}
            for s, mid in raw.items()}


def set_owners(body):
    o = body.get("owners")
    if not isinstance(o, dict):
        raise ValueError("owners must be {scope: member id or null}")
    return _api("PUT", "/api/v1/owners", {"owners": o})


def owner_note(scopes):
    """The first scope (most specific first) owned by someone else -> {owner, scope, note}; else {}."""
    own = owners()
    for s in scopes or []:
        o = own.get(s)
        if o:
            return {} if o["me"] else {"owner": o["name"], "colour": o["colour"], "scope": s, "note": f"Owned by {o['name']}"}
    return {}


def reviewers():
    """Members who may approve (approver flag, admin or owner), not me."""
    me = _me().get("id")
    out = []
    for m in _api("GET", "/api/v1/members").get("members") or []:
        if m.get("active") and m.get("id") != me and (m.get("approver") or m.get("role") in ("owner", "admin")):
            out.append({"id": m["id"], "name": m.get("name") or m.get("email"), "colour": m.get("colour"),
                        "role": m.get("role"), "approver": bool(m.get("approver"))})
    return {"reviewers": out}


# ---------- what an item is: label, group, person, preview lines ----------

def _short(v, n=300):
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    return s if len(s) <= n else s[:n - 3] + "..."


def label_of(coll, data):
    d = data or {}
    if coll.startswith("ads:"):
        return d.get("summary") or d.get("id") or ""
    if coll == "seo:changes":
        return f"{d.get('post_title') or d.get('post_id')}: {d.get('field', '')}"
    if coll == "seo:new_posts":
        return "New page: " + str(d.get("title") or "")
    if coll == "email":
        f = d.get("fields") or {}
        return f.get("subject") or f.get("name") or "Email draft"
    if coll == "social":
        return d.get("title") or _short(d.get("caption") or "", 80) or "Post"
    return ""


def group_of(coll, data):
    """The whole post / page / campaign an item belongs to: (key, label)."""
    d = data or {}
    if coll.startswith("ads:"):
        c = d.get("change") or {}
        k = c.get("campaign") or c.get("campaign_id") or c.get("campaign_draft") or ""
        return str(k), (f"Campaign {k}" if k else "Account-wide")
    if coll == "seo:changes":
        return str(d.get("post_id")), str(d.get("post_title") or d.get("post_id"))
    return None, None


def lines_of(coll, data):
    """Old / new rows for the review screen, like the area's Push / Publish preview: [{label, old, new}]."""
    d = data or {}
    if data is None:
        return [{"label": "Removed", "old": "", "new": "(removed from the draft)"}]
    if coll.startswith("ads:"):
        rows = [{"label": "Change", "old": "", "new": d.get("summary") or ""}]
        if d.get("note"):
            rows.append({"label": "Note", "old": "", "new": d["note"]})
        for k, v in (d.get("change") or {}).items():
            if k != "type":
                rows.append({"label": k.replace("_", " "), "old": "", "new": _short(v)})
        return rows
    if coll == "seo:changes":
        return [{"label": d.get("field") or "", "old": _short(d.get("old") or "", 400), "new": _short(d.get("new") or "", 400)}]
    if coll == "seo:new_posts":
        return [{"label": k.replace("_", " "), "old": "", "new": _short(d.get(k) or "", 400)}
                for k in ("title", "slug", "seo_title", "meta_desc", "focus_kw", "status", "content") if d.get(k)]
    if coll == "email":
        return [{"label": k.replace("_", " "), "old": "", "new": _short(v, 400)} for k, v in (d.get("fields") or {}).items()]
    if coll == "social":
        rows = [{"label": k, "old": "", "new": _short(d.get(k), 400)} for k in ("title", "caption", "channels", "when", "link")
                if d.get(k)]
        if d.get("media"):
            rows.append({"label": "media", "old": "", "new": f"{len(d['media'])} file(s)"})
        return rows
    return [{"label": k, "old": "", "new": _short(v)} for k, v in d.items()]


def _person(updated_by):
    me = _me().get("name") or ""
    return "me" if updated_by in ("", None, "you", "claude", me) else str(updated_by)


# ---------- gating ----------

def entries_of(colls):
    """Every live local item of these collections: [{coll, id, label, group, group_label, person, hash}]."""
    out, root = [], _root()
    for coll in colls:
        try:
            recs = draftstore.items(coll, root)
        except (ValueError, OSError):
            continue
        for r in recs:
            g, gl = group_of(coll, r["data"])
            out.append({"coll": coll, "id": r["id"], "label": label_of(coll, r["data"]), "group": g, "group_label": gl,
                        "person": _person(r.get("updated_by")), "hash": content_hash(r["data"])})
    return out


def _single_entry(coll, iid):
    data, _ = draftstore.peek(coll, iid, _root())
    recs = [] if data is None else [r for r in draftstore.items(coll, _root()) if r["id"] == str(iid)]
    who = recs[0].get("updated_by") if recs else ""
    g, gl = group_of(coll, data)
    return {"coll": coll, "id": str(iid), "label": label_of(coll, data) or f"{coll} {iid}", "group": g, "group_label": gl,
            "person": _person(who), "hash": content_hash(data), "exists": data is not None}


def plan(area, entries, scope=None):
    """Which of these items may go in this push. -> {joined, required, scope, allowed: ["coll\\tid"], needs, people,
    why_required, offline, error}. Not in a team: everything is allowed and nothing else changes."""
    scope = "mine" if scope == "mine" else "all"
    keys = [team._key(e["coll"], e["id"]) for e in entries]
    if not joined():
        return {"joined": False, "required": False, "scope": "all", "allowed": keys, "needs": [], "people": [],
                "why_required": False}
    pol = policy()
    required = bool(pol["require_approval"].get(area))
    out = {"joined": True, "required": required, "scope": scope, "why_required": True, "offline": pol.get("offline", False),
           "area": area, "needs": [], "error": ""}
    allowed = list(entries)
    if required:
        try:
            cov = approvals()
        except team.TeamError as e:
            cov, out["error"] = {}, f"Can't check approvals with the team server ({e}). Nothing can go out until it can."
        allowed = []
        for e in entries:
            key = team._key(e["coll"], e["id"])
            ok = any(a["hash"] == e["hash"] for a in cov.get(key, []))
            if ok:
                allowed.append(e)
            else:
                out["needs"].append({"coll": e["coll"], "id": e["id"], "label": e["label"], "group": e.get("group"),
                                     "group_label": e.get("group_label"), "person": e["person"],
                                     "reason": "changed since it was approved" if key in cov else "needs approval"})
    people = {}
    for e in allowed:
        people[e["person"]] = people.get(e["person"], 0) + 1
    out["people"] = [{"name": "You" if p == "me" else p, "mine": p == "me", "count": n}
                     for p, n in sorted(people.items(), key=lambda x: (x[0] != "me", x[0]))]
    if scope == "mine":
        allowed = [e for e in allowed if e["person"] == "me"]
    out["allowed"] = [team._key(e["coll"], e["id"]) for e in allowed]
    out["count"] = len(out["allowed"])
    return out


def only_ids(p, coll):
    """The item ids of one collection a plan allows, or None when nothing is filtered (solo, or policy off + all)."""
    if not p.get("joined") or (not p.get("required") and p.get("scope") != "mine"):
        return None
    pre = coll + "\t"
    return {k[len(pre):] for k in p["allowed"] if k.startswith(pre)}


def public(p):
    return {k: v for k, v in p.items() if k != "allowed"}


def require_why(body):
    """In a team every push carries a one-line why (sent to the feed). -> the note, or "" when not in a team."""
    if not joined():
        return ""
    why = " ".join(str(body.get("why") or "").split())
    if not why:
        raise ValueError("Add a one-line note on why (your team sees it in the activity feed)")
    return why[:MAX_WHY]


def report_push(area, what, why, counts=None, items=None, scope="all", kind="push"):
    """Post the push to the team feed. Best effort: a push that went out is never undone by a failed report."""
    if not joined():
        return False
    summary = f"{what}: {why}" if why else what
    data = {"why": why, "counts": counts or {}, "items": [{"coll": c, "id": i} for c, i in (items or [])][:200],
            "scope": scope}
    try:
        _api("POST", "/api/v1/events", {"kind": kind, "area": area, "summary": summary[:500], "data": data})
        return True
    except team.TeamError:
        return False


def gated_preview(area, coll, body, fn):
    """Push / Publish preview for a list area. fn(only) -> the area's preview dict; the plan goes in res["team"]."""
    if not joined():
        return fn(None)
    p = plan(area, entries_of([coll]), body.get("scope"))
    res = fn(only_ids(p, coll))
    if isinstance(res, dict) and p["joined"]:
        res["team"] = public(p)
    return res


def gated_push(area, coll, body, fn, what):
    """The push itself: same plan as the preview, the why note required, reported to the feed when it went out."""
    if not joined():
        return fn(None)
    p = plan(area, entries_of([coll]), body.get("scope"))
    why = require_why(body)
    only = only_ids(p, coll)
    if only is not None and not only:
        return {"ok": False, "team": public(p), "needs_team_approval": bool(p["needs"]),
                "message": "Nothing to push: " + ("none of the changes has a current approval."
                                                  if p["required"] else "none of the changes is yours.")}
    res = fn(only)
    if isinstance(res, dict) and p["joined"]:
        res["team"] = public(p)
        if res.get("ok"):
            n = res.get("applied") if isinstance(res.get("applied"), int) else len(p["allowed"])
            pushed = [k.split("\t", 1) for k in p["allowed"] if only is None or k.split("\t", 1)[1] in only]
            res["reported"] = report_push(area, f"{what} ({len(pushed)} change{'s' if len(pushed) != 1 else ''})", why,
                                          {"items": len(pushed), "operations": n}, pushed, p["scope"])
    return res


def filter_lists(area, lists, body):
    """For areas with several collections in one push (SEO: edits and new pages). lists: [(coll, [item dicts with id])]
    -> ([filtered list per coll], plan)."""
    if not joined():
        return [list(xs) for _, xs in lists], {"joined": False, "required": False, "scope": "all"}
    p = plan(area, entries_of([c for c, _ in lists]), body.get("scope"))
    out = []
    for coll, xs in lists:
        only = only_ids(p, coll)
        out.append(list(xs) if only is None else [x for x in xs if str(x.get("id")) in only])
    return out, p


def gate(area, coll, iid, scope=None):
    """The plan for one item (an email campaign, a SoMe post), for the dialog before Send / Schedule / Post."""
    return public(plan(area, [_single_entry(coll, iid)], scope))


def single_action(area, coll, iid, body, what, fn):
    """Send / Schedule / Post now for one item: gated, why required, reported when it went out."""
    if not joined():
        return fn()
    p = plan(area, [_single_entry(coll, iid)])
    why = require_why(body)
    if p["required"] and not p["allowed"]:
        n = p["needs"][0] if p["needs"] else {}
        msg = p.get("error") or ("This changed since it was approved: send it for approval again."
                                  if n.get("reason", "").startswith("changed") else
                                  "Your team needs an approval for this first: send it for approval.")
        return {"ok": False, "needs_team_approval": True, "message": msg, "team": public(p)}
    label = _single_entry(coll, iid)["label"]  # before fn: Send removes the email draft
    res = fn()
    if isinstance(res, dict) and res.get("ok"):
        res["reported"] = report_push(area, f"{what}: {label}", why, {"items": 1}, [(coll, str(iid))])
    if isinstance(res, dict):
        res["team"] = public(p)
    return res


# ---------- requests and decisions ----------

def group_items(group):
    """A whole post / page / campaign -> [(coll, id)]. group: {area, key, coll?}."""
    area, key = group.get("area"), str(group.get("key") or "")
    if area == "email":
        return [("email", key)]
    if area == "social":
        return [("social", key)]
    colls = [group["coll"]] if group.get("coll") else (["seo:changes", "seo:new_posts"] if area == "seo" else [])
    out = []
    for e in entries_of(colls):
        if e["group"] == key or (e["coll"] == "seo:new_posts" and e["id"] == key):
            out.append((e["coll"], e["id"]))
    if not out:
        raise ValueError("Nothing in the draft for that " + ("campaign" if area == "ads" else "post"))
    return out


def _versions(pairs):
    """[(coll, id)] -> [{coll, id, version}] from the last sync. Refuses an item whose local copy is not on the server."""
    synced = team._load("synced.json", {})
    out = []
    for coll, iid in pairs:
        iid = str(iid)
        data, _ = draftstore.peek(coll, iid, _root())
        s = synced.get(team._key(coll, iid))
        if data is None:
            v = int(s["v"]) if s else 0
        elif s and not s.get("deleted") and content_hash(s.get("base")) == content_hash(data):
            v = int(s["v"])
        else:
            raise ValueError(f"{label_of(coll, data) or iid} has changes the team server doesn't have yet (a conflict, or "
                             "refused): sort that out in Sync first")
        out.append({"coll": coll, "id": iid, "version": v})
    return out


def request(body):
    """Send for approval. body: {changeset: true | cs id} or {items: [{coll, id}]} or {group: {area, key, coll?}},
    plus reviewers [member id], note, title. Syncs first. -> the review."""
    if not joined():
        raise ValueError("Join a team first")
    if team._role() == "viewer":
        raise ValueError("Viewers can't ask for reviews")
    rv = body.get("reviewers")
    if not isinstance(rv, list) or not rv:
        raise ValueError("Pick at least one reviewer")
    s = team.sync()
    if s.get("offline") or s.get("removed"):
        raise ValueError(s.get("message") or "Can't reach the team server")
    out = {"reviewers": [int(x) for x in rv], "note": str(body.get("note") or "")[:500].strip()}
    if body.get("title"):
        out["title"] = str(body["title"])[:200]
    if body.get("area"):
        out["area"] = str(body["area"])[:50]
    if body.get("changeset"):
        cid = body["changeset"] if isinstance(body["changeset"], str) else None
        if cid and not any(x["id"] == cid for x in team.changeset_get()["list"]):
            raise ValueError("No such changeset of yours")
        cs = team._cs_for(cid)
        out.update(changeset=cs, from_changeset=True)
    else:
        if body.get("group"):
            pairs = group_items(body["group"])
        else:
            items = body.get("items")
            if not isinstance(items, list) or not items:
                raise ValueError("Nothing picked to send for approval")
            pairs = [(str(x["coll"]), str(x["id"])) for x in items]
        out["items"] = _versions(pairs)
        out["changeset"] = team._cs_for(None)
    return _decorate(_api("POST", "/api/v1/reviews", out))


def decide(body):
    d = body.get("decision")
    if d not in ("approve", "request_changes"):
        raise ValueError("decision must be approve or request_changes")
    return _decorate(_api("POST", f"/api/v1/reviews/{int(body['id'])}/decide", {"decision": d, "note": str(body.get("note") or "")[:500]}))


def withdraw(body):
    return _decorate(_api("POST", f"/api/v1/reviews/{int(body['id'])}/withdraw", {}))


def _fresh_me():
    """Me as the server sees me now (the approver flag or role may have changed since joining)."""
    with contextlib.suppress(team.TeamError):
        m = _api("GET", "/api/v1/me").get("member")
        if isinstance(m, dict):
            return m
    return _me()


def _decorate(rv, me=None):
    me = me or _fresh_me()
    names = _names([rv.get("requested_by"), rv.get("decided_by")] + list(rv.get("reviewers") or []))
    rv["requested_by_name"] = names.get(str(rv.get("requested_by")), f"member {rv.get('requested_by')}")
    rv["reviewer_names"] = [names.get(str(m), f"member {m}") for m in rv.get("reviewers") or []]
    rv["decided_by_name"] = names.get(str(rv.get("decided_by")), "") if rv.get("decided_by") else ""
    rv["mine"] = rv.get("requested_by") == me.get("id")
    rv["can_decide"] = (rv.get("status") == "open" and me.get("id") in (rv.get("reviewers") or []) and not rv["mine"]
                        and (bool(me.get("approver")) or me.get("role") in ("owner", "admin")))
    return rv


def list_reviews(q):
    """q.filter: to_me (open, waiting for me), mine (my requests), all. -> {reviews, counts}."""
    if not joined():
        return {"joined": False, "reviews": []}
    f = q.get("filter") or "to_me"
    path = {"to_me": "/api/v1/reviews?to_me=1&status=open", "mine": "/api/v1/reviews?mine=1"}.get(f, "/api/v1/reviews")
    with contextlib.suppress(team.TeamError):
        team._refresh_members()
    me = _fresh_me()
    revs = [_decorate(r, me) for r in _api("GET", path).get("reviews") or []]
    return {"joined": True, "reviews": revs, "me": _me(), "changeset": team.changeset_get()["active"]}


def counts():
    """For the top-bar badge: open reviews waiting for me."""
    if not joined():
        return {"joined": False, "to_me": 0}
    try:
        return {"joined": True, "to_me": len(_api("GET", "/api/v1/reviews?to_me=1&status=open").get("reviews") or [])}
    except team.TeamError as e:
        return {"joined": True, "to_me": 0, "error": str(e)}


def view(q):
    """One review with each item's old / new lines from my local copy (synced first when it is behind)."""
    rv = _api("GET", f"/api/v1/reviews/{int(q['id'])}")
    synced = team._load("synced.json", {})
    if any(int((synced.get(team._key(i["coll"], i["id"])) or {}).get("v", 0)) < i["current_version"] for i in rv["items"]):
        with contextlib.suppress(team.TeamError, ValueError):
            team.sync()
    items = []
    for i in rv["items"]:
        try:
            data, _ = draftstore.peek(i["coll"], i["id"], _root())
        except ValueError:
            data = None
        same = content_hash(data) == i.get("hash")
        items.append({**i, "label": label_of(i["coll"], data) or f"{i['coll']} {i['id']}", "area": area_of(i["coll"]),
                      "lines": lines_of(i["coll"], data), "local_matches": same})
    rv["items"] = items
    return _decorate(rv)


def comments(q):
    coll, iid = q.get("coll") or "review", str(q.get("id") or "")
    names = _names()
    out = _api("GET", "/api/v1/comments?" + urllib.parse.urlencode({"coll": coll, "id": iid})).get("comments") or []
    for c in out:
        c["author_name"] = names.get(str(c.get("author")), f"member {c.get('author')}")
    return {"comments": out}


def comment(body):
    return _api("POST", "/api/v1/comments", {"coll": str(body.get("coll") or "review"), "id": str(body["id"]),
                                              "body": str(body.get("body") or "")})


# ---------- engine routes: /api/team/reviews/<name> ----------

def get_route(path, q):
    name = path.rsplit("/", 1)[-1]
    fn = {"list": list_reviews, "counts": lambda q: counts(), "view": view, "policy": lambda q: policy(),
          "owners": lambda q: {"owners": owners(), "me": _me()}, "reviewers": lambda q: reviewers(),
          "comments": comments, "owner_note": lambda q: owner_note([s for s in (q.get("scopes") or "").split("|") if s]),
          "gate": lambda q: gate(q.get("area"), q.get("coll"), q.get("id"), q.get("scope"))}.get(name)
    if not fn:
        raise ValueError("no such reviews route")
    return fn(q)


def post_route(path, body):
    name = path.rsplit("/", 1)[-1]
    fn = {"request": request, "decide": decide, "withdraw": withdraw, "comment": comment, "policy": set_policy,
          "owners": set_owners}.get(name)
    if not fn:
        raise ValueError("no such reviews route")
    return fn(body)
