"""HTTP layer of the team server: routing, auth, limits, JSON errors and the SSE stream.

TeamServer wires a Store to a ThreadingHTTPServer and a Broker (fan-out of live updates to open streams).
"""
import json
import os
import queue
import re
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

from . import PROTOCOL, __version__
from .store import ApiError, MANAGERS, Store, iso

MAX_BODY_BYTES = 4 * 1024 * 1024
JOIN_LIMIT = (10, 60)        # attempts per IP per window (seconds)
REQUEST_LIMIT = (1200, 60)   # requests per device per window


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


class Broker:
    """Keeps one queue per open SSE stream and routes live updates to them."""

    def __init__(self):
        self.lock = threading.Lock()
        self.subs = {}  # sid -> (member_id, device_id, queue)
        self.next_id = 0

    def subscribe(self, member_id, device_id):
        q = queue.Queue(maxsize=1000)
        with self.lock:
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

    # ---------- helpers ----------

    @property
    def team(self):
        return self.server.team

    def log_message(self, fmt, *args):
        if self.team.verbose:
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def log_request(self, code="-", size="-"):
        if self.team.verbose:
            path = self.path.split("?", 1)[0]  # never log ?token=
            sys.stderr.write("%s %s %s %s\n" % (self.address_string(), self.command, path, code))

    def send_json(self, status, obj):
        body = json.dumps(obj, separators=(",", ":")).encode("utf-8")
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
        raw = self.rfile.read(n) if n else b""
        if not raw:
            return {}
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "body must be JSON")
        if not isinstance(obj, dict):
            raise ApiError(400, "body must be a JSON object")
        return obj

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

    def do_DELETE(self):
        self.dispatch("DELETE")

    def dispatch(self, method):
        try:
            u = urlparse(self.path)
            query = parse_qs(u.query)
            path = u.path.rstrip("/") or "/"
            for m, pattern, fn in ROUTES:
                match = pattern.fullmatch(path)
                if match and m == method:
                    body = self.read_json() if method in ("POST", "PATCH", "DELETE") else {}
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
        page = JOIN_PAGE % {"team": _html(self.team.store.team_name()), "server": json.dumps(self.team.url_for(self))}
        data = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def r_join(self, query, body):
        self.team.join_limit.hit(self.client_address[0])
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
        token, expires = self.team.store.create_invite(body.get("email"), body.get("role"), body.get("name"),
                                                       by=ctx["member"])
        out = invite_links(self.team.url_for(self), token)
        out.update(email=body.get("email", "").strip().lower(), role=body.get("role", "").strip().lower(),
                   expires_at=iso(expires))
        return out

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
        return self.team.store.mark_read(ctx["member"]["id"], body.get("ids"))

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
        sid, q = broker.subscribe(ctx["member"]["id"], ctx["device_id"])
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
            while not self.team.stopping.is_set():
                try:
                    item = q.get(timeout=self.team.ping_interval)
                except queue.Empty:
                    if not self.team.store.device_ok(ctx["device_id"]):
                        break  # revoked or deactivated from elsewhere (for example the CLI)
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                if item is None:
                    break
                kind, payload = item
                self.wfile.write(("event: %s\ndata: %s\n\n" % (kind, json.dumps(payload, separators=(",", ":"))))
                                 .encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            broker.unsubscribe(sid)
        return None


def _html(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


def _qstr(query, key):
    v = query.get(key)
    return v[0] if v else None


def _qint(query, key, default):
    v = _qstr(query, key)
    if v is None or v == "":
        return default
    try:
        n = int(v)
    except ValueError:
        raise ApiError(400, "%s must be an integer" % key)
    if n < 0:
        raise ApiError(400, "%s must be >= 0" % key)
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


class _HTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class TeamServer:
    """The running server: store + HTTP + live updates. `clock` and `ping_interval` are for tests."""

    def __init__(self, data_dir, host="127.0.0.1", port=8790, public_url=None, clock=None,
                 ping_interval=20.0, verbose=False):
        os.makedirs(data_dir, exist_ok=True)
        self.clock = clock or time.time
        self.store = Store(os.path.join(data_dir, "team.db"), clock=self.clock)
        self.broker = Broker()
        self.store.on_emit = self.broker.publish
        self.public_url = public_url
        self.ping_interval = ping_interval
        self.verbose = verbose
        self.stopping = threading.Event()
        self.join_limit = RateLimiter(JOIN_LIMIT[0], JOIN_LIMIT[1], time.time)
        self.req_limit = RateLimiter(REQUEST_LIMIT[0], REQUEST_LIMIT[1], time.time)
        self.httpd = _HTTPServer((host, port), Handler)
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
        while not self.stopping.wait(5):
            try:
                self.store.expire_presence()
            except Exception:
                traceback.print_exc(file=sys.stderr)

    def start(self):
        """Serve in background threads (tests, embedding)."""
        for target in (self.httpd.serve_forever, self._expiry_loop):
            t = threading.Thread(target=target, daemon=True)
            t.start()
            self._threads.append(t)
        return self

    def serve_forever(self):
        """Serve in the foreground until Ctrl-C."""
        t = threading.Thread(target=self._expiry_loop, daemon=True)
        t.start()
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
        self.broker.close_all()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.store.close()
