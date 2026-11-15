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
        cached = team._state().get("review_policy")
        if not isinstance(cached, dict):  # never seen the rules: live actions wait until they can be checked
            return {"require_approval": {a: True for a in AREAS}, "offline": True, "unknown": True, "error": str(e)}
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
        if rv.get("decider_ok") is False:
            continue  # the approver has lost the right to approve since
        for it in rv.get("items") or []:
            if not it.get("stale"):
                h = it.get("content_hash") if it["coll"] in CONTENT else it.get("hash")  # never trust a hash meant elsewhere
                out.setdefault(team._key(it["coll"], it["id"]), []).append(
                    {"review": rv["id"], "hash": h or "", "title": rv.get("title") or ""})
    return out


def changes_requested():
    """{"coll\tid": [{review, hash}]}: content someone asked changes for. While an item is still exactly that content it
    stays out of every push, even when another review approved it; a change to it (or withdrawing that review) lifts it."""
    out = {}
    for rv in _api("GET", "/api/v1/reviews?status=changes_requested&current=1").get("reviews") or []:
        for it in rv.get("items") or []:
            if not it.get("stale"):
                h = it.get("content_hash") if it["coll"] in CONTENT else it.get("hash")
                out.setdefault(team._key(it["coll"], it["id"]), []).append({"review": rv["id"], "hash": h or ""})
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
            colour = m.get("colour") if team.COLOUR_RE.match(str(m.get("colour") or "")) else "#888888"
            out.append({"id": m["id"], "name": m.get("name") or m.get("email"), "colour": colour,
                        "role": m.get("role"), "approver": bool(m.get("approver"))})
    return {"reviewers": out}


# ---------- content that is checked as it will really go out ----------

def _email_content(iid):
    """An email campaign as a Send would send it: the platform copy (fetched fresh) with any Studio draft on top."""
    from areas import email as em  # noqa: PLC0415
    return em.team_content(iid)


# coll -> fn(id) -> dict: for these an approval covers the content that goes out, not only the Studio draft item, so
# edits on the platform or a draft saved to the platform outside Send are caught at send time.
CONTENT = {"email": _email_content}


def content_of(coll, iid):
    try:
        return CONTENT[coll](iid)
    except Exception as e:  # noqa: BLE001 - can't read it: it can't be approved or sent as approved
        raise ValueError(f"Can't read {coll} {iid} to check it: {e}") from None


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
        f = d.get("fields") or d
        return f.get("subject") or f.get("name") or "Email campaign"
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


def _full(v):
    """A value as text, in full (never cut: the approver confirms exactly what is shown)."""
    if v is None:
        return ""
    return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str, indent=1)


def _row(label, new, old="", kind="text"):
    return {"label": label, "old": _full(old), "new": _full(new), "kind": kind}


def _rest(d, shown):
    """Every other field of the item (bookkeeping keys aside), so nothing the hash covers goes unseen."""
    return [_row(k.replace("_", " "), v) for k, v in d.items()
            if k not in shown and k not in CONTENT_SKIP and v not in (None, "", [], {})]


def _social_rows(d):
    """Per channel, what that channel receives: built with the same function Post now / Schedule use (build_job)."""
    from areas import social as so  # noqa: PLC0415
    from areas import _social_publish as pub  # noqa: PLC0415
    from areas import _social_store as sst  # noqa: PLC0415
    rows = []
    for ch in d.get("channels") or []:
        lab = sst.CHANNEL_LABEL.get(ch, ch)
        try:
            job = pub.build_job(d, ch, so.media_lookup, so.tracker(d))
            media = [f"{m.get('kind', '')} {Path_name(m.get('path'))} ({m.get('media_id')})" for m in job.get("media") or []]
        except Exception:  # noqa: BLE001 - media not on this Mac: the same fields, without the file lookup
            job = {"text": sst.caption_for(d, ch), "link": d.get("link", "") if ch != "instagram" else "",
                   "title": d.get("title", ""), "alt_text": d.get("alt_text", ""), "location": d.get("location", ""),
                   "first_comment": d.get("first_comment", "") if ch == "instagram" else "", "format": d.get("format") or "text"}
            media = [f"{m.get('kind', '')} {m.get('name') or m.get('file') or ''} ({m.get('media_id')})" for m in d.get("media") or []]
        rows.append(_row(f"{lab}: text", job.get("text")))
        for k in ("link", "title", "first_comment", "alt_text", "location", "format"):
            if job.get(k):
                rows.append(_row(f"{lab}: {k.replace('_', ' ')}", job[k]))
        rows.append(_row(f"{lab}: media", f"{len(media)} file(s)" + ("\n" + "\n".join(media) if media else "")))
    return rows


def Path_name(p):
    return str(p or "").rsplit("/", 1)[-1]


def lines_of(coll, data):
    """Rows for the review screen [{label, old, new, kind}]: every field the area sends live, in full (kind "html" is
    shown rendered in a sandbox with a source toggle), then every other field of the item. Nothing is shortened."""
    d = data or {}
    if data is None:
        return [_row("Removed", "(removed from the draft)")]
    if coll.startswith("ads:"):
        c = d.get("change") or {}
        rows = [_row("Change", d.get("summary") or ""), _row("Type", c.get("type") or "")]
        if d.get("note"):
            rows.append(_row("Note", d["note"]))
        rows += [_row(k.replace("_", " "), v) for k, v in c.items() if k != "type"]
        return rows + _rest(d, {"summary", "note", "change", "id"})
    if coll == "seo:changes":
        f = d.get("field") or ""
        rows = [_row("Post", f"{d.get('post_title') or ''} (post id {d.get('post_id')})"),
                _row(f, d.get("new"), d.get("old"), "html" if f == "content" else "text")]
        return rows + _rest(d, {"field", "old", "new", "post_title", "post_id", "id"})
    if coll == "seo:new_posts":
        keys = ("title", "slug", "seo_title", "meta_desc", "focus_kw", "status", "excerpt", "categories", "node_title", "content")
        rows = [_row(k.replace("_", " "), d.get(k), kind="html" if k == "content" else "text") for k in keys if d.get(k) not in (None, "", [])]
        return rows + _rest(d, set(keys) | {"id"})
    if coll == "email":
        f = d.get("fields") if isinstance(d.get("fields"), dict) else d
        return [_row(k.replace("_", " "), v, kind="html" if k == "html" else "text") for k, v in f.items()
                if k != "campaign_id" and v not in (None, "")]
    if coll == "social":
        return _social_rows(d) + [_row(k, d.get(k)) for k in ("channels", "when") if d.get(k)] + \
            _rest(d, {"channels", "when", "id"})
    return _rest(d, set())


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
    h, label = content_hash(data), label_of(coll, data)
    if coll in CONTENT:
        c = content_of(coll, iid)
        h, label = content_hash(c), label_of(coll, c)
    return {"coll": coll, "id": str(iid), "label": label or f"{coll} {iid}", "group": g, "group_label": gl,
            "person": _person(who), "hash": h, "exists": data is not None}


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
            if pol.get("unknown"):
                raise team.TeamError("unknown rules")
            cov, held = approvals(), changes_requested()
        except team.TeamError as e:
            cov, held = {}, {}
            out["error"] = ("Can't check the team's approval rules: reconnect and try again." if pol.get("unknown") else
                            f"Can't check approvals with the team server ({e}). Nothing can go out until it can.")
        allowed = []
        for e in entries:
            key = team._key(e["coll"], e["id"])
            ok = any(a["hash"] == e["hash"] for a in cov.get(key, []))
            held_now = any(a["hash"] == e["hash"] for a in held.get(key, []))  # someone asked for changes to this
            if ok and not held_now:
                allowed.append(e)
            else:
                out["needs"].append({"coll": e["coll"], "id": e["id"], "label": e["label"], "group": e.get("group"),
                                     "group_label": e.get("group_label"), "person": e["person"],
                                     "reason": "changes requested" if held_now else
                                     "changed since it was approved" if key in cov else "needs approval"})
    people = {}
    for e in allowed:
        people[e["person"]] = people.get(e["person"], 0) + 1
    out["people"] = [{"name": "You" if p == "me" else p, "mine": p == "me", "count": n}
                     for p, n in sorted(people.items(), key=lambda x: (x[0] != "me", x[0]))]
    if scope == "mine":
        allowed = [e for e in allowed if e["person"] == "me"]
    out["allowed"] = [team._key(e["coll"], e["id"]) for e in allowed]
    out["_hashes"] = {team._key(e["coll"], e["id"]): e["hash"] for e in allowed}
    out["count"] = len(out["allowed"])
    return out


class Only(dict):
    """The ids a push may include -> the content hash the plan saw. admits(item) is False for an item that changed
    since the plan (the push re-reads the draft; an item edited in between is left out, never pushed unchecked)."""

    def admits(self, item):
        iid = str((item or {}).get("id"))
        return iid in self and content_hash(item) == self[iid]


def only_ids(p, coll):
    """The item ids of one collection a plan allows, or None when nothing is filtered (solo, or policy off + all)."""
    if not p.get("joined") or (not p.get("required") and p.get("scope") != "mine"):
        return None
    pre = coll + "\t"
    return Only({k[len(pre):]: p["_hashes"][k] for k in p["allowed"] if k.startswith(pre)})


def admits(only, item):
    """For the push functions: is this draft item in the push? only None = everything."""
    if only is None:
        return True
    return only.admits(item) if hasattr(only, "admits") else item.get("id") in only


def public(p):
    return {k: v for k, v in p.items() if k not in ("allowed", "_hashes")}


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


def gated_push(area, coll, body, fn, what, kind="push"):
    """The push itself: same plan as the preview, the why note required, reported to the feed when it went out."""
    if not joined():
        return fn(None)
    p = plan(area, entries_of([coll]), body.get("scope"))
    why = require_why(body)
    only = only_ids(p, coll)
    if only is not None and not only:
        msg = p.get("error") or ("Nothing to push: " + ("none of the changes has a current approval."
                                                        if p["required"] else "none of the changes is yours."))
        return {"ok": False, "team": public(p), "needs_team_approval": bool(p["needs"]), "message": msg, "output": msg}
    res = fn(only)
    if isinstance(res, dict) and p["joined"]:
        res["team"] = public(p)
        if res.get("ok"):
            n = res.get("applied") if isinstance(res.get("applied"), int) else len(p["allowed"])
            pushed = [k.split("\t", 1) for k in p["allowed"] if only is None or k.split("\t", 1)[1] in only]
            res["reported"] = report_push(area, f"{what} ({len(pushed)} change{'s' if len(pushed) != 1 else ''})", why,
                                          {"items": len(pushed), "operations": n}, pushed, p["scope"], kind)
            res["team_event"] = res["reported"]  # the feed already has it (ts-04's after_post skips these)
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
        out.append(list(xs) if only is None else [x for x in xs if admits(only, x)])
    return out, p


def gate(area, coll, iid, scope=None):
    """The plan for one item (an email campaign, a SoMe post), for the dialog before Send / Schedule / Post."""
    return public(plan(area, [_single_entry(coll, iid)], scope))


def single_action(area, coll, iid, body, what, fn):
    """Send / Schedule / Post now for one item: gated, why required, reported when it went out."""
    if not joined():
        return fn()
    body.pop("_team_expect", None)
    entry = _single_entry(coll, iid)
    p = plan(area, [entry], body.get("scope"))
    why = require_why(body)
    if not p["allowed"]:
        n = p["needs"][0] if p["needs"] else {}
        if p["required"]:
            msg = p.get("error") or ("This changed since it was approved: send it for approval again."
                                      if n.get("reason", "").startswith("changed") else
                                      "Your team needs an approval for this first: send it for approval.")
        else:
            msg = "This is someone else's change: pick Everything (not Only mine) to include it."
        return {"ok": False, "needs_team_approval": bool(p["required"]), "message": msg, "team": public(p)}
    if p["required"]:
        body["_team_expect"] = entry["hash"]  # the area checks it again right before it sends (recheck())
    label = entry["label"]  # before fn: Send removes the email draft
    res = fn()
    if isinstance(res, dict) and res.get("ok"):
        res["reported"] = report_push(area, f"{what}: {label}", why, {"items": 1}, [(coll, str(iid))])
        res["team_event"] = res["reported"]
    if isinstance(res, dict):
        res["team"] = public(p)
    return res


def recheck(body, coll, iid, data=None):
    """Right before an area sends: is the item still the approved content? -> "" or why not. data: the item as the
    area has it now (CONTENT colls are read again from the platform)."""
    want = body.get("_team_expect")
    if not want:
        return ""
    try:
        now = content_hash(content_of(coll, iid) if coll in CONTENT else data)
    except ValueError as e:
        return str(e)
    return "" if now == want else "It changed after it was approved (in Studio or on the platform): send it for approval again."


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
        entry = {"coll": coll, "id": iid, "version": v}
        if coll in CONTENT:
            ch = content_hash(content_of(coll, iid))
            if not ch:
                raise ValueError(f"{coll} {iid}: there is nothing to review")
            entry["content_hash"] = ch
        elif data is None:
            raise ValueError(f"{coll} {iid} is not in the draft: there is nothing to review")
        out.append(entry)
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
    rid = int(body["id"])
    out = {"decision": d, "note": str(body.get("note") or "")[:500]}
    if d == "approve":
        # Every item: the hash of exactly what the review screen showed (the UI sends view()'s shown_hash back), or,
        # without a screen (tests, scripts), of my synced copy now. The server only accepts what was asked about.
        v = view({"id": rid})
        if any(i.get("stale") for i in v["items"]):
            # Changed since it was asked about: approving now would cover content nobody reviewed. Nothing is sent;
            # the review stays open (ask for changes, or the requester asks again).
            v["refused"] = "Some items changed after the request, so this review can't be approved. Ask for changes, or ask the requester to send it again."
            return v
        shown = body.get("shown")
        if isinstance(shown, list) and shown:
            out["confirm"] = [{"coll": str(x.get("coll")), "id": str(x.get("id")), "content_hash": str(x.get("hash") or "")}
                              for x in shown if isinstance(x, dict)]
        else:
            out["confirm"] = [{"coll": i["coll"], "id": i["id"], "content_hash": i["shown_hash"]} for i in v["items"]]
    return _decorate(_api("POST", f"/api/v1/reviews/{rid}/decide", out))


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
        if i["coll"] in CONTENT:
            try:
                data = content_of(i["coll"], i["id"])
            except ValueError:
                data = None
        shown = content_hash(data)  # what this screen shows; Approve confirms exactly this
        same = shown == (i.get("content_hash") if i["coll"] in CONTENT else i.get("hash"))
        items.append({**i, "label": label_of(i["coll"], data) or f"{i['coll']} {i['id']}", "area": area_of(i["coll"]),
                      "lines": lines_of(i["coll"], data), "local_matches": same, "shown_hash": shown})
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
