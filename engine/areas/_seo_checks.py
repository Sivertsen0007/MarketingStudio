"""SEO area: checks on drafted changes. level "error" blocks Publish, "warn" is shown for the user to judge.

Copy rules come from team_settings (key copy_rules, area "seo"): this Mac's own, or the team's.
"""
import concurrent.futures as cf
import re
import time
import urllib.error
import urllib.request

from . import _seo_common as c
from . import _seo_wp as wp

SEO_TITLE = (30, 60)
META = (120, 156)  # Yoast's green band; Google cuts a desktop description at about 156-160 characters
FOCUS_MAX_WORDS = 4
SITE_NAME = "Master Liveaboards"  # what %%sitename%% fills in (the WordPress site title), for measuring only
SNIPPET_MSG = ("Yoast fields can't be written yet: install the Studio snippet (wp-snippets/yoast-rest-fields.php, see the README), "
               "or remove this change.")
LINK_TTL = 3600
_link_cache = {}


def _issue(level, field, msg):
    return {"level": level, "field": field, "msg": msg}


def text_rules(field, text):
    """Copy rules on visible text (HTML stripped for the body)."""
    out = []
    t = wp.text_of(text) if field in ("content", "excerpt") else str(text or "")
    try:
        import team_settings  # noqa: PLC0415
        rules = team_settings.copy_rules_full("seo")
    except Exception:  # noqa: BLE001
        rules = []
    for r in rules:
        m = r["rx"].search(str(text or "") if r["raw"] else t)  # raw: the whole field, markup included
        if m:
            msg = f'{r["message"]} Found: "{m.group(0)[:60]}".' if r["quote"] else r["message"]
            out.append(_issue("error" if r["level"] == "block" else "warn", field, msg))
    return out


def yoast_vars(post=None, site_name=None):
    """What Yoast puts in place of the %%variables%% Studio knows, for measuring a title or description."""
    post = post or {}
    cats = post.get("category_names") or []
    return {"title": str(post.get("title") or ""), "sitename": site_name or SITE_NAME, "sep": "-", "page": "",
            "primary_category": cats[0] if cats else "", "category": ", ".join(cats), "excerpt": wp.text_of(post.get("excerpt") or "")[:155],
            "currentyear": str(time.localtime().tm_year), "currentmonth": time.strftime("%B"), "currentday": str(time.localtime().tm_mday)}


def expand_vars(text, post=None, site_name=None):
    """(text with Yoast's %%variables%% filled in, [unknown variable names])."""
    v, unknown = yoast_vars(post, site_name), []

    def sub(m):
        k = m.group(1).lower()
        if k in v:
            return v[k]
        unknown.append(m.group(0))
        return ""
    return re.sub(r"%%\s*([a-z_]+)\s*%%", sub, str(text or ""), flags=re.I), unknown


def _words(s):
    return [w for w in re.findall(r"[a-z0-9à-ɏ]+", (s or "").lower()) if len(w) > 1]


def has_phrase(text, phrase):
    """Every word of the keyphrase is in the text (Yoast matches words, in any order)."""
    want, have = _words(phrase), set(_words(text))
    return bool(want) and all(w in have for w in want)


def _first_par(html_text):
    m = re.search(r"(?is)<p\b[^>]*>(.*?)</p>", html_text or "")
    return wp.text_of(m.group(1) if m else html_text or "")[:600]


def _context_issues(f, new, ctx):
    """Checks that need the rest of the post: snippet, Yoast variables, focus keyphrase, noindex and canonical."""
    out = []
    post = {**(ctx.get("post") or {}), f: new}
    snippet = ctx.get("snippet")
    if f in wp.YOAST_META and snippet is False and (not ctx.get("new") or (new or "").strip()):  # clearing one is a write too
        out.append(_issue("warn" if ctx.get("new") else "error", f, SNIPPET_MSG if not ctx.get("new") else
                          "Yoast fields are not saved on a new page until the Studio snippet is installed (wp-snippets/yoast-rest-fields.php)."))
    kw = (post.get("focus_kw") or "").strip()
    if f == "focus_kw" and kw:
        if len(_words(kw)) > FOCUS_MAX_WORDS:
            out.append(_issue("warn", f, f"The focus keyphrase has {len(_words(kw))} words. Keep it to {FOCUS_MAX_WORDS} or fewer."))
        miss = [lbl for lbl, txt in (("the SEO title", expand_vars(post.get("seo_title") or post.get("title"), post, ctx.get("site_name"))[0]),
                                     ("the meta description", expand_vars(post.get("meta_desc"), post, ctx.get("site_name"))[0]),
                                     ("the slug", (post.get("slug") or "").replace("-", " ")),
                                     ("the first paragraph", _first_par(post.get("content"))))
                if (txt or "").strip() and not has_phrase(txt, kw)]
        if miss:
            out.append(_issue("warn", f, f'The focus keyphrase "{kw}" is not in {", ".join(miss[:-1]) + " or " + miss[-1] if len(miss) > 1 else miss[0]}.'))
        twin = next((o for o in ctx.get("others") or [] if o.get("id") != ctx.get("self_id")
                     and (o.get("focus_kw") or "").strip().lower() == kw.lower()), None)
        if twin:
            out.append(_issue("warn", f, f'"{kw}" is already the focus keyphrase of "{twin.get("title")}": two pages aiming at one phrase compete with each other.'))
    elif f in ("seo_title", "meta_desc") and kw and (new or "").strip():
        if not has_phrase(expand_vars(new, post, ctx.get("site_name"))[0], kw):
            out.append(_issue("warn", f, f'The focus keyphrase "{kw}" is not in the {wp.LABELS.get(f, f).lower()}.'))
    if f in ("title", "seo_title", "meta_desc", "focus_kw") and not ctx.get("new"):
        if post.get("noindex"):
            out.append(_issue("warn", f, "This post is set to noindex in Yoast: Google will not show it, whatever its title and description say."))
        canon, url = (post.get("canonical") or "").strip(), post.get("url") or ""
        if canon and url and c.url_key(canon) != c.url_key(url):
            out.append(_issue("warn", f, f"Yoast's canonical URL points to another page ({canon}): Google shows that page instead of this one."))
    return out


def check_change(ch, link_check=None, ctx=None):
    """Issues for one drafted change {field, old, new, kind}. ctx (optional): {post: the post's values with the draft's
    other edits, snippet: True/False/None (Yoast fields writable over REST), others: [{id, title, focus_kw}], self_id,
    new: a page Studio creates, site_name}."""
    f, new = ch.get("field"), ch.get("new")
    ctx = ctx or {}
    if ch.get("kind") == "todo":
        return [_issue("info", f, "Suggestion: write the new text in the post editor, then it becomes a change.")]
    if f not in wp.FIELDS:
        return [_issue("error", f, f"Unknown field {f}.")]
    out = []
    post = {**(ctx.get("post") or {}), f: new}
    if f in ("seo_title", "meta_desc"):
        text, unknown = expand_vars(new, post, ctx.get("site_name"))
        n, (lo, hi), lbl = len(text), SEO_TITLE if f == "seo_title" else META, wp.LABELS[f]
        filled = " with its Yoast variables filled in" if text != (new or "") else ""
        if (new or "").strip() and not lo <= n <= hi:
            out.append(_issue("warn", f, f"{lbl} is {n} characters{filled}. Aim for {lo}-{hi}."))
        elif f == "seo_title" and not (new or "").strip():
            out.append(_issue("warn", f, f"SEO title is empty: Yoast uses its default title. Aim for {lo}-{hi} characters."))
        elif f == "meta_desc" and not (new or "").strip():
            out.append(_issue("warn", f, f"Meta description is empty: Google picks text from the page. Aim for {lo}-{hi} characters."))
        if unknown:
            out.append(_issue("warn", f, f"Studio does not know {', '.join(dict.fromkeys(unknown))}: the length above counts it as empty."))
    if f == "title" and not (new or "").strip():
        out.append(_issue("error", f, "The title is empty."))
    if f == "slug":
        bad_slug = not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", new or "")
        if bad_slug:
            out.append(_issue("error", f, "A slug uses only lower-case letters, numbers and single dashes."))
        elif (ch.get("old") or "") and new != ch.get("old"):
            out.append(_issue("warn", f, f"Changing the slug changes the URL. Add a 301 redirect from /{ch.get('old')}/ to /{new}/ "
                                         "(Yoast Premium or the Redirection plugin), and expect a dip while Google catches up."))
    if f == "categories" and not new:
        out.append(_issue("warn", f, "No category: WordPress will put it in Uncategorised."))
    if f in ("title", "seo_title", "meta_desc", "excerpt", "content", "focus_kw"):
        out += text_rules(f, new)
    if f == "content":
        if ch.get("base") == "rendered":
            out.append(_issue("warn", f, "This body was edited from the published HTML, not the WordPress source. Check block layout on staging first."))
        out += check_links(new, link_check)
    out += _context_issues(f, new, ctx)
    return out


def _status(url):
    now = time.time()
    hit = _link_cache.get(url)
    if hit and now - hit[1] < LINK_TTL:
        return hit[0]
    code = None
    for method in ("HEAD", "GET"):
        try:
            req = urllib.request.Request(url, method=method, headers={"User-Agent": c.UA})
            with urllib.request.urlopen(req, timeout=10) as r:
                code = r.status
        except urllib.error.HTTPError as e:
            code = e.code
        except Exception:  # noqa: BLE001 - DNS, timeout, TLS
            code = 0
        if code and code not in (403, 405, 501):
            break  # some servers refuse HEAD; try GET once
    _link_cache[url] = (code, now)
    return code


def body_links(html_text):
    out = []
    for href in wp.links_of(html_text):
        h = href.strip()
        if not h or h.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        if h.startswith("/"):
            h = c.SITES["prod"] + h
        if h.startswith(("http://", "https://")) and h not in out:
            out.append(h)
    return out


def check_links(html_text, link_check=None, cap=60):
    """Every link in the body must answer 2xx or 3xx (urllib follows redirects, so 3xx shows as the final page)."""
    links = body_links(html_text)[:cap]
    if not links:
        return []
    fn = link_check or _status
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        codes = dict(zip(links, ex.map(fn, links)))
    bad = [(u, s) for u, s in codes.items() if not (s and 200 <= s < 400)]
    return [_issue("warn", "content", f"Link answers {s or 'no response'}: {u}") for u, s in bad]


def summarise(issues):
    return {"errors": sum(1 for i in issues if i["level"] == "error"), "warnings": sum(1 for i in issues if i["level"] == "warn")}
