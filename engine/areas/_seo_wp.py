"""SEO area: WordPress (masterliveaboards.com). Read over the public REST API, write through an adapter.

Reading: /wp-json/wp/v2/posts?_embed, per_page=100, pages from X-WP-TotalPages. No auth needed for published posts.
Writing: WpRestWriter (Application Password of the "Marketing Studio" Editor user) or DryRunWriter (default, records only).
"""
import base64
import html
import re
import time
import uuid
from html.parser import HTMLParser

from . import _seo_common as c

POSTS_TTL = 30 * 60
FIELDS = ("title", "slug", "seo_title", "meta_desc", "focus_kw", "excerpt", "categories", "content")
LABELS = {"title": "Title", "slug": "Slug", "seo_title": "SEO title", "meta_desc": "Meta description", "focus_kw": "Focus keyphrase",
          "excerpt": "Excerpt", "categories": "Categories", "content": "Body"}
YOAST_META = {"seo_title": "_yoast_wpseo_title", "meta_desc": "_yoast_wpseo_metadesc", "focus_kw": "_yoast_wpseo_focuskw"}
STOP = set("a an and are as at be by for from how in is it of on or the to with you your our we what why when where which this that "
           "about into over vs best guide top things know didnt didn't probably facts dive diving liveaboard liveaboards trip "
           "trips master masterliveaboards".split())


# ---------- HTML helpers ----------

class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.links, self._skip = [], [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self._skip += 1
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)
        if tag in ("p", "br", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr"):
            self.out.append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.out.append(data)


def parse_html(s):
    p = _Text()
    try:
        p.feed(s or "")
        p.close()
    except Exception:  # noqa: BLE001 - malformed HTML: use what was read
        pass
    return re.sub(r"\s+", " ", "".join(p.out)).strip(), p.links


def text_of(s):
    return parse_html(s)[0]


def links_of(s):
    return parse_html(s)[1]


def word_count(s):
    return len(re.findall(r"[\w'À-ɏ-]+", text_of(s)))


def tokens(s):
    return {w for w in re.findall(r"[a-z0-9à-ɏ]+", (s or "").lower()) if len(w) > 2 and w not in STOP}


# ---------- reading ----------

def _fetch_all(base, auth=None, statuses=None):
    out, page, pages = [], 1, 1
    while page <= pages:
        q = f"{base}/wp-json/wp/v2/posts?per_page=100&page={page}&_embed=1&orderby=date&order=desc"
        if statuses:
            q += "&context=edit&status=" + ",".join(statuses)
        st, hdrs, body = c.http(q, headers=({"Authorization": auth} if auth else None), timeout=60)
        if st != 200 or not isinstance(body, list):
            raise RuntimeError(f"WordPress REST answered {st}: {c.scrub(c.err_text(body))}")
        out += body
        pages = int({k.lower(): v for k, v in hdrs.items()}.get("x-wp-totalpages", 1) or 1)
        page += 1
    return out


def parse_post(p):
    yj = p.get("yoast_head_json") or {}
    meta = p.get("meta") or {}
    emb = p.get("_embedded") or {}
    fm = (emb.get("wp:featuredmedia") or [{}])[0] or {}
    sizes = ((fm.get("media_details") or {}).get("sizes") or {})
    img = (sizes.get("medium_large") or sizes.get("medium") or {}).get("source_url") or fm.get("source_url")
    cats = {t["id"]: t.get("name", "") for grp in (emb.get("wp:term") or []) for t in (grp or []) if t.get("taxonomy") == "category"}
    content = (p.get("content") or {}).get("raw") or (p.get("content") or {}).get("rendered") or ""
    rendered = (p.get("content") or {}).get("rendered") or content
    title_raw = (p.get("title") or {}).get("raw")
    excerpt = (p.get("excerpt") or {}).get("raw")
    robots = yj.get("robots") or {}
    return {
        "id": p["id"], "title": html.unescape(title_raw if title_raw is not None else (p.get("title") or {}).get("rendered", "")),
        "url": p.get("link", ""), "key": c.url_key(p.get("link", "")), "slug": p.get("slug", ""), "status": p.get("status", ""),
        "date": p.get("date", ""), "modified": p.get("modified", ""), "words": word_count(rendered),
        "seo_title": html.unescape(meta.get("_yoast_wpseo_title") or yj.get("title") or ""),
        "seo_title_source": "yoast meta" if meta.get("_yoast_wpseo_title") else "rendered",
        "meta_desc": html.unescape(meta.get("_yoast_wpseo_metadesc") or yj.get("description") or ""),
        "focus_kw": meta.get("_yoast_wpseo_focuskw") if "_yoast_wpseo_focuskw" in meta else None,
        "excerpt": html.unescape(excerpt) if excerpt is not None else text_of((p.get("excerpt") or {}).get("rendered", "")),
        "categories": list(p.get("categories") or []), "category_names": [cats.get(i, str(i)) for i in p.get("categories") or []],
        "image": img, "image_alt": fm.get("alt_text", ""), "noindex": robots.get("index") == "noindex",
        "canonical": yj.get("canonical", ""), "content": content, "content_base": "raw" if (p.get("content") or {}).get("raw") else "rendered",
        "rendered": rendered,
    }


def link_graph(posts):
    """links_out: unique internal links in the body (any ML page). links_in: other posts linking to this post. Mutates posts."""
    by_key = {p["key"]: p for p in posts if p.get("key")}
    for p in posts:
        p["links_in"], p["linked_from"] = 0, []
    for p in posts:
        outs = set()
        for href in links_of(p.get("rendered") or p.get("content") or ""):
            if not c.is_internal(href) or href.startswith("#"):
                continue
            k = c.url_key(href if "://" in href else "https://masterliveaboards.com" + ("" if href.startswith("/") else "/") + href)
            if k and k != p["key"]:
                outs.add(k)
        p["links_out"] = len(outs)
        p["links_to"] = sorted(outs)
        for k in outs:
            t = by_key.get(k)
            if t:
                t["links_in"] += 1
                t["linked_from"].append(p["id"])
    return posts


def posts(force=False):
    """All posts, parsed, with the internal-link graph. Cached 30 min. Drafts too when connected (context=edit)."""
    def load():
        user, pw = c.wp_creds()
        raw = None
        if user:
            try:
                raw = _fetch_all(c.SITES["prod"], auth=_basic(user, pw), statuses=["publish", "draft", "pending", "future", "private"])
            except Exception:  # noqa: BLE001 - fall back to the public list
                raw = None
        authed = raw is not None
        raw = raw if authed else _fetch_all(c.SITES["prod"])
        rows = link_graph([parse_post(p) for p in raw])
        return {"at": time.time(), "authed": authed, "posts": rows}
    return c.cached("posts", POSTS_TTL, load, force=force)


def post(post_id):
    pid = int(post_id)
    p = next((x for x in posts()["posts"] if x["id"] == pid), None)
    if not p:
        raise KeyError(f"Post {post_id} not found (refresh the list).")
    return p


def categories():
    def load():
        st, _, body = c.http(f"{c.SITES['prod']}/wp-json/wp/v2/categories?per_page=100&_fields=id,name,count,parent")
        if st != 200 or not isinstance(body, list):
            raise RuntimeError(f"Categories: WordPress answered {st}")
        return body
    return c.cached("categories", 6 * 3600, load)


def related(target, all_posts, n=3):
    """Posts most like `target` by title/slug words and shared categories, that do not link to it yet."""
    tt = tokens(target["title"] + " " + target["slug"].replace("-", " "))
    cats = set(target.get("categories") or [])
    out = []
    for p in all_posts:
        if p["id"] == target["id"] or p.get("status") != "publish":
            continue
        pt = tokens(p["title"] + " " + p["slug"].replace("-", " "))
        if not tt or not pt:
            continue
        shared = tt & pt
        score = len(shared) / len(tt | pt) + 0.15 * len(cats & set(p.get("categories") or []))
        if shared and score >= 0.15 and target["key"] not in p.get("links_to", []):
            out.append((round(score, 3), p, sorted(shared)))
    out.sort(key=lambda x: -x[0])
    return out[:n]


# ---------- writers ----------

def _basic(user, pw):
    return "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()


def rest_payload(fields):
    """Studio field names -> the WP REST body. Yoast fields go in meta (needs wp-snippets/yoast-rest-fields.php)."""
    body, meta = {}, {}
    for k, v in fields.items():
        if k in YOAST_META:
            meta[YOAST_META[k]] = v or ""
        elif k in ("title", "slug", "excerpt", "content"):
            body[k] = v
        elif k == "categories":
            body[k] = [int(x) for x in v]
    if meta:
        body["meta"] = meta
    return body


class DryRunWriter:
    """Default. Records what it would send in seo_dryrun.jsonl; never calls WordPress."""
    name = "dry"
    live = False

    def __init__(self, target="staging", delay=0.25):
        self.target, self.delay = target, delay

    def _log(self, action, post_id, payload=None, **extra):
        c.append_jsonl("seo_dryrun.jsonl", {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "target": self.target, "action": action,
                                            "post_id": post_id, "payload": payload, **extra})

    def _wait(self):
        if self.delay:
            time.sleep(self.delay)

    def current(self, post_id):
        self._wait()
        p = post(post_id)
        return {**{k: p.get(k) for k in FIELDS}, "_rendered": p.get("rendered") or p.get("content"), "_modified": p.get("modified")}

    def save_revision_note(self, post_id, before):
        self._wait()
        rev = f"dry-{uuid.uuid4().hex[:8]}"
        self._log("revision", post_id, before=before, revision_id=rev)
        return rev

    def update(self, post_id, fields):
        self._wait()
        payload = rest_payload(fields)
        self._log("update", post_id, payload, url=f"{c.SITES[self.target]}/wp-json/wp/v2/posts/{post_id}")
        return {"ok": True, "sent": payload}

    def revisions(self, post_id):
        return [r for r in c.read_jsonl("seo_dryrun.jsonl", 2000) if r.get("post_id") == post_id and r.get("action") == "revision"]

    def restore(self, post_id, snapshot):
        self._wait()
        payload = rest_payload(snapshot.get("before") or {})
        self._log("restore", post_id, payload, revision_id=snapshot.get("revision_id"))
        return {"ok": True, "sent": payload, "from": "saved values"}


class WpRestWriter:
    """Writes with the Marketing Studio user's Application Password. Only ever run from the user's own Publish / Undo button."""
    name = "wp"
    live = True

    def __init__(self, target="staging"):
        user, pw = c.wp_creds()
        if not user:
            raise RuntimeError("Not connected: add the Marketing Studio WordPress user in SEO > Settings > Connect.")
        self.target, self.base, self._auth = target, c.SITES[target], _basic(user, pw)

    def _call(self, path, method="GET", body=None):
        st, _, res = c.http(f"{self.base}/wp-json/wp/v2/{path}", method=method, body=body, headers={"Authorization": self._auth}, timeout=60)
        if st >= 300:
            raise RuntimeError(f"WordPress {self.target} answered {st}: {c.scrub(c.err_text(res))}")
        return res

    def me(self):
        return self._call("users/me?context=edit&_fields=id,name,roles,capabilities")

    def current(self, post_id):
        p = self._call(f"posts/{int(post_id)}?context=edit")
        meta = p.get("meta") or {}
        return {"title": (p.get("title") or {}).get("raw", ""), "slug": p.get("slug", ""), "excerpt": (p.get("excerpt") or {}).get("raw", ""),
                "content": (p.get("content") or {}).get("raw", ""), "categories": p.get("categories") or [],
                "_rendered": (p.get("content") or {}).get("rendered", ""), "_modified": p.get("modified_gmt"),
                **{k: meta.get(m) for k, m in YOAST_META.items()}, "_meta_keys": sorted(meta)}

    def save_revision_note(self, post_id, before):
        """WordPress keeps a revision of title/body/excerpt on each update; note the newest one now so undo can go back to it."""
        try:
            revs = self._call(f"posts/{int(post_id)}/revisions?per_page=1&_fields=id,date")
        except RuntimeError:
            return None  # revisions off (WP_POST_REVISIONS false): undo uses the values saved in Studio's history
        return revs[0]["id"] if revs else None

    def update(self, post_id, fields):
        payload = rest_payload(fields)
        if "meta" in payload:
            missing = [m for m in payload["meta"] if m not in (self.current(post_id).get("_meta_keys") or [])]
            if missing:
                raise RuntimeError("Yoast fields are not writable over REST yet: install wp-snippets/yoast-rest-fields.php (the README).")
        res = self._call(f"posts/{int(post_id)}", "POST", payload)
        return {"ok": True, "modified": res.get("modified_gmt")}

    def revisions(self, post_id):
        return self._call(f"posts/{int(post_id)}/revisions?per_page=20&_fields=id,date,author")

    def restore(self, post_id, snapshot):
        """Undo: restore the revision noted before publishing (title/body/excerpt), plus the saved Yoast/slug/category values.
        If WordPress keeps no revisions (or that one is gone), the values Studio saved before publishing are used instead."""
        before = dict(snapshot.get("before") or {})
        rev, source = snapshot.get("revision_id"), "saved values"
        if rev and any(k in before for k in ("title", "content", "excerpt")):
            try:
                r = self._call(f"posts/{int(post_id)}/revisions/{int(rev)}?context=edit")
                for k in ("title", "content", "excerpt"):
                    if k in before:
                        before[k] = (r.get(k) or {}).get("raw", before[k])
                source = "revision"
            except RuntimeError:
                pass
        res = self._call(f"posts/{int(post_id)}", "POST", rest_payload(before))
        return {"ok": True, "modified": res.get("modified_gmt"), "from": source}
