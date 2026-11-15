"""Marketing Studio: SEO area (masterliveaboards.com blog posts + Google Search Console + GA4).

See it: posts grid with Yoast fields, internal links, Search Console and GA4 organic numbers.
Build a draft: you (the post editor) or Claude (add_draft, suggestions). Check it: copy rules, lengths, links.
Make it real: only the user's own Publish button. Default writer is a dry run; WordPress writes need the
Marketing Studio Application Password . divetravel.co.uk stays off until the director agrees.

For an MCP tool later: list_drafts() and add_draft(change, by). There is deliberately no publish function for Claude.
"""
import datetime as dt
import hashlib
import json
import os
import time
import uuid

import draftstore
import progress

from . import _seo_checks as chk
from . import _seo_common as c
from . import _seo_google as g
from . import _seo_wp as wp
from . import _seo_map as tmap

LABEL = "SEO"
PKEY = "seo"  # progress key: GET /api/progress?key=seo
STEPS = ("Checking", "Saving revision note", "Updating", "Re-inspecting")
DRAFT = "seo_draft.json"
HISTORY = "seo_history.jsonl"
SITES = [{"id": "ml", "name": "masterliveaboards.com", "on": True, "note": "WordPress on Kinsta, Yoast"},
         {"id": "dt", "name": "divetravel.co.uk", "on": False, "note": "Off until the director agrees"}]
EXPECTED_CTR = {3: 0.10, 4: 0.07, 5: 0.05, 6: 0.04, 7: 0.03, 8: 0.025, 9: 0.02, 10: 0.018}


def _now():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _sites():
    """The site list: the SEO sites setting (team_settings, key seo_sites) when it names any, else the built-in one."""
    try:
        import team_settings  # noqa: PLC0415
        return team_settings.get("seo_sites")["sites"] or SITES
    except Exception:  # noqa: BLE001
        return SITES


# ---------- settings + writer ----------

def settings():
    s = c.read_json("seo_settings.json", {})
    user, _ = c.wp_creds()
    mode = s.get("mode", "dry") if user else "dry"
    return {"target": s.get("target", "staging"), "mode": mode, "connected": bool(user), "user": user or ""}


def writer(s=None):
    s = s or settings()
    if s["mode"] == "live" and s["connected"]:
        return wp.WpRestWriter(s["target"])
    return wp.DryRunWriter(s["target"], delay=float(os.environ.get("SEO_DRY_DELAY", "0.25")))


def get_settings(q=None):
    s = settings()
    sc = g.scopes()
    snippet = None
    try:
        snippet = any(p.get("focus_kw") is not None for p in wp.posts()["posts"])
    except Exception:  # noqa: BLE001
        pass
    return {**s, "writer": "WordPress REST" if s["mode"] == "live" and s["connected"] else "Dry run (records only)",
            "secrets_file": str(c.SECRETS), "sites": _sites(), "snippet_installed": snippet,
            "scopes": {"search_console_read": "https://www.googleapis.com/auth/webmasters.readonly" in sc or "https://www.googleapis.com/auth/webmasters" in sc,
                       "url_inspection": "https://www.googleapis.com/auth/webmasters.readonly" in sc or "https://www.googleapis.com/auth/webmasters" in sc,
                       "sitemap_submit": "https://www.googleapis.com/auth/webmasters" in sc,
                       "ga4_read": "https://www.googleapis.com/auth/analytics.readonly" in sc},
            "quota_left": g.quota_left()}


def save_settings(body):
    s = c.read_json("seo_settings.json", {})
    if body.get("target") in ("staging", "prod"):
        s["target"] = body["target"]
    if body.get("mode") in ("dry", "live"):
        if body["mode"] == "live" and not c.wp_creds()[0]:
            raise ValueError("Connect the Marketing Studio WordPress user first.")
        s["mode"] = body["mode"]
    with c.locked_json("seo_settings.json", {}) as d:
        d.clear()
        d.update(s)
    return get_settings()


def _test_login(user, password, target):
    st, _, res = c.http(f"{c.SITES[target]}/wp-json/wp/v2/users/me?context=edit&_fields=id,name,roles",
                        headers={"Authorization": wp._basic(user, password)})
    if st != 200:
        msg = c.err_text(res).replace(password, "***")
        raise RuntimeError(f"WordPress {target} did not accept the login ({st}): {msg}")
    return res


def connect(body):
    """Save WP_STUDIO_USER + WP_STUDIO_APP_PASSWORD after a test login. The password is never returned or logged."""
    user, pw = (body.get("user") or "").strip(), (body.get("password") or "").strip()
    target = body.get("target") if body.get("target") in ("staging", "prod") else settings()["target"]
    me = _test_login(user, pw, target)
    roles = me.get("roles") or []
    if roles and not set(roles) & {"editor", "administrator"}:
        raise RuntimeError(f"That user's role is {', '.join(roles)}. It needs Editor.")
    c.save_wp_creds(user, pw)
    c.clear_cache("posts")
    return {"ok": True, "user": user, "roles": roles, "message": f"Connected as {me.get('name') or user} on {target}."}


def disconnect(body=None):
    c.forget_wp_creds()
    with c.locked_json("seo_settings.json", {}) as d:
        d["mode"] = "dry"
    c.clear_cache("posts")
    return get_settings()


# ---------- posts grid ----------

def _safe(fn, errors, label):
    try:
        return fn()
    except Exception as ex:  # noqa: BLE001 - one source failing must not hide the grid
        errors.append(f"{label}: {c.scrub(ex)}")
        return {}


def losing(cur, prev):
    pc, cc = (prev or {}).get("clicks", 0), (cur or {}).get("clicks", 0)
    return pc >= 10 and cc <= pc * 0.7 and pc - cc >= 5


def get_posts(q):
    s, e, ps, pe = g.date_range(q)
    force = q.get("force") in ("1", "true")
    data = wp.posts(force=force)
    errors = []
    cur = _safe(lambda: g.gsc_pages(s, e, force), errors, "Search Console")
    prev = _safe(lambda: g.gsc_pages(ps, pe, force), errors, "Search Console (previous period)")
    ga = _safe(lambda: g.ga4_landing(s, e, force), errors, "GA4")
    insp = c.read_json("seo_inspect.json", {})
    drafted = {}
    for ch in _draft()["changes"]:
        drafted.setdefault(str(ch["post_id"]), []).append(ch)
    rows = []
    for p in data["posts"]:
        m, pm, a = cur.get(p["key"]) or {}, prev.get(p["key"]) or {}, ga.get(p["key"]) or {}
        ix = insp.get(p["url"])
        r = {k: v for k, v in p.items() if k not in ("content", "rendered", "linked_from", "links_to")}
        r.update({"clicks": m.get("clicks", 0), "impressions": m.get("impressions", 0), "ctr": m.get("ctr", 0), "position": m.get("position"),
                  "prev_clicks": pm.get("clicks", 0), "delta_clicks": m.get("clicks", 0) - pm.get("clicks", 0),
                  "losing": losing(m, pm), "sessions": a.get("sessions", 0), "select_cabin": a.get("select_cabin", 0),
                  "index": (ix or {}).get("coverage"), "indexed": g.indexed(ix) if ix else None,
                  "draft": [{"field": x["field"], "old": x.get("old"), "new": x.get("new"), "kind": x.get("kind")} for x in drafted.get(str(p["id"]), [])]})
        rows.append(r)
    return {"rows": rows, "range": {"start": s, "end": e, "prev_start": ps, "prev_end": pe}, "authed": data.get("authed", False),
            "fetched": data.get("at"), "errors": errors, "snippet_installed": any(p.get("focus_kw") is not None for p in data["posts"]),
            "settings": settings()}


def _qp_for(key, qp):
    return [r for r in qp if r[1] == key]


def cannibalisation(key, qp, top=10, titles=None):
    mine = sorted(_qp_for(key, qp), key=lambda r: -r[3])[:top]
    by_q = {}
    for r in qp:
        by_q.setdefault(r[0], []).append(r)
    out = []
    for q, _, clicks, imps, ctr, pos in mine:
        if imps < 5:
            continue
        others = [{"key": o[1], "title": (titles or {}).get(o[1], ""), "clicks": o[2], "impressions": o[3], "position": o[5]}
                  for o in by_q.get(q, []) if o[1] != key and o[3] >= max(5, 0.1 * imps)]
        if others:
            out.append({"query": q, "this": {"clicks": clicks, "impressions": imps, "position": pos}, "others": sorted(others, key=lambda x: -x["impressions"])})
    return out


def get_post(q):
    p = dict(wp.post(q["id"]))
    s, e, ps, pe = g.date_range(q)
    errors = []
    qp = _safe(lambda: g.gsc_query_page(s, e), errors, "Search Console") or []
    all_posts = wp.posts()["posts"]
    titles = {x["key"]: x["title"] for x in all_posts}
    queries = sorted(([r[0], r[2], r[3], r[4], r[5]] for r in _qp_for(p["key"], qp)), key=lambda r: -r[2])[:100]
    st = settings()
    if st["connected"]:
        try:  # edit from the WordPress source, not the published HTML
            cur = wp.WpRestWriter("prod").current(p["id"])
            p["content"], p["content_base"] = cur["content"], "raw"
            p["excerpt"] = cur["excerpt"]
        except Exception as ex:  # noqa: BLE001
            errors.append(f"WordPress source: {c.scrub(ex)}")
    p.pop("rendered", None)
    rel = [{"id": x["id"], "title": x["title"], "url": x["url"], "shared": sh} for _, x, sh in wp.related(p, all_posts, 5)]
    return {"post": p, "queries": [dict(zip(("query", "clicks", "impressions", "ctr", "position"), r)) for r in queries],
            "cannibalisation": cannibalisation(p["key"], qp, titles=titles), "inspection": g.inspected(p["url"]),
            "inspection_link": g.inspection_link(p["url"]), "related": rel,
            "linked_from": [{"id": x["id"], "title": x["title"]} for x in all_posts if x["id"] in p.get("linked_from", [])],
            "draft": [x for x in _draft()["changes"] if x["post_id"] == p["id"]], "range": {"start": s, "end": e},
            "map_nodes": [{"id": nid, "title": n["title"], "type": n.get("type"), "queries": n.get("queries", [])}
                          for nid, n in tmap.load()["nodes"].items() if tmap._key(n.get("url")) == tmap._key(p["url"])],
            "errors": errors, "quota_left": g.quota_left()}


def get_categories(q=None):
    return wp.categories()


def get_sitemaps(q=None):
    return {"sitemaps": g.sitemaps(), "can_submit": get_settings()["scopes"]["sitemap_submit"],
            "note": "Submitting a sitemap needs the full webmasters scope. Today Studio has read-only access."}


# ---------- URL Inspection ----------

def do_inspect(body):
    if body.get("all"):
        ps = [p for p in wp.posts()["posts"] if p["status"] == "publish"]
        todo = [p for p in ps if not (g.inspected(p["url"]) and time.time() - g.inspected(p["url"])["at"] < g.INSPECT_TTL)]
        todo = todo[:max(0, g.quota_left())]
        progress.start(PKEY, "inspect", [(p["id"], p["title"], 1) for p in todo], phase="sending", message="Checking index status")
        n_bad, n_err = 0, 0
        for p in todo:
            try:
                r = g.inspect(p["url"])
                progress.sent(PKEY, p["id"])
                n_bad += 0 if g.indexed(r) else 1
            except Exception:  # noqa: BLE001
                n_err += 1
                progress.mark(PKEY, p["id"], "failed")
        progress.finish(PKEY, n_err == 0, f"{len(todo)} checked, {n_bad} not indexed, {n_err} failed")
        return {"checked": len(todo), "not_indexed": n_bad, "failed": n_err, "quota_left": g.quota_left(), "skipped_cached": len(ps) - len(todo)}
    p = wp.post(body["id"])
    return g.inspect(p["url"], force=bool(body.get("force")))


# ---------- opportunities ----------

def _min_imps(days):
    return 30 if days <= 7 else 100 if days <= 30 else 250


def opportunities_from(posts_, qp, cur, prev, insp, days):
    by_key = {p["key"]: p for p in posts_}
    out = {"low_ctr": [], "losing": [], "not_indexed": [], "no_meta": [], "few_links": []}
    for q, key, clicks, imps, ctr, pos in qp:
        if 3 <= pos <= 10.5 and imps >= _min_imps(days):
            exp = EXPECTED_CTR[min(10, max(3, round(pos)))]
            if ctr < 0.6 * exp:
                p = by_key.get(key)
                out["low_ctr"].append({"query": q, "key": key, "post_id": p and p["id"], "title": p and p["title"], "clicks": clicks,
                                       "impressions": imps, "ctr": ctr, "position": pos, "missed": round(imps * (exp - ctr)),
                                       "suggest": p and {"post_id": p["id"], "field": "seo_title",
                                                         "note": f'Low CTR for "{q}" at position {pos}: {imps} impressions, {clicks} clicks. '
                                                                 "Rewrite the SEO title and meta description to answer this query."}})
    out["low_ctr"].sort(key=lambda r: -r["missed"])
    out["low_ctr"] = out["low_ctr"][:60]
    for key, pm in prev.items():
        m = cur.get(key) or {}
        if losing(m, pm):
            p = by_key.get(key)
            out["losing"].append({"key": key, "post_id": p and p["id"], "title": p and p["title"], "clicks": m.get("clicks", 0),
                                  "prev_clicks": pm["clicks"], "delta": m.get("clicks", 0) - pm["clicks"], "position": m.get("position"),
                                  "prev_position": pm.get("position"),
                                  "suggest": p and {"post_id": p["id"], "field": "content",
                                                    "note": f"Clicks fell from {pm['clicks']} to {m.get('clicks', 0)}. Refresh the post: check facts, "
                                                            "dates and prices, and cover the queries it lost."}})
    out["losing"].sort(key=lambda r: r["delta"])
    out["losing"] = out["losing"][:60]
    for p in posts_:
        if p.get("status") != "publish":
            continue
        ix = insp.get(p["url"])
        if ix and not g.indexed(ix):
            out["not_indexed"].append({"post_id": p["id"], "title": p["title"], "url": p["url"], "coverage": ix.get("coverage"),
                                       "last_crawl": ix.get("last_crawl"), "link": ix.get("link") or g.inspection_link(p["url"])})
        if not (p.get("meta_desc") or "").strip():
            out["no_meta"].append({"post_id": p["id"], "title": p["title"], "impressions": (cur.get(p["key"]) or {}).get("impressions", 0),
                                   "suggest": {"post_id": p["id"], "field": "meta_desc",
                                               "note": "No meta description: Google writes its own. Write one of 70-160 characters."}})
        if p.get("links_in", 0) == 0:
            rel = wp.related(p, posts_, 2)
            out["few_links"].append({"post_id": p["id"], "title": p["title"], "links_in": 0,
                                     "from": [{"id": x["id"], "title": x["title"], "shared": sh} for _, x, sh in rel],
                                     "suggest": [{"post_id": x["id"], "field": "content",
                                                  "note": f'Add an internal link to "{p["title"]}" ({p["url"]}). Shared topic: {", ".join(sh)}.'}
                                                 for _, x, sh in rel]})
    out["no_meta"].sort(key=lambda r: -r["impressions"])
    out["not_inspected"] = sum(1 for p in posts_ if p.get("status") == "publish" and p["url"] not in insp)
    return out


def get_opportunities(q):
    s, e, ps, pe = g.date_range(q)
    days = (dt.date.fromisoformat(e) - dt.date.fromisoformat(s)).days + 1
    errors = []
    posts_ = wp.posts()["posts"]
    qp = _safe(lambda: g.gsc_query_page(s, e), errors, "Search Console") or []
    cur = _safe(lambda: g.gsc_pages(s, e), errors, "Search Console")
    prev = _safe(lambda: g.gsc_pages(ps, pe), errors, "Search Console (previous period)")
    out = opportunities_from(posts_, qp, cur, prev, c.read_json("seo_inspect.json", {}), days)
    return {**out, "range": {"start": s, "end": e, "prev_start": ps, "prev_end": pe}, "errors": errors}


# ---------- the SEO draft ----------

def _draft():
    """{version, changes, new_posts?} (draftstore collections seo:changes and seo:new_posts, both in DRAFT)."""
    return draftstore.read("seo:changes", c.data_dir(), {"version": 0, "changes": []})


def _locked_draft(by=None):
    """The draft file under its lock; callers bump its version themselves, as before."""
    return draftstore.doc("seo:changes", c.data_dir(), by=by, default={"version": 0, "changes": []})


def _norm(f, v):
    if f == "categories":
        return sorted(int(x) for x in (v or []))
    return (v or "").strip() if isinstance(v, str) or v is None else v


def add_draft(change, by="claude"):
    """Add one change to the SEO draft (never to WordPress). change: {post_id, field, new} or a suggestion
    {post_id, field, note, kind: "todo"}. Same post + field replaces the earlier edit and keeps the original old value."""
    pid, f = int(change["post_id"]), change.get("field")
    if f not in wp.FIELDS:
        raise ValueError(f"Field must be one of {', '.join(wp.FIELDS)}.")
    p = wp.post(pid)
    kind = "todo" if change.get("kind") == "todo" else "edit"
    if kind == "edit" and "new" not in change:
        raise ValueError("An edit needs a new value. Use kind todo for a suggestion without text.")
    with _locked_draft(by) as d:
        if kind == "todo":
            if any(x for x in d["changes"] if x["post_id"] == pid and x["field"] == f and x.get("kind") == "todo" and x.get("note") == change.get("note")):
                return {"ok": True, "duplicate": True, "version": d["version"]}
            row = {"id": uuid.uuid4().hex[:10], "post_id": pid, "post_title": p["title"], "url": p["url"], "field": f, "kind": "todo",
                   "note": (change.get("note") or "")[:600], "by": by, "at": _now()}
            d["changes"].append(row)
        else:
            old = next((x["old"] for x in d["changes"] if x["post_id"] == pid and x["field"] == f and x.get("kind") != "todo"), None)
            if old is None:
                old = change.get("old", p.get(f))
            d["changes"] = [x for x in d["changes"] if not (x["post_id"] == pid and x["field"] == f and x.get("kind") != "todo")]
            new = _norm(f, change["new"])
            row = None
            if new != _norm(f, old):
                row = {"id": uuid.uuid4().hex[:10], "post_id": pid, "post_title": p["title"], "url": p["url"], "field": f, "kind": "edit",
                       "old": old, "new": new, "base": change.get("base") or (p.get("content_base") if f == "content" else None),
                       "note": (change.get("note") or "")[:600], "by": by, "at": _now()}
                d["changes"].append(row)
        d["version"] += 1
        v = d["version"]
    if not row:
        return {"ok": True, "unchanged": True, "version": v}
    return {"ok": True, "change": row, "issues": chk.check_change(row, link_check=lambda u: 200), "version": v}


def list_drafts():
    """The SEO draft with quick checks per change (links are checked in Check and before Publish)."""
    d = _draft()
    return {"version": d["version"], "changes": [{**x, "label": wp.LABELS.get(x["field"], x["field"]),
                                                   "issues": chk.check_change(x, link_check=lambda u: 200)} for x in d["changes"]],
            "new_posts": [{**x, "issues": _new_checks(x)} for x in d.get("new_posts", [])]}


def remove_drafts(ids):
    ids = set(ids or [])
    with _locked_draft() as d:
        d["changes"] = [x for x in d["changes"] if x["id"] not in ids]
        d["version"] += 1
    return list_drafts()


def clear_drafts(body=None):
    with _locked_draft() as d:
        d["changes"] = []
        d["new_posts"] = []
        d["version"] += 1
    return list_drafts()


def post_add(body):
    out = [add_draft(ch, by="you") for ch in body.get("changes") or []]
    return {"results": out, "draft": list_drafts()}


# ---------- new pages (written for a topical-map node, created in WordPress on Publish) ----------

NEW_FIELDS = ("title", "slug", "seo_title", "meta_desc", "focus_kw", "excerpt", "categories", "content")


def slugify(s):
    import re  # noqa: PLC0415
    import unicodedata  # noqa: PLC0415
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:80]


def _new_checks(np_):
    out = []
    for f in NEW_FIELDS:
        v = np_.get(f)
        if f in ("title", "slug", "categories") or (v not in (None, "") and f != "focus_kw"):
            out += chk.check_change({"field": f, "new": v if v is not None else "", "old": "", "kind": "edit"}, link_check=lambda u: 200)
    if not wp.text_of(np_.get("content") or "").strip():
        out.append({"level": "error", "field": "content", "msg": "The body is empty."})
    return out


def save_new_post(body, by="claude"):
    """Create or update a NEW page in the draft (never in WordPress). body: {id?, node_id, fields...}.
    One new page per node: saving for a node that already has one updates it."""
    nodes = tmap.load()["nodes"]
    nid = body.get("node_id") or ""
    if nid and nid not in nodes:
        raise ValueError("That topical-map node does not exist.")
    vals = {f: body[f] for f in NEW_FIELDS if f in body}
    if "categories" in vals:
        vals["categories"] = sorted(int(x) for x in vals["categories"] or [])
    status = body.get("status")
    if status is not None and status not in ("publish", "draft"):
        raise ValueError("status is publish or draft.")
    with _locked_draft(by) as d:
        lst = d.setdefault("new_posts", [])
        row = next((x for x in lst if (body.get("id") and x["id"] == body["id"]) or (not body.get("id") and nid and x.get("node_id") == nid)), None)
        if row is None:
            if body.get("id"):
                raise ValueError("That new page is no longer in the draft.")
            if not (vals.get("title") or "").strip():
                raise ValueError("A new page needs a title.")
            row = {"id": "n" + uuid.uuid4().hex[:9], "node_id": nid, "status": "publish", "by": by, "at": _now(),
                   **{f: ([] if f == "categories" else "") for f in NEW_FIELDS}}
            lst.append(row)
        row.update(vals)
        if status:
            row["status"] = status
        if not row.get("slug"):
            row["slug"] = slugify(row.get("title"))
        row["node_title"] = nodes.get(row.get("node_id"), {}).get("title", "")
        row["edited_by"], row["edited_at"] = by, _now()
        if body.get("note") is not None:
            row["note"] = (body.get("note") or "")[:600]
        d["version"] += 1
        out = dict(row)
    return {"ok": True, "new_post": {**out, "issues": _new_checks(out)}}


def remove_new_post(body):
    with _locked_draft() as d:
        d["new_posts"] = [x for x in d.get("new_posts", []) if x["id"] != body.get("id")]
        d["version"] += 1
    return list_drafts()


def check_all(body=None, link_check=None):
    d = _draft()
    return {"changes": [{**x, "label": wp.LABELS.get(x["field"], x["field"]), "issues": chk.check_change(x, link_check)} for x in d["changes"]],
            "version": d["version"]}


# ---------- publish (the user's button only) ----------

def _token(edits, s, w, new_posts=()):
    blob = json.dumps([[x["id"], x["new"]] for x in edits] + [[x["id"], {f: x.get(f) for f in NEW_FIELDS + ("status",)}] for x in new_posts]
                      + [s["target"], w.name], sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _new_fields(np_):
    return {f: np_.get(f) for f in NEW_FIELDS if np_.get(f) not in (None, "", []) or f in ("title", "content", "slug")}


def _team_gate(edits, news, body):
    """[team reviews] In a team: only approved edits (when the team requires it) or only mine. -> (edits, news, plan)."""
    import team_reviews  # noqa: PLC0415
    (edits, news), plan = team_reviews.filter_lists("seo", [("seo:changes", edits), ("seo:new_posts", news)], body or {})
    return edits, news, plan


def publish_preview(body=None, link_check=None):
    """The change set per post, with checks and a token the Publish call must send back."""
    s = settings()
    w = writer(s)
    d = _draft()
    edits = [x for x in d["changes"] if x.get("kind") != "todo"]
    news = d.get("new_posts", [])
    edits, news, team_plan = _team_gate(edits, news, body)
    by_post = {}
    for x in edits:
        by_post.setdefault(x["post_id"], {"post_id": x["post_id"], "title": x["post_title"], "url": x["url"], "fields": [], "issues": []})
        b = by_post[x["post_id"]]
        b["fields"].append({"id": x["id"], "field": x["field"], "label": wp.LABELS.get(x["field"], x["field"]), "old": x.get("old"), "new": x["new"]})
        b["issues"] += chk.check_change(x, link_check)
    posts_ = list(by_post.values())
    for x in news:
        posts_.append({"post_id": x["id"], "new": True, "status": x.get("status", "publish"), "node_title": x.get("node_title", ""),
                       "title": x["title"], "url": f"{c.SITES[s['target']]}/{x.get('slug')}/",
                       "fields": [{"id": x["id"], "field": f, "label": wp.LABELS.get(f, f), "old": "", "new": v} for f, v in _new_fields(x).items()],
                       "issues": _new_checks(x)})
    errs = sum(chk.summarise(p["issues"])["errors"] for p in posts_)
    out = {"target": s["target"], "writer": w.name, "live": w.live, "posts": posts_, "errors": errs, "new_pages": len(news),
           "warnings": sum(chk.summarise(p["issues"])["warnings"] for p in posts_), "blocked": errs > 0 or not posts_,
           "todos": sum(1 for x in d["changes"] if x.get("kind") == "todo"), "token": _token(edits, s, w, news)}
    if team_plan.get("joined"):
        import team_reviews  # noqa: PLC0415
        out["team"] = team_reviews.public(team_plan)
    return out


def _same(f, a, b):
    if f == "content":
        return wp.text_of(a or "") == wp.text_of(b or "")
    return _norm(f, a) == _norm(f, b)


def _base_value(x, cur, w):
    """What the draft's old value must still match. A body drafted from the published HTML is compared with the
    published HTML (content.rendered) when writing for real, because the WordPress source looks different."""
    if x["field"] == "content" and x.get("base") == "rendered" and w.live:
        return cur.get("_rendered")
    return cur.get(x["field"])


def _fingerprint(f, v):
    if f == "content":
        v = wp.text_of(v or "")
    return hashlib.sha256(json.dumps(_norm(f, v), default=str).encode()).hexdigest()[:16]


def publish(body, link_check=None, w=None):
    """Runs only from the user's Publish button (token from publish_preview). Progress per post: Checking, Saving revision note,
    Updating, Re-inspecting. Never called by Claude or the MCP."""
    s = settings()
    w = w or writer(s)
    d = _draft()
    edits = [x for x in d["changes"] if x.get("kind") != "todo"]
    news = d.get("new_posts", [])
    if not edits and not news:
        raise ValueError("Nothing to publish: the draft has no edits.")
    edits, news, team_plan = _team_gate(edits, news, body)
    import team_reviews  # noqa: PLC0415
    why = team_reviews.require_why(body)
    if not edits and not news:
        raise ValueError("Nothing to publish: " + ("no edit has a current approval from your team."
                                                   if team_plan.get("required") else "none of the edits is yours."))
    if body.get("token") != _token(edits, s, w, news):
        raise ValueError("The draft or target changed since the preview. Open Publish again.")
    by_post = {}
    for x in edits:
        by_post.setdefault(x["post_id"], []).append(x)
    issues = [i for x in edits for i in chk.check_change(x, link_check)] + [i for x in news for i in _new_checks(x)]
    if chk.summarise(issues)["errors"]:
        raise ValueError("The draft has errors. Fix them before publishing.")
    push_id = uuid.uuid4().hex[:10]
    progress.start(PKEY, "publish", [(pid, xs[0]["post_title"], len(STEPS)) for pid, xs in by_post.items()]
                   + [(x["id"], "New page: " + x["title"], len(STEPS)) for x in news], phase="sending",
                   message=f"Publishing to {s['target']} ({'dry run' if not w.live else 'WordPress'})")
    results, done_ids = [], []
    new_done = _publish_new(news, s, w, push_id)
    results += new_done
    for pid, xs in by_post.items():
        title, url = xs[0]["post_title"], xs[0]["url"]
        res = {"post_id": pid, "title": title, "url": url, "ok": False, "steps": []}
        try:
            progress.mark(PKEY, pid, "checking")
            progress.phase(PKEY, "sending", f"{title}: {STEPS[0]}")
            cur = w.current(pid)
            moved = [x["field"] for x in xs if not _same(x["field"], x.get("old"), _base_value(x, cur, w))]
            if moved:
                raise RuntimeError(f"Changed in WordPress since you drafted it ({', '.join(wp.LABELS[f] for f in moved)}). Draft again from the current version.")
            progress.sent(PKEY, pid)
            res["steps"].append(STEPS[0])
            progress.phase(PKEY, "sending", f"{title}: {STEPS[1]}")
            before = {x["field"]: cur.get(x["field"]) for x in xs}
            rev = w.save_revision_note(pid, before)
            progress.sent(PKEY, pid)
            res["steps"].append(STEPS[1])
            progress.phase(PKEY, "sending", f"{title}: {STEPS[2]}")
            sent_ = w.update(pid, {x["field"]: x["new"] for x in xs})
            progress.sent(PKEY, pid)
            res["steps"].append(STEPS[2])
            progress.phase(PKEY, "sending", f"{title}: {STEPS[3]}")
            if w.live and s["target"] == "prod":
                try:
                    ix = g.inspect(url, force=True)
                    res["inspection"] = ix.get("coverage")
                except Exception as ex:  # noqa: BLE001 - publishing worked; the re-check is a bonus
                    res["inspection"] = f"Not re-inspected: {c.scrub(ex)}"
            else:
                res["inspection"] = "Skipped: " + ("dry run" if not w.live else "staging is not in Search Console")
            progress.sent(PKEY, pid)
            res["steps"].append(STEPS[3])
            after = {x["field"]: x["new"] for x in xs}
            res.update(ok=True, revision_id=rev, sent=sent_.get("sent"), request_indexing=g.inspection_link(url))
            c.append_jsonl(HISTORY, {"push_id": push_id, "at": _now(), "action": "publish", "target": s["target"], "writer": w.name,
                                     "post_id": pid, "title": title, "url": url, "before": before, "after": after,
                                     "after_hash": {f: _fingerprint(f, v) for f, v in after.items()},
                                     "modified": sent_.get("modified"), "revision_id": rev, "ok": True})
            done_ids += [x["id"] for x in xs]
        except Exception as ex:  # noqa: BLE001 - one post failing does not stop the rest
            res["error"] = c.scrub(ex)
            progress.mark(PKEY, pid, "failed")
        results.append(res)
    if done_ids:
        remove_drafts(done_ids)
    ok_n = sum(1 for r in results if r["ok"])
    ok = ok_n == len(results)
    n_new = sum(1 for r in new_done if r["ok"])
    msg = f"{ok_n} of {len(results)} posts {'recorded (dry run, nothing sent)' if not w.live else 'published'} on {s['target']}" \
          + (f", {n_new} of them new" if new_done else "") + "."
    progress.finish(PKEY, ok, msg)
    team_event = False
    if ok_n and w.live and team_plan.get("joined"):  # [team reviews] the why note goes to the team feed (not a dry run)
        team_event = team_reviews.report_push("seo", f"Published {ok_n} post{'s' if ok_n != 1 else ''} on {s['target']}", why,
                                 {"posts": ok_n, "failed": len(results) - ok_n, "new_pages": n_new},
                                 [("seo:changes", i) for i in done_ids] + [("seo:new_posts", x["id"]) for x, r in zip(news, new_done) if r["ok"]],
                                 team_plan["scope"])
    out = {"ok": ok, "message": msg, "push_id": push_id, "results": results, "live": w.live, "target": s["target"]}
    if team_event:
        out["team_event"] = True
    return out


def _publish_new(news, s, w, push_id):
    """Create each new page in WordPress, point its topical-map node at the new URL, log it so Undo can trash it."""
    out, done = [], []
    for x in news:
        title = x["title"]
        res = {"post_id": x["id"], "title": "New page: " + title, "url": "", "ok": False, "steps": [], "new": True}
        try:
            progress.mark(PKEY, x["id"], "checking")
            progress.phase(PKEY, "sending", f"{title}: Checking")
            if [i for i in _new_checks(x) if i["level"] == "error"]:
                raise RuntimeError("Has errors: fix them first.")
            progress.sent(PKEY, x["id"])
            progress.sent(PKEY, x["id"])  # no revision to note for a page that does not exist yet
            res["steps"] += ["Checking", "Nothing to note (new page)"]
            progress.phase(PKEY, "sending", f"{title}: Creating")
            made = w.create(_new_fields(x), x.get("status", "publish"))
            progress.sent(PKEY, x["id"])
            res["steps"].append("Created as " + ("published" if x.get("status", "publish") == "publish" else "WordPress draft"))
            res["url"] = made.get("link") or ""
            if made.get("warning"):
                res["steps"].append(made["warning"])
            if w.live and x.get("node_id") and res["url"]:
                try:
                    tmap.edit([{"op": "update", "id": x["node_id"], "node": {"url": res["url"]}}], by="studio")
                    res["steps"].append("Map node points at the new URL")
                except Exception as ex:  # noqa: BLE001 - the page exists; the map can be fixed by hand
                    res["steps"].append(f"Map not updated: {c.scrub(ex)}")
            progress.sent(PKEY, x["id"])
            res.update(ok=True, post_id=made.get("id"), request_indexing=g.inspection_link(res["url"]) if res["url"] else "",
                       inspection="Skipped: new page")
            c.append_jsonl(HISTORY, {"push_id": push_id, "at": _now(), "action": "publish", "created": True, "target": s["target"], "writer": w.name,
                                     "post_id": made.get("id"), "title": title, "url": res["url"], "node_id": x.get("node_id"),
                                     "before": {}, "after": _new_fields(x), "modified": made.get("modified"), "ok": True})
            done.append(x["id"])
        except Exception as ex:  # noqa: BLE001
            res["error"] = c.scrub(ex)
            progress.mark(PKEY, x["id"], "failed")
        out.append(res)
    if done:
        with _locked_draft() as d:
            d["new_posts"] = [y for y in d.get("new_posts", []) if y["id"] not in done]
            d["version"] += 1
    return out


def history(q=None):
    rows = c.read_jsonl(HISTORY, 500)
    undone = {r["push_id"] for r in rows if r.get("action") == "undo"}
    pushes = {}
    for r in rows:
        if r.get("action") != "publish":
            continue
        p = pushes.setdefault(r["push_id"], {"push_id": r["push_id"], "at": r["at"], "target": r["target"], "writer": r["writer"],
                                              "posts": [], "undone": r["push_id"] in undone})
        p["posts"].append({"post_id": r["post_id"], "title": r["title"], "fields": sorted(r.get("after") or {}), "new": bool(r.get("created"))})
    return sorted(pushes.values(), key=lambda p: p["at"], reverse=True)[:30]


def _changed_since(r, w):
    """Why this post no longer looks like what Studio published, or "" when it is unchanged."""
    if r.get("created"):  # a page Studio made: only WordPress's modified time can tell (a dry run made nothing)
        if not w.live:
            return ""
        cur = w.current(r["post_id"])
        return f"edited {cur['_modified']}" if r.get("modified") and cur.get("_modified") and cur["_modified"] != r["modified"] else ""
    cur = w.current(r["post_id"])
    if w.live and r.get("modified") and cur.get("_modified"):  # WordPress's own modified time is exact (it may tidy the HTML we sent)
        return f"edited {cur['_modified']}" if cur["_modified"] != r["modified"] else ""
    # live: the site should hold what we sent. Dry run: nothing was sent, so the site should still hold the old values.
    expect = r.get("after") if w.live else r.get("before")
    moved = [f for f, v in (expect or {}).items() if _fingerprint(f, v) != _fingerprint(f, cur.get(f))]
    return f"{', '.join(wp.LABELS.get(f, f) for f in moved)} changed" if moved else ""


def undo(body, w=None):
    """Undo one publish: restore the revision / values saved before it. The user's button only."""
    pid_ = body.get("push_id")
    rows = [r for r in c.read_jsonl(HISTORY, 2000) if r.get("push_id") == pid_ and r.get("action") == "publish"]
    if not rows:
        raise ValueError("That publish was not found.")
    if any(r.get("push_id") == pid_ and r.get("action") == "undo" for r in c.read_jsonl(HISTORY, 2000)):
        raise ValueError("That publish was already undone.")
    wname, target = rows[0]["writer"], rows[0]["target"]
    if w is None:
        w = wp.WpRestWriter(target) if wname == "wp" else wp.DryRunWriter(target, delay=float(os.environ.get("SEO_DRY_DELAY", "0.25")))
    changed = []
    for r in rows:  # check every post first: undo never overwrites an edit made after our publish
        why = _changed_since(r, w)
        if why:
            changed.append(f'{r["title"]} ({why})')
    if changed:
        raise ValueError("Not undone: changed in WordPress since Studio published it, so undo would overwrite that edit: "
                         + "; ".join(changed) + ". Fix it in wp-admin (Revisions) instead.")
    progress.start(PKEY, "undo", [(r["post_id"], r["title"], 1) for r in rows], phase="sending", message=f"Undoing on {target}")
    bad, how = 0, []
    for r in rows:
        try:
            if r.get("created"):  # undo a new page = move it to the WordPress bin (restorable there) and clear the node's URL
                w.trash(r["post_id"])
                how.append("bin")
                if r.get("node_id") and w.live:
                    try:
                        tmap.edit([{"op": "update", "id": r["node_id"], "node": {"url": ""}}], by="studio")
                    except Exception:  # noqa: BLE001,S110 - node may be gone; the page is in the bin either way
                        pass
            else:
                out = w.restore(r["post_id"], r) or {}
                how.append(out.get("from") or "saved values")
            progress.sent(PKEY, r["post_id"])
            progress.mark(PKEY, r["post_id"], "reversed")
        except Exception as ex:  # noqa: BLE001
            bad += 1
            r["error"] = c.scrub(ex)
            progress.mark(PKEY, r["post_id"], "failed")
    c.append_jsonl(HISTORY, {"push_id": pid_, "at": _now(), "action": "undo", "target": target, "writer": wname, "failed": bad})
    edited = [h for h in how if h != "bin"]
    src = "the WordPress revision" if edited and all(h == "revision" for h in edited) else \
        "the values Studio saved before publishing" if "revision" not in edited else "revisions where WordPress had them, otherwise Studio's saved values"
    binned = how.count("bin")
    msg = (f"{len(rows) - bad} of {len(rows)} posts undone" + (f": {len(edited)} restored from {src}" if edited else "")
           + (f"{',' if edited else ':'} {binned} new page{'s' if binned != 1 else ''} moved to the WordPress bin" if binned else "")
           + f"{' (dry run)' if wname != 'wp' else ''}.")
    progress.finish(PKEY, bad == 0, msg)
    return {"ok": bad == 0, "message": msg, "live": wname == "wp"}  # [team live] live: false = a dry run


PARTS = {"gsc": ("Search Console", ("gsc_",)), "ga4": ("GA4", ("ga4_",)), "wp": ("WordPress posts", ("posts", "categories"))}


def refresh(body=None):
    """Drop the cached Search Console, GA4 and/or WordPress data (body.parts: any of gsc, ga4, wp; default all) and
    fetch the default 28-day view again. URL Inspection results are kept (they cost quota). Read-only."""
    parts = [p for p in ((body or {}).get("parts") or list(PARTS)) if p in PARTS]
    n = sum(c.clear_cache(pre) for p in parts for pre in PARTS[p][1])
    errors = []
    s, e, ps, pe = g.date_range({})
    for p in parts:
        fns = {"gsc": lambda: (g.gsc_pages(s, e, True), g.gsc_pages(ps, pe, True)), "ga4": lambda: g.ga4_landing(s, e, True),
               "wp": lambda: wp.posts(force=True)}[p]
        _safe(fns, errors, PARTS[p][0])
    return {"cleared": n, "parts": parts, "errors": errors}


# ---------- health + overview ----------

def _row(channel, account, check, ok, detail, fix=""):
    try:
        import health  # noqa: PLC0415
        return health._row(channel, account, check, ok, detail, fix)
    except Exception:  # noqa: BLE001
        return {"channel": channel, "account": account, "check": check, "ok": ok, "detail": detail, "fix": fix}


def health(today=None):
    out = []
    try:
        st, h, body = c.http(f"{c.SITES['prod']}/wp-json/wp/v2/posts?per_page=1&_fields=id", timeout=20)
        total = {k.lower(): v for k, v in h.items()}.get("x-wp-total")
        out.append(_row("SEO", "masterliveaboards.com", "WordPress REST answers", st == 200,
                        f"{total} published posts readable without login." if st == 200 else f"Answered {st}.", "Check the site on Kinsta."))
    except Exception as ex:  # noqa: BLE001
        out.append(_row("SEO", "masterliveaboards.com", "WordPress REST answers", False, str(ex), "Check the site on Kinsta."))
    user, _ = c.wp_creds()
    if not user:
        out.append(_row("SEO", "masterliveaboards.com", "WordPress write access", None, "Not connected. Publish runs as a dry run.",
                        "the README: create the Marketing Studio Editor user and an Application Password, then SEO > Settings > Connect."))
    else:
        s = settings()
        try:
            me = wp.WpRestWriter(s["target"]).me()
            roles = me.get("roles") or []
            out.append(_row("SEO", f"masterliveaboards.com ({s['target']})", "WordPress write access", bool(set(roles) & {"editor", "administrator"}),
                            f"Logged in as {user}, role {', '.join(roles) or 'unknown'}.", "The user needs the Editor role."))
        except Exception as ex:  # noqa: BLE001
            out.append(_row("SEO", f"masterliveaboards.com ({s['target']})", "WordPress write access", False, c.scrub(ex),
                            "Make a new Application Password and connect again."))
    e = (dt.date.today() - dt.timedelta(days=3)).isoformat()
    try:
        t = g.gsc_totals(e, e)
        out.append(_row("SEO", g.SITE, "Search Console answers", True, f"{t['clicks']} clicks on {e}."))
    except Exception as ex:  # noqa: BLE001
        out.append(_row("SEO", g.SITE, "Search Console answers", False, str(ex)[:300], "Sign in with Google again (Setup)."))
    try:
        t = g.ga4_organic_totals(e, e)
        out.append(_row("SEO", f"GA4 {g.GA4_PROPERTY}", "GA4 answers", True, f"{t['sessions']} organic sessions, {t['select_cabin']} Select Cabin on {e}."))
    except Exception as ex:  # noqa: BLE001
        out.append(_row("SEO", f"GA4 {g.GA4_PROPERTY}", "GA4 answers", False, str(ex)[:300], "Sign in with Google again (Setup)."))
    out.append(_row("SEO", "divetravel.co.uk", "Site", None, "Off until the director agrees. No calls are made.", ""))
    return out


def _page_label(path):
    """'/' -> 'Homepage', '/blog/some-post/' -> 'some-post'."""
    p = str(path or "").split("://")[-1]
    p = p[p.find("/"):] if "/" in p else p
    parts = [x for x in p.split("/") if x]
    return parts[-1] if parts else "Homepage"


def overview(days="7"):
    n = int(days) if str(days) in ("7", "14", "30") else 7
    s, e, ps, pe = g.date_range({"range": n})
    t, tp = g.gsc_totals(s, e), g.gsc_totals(ps, pe)
    ga = g.ga4_organic_totals(s, e)
    cur, prev = g.gsc_pages(s, e), g.gsc_pages(ps, pe)
    moves = sorted(((k, (cur.get(k) or {}).get("clicks", 0) - (prev.get(k) or {}).get("clicks", 0)) for k in set(cur) | set(prev)),
                   key=lambda kv: -abs(kv[1]))
    top = moves[0] if moves else None
    d = t["clicks"] - tp["clicks"]
    return {"title": "SEO", "tiles": [
        {"k": "Organic clicks", "v": f"{t['clicks']:,} ({'+' if d >= 0 else ''}{d:,})", "hi": True},
        {"k": "Impressions", "v": f"{t['impressions']:,}"},
        {"k": "Avg position", "v": f"{t['position']:.1f}"},
        {"k": "Select Cabin (organic)", "v": f"{ga['select_cabin']:,}", "hi": True},
        {"k": "Top mover", "v": f"{_page_label(top[0])} ({'+' if top[1] >= 0 else ''}{top[1]})" if top else "None"}],
        "note": f"Search Console {s} to {e} (data lags 2 days) vs the {n} days before. Select Cabin: GA4 select_cabin events in organic search sessions."}


# ---------- topical map (Koray) joined with Search Console + GA4 ----------

def get_map(q):
    """The topical map with live numbers, issues, coverage and unmapped demand (see _seo_map.report)."""
    s_, e, ps, pe = g.date_range(q)
    force = q.get("force") in ("1", "true")
    errors = []
    cur = _safe(lambda: g.gsc_pages(s_, e, force), errors, "Search Console")
    prev = _safe(lambda: g.gsc_pages(ps, pe, force), errors, "Search Console (previous period)")
    qp = _safe(lambda: g.gsc_query_page(s_, e, force), errors, "Search Console queries") or []
    ga = _safe(lambda: g.ga4_landing(s_, e, force), errors, "GA4")
    posts = []
    try:
        posts = wp.posts().get("posts", [])
    except Exception as ex:  # noqa: BLE001 - the map still works without the link graph
        errors.append(f"WordPress: {ex}")
    r = tmap.report(cur or {}, prev or {}, qp if isinstance(qp, list) else [], ga or {}, posts, c.read_json("seo_inspect.json", {}),
                    min_imps=_min_imps((dt.date.fromisoformat(e) - dt.date.fromisoformat(s_)).days + 1))
    by_key = {p["key"]: p for p in posts}
    d = _draft()
    edits = {}
    for x in d["changes"]:
        edits[x["post_id"]] = edits.get(x["post_id"], 0) + 1
    news = {x["node_id"]: x for x in d.get("new_posts", []) if x.get("node_id")}
    for n in r["nodes"]:  # connect each node to its WordPress post and to any page or edit waiting in the draft
        p = by_key.get(tmap._key(n.get("url")) or "\0")
        n["post_id"] = p["id"] if p else None
        n["post_status"] = p.get("status") if p else None
        n["in_draft"] = edits.get(p["id"], 0) if p else 0
        np_ = news.get(n["id"])
        n["new_post"] = {"id": np_["id"], "title": np_["title"], "by": np_.get("by"), "edited_by": np_.get("edited_by"),
                         "words": wp.word_count(np_.get("content") or "")} if np_ else None
    return {**r, "range": {"start": s_, "end": e, "prev_start": ps, "prev_end": pe}, "errors": errors}


def map_edit(body):
    return tmap.edit(body.get("ops") or [], by="you")


def map_import(body):
    return tmap.import_csv(body.get("csv") or "", as_proposal=bool(body.get("as_proposal")), by="you")


GET = {"posts": get_posts, "post": get_post, "opportunities": get_opportunities, "draft": lambda q: list_drafts(),
       "settings": get_settings, "history": history, "categories": get_categories, "sitemaps": get_sitemaps,
       "map": get_map, "map_history": lambda q: tmap.history()}
POST = {"add": post_add, "remove": lambda b: remove_drafts(b.get("ids")), "clear": clear_drafts, "check": check_all,
        "inspect": do_inspect, "publish_preview": publish_preview, "publish": publish, "undo": undo, "settings": save_settings,
        "connect": connect, "disconnect": disconnect, "refresh": refresh,
        "map_edit": map_edit, "map_import": map_import, "map_accept": lambda b: tmap.accept(b["id"]),
        "new_post_save": lambda b: save_new_post(b, by="you"), "new_post_remove": remove_new_post, "map_reject": lambda b: tmap.reject(b["id"])}
