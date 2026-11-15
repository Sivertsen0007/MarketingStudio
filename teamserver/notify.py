"""Notifications for the team server: rules, delivery (bell, email, webhook), digests, Focus and Away.
See PROTOCOL.md, "Notifications".

What pings a member (rules, per member, GET/PUT /api/v1/notify/prefs):
  always (on by default, each can be turned off)
    review_requested  a review addressed to you
    mention           a comment that @mentions you (also an @mention added by editing a comment)
    review_decided    your own request approved or sent back
    pending_changed   someone changed an item you have pending (your last write to it was tagged with a changeset and
                      you have not pushed / published it since)
    claim             a claim on something you own (an owner scope that covers it, or an item you created)
  follow (off by default, per area ads / email / seo / social)
    push / publish / send_to_editor in that area
  Edits never ping anyone (only the pending case above).

Delivery: every notification lands on the bell (in the app). Email (to the member's address, when the server has SMTP
settings) and a webhook (Slack-compatible JSON {text}) are optional. Timing per channel: instant, hourly digest (on the
hour) or daily digest (at the member's time of day). Mentions and reviews addressed to you are always instant on the
bell. Focus (until a time or until turned off) and Away hold email and webhook (the bell still counts, quietly) and send
one digest when they end. Away with a backup: new reviews addressed to you also go to the backup, who is added as a
reviewer when they may approve them.

Wiring (small hooks): Store.on_events (store.py) hands this module every committed event; Store.on_emit is wrapped so
`mention` emits (new comments and edits) become mention notifications; app.py calls install() and adds routes();
a background thread (Notifier.start) sends email and webhooks with retry and backoff.
"""
import ipaddress
import json
import os
import smtplib
import socket
import ssl
import sys
import threading
import time
import traceback
import http.client
from datetime import datetime, timezone
from email.message import EmailMessage
from urllib.parse import quote, urlsplit

from .store import ApiError, MANAGERS, iso, need_int

ALWAYS = ("review_requested", "mention", "review_decided", "pending_changed", "claim")
URGENT = ("review_requested", "mention")  # always instant on the bell, whatever the bell's timing
FOLLOW_KINDS = ("push", "publish", "send_to_editor")
AREAS = ("ads", "email", "seo", "social")
TIMINGS = ("instant", "hourly", "daily")
CHANNELS = ("email", "webhook")

MAX_TEXT = 300
MAX_URL = 2000
MAX_TRIES = 6             # attempts per channel before a batch is marked failed
BACKOFF = 30.0            # seconds after the first failure, doubling, capped at BACKOFF_MAX
BACKOFF_MAX = 3600.0
RATE = {"email": (12, 3600.0), "webhook": (30, 3600.0)}  # messages per member per window; the rest waits, then goes as one digest
TEST_RATE = (5, 600.0)    # POST /notify/test per member
DIGEST_SHOWN = 30         # lines in one digest message
KEEP_DAYS = 90            # notifications older than this are deleted
LIST_MAX = 200

SCHEMA = """
CREATE TABLE IF NOT EXISTS notify_prefs (
    member_id INTEGER PRIMARY KEY, prefs TEXT NOT NULL, webhook_url TEXT, held INTEGER NOT NULL DEFAULT 0,
    updated_at REAL);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT, member_id INTEGER NOT NULL, kind TEXT NOT NULL, actor INTEGER, area TEXT,
    coll TEXT, item_id TEXT, text TEXT NOT NULL, data TEXT, created REAL NOT NULL, bell_at REAL, email TEXT,
    webhook TEXT, read INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS notifications_member ON notifications(member_id, id);
CREATE INDEX IF NOT EXISTS notifications_email ON notifications(email);
CREATE INDEX IF NOT EXISTS notifications_webhook ON notifications(webhook);
CREATE INDEX IF NOT EXISTS notifications_bell ON notifications(bell_at);
CREATE TABLE IF NOT EXISTS notify_pending (
    member_id INTEGER NOT NULL, coll TEXT NOT NULL, item_id TEXT NOT NULL, at REAL NOT NULL,
    PRIMARY KEY (member_id, coll, item_id));
CREATE INDEX IF NOT EXISTS notify_pending_item ON notify_pending(coll, item_id);
CREATE TABLE IF NOT EXISTS notify_seen (member_id INTEGER NOT NULL, key TEXT NOT NULL, at REAL NOT NULL,
    PRIMARY KEY (member_id, key));
CREATE TABLE IF NOT EXISTS notify_channel (
    member_id INTEGER NOT NULL, channel TEXT NOT NULL, fails INTEGER NOT NULL DEFAULT 0, next_try REAL,
    last_error TEXT, PRIMARY KEY (member_id, channel));
"""


def default_prefs():
    return {"rules": {k: True for k in ALWAYS}, "follow": {a: False for a in AREAS},
            "bell": {"timing": "instant"}, "email": {"on": False, "timing": "instant"},
            "webhook": {"on": False, "timing": "instant"}, "daily_at": "09:00", "tz_offset": 0,
            "focus": {"on": False, "until": None}, "away": {"on": False, "until": None, "backup": None}}


def _merge_stored(raw):
    """Stored prefs on top of the defaults (keys added later get their default)."""
    p = default_prefs()
    try:
        got = json.loads(raw) if raw else {}
    except ValueError:
        got = {}
    for k, v in got.items():
        if k in p and isinstance(p[k], dict) and isinstance(v, dict):
            p[k].update({kk: vv for kk, vv in v.items() if kk in p[k]})
        elif k in p and not isinstance(p[k], dict):
            p[k] = v
    return p


def _parse_time(v, key):
    """ISO 8601 (or epoch seconds) -> epoch seconds; None stays None."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    if not isinstance(v, str) or len(v) > 40:
        raise ApiError(400, "%s must be an ISO 8601 time (or null)" % key)
    try:
        d = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ApiError(400, "%s must be an ISO 8601 time (or null)" % key)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp()


def _active(state, now):
    """Focus / Away: on, and either no end or an end still ahead."""
    return bool(state.get("on")) and (state.get("until") is None or now < float(state["until"]))


def held(prefs, now):
    """0 = not held, 1 = Focus, 2 = Away (email and webhook wait, then go as one digest)."""
    return 2 if _active(prefs["away"], now) else 1 if _active(prefs["focus"], now) else 0


def due_at(created, timing, prefs):
    """When a notification made at `created` is due on a channel with this timing."""
    if timing == "hourly":
        return (int(created // 3600) + 1) * 3600.0
    if timing == "daily":
        hh, mm = (int(x) for x in prefs.get("daily_at", "09:00").split(":"))
        off = int(prefs.get("tz_offset") or 0) * 60
        local = created + off
        t = int(local // 86400) * 86400 + hh * 3600 + mm * 60
        if t <= local:
            t += 86400
        return float(t - off)
    return created


def link_for(coll, iid, area=None):
    """The app link a notification opens: marketingstudio://open?coll=..&id=.. (or ?area=.. when it has no item)."""
    if not coll:
        return "marketingstudio://open" + ("?area=" + quote(area, safe="") if area in AREAS else "")
    out = "marketingstudio://open?coll=" + quote(str(coll), safe="")
    if iid:
        out += "&id=" + quote(str(iid), safe="")
    return out


def _short(text, n=MAX_TEXT):
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n - 3] + "..."


# ---------- webhook URL checks ----------

def _ip_ok(ip, allow_private):
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if allow_private:
        return not ip.is_unspecified and not ip.is_multicast
    return ip.is_global and not ip.is_multicast


def check_webhook_url(url, allow_private=False, resolve=socket.getaddrinfo):
    """-> (scheme, host, port, path, [ip, ...]) or ApiError 400. https only; every address the host resolves to must be
    public (no loopback, private, link-local or reserved ranges) unless allow_private (tests, private networks: then
    plain http is also accepted, only for such addresses)."""
    if not isinstance(url, str) or not url.strip():
        raise ApiError(400, "webhook url is required")
    url = url.strip()
    if len(url) > MAX_URL or any(ord(c) < 33 or ord(c) == 127 for c in url):
        raise ApiError(400, "webhook url is too long or has spaces or control characters")
    try:
        u = urlsplit(url)
        port = u.port
    except ValueError:
        raise ApiError(400, "webhook url is not a valid URL")
    scheme = (u.scheme or "").lower()
    if scheme not in ("https", "http") or (scheme == "http" and not allow_private):
        raise ApiError(400, "webhook url must start with https://")
    if u.username or u.password or "@" in u.netloc:
        raise ApiError(400, "webhook url must not contain a user name or password")
    host = (u.hostname or "").lower()
    if not host:
        raise ApiError(400, "webhook url has no host")
    port = port or (443 if scheme == "https" else 80)
    try:
        infos = resolve(host, port, 0, socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError):
        raise ApiError(400, "webhook host could not be resolved")
    ips = []
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0].split("%")[0])
        except ValueError:
            raise ApiError(400, "webhook host resolved to an address that is not allowed")
        if not _ip_ok(ip, allow_private):
            raise ApiError(400, "webhook host is a private, loopback or reserved address; use a public https URL")
        if scheme == "http" and ip.is_global:
            raise ApiError(400, "webhook url must start with https://")
        if str(ip) not in ips:
            ips.append(str(ip))
    if not ips:
        raise ApiError(400, "webhook host could not be resolved")
    path = (u.path or "/") + ("?" + u.query if u.query else "")
    return scheme, host, port, path, ips


class _PinnedHTTPS(http.client.HTTPSConnection):
    """HTTPS to an address checked beforehand (no second DNS lookup), with the certificate checked for the host."""

    def __init__(self, host, ip, port, timeout):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._ip = ip

    def connect(self):
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def post_webhook(url, text, allow_private=False, timeout=10.0):
    """POST {text} to the webhook. Raises RuntimeError with a short reason (never the URL: it may hold a secret)."""
    scheme, host, port, path, ips = check_webhook_url(url, allow_private)
    body = json.dumps({"text": text}, ensure_ascii=False).encode("utf-8")
    if scheme == "https":
        conn = _PinnedHTTPS(host, ips[0], port, timeout)
    else:
        conn = http.client.HTTPConnection(ips[0], port, timeout=timeout)
    try:
        conn.request("POST", path, body=body, headers={
            "Content-Type": "application/json; charset=utf-8", "User-Agent": "MarketingStudioTeam-notify/1",
            "Host": host if port in (80, 443) else "%s:%d" % (host, port)})
        r = conn.getresponse()
        r.read(4096)
        if not 200 <= r.status < 300:
            raise RuntimeError("webhook answered HTTP %d" % r.status)
    except (OSError, http.client.HTTPException) as e:
        raise RuntimeError("webhook not reachable (%s)" % type(e).__name__)
    finally:
        conn.close()


# ---------- SMTP ----------

def smtp_from_env_and_args(args=None):
    """SMTP settings from the CLI (args) and the environment. The password only ever comes from the environment
    (TEAMSERVER_SMTP_PASSWORD). None when no host is set (email is then not offered)."""
    g = lambda name, env: (getattr(args, name, None) if args is not None else None) or os.environ.get(env)  # noqa: E731
    host = g("smtp_host", "TEAMSERVER_SMTP_HOST")
    if not host:
        return None
    sender = g("smtp_from", "TEAMSERVER_SMTP_FROM")
    if not sender:
        raise SystemExit("--smtp-from (or TEAMSERVER_SMTP_FROM) is required with an SMTP host")
    starttls = getattr(args, "smtp_starttls", None) if args is not None else None
    if starttls is None:
        starttls = os.environ.get("TEAMSERVER_SMTP_STARTTLS", "1").lower() not in ("0", "false", "no")
    return {"host": host, "port": int(g("smtp_port", "TEAMSERVER_SMTP_PORT") or 587),
            "user": g("smtp_user", "TEAMSERVER_SMTP_USER") or "", "password": os.environ.get("TEAMSERVER_SMTP_PASSWORD", ""),
            "from": sender, "starttls": bool(starttls)}


def add_cli_args(sp):
    """`serve` options for notifications (cli.py)."""
    sp.add_argument("--smtp-host", help="SMTP server for notification email (or $TEAMSERVER_SMTP_HOST)")
    sp.add_argument("--smtp-port", type=int, help="SMTP port (default 587, or $TEAMSERVER_SMTP_PORT)")
    sp.add_argument("--smtp-user", help="SMTP user (or $TEAMSERVER_SMTP_USER); the password only from $TEAMSERVER_SMTP_PASSWORD")
    sp.add_argument("--smtp-from", help="From address for notification email (or $TEAMSERVER_SMTP_FROM)")
    sp.add_argument("--smtp-starttls", dest="smtp_starttls", action="store_true", default=None,
                    help="use STARTTLS (the default; or $TEAMSERVER_SMTP_STARTTLS=1)")
    sp.add_argument("--no-smtp-starttls", dest="smtp_starttls", action="store_false",
                    help="plain SMTP (only for a relay on the same host or private network)")
    sp.add_argument("--allow-private-webhooks", action="store_true",
                    default=os.environ.get("TEAMSERVER_ALLOW_PRIVATE_WEBHOOKS", "") in ("1", "true", "yes"),
                    help="accept webhook URLs on private / loopback addresses (tests, private networks)")


def send_email(smtp, to, subject, body, timeout=20.0):
    msg = EmailMessage()
    msg["From"] = smtp["from"]
    msg["To"] = to
    msg["Subject"] = " ".join(subject.split())[:200]
    msg.set_content(body, cte="8bit")  # links stay readable (no quoted-printable "=3D")
    try:
        with smtplib.SMTP(smtp["host"], smtp["port"], timeout=timeout) as s:
            if smtp.get("starttls"):
                s.starttls(context=ssl.create_default_context())
            if smtp.get("user"):
                s.login(smtp["user"], smtp.get("password") or "")
            s.send_message(msg)
    except (OSError, smtplib.SMTPException) as e:
        raise RuntimeError("email not sent (%s)" % type(e).__name__)


# ---------- the notifier ----------

class _Rate:
    """Sliding window of send times per key, on the injected clock."""

    def __init__(self):
        self.hits = {}

    def ok(self, key, limit, window, now):
        hs = [t for t in self.hits.get(key, []) if now - t < window]
        self.hits[key] = hs
        return len(hs) < limit

    def add(self, key, now):
        self.hits.setdefault(key, []).append(now)


class Notifier:
    def __init__(self, store, smtp=None, allow_private_webhooks=False, team_name=None, interval=1.0,
                 webhook_sender=None, email_sender=None):
        self.store = store
        self.smtp = smtp
        self.allow_private = allow_private_webhooks
        self.interval = interval
        self.team_name = team_name or store.team_name
        self.post_webhook = webhook_sender or (lambda url, text: post_webhook(url, text, self.allow_private))
        self.send_email = email_sender or (lambda to, subject, body: send_email(self.smtp, to, subject, body))
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.rate = _Rate()
        self.last_prune = 0.0
        self.thread = None
        with store.lock:
            store.db.executescript(SCHEMA)

    # ----- prefs -----

    def _prefs_row(self, db, mid):
        return db.execute("SELECT * FROM notify_prefs WHERE member_id=?", (mid,)).fetchone()

    def prefs_of(self, db, mid):
        r = self._prefs_row(db, mid)
        return _merge_stored(r["prefs"] if r else None), (r["webhook_url"] if r else None)

    def _prefs_out(self, db, mid, prefs, url):
        now = self.store.now()
        p = json.loads(json.dumps(prefs))
        for k in ("focus", "away"):
            p[k]["until"] = iso(p[k]["until"]) if p[k]["until"] is not None else None
            p[k]["active"] = _active(prefs[k], now)
        host = ""
        if url:
            try:
                host = (urlsplit(url).hostname or "")
            except ValueError:
                host = ""
        p["webhook"]["set"] = bool(url)
        p["webhook"]["host"] = host  # the URL itself is write-only: it often holds a secret
        b = prefs["away"].get("backup")
        p["away"]["backup_ok"] = None
        if b:
            m = db.execute("SELECT * FROM members WHERE id=?", (b,)).fetchone()
            p["away"]["backup_ok"] = bool(m) and bool(m["active"]) and (bool(m["approver"]) or m["role"] in MANAGERS)
        chans = {r["channel"]: r for r in db.execute("SELECT * FROM notify_channel WHERE member_id=?", (mid,))}
        for ch in CHANNELS:
            r = chans.get(ch)
            p[ch]["last_error"] = (r["last_error"] or "") if r else ""
        return {"prefs": p, "server": {"email": bool(self.smtp)}, "rules": list(ALWAYS), "areas": list(AREAS),
                "timings": list(TIMINGS)}

    def get_prefs(self, actor):
        with self.store.lock:
            prefs, url = self.prefs_of(self.store.db, actor["id"])
            return self._prefs_out(self.store.db, actor["id"], prefs, url)

    def put_prefs(self, actor, body):
        """Merge a partial prefs object. webhook.url: a URL to set (checked), "" or null to remove it."""
        if not isinstance(body, dict):
            raise ApiError(400, "body must be an object")
        body = body.get("prefs", body) if isinstance(body.get("prefs"), dict) else body
        now = self.store.now()
        new_url = Ellipsis
        wh = body.get("webhook")
        if isinstance(wh, dict) and "url" in wh:
            new_url = wh.get("url") or None
            if new_url is not None:
                check_webhook_url(new_url, self.allow_private)
                new_url = new_url.strip()
        with self.store.tx() as db:
            actor = self.store._fresh_actor(db, actor)
            prefs, url = self.prefs_of(db, actor["id"])
            was_held = held(prefs, now)
            self._apply(db, actor, prefs, body)
            if new_url is not Ellipsis:
                url = new_url
                db.execute("DELETE FROM notify_channel WHERE member_id=? AND channel='webhook'", (actor["id"],))
            db.execute("INSERT INTO notify_prefs(member_id, prefs, webhook_url, held, updated_at) VALUES(?,?,?,?,?)"
                       " ON CONFLICT(member_id) DO UPDATE SET prefs=excluded.prefs, webhook_url=excluded.webhook_url,"
                       " held=CASE WHEN ? > 0 THEN ? ELSE notify_prefs.held END, updated_at=excluded.updated_at",
                       (actor["id"], json.dumps(prefs), url, was_held, now, was_held, was_held))
            out = self._prefs_out(db, actor["id"], prefs, url)
        self.wake.set()  # Focus or Away may have ended: send the digest now
        return out

    def _apply(self, db, actor, prefs, body):
        def obj(k):
            v = body.get(k)
            if v is None:
                return None
            if not isinstance(v, dict):
                raise ApiError(400, "%s must be an object" % k)
            return v

        def boolean(v, key):
            if not isinstance(v, bool):
                raise ApiError(400, "%s must be true or false" % key)
            return v

        def timing(v, key):
            if v not in TIMINGS:
                raise ApiError(400, "%s must be one of %s" % (key, ", ".join(TIMINGS)))
            return v
        rules = obj("rules")
        for k, v in (rules or {}).items():
            if k not in ALWAYS:
                raise ApiError(400, "rules: unknown rule %s (known: %s)" % (str(k)[:40], ", ".join(ALWAYS)))
            prefs["rules"][k] = boolean(v, "rules." + k)
        follow = obj("follow")
        for k, v in (follow or {}).items():
            if k not in AREAS:
                raise ApiError(400, "follow: area must be one of %s" % ", ".join(AREAS))
            prefs["follow"][k] = boolean(v, "follow." + k)
        bell = obj("bell")
        if bell and "timing" in bell:
            prefs["bell"]["timing"] = timing(bell["timing"], "bell.timing")
        for ch in CHANNELS:
            c = obj(ch)
            if not c:
                continue
            if "on" in c:
                prefs[ch]["on"] = boolean(c["on"], ch + ".on")
            if "timing" in c:
                prefs[ch]["timing"] = timing(c["timing"], ch + ".timing")
        if "daily_at" in body:
            v = body["daily_at"]
            ok = isinstance(v, str) and len(v) == 5 and v[2] == ":" and v[:2].isdigit() and v[3:].isdigit() \
                and int(v[:2]) < 24 and int(v[3:]) < 60
            if not ok:
                raise ApiError(400, "daily_at must be HH:MM (24-hour)")
            prefs["daily_at"] = v
        if "tz_offset" in body:
            v = body["tz_offset"]
            if not isinstance(v, int) or isinstance(v, bool) or not -840 <= v <= 840:
                raise ApiError(400, "tz_offset must be whole minutes east of UTC (-840 to 840)")
            prefs["tz_offset"] = v
        focus = obj("focus")
        if focus:
            if "on" in focus:
                prefs["focus"]["on"] = boolean(focus["on"], "focus.on")
            if "until" in focus:
                prefs["focus"]["until"] = _parse_time(focus["until"], "focus.until")
        away = obj("away")
        if away:
            if "on" in away:
                prefs["away"]["on"] = boolean(away["on"], "away.on")
            if "until" in away:
                prefs["away"]["until"] = _parse_time(away["until"], "away.until")
            if "backup" in away:
                b = away["backup"]
                if b is not None:
                    b = need_int(b, "away.backup", 1)
                    if b == actor["id"]:
                        raise ApiError(400, "pick someone else as your backup")
                    if not db.execute("SELECT 1 FROM members WHERE id=? AND active=1", (b,)).fetchone():
                        raise ApiError(400, "away.backup: unknown member %d" % b)
                prefs["away"]["backup"] = b

    # ----- making notifications -----

    def on_emit_wrap(self, inner):
        """Wraps Store.on_emit: a `mention` emit (new comment or an edit that adds a mention) -> a notification."""
        def emit(kind, payload, to=None):
            if inner:
                inner(kind, payload, to)
            if kind == "mention" and to:
                try:
                    self._on_mention(payload.get("comment_id"), list(to))
                except Exception:
                    traceback.print_exc(file=sys.stderr)
        return emit

    def on_events(self, entries):
        made = []
        for e in entries:
            try:
                if e[0] == "edit_session":
                    made += self._on_edit(e[1], e[2], e[3])
                else:
                    made += self._on_event(e[1])
            except Exception:
                traceback.print_exc(file=sys.stderr)
        self._announce(made)

    def _announce(self, made):
        """SSE `notify` to each member with a notification now on their bell; wake the sender."""
        if not made:
            return
        now = self.store.now()
        seen = {}
        for mid, nid, on_bell in made:
            if on_bell:
                seen[mid] = nid
        for mid, nid in seen.items():
            with self.store.lock:
                n = self.store.db.execute("SELECT COUNT(*) n FROM notifications WHERE member_id=? AND read=0"
                                          " AND bell_at IS NOT NULL", (mid,)).fetchone()["n"]
                prefs, _ = self.prefs_of(self.store.db, mid)
            self.store.emit("notify", {"id": nid, "unread": n, "quiet": bool(held(prefs, now))}, {mid})
        self.wake.set()

    def _add(self, db, mid, kind, actor, area, coll, iid, text, data=None, dedupe=None):
        """One notification for member mid (inside a transaction). -> (mid, id, on_bell) or None."""
        if mid is None or mid == actor:
            return None
        m = db.execute("SELECT * FROM members WHERE id=?", (mid,)).fetchone()
        if not m or not m["active"]:
            return None
        prefs, url = self.prefs_of(db, mid)
        if kind in prefs["rules"] and not prefs["rules"][kind]:
            return None
        now = self.store.now()
        if dedupe:
            if db.execute("SELECT 1 FROM notify_seen WHERE member_id=? AND key=?", (mid, dedupe)).fetchone():
                return None
            db.execute("INSERT INTO notify_seen(member_id, key, at) VALUES(?,?,?)", (mid, dedupe, now))
        bell_at = now if (kind in URGENT or prefs["bell"]["timing"] == "instant") else None
        email = "pending" if (prefs["email"]["on"] and self.smtp and m["email"]) else None
        webhook = "pending" if (prefs["webhook"]["on"] and url) else None
        cur = db.execute("INSERT INTO notifications(member_id, kind, actor, area, coll, item_id, text, data, created,"
                         " bell_at, email, webhook, read) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0)",
                         (mid, kind, actor, area, coll, iid, _short(text), json.dumps(data or {}), now, bell_at,
                          email, webhook))
        return (mid, cur.lastrowid, bell_at is not None)

    @staticmethod
    def _name(db, mid):
        r = db.execute("SELECT name FROM members WHERE id=?", (mid,)).fetchone() if mid else None
        return r["name"] if r else "Someone"

    def _on_mention(self, cid, members):
        if not cid:
            return
        made = []
        with self.store.tx() as db:
            c = db.execute("SELECT * FROM comments WHERE id=?", (cid,)).fetchone()
            if not c:
                return
            who = self._name(db, c["author"])
            area = (c["coll"] or "").split(":", 1)[0]
            for mid in members:
                r = self._add(db, mid, "mention", c["author"], area, c["coll"], c["item_id"],
                              "%s mentioned you: %s" % (who, _short(c["body"], 160)), {"comment_id": cid},
                              dedupe="mention:%d" % cid)
                if r:
                    made.append(r)
        self._announce(made)

    def _on_event(self, rowid):
        made = []
        with self.store.lock:
            k = self.store.db.execute("SELECT kind FROM events WHERE rowid_=?", (rowid,)).fetchone()
        if not k or k["kind"] not in ("review_requested", "review_decided", "claim") + FOLLOW_KINDS:
            return made
        with self.store.tx() as db:
            e = db.execute("SELECT * FROM events WHERE rowid_=?", (rowid,)).fetchone()
            if not e:
                return []
            kind, actor = e["kind"], e["actor"]
            try:
                data = json.loads(e["data"] or "{}")
            except ValueError:
                data = {}
            who = self._name(db, actor)
            if kind == "review_requested":
                made += self._review_requested(db, e, data, who)
            elif kind == "review_decided":
                rv = db.execute("SELECT * FROM reviews WHERE id=?", (data.get("review"),)).fetchone()
                if rv:
                    made.append(self._add(db, rv["requested_by"], "review_decided", actor, e["area"], "review",
                                          str(rv["id"]), e["summary"] or "%s decided your review" % who,
                                          {"review": rv["id"], "status": data.get("status")}))
            elif kind == "claim" and data.get("claimed"):
                for mid in self._owners_of(db, e["coll"], e["item_id"]):
                    made.append(self._add(db, mid, "claim", actor, e["area"], e["coll"], e["item_id"],
                                          "%s claimed %s/%s, which you own" % (who, e["coll"], e["item_id"]),
                                          dedupe="claim:%d" % e["seq"]))
            elif kind in FOLLOW_KINDS:
                self._clear_pending(db, actor, e, data)
                area = e["area"] or ""
                if area in AREAS:
                    for r in db.execute("SELECT member_id, prefs FROM notify_prefs").fetchall():
                        if _merge_stored(r["prefs"])["follow"].get(area):
                            made.append(self._add(db, r["member_id"], kind, actor, area, e["coll"], e["item_id"],
                                                  "%s %s" % (who, e["summary"] or kind), {"seq": e["seq"]}))
        return [m for m in made if m]

    def _review_requested(self, db, e, data, who):
        from . import reviews  # noqa: PLC0415
        made = []
        rid = data.get("review")
        rv = db.execute("SELECT * FROM reviews WHERE id=?", (rid,)).fetchone()
        if not rv:
            return made
        title = rv["title"] or "a review"
        reviewers = json.loads(rv["reviewers"])
        now = self.store.now()
        added = []
        for mid in list(reviewers):
            made.append(self._add(db, mid, "review_requested", e["actor"], e["area"], "review", str(rid),
                                  "%s asked you to review: %s" % (who, title), {"review": rid}))
            prefs, _ = self.prefs_of(db, mid)
            b = prefs["away"].get("backup")
            if not (_active(prefs["away"], now) and b) or b in reviewers or b in added:
                continue
            bm = db.execute("SELECT * FROM members WHERE id=?", (b,)).fetchone()
            if (not bm or b == rv["requested_by"] or not reviews._eligible(bm)
                    or reviews._authored_by(db, json.loads(rv["items"]), b) or rv["status"] != "open"):
                continue  # not someone who may approve this review: the request stays with the reviewers asked
            added.append(b)
            made.append(self._add(db, b, "review_requested", e["actor"], e["area"], "review", str(rid),
                                  "%s asked %s to review: %s (you are %s's backup while they are away)"
                                  % (who, self._name(db, mid), title, self._name(db, mid)),
                                  {"review": rid, "backup_for": mid}))
        if added:
            db.execute("UPDATE reviews SET reviewers=?, updated_at=? WHERE id=?",
                       (json.dumps(reviewers + added), now, rid))
            for b in added:
                self.store.emit("review", {"id": rid, "status": "open"}, {b})
        return made

    @staticmethod
    def _owners_of(db, coll, iid):
        """Members who own a claimed item: an owner scope covering it (the collection, or coll/id and anything under
        it), and the member who created the item."""
        out = []
        key = "%s/%s" % (coll, iid) if iid else coll
        for r in db.execute("SELECT scope, member_id FROM owners"):
            s = r["scope"]
            if s == coll or s == key or key.startswith(s + "/") or (iid and s.startswith(key + "/")):
                if r["member_id"] not in out:
                    out.append(r["member_id"])
        if iid:
            it = db.execute("SELECT author FROM items WHERE coll=? AND id=?", (coll, iid)).fetchone()
            if it and it["author"] and it["author"] not in out:
                out.append(it["author"])
        return out

    @staticmethod
    def _clear_pending(db, actor, e, data):
        """A push / publish by the member: what it lists is no longer pending for them."""
        items = [x for x in (data.get("items") or []) if isinstance(x, dict)]
        if items:
            for x in items:
                db.execute("DELETE FROM notify_pending WHERE member_id=? AND coll=? AND item_id=?",
                           (actor, str(x.get("coll")), str(x.get("id"))))
        elif e["coll"] and e["item_id"]:
            db.execute("DELETE FROM notify_pending WHERE member_id=? AND coll=? AND item_id=?",
                       (actor, e["coll"], e["item_id"]))
        elif e["coll"]:
            db.execute("DELETE FROM notify_pending WHERE member_id=? AND coll=?", (actor, e["coll"]))
        elif e["area"]:
            db.execute("DELETE FROM notify_pending WHERE member_id=? AND (coll=? OR coll LIKE ?)",
                       (actor, e["area"], e["area"] + ":%"))

    def _on_edit(self, session, actor, touched):
        """Item writes never ping, except: someone else changed an item a member has pending."""
        made = []
        now = self.store.now()
        with self.store.tx() as db:
            hit = {}
            for coll, iid in touched:
                r = db.execute("SELECT updated_by, changeset, deleted FROM items WHERE coll=? AND id=?",
                               (coll, iid)).fetchone()
                if r and r["updated_by"] == actor and r["changeset"] and not r["deleted"]:
                    db.execute("INSERT INTO notify_pending(member_id, coll, item_id, at) VALUES(?,?,?,?)"
                               " ON CONFLICT(member_id, coll, item_id) DO UPDATE SET at=excluded.at",
                               (actor, coll, iid, now))
                for p in db.execute("SELECT member_id FROM notify_pending WHERE coll=? AND item_id=? AND member_id!=?",
                                    (coll, iid, actor)).fetchall():
                    key = "pending:%s:%s:%s/%s" % (actor, session, coll, iid)
                    if db.execute("SELECT 1 FROM notify_seen WHERE member_id=? AND key=?",
                                  (p["member_id"], key)).fetchone():
                        continue
                    hit.setdefault(p["member_id"], []).append((coll, iid, key))
            who = self._name(db, actor)
            for mid, items in hit.items():
                coll, iid = items[0][0], items[0][1]
                n = len(items)
                text = ("%s changed %s/%s, which you have pending" % (who, coll, iid) if n == 1 else
                        "%s changed %d items you have pending (%s/%s and more)" % (who, n, coll, iid))
                r = self._add(db, mid, "pending_changed", actor, coll.split(":", 1)[0], coll, iid, text,
                              {"items": [{"coll": c, "id": i} for c, i, _ in items[:50]], "session": session})
                if r:
                    for _, _, key in items:
                        db.execute("INSERT OR IGNORE INTO notify_seen(member_id, key, at) VALUES(?,?,?)",
                                   (mid, key, now))
                    made.append(r)
        return made

    # ----- reading -----

    @staticmethod
    def _out(r):
        return {"id": r["id"], "kind": r["kind"], "actor": r["actor"], "area": r["area"] or "", "coll": r["coll"],
                "item_id": r["item_id"], "text": r["text"], "data": json.loads(r["data"] or "{}"),
                "at": iso(r["created"]), "read": bool(r["read"]), "link": link_for(r["coll"], r["item_id"], r["area"])}

    def list_for(self, actor, query):
        def q(k):
            v = query.get(k)
            return v[0] if v else None
        limit = q("limit")
        limit = min(max(int(limit), 1), LIST_MAX) if limit and limit.isdigit() and len(limit) < 6 else 50
        sql, args = "SELECT * FROM notifications WHERE member_id=? AND bell_at IS NOT NULL", [actor["id"]]
        if q("unread") in ("1", "true", "yes"):
            sql += " AND read=0"
        before = q("before")
        if before:
            if not before.isdigit() or len(before) > 12:
                raise ApiError(400, "before must be a notification id")
            sql += " AND id<?"
            args.append(int(before))
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit + 1)
        with self.store.lock:
            rows = self.store.db.execute(sql, args).fetchall()
            unread = self.store.db.execute("SELECT COUNT(*) n FROM notifications WHERE member_id=? AND read=0"
                                           " AND bell_at IS NOT NULL", (actor["id"],)).fetchone()["n"]
        more = len(rows) > limit
        rows = rows[:limit]
        return {"notifications": [self._out(r) for r in rows], "unread": unread, "more": more,
                "next_before": rows[-1]["id"] if more else None}

    def mark_read(self, actor, body):
        """{ids: [..]} or {all: true}. Mention notifications also mark their mention read (and the other way round:
        mentions_read)."""
        ids = body.get("ids")
        every = body.get("all") is True
        if not every and (not isinstance(ids, list) or len(ids) > 1000):
            raise ApiError(400, "ids must be a list of notification ids (or all: true)")
        ids = [need_int(i, "ids", 1) for i in (ids or [])]
        now = self.store.now()
        with self.store.tx() as db:
            if every:
                rows = db.execute("SELECT id, kind, data FROM notifications WHERE member_id=? AND read=0"
                                  " AND bell_at IS NOT NULL", (actor["id"],)).fetchall()
            else:
                rows = [r for i in ids for r in db.execute("SELECT id, kind, data FROM notifications WHERE id=?"
                                                           " AND member_id=? AND read=0", (i, actor["id"]))]
            for r in rows:
                db.execute("UPDATE notifications SET read=1 WHERE id=?", (r["id"],))
                if r["kind"] == "mention":
                    cid = json.loads(r["data"] or "{}").get("comment_id")
                    if cid:
                        db.execute("UPDATE mentions SET read=1 WHERE comment_id=? AND member_id=?", (cid, actor["id"]))
            unread = db.execute("SELECT COUNT(*) n FROM notifications WHERE member_id=? AND read=0"
                                " AND bell_at IS NOT NULL", (actor["id"],)).fetchone()["n"]
        self.store.emit("notify", {"unread": unread, "quiet": True}, {actor["id"]})
        return {"ok": True, "updated": len(rows), "unread": unread}

    def mentions_read(self, member_id, ids):
        """POST /mentions/read marked comments read: their mention notifications are read too."""
        try:
            keys = {int(i) for i in ids or [] if isinstance(i, int) and not isinstance(i, bool)}
        except (TypeError, ValueError):
            return
        if not keys:
            return
        with self.store.tx() as db:
            for r in db.execute("SELECT id, data FROM notifications WHERE member_id=? AND kind='mention' AND read=0",
                                (member_id,)).fetchall():
                if json.loads(r["data"] or "{}").get("comment_id") in keys:
                    db.execute("UPDATE notifications SET read=1 WHERE id=?", (r["id"],))
        self.store.emit("notify", {"quiet": True}, {member_id})

    # ----- sending -----

    def start(self, stopping):
        if self.interval is None:
            return None

        def loop():
            while not stopping.is_set():
                self.wake.wait(self.interval)
                self.wake.clear()
                if stopping.is_set():
                    break
                try:
                    self.run_once()
                except Exception:
                    traceback.print_exc(file=sys.stderr)
        self.thread = threading.Thread(target=loop, daemon=True, name="notify-sender")
        self.thread.start()
        return self.thread

    def run_once(self):
        """One pass: bell digests that are due, then email / webhook for every member with something waiting."""
        with self.lock:
            now = self.store.now()
            with self.store.lock:
                mids = [r["member_id"] for r in self.store.db.execute(
                    "SELECT DISTINCT member_id FROM notifications WHERE email='pending' OR webhook='pending'"
                    " OR bell_at IS NULL UNION SELECT member_id FROM notify_prefs WHERE held>0")]
            for mid in mids:
                try:
                    self._run_member(mid, now)
                except Exception:
                    traceback.print_exc(file=sys.stderr)
            if now - self.last_prune > 3600:
                self.last_prune = now
                with self.store.tx() as db:
                    db.execute("DELETE FROM notifications WHERE created<?", (now - KEEP_DAYS * 86400,))
                    db.execute("DELETE FROM notify_seen WHERE at<?", (now - 30 * 86400,))

    def _run_member(self, mid, now):
        with self.store.lock:
            db = self.store.db
            m = db.execute("SELECT * FROM members WHERE id=?", (mid,)).fetchone()
            prefs, url = self.prefs_of(db, mid)
            row = self._prefs_row(db, mid)
            was_held = row["held"] if row else 0
            bell_rows = db.execute("SELECT id, created FROM notifications WHERE member_id=? AND bell_at IS NULL",
                                   (mid,)).fetchall()
        # bell digests: Focus and Away do not hold the bell (it counts quietly)
        due = [r["id"] for r in bell_rows if due_at(r["created"], prefs["bell"]["timing"], prefs) <= now]
        if due:
            with self.store.tx() as db:
                for i in due:
                    db.execute("UPDATE notifications SET bell_at=? WHERE id=?", (now, i))
            self._announce([(mid, max(due), True)])
        if not m or not m["active"]:
            with self.store.tx() as db:
                db.execute("UPDATE notifications SET email='skipped' WHERE member_id=? AND email='pending'", (mid,))
                db.execute("UPDATE notifications SET webhook='skipped' WHERE member_id=? AND webhook='pending'", (mid,))
            return
        h = held(prefs, now)
        if h:
            if h != was_held:
                with self.store.tx() as db:
                    db.execute("UPDATE notify_prefs SET held=? WHERE member_id=?", (h, mid))
            return
        why = None
        if was_held:  # Focus or Away just ended: everything that waited goes as one digest
            why = "while you were away" if was_held == 2 else "while you were in Focus"
        for ch in CHANNELS:
            self._run_channel(m, ch, prefs, url, now, why)
        if was_held:
            with self.store.tx() as db:
                db.execute("UPDATE notify_prefs SET held=0 WHERE member_id=?", (mid,))

    def _run_channel(self, m, ch, prefs, url, now, why):
        mid = m["id"]
        with self.store.lock:
            rows = self.store.db.execute("SELECT * FROM notifications WHERE member_id=? AND %s='pending' ORDER BY id"
                                         % ch, (mid,)).fetchall()
            st = self.store.db.execute("SELECT * FROM notify_channel WHERE member_id=? AND channel=?",
                                       (mid, ch)).fetchone()
        if not rows:
            return
        usable = prefs[ch]["on"] and (url if ch == "webhook" else (self.smtp and m["email"]))
        if not usable:  # turned off (or no URL / no SMTP any more): these will not be sent
            with self.store.tx() as db:
                db.execute("UPDATE notifications SET %s='skipped' WHERE member_id=? AND %s='pending'" % (ch, ch), (mid,))
            return
        timing = prefs[ch]["timing"]
        if not why and not any(due_at(r["created"], timing, prefs) <= now for r in rows):
            return
        if st and st["next_try"] and st["next_try"] > now:
            return  # backing off after a failure
        limit, window = RATE[ch]
        if not self.rate.ok((mid, ch), limit, window, now):
            return  # over the rate limit: they wait and go together as a digest
        subject, text = self._compose(rows, why, timing)
        try:
            if ch == "email":
                self.send_email(m["email"], subject, text + "\n\n" + FOOTER)
            else:
                self.post_webhook(url, text)
        except Exception as e:  # noqa: BLE001 - every failure is retried, then given up
            fails = (st["fails"] if st else 0) + 1
            err = str(e)[:200] if isinstance(e, (RuntimeError, ApiError)) else type(e).__name__
            if isinstance(e, ApiError):
                err = e.message[:200]
            with self.store.tx() as db:
                if fails >= MAX_TRIES:
                    db.execute("UPDATE notifications SET %s='failed' WHERE member_id=? AND %s='pending' AND id<=?"
                               % (ch, ch), (mid, rows[-1]["id"]))
                    fails, nxt = 0, None
                else:
                    nxt = now + min(BACKOFF * 2 ** (fails - 1), BACKOFF_MAX)
                db.execute("INSERT INTO notify_channel(member_id, channel, fails, next_try, last_error) VALUES(?,?,?,?,?)"
                           " ON CONFLICT(member_id, channel) DO UPDATE SET fails=excluded.fails,"
                           " next_try=excluded.next_try, last_error=excluded.last_error", (mid, ch, fails, nxt, err))
            return
        self.rate.add((mid, ch), now)
        with self.store.tx() as db:
            for r in rows:
                db.execute("UPDATE notifications SET %s='sent' WHERE id=?" % ch, (r["id"],))
            db.execute("DELETE FROM notify_channel WHERE member_id=? AND channel=?", (mid, ch))

    def _compose(self, rows, why, timing):
        """(subject, text): one notification as itself, several as a digest. Plain text, no secrets: the summary and
        an app link per line."""
        team = self.team_name() if callable(self.team_name) else self.team_name
        if len(rows) == 1 and not why:
            r = rows[0]
            return ("%s: %s" % (team, _short(r["text"], 120)),
                    "%s\nOpen in Marketing Studio: %s" % (r["text"], link_for(r["coll"], r["item_id"], r["area"])))
        n = len(rows)
        head = "%d notification%s" % (n, "" if n == 1 else "s")
        if why:
            head += " " + why
        elif timing == "hourly":
            head += " (hourly digest)"
        elif timing == "daily":
            head += " (daily digest)"
        lines = ["%s: %s" % (team, head)]
        for r in rows[-DIGEST_SHOWN:]:
            lines.append("- %s\n  %s" % (r["text"], link_for(r["coll"], r["item_id"], r["area"])))
        if n > DIGEST_SHOWN:
            lines.insert(1, "(the latest %d of %d)" % (DIGEST_SHOWN, n))
        return "%s: %s" % (team, head), "\n".join(lines)

    def test(self, actor, body):
        """POST /notify/test {channel}: send a test message now (rate limited)."""
        ch = body.get("channel")
        if ch not in CHANNELS:
            raise ApiError(400, "channel must be email or webhook")
        now = self.store.now()
        if not self.rate.ok((actor["id"], "test"), TEST_RATE[0], TEST_RATE[1], now):
            raise ApiError(429, "too many test messages, try again in a few minutes")
        self.rate.add((actor["id"], "test"), now)
        with self.store.lock:
            prefs, url = self.prefs_of(self.store.db, actor["id"])
        team = self.team_name() if callable(self.team_name) else self.team_name
        text = "%s: a test notification for %s. Notifications reach you here." % (team, actor["name"])
        try:
            if ch == "email":
                if not self.smtp:
                    raise ApiError(400, "this server has no email settings (ask the server admin)")
                self.send_email(actor["email"], "%s: test notification" % team, text + "\n\n" + FOOTER)
            else:
                if not url:
                    raise ApiError(400, "set a webhook URL first")
                self.post_webhook(url, text)
        except RuntimeError as e:
            return {"ok": False, "error": str(e)[:200]}
        return {"ok": True}


FOOTER = "You get this because of your notification settings in Marketing Studio (Team tab > Notifications)."


def install(store, smtp=None, allow_private_webhooks=False, interval=1.0, **kw):
    """Create the tables, hook the store, return the Notifier (TeamServer keeps it as .notifier)."""
    n = Notifier(store, smtp=smtp, allow_private_webhooks=allow_private_webhooks, interval=interval, **kw)
    store.on_events = n.on_events
    store.on_emit = n.on_emit_wrap(store.on_emit)
    return n


# ---------- HTTP ----------

def r_prefs_get(h, query, body):
    ctx = h.authed(query)
    return h.team.notifier.get_prefs(ctx["member"])


def r_prefs_put(h, query, body):
    ctx = h.authed(query)
    return h.team.notifier.put_prefs(ctx["member"], body)


def r_list(h, query, body):
    ctx = h.authed(query)
    return h.team.notifier.list_for(ctx["member"], query)


def r_read(h, query, body):
    ctx = h.authed(query)
    return h.team.notifier.mark_read(ctx["member"], body)


def r_test(h, query, body):
    ctx = h.authed(query)
    return h.team.notifier.test(ctx["member"], body)


def routes(make):
    """make(method, path, fn) -> a ROUTES entry (app._r)."""
    return [make("GET", r"/api/v1/notify/prefs", r_prefs_get),
            make("PUT", r"/api/v1/notify/prefs", r_prefs_put),
            make("POST", r"/api/v1/notify/test", r_test),
            make("GET", r"/api/v1/notifications", r_list),
            make("POST", r"/api/v1/notifications/read", r_read)]
