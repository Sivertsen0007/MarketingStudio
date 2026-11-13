"""SEO area: checks on drafted changes. level "error" blocks Publish, "warn" is shown for the user to judge.

ML copy rules (memory feedback notes): no emojis, never "Bahamas", "vessel" not "yacht", no promised sightings,
"spaces" not "cabins" for pricing, and no em dash anywhere.
"""
import concurrent.futures as cf
import re
import time
import urllib.error
import urllib.request

from . import _seo_common as c
from . import _seo_wp as wp

EM_DASH = chr(0x2014)  # the em dash, written as a code so this file has none
EMOJI = re.compile("[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002B50\U00002B55\U00002934\U00002935\U00003030\U0000303D\U0000FE0F]")
PRICE = r"(?:£|\$|€|usd|gbp|eur|price|prices|priced|from|per|rate|rates|cost|costs)"
RULES = [
    ("error", re.compile(r"\bbahamas\b", re.I), 'Never use "Bahamas" in ML copy.'),
    ("warn", re.compile(r"\byachts?\b", re.I), 'Say "vessel", not "yacht".'),
    ("warn", re.compile(r"\b(guarantee[sd]?|guaranteed sightings?|you will (?:see|spot|meet)|you'll (?:see|spot|meet)|always see|certain to see|sure to see)\b", re.I),
     "Do not promise sightings, weather or conditions."),
    ("warn", re.compile(rf"(\bcabins?\b[^.]{{0,40}}{PRICE}\b|{PRICE}\b[^.]{{0,40}}\bcabins?\b)", re.I), 'For pricing, say "spaces", not "cabins".'),
]
SEO_TITLE = (30, 60)
META = (70, 160)
LINK_TTL = 3600
_link_cache = {}


def _issue(level, field, msg):
    return {"level": level, "field": field, "msg": msg}


def text_rules(field, text):
    """Copy rules on visible text (HTML stripped for the body)."""
    out = []
    t = wp.text_of(text) if field in ("content", "excerpt") else str(text or "")
    if EM_DASH in str(text or ""):
        out.append(_issue("error", field, "Has an em dash. Use a regular dash or rewrite."))
    if EMOJI.search(t):
        out.append(_issue("error", field, "Has an emoji. ML copy uses none."))
    for level, rx, msg in RULES:
        m = rx.search(t)
        if m:
            out.append(_issue(level, field, f'{msg} Found: "{m.group(0)[:60]}".'))
    return out


def check_change(ch, link_check=None):
    """Issues for one drafted change {field, old, new, kind}."""
    f, new = ch.get("field"), ch.get("new")
    if ch.get("kind") == "todo":
        return [_issue("info", f, "Suggestion: write the new text in the post editor, then it becomes a change.")]
    if f not in wp.FIELDS:
        return [_issue("error", f, f"Unknown field {f}.")]
    out = []
    if f == "seo_title":
        n = len(new or "")
        if not SEO_TITLE[0] <= n <= SEO_TITLE[1]:
            out.append(_issue("warn", f, f"SEO title is {n} characters. Aim for {SEO_TITLE[0]}-{SEO_TITLE[1]}."))
    if f == "meta_desc":
        n = len(new or "")
        if not META[0] <= n <= META[1]:
            out.append(_issue("warn", f, f"Meta description is {n} characters. Aim for {META[0]}-{META[1]}."))
    if f == "title" and not (new or "").strip():
        out.append(_issue("error", f, "The title is empty."))
    if f == "slug":
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", new or ""):
            out.append(_issue("error", f, "A slug uses only lower-case letters, numbers and single dashes."))
        if (ch.get("old") or "") and new != ch.get("old"):
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
            h = "https://www.masterliveaboards.com" + h
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
