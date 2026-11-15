"""SEO area: shared helpers (data folder, caches, HTTP, URL keys, the Studio secrets file).

The module names start with an underscore so areas/__init__.py does not load them as their own area.
Secrets: the macOS Keychain via appconfig (a chmod-600 file when SEO_SECRETS_FILE is set, in tests). Passwords are never logged, echoed or returned.
"""
import contextlib
import fcntl
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ML_HOSTS = ["masterliveaboards.com", "www.masterliveaboards.com"]  # the site's own hosts (internal links)
SITES = {
    "prod": "https://www.masterliveaboards.com",
    "staging": "https://staging.masterliveaboards.com",
}
_BUILTIN = (list(ML_HOSTS), dict(SITES))


def apply_site_settings():
    """The SEO sites setting (team_settings, key seo_sites: this Mac's own or the team's) replaces the built-in prod /
    staging addresses where it gives one. Runs at import and whenever the settings change; edits in place, so every
    module that holds SITES / ML_HOSTS sees it."""
    hosts, sites = list(_BUILTIN[0]), dict(_BUILTIN[1])
    try:
        import team_settings  # noqa: PLC0415
        v = team_settings.get("seo_sites")
        team_settings.on_change(apply_site_settings)
    except Exception:  # noqa: BLE001 - no settings: the built-in sites
        v = {}
    for k in ("prod", "staging"):
        if v.get(k):
            sites[k] = v[k]
    if v.get("prod"):
        h = urllib.parse.urlparse(v["prod"]).hostname or ""
        bare = h[4:] if h.startswith("www.") else h
        hosts = [bare, "www." + bare] if bare else hosts
    SITES.clear()
    SITES.update(sites)
    ML_HOSTS[:] = hosts
SECRETS = Path(os.environ.get("SEO_SECRETS_FILE", str(Path.home() / ".claude" / "secrets" / "marketing-studio.env")))
UA = "MarketingStudio-SEO/1.0 (+local; read-only unless the user publishes)"


def data_dir():
    """core.STUDIO_DIR/seo, or SEO_STUDIO_DIR (tests)."""
    d = os.environ.get("SEO_STUDIO_DIR")
    if d:
        p = Path(d)
    else:
        import core  # noqa: PLC0415 - lazy, so tests can run without the Editor database
        p = Path(core.STUDIO_DIR)
    p.mkdir(parents=True, exist_ok=True)
    return p


def cache_dir():
    p = data_dir() / "seo_cache"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _safe(key):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", key)[:150]


def source_of(key):
    """datapolicy source of a cache key: Search Console, GA4 or WordPress."""
    return "seo.gsc" if key.startswith("gsc") else "seo.ga4" if key.startswith("ga4") else "seo.wp"


def _policy():
    try:
        import datapolicy  # noqa: PLC0415 - Studio's folder; tests without it fall back to plain ttl
        return datapolicy
    except ImportError:
        return None


def cached(key, ttl, fn, force=False):
    """JSON cache in seo_cache/<key>.json. When to fetch again is datapolicy's rule (manual mode, the default: only on
    a Refresh; auto: after ttl seconds). force fetches now."""
    f = cache_dir() / f"{_safe(key)}.json"
    dp = _policy()
    if not force and f.exists():
        mt = f.stat().st_mtime
        ok = dp.fresh(source_of(key), time.time() - mt, ttl, mt) if dp else time.time() - mt < ttl
        if ok:
            try:
                return json.loads(f.read_text())
            except ValueError:
                pass
    val = fn()
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(val, ensure_ascii=False))
    os.replace(tmp, f)
    if dp:
        dp.mark(source_of(key))
    return val


def newest(prefixes):
    """Epoch of the newest cache file starting with any of prefixes, or None."""
    ts = [f.stat().st_mtime for p in prefixes for f in cache_dir().glob(f"{_safe(p)}*.json")]
    return max(ts) if ts else None


def cache_age(key):
    f = cache_dir() / f"{_safe(key)}.json"
    return round(time.time() - f.stat().st_mtime) if f.exists() else None


def clear_cache(prefix=""):
    n = 0
    for f in cache_dir().glob(f"{_safe(prefix)}*.json"):
        if f.name.startswith("inspect"):
            continue  # URL Inspection results cost quota: keep them
        f.unlink()
        n += 1
    return n


@contextlib.contextmanager
def locked_json(name, default):
    """Read-modify-write a JSON file in the data folder under a file lock."""
    d = data_dir()
    path = d / name
    with open(d / f".{name}.lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        val = json.loads(path.read_text()) if path.exists() else default
        yield val
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(val, ensure_ascii=False, indent=1))
        os.replace(tmp, path)


def read_json(name, default):
    p = data_dir() / name
    try:
        return json.loads(p.read_text()) if p.exists() else default
    except ValueError:
        return default


def append_jsonl(name, row):
    with open(data_dir() / name, "a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_jsonl(name, limit=200):
    p = data_dir() / name
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines()[-limit:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


# ---------- HTTP ----------

def http(url, method="GET", body=None, headers=None, timeout=30):
    """Returns (status, headers dict, parsed JSON or text). Raises on network errors only."""
    h = {"User-Agent": UA, "Accept": "application/json", **(headers or {})}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw, status, hdrs = r.read(), r.status, dict(r.headers)
    except urllib.error.HTTPError as e:
        raw, status, hdrs = e.read(), e.code, dict(e.headers or {})
    txt = raw.decode("utf-8", "replace")
    try:
        return status, hdrs, json.loads(txt)
    except ValueError:
        return status, hdrs, txt


def err_text(body):
    if isinstance(body, dict):
        e = body.get("error")
        if isinstance(e, dict):
            return e.get("message") or json.dumps(e)[:300]
        return body.get("message") or str(e or body)[:300]
    return str(body)[:300]


# ---------- URLs ----------

def url_key(url):
    """One key for WP links, GSC pages and GA4 landing pages: the lower-case path without a trailing slash."""
    if not url:
        return ""
    u = urllib.parse.urlsplit(url.strip())
    if u.netloc and u.netloc.lower() not in ML_HOSTS:
        return ""
    path = urllib.parse.unquote(u.path or "/").lower()
    path = path.rstrip("/") or "/"
    return path


def is_internal(href):
    u = urllib.parse.urlsplit(href or "")
    if u.scheme and u.scheme not in ("http", "https"):
        return False
    return not u.netloc or u.netloc.lower() in ML_HOSTS


# ---------- secrets ----------

def _read_env():
    out = {}
    try:
        for line in SECRETS.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return out


def _file_mode():
    return bool(os.environ.get("SEO_SECRETS_FILE"))  # tests keep the keys in a file


def wp_creds():
    """(user, app password) or (None, None). Never log the password."""
    if _file_mode():
        e = _read_env()
        u, p = e.get("WP_STUDIO_USER"), e.get("WP_STUDIO_APP_PASSWORD")
    else:
        import appconfig  # noqa: PLC0415
        u, p = appconfig.get("WP_STUDIO_USER"), appconfig.get("WP_STUDIO_APP_PASSWORD")
    return (u, p) if u and p else (None, None)


def save_wp_creds(user, password):
    """Save the two WP keys (Keychain via appconfig; a chmod-600 file in tests)."""
    if not user or not password:
        raise ValueError("Enter the WordPress user name and the application password.")
    if "\n" in user + password or "=" in user:
        raise ValueError("The user name or password has characters that are not allowed.")
    if not _file_mode():
        import appconfig  # noqa: PLC0415
        appconfig.set_many({"WP_STUDIO_USER": user, "WP_STUDIO_APP_PASSWORD": password})
        return
    SECRETS.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(SECRETS.parent, 0o700)
    keep = []
    if SECRETS.exists():
        keep = [ln for ln in SECRETS.read_text().splitlines()
                if not ln.strip().startswith(("WP_STUDIO_USER=", "WP_STUDIO_APP_PASSWORD="))]
    lines = keep + [f"WP_STUDIO_USER={user}", f"WP_STUDIO_APP_PASSWORD={password}"]
    fd = os.open(str(SECRETS), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(SECRETS, 0o600)


def forget_wp_creds():
    if not _file_mode():
        import appconfig  # noqa: PLC0415
        appconfig.set_many({"WP_STUDIO_USER": None})
        appconfig._secret_set("WP_STUDIO_APP_PASSWORD", None)
        appconfig._cache.pop("WP_STUDIO_APP_PASSWORD", None)
        return
    if not SECRETS.exists():
        return
    keep = [ln for ln in SECRETS.read_text().splitlines()
            if not ln.strip().startswith(("WP_STUDIO_USER=", "WP_STUDIO_APP_PASSWORD="))]
    fd = os.open(str(SECRETS), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(keep) + ("\n" if keep else ""))


def scrub(text):
    """Remove the app password (and anything that looks like a bearer token) from a message."""
    s = str(text or "")
    _, p = wp_creds()
    if p:
        s = s.replace(p, "***").replace(p.replace(" ", ""), "***")
    return re.sub(r"(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", r"\1 ***", s)


apply_site_settings()
