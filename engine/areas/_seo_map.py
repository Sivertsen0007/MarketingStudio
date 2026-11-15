"""Topical map for the SEO area, built the Koray way, joined with Search Console + GA4.

The map (one per site, <Studio data>/seo/topical_map.json):
  site    central_entity (what the whole site is about), source_context (who you are, why you publish:
          it decides which attributes belong), central_search_intent (the user's underlying purpose)
  nodes   {id: node}. A node is one page in the map:
            parent, title, section ("core" = the money side the source context exists for, "outer" = the
            attribute/authority side), type ("root" | "hub" | "attribute" | "commercial"),
            entity + attribute (the entity-attribute pair the page is about), macro_context,
            queries (target queries), intent, priority (1 = highest .. 4), url, links (node ids this page
            must link to, with anchor text), note
Changes by Claude (MCP) never touch the map directly: they land as PROPOSALS that the user accepts or
rejects in Studio. The user's own edits in Studio apply straight away. Every applied change is logged.

report() joins every node with Search Console (page + query level) and GA4 and returns:
  per node: clicks, impressions, CTR, position, change vs the previous period, sessions, key events, and
            issues (missing page, not in Search Console, losing clicks, striking distance, low CTR, target
            query landing on another page, not indexed, missing internal links per the map)
  coverage per section, and "unmapped demand": queries with impressions whose page has no node.
"""
import csv
import datetime as dt
import io
import re
import uuid

try:
    from . import _seo_common as c
except ImportError:  # tests import it flat
    import _seo_common as c

FILE = "seo_topical_map.json"
LOG = "seo_topical_map_log.jsonl"
SECTIONS = ("core", "outer")
TYPES = ("root", "hub", "attribute", "commercial")
FIELDS = ("parent", "title", "section", "type", "entity", "attribute", "macro_context", "queries", "intent", "priority", "url", "links", "note")
SITE_FIELDS = ("central_entity", "source_context", "central_search_intent")
EXPECTED_CTR = {1: 0.25, 2: 0.15, 3: 0.10, 4: 0.07, 5: 0.05, 6: 0.04, 7: 0.03, 8: 0.025, 9: 0.02, 10: 0.018}


def _now():
    return dt.datetime.now().isoformat(timespec="seconds")


def _empty():
    return {"site": {**{k: "" for k in SITE_FIELDS}, "exclude": []}, "nodes": {}, "proposals": [], "version": 0}


def load():
    m = c.read_json(FILE, None) or _empty()
    for k, v in _empty().items():
        m.setdefault(k, v)
    return m


# ---------- validating one node ----------

def _clean_node(d, existing=None):
    n = dict(existing or {})
    for k in FIELDS:
        if k not in d:
            continue
        v = d[k]
        if k == "queries":
            v = [q.strip().lower() for q in (v if isinstance(v, list) else re.split(r"[,\n;]+", str(v or ""))) if q.strip()]
            v = list(dict.fromkeys(v))
        elif k == "links":
            out = []
            for x in v or []:
                x = x if isinstance(x, dict) else {"to": str(x)}
                if x.get("to"):
                    out.append({"to": str(x["to"]), "anchor": str(x.get("anchor") or "")[:120]})
            v = out
        elif k == "priority":
            try:
                v = max(1, min(4, int(v)))
            except (TypeError, ValueError):
                v = 3
        elif k == "section":
            v = str(v or "").lower()
            if v not in SECTIONS:
                raise ValueError(f"section must be one of {', '.join(SECTIONS)}")
        elif k == "type":
            v = str(v or "").lower()
            if v not in TYPES:
                raise ValueError(f"type must be one of {', '.join(TYPES)}")
        elif k == "url":
            v = str(v or "").strip()
            if v and not re.match(r"^https?://", v) and not v.startswith("/"):
                raise ValueError("url must start with https:// or /")
        else:
            v = str(v or "").strip()[:2000]
        n[k] = v
    n.setdefault("section", "outer")
    n.setdefault("type", "attribute")
    n.setdefault("queries", [])
    n.setdefault("links", [])
    n.setdefault("priority", 3)
    if not (n.get("title") or "").strip():
        raise ValueError("a node needs a title")
    return n


# ---------- applying operations (one shape for the user's edits and accepted proposals) ----------

OPS = ("add", "update", "remove", "site")


def _apply(m, ops, by):
    """ops: [{"op": "add", "node": {...}} | {"op": "update", "id", "node": {...}} | {"op": "remove", "id"} |
    {"op": "site", "site": {...}}]. Adds may carry a "ref" (a temporary name) so other ops in the same
    batch can use it as a parent or link target. Returns the list of what happened (for the log)."""
    refs, done = {}, []
    nodes = m["nodes"]

    def rid(x):
        return refs.get(x, x) if x else x

    for op in ops:
        kind = op.get("op")
        if kind not in OPS:
            raise ValueError(f"unknown op {kind!r}")
        if kind == "site":
            for k in SITE_FIELDS:
                if k in (op.get("site") or {}):
                    m["site"][k] = str(op["site"][k] or "").strip()[:2000]
            if "exclude" in (op.get("site") or {}):  # pages that deliberately have no node: [{"path": "/de/", "why": "..."}]
                ex = []
                for x in op["site"]["exclude"] or []:
                    x = x if isinstance(x, dict) else {"path": str(x)}
                    path = str(x.get("path") or "").strip().lower()
                    if path:
                        ex.append({"path": path, "why": str(x.get("why") or "")[:200]})
                m["site"]["exclude"] = ex
            done.append({"op": "site"})
            continue
        if kind == "add":
            d = dict(op.get("node") or {})
            nid = "n_" + uuid.uuid4().hex[:8]
            if op.get("ref"):
                refs[op["ref"]] = nid
            d["parent"] = rid(d.get("parent"))
            d["links"] = [{**(x if isinstance(x, dict) else {"to": x}), "to": rid((x if isinstance(x, dict) else {"to": x})["to"])} for x in d.get("links") or []]
            n = _clean_node(d)
            n.update({"id": nid, "created": _now(), "by": by})
            nodes[nid] = n
            done.append({"op": "add", "id": nid, "title": n["title"]})
            continue
        nid = rid(op.get("id"))
        if nid not in nodes:
            raise ValueError(f"no node {op.get('id')!r}")
        if kind == "update":
            d = dict(op.get("node") or {})
            if "parent" in d:
                d["parent"] = rid(d["parent"])
            if "links" in d:
                d["links"] = [{**(x if isinstance(x, dict) else {"to": x}), "to": rid((x if isinstance(x, dict) else {"to": x})["to"])} for x in d["links"] or []]
            nodes[nid] = {**_clean_node(d, nodes[nid]), "updated": _now(), "by": by}
            done.append({"op": "update", "id": nid, "title": nodes[nid]["title"]})
        else:  # remove: children move up to the removed node's parent
            parent = nodes[nid].get("parent")
            for x in nodes.values():
                if x.get("parent") == nid:
                    x["parent"] = parent
                x["links"] = [l for l in x.get("links", []) if l["to"] != nid]
            done.append({"op": "remove", "id": nid, "title": nodes.pop(nid)["title"]})
    for x in nodes.values():  # a parent that points nowhere is dropped
        if x.get("parent") and x["parent"] not in nodes:
            x["parent"] = ""
    m["version"] = m.get("version", 0) + 1
    return done


def edit(ops, by="you"):
    """The user's own edit in Studio: applied straight away."""
    with c.locked_json(FILE, _empty()) as m:
        for k, v in _empty().items():
            m.setdefault(k, v)
        done = _apply(m, ops, by)
    c.append_jsonl(LOG, {"at": _now(), "by": by, "kind": "edit", "done": done})
    return {"ok": True, "done": done}


def propose(ops, note="", by="claude"):
    """Claude's changes: checked now (on a copy), stored as a proposal, applied only when the user accepts."""
    import copy
    m = load()
    preview = _apply(copy.deepcopy(m), ops, by)  # raises on a bad op, so a broken proposal never lands
    p = {"id": "p_" + uuid.uuid4().hex[:8], "at": _now(), "by": by, "note": str(note or "")[:500], "ops": ops, "summary": preview}
    with c.locked_json(FILE, _empty()) as mm:
        for k, v in _empty().items():
            mm.setdefault(k, v)
        mm["proposals"].append(p)
        mm["version"] = mm.get("version", 0) + 1
    return {"ok": True, "proposal": {k: p[k] for k in ("id", "at", "by", "note", "summary")}}


def accept(pid, by="you"):
    with c.locked_json(FILE, _empty()) as m:
        p = next((x for x in m.get("proposals", []) if x["id"] == pid), None)
        if not p:
            raise ValueError("that proposal is gone")
        done = _apply(m, p["ops"], f"{p['by']} (accepted by {by})")
        m["proposals"] = [x for x in m["proposals"] if x["id"] != pid]
    c.append_jsonl(LOG, {"at": _now(), "by": by, "kind": "accept", "proposal": pid, "note": p.get("note"), "done": done})
    return {"ok": True, "done": done}


def reject(pid, by="you"):
    with c.locked_json(FILE, _empty()) as m:
        before = len(m.get("proposals", []))
        m["proposals"] = [x for x in m.get("proposals", []) if x["id"] != pid]
        m["version"] = m.get("version", 0) + 1
        gone = before - len(m["proposals"])
    c.append_jsonl(LOG, {"at": _now(), "by": by, "kind": "reject", "proposal": pid})
    return {"ok": bool(gone)}


def history(limit=100):
    return list(reversed(c.read_jsonl(LOG, limit)))


# ---------- import from a spreadsheet (CSV) ----------

_COLS = {
    "title": ("title", "page title", "page", "name", "topic"),
    "parent": ("parent", "parent entity", "parent title", "parent page"),
    "url": ("url", "full url", "page url", "url slug", "slug"),
    "queries": ("queries", "keywords", "primary keywords", "target keywords", "target queries"),
    "type": ("type", "entity type", "page type", "node type"),
    "section": ("section",),
    "priority": ("priority",),
    "intent": ("intent", "search intent"),
    "macro_context": ("purpose", "macro context", "macro_context"),
    "entity": ("entity", "central entity"),
    "attribute": ("attribute",),
    "note": ("note", "notes", "existing post", "comment"),
}
_PRIO = {"critical": 1, "high": 2, "medium": 3, "low": 4}
_CORE_HINT = re.compile(r"commercial|boat|vessel|itinerar|product|trip|tour|hub|region|destination|service|pricing|category", re.I)


def _map_type(raw, level=""):
    t = (raw or "").lower()
    if (level or "").lower().startswith("root") or t == "root":
        return "root"
    if "hub" in t or "region" in t or "destination" in t or "category" in t:
        return "hub"
    if _CORE_HINT.search(t) and "hub" not in t:
        return "commercial"
    return "attribute"


def parse_csv(text):
    """CSV rows -> add ops (parents by title). Column names are matched loosely, so most topical-map sheets import as they are."""
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        raise ValueError("the file has no rows")
    heads = {h.strip().lower(): h for h in rows[0].keys() if h}

    def col(k):
        for name in _COLS[k]:
            if name in heads:
                return heads[name]
        return None
    cols = {k: col(k) for k in _COLS}
    if not cols["title"]:
        raise ValueError("no title column (Title, Page Title, Name or Topic)")
    level_col = heads.get("level")
    ops, seen = [], set()
    for i, r in enumerate(rows):
        title = (r.get(cols["title"]) or "").strip()
        if not title or title.lower() in seen:
            continue
        seen.add(title.lower())
        raw_type = r.get(cols["type"]) if cols["type"] else ""
        level = r.get(level_col) if level_col else ""
        typ = _map_type(raw_type, level)
        sec = (r.get(cols["section"]) or "").strip().lower() if cols["section"] else ""
        if sec not in SECTIONS:
            sec = "core" if typ in ("root", "hub", "commercial") else "outer"
        url = (r.get(cols["url"]) or "").strip() if cols["url"] else ""
        if url and not url.startswith(("http://", "https://", "/")):
            url = "/" + url.lstrip("/")
        node = {"title": title[:300], "type": typ, "section": sec, "url": url,
                "queries": r.get(cols["queries"]) or "" if cols["queries"] else "",
                "priority": _PRIO.get((r.get(cols["priority"]) or "").strip().lower(), 3) if cols["priority"] else 3}
        for k in ("intent", "macro_context", "entity", "attribute", "note"):
            if cols[k] and (r.get(cols[k]) or "").strip():
                node[k] = r[cols[k]].strip()
        parent = (r.get(cols["parent"]) or "").strip().lower() if cols["parent"] else ""
        if parent:
            node["parent"] = "t:" + parent
        ops.append({"op": "add", "ref": "t:" + title.lower(), "node": node})
    refs = {o["ref"] for o in ops}
    for o in ops:  # a parent that is not in the sheet stays empty instead of failing the import
        if o["node"].get("parent") and o["node"]["parent"] not in refs:
            o["node"]["note"] = (o["node"].get("note", "") + f" (parent in the sheet: {o['node']['parent'][2:]})").strip()
            o["node"]["parent"] = ""
    # parents first, so references resolve in one pass
    order, placed, pending = [], set(), list(ops)
    for _ in range(len(ops) + 1):
        rest = []
        for o in pending:
            p = o["node"].get("parent")
            if not p or p in placed:
                order.append(o)
                placed.add(o["ref"])
            else:
                rest.append(o)
        pending = rest
        if not pending:
            break
    return order + pending


def import_csv(text, as_proposal=False, by="you", note="Imported from a spreadsheet"):
    ops = parse_csv(text)
    return propose(ops, note, by) if as_proposal else edit(ops, by)


# ---------- the report: map x Search Console x GA4 ----------

def _key(url):
    """The same key Search Console rows use (lower-case path, no trailing slash), from a full URL or a path.
    The host is ignored: a node always belongs to the map's own site."""
    if not url:
        return ""
    import urllib.parse
    u = urllib.parse.urlsplit(url.strip())
    path = urllib.parse.unquote(u.path or "/").lower()
    return path.rstrip("/") or "/"


def report(cur_pages, prev_pages, qp, ga=None, posts=None, inspect=None, min_imps=20):
    """Pure join, so it can be tested without Google. cur/prev_pages: {key: {clicks, impressions, ctr, position}};
    qp: [[query, key, clicks, impressions, ctr, position]]; posts: WordPress posts with key + links_to."""
    m = load()
    nodes = m["nodes"]
    ga, inspect = ga or {}, inspect or {}
    posts_by_key = {p["key"]: p for p in (posts or [])}
    by_query = {}
    for q, k, cl, im, ctr, pos in qp:
        by_query.setdefault(q, []).append((k, cl, im, pos))
    node_keys = {nid: _key(n.get("url")) for nid, n in nodes.items()}
    key_to_node = {k: nid for nid, k in node_keys.items() if k}
    kids = {}
    for nid, n in nodes.items():
        kids.setdefault(n.get("parent") or "", []).append(nid)

    out = []
    for nid, n in nodes.items():
        k = node_keys[nid]
        cur, prev, a = cur_pages.get(k) or {}, prev_pages.get(k) or {}, ga.get(k) or {}
        issues = []
        live = bool(k and (cur.get("impressions") or k in posts_by_key))
        if not k:
            issues.append({"kind": "missing_page", "level": "gap", "text": "No page yet: write it or point the node at an existing URL."})
        elif not live:
            issues.append({"kind": "page_not_found", "level": "gap",
                           "text": "No page found at this URL: not a WordPress post and no Search Console impressions. Write it, or point the node at the right URL."})
        elif not cur.get("impressions"):
            issues.append({"kind": "no_search_data", "level": "warn", "text": "The page has no impressions in Search Console for this period."})
        pc, cc = prev.get("clicks", 0), cur.get("clicks", 0)
        if pc >= 10 and cc <= pc * 0.7 and pc - cc >= 5:
            issues.append({"kind": "losing", "level": "update", "text": f"Losing clicks: {pc} -> {cc}."})
        pos = cur.get("position") or 0
        if cur.get("impressions", 0) >= min_imps and 8 <= pos <= 20:
            issues.append({"kind": "striking", "level": "update", "text": f"Average position {pos}: close to page one, worth an update."})
        exp = EXPECTED_CTR.get(round(pos)) if pos else None
        if exp and cur.get("impressions", 0) >= min_imps * 5 and cur.get("ctr", 0) < exp * 0.5:
            issues.append({"kind": "low_ctr", "level": "update", "text": f"CTR {cur['ctr']:.1%} at position {pos} (expected about {exp:.0%}): rewrite the title and meta description."})
        ix = inspect.get(n.get("url")) or {}
        if ix and ix.get("coverage") and "indexed" not in str(ix.get("coverage")).lower():
            issues.append({"kind": "not_indexed", "level": "update", "text": f"Not indexed: {ix.get('coverage')}."})
        qstats = []
        for q in n.get("queries", []):
            rows = sorted(by_query.get(q, []), key=lambda r: -r[2])
            mine = next((r for r in rows if r[0] == k), None)
            best = rows[0] if rows else None
            qs = {"query": q, "impressions": sum(r[2] for r in rows), "clicks": mine[1] if mine else 0,
                  "position": mine[3] if mine else None, "lands_on": None}
            if best and k and best[0] != k and best[2] >= 5:
                qs["lands_on"] = best[0]
                issues.append({"kind": "query_elsewhere", "level": "update",
                               "text": f'"{q}" mostly lands on {best[0]}' + (" (another node)" if best[0] in key_to_node else " (not in the map)") + "."})
            qstats.append(qs)
        if k in posts_by_key:  # internal links the map asks for, against the live link graph
            have = set(posts_by_key[k].get("links_to") or [])
            want = [x["to"] for x in n.get("links", [])]
            if n.get("parent"):
                want.append(n["parent"])
            want += kids.get(nid, [])
            missing = [nodes[t]["title"] for t in dict.fromkeys(want) if t in nodes and node_keys.get(t) and node_keys[t] not in have]
            if missing:
                issues.append({"kind": "missing_links", "level": "update", "text": "Not linking to: " + ", ".join(missing[:6]) + ("..." if len(missing) > 6 else "") + "."})
        out.append({**{f: n.get(f) for f in ("id", "parent", "title", "section", "type", "entity", "attribute", "macro_context", "intent", "priority", "url", "note")},
                    "queries": qstats, "links": n.get("links", []), "children": len(kids.get(nid, [])), "live": live,
                    "clicks": cc, "impressions": cur.get("impressions", 0), "ctr": cur.get("ctr", 0), "position": cur.get("position"),
                    "prev_clicks": pc, "delta_clicks": cc - pc, "sessions": a.get("sessions", 0), "key_events": a.get("select_cabin", 0),
                    "issues": issues,
                    "state": "gap" if any(i["level"] == "gap" for i in issues) else "update" if any(i["level"] == "update" for i in issues) else "ok"})

    cov = {}
    for s in SECTIONS:
        ns = [r for r in out if r["section"] == s]
        cov[s] = {"nodes": len(ns), "live": sum(1 for r in ns if r["live"]), "gaps": sum(1 for r in ns if r["state"] == "gap"),
                  "update": sum(1 for r in ns if r["state"] == "update"),
                  "pct": round(100 * sum(1 for r in ns if r["live"]) / len(ns)) if ns else 0}
    covered_q = {q for n in nodes.values() for q in n.get("queries", [])}
    excl = [(x["path"].rstrip("/") or "/", x["path"].endswith("/") and x["path"] != "/") for x in m["site"].get("exclude", [])]

    def excluded(k):  # "/de/" = everything under /de, "/inseanq" = that page only
        return any(k == p or (prefix and k.startswith(p + "/")) for p, prefix in excl)
    unmapped = {}
    for q, rows in by_query.items():
        imps = sum(r[2] for r in rows)
        if imps < min_imps or q in covered_q:
            continue
        top = max(rows, key=lambda r: r[2])
        if top[0] in key_to_node or excluded(top[0]):
            continue
        g = unmapped.setdefault(top[0], {"page": top[0], "impressions": 0, "clicks": 0, "queries": []})
        g["impressions"] += imps
        g["clicks"] += sum(r[1] for r in rows)
        g["queries"].append({"query": q, "impressions": imps})
    demand = sorted(unmapped.values(), key=lambda g: -g["impressions"])[:100]
    for g in demand:
        g["queries"] = sorted(g["queries"], key=lambda x: -x["impressions"])[:15]
    return {"site": m["site"], "nodes": out, "coverage": cov, "unmapped": demand, "version": m.get("version", 0),
            "proposals": [{k: p[k] for k in ("id", "at", "by", "note", "summary")} for p in m.get("proposals", [])]}
