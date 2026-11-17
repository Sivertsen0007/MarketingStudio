"""One shared connection to Meta's Graph / Marketing API for AI-Hub (stdlib only).

Used by Ads Studio (shared/gae/studio/meta_ads.py), the dashboard connector (connectors/meta.py) and scripts.
Bump GRAPH_VERSION here and every user follows.

    import meta_api
    meta_api.get("act_123/campaigns", fields="id,name")
    meta_api.get_all("act_123/ads", fields="id,name")
    meta_api.post("<adset_id>", {"status": "PAUSED"}, validate=True)   # validate_only: changes nothing
    meta_api.usage()          # latest rate-usage headers
    meta_api.debug_token()    # token type, validity, expiry, scopes (never the token itself)

Token: META_ACCESS_TOKEN from appconfig (Keychain, or the environment). Never print or log it.

Retries:
- GET: up to 3 tries, with backoff, on rate limits, HTTP 5xx / transient Meta errors and network errors.
- POST: retried ONCE, and only when Meta answered with a rate-limit error (a refused call changed nothing).
  Never retried after a network error or timeout, because the write may have been applied.
"""
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Version check, 17 November 2026, from Meta for Developers:
#   https://developers.facebook.com/documentation/ads-commerce/marketing-api/marketing-api-changelog
#     Marketing API: v27.0 (4 November 2026, TBD), v26.0 (29 July 2026, TBD), v25.0 until 23 February 2027.
# Chosen: v27.0. Its changes that touch Studio's calls are handled in meta_ads.py (see V27 there):
#   - ad sets: "Reach more people across other mobile devices" turns on when targeting_automation.individual_setting
#     .device is left out, so Studio always sends it (0 unless the ad set already has it on);
#   - creatives: Meta may switch Product showcase on when product_extensions is left out, so Studio sends
#     degrees_of_freedom_spec.creative_features_spec.product_extensions.enroll_status OPT_OUT unless it is already set;
#   - Lookalike audiences: lookalike_spec.country / location_spec are refused (Studio creates no audiences);
#   - meta_moment_maker_spec is gone from ad set reads and writes (Studio never used it).
# Every Graph call Studio makes was checked on v27.0 (read-only GETs and validate_only POSTs) on 17 November 2026.
GRAPH_VERSION = "v27.0"
GRAPH_HOST = "https://graph.facebook.com"
API = f"{GRAPH_HOST}/{GRAPH_VERSION}"

TIMEOUT = 90
RATE_LIMIT_CODES = (4, 17, 32, 613, 80004)
GET_TRIES = 3
GET_BACKOFF_RATE = (15, 45)      # seconds before try 2 and 3 after a rate limit
GET_BACKOFF_OTHER = (2, 8)       # after 5xx / network errors
POST_RATE_WAIT = 35              # one patient retry for a rate-limited POST
USAGE_HEADERS = ("x-business-use-case-usage", "x-ad-account-usage", "x-app-usage")


def _load_env(path):
    out = {}
    try:
        for line in Path(path).read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


import appconfig  # noqa: E402

TOKEN = appconfig.get("META_ACCESS_TOKEN")


def _refresh():
    global TOKEN
    TOKEN = appconfig.get("META_ACCESS_TOKEN")


appconfig.on_change(_refresh)

_usage = {}
_usage_lock = threading.Lock()


class MetaError(Exception):
    """A Meta API error with a readable message. .code / .subcode from Meta, .status = HTTP status (if any)."""

    def __init__(self, err, status=None):
        self.err = err if isinstance(err, dict) else {"message": str(err)}
        e = self.err
        msg = e.get("error_user_msg") or e.get("message") or "Meta error"
        if e.get("error_user_title") and e.get("error_user_title") not in msg:
            msg = f"{e['error_user_title']}: {msg}"
        super().__init__(msg)
        self.code = e.get("code")
        self.subcode = e.get("error_subcode")
        self.status = status

    @property
    def rate_limited(self):
        return self.code in RATE_LIMIT_CODES

    @property
    def transient(self):
        """Server-side hiccup worth retrying on a read (HTTP 5xx or Meta's is_transient flag)."""
        return bool(self.err.get("is_transient")) or (self.status is not None and self.status >= 500)


class _NetworkError(Exception):
    """Internal: no answer from Meta (timeout, DNS, connection reset). Re-raised as MetaError."""


def _record_usage(headers):
    got = {}
    for h in USAGE_HEADERS:
        v = headers.get(h) if headers is not None else None
        if v:
            try:
                got[h] = json.loads(v)
            except ValueError:
                got[h] = v
    if got:
        with _usage_lock:
            _usage.update(got)
            _usage["at"] = time.strftime("%Y-%m-%dT%H:%M:%S")


def usage():
    """Latest usage headers seen from Meta, plus "at" (local time of the last update). Empty until a call is made."""
    with _usage_lock:
        return json.loads(json.dumps(_usage))


def _once(method, path, params=None, validate=False, token=None):
    q = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in dict(params or {}).items()}
    q["access_token"] = token or TOKEN
    q["locale"] = "en_GB"  # Meta otherwise answers in the token owner's language
    if validate:
        q["execution_options"] = json.dumps(["validate_only"])
    absolute = path.startswith("http")
    url = path if absolute else f"{API}/{path.lstrip('/')}"
    data = urllib.parse.urlencode(q).encode() if method == "POST" else None
    if method == "GET" and not absolute:  # paging "next" links already carry their query
        url += "?" + urllib.parse.urlencode(q)
    req = urllib.request.Request(url, data=data, method=method, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            _record_usage(r.headers)
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        _record_usage(e.headers)
        try:
            err = json.loads(e.read())["error"]
        except (ValueError, KeyError, TypeError, OSError):
            err = {"message": f"HTTP {e.code}"}
        raise MetaError(err, status=e.code) from None
    except (urllib.error.URLError, OSError) as e:  # timeout, DNS, reset; message never contains the URL
        reason = getattr(e, "reason", None) or e.__class__.__name__
        raise _NetworkError(f"no answer from Meta ({reason})") from None


def call(method, path, params=None, validate=False, retry_rate=True, token=None):
    """One API call with the retry rules above. Raises MetaError."""
    method = method.upper()
    if method == "GET":
        for i in range(GET_TRIES):
            last = i == GET_TRIES - 1
            try:
                return _once("GET", path, params, validate, token)
            except MetaError as ex:
                if last or not (ex.rate_limited or ex.transient):
                    raise
                time.sleep((GET_BACKOFF_RATE if ex.rate_limited else GET_BACKOFF_OTHER)[i])
            except _NetworkError as ex:
                if last:
                    raise MetaError({"message": str(ex)}) from None
                time.sleep(GET_BACKOFF_OTHER[i])
    try:
        return _once(method, path, params, validate, token)
    except MetaError as ex:
        if not (retry_rate and ex.rate_limited):
            raise
        time.sleep(POST_RATE_WAIT)
    except _NetworkError as ex:  # the write may have gone through: never retry, tell the caller
        raise MetaError({"message": f"{ex}; the change may or may not have been applied, check before retrying"}) from None
    try:
        return _once(method, path, params, validate, token)
    except _NetworkError as ex:
        raise MetaError({"message": f"{ex}; the change may or may not have been applied, check before retrying"}) from None


def get(path, **params):
    return call("GET", path, params)


def post(path, params, validate=False, retry_rate=True):
    """POST with one patient retry when Meta rate-limits. validate=True sends execution_options=validate_only."""
    return call("POST", path, params, validate=validate, retry_rate=retry_rate)


def get_all(path, **params):
    """GET every page of a list edge and return the combined "data" rows."""
    params.setdefault("limit", 500)
    out, page = [], get(path, **params)
    while True:
        out += page.get("data", [])
        nxt = page.get("paging", {}).get("next")
        if not nxt:
            return out
        page = call("GET", nxt)


def debug_token(token=None):
    """Type, validity, expiry and scopes of the token. The token itself is never returned."""
    t = token or TOKEN
    if not t:
        return {"is_valid": False, "error": "META_ACCESS_TOKEN is not set"}
    d = (get("debug_token", input_token=t) or {}).get("data", {})
    return {"type": d.get("type"), "is_valid": d.get("is_valid"), "expires_at": d.get("expires_at"),
            "scopes": d.get("scopes", [])}
