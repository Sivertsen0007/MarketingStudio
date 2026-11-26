"""Email area: email campaigns from your email platform, in Marketing Studio.

Read campaigns and stats, edit drafts (in Studio first, then "Save to <platform>"), check them, and only from the
user's own buttons: Send test, Schedule, Send now and Cancel schedule.

Providers (how Studio talks to an email platform). adapter() picks the first one that is set up and answers:
  EmailApiAdapter   the Marketing Studio Email API: a small REST contract any platform can implement
                    (EMAIL_API_URL + EMAIL_API_TOKEN). Full read + write. The contract is in the README.
  McpAdapter        the platform's MCP server over HTTP (EMAIL_MCP_URL + EMAIL_MCP_TOKEN), using the email_* tools
                    listed in MCP_TOOLS. Reads, plus creating a new draft.
  FakeAdapter       demo data in a JSON file when nothing is connected. Actions only change fake state.
To add a platform: write a class with the same methods and `can` flags as EmailApiAdapter and put it in PROVIDERS.

Personal data: Studio only ever reads counts and list or segment names, never subscriber rows or addresses.
Claude may only draft: list_drafts() and add_draft() are the functions an MCP tool may call. The live actions
(test_send, schedule, send_now, cancel) need confirm="user-pressed" from the UI button and have no MCP tool.
"""
import concurrent.futures
import datetime as dt
import html as htmllib
import json
import os
import re
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo

import draftstore

LABEL = "Email"
KEY = "email"  # progress key
FROM_ADDRESS = ""  # set from Setup (EMAIL_FROM_ADDRESS); empty = the platform decides
TEST_ADDRESS = "the test address"  # the first of TEST_ADDRESSES (shown when no address is set up)
TEST_ADDRESSES = []  # Setup: STUDIO_TEST_EMAIL, one or more addresses; with several you pick one per test
PLATFORM = "your email platform"  # set from Setup (EMAIL_PLATFORM_NAME), used in labels and messages
IMAGE_HOST = ""  # optional (EMAIL_IMAGE_HOST): warn when an image is not on this host
OWN_DOMAINS = ()  # business domains whose addresses may be shown (from the from-address + EMAIL_OWN_DOMAINS)
LONDON = ZoneInfo("Europe/London")  # the send timezone (EMAIL_TIMEZONE)


def test_addresses(raw):
    """'a@x.com, b@x.com' (commas, semicolons, spaces or new lines) -> ['a@x.com', 'b@x.com'], lower-case, no repeats."""
    out = []
    for part in re.split(r"[\s,;]+", raw or ""):
        part = part.strip().lower()
        if part and re.fullmatch(r"[a-z0-9._%+'-]+@[a-z0-9.-]+\.[a-z]{2,}", part) and part not in out:
            out.append(part)
    return out


def _load_test_address():
    """Read the email settings from Setup (appconfig). Runs at import and whenever keys change."""
    global TEST_ADDRESS, TEST_ADDRESSES, FROM_ADDRESS, PLATFORM, IMAGE_HOST, OWN_DOMAINS, LONDON
    try:
        import appconfig  # noqa: PLC0415
    except ImportError:
        return
    TEST_ADDRESSES = test_addresses(appconfig.get("STUDIO_TEST_EMAIL"))
    try:  # the team's test addresses replace this Mac's own (team_settings, key email_test_addresses)
        import team_settings  # noqa: PLC0415
        tv = team_settings.team_value("email_test_addresses")
        if tv and tv["addresses"]:
            TEST_ADDRESSES = list(tv["addresses"])
        team_settings.on_change(_load_test_address)
    except Exception:  # noqa: BLE001
        pass
    TEST_ADDRESS = TEST_ADDRESSES[0] if TEST_ADDRESSES else "the test address"
    FROM_ADDRESS = appconfig.get("EMAIL_FROM_ADDRESS").strip().lower()
    PLATFORM = appconfig.get("EMAIL_PLATFORM_NAME") or "your email platform"
    IMAGE_HOST = appconfig.get("EMAIL_IMAGE_HOST").strip().lower()
    doms = [d.strip().lower() for d in appconfig.get("EMAIL_OWN_DOMAINS").split(",") if d.strip()]
    if "@" in FROM_ADDRESS:
        doms.append(FROM_ADDRESS.split("@")[1])
    OWN_DOMAINS = tuple(dict.fromkeys(doms))
    try:
        LONDON = ZoneInfo(appconfig.get("EMAIL_TIMEZONE") or "Europe/London")
    except Exception:  # noqa: BLE001 - a typo in Setup must not break the area
        LONDON = ZoneInfo("Europe/London")
    if _load_test_address not in appconfig._listeners:
        appconfig.on_change(_load_test_address)


SECRETS = "macOS Keychain (Marketing Studio)"
EDITABLE = ("name", "subject", "preview_text", "html", "text", "from_name", "list_id", "segment_id")
STATUSES = ("draft", "scheduled", "sending", "sent", "cancelled", "failed")
CONFIRM = "user-pressed"

_lock = threading.RLock()
DATA_DIR = None  # tests set this; otherwise core.STUDIO_DIR


def _data_dir():
    d = DATA_DIR
    if d is None:
        import core
        d = core.STUDIO_DIR
    d = Path(d)
    d.mkdir(parents=True, exist_ok=True)
    return d


def campaigns_root():
    import appconfig
    return Path(appconfig.get("STUDIO_EMAIL_FOLDER") or appconfig.DATA / "email-campaigns").expanduser()


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _iso(t):
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(s):
    if not s:
        return None
    t = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def _write_json(path, data):
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


def _read_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, ValueError):
        return default


# ---------------------------------------------------------------- settings (token never leaves this module)

def _file_read_env():
    out = {}
    try:
        for line in SECRETS.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return out


def _file_write_env(updates):
    """Change only our keys; other areas may keep their own keys in the same file. chmod 600."""
    with _lock:
        SECRETS.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(SECRETS.parent, 0o700)
        except OSError:
            pass
        try:
            lines = SECRETS.read_text().splitlines()
        except FileNotFoundError:
            lines = []
        seen, out = set(), []
        for line in lines:
            k = line.split("=", 1)[0].strip() if "=" in line and not line.strip().startswith("#") else None
            if k in updates:
                seen.add(k)
                if updates[k] is not None:
                    out.append(f"{k}={updates[k]}")
            else:
                out.append(line)
        out += [f"{k}={v}" for k, v in updates.items() if k not in seen and v is not None]
        fd, tmp = tempfile.mkstemp(dir=SECRETS.parent, prefix=".mstudio.")
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(out) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, SECRETS)


EMAIL_KEYS = ("EMAIL_API_URL", "EMAIL_API_TOKEN", "EMAIL_MCP_URL", "EMAIL_MCP_TOKEN", "EMAIL_PLATFORM_NAME")


def _read_env():
    if isinstance(SECRETS, Path):  # tests point SECRETS at a temp file
        return _file_read_env()
    import appconfig  # noqa: PLC0415
    return {k: appconfig.get(k) for k in EMAIL_KEYS if appconfig.get(k)}


def _write_env(updates):
    """Keys go to the Keychain / config.json via appconfig (a file in tests). None removes a key."""
    if isinstance(SECRETS, Path):
        return _file_write_env(updates)
    import appconfig  # noqa: PLC0415
    appconfig.set_many(dict(updates))


def settings():
    env = _read_env()
    return {"api_url": (env.get("EMAIL_API_URL") or "").rstrip("/"), "token": env.get("EMAIL_API_TOKEN", ""),
            "mcp_url": (env.get("EMAIL_MCP_URL") or "").rstrip("/"), "mcp_token": env.get("EMAIL_MCP_TOKEN", ""),
            "name": env.get("EMAIL_PLATFORM_NAME") or PLATFORM}


NEEDS_API = "Needs the Email API connection (Setup > Email: API URL + token)"
PROBE_TTL = 300
_probes = {}    # hash(kind, base, token) -> (time, ok, message)
_adapters = {}  # hash(kind, base, token) -> adapter (so the MCP session and caches live across requests)


def _key(*parts):
    import hashlib
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()[:16]


def _cached_adapter(kind, url, token):
    k = _key(kind, url, token)
    if k not in _adapters:
        _adapters[k] = PROVIDERS[kind](url, token)
    return _adapters[k]


def _policy():
    try:
        import datapolicy  # noqa: PLC0415 - Studio's folder
        return datapolicy
    except ImportError:  # pragma: no cover
        return None


def _fresh_ok(age, ttl, filled_at):
    """datapolicy's rule for the Email source (manual: kept until Refresh; auto: ttl seconds)."""
    dp = _policy()
    return dp.fresh("email", age, ttl, filled_at) if dp else age < ttl


def _mark():
    dp = _policy()
    if dp:
        dp.mark("email")


def probe(a, fresh=False):
    """(ok, message) for a real adapter: a cheap read (one row) that picks Studio API or MCP. Kept like the rest of the
    Email data (datapolicy: until Refresh in manual mode, 5 minutes in auto); Test connection and Refresh probe again."""
    k = _key(a.source, a.base_url, a._token)
    hit = _probes.get(k)
    if hit and not fresh and _fresh_ok(time.time() - hit[0], PROBE_TTL, hit[0]):
        return hit[1], hit[2]
    try:
        a.ping()
        res = (True, "answered")
    except AdapterError as ex:
        res = (False, str(ex))
    _probes[k] = (time.time(), *res)
    return res


def adapter():
    """The Email API if it is set up and answers, else the MCP if it is set up, else demo data."""
    s = settings()
    if s["api_url"] and s["token"]:
        api = _cached_adapter("api", s["api_url"], s["token"])
        if probe(api)[0]:
            return api
    if s["mcp_url"] and s["mcp_token"]:
        return _cached_adapter("mcp", s["mcp_url"], s["mcp_token"])
    return FakeAdapter()


_load_test_address()


# ---------------------------------------------------------------- adapters

class AdapterError(ValueError):
    def __init__(self, msg, code=None):
        super().__init__(msg)
        self.code = code


class EmailApiAdapter:
    """HTTP client for the Marketing Studio Email API (see the README for the contract). Errors never contain the token."""
    demo = False
    source = "api"
    label = "Connected (Email API)"
    can = {"edit": True, "test": True, "schedule": True, "create": True, "segments_on_create": True, "delete": True}

    def __init__(self, api_url, token, timeout=20):
        self.base_url = api_url.rstrip("/")
        self.base = self.base_url
        self._token = token
        self.timeout = timeout
        self._mem = {}  # reads kept in memory only (datapolicy "email"); never written to disk

    def _cached(self, key, fn, fresh=False):
        hit = self._mem.get(key)
        if hit and not fresh and _fresh_ok(time.time() - hit[0], MCP_TTL, hit[0]):
            return hit[1]
        v = fn()
        self._mem[key] = (time.time(), v)
        _mark()
        return v

    def forget(self):
        self._mem.clear()

    def _req(self, method, path, body=None, confirm=False, timeout=None):
        if method != "GET" and not path.endswith(("/preview", "/checks")):
            self.forget()  # the user changed something on the platform: read it again next time
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Authorization", "Bearer " + self._token)
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", "MarketingStudio/1")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if confirm:
            req.add_header("X-Studio-Confirm", CONFIRM)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                raw = r.read()
        except urllib.error.HTTPError as ex:
            try:
                msg = json.loads(ex.read()).get("error") or ex.reason
            except Exception:  # noqa: BLE001
                msg = ex.reason
            raise AdapterError(f"{PLATFORM[0].upper() + PLATFORM[1:]} said {ex.code}: {msg}", ex.code) from None
        except (urllib.error.URLError, TimeoutError, OSError) as ex:
            raise AdapterError(f"{PLATFORM[0].upper() + PLATFORM[1:]} did not answer: {getattr(ex, 'reason', ex)}") from None
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            raise AdapterError("The Email API answered, but not with JSON (check the API URL)") from None

    def ping(self):
        self._req("GET", "/campaigns?limit=1", timeout=6)

    def list_campaigns(self, status=None, limit=None, fresh=False):
        q = urllib.parse.urlencode({k: v for k, v in {"status": status, "limit": limit}.items() if v})
        return self._cached("campaigns?" + q, lambda: self._req("GET", "/campaigns" + ("?" + q if q else ""))["campaigns"], fresh)

    def get_campaign(self, cid, fresh=False):
        return self._cached(f"campaign:{cid}", lambda: self._req("GET", f"/campaigns/{urllib.parse.quote(str(cid))}")["campaign"], fresh)

    def audiences(self, fresh=False):
        def get():
            r = self._req("GET", "/audiences")
            return {"lists": r.get("lists", []), "segments": r.get("segments", [])}
        return self._cached("audiences", get, fresh)

    def create(self, fields):
        return self._req("POST", "/campaigns", fields)["campaign"]

    def patch(self, cid, fields):
        return self._req("PATCH", f"/campaigns/{urllib.parse.quote(str(cid))}", fields)["campaign"]

    def preview(self, cid):
        return self._req("POST", f"/campaigns/{urllib.parse.quote(str(cid))}/preview", {})["html"]

    def server_checks(self, cid):
        return self._req("POST", f"/campaigns/{urllib.parse.quote(str(cid))}/checks", {})

    def test_send(self, cid, to=None):
        return self._req("POST", f"/campaigns/{urllib.parse.quote(str(cid))}/test-send", {"to": to} if to else {}, confirm=True)

    def schedule(self, cid, when_iso):
        return self._req("POST", f"/campaigns/{urllib.parse.quote(str(cid))}/schedule", {"scheduled_for": when_iso}, confirm=True)["campaign"]

    def cancel(self, cid):
        return self._req("POST", f"/campaigns/{urllib.parse.quote(str(cid))}/cancel", {}, confirm=True)["campaign"]

    def progress(self, cid):
        return self._req("GET", f"/campaigns/{urllib.parse.quote(str(cid))}/progress")

    def delete(self, cid):
        return self._req("DELETE", f"/campaigns/{urllib.parse.quote(str(cid))}", confirm=True)


EMAIL_RE = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
MCP_TTL = 600  # 10 minutes


def _scrub(v):
    """Drop any email address that is not one of our own business addresses (a person's address never reaches Studio)."""
    if not isinstance(v, str):
        return v
    return EMAIL_RE.sub(lambda m: m.group(0) if m.group(0).lower().split("@")[1].endswith(OWN_DOMAINS) else "[address removed]", v)


def _pick(d, keys):
    """Whitelist: only these keys (source name -> Studio name), strings scrubbed. Everything else is dropped."""
    d = d if isinstance(d, dict) else {}
    return {dst: _scrub(d.get(src)) for src, dst in keys.items()}


_CAMPAIGN_KEYS = {"id": "id", "name": "name", "subject": "subject", "status": "status", "listId": "list_id", "listName": "list_name",
                  "segmentId": "segment_id", "segmentName": "segment_name", "scheduledFor": "scheduled_for", "sentAt": "sent_at",
                  "fromName": "from_name", "fromAddress": "from_address", "replyTo": "reply_to", "updatedAt": "updated_at",
                  "sendError": "send_error", "folderName": "folder_name"}


class McpAdapter:
    """The email platform's MCP server (JSON-RPC 2.0 over HTTP POST, JSON or SSE replies), using the tools in MCP_TOOLS.

    Reads only, plus create_draft_campaign from the user's button. HTML updates, test sends, scheduling, sending and
    cancelling are not on the MCP, so they need the Studio API. save_campaign_blocks is never called: it rewrites a
    campaign from blocks and broke a raw-HTML campaign before.
    Personal data: every tool result is cut down to a whitelist of fields (counts and names of lists and segments,
    never subscribers) and any stray email address is removed before it is cached or shown.
    """
    demo = False
    source = "mcp"
    label = "Live data through MCP (read-only)"
    can = {"edit": False, "test": False, "schedule": False, "create": True, "segments_on_create": False, "delete": False}
    PROTOCOL = "2025-06-18"

    def __init__(self, mcp_url, token, timeout=20, cache_path=None):
        self.base_url = mcp_url.rstrip("/")
        self.url = self.base_url
        self._token = token
        self.timeout = timeout
        self._id = 0
        self._session = None
        self._ready = False
        self._cache_path = Path(cache_path) if cache_path else None
        self._tag = _key("mcp-cache", self.base_url, token)  # cache belongs to this token; never the token itself

    # -- transport
    def _post(self, payload, timeout=None):
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode(), method="POST")
        req.add_header("Authorization", "Bearer " + self._token)
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json, text/event-stream")
        req.add_header("MCP-Protocol-Version", self.PROTOCOL)
        req.add_header("User-Agent", "MarketingStudio/1")
        if self._session:
            req.add_header("Mcp-Session-Id", self._session)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                self._session = r.headers.get("Mcp-Session-Id") or self._session
                raw, ctype = r.read(), r.headers.get("Content-Type", "")
        except urllib.error.HTTPError as ex:
            if ex.code == 401:
                raise AdapterError("The MCP server refused the token (401). Check it in Email > Connect, and that it has access to email.", 401) from None
            raise AdapterError(f"The MCP server said {ex.code}: {ex.reason}", ex.code) from None
        except (urllib.error.URLError, TimeoutError, OSError) as ex:
            raise AdapterError(f"{PLATFORM[0].upper() + PLATFORM[1:]} did not answer: {getattr(ex, 'reason', ex)}") from None
        if not raw:
            return None
        if "text/event-stream" in ctype:  # streamable HTTP may answer as SSE; take the last data line
            lines = [l[5:].strip() for l in raw.decode().splitlines() if l.startswith("data:")]
            raw = (lines[-1] if lines else "{}").encode()
        try:
            return json.loads(raw)
        except ValueError:
            raise AdapterError("The MCP server answered, but not with JSON") from None

    def _rpc(self, method, params=None, timeout=None):
        self._id += 1
        r = self._post({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}, timeout)
        if not isinstance(r, dict):
            raise AdapterError("The MCP server gave an empty answer")
        if r.get("error"):
            raise AdapterError(f"MCP error: {r['error'].get('message', 'unknown')}")
        return r.get("result") or {}

    def _init(self, timeout=None):
        if self._ready:
            return
        self._rpc("initialize", {"protocolVersion": self.PROTOCOL, "capabilities": {},
                                 "clientInfo": {"name": "marketing-studio", "version": "1"}}, timeout)
        try:
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, timeout)
        except AdapterError:
            pass
        self._ready = True

    def call(self, tool, args=None, timeout=None):
        """tools/call with the wire name (dots become underscores). Returns the parsed JSON text of the result."""
        self._init(timeout)
        res = self._rpc("tools/call", {"name": tool.replace(".", "_"), "arguments": args or {}}, timeout)
        text = "".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
        if res.get("isError"):
            raise AdapterError("MCP: " + _scrub(text[7:] if text.startswith("Error: ") else text)[:300])
        try:
            return json.loads(text) if text else None
        except ValueError:
            return {"text": _scrub(text)}

    # -- cache (sanitised data only)
    def _cpath(self):
        return self._cache_path or (_data_dir() / "email_mcp_cache.json")

    def _cached(self, key, fn, fresh=False, ttl=MCP_TTL):
        with _lock:
            c = _read_json(self._cpath(), {})
            if c.get("tag") != self._tag:
                c = {"tag": self._tag, "items": {}}
            hit = c["items"].get(key)
            if hit and not fresh and _fresh_ok(time.time() - hit["t"], ttl, hit["t"]):
                return hit["v"]
        v = fn()
        with _lock:
            c = _read_json(self._cpath(), {})
            if c.get("tag") != self._tag:
                c = {"tag": self._tag, "items": {}}
            now = time.time()
            dp = _policy()
            if not dp or not dp.manual():  # auto mode: drop old entries; manual keeps them until Refresh
                c["items"] = {k: x for k, x in c["items"].items() if now - x["t"] < ttl * 3}
            c["items"][key] = {"t": now, "v": v}
            _write_json(self._cpath(), c)
        _mark()
        return v

    def forget(self):
        with _lock:
            _write_json(self._cpath(), {"tag": self._tag, "items": {}})

    # -- reads
    def ping(self):
        self.call("email.overview", timeout=8)

    def _stats(self, cid, fresh=False):
        def get():
            s = self.call("email.campaign_stats", {"campaignId": cid}) or {}
            return {"sent": s.get("sent") or 0, "delivered": s.get("delivered") or 0, "opens": s.get("opened") or 0,
                    "clicks": s.get("clicked") or 0, "bounces": s.get("bounced") or 0, "complaints": s.get("complained") or 0,
                    "unsubscribes": None, "total": s.get("total") or 0, "failed": s.get("failed") or 0}
        return self._cached(f"stats:{cid}", get, fresh)

    def audiences(self, fresh=False):
        def get():
            lists = [{**_pick(x, {"id": "id", "name": "name"}), "count": int(x.get("activeCount") if x.get("activeCount") is not None else x.get("memberCount") or 0)}
                     for x in (self.call("email.list_lists") or []) if isinstance(x, dict)]
            segs = [_pick(x, {"id": "id", "name": "name"}) for x in (self.call("email.list_segments") or []) if isinstance(x, dict)]

            def count(sg):
                try:
                    return {**sg, "count": int((self.call("email.segment_count", {"segmentId": sg["id"]}) or {}).get("count") or 0)}
                except AdapterError:
                    return {**sg, "count": None}
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
                segs = list(ex.map(count, segs))
            return {"lists": lists, "segments": segs}
        return self._cached("audiences", get, fresh)

    def _with_audience(self, c, aud):
        n = None
        if c.get("segment_id"):
            n = next((x["count"] for x in aud["segments"] if x["id"] == c["segment_id"]), None)
        elif c.get("list_id"):
            n = next((x["count"] for x in aud["lists"] if x["id"] == c["list_id"]), None)
        return {**c, "audience_count": n}

    def list_campaigns(self, status=None, limit=None, fresh=False):
        args = {"limit": int(limit or 100)}
        if status:
            args["status"] = status
        rows = self._cached("campaigns:" + json.dumps(args, sort_keys=True),
                            lambda: [_pick(x, _CAMPAIGN_KEYS) for x in (self.call("email.list_campaigns", args) or []) if isinstance(x, dict)], fresh)
        aud = self.audiences(fresh=False)
        zero = {"sent": 0, "delivered": 0, "opens": 0, "clicks": 0, "bounces": 0, "complaints": 0, "unsubscribes": None}

        def full(c):
            st = self._stats(c["id"], fresh) if c.get("status") in ("sending", "sent", "failed", "cancelled") else dict(zero)
            return {**self._with_audience(c, aud), "stats": st}
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
            return list(ex.map(full, rows))

    def get_campaign(self, cid, fresh=False):
        def get():
            c = self.call("email.get_campaign", {"id": str(cid)})
            if not c:
                raise AdapterError("campaign not found", 404)
            out = _pick(c, _CAMPAIGN_KEYS)
            out.update(html=c.get("html") or "", text=c.get("text") or "", preview_text=None,
                       authored="blocks" if c.get("blocks") else "html")
            return out
        c = self._cached(f"campaign:{cid}", get, fresh)
        st = self._stats(c["id"], fresh) if c.get("status") in ("sending", "sent", "failed", "cancelled") else \
            {"sent": 0, "delivered": 0, "opens": 0, "clicks": 0, "bounces": 0, "complaints": 0, "unsubscribes": None}
        return {**self._with_audience(c, self.audiences()), "stats": st}

    def merge_tags(self):
        """The platform's merge tags (email_list_merge_tags, e.g. {note, mergeTags: [{token: "{{first_name}}", label}]}),
        cached. None when the server has no such tool, fails or answers in a shape Studio can't read: that is not
        cached, so the next check asks again."""
        def get():
            r = self.call("email.list_merge_tags", timeout=8)
            tags = parse_merge_tags(r)
            if tags is None:
                raise AdapterError("the platform's merge tag list could not be read")
            return tags
        try:
            return self._cached("merge_tags2", get)  # a failure raises before anything is written; "2": older builds cached []
        except Exception:  # noqa: BLE001 - optional tool
            return None

    def overview_numbers(self, fresh=False):
        return self._cached("overview", lambda: {k: v for k, v in (self.call("email.overview") or {}).items() if isinstance(v, (int, float))}, fresh)

    def preview(self, cid):
        # compile_preview compiles a block document, not raw HTML, and leaves merge tags as they are: so Studio renders locally.
        return render_local(self.get_campaign(cid).get("html") or "")

    def server_checks(self, cid):
        return {"ok": True, "problems": []}

    # -- the one write: a new draft, from the user's button
    def create(self, fields):
        if fields.get("segment_id"):
            raise AdapterError("Through the MCP a new draft can only be addressed to a list. Pick a list, or set the segment on the platform.")
        args = {"name": fields.get("name"), "subject": fields.get("subject") or fields.get("name"), "html": fields.get("html") or STARTER_HTML,
                **({"fromAddress": FROM_ADDRESS, "replyTo": FROM_ADDRESS} if FROM_ADDRESS else {}), **({"fromName": fields["from_name"]} if fields.get("from_name") else {})}
        if fields.get("list_id"):
            args["listId"] = fields["list_id"]
        if fields.get("text"):
            args["text"] = fields["text"]
        r = self.call("email.create_draft_campaign", args) or {}
        if not r.get("id"):
            raise AdapterError("The platform did not return the new draft's id")
        self.forget()
        return self.get_campaign(r["id"], fresh=True)

    # -- not on the MCP
    def patch(self, cid, fields):
        raise AdapterError(NEEDS_API)

    def test_send(self, cid, to=None):
        raise AdapterError(NEEDS_API)

    def schedule(self, cid, when_iso):
        raise AdapterError(NEEDS_API)

    def cancel(self, cid):
        raise AdapterError(NEEDS_API)

    def progress(self, cid):
        c = self.get_campaign(cid, fresh=True)
        s = c["stats"]
        return {"status": c["status"], "total": s.get("total") or c.get("audience_count") or 0, "sent": s.get("sent", 0), "failed": s.get("failed", 0)}


STARTER_HTML = """<!doctype html>
<html>
<body style="margin:0;background:#f4f4f4">
<table width="100%" cellpadding="0" cellspacing="0"><tr><td align="center">
  <table width="600" style="max-width:600px;background:#fff;font-family:Arial,sans-serif;color:#1c2b39">
    <tr><td style="padding:24px">
      <p>Hi {{first_name}},</p>
      <p>Write your email here.</p>
      <p style="font-size:12px;color:#777">Demo Brand, hello@example.com<br>
        <a href="{{unsubscribe_url}}">Unsubscribe</a></p>
    </td></tr>
  </table>
</td></tr></table>
</body>
</html>
"""


_SAMPLE_FOOT = """      <p style="font-size:12px;color:#777">Demo Brand, hello@example.com<br>
        <a href="{{unsubscribe_url}}">Unsubscribe</a></p>"""


def _sample_html(title, body, link="https://www.example.com/"):
    """Demo email: a plain layout, no images (so the demo never points at a file that doesn't exist)."""
    return f"""<!doctype html>
<html>
<body style="margin:0;background:#f4f4f4">
<table width="100%" cellpadding="0" cellspacing="0"><tr><td align="center">
  <table width="600" style="max-width:600px;background:#fff;font-family:Arial,sans-serif;color:#1c2b39">
    <tr><td style="background:#0b2545;color:#fff;padding:18px 24px;font-size:18px">Master Liveaboards</td></tr>
    <tr><td style="padding:24px">
      <h1 style="font-size:24px;margin:0 0 12px">{title}</h1>
      <p>Hi {{{{first_name}}}},</p>
      <p>{body}</p>
      <p><a href="{link}" style="background:#0b2545;color:#fff;padding:12px 20px;text-decoration:none;border-radius:4px">See the trips</a></p>
{_SAMPLE_FOOT}
    </td></tr>
  </table>
</td></tr></table>
</body>
</html>
"""


# ---------------------------------------------------------------- providers

PROVIDERS = {"api": EmailApiAdapter, "mcp": McpAdapter}  # add a platform here (and in adapter())
StudioApiAdapter = EmailApiAdapter  # older name
# The MCP tools Studio calls on the platform's MCP server (the MCP side of the contract, see the README).
MCP_TOOLS = ("email_overview", "email_list_campaigns", "email_get_campaign", "email_campaign_stats", "email_list_lists",
             "email_list_segments", "email_segment_count", "email_create_draft_campaign")


def _seed():
    now = _now()
    d = lambda days, h=9: _iso((now + dt.timedelta(days=days)).replace(hour=h, minute=0, second=0, microsecond=0))
    sender = FROM_ADDRESS or "hello@example.com"
    base = {"from_name": "Demo Brand", "from_address": sender, "reply_to": sender, "text": ""}
    rows = [
        {"id": "demo-1", "name": "October newsletter", "subject": "Autumn trips with spaces left", "status": "sent",
         "list_id": "l1", "segment_id": None, "scheduled_for": d(-24), "sent_at": d(-24),
         "stats": {"sent": 18420, "delivered": 18102, "opens": 6880, "clicks": 911, "bounces": 318, "unsubscribes": 41},
         "preview_text": "A few spaces left this autumn", "html": _sample_html("Autumn trips", "Here are the trips with spaces left this autumn.")},
        {"id": "demo-2", "name": "Spring specials", "subject": "New dates for spring", "status": "sent",
         "list_id": None, "segment_id": "s1", "scheduled_for": d(-5), "sent_at": d(-5),
         "stats": {"sent": 6210, "delivered": 6150, "opens": 2575, "clicks": 402, "bounces": 60, "unsubscribes": 12},
         "preview_text": "New spring dates are open", "html": _sample_html("Spring dates", "New spring dates are now open for booking.")},
        {"id": "demo-3", "name": "Black Friday early access", "subject": "Early access starts Monday", "status": "scheduled",
         "list_id": "l1", "segment_id": None, "scheduled_for": d(3), "sent_at": None,
         "stats": {"sent": 0, "delivered": 0, "opens": 0, "clicks": 0, "bounces": 0, "unsubscribes": 0},
         "preview_text": "Our subscribers see the offers first", "html": _sample_html("Early access", "Our subscribers see the offers first.")},
        {"id": "demo-4", "name": "New season", "subject": "The new season opens", "status": "draft",
         "list_id": None, "segment_id": "s2", "scheduled_for": None, "sent_at": None,
         "stats": {"sent": 0, "delivered": 0, "opens": 0, "clicks": 0, "bounces": 0, "unsubscribes": 0},
         "preview_text": "Dates and spaces for the new season", "html": _sample_html("The new season", "Dates and spaces for the new season are open.")},
        {"id": "demo-5", "name": "Test draft with problems", "subject": "Book now, results guaranteed!", "status": "draft",
         "list_id": "l2", "segment_id": None, "scheduled_for": None, "sent_at": None,
         "stats": {"sent": 0, "delivered": 0, "opens": 0, "clicks": 0, "bounces": 0, "unsubscribes": 0},
         "preview_text": "", "html": '<html><body><img src="https://staging.example.com/x.webp"><p>Guaranteed results, from 900.</p>'
                                     '<a href="https://www.example.com/no-such-page">Book</a></body></html>'},
    ]
    return {"campaigns": [{**base, **r, "updated_at": _iso(now)} for r in rows],
            "lists": [{"id": "l1", "name": "All subscribers", "count": 18420}, {"id": "l2", "name": "Partners", "count": 640}],
            "segments": [{"id": "s1", "name": "Spring interest", "count": 6210}, {"id": "s2", "name": "Engaged last 90 days", "count": 4385}],
            "progress": {}}


class FakeAdapter:
    """A fake email platform in a JSON file. Same shape as the Email API. Sending is simulated in a few seconds."""
    demo = True
    source = "demo"
    label = "Demo data: no email platform connected."
    can = {"edit": True, "test": True, "schedule": True, "create": True, "segments_on_create": True, "delete": True}
    SEND_SECONDS = 6.0

    def __init__(self, path=None):
        self.path = Path(path) if path else _data_dir() / "email_fake.json"

    def _load(self):
        st = _read_json(self.path, None)
        if not st:
            st = _seed()
            _write_json(self.path, st)
        return st

    def _save(self, st):
        _write_json(self.path, st)

    def _aud(self, st, c):
        if c.get("segment_id"):
            s = next((x for x in st["segments"] if x["id"] == c["segment_id"]), None)
            return None, s
        lst = next((x for x in st["lists"] if x["id"] == c.get("list_id")), None)
        return lst, None

    def _tick(self, st):
        """Move scheduled campaigns whose time has come through sending to sent."""
        now, changed = _now(), False
        for c in st["campaigns"]:
            if c["status"] in ("scheduled", "sending") and _parse(c["scheduled_for"]) and _parse(c["scheduled_for"]) <= now:
                lst, seg = self._aud(st, c)
                total = (seg or lst or {}).get("count", 0)
                start = _parse(c["scheduled_for"])
                done = min(total, int(total * max((now - start).total_seconds(), 0) / self.SEND_SECONDS))
                st["progress"][c["id"]] = {"status": "sending", "total": total, "sent": done, "failed": 0}
                c["status"] = "sending"
                c["stats"]["sent"] = done
                if done >= total:
                    c["status"], c["sent_at"] = "sent", _iso(now)
                    c["stats"].update({"sent": total, "delivered": int(total * 0.98), "opens": int(total * 0.38),
                                       "clicks": int(total * 0.05), "bounces": total - int(total * 0.98), "unsubscribes": int(total * 0.002)})
                    st["progress"][c["id"]]["status"] = "sent"
                changed = True
        if changed:
            self._save(st)
        return st

    def _out(self, st, c, full=False):
        lst, seg = self._aud(st, c)
        keys = ("id", "name", "subject", "status", "list_id", "segment_id", "scheduled_for", "sent_at", "stats")
        out = {k: c.get(k) for k in keys}
        out.update(list_name=(lst or {}).get("name"), segment_name=(seg or {}).get("name"), audience_count=(seg or lst or {}).get("count", 0))
        out["stats"] = dict(c["stats"])
        if full:
            out.update({k: c.get(k) for k in ("preview_text", "html", "text", "from_name", "from_address", "reply_to", "updated_at")})
        return out

    def _find(self, st, cid):
        c = next((x for x in st["campaigns"] if x["id"] == str(cid)), None)
        if not c:
            raise AdapterError("campaign not found", 404)
        return c

    def list_campaigns(self, status=None, limit=None, fresh=False):
        with _lock:
            st = self._tick(self._load())
            rows = [self._out(st, c) for c in st["campaigns"] if not status or c["status"] == status]
        return rows[: int(limit)] if limit else rows

    def get_campaign(self, cid, fresh=False):
        with _lock:
            st = self._tick(self._load())
            return self._out(st, self._find(st, cid), full=True)

    def audiences(self, fresh=False):
        st = self._load()
        return {"lists": [dict(x) for x in st["lists"]], "segments": [dict(x) for x in st["segments"]]}

    def create(self, fields):
        with _lock:
            st = self._load()
            c = {"id": "demo-" + uuid.uuid4().hex[:6], "name": fields.get("name") or "Untitled", "subject": fields.get("subject", ""),
                 "status": "draft", "list_id": fields.get("list_id"), "segment_id": fields.get("segment_id"), "scheduled_for": None,
                 "sent_at": None, "preview_text": "", "html": fields.get("html") or _sample_html("New email", "Write your email here."),
                 "text": "", "from_name": "Demo Brand", "from_address": FROM_ADDRESS or "hello@example.com", "reply_to": FROM_ADDRESS or "hello@example.com",
                 "stats": {"sent": 0, "delivered": 0, "opens": 0, "clicks": 0, "bounces": 0, "unsubscribes": 0}, "updated_at": _iso(_now())}
            st["campaigns"].insert(0, c)
            self._save(st)
            return self._out(st, c, full=True)

    def patch(self, cid, fields):
        with _lock:
            st = self._load()
            c = self._find(st, cid)
            if c["status"] != "draft":
                raise AdapterError("only drafts can be changed", 409)
            for k, v in fields.items():
                if k in EDITABLE:
                    c[k] = v
            if "segment_id" in fields and fields["segment_id"]:
                c["list_id"] = None
            if "list_id" in fields and fields["list_id"]:
                c["segment_id"] = None
            c["updated_at"] = _iso(_now())
            self._save(st)
            return self._out(st, c, full=True)

    def preview(self, cid):
        return render_local(self.get_campaign(cid).get("html") or "")

    def server_checks(self, cid):
        return {"ok": True, "problems": []}

    def test_send(self, cid, to=None):
        self.get_campaign(cid)
        return {"ok": True, "to": to or TEST_ADDRESS}

    def schedule(self, cid, when_iso):
        with _lock:
            st = self._load()
            c = self._find(st, cid)
            if c["status"] != "draft":
                raise AdapterError("only drafts can be scheduled", 409)
            bad = [p for p in run_checks(self._out(st, c, full=True), links=False) if p["level"] == "error"]
            if bad:
                raise AdapterError("checks failed: " + "; ".join(p["message"] for p in bad), 422)
            c["status"], c["scheduled_for"] = "scheduled", _iso(_parse(when_iso))
            self._save(st)
            return self._out(st, c)

    def cancel(self, cid):
        with _lock:
            st = self._load()
            c = self._find(st, cid)
            if c["status"] != "scheduled":
                raise AdapterError("only a scheduled campaign can be cancelled", 409)
            c["status"], c["scheduled_for"] = "draft", None
            self._save(st)
            return self._out(st, c)

    def delete(self, cid):
        with _lock:
            st = self._load()
            c = self._find(st, cid)
            if c["status"] != "draft":
                raise AdapterError("only a draft can be deleted", 409)
            st["campaigns"] = [x for x in st["campaigns"] if x["id"] != c["id"]]
            self._save(st)
            return {"ok": True, "id": c["id"]}

    def progress(self, cid):
        with _lock:
            st = self._tick(self._load())
            c = self._find(st, cid)
            p = st["progress"].get(c["id"])
            if p:
                return dict(p)
            lst, seg = self._aud(st, c)
            return {"status": c["status"], "total": (seg or lst or {}).get("count", 0), "sent": c["stats"]["sent"], "failed": 0}


# ---------------------------------------------------------------- preview and checks

MERGE = re.compile(r"\{\{\s*[\w.]+\s*\}\}")
UNSUB = re.compile(r"\{\{\s*unsubscribe_url\s*\}\}")


def render_local(html):
    """The HTML as is, with merge tags highlighted (outside tags and attributes only)."""
    parts = re.split(r"(<[^>]*>)", html or "")
    out = []
    for p in parts:
        if p.startswith("<"):
            out.append(p)
        else:
            out.append(MERGE.sub(lambda m: f'<mark style="background:#fff3b0;color:#5a4500;border-radius:2px;padding:0 2px">{m.group(0)}</mark>', p))
    return "".join(out)


def copy_rules():
    """[(level "error"|"warn", compiled pattern, message)]: the copy rules for Email (team_settings, key copy_rules:
    this Mac's own, or the team's) plus this Mac's extra rules from <Studio data>/email_copy_rules.json, a list of
    {"level": "error"|"warn", "pattern": "<regex>", "message": "..."} (bad lines are skipped). The extra file is not
    used while your team sets the copy rules."""
    out, team_set = [], False
    try:
        import team_settings  # noqa: PLC0415
        out = [("error" if lv == "block" else "warn", rx, msg if msg.endswith(".") else msg + ".")
               for lv, rx, msg in team_settings.copy_rules("email")]
        team_set = team_settings.team_value("copy_rules") is not None
    except Exception:  # noqa: BLE001 - no settings: the extra file alone
        pass
    if team_set:
        return out
    try:
        for r in json.loads((_data_dir() / "email_copy_rules.json").read_text()):
            try:
                out.append((r.get("level", "warn"), re.compile(r["pattern"], re.I), r.get("message") or r["pattern"]))
            except (re.error, KeyError, TypeError, AttributeError):
                continue
    except (OSError, ValueError, TypeError):
        pass
    return out


COPY_RULES = copy_rules  # kept for callers that iterate it: call it


def visible_text(html):
    h = re.sub(r"(?is)<(style|script|head)[^>]*>.*?</\1>", " ", html or "")
    h = re.sub(r"(?s)<!--.*?-->", " ", h)
    alts = " ".join(re.findall(r'(?i)\b(?:alt|title)\s*=\s*"([^"]*)"', h))
    return htmllib.unescape(re.sub(r"<[^>]+>", " ", h) + " " + alts)


def _attr_urls(html, tag, attr):
    pat = re.compile(rf'(?is)<{tag}\b[^>]*?\b{attr}\s*=\s*(["\'])(.*?)\1')
    return [htmllib.unescape(m.group(2)).strip() for m in pat.finditer(html or "")]


def image_problems(html):
    out = []
    srcs = _attr_urls(html, "img", "src") + re.findall(r"(?i)url\(\s*['\"]?([^'\")]+)", html or "")
    for src in dict.fromkeys(srcs):
        if MERGE.search(src) or src.startswith("cid:"):
            continue
        host = (urllib.parse.urlparse(src).hostname or "").lower()
        path = urllib.parse.urlparse(src).path.lower()
        if path.endswith(".webp"):
            out.append({"level": "error", "message": f"Image is .webp (use jpg or png): {src}"})
        if host.startswith("staging.") or ".staging." in host or host in ("localhost", "127.0.0.1", "0.0.0.0") or not host:
            out.append({"level": "error", "message": f"Image is on a staging or local host: {src}"})
        elif IMAGE_HOST and host != IMAGE_HOST:
            out.append({"level": "warn", "message": f"Image is not on {IMAGE_HOST}: {src}"})
    return out


_link_cache = {}  # url -> (time, ok, detail)
LINK_TTL = 3600


def _fetch_status(url, timeout=8):
    """(status code or None, detail). HEAD first, then GET if HEAD fails or is refused."""
    detail = ""
    for method in ("HEAD", "GET"):
        req = urllib.request.Request(url, method=method, headers={"User-Agent": "Mozilla/5.0 MarketingStudio-linkcheck"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, ""
        except urllib.error.HTTPError as ex:
            if method == "GET" or ex.code not in (403, 405, 501, 400, 404, 429):
                return ex.code, ex.reason
            detail = str(ex.code)
        except Exception as ex:  # noqa: BLE001
            detail = str(getattr(ex, "reason", ex))
            if method == "GET":
                return None, detail
    return None, detail


def check_link(url, fetch=None):
    hit = _link_cache.get(url)
    if hit and time.time() - hit[0] < LINK_TTL:
        return hit[1], hit[2]
    code, detail = (fetch or _fetch_status)(url)
    ok = code is not None and 200 <= code < 400
    res = (ok, f"{code}" if code else (detail or "no answer"))
    _link_cache[url] = (time.time(), *res)
    return res


def link_urls(html):
    out = []
    for u in dict.fromkeys(_attr_urls(html, "a", "href")):
        if not u or u.startswith("#") or MERGE.search(u) or u.lower().startswith(("mailto:", "tel:", "sms:")):
            continue
        if not u.lower().startswith(("http://", "https://")):
            continue
        out.append(u)
    return out


def link_problems(html, fetch=None):
    urls = link_urls(html)
    if not urls:
        return []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        res = list(ex.map(lambda u: (u, *check_link(u, fetch)), urls))
    return [{"level": "error", "message": f"Link does not answer ({d}): {u}"} for u, ok, d in res if not ok]


def copy_problems(text, where):
    out = []
    for level, pat, msg in copy_rules():
        if pat.search(text or ""):
            out.append({"level": level, "message": f"{msg[:-1]} ({where})."})  # rules end with "."
    return out


def _no_comments(html):
    return re.sub(r"(?s)<!--.*?-->", " ", html or "")


def unsubscribe_problems(html):
    """{{unsubscribe_url}} must be the href of a real link: a tag in plain text or inside an HTML comment is not a link."""
    h = _no_comments(html)
    hrefs = _attr_urls(h, "a", "href") + [htmllib.unescape(m) for m in re.findall(r"(?is)<a\b[^>]*?\bhref\s*=\s*([^\s\"'>]+)", h)]
    if any(UNSUB.search(u) for u in hrefs):
        return []
    if UNSUB.search(html or ""):
        return [{"level": "error", "message": '{{unsubscribe_url}} is not in a link: put it in <a href="{{unsubscribe_url}}">Unsubscribe</a> '
                                              "(as plain text or inside an HTML comment it does not work)."}]
    return [{"level": "error", "message": "{{unsubscribe_url}} is missing."}]


GMAIL_WARN_KB, GMAIL_CLIP_KB = 90, 102  # Gmail clips a message whose HTML is over about 102 KB


def size_problems(html):
    """Gmail clipping: the HTML as sent, in UTF-8 bytes (the platform's tracking links add a little more)."""
    n = len((html or "").encode("utf-8"))
    kb = round(n / 1024)
    if n >= GMAIL_CLIP_KB * 1024:
        return [{"level": "error", "message": f"The email is {kb} KB: Gmail clips anything over {GMAIL_CLIP_KB} KB and hides the rest, "
                                              "including the unsubscribe link. Make it smaller."}]
    if n >= GMAIL_WARN_KB * 1024:
        return [{"level": "warn", "message": f"The email is {kb} KB: close to Gmail's {GMAIL_CLIP_KB} KB clipping limit "
                                             f"(keep it under {GMAIL_WARN_KB} KB to leave room for tracking links)."}]
    return []


# Merge tags every platform Studio talks to fills in; the EMAIL_MERGE_TAGS setting (comma separated) and the platform add more.
MERGE_TAGS = ("first_name", "last_name", "full_name", "name", "email", "company", "city", "country", "phone",
              "unsubscribe_url", "preferences_url", "manage_preferences_url", "view_in_browser_url", "web_version_url",
              "webview_url", "current_year")


def parse_merge_tags(r):
    """Tag names from a platform's merge tag list: a list of names or of {token|tag|key|name}, or a dict holding one
    under mergeTags / merge_tags / tags. Tokens lose their braces ("{{first_name}}" -> "first_name"). None = unreadable."""
    items = r if isinstance(r, list) else next((r[k] for k in ("mergeTags", "merge_tags", "tags") if isinstance(r, dict) and isinstance(r.get(k), list)), None)
    if items is None:
        return None
    out = []
    for x in items:
        t = x if isinstance(x, str) else next((x[k] for k in ("token", "tag", "key", "name") if isinstance(x, dict) and x.get(k)), "")
        t = re.sub(r"^\{+\s*|\s*\}+$", "", str(t).strip())
        if t and _TAG_NAME.fullmatch(t):
            out.append(t)
    return out


class KnownTags(set):
    """The merge tags a check accepts. listed: the platform's own list was read (then an unknown tag is an error;
    without it, only a warning, because Studio can't know every tag the platform fills in)."""
    listed = False


def known_merge_tags(a=None):
    """The built-in list, the EMAIL_MERGE_TAGS setting and the platform's own list when the adapter can give one
    (merge_tags() -> list, or None when it can't). Never raises."""
    out = KnownTags(MERGE_TAGS)
    try:
        import appconfig  # noqa: PLC0415
        out |= {t.strip().strip("{} ").lower() for t in re.split(r"[\s,;]+", appconfig.get("EMAIL_MERGE_TAGS") or "") if t.strip("{} ")}
    except Exception:  # noqa: BLE001
        pass
    fn = getattr(a, "merge_tags", None)
    if fn:
        try:
            got = fn()
        except Exception:  # noqa: BLE001 - the platform's list is a bonus
            got = None
        if got is not None:
            out |= {str(t).strip().strip("{} ").lower() for t in got if str(t).strip("{} ")}
            out.listed = True
    return out


_TAG = re.compile(r"\{\{\{?([^{}]*)\}?\}\}")  # {{tag}}, and {{{tag}}} (unescaped)
_TAG_NAME = re.compile(r"[A-Za-z_][\w.]*")
_SINGLE = re.compile(r"(?<![{$%])\{\s*([A-Za-z_][\w.]*)\s*\}(?!\})")
_MAILCHIMP = re.compile(r"\*\|\s*[A-Za-z_][\w:]*\s*\|\*")


def merge_tag_problems(c, tags=None):
    """Merge tags in the subject, preview text, HTML and plain text. Unknown or malformed {{tags}} are errors (they reach
    the inbox as typed) when the platform listed its tags, warnings when it could not; unbalanced braces, {single}
    braces, {{tag|fallback}} (no platform Studio knows has a fallback syntax: the "|..." is sent as text) and
    Mailchimp-style *|TAGS|* are warnings. tags: known_merge_tags(adapter), or any set (taken as the full list)."""
    known = known_merge_tags() if tags is None else tags if isinstance(tags, KnownTags) else KnownTags(t.lower() for t in tags)
    if tags is not None and not isinstance(tags, KnownTags):
        known.listed = True
    body = re.sub(r"(?is)<(style|script)[^>]*>.*?</\1>", " ", _no_comments(c.get("html")))
    out, seen = [], set()

    def add(level, msg):
        if msg not in seen:
            seen.add(msg)
            out.append({"level": level, "message": msg})
    for where, text in (("subject", c.get("subject")), ("preview text", c.get("preview_text")), ("email body", body),
                        ("plain text", c.get("text"))):
        text = text or ""
        if "{" not in text and "*|" not in text:
            continue
        for m in _TAG.finditer(text):
            name = m.group(1).split("|")[0].strip()  # {{ first_name | fallback }}: the name is before the bar
            if not _TAG_NAME.fullmatch(name):
                add("error", f"Merge tag {m.group(0)} is not valid ({where}).")
                continue
            if "|" in m.group(1):
                add("warn", f"{m.group(0)}: {PLATFORM} has no fallback syntax, so the part after | is sent as text. "
                            f"Use {{{{{name}}}}} and write copy that reads well when it is blank ({where}).")
            if name.lower() not in known:
                listed = known.listed
                add("error" if listed else "warn", f"Unknown merge tag {m.group(0)} ({where}): check the spelling. "
                    + ("" if listed else f"{PLATFORM} did not list its tags, so this may still be fine. ")
                    + "Known tags: " + ", ".join(sorted(known)[:12]) + (", ..." if len(known) > 12 else "")
                    + ". Add your own with the EMAIL_MERGE_TAGS setting.")
        rest = _TAG.sub(" ", text)
        if "{{" in rest or "}}" in rest:
            frag = re.search(r".{0,20}(\{\{|\}\}).{0,20}", rest, re.S).group(0).strip()
            add("warn", f"Unbalanced merge tag braces near \"{frag}\" ({where}).")
        for m in _SINGLE.finditer(rest):
            add("warn", f"{m.group(0)} has single braces: a merge tag needs two, like {{{{{m.group(1)}}}}} ({where}).")
        for m in _MAILCHIMP.finditer(text):
            add("warn", f"{m.group(0)} is a Mailchimp-style tag that {PLATFORM} will not fill in: use {{{{ }}}} tags ({where}).")
    return out


def run_checks(c, links=True, fetch=None, tags=None):
    """Problems [{level: error|warn, message}] for a campaign (with any Studio draft already merged in).
    tags: the merge tags the platform knows (known_merge_tags(adapter)); None = the built-in list plus Setup."""
    p = []
    html = c.get("html") or ""
    subj = (c.get("subject") or "").strip()
    for field, label in (("from_address", "From"), ("reply_to", "Reply-to")) if FROM_ADDRESS else ():
        if (c.get(field) or "").strip().lower() != FROM_ADDRESS:
            p.append({"level": "error", "message": f"{label} must be {FROM_ADDRESS} (is {c.get(field) or 'empty'})."})
    if not subj:
        p.append({"level": "error", "message": "Subject is empty."})
    elif len(subj) > 60:
        p.append({"level": "warn", "message": f"Subject is {len(subj)} characters; keep it to 60 or less."})
    if not html.strip():
        p.append({"level": "error", "message": "The email has no HTML."})
    else:
        p += unsubscribe_problems(html)
    if not (c.get("list_id") or c.get("segment_id")):
        p.append({"level": "error", "message": "Pick a list or segment."})
    p += size_problems(html)
    p += merge_tag_problems(c, tags)
    p += image_problems(html)
    p += copy_problems(subj, "subject")
    p += copy_problems(c.get("preview_text") or "", "preview text")
    p += copy_problems(visible_text(html), "email body")
    if links and html:
        p += link_problems(html, fetch)
    return p


# ---------------------------------------------------------------- Studio drafts (Claude may only use these)

def _drafts():
    """{version, drafts: {campaign_id: {fields, by, at, note}}} (draftstore collection "email")."""
    return draftstore.read("email", _data_dir())


def list_drafts():
    """[{campaign_id, fields, by, at, note}] Studio-side edits not yet saved to the platform."""
    with _lock:
        return [{"campaign_id": k, **v} for k, v in _drafts()["drafts"].items()]


def add_draft(change, by="you"):
    """change = {campaign_id, fields: {subject, preview_text, html, from_name, list_id, segment_id, name, text}, note?}.
    Merges into the Studio draft for that campaign. Never touches the platform."""
    cid = str(change.get("campaign_id") or "").strip()
    if not cid:
        raise ValueError("campaign_id is needed")
    fields = {k: v for k, v in (change.get("fields") or {}).items() if k in EDITABLE}
    bad = set(change.get("fields") or {}) - set(EDITABLE)
    if bad:
        raise ValueError(f"these fields can't be drafted: {', '.join(sorted(bad))} (from and reply-to are fixed)")
    if not fields:
        raise ValueError("nothing to draft")
    if fields.get("segment_id"):
        fields["list_id"] = None
    elif fields.get("list_id"):
        fields["segment_id"] = None
    with _lock, draftstore.tx("email", _data_dir(), by=by) as t:
        cur = (t.get(cid) or {"data": {"fields": {}}})["data"]
        cur = {"fields": {**cur["fields"], **fields}, "by": by, "at": _iso(_now()), "note": change.get("note") or cur.get("note")}
        t.put(cid, cur, by)
        return {"campaign_id": cid, **cur}


def remove_draft(cid):
    with _lock, draftstore.tx("email", _data_dir()) as t:
        return t.delete(str(cid))


def _merged(a, cid, fresh=False):
    c = a.get_campaign(cid, fresh=fresh)
    dr = _drafts()["drafts"].get(str(cid))
    return ({**c, **dr["fields"]} if dr else c), c, dr


def save_draft_to_platform(cid, a=None):
    a = a or adapter()
    dr = _drafts()["drafts"].get(str(cid))
    if not dr:
        return a.get_campaign(cid)
    c = a.patch(cid, dr["fields"])
    remove_draft(cid)
    return c


# ---------------------------------------------------------------- helpers for routes

def _rate(n, d):
    return round(100.0 * n / d, 1) if d else None


def _row(c, drafts):
    s = c.get("stats") or {}
    base = s.get("delivered") or s.get("sent") or 0
    return {**c, "audience": c.get("segment_name") or c.get("list_name") or "",
            "audience_kind": "segment" if c.get("segment_id") else "list" if c.get("list_id") else "",
            "sent": s.get("sent", 0), "delivered": s.get("delivered", 0), "opens": s.get("opens", 0), "clicks": s.get("clicks", 0),
            "bounces": s.get("bounces", 0), "unsubscribes": s.get("unsubscribes", 0),
            "open_rate": _rate(s.get("opens", 0), base), "click_rate": _rate(s.get("clicks", 0), base),
            "draft": str(c.get("id")) in drafts}


def _need_user(body):
    if (body or {}).get("confirm") != CONFIRM:
        raise ValueError("this action only runs from your own button in Marketing Studio")


def to_utc(local, tz="Europe/London"):
    """'2026-11-12T09:00' (wall time in tz) -> aware UTC datetime."""
    t = dt.datetime.fromisoformat(str(local).replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=ZoneInfo(tz or "Europe/London"))
    return t.astimezone(dt.timezone.utc)


def slugify(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:80]


def _source(a, s=None):
    s = s or settings()
    out = {"source": a.source, "source_label": a.label, "demo": a.demo, "connected": not a.demo, "can": dict(a.can), "needs_api": NEEDS_API}
    out["platform"] = PLATFORM
    out["send_tz"] = getattr(LONDON, "key", None) or "Europe/London"  # the shared posting zone (Setup: EMAIL_TIMEZONE)
    if s["api_url"] and s["token"] and a.source != "api":
        ok, msg = probe(_cached_adapter("api", s["api_url"], s["token"]))
        out["api_note"] = "" if ok else f"The Email API is set up, but it did not answer ({msg})."
    return out


def g_state(q):
    s = settings()
    a = adapter()
    return {**_source(a, s), "api_url": s["api_url"], "mcp_url": s["mcp_url"], "token_set": bool(s["token"]), "mcp_token_set": bool(s["mcp_token"]),
            "from_address": FROM_ADDRESS, "test_address": TEST_ADDRESS, "test_addresses": TEST_ADDRESSES, "drafts": list_drafts(), "secrets_file": str(SECRETS)}


def _fresh(q):
    return str((q or {}).get("fresh") or "") in ("1", "true", "yes")


def g_campaigns(q):
    a = adapter()
    rows = a.list_campaigns(limit=q.get("limit") or None, fresh=_fresh(q))
    drafts = {d["campaign_id"] for d in list_drafts()}
    rows = [_row(c, drafts) for c in rows]
    counts = {"all": len(rows)}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    st, needle = q.get("status") or "", (q.get("search") or "").lower().strip()
    if st and st != "all":
        rows = [r for r in rows if r["status"] == st]
    if needle:
        rows = [r for r in rows if needle in " ".join(str(r.get(k) or "") for k in ("name", "subject", "audience")).lower()]
    return {**_source(a), "campaigns": rows, "counts": counts}


def g_campaign(q):
    a = adapter()
    merged, orig, dr = _merged(a, q["id"], fresh=_fresh(q))
    return {"demo": a.demo, "source": a.source, "campaign": merged, "original": orig, "draft": dr}


def g_audiences(q):
    return adapter().audiences(fresh=_fresh(q))


def g_folders(q):
    root = campaigns_root()
    return {"root": str(root), "folders": sorted(p.name for p in root.iterdir() if p.is_dir()) if root.is_dir() else []}


def g_drafts(q):
    return {"drafts": list_drafts()}


def g_settings(q):
    s = settings()
    return {"api_url": s["api_url"], "mcp_url": s["mcp_url"], "name": s["name"], "token_set": bool(s["token"]), "mcp_token_set": bool(s["mcp_token"]),
            "same_token": bool(s["token"]) and s["token"] == s["mcp_token"], "secrets_file": str(SECRETS)}


def p_preview(body):
    """Live preview of the HTML in the editor. Email API: the platform fills merge tags when the HTML equals what is saved.
    MCP and demo: rendered here with merge tags highlighted (the MCP's compile_preview only takes block documents)."""
    a = adapter()
    html = body.get("html")
    cid = body.get("id")
    if a.source == "api" and cid:
        saved = a.get_campaign(cid)
        if html is None or html == saved.get("html"):
            return {"html": a.preview(cid), "source": "Platform preview (merge tags filled with sample data)"}
        return {"html": render_local(html), "source": f"Local preview: save to {PLATFORM} to see merge tags filled in"}
    if html is None and cid:
        html = a.get_campaign(cid).get("html")
    return {"html": render_local(html or ""), "source": "Local preview: merge tags highlighted"}


def p_checks(body):
    a = adapter()
    base, orig, _dr = _merged(a, body["id"])
    merged = {**base, **{k: v for k, v in (body.get("fields") or {}).items() if k in EDITABLE}}
    probs = run_checks(merged, links=body.get("links", True), tags=known_merge_tags(a))
    unsaved = any((merged.get(k) or "") != (orig.get(k) or "") for k in EDITABLE)
    if a.source == "api" and unsaved:
        probs.append({"level": "info", "message": f"{PLATFORM}'s own checks (audience, tracking) run when you save or send."})
    if a.source == "api" and not unsaved:
        try:
            probs += [{"level": x.get("level", "warn"), "message": "Platform: " + x.get("message", "")} for x in a.server_checks(body["id"]).get("problems", [])]
        except AdapterError as ex:
            probs.append({"level": "warn", "message": f"The platform's checks did not run: {ex}"})
    return {"ok": not any(x["level"] == "error" for x in probs), "problems": probs}


def p_save_draft(body):
    """The UI sends every field that differs from the platform, with replace=True; nothing left means no draft."""
    if body.get("replace"):
        remove_draft(body.get("id"))
        if not body.get("fields"):
            return {"campaign_id": str(body.get("id")), "fields": {}, "removed": True}
    return add_draft({"campaign_id": body.get("id"), "fields": body.get("fields") or {}, "note": body.get("note")}, by="you")


def p_discard_draft(body):
    return {"ok": remove_draft(body["id"])}


def p_save_to_platform(body):
    a = adapter()
    if not a.can["edit"]:
        raise ValueError(NEEDS_API)
    return {"ok": True, "campaign": save_draft_to_platform(body["id"], a), "demo": a.demo}


def p_new_campaign(body):
    """Create a new DRAFT on the platform (MCP: email_create_draft_campaign). The user's button only; never an MCP tool here."""
    _need_user(body)
    name = (body.get("name") or "").strip()
    if not name:
        raise ValueError("give the campaign a name")
    a = adapter()
    if not a.can["create"]:
        raise ValueError(NEEDS_API)
    fields = {"name": name}
    for k in ("subject", "list_id", "segment_id", "html", "from_name", "text"):
        if body.get(k):
            fields[k] = body[k]
    if not a.demo:
        bad = _blocking(run_checks({**fields, "from_address": FROM_ADDRESS or "", "reply_to": FROM_ADDRESS or "", "html": fields.get("html") or STARTER_HTML,
                                    "list_id": fields.get("list_id") or "-"}, links=False))
        if bad:
            raise ValueError("Fix these first: " + "; ".join(p["message"] for p in bad[:4]))
    return {"campaign": a.create(fields), "source": a.source}


def p_save_html(body):
    root = campaigns_root().resolve()
    folder = slugify(body.get("folder"))
    slug = slugify(body.get("slug") or body.get("name") or "email")
    if not folder or not slug:
        raise ValueError("pick or type a folder and a file name")
    html = body.get("html") or ""
    if not html.strip():
        raise ValueError("there is no HTML to save")
    path = (root / folder / f"{slug}.html").resolve()
    if root not in path.parents:
        raise ValueError("that folder is outside the campaigns folder")
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    path.write_text(html)
    return {"ok": True, "path": str(path), "replaced": existed}


TOKEN_RE = re.compile(r"\S{8,}")


def _url_ok(u):
    p = urllib.parse.urlparse(u)
    return p.scheme in ("https", "http") and bool(p.hostname)


def p_connect(body):
    """Save the connection: Email API URL + token and/or MCP URL + token, and the platform's display name.
    same_token=True stores one token for both. A blank token keeps the saved one; disconnect removes both tokens."""
    upd = {}
    for k, key in (("api_url", "EMAIL_API_URL"), ("mcp_url", "EMAIL_MCP_URL")):
        u = (body.get(k) or "").strip().rstrip("/")
        if k in body:
            if u and not _url_ok(u):
                raise ValueError(f"the {'API' if k == 'api_url' else 'MCP'} URL must look like https://...")
            upd[key] = u or None
    if (body.get("name") or "").strip():
        upd["EMAIL_PLATFORM_NAME"] = body["name"].strip()[:60]
    tok, mtok = (body.get("token") or "").strip(), (body.get("mcp_token") or "").strip()
    if body.get("same_token"):
        one = tok or mtok
        if one:
            tok = mtok = one
    for value, key, what in ((tok, "EMAIL_API_TOKEN", "API"), (mtok, "EMAIL_MCP_TOKEN", "MCP")):
        if value:
            if not TOKEN_RE.fullmatch(value):
                raise ValueError(f"the {what} token looks wrong (no spaces, at least 8 characters)")
            upd[key] = value
    if body.get("disconnect"):
        upd["EMAIL_API_TOKEN"] = upd["EMAIL_MCP_TOKEN"] = None
    if upd:
        _write_env(upd)
        _probes.clear()
        if upd.get("EMAIL_PLATFORM_NAME"):
            global PLATFORM
            PLATFORM = upd["EMAIL_PLATFORM_NAME"]
    return {**g_settings({}), "ok": True}


def p_test_connection(body):
    """Test both connections with what is saved. Messages never contain a token."""
    s = settings()
    out = {"api": None, "mcp": None}
    if s["api_url"] and s["token"]:
        ok, msg = probe(_cached_adapter("api", s["api_url"], s["token"]), fresh=True)
        out["api"] = {"ok": ok, "message": "Email API answered." if ok else f"Email API: {msg}"}
    if s["mcp_url"] and s["mcp_token"]:
        ok, msg = probe(_cached_adapter("mcp", s["mcp_url"], s["mcp_token"]), fresh=True)
        out["mcp"] = {"ok": ok, "message": "MCP answered (email overview)." if ok else f"MCP: {msg}"}
    a = adapter()
    parts = [x["message"] for x in (out["api"], out["mcp"]) if x] or ["Nothing connected, so Studio shows demo data."]
    return {**out, "ok": any(x and x["ok"] for x in (out["api"], out["mcp"])), "active": a.source, "active_label": a.label,
            "message": " ".join(parts) + f" Using: {a.label}"}


# ---------------------------------------------------------------- the user's buttons (never an MCP tool)

def _blocking(problems):
    return [p for p in problems if p["level"] == "error"]


def _need(a, cap):
    if not a.can.get(cap):
        raise ValueError(NEEDS_API)


def _prepare(a, cid, kind, extra_lines):
    """Checking and Saving steps shared by every action. Returns the campaign as saved on the platform."""
    import progress
    merged, orig, dr = _merged(a, cid, fresh=True)
    progress.start(KEY, kind, [("check", "Checking", 1), ("save", f"Saving to {PLATFORM}", 1), *extra_lines], "checking", "Checking the email")
    probs = run_checks(merged, tags=known_merge_tags(a))
    if a.source == "api" and not dr:
        try:
            probs += [{"level": x.get("level", "warn"), "message": "Platform: " + x.get("message", "")} for x in a.server_checks(cid).get("problems", [])]
        except AdapterError:
            pass
    if kind == "test":  # a test goes to the test inbox only: the audience does not matter for it
        for x in probs:
            if x["level"] == "error" and x["message"].startswith("Platform:") and any(w in x["message"].lower() for w in ("audience", "subscriber", "recipient")):
                x["level"], x["message"] = "warn", x["message"] + " (fine for a test)"
    bad = _blocking(probs)
    if bad:
        progress.mark(KEY, "check", "failed")
        raise_msg = "Stopped by the checks: " + "; ".join(p["message"] for p in bad[:5])
        progress.finish(KEY, False, raise_msg)
        return None, raise_msg, probs
    progress.sent(KEY, "check")
    progress.phase(KEY, "saving", f"Saving the draft to {PLATFORM}" if dr else "Nothing new to save")
    if dr:
        if orig["status"] != "draft":
            progress.mark(KEY, "save", "failed")
            msg = "Only drafts can be changed. Cancel the schedule first, or discard the Studio draft."
            progress.finish(KEY, False, msg)
            return None, msg, probs
        orig = save_draft_to_platform(cid, a)
    progress.sent(KEY, "save")
    return orig, None, probs


def _guard(fn):
    def run(body):
        import progress
        _need_user(body)
        try:
            return fn(body)
        except Exception as ex:  # noqa: BLE001
            progress.finish(KEY, False, str(ex))
            return {"ok": False, "message": str(ex)}
    return run


@_guard
def p_test_send(body):
    import progress
    a = adapter()
    _need(a, "test")
    want = (body.get("to") or "").strip().lower()
    if want and want not in TEST_ADDRESSES:
        raise ValueError("Pick one of the test addresses from Setup.")
    if not want and len(TEST_ADDRESSES) > 1:
        raise ValueError("Pick which test address to send to.")
    want = want or (TEST_ADDRESSES[0] if TEST_ADDRESSES else "")
    shown = want or "the platform's test inbox"
    c, err, probs = _prepare(a, body["id"], "test", [("test", f"Test to {shown}", 1)])
    if err:
        return {"ok": False, "message": err, "problems": probs}
    progress.phase(KEY, "sending", f"Sending a test to {shown}")
    r = a.test_send(body["id"], want or None)
    progress.sent(KEY, "test")
    to = r.get("to") or shown
    progress.finish(KEY, True, f"Test sent to {to}" + (" (demo: nothing was sent)" if a.demo else ""))
    return {"ok": True, "message": f"Test sent to {to}." + (" Demo data: nothing was really sent." if a.demo else ""), "problems": probs}


def _calendar(fn, *args):
    """shared_calendar.<fn>(*args), or None: the shared calendar must never stop a schedule or a cancel."""
    try:
        import shared_calendar  # noqa: PLC0415
        return getattr(shared_calendar, fn)(*args)
    except Exception:  # noqa: BLE001
        return None


def _schedule(a, body, when, now_send):
    import progress
    _need(a, "schedule")
    cid = body["id"]
    lines = [("schedule", "Scheduled", 1)]
    if now_send:
        lines.append(("send", "Sending", 1))
    c, err, probs = _prepare(a, cid, "send" if now_send else "schedule", lines)
    if err:
        return {"ok": False, "message": err, "problems": probs}
    if c["status"] != "draft":
        progress.finish(KEY, False, f"It is {c['status']}, not a draft.")
        return {"ok": False, "message": f"Only a draft can be scheduled (this one is {c['status']})."}
    clash = _calendar("email_clashes", c, when) or []  # warnings only (shared calendar); never stops the schedule
    if body.get("_team_expect"):  # [team reviews] last check, as the platform has it now, just before scheduling
        import team_reviews  # noqa: PLC0415
        why = team_reviews.recheck(body, "email", str(cid))
        if why:
            progress.finish(KEY, False, why)
            return {"ok": False, "needs_team_approval": True, "message": why}
    progress.phase(KEY, "scheduling", "Scheduling")
    merged, c = c, a.schedule(cid, _iso(when))
    progress.sent(KEY, "schedule")
    _calendar("mark_email", {**merged, "id": cid}, when)
    london = when.astimezone(LONDON).strftime("%d %B %Y %H:%M").lstrip("0")
    if not now_send:
        progress.finish(KEY, True, f"Scheduled for {london} (London)" + (" (demo)" if a.demo else ""))
        return {"ok": True, "message": f"Scheduled for {london}, London time." + (" Demo data: nothing will really go out." if a.demo else ""), "campaign": c,
                "demo": bool(a.demo), "clashes": clash}  # [team live] demo: nothing really went out
    deadline, last = time.time() + 20 * 60, None
    while time.time() < deadline:
        p = a.progress(cid)
        total, sent = int(p.get("total") or 0), int(p.get("sent") or 0)
        progress.totals(KEY, {"send": max(total, 1)})
        if last is None or sent != last:
            if sent > (last or 0):
                progress.sent(KEY, "send", sent - (last or 0))
            last = sent
        msg = f"{sent:,} of {total:,} sent" if p.get("status") in ("sending", "sent") else "Waiting for the platform to send"
        progress.phase(KEY, "sending", msg + (f", {p.get('failed')} failed" if p.get("failed") else ""))
        if p.get("status") in ("sent", "failed", "cancelled"):
            ok = p["status"] == "sent"
            progress.finish(KEY, ok, f"{sent:,} of {total:,} sent" + ("" if ok else f" ({p['status']})") + (" (demo)" if a.demo else ""))
            return {"ok": ok, "message": f"{sent:,} of {total:,} sent." + (" Demo data: nothing was really sent." if a.demo else ""),
                    "demo": bool(a.demo), "clashes": clash}
        time.sleep(0.5 if a.demo else 3)
    progress.finish(KEY, True, "Still sending: see the campaigns grid for the latest count")
    return {"ok": True, "message": "Still sending. The grid shows the latest count.", "demo": bool(a.demo)}


TEAM_CONTENT = EDITABLE + ("from_address", "reply_to", "blocks")


def team_content(cid, campaign=None):
    """[team reviews] What a Send would send: the platform campaign (fetched fresh) with any Studio draft on top.
    An approval covers exactly this, so edits on the platform or a draft saved there outside Send are caught."""
    if campaign is None:
        campaign, _, _ = _merged(adapter(), cid, fresh=True)
    return {"campaign_id": str(cid), **{k: campaign.get(k) for k in TEAM_CONTENT}}


def _team(body, what, fn):
    """[team reviews] In a team: an approval when the team requires one for Email, a why note, a feed event."""
    import team_reviews  # noqa: PLC0415
    return team_reviews.single_action("email", "email", str(body["id"]), body, what, fn)


@_guard
def p_schedule(body):
    when = to_utc(body.get("when"), body.get("tz") or getattr(LONDON, "key", None) or "Europe/London")
    if when < _now() + dt.timedelta(minutes=2):
        raise ValueError("pick a time at least a few minutes from now (or use Send now)")
    return _team(body, "Scheduled an email", lambda: _schedule(adapter(), body, when, False))


@_guard
def p_send_now(body):
    return _team(body, "Sent an email", lambda: _schedule(adapter(), body, _now(), True))


def p_delete(body):
    """Delete a DRAFT campaign on the platform (the user's right-click > Delete draft). Never an MCP tool.
    The Studio draft of it goes too. Anything that is not a draft is refused here and by the platform."""
    _need_user(body)
    a = adapter()
    if not a.can.get("delete"):
        raise ValueError(NEEDS_API)
    cid = str(body["id"])
    c = a.get_campaign(cid, fresh=True)
    if c.get("status") != "draft":
        raise ValueError(f"Only drafts can be deleted (this campaign is {c.get('status')}).")
    try:
        a.delete(cid)
    except AdapterError as ex:
        raise ValueError(str(ex)) from None
    remove_draft(cid)
    if hasattr(a, "forget"):
        a.forget()
    return {"ok": True, "id": cid, "name": c.get("name"), "demo": a.demo}


@_guard
def p_cancel(body):
    import progress
    a = adapter()
    _need(a, "schedule")
    progress.start(KEY, "cancel", [("cancel", "Cancel the schedule", 1)], "sending", "Cancelling the schedule")
    c = a.cancel(body["id"])
    _calendar("unmark_email", body["id"])
    progress.sent(KEY, "cancel")
    progress.finish(KEY, True, "Back to draft: it will not go out")
    return {"ok": True, "message": "Cancelled. The campaign is a draft again.", "campaign": c}


def g_progress(q):
    """The campaign's own send progress, from the platform (fake: simulated)."""
    return adapter().progress(q["id"])


def refresh(body=None):
    """The Refresh menu's Email item and the Email page's Refresh button: probe the source again and re-read the campaign
    list, audiences and overview numbers. Reads only: nothing is saved, sent or scheduled."""
    t0 = time.time()
    _probes.clear()
    a = adapter()
    rows = a.list_campaigns(fresh=True)
    a.audiences(fresh=True)
    fn = getattr(a, "overview_numbers", None)
    if fn:
        try:
            fn(fresh=True)
        except AdapterError:
            pass
    dp = _policy()
    if dp:
        dp.invalidate("email", at=t0)
        dp.mark("email")
    return {"via": a.source, "demo": a.demo, "campaigns": len(rows)}


def g_overview_numbers(q):
    a = adapter()
    fn = getattr(a, "overview_numbers", None)
    return {"source": a.source, "numbers": fn(fresh=_fresh(q)) if fn else None}


GET = {"state": g_state, "campaigns": g_campaigns, "campaign": g_campaign, "audiences": g_audiences, "folders": g_folders,
       "drafts": g_drafts, "settings": g_settings, "send_progress": g_progress,
       "numbers": g_overview_numbers}
POST = {"refresh": refresh, "preview": p_preview, "checks": p_checks, "save_draft": p_save_draft, "discard_draft": p_discard_draft,
        "save_to_platform": p_save_to_platform, "delete": p_delete, "new_campaign": p_new_campaign, "save_html": p_save_html, "connect": p_connect,
        "test_connection": p_test_connection, "test_send": p_test_send, "schedule": p_schedule, "send_now": p_send_now, "cancel": p_cancel}


# ---------------------------------------------------------------- Connections health + Overview

def health(today):
    from health import _row as hrow
    s = settings()
    api, mcp = bool(s["api_url"] and s["token"]), bool(s["mcp_url"] and s["mcp_token"])
    if not api and not mcp:
        return [hrow("email", PLATFORM, "Connection", None, "Not connected: the Email area shows demo data.",
                     "Connect your email platform in Setup > Email or Email > Connect (Email API and/or MCP). See the README.")]
    rows = []
    if api:
        ok, msg = probe(EmailApiAdapter(s["api_url"], s["token"], timeout=10), fresh=True)
        rows.append(hrow("email", PLATFORM, "Email API", True if ok else None if mcp else False,
                         "The Email API answered (campaigns, limit 1)." if ok else f"Not answering: {msg}" + (" Studio uses the MCP instead." if mcp else ""),
                         "" if ok else "Check the API URL and token in Email > Connect."))
    if mcp:
        ok, msg = probe(McpAdapter(s["mcp_url"], s["mcp_token"], timeout=10), fresh=True)
        rows.append(hrow("email", PLATFORM, "MCP (read)", ok, "The MCP answered (email overview)." if ok else msg,
                         "" if ok else "Check the MCP URL and token in Email > Connect, and that the token has access to email."))
    return rows


def overview(days):
    a = adapter()
    since = _now() - dt.timedelta(days=int(days or 7))
    rows = [c for c in a.list_campaigns(status="sent") if _parse(c.get("sent_at")) and _parse(c["sent_at"]) >= since]
    st = lambda c, k: (c.get("stats") or {}).get(k) or 0
    sent = sum(st(c, "sent") for c in rows)
    deliv = sum(st(c, "delivered") or st(c, "sent") for c in rows)
    opens = sum(st(c, "opens") for c in rows)
    clicks = sum(st(c, "clicks") for c in rows)
    pct = lambda n: f"{_rate(n, deliv)}%" if deliv else "-"
    note = f"Campaigns sent in the last {int(days)} days. Rates are of delivered emails."
    if a.demo:
        note = "Demo data: no email platform connected. " + note
    elif a.source == "mcp":
        note = "Live data through MCP (read-only). " + note
    return {"title": "Email", "tiles": [{"k": "Campaigns sent", "v": str(len(rows)), "hi": 0}, {"k": "Emails sent", "v": f"{sent:,}", "hi": 0},
                                        {"k": "Open rate", "v": pct(opens), "hi": 1}, {"k": "Click rate", "v": pct(clicks), "hi": 0}], "note": note}
