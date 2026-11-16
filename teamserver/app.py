"""HTTP layer of the team server: routing, auth, limits, JSON errors and the SSE stream.

TeamServer wires a Store to a ThreadingHTTPServer and a Broker (fan-out of live updates to open streams).
"""
import json
import os
import queue
import re
import socket
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

from . import PROTOCOL, __version__
from . import reviews
from .store import ApiError, MANAGERS, Store, iso
from . import teamsettings
from . import notify

MAX_BODY_BYTES = 4 * 1024 * 1024
JOIN_LIMIT = (10, 60)        # attempts per IP per window (seconds)
REQUEST_LIMIT = (1200, 60)   # requests per device per window
MAX_CONNECTIONS = 200        # concurrent connections (one thread each)
MAX_STREAMS_PER_DEVICE = 5
REQUEST_TIMEOUT = 15.0       # seconds a socket may sit idle or trickle a request
DEVICE_CHECK_INTERVAL = 2.0  # how often an open stream rechecks its token
REQUEST_DEADLINE = 20.0      # total seconds for request line + headers + body
MAX_CONNECTIONS_PER_IP = 20


class RateLimiter:
    """Fixed-window counter per key; good enough to stop runaway clients and invite guessing."""

    def __init__(self, limit, window, clock):
        self.limit, self.window, self.clock = limit, window, clock
        self.lock = threading.Lock()
        self.hits = {}

    def hit(self, key):
        now = self.clock()
        with self.lock:
            start, n = self.hits.get(key, (now, 0))
            if now - start >= self.window:
                start, n = now, 0
            n += 1
            self.hits[key] = (start, n)
            if len(self.hits) > 10000:
                self.hits = {k: v for k, v in self.hits.items() if now - v[0] < self.window}
        if n > self.limit:
            raise ApiError(429, "too many requests, slow down")


class IpSlots:
    """Counts open connections (or requests) per client IP and refuses more than `limit`."""

    def __init__(self, limit):
        self.limit = limit
        self.lock = threading.Lock()
        self.open = {}

    def acquire(self, ip):
        with self.lock:
            n = self.open.get(ip, 0)
            if n >= self.limit:
                return False
            self.open[ip] = n + 1
            return True

    def release(self, ip):
        with self.lock:
            n = self.open.get(ip, 0) - 1
            if n > 0:
                self.open[ip] = n
            else:
                self.open.pop(ip, None)


class Deadlines:
    """Watchdog for slow clients: a socket still sending its request after its deadline is shut down.

    The per-read socket timeout alone lets a client trickle one byte every few seconds forever; this caps the
    whole request (line, headers and body). Streams disarm it once their headers are read.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.armed = {}  # id(handler) -> (deadline, socket)

    def arm(self, handler, seconds):
        with self.lock:
            self.armed[id(handler)] = (time.monotonic() + seconds, handler.connection)

    def disarm(self, handler):
        with self.lock:
            self.armed.pop(id(handler), None)

    def sweep(self):
        now = time.monotonic()
        with self.lock:
            late = [k for k, (d, _) in self.armed.items() if d <= now]
            socks = [self.armed.pop(k)[1] for k in late]
        for sock in socks:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        return len(socks)


class Broker:
    """Keeps one queue per open SSE stream and routes live updates to them."""

    def __init__(self):
        self.lock = threading.Lock()
        self.subs = {}  # sid -> (member_id, device_id, queue)
        self.next_id = 0

    def subscribe(self, member_id, device_id, max_per_device=MAX_STREAMS_PER_DEVICE):
        q = queue.Queue(maxsize=1000)
        with self.lock:
            if sum(1 for _, did, _ in self.subs.values() if did == device_id) >= max_per_device:
                raise ApiError(429, "too many open streams for this device (max %d)" % max_per_device)
            self.next_id += 1
            sid = self.next_id
            self.subs[sid] = (member_id, device_id, q)
        return sid, q

    def unsubscribe(self, sid):
        with self.lock:
            self.subs.pop(sid, None)

    def _put(self, q, item):
        try:
            q.put_nowait(item)
        except queue.Full:
            pass  # a stuck client misses updates; it re-syncs from cursors on reconnect

    def publish(self, kind, payload, to=None):
        """Store hook. Kinds starting with '_' are control messages (close streams), not sent to clients."""
        with self.lock:
            subs = list(self.subs.values())
        if kind == "_close_device":
            for _, did, q in subs:
                if did == payload["device"]:
                    self._put(q, None)
            return
        if kind == "_close_member":
            for mid, _, q in subs:
                if mid == payload["member"]:
                    self._put(q, None)
            return
        for mid, _, q in subs:
            if to is None or mid in to:
                self._put(q, (kind, payload))

    def close_all(self):
        with self.lock:
            subs = list(self.subs.values())
        for _, _, q in subs:
            self._put(q, None)


JOIN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Join the team</title>
<style>body{font:16px/1.5 -apple-system,system-ui,sans-serif;max-width:32rem;margin:4rem auto;padding:0 1rem;color:#111827}
a.btn{display:inline-block;background:#111827;color:#fff;padding:.6rem 1rem;border-radius:.5rem;text-decoration:none}
code{word-break:break-all}</style></head>
<body><h1>Join %(team)s in Marketing Studio</h1>
<p>Open this invite in the Marketing Studio app on your Mac.</p>
<p><a class="btn" id="open" href="#">Open in Marketing Studio</a></p>
<p>If nothing happens, open Settings &gt; Team in the app and paste this invite code:<br><code id="code"></code></p>
<script>
var t = location.hash.slice(1);
document.getElementById('code').textContent = t || '(missing)';
document.getElementById('open').href = 'marketingstudio://join?server=' + encodeURIComponent(%(server)s) +
  '&invite=' + encodeURIComponent(t);
</script></body></html>
"""


def invite_links(public_url, token):
    """The two links an invite is shared as: a web page and the app's URL scheme."""
    base = public_url.rstrip("/")
    return {"web": "%s/join#%s" % (base, token),
            "app": "marketingstudio://join?server=%s&invite=%s" % (quote(base, safe=""), token)}


class Handler(BaseHTTPRequestHandler):
    """One request. `self.server.team` is the TeamServer."""

    protocol_version = "HTTP/1.1"
    server_version = "MarketingStudioTeam/" + __version__
    sys_version = ""

    def setup(self):
        self.timeout = self.server.team.request_timeout  # socket timeout: stops slow or idle clients
        super().setup()

    def handle_one_request(self):
        """Each request (also the next one on a kept-alive connection) must arrive whole within the deadline."""
        self.team.deadlines.arm(self, self.team.request_deadline)
        self._ip_slot = None
        try:
            super().handle_one_request()
        except (socket.timeout, OSError):
            self.close_connection = True
        finally:
            self.team.deadlines.disarm(self)
            if self._ip_slot is not None:
                self.team.request_ips.release(self._ip_slot)
                self._ip_slot = None

    # ---------- helpers ----------

    @property
    def team(self):
        return self.server.team

    def log_message(self, fmt, *args):
        if self.team.verbose:
            sys.stderr.write("%s %s\n" % (self.address_string(), _redact(fmt % args)))

    def log_request(self, code="-", size="-"):
        if self.team.verbose:
            # self.path is missing when the request line could not be parsed; tokens are always redacted
            path = getattr(self, "path", None) or getattr(self, "requestline", "") or "-"
            sys.stderr.write("%s %s %s %s\n" % (self.address_string(), getattr(self, "command", None) or "-",
                                                _redact(path), code))

    def send_json(self, status, obj):
        body = json.dumps(obj, separators=(",", ":"), allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def read_json(self):
        length = self.headers.get("Content-Length")
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            self.close_connection = True
            raise ApiError(400, "chunked bodies are not supported; send Content-Length")
        if not length:
            return {}
        try:
            n = int(length)
        except ValueError:
            self.close_connection = True
            raise ApiError(400, "bad Content-Length")
        if n < 0 or n > MAX_BODY_BYTES:
            self.close_connection = True
            raise ApiError(413, "request body too large (max %d bytes)" % MAX_BODY_BYTES)
        try:
            raw = self.rfile.read(n) if n else b""
        except (socket.timeout, OSError):
            raw = None
        if raw is None or len(raw) < n:
            self.close_connection = True
            raise ApiError(408, "request body incomplete or too slow")
        self.team.deadlines.disarm(self)  # the whole request is in; processing is not timed
        if not raw:
            return {}
        try:
            obj = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant, parse_float=_finite_float,
                              parse_int=_short_int)
        except _NumberTooLong:
            raise ApiError(400, "numbers may have at most %d digits" % MAX_INT_DIGITS)
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "body must be JSON (no NaN or Infinity)")
        except RecursionError:
            raise ApiError(400, "body is nested too deeply")
        if not isinstance(obj, dict):
            raise ApiError(400, "body must be a JSON object")
        try:  # lone surrogates ("\ud800") parse fine but cannot be stored or sent back
            json.dumps(obj, ensure_ascii=False).encode("utf-8")
        except UnicodeEncodeError:
            raise ApiError(400, "body contains invalid unicode")
        return obj

    def client_ip(self):
        """The socket peer, or the last X-Forwarded-For hop when the server runs behind a trusted proxy."""
        if self.team.trust_proxy:
            xff = self.headers.get("X-Forwarded-For", "")
            hops = [h.strip() for h in xff.split(",") if h.strip()]
            if hops:
                return hops[-1][:64]
        return self.client_address[0]

    def bearer(self, query):
        h = self.headers.get("Authorization", "")
        if h.lower().startswith("bearer "):
            return h[7:].strip()
        return None

    def authed(self, query, allow_query_token=False):
        token = self.bearer(query)
        if token is None and allow_query_token:
            token = (query.get("token") or [None])[0]
        ctx = self.team.store.auth(token)
        self.team.req_limit.hit(ctx["device_id"])
        return ctx

    # ---------- dispatch ----------

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_PATCH(self):
        self.dispatch("PATCH")

    def do_PUT(self):
        self.dispatch("PUT")

    def do_DELETE(self):
        self.dispatch("DELETE")

    def dispatch(self, method):
        try:
            if self.team.trust_proxy:  # behind a proxy every socket is the proxy; cap by forwarded IP instead
                ip = self.client_ip()
                if not self.team.request_ips.acquire(ip):
                    self.close_connection = True
                    raise ApiError(429, "too many open connections from your address")
                self._ip_slot = ip
            if method not in ("POST", "PUT", "PATCH", "DELETE") and (
                    self.headers.get("Content-Length", "0") not in ("", "0") or self.headers.get("Transfer-Encoding")):
                self.close_connection = True  # a body we will not read: never reuse the connection
            u = urlparse(self.path)
            query = parse_qs(u.query)
            path = u.path.rstrip("/") or "/"
            for m, pattern, fn in ROUTES:
                match = pattern.fullmatch(path)
                if match and m == method:
                    if method in ("POST", "PUT", "PATCH", "DELETE"):
                        body = self.read_json()
                    else:
                        body = {}
                        self.team.deadlines.disarm(self)
                    result = fn(self, query, body, *match.groups())
                    if result is not None:
                        self.send_json(200, result)
                    return
            if any(p.fullmatch(path) for _, p, _ in ROUTES):
                raise ApiError(405, "method not allowed")
            raise ApiError(404, "not found")
        except ApiError as e:
            out = {"error": e.message}
            out.update(e.extra)
            self.safe_send(e.status, out)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception:
            traceback.print_exc(file=sys.stderr)
            self.safe_send(500, {"error": "internal error"})

    def safe_send(self, status, obj):
        try:
            self.send_json(status, obj)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    # ---------- routes ----------

    def r_health(self, query, body):
        store = self.team.store
        return {"ok": True, "protocol": PROTOCOL, "version": __version__, "team": store.team_name()}

    def r_join_page(self, query, body):
        page = JOIN_PAGE % {"team": _html(self.team.store.team_name()), "server": _js_string(self.team.url_for(self))}
        data = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def r_join(self, query, body):
        self.team.join_limit.hit(self.client_ip())
        token, member = self.team.store.join(body.get("invite"), body.get("device_name"))
        return {"device_token": token, "member": member, "team": self.team.store.team_name()}

    def r_me(self, query, body):
        ctx = self.authed(query)
        return {"member": ctx["member"], "team": self.team.store.team_name(),
                "device": {"id": ctx["device_id"], "name": ctx["device_name"]}}

    def r_members(self, query, body):
        ctx = self.authed(query)
        is_manager = ctx["member"]["role"] in MANAGERS
        out = {"members": self.team.store.list_members(with_devices=is_manager)}
        if is_manager:
            out["invites"] = self.team.store.pending_invites()
        return out

    def r_member_patch(self, query, body, mid):
        ctx = self.authed(query)
        return self.team.store.update_member(ctx["member"], int(mid), body)

    def r_invite(self, query, body):
        ctx = self.authed(query)
        inv = self.team.store.create_invite(body.get("email"), body.get("role"), body.get("name"), by=ctx["member"])
        out = invite_links(self.team.url_for(self), inv["token"])
        out.update(id=inv["id"], email=inv["email"], role=inv["role"], device=inv["member_id"] is not None,
                   expires_at=iso(inv["expires_at"]))
        return out

    def r_invite_delete(self, query, body, iid):
        ctx = self.authed(query)
        return self.team.store.delete_invite(ctx["member"], int(iid))

    def r_device_delete(self, query, body, did):
        ctx = self.authed(query)
        return self.team.store.revoke_device(ctx["member"], int(did))

    def r_items_get(self, query, body):
        self.authed(query)
        since = _qint(query, "since", 0)
        limit = min(max(_qint(query, "limit", 1000), 1), 5000)
        return self.team.store.items_since(since, limit)

    def r_items_post(self, query, body):
        ctx = self.authed(query)
        return self.team.store.write_items(ctx["member"], body.get("ops"))

    def r_events_get(self, query, body):
        self.authed(query)
        since = _qint(query, "since", 0)
        limit = min(max(_qint(query, "limit", 200), 1), 1000)
        return self.team.store.events_since(since, limit)

    def r_events_post(self, query, body):
        ctx = self.authed(query)
        return self.team.store.post_event(ctx["member"], body)

    def r_comments_get(self, query, body):
        self.authed(query)
        return {"comments": self.team.store.comments_for(_qstr(query, "coll"), _qstr(query, "id"))}

    def r_comment_counts(self, query, body):
        self.authed(query)
        return self.team.store.comment_counts(_qstr(query, "coll"))

    def r_comments_post(self, query, body):
        ctx = self.authed(query)
        return self.team.store.add_comment(ctx["member"], body)

    def r_comment_patch(self, query, body, cid):
        ctx = self.authed(query)
        return self.team.store.patch_comment(ctx["member"], int(cid), body)

    def r_mentions(self, query, body):
        ctx = self.authed(query)
        unread = _qstr(query, "unread") in ("1", "true", "yes")
        return {"mentions": self.team.store.mentions_for(ctx["member"]["id"], unread_only=unread)}

    def r_mentions_read(self, query, body):
        ctx = self.authed(query)
        out = self.team.store.mark_read(ctx["member"]["id"], body.get("ids"))
        self.team.notifier.mentions_read(ctx["member"]["id"], body.get("ids"))  # their notifications too
        return out

    def r_presence_post(self, query, body):
        ctx = self.authed(query)
        return self.team.store.presence_beat(ctx["member"], body)

    def r_presence_get(self, query, body):
        self.authed(query)
        return {"presence": self.team.store.presence_list()}

    def r_claims_get(self, query, body):
        self.authed(query)
        return {"claims": self.team.store.claims()}

    def r_claims_post(self, query, body):
        ctx = self.authed(query)
        return self.team.store.claim(ctx["member"], body)

    def r_claims_delete(self, query, body):
        ctx = self.authed(query)
        if not body:  # DELETE bodies are sometimes dropped by proxies; accept the query too
            body = {"coll": _qstr(query, "coll"), "id": _qstr(query, "id")}
        return self.team.store.release(ctx["member"], body)

    def r_stream(self, query, body):
        ctx = self.authed(query, allow_query_token=True)
        broker = self.team.broker
        sid, q = broker.subscribe(ctx["member"]["id"], ctx["device_id"], self.team.max_streams_per_device)
        self.close_connection = True
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b"retry: 5000\n: connected\n\n")
            self.wfile.flush()
            last_check = time.monotonic()
            next_ping = last_check + self.team.ping_interval
            while not self.team.stopping.is_set():
                now = time.monotonic()
                if now - last_check >= self.team.device_check_interval:
                    # Revoked or deactivated elsewhere (for example the CLI): checked whether or not events flow.
                    if not self.team.store.device_ok(ctx["device_id"]):
                        break
                    last_check = now
                wait = min(next_ping, last_check + self.team.device_check_interval) - now
                try:
                    item = q.get(timeout=max(wait, 0.01))
                except queue.Empty:
                    if time.monotonic() >= next_ping:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        next_ping = time.monotonic() + self.team.ping_interval
                    continue
                if item is None:
                    break
                kind, payload = item
                self.wfile.write(("event: %s\ndata: %s\n\n" % (kind, json.dumps(payload, separators=(",", ":"),
                                                                                  allow_nan=False)))
                                 .encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            broker.unsubscribe(sid)
        return None


MAX_INT_DIGITS = 20


class _NumberTooLong(ValueError):
    pass


def _short_int(text):
    """int() of a JSON integer, refusing long digit runs: Python 3.9 converts them in quadratic time."""
    if len(text.lstrip("-")) > MAX_INT_DIGITS:
        raise _NumberTooLong(text[:10])
    return int(text)


def _reject_constant(name):
    raise ValueError("%s is not allowed" % name)


def _finite_float(text):
    f = float(text)
    if f != f or f in (float("inf"), float("-inf")):
        raise ValueError("number out of range")
    return f


def _js_string(s):
    """A JSON string literal that is safe inside a <script> element."""
    return (json.dumps(s).replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def _redact(text):
    """Hide ?token= values (and invite codes after #) from log lines."""
    return re.sub(r"(token=)[^&\s\"]*", r"\1[redacted]", text)


def _html(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


def _qstr(query, key):
    v = query.get(key)
    return v[0] if v else None


def _qint(query, key, default):
    v = _qstr(query, key)
    if v is None or v == "":
        return default
    if len(v) > 19 or not v.isdigit():
        raise ApiError(400, "%s must be a whole number" % key)
    n = int(v)
    if n > 2 ** 63 - 1:
        raise ApiError(400, "%s is too large" % key)
    return n


def _r(method, path, fn):
    return (method, re.compile(path), fn)


ROUTES = [
    _r("GET", r"/api/v1/health", Handler.r_health),
    _r("GET", r"/join", Handler.r_join_page),
    _r("POST", r"/api/v1/join", Handler.r_join),
    _r("GET", r"/api/v1/me", Handler.r_me),
    _r("GET", r"/api/v1/members", Handler.r_members),
    _r("PATCH", r"/api/v1/members/(\d{1,12})", Handler.r_member_patch),
    _r("POST", r"/api/v1/invites", Handler.r_invite),
    _r("DELETE", r"/api/v1/invites/(\d{1,12})", Handler.r_invite_delete),
    _r("DELETE", r"/api/v1/devices/(\d{1,12})", Handler.r_device_delete),
    _r("GET", r"/api/v1/items", Handler.r_items_get),
    _r("POST", r"/api/v1/items", Handler.r_items_post),
    _r("GET", r"/api/v1/events", Handler.r_events_get),
    _r("POST", r"/api/v1/events", Handler.r_events_post),
    _r("GET", r"/api/v1/comments/counts", Handler.r_comment_counts),
    _r("GET", r"/api/v1/comments", Handler.r_comments_get),
    _r("POST", r"/api/v1/comments", Handler.r_comments_post),
    _r("PATCH", r"/api/v1/comments/(\d{1,12})", Handler.r_comment_patch),
    _r("GET", r"/api/v1/mentions", Handler.r_mentions),
    _r("POST", r"/api/v1/mentions/read", Handler.r_mentions_read),
    _r("GET", r"/api/v1/presence", Handler.r_presence_get),
    _r("POST", r"/api/v1/presence", Handler.r_presence_post),
    _r("GET", r"/api/v1/claims", Handler.r_claims_get),
    _r("POST", r"/api/v1/claims", Handler.r_claims_post),
    _r("DELETE", r"/api/v1/claims", Handler.r_claims_delete),
    _r("GET", r"/api/v1/stream", Handler.r_stream),
]
ROUTES += [_r(m, p, fn) for m, p, fn in reviews.ROUTES]  # reviews, approval policy, owners (reviews.py)
ROUTES += teamsettings.routes(_r)  # team settings (teamsettings.py)
ROUTES += notify.routes(_r)  # notifications, prefs, Focus and Away (notify.py)


_BUSY = (b"HTTP/1.1 503 Service Unavailable\r\nContent-Type: application/json\r\nContent-Length: 34\r\n"
         b"Connection: close\r\n\r\n{\"error\":\"server busy, try again\"}")


_TOO_MANY = (b"HTTP/1.1 429 Too Many Requests\r\nContent-Type: application/json\r\nContent-Length: 55\r\n"
             b"Connection: close\r\n\r\n{\"error\":\"too many open connections from your address\"}")


class _HTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with a cap on concurrent connections (each one holds a thread)."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, max_connections, ip_slots):
        self.slots = threading.BoundedSemaphore(max_connections)
        self.ip_slots = ip_slots  # None when behind a trusted proxy (then capped per request by forwarded IP)
        super().__init__(addr, handler)

    def _refuse(self, request, response):
        try:
            request.settimeout(1)
            request.sendall(response)
        except OSError:
            pass
        self.shutdown_request(request)

    def process_request(self, request, client_address):
        if self.ip_slots is not None and not self.ip_slots.acquire(client_address[0]):
            self._refuse(request, _TOO_MANY)
            return
        if not self.slots.acquire(blocking=False):
            if self.ip_slots is not None:
                self.ip_slots.release(client_address[0])
            self._refuse(request, _BUSY)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._release(client_address)
            raise

    def _release(self, client_address):
        self.slots.release()
        if self.ip_slots is not None:
            self.ip_slots.release(client_address[0])

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._release(client_address)


class TeamServer:
    """The running server: store + HTTP + live updates. `clock` and `ping_interval` are for tests."""

    def __init__(self, data_dir, host="127.0.0.1", port=8790, public_url=None, clock=None,
                 ping_interval=20.0, verbose=False, trust_proxy=False, max_connections=MAX_CONNECTIONS,
                 max_streams_per_device=MAX_STREAMS_PER_DEVICE, request_timeout=REQUEST_TIMEOUT,
                 device_check_interval=DEVICE_CHECK_INTERVAL, request_deadline=REQUEST_DEADLINE,
                 max_connections_per_ip=MAX_CONNECTIONS_PER_IP, smtp=None, allow_private_webhooks=False,
                 notify_interval=1.0):
        os.makedirs(data_dir, exist_ok=True)
        self.clock = clock or time.time
        self.store = Store(os.path.join(data_dir, "team.db"), clock=self.clock)
        teamsettings.install(self.store)
        self.broker = Broker()
        self.store.on_emit = self.broker.publish
        reviews.install(self.store)
        # notify_interval None: no sender thread (tests call notifier.run_once())
        self.notifier = notify.install(self.store, smtp=smtp, allow_private_webhooks=allow_private_webhooks,
                                       interval=notify_interval)
        self.public_url = public_url
        self.ping_interval = ping_interval
        self.verbose = verbose
        self.trust_proxy = trust_proxy
        self.max_streams_per_device = max_streams_per_device
        self.request_timeout = request_timeout
        self.device_check_interval = device_check_interval
        self.request_deadline = request_deadline
        self.deadlines = Deadlines()
        self.request_ips = IpSlots(max_connections_per_ip)
        self.stopping = threading.Event()
        self.join_limit = RateLimiter(JOIN_LIMIT[0], JOIN_LIMIT[1], time.time)
        self.req_limit = RateLimiter(REQUEST_LIMIT[0], REQUEST_LIMIT[1], time.time)
        self.httpd = _HTTPServer((host, port), Handler, max_connections,
                                 None if trust_proxy else IpSlots(max_connections_per_ip))
        self.httpd.team = self
        self.port = self.httpd.server_address[1]
        self._threads = []

    def url_for(self, handler=None):
        """Public base URL for links: configured URL, else the request's Host header, else localhost."""
        if self.public_url:
            return self.public_url.rstrip("/")
        if handler is not None and handler.headers.get("Host"):
            return "http://%s" % handler.headers["Host"]
        return "http://localhost:%d" % self.port

    def _expiry_loop(self):
        """Background housekeeping: shut slow requests every 0.25 s, expire presence every 5 s."""
        last = time.monotonic()
        while not self.stopping.wait(0.25):
            try:
                self.deadlines.sweep()
                if time.monotonic() - last >= 5:
                    last = time.monotonic()
                    self.store.expire_presence()
            except Exception:
                traceback.print_exc(file=sys.stderr)

    def start(self):
        """Serve in background threads (tests, embedding)."""
        for target in (self.httpd.serve_forever, self._expiry_loop):
            t = threading.Thread(target=target, daemon=True)
            t.start()
            self._threads.append(t)
        self.notifier.start(self.stopping)
        return self

    def serve_forever(self):
        """Serve in the foreground until Ctrl-C."""
        t = threading.Thread(target=self._expiry_loop, daemon=True)
        t.start()
        self.notifier.start(self.stopping)
        try:
            self.httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def stop(self):
        if self.stopping.is_set():
            return
        self.stopping.set()
        self.notifier.wake.set()
        self.broker.close_all()
        self.httpd.shutdown()
        self.httpd.server_close()
        t = self.notifier.thread  # daemon; give a pass in progress a moment to finish before the database closes
        if t is not None and t is not threading.current_thread():
            t.join(timeout=2.0)
        self.store.close()
