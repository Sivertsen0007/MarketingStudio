#!/usr/bin/env python3
"""Marketing Studio (Ads, Email, SEO, SoMe) on the Mac (http://localhost:4830). The Ads area is the original Ads Studio:
our own Google Ads Editor.

Shows everything Editor holds for an account, lets you (and Claude, through the MCP server in mcp_server.py)
build up a draft of changes, and pushes the draft into Editor with one click (gae apply --restart).
Editor is then only where you review and Post. Or push live: the draft goes straight to the Google Ads API
(live.py) after a validateOnly preview and your confirmation. Meta (Facebook + Instagram) works the same way
through meta_ads.py: one key per Meta ad account ("meta" for the .env account, "meta_<id>" for ones added in Settings).

  python3 shared/gae/studio/server.py [--port 4830] [--no-open]

Guard (every request): the Host header must be localhost:<port> or 127.0.0.1:<port> (stops DNS rebinding).
Every POST also needs Content-Type application/json, an Origin (if sent) of this server, and the header
X-Studio-Press with a random secret made at start-up and put into index.html when it is served. So another web page
in the browser can't press Studio's buttons. A program running on the Mac could still read the page and the secret:
the real rule stays that Claude (mcp_server.py) has no tools for live actions.
"""
import argparse, html, json, os, secrets, threading, traceback, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import core
import datapolicy
import datasources
import fx
import live
import meta_ads as meta
import overview
import health
import gapi
import progress
import areas
import setup_routes  # keys (Keychain), Sign in with Google, .env import
import team  # team sync: join a team server, sync drafts (team.py)
import team_reviews  # team approvals, push gating, why note, owners (team_reviews.py)
import updater  # [mac-app updater] in-app updates (GitHub Releases), see updater.py

HERE = Path(__file__).resolve().parent
PUSH_LOCK = threading.Lock()
PRESS = secrets.token_urlsafe(24)  # per process; the page gets it in <meta name="studio-press">
FAVICON = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="7" fill="#1f3a5f"/>'
           b'<path d="M8 21l5-6 4 4 7-9" stroke="#e8c07a" stroke-width="3" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>')


def _int(v):
    return int(v) if v not in (None, "", "null") else None


def state(acct, q=None):
    q = q or {}
    info = next((a for a in gapi.accounts_any() if a["id"] == acct), {})
    try:
        pending = core.editor_pending(acct)
    except Exception:  # noqa: BLE001 - no Editor data on this machine
        pending = {}
    try:
        running = core.gae.editor_running()
    except Exception:  # noqa: BLE001
        running = False
    return {"draft": core.draft(acct), "editor_pending": pending, "last_download": info.get("last_download"),
            "editor_running": running, "pushing": PUSH_LOCK.locked(), "last_live_push": live.last_push(acct),
            "google": gapi.info(acct, q.get("days"), q.get("start"), q.get("end"))}


def _after_live(acct, res):
    """Studio's API cache should show what was just pushed or undone: only this account is read again (and marked
    fetched). Meta does the same inside meta.push / meta.undo (meta.refresh_account)."""
    if isinstance(res, dict) and res.get("ok"):
        threading.Thread(target=lambda: gapi.refresh(acct), daemon=True).start()
    return res


_CUR = {}  # account id -> its currency (an account's currency never changes); also kept in account_currency.json


def _cur_file():
    return core.STUDIO_DIR / "account_currency.json"


def account_currency(a):
    """The account's own currency: Google from Editor's AccountSetting (else the API, asked once and kept), Meta from
    its cached account."""
    if not _CUR:
        try:
            _CUR.update(json.loads(_cur_file().read_text()))
        except (OSError, ValueError):
            pass
    if a["id"] in _CUR:
        return _CUR[a["id"]]
    cur = ""
    try:
        if a.get("channel") == "meta":
            c = core.STUDIO_DIR / f"meta_cache_{a['id']}.json"
            cur = (json.loads(c.read_text()).get("account") or {}).get("currency", "") if c.exists() else ""
        else:
            try:
                db = core._db(a["id"])
                try:
                    row = db.execute("SELECT currencyCode FROM AccountSetting LIMIT 1").fetchone()
                finally:
                    db.close()
                cur = (row[0] if row else "") or ""
            except (Exception, SystemExit):  # noqa: BLE001 - no Editor copy: ask the API (read-only)
                r = live.query(a["id"], "SELECT customer.currency_code FROM customer LIMIT 1")
                cur = (r[0] if r else {}).get("customer", {}).get("currencyCode", "")
    except Exception:  # noqa: BLE001 - unknown: the UI then shows the native figure only
        cur = ""
    if cur:
        _CUR[a["id"]] = cur.upper()
        try:
            _cur_file().write_text(json.dumps(_CUR))
        except OSError:
            pass
    return cur.upper()


def accounts_with_currency():
    return [{**a, "currency": account_currency(a)} for a in gapi.accounts_any() + meta.accounts()]


def display_currency():
    """'own' (each account in its own currency, the default) or a currency code. Display only."""
    cur = str(meta.settings().get("display_currency") or "own").strip()
    return "own" if cur.lower() == "own" else cur.upper()


def set_display_currency(cur):
    cur = (cur or "own").strip()
    cur = "own" if cur.lower() == "own" else cur.upper()
    if cur != "own" and fx.rate("EUR", cur) is None:
        raise ValueError(f"no ECB rate for {cur}")
    s = meta.settings()
    s["display_currency"] = cur
    meta.save_settings(s)
    return {"display_currency": cur}


def settings_view():
    s = meta.settings()
    accts = accounts_with_currency()
    own = sorted({a["currency"] for a in accts if a["currency"]})
    return {"google": [{**a, "source": gapi.source(a["id"])} for a in gapi.accounts_any()], "meta": meta.accounts(), "views": s["views"], "meta_token": bool(meta.TOKEN),
            "display_currency": display_currency(), "currencies": fx.DISPLAY + [c for c in own if c not in fx.DISPLAY], "fx": fx.info()}


def google_test(acct):
    """Read-only check that the Google Ads API can reach this account (needed for Push live)."""
    cid = core.resolve(acct)
    try:
        r = live.query(cid, "SELECT customer.descriptive_name, customer.currency_code FROM customer LIMIT 1")
        c = (r[0] if r else {}).get("customer", {})
        return {"ok": True, "message": f"API access OK: {c.get('descriptiveName', cid)} ({c.get('currencyCode', '')})"}
    except Exception as ex:  # noqa: BLE001
        return {"ok": False, "message": f"No API access: {ex}"}


def _range_label(q):
    """'1 Oct to 31 Oct' (year added when it is not this year), or 'the last 30 days'."""
    import datetime as dt  # noqa: PLC0415
    try:
        a, b = dt.date.fromisoformat(q["start"]), dt.date.fromisoformat(q["end"])
    except (KeyError, TypeError, ValueError):
        return f"the last {q.get('days') or 7} days"
    f = lambda d: f"{d.day} {d:%b}" + ("" if d.year == dt.date.today().year else f" {d.year}")  # noqa: E731
    return f"{f(a)} to {f(b)}"


def data_cached(q):
    """Is the date range on screen already cached for this account (so showing it pulls nothing)? For the UI's
    "Fetching stats for ..." note. No API calls."""
    acct = q.get("acct")
    label = _range_label(q)
    if meta.is_meta(acct):
        meta.use(acct)
        win = _meta_window(q)
        level = {"campaigns": "campaign", "adsets": "adset", "ads": "ad"}.get(q.get("kind") or "campaigns")
        return {"cached": meta._cache_file().exists() and (level is None or meta.insights_cached(level, **win)), "label": label}
    cid = core.resolve(acct)
    key, _ = gapi.window(q.get("days"), q.get("start"), q.get("end"))
    return {"cached": not gapi.use_api(cid) or (gapi.is_fresh(cid, "base") and gapi.is_fresh(cid, key)), "label": label}


def data_refresh(body):
    return datasources.refresh(body.get("source"), body.get("account") or None,
                               **{k: body.get(k) for k in ("days", "start", "end", "parts") if body.get(k) not in (None, "")})


def _meta_window(q):
    """The UI's date range (days, or start/end) -> meta.grid / meta.insights arguments (window, or since/until)."""
    if q.get("start") or q.get("end"):
        return {"window": None, "since": q.get("start"), "until": q.get("end")}
    return {"window": q.get("days") or q.get("window") or 7, "since": None, "until": None}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _port(self):
        return self.server.server_address[1]

    def _host_ok(self):
        """DNS rebinding guard: only answer requests addressed to this server by its loopback name."""
        return (self.headers.get("Host") or "").strip().lower() in (f"localhost:{self._port()}", f"127.0.0.1:{self._port()}")

    def _post_problem(self):
        """None if this POST came from Studio's own page, else why not."""
        if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
            return "POST needs Content-Type application/json"
        origin = self.headers.get("Origin")
        if origin is not None and origin.rstrip("/").lower() not in (f"http://localhost:{self._port()}", f"http://127.0.0.1:{self._port()}"):
            return "POST from another site is not allowed"
        if not secrets.compare_digest(self.headers.get("X-Studio-Press") or "", PRESS):
            return "POST needs Studio's own page (missing or wrong X-Studio-Press header): reload the page"
        return None

    def _index(self):
        page = (HERE / "index.html").read_text(encoding="utf-8")
        page = page.replace("<head>", f'<head>\n<meta name="studio-press" content="{html.escape(PRESS)}">', 1)
        return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")

    def _run(self, fn):
        try:
            self._send(200, fn())
        except Exception as ex:  # noqa: BLE001 - surface every error in the UI
            traceback.print_exc()
            self._send(400, {"error": str(ex)})

    def _media(self, media_id):
        """A file stored for a new Meta ad (not on Meta yet), so the UI can show it."""
        try:
            path, ctype = meta.media_file(media_id)
            data = path.read_bytes()
        except Exception as ex:  # noqa: BLE001
            return self._send(404, {"error": str(ex)})
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    def _static(self, rel):
        f = (HERE / "static" / rel).resolve()
        if HERE / "static" not in f.parents or not f.is_file():
            return self._send(404, {"error": "not found"})
        ctype = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}.get(f.suffix, "application/octet-stream")
        return self._send(200, f.read_bytes(), ctype)

    def _area(self, method, path, args):
        """/api/area/<name>/<key> -> areas/<name>.py GET/POST[key](args)."""
        parts = path.split("?")[0].split("/")[3:]
        if len(parts) != 2 or parts[0] not in areas.AREAS:
            return self._send(404, {"error": "no such area"})
        fn = getattr(areas.AREAS[parts[0]], method, {}).get(parts[1])
        if not fn:
            return self._send(404, {"error": "not found"})
        return self._run(lambda: fn(args))

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if not self._host_ok():
            return self._send(403, {"error": "Studio only answers on localhost (wrong Host header)"})
        if u.path in ("/", "/index.html"):
            return self._index()
        if u.path == "/favicon.ico":
            return self._send(200, FAVICON, "image/svg+xml")
        if u.path == "/api/accounts":
            return self._run(accounts_with_currency)
        if u.path == "/api/fx":
            return self._run(lambda: {**fx.info(), "display_currency": display_currency()})
        if u.path == "/api/overview":
            return self._run(lambda: overview.build(q.get("days", "7"), display_currency()))
        if u.path == "/api/settings":
            return self._run(settings_view)
        if u.path.startswith("/static/"):
            return self._static(u.path[len("/static/"):])
        if u.path == "/api/setup":
            return self._run(setup_routes.status)
        if u.path == "/oauth/google/callback":
            code, page = setup_routes.google_callback(q)
            return self._send(code, page.encode(), "text/html; charset=utf-8")
        # [mac-app updater] begin - version + update check for the native app's "Update available" banner
        if u.path == "/api/app/version":
            return self._run(updater.version_info)
        if u.path == "/api/app/update/check":
            return self._run(lambda: updater.status(force=q.get("force") == "1"))
        # [mac-app updater] end
        if u.path == "/api/team/status":
            return self._run(team.status)
        if u.path == "/api/team/conflicts":
            return self._run(team.conflicts)
        if u.path == "/api/team/changeset":
            return self._run(team.changeset_get)
        if u.path.startswith("/api/team/reviews/"):  # [team reviews]
            return self._run(lambda: team_reviews.get_route(u.path, q))
        if u.path == "/api/areas":
            return self._run(lambda: [{"name": n, "label": getattr(m, "LABEL", n)} for n, m in areas.AREAS.items()])
        if u.path == "/api/progress":
            return self._run(lambda: progress.get(q.get("key", "")))
        if u.path.startswith("/api/area/"):
            return self._area("GET", u.path, q)
        if u.path == "/api/data/status":
            return self._run(datasources.status)
        if u.path == "/api/data/cached":
            return self._run(lambda: data_cached(q))
        if u.path == "/api/data/calls" and os.environ.get("STUDIO_CALL_LOG"):
            return self._run(lambda: datapolicy.calls(q.get("reset") == "1"))
        if u.path == "/api/health":
            return self._run(lambda: {"summary": health.summary(), **(health.cached() or {"checked_at": None, "results": []})})
        if meta.is_meta(q.get("acct")):
            try:
                meta.use(q.get("acct"))
            except ValueError as ex:
                return self._send(400, {"error": str(ex)})
            if u.path == "/api/meta/media":
                return self._media(q.get("id"))
            win = _meta_window(q)
            routes = {
                "/api/meta/preview": lambda: meta.ad_preview(q["id"], q.get("placement", "facebook"), q.get("creative")),
                "/api/meta/insights": lambda: meta.insights(q.get("level", "campaign"), **win),
                "/api/meta/media/list": meta.list_media,
                "/api/tree": lambda: meta.tree(q.get("status", "active")),
                "/api/grid": lambda: meta.grid(q["kind"], q.get("campaign_id"), q.get("ad_group_id"), q.get("status", "active"), q.get("search"), **win),
                "/api/state": lambda: {**meta.state(), "pushing": PUSH_LOCK.locked()},
                "/api/live/history": lambda: meta.history(),
                "/api/live/progress": lambda: progress.get(meta.acct()),
            }
            return self._run(routes[u.path]) if u.path in routes else self._send(404, {"error": "not found"})
        acct = core.resolve(q.get("acct"))
        routes = {
            "/api/tree": lambda: gapi.tree_any(acct, q.get("status", "active")),
            "/api/grid": lambda: gapi.grid_any(acct, q["kind"], _int(q.get("campaign_id")), _int(q.get("ad_group_id")),
                                               q.get("status", "active"), q.get("search"), None, q.get("days"), q.get("start"), q.get("end")),
            "/api/state": lambda: state(acct, q),
            "/api/geo": lambda: gapi.geo_any(acct, q.get("q", "")),
            "/api/live/history": lambda: live.history(acct),
            "/api/live/progress": lambda: progress.get(acct),
        }
        if u.path in routes:
            return self._run(routes[u.path])
        self._send(404, {"error": "not found"})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if not self._host_ok():
            return self._send(403, {"error": "Studio only answers on localhost (wrong Host header)"})
        why = self._post_problem()
        if why:
            return self._send(403, {"error": why})
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            return self._send(400, {"error": "body is not valid JSON"})
        if not isinstance(body, dict):
            return self._send(400, {"error": "body must be a JSON object"})
        if self.path == "/api/setup/save":
            return self._run(lambda: setup_routes.save(body))
        if self.path == "/api/setup/import_env":
            return self._run(lambda: setup_routes.import_env(body))
        if self.path == "/api/setup/google/start":
            return self._run(lambda: setup_routes.google_start(self.server.server_address[1]))
        if self.path == "/api/setup/google/sign_out":
            return self._run(lambda: setup_routes.google_sign_out(body))
        if self.path == "/api/app/update/apply":  # [mac-app updater] download, verify, swap the .app
            return self._run(updater.apply_update)
        if self.path.startswith("/api/team/"):
            return self._team_post(body)
        if self.path == "/api/data/refresh":
            return self._run(lambda: data_refresh(body))
        if self.path.startswith("/api/settings/"):
            return self._settings_post(body)
        if self.path.startswith("/api/area/"):
            return self._area("POST", self.path, body)
        if meta.is_meta(body.get("acct")):
            return self._meta_post(body)
        acct = core.resolve(body.get("acct"))

        def push():
            if not PUSH_LOCK.acquire(blocking=False):
                raise ValueError("a push is already running")
            try:  # [team reviews] gated like Push live when the team requires approval for Ads
                return team_reviews.gated_push("ads", f"ads:{acct}", body, lambda only: core.push(
                    acct, body.get("title"), body.get("why"), only), "Sent to Google Ads Editor", kind="send_to_editor")
            finally:
                PUSH_LOCK.release()

        def live_push():
            if not PUSH_LOCK.acquire(blocking=False):
                raise ValueError("a push is already running")
            try:
                # [team reviews] only approved items (when the team requires it) / only mine; why note; feed event
                return _after_live(acct, team_reviews.gated_push("ads", f"ads:{acct}", body, lambda only: live.push(
                    acct, bool(body.get("enable_new")), bool(body.get("accept_spend")), bool(body.get("accept_permanent")), only),
                    "Pushed live to Google Ads"))
            finally:
                PUSH_LOCK.release()

        routes = {
            "/api/live/preview": lambda: team_reviews.gated_preview("ads", f"ads:{acct}", body, lambda only: live.preview(
                acct, bool(body.get("enable_new")), only=only)),
            "/api/live/push": live_push,
            "/api/live/undo": lambda: _after_live(acct, live.undo(acct, body["push_id"])),
            "/api/google/refresh": lambda: gapi.refresh(acct, body.get("days"), body.get("start"), body.get("end")),
            "/api/draft/add": lambda: core.add(acct, body["changes"], body.get("by", "you"), body.get("note")),
            "/api/draft/remove": lambda: {"removed": core.remove(acct, body["ids"])},
            "/api/draft/clear": lambda: {"removed": core.clear(acct)},
            "/api/validate": lambda: core.validate(acct),
            "/api/push": push,
        }
        if self.path in routes:
            return self._run(routes[self.path])
        self._send(404, {"error": "not found"})

    def _team_post(self, body):
        if self.path.startswith("/api/team/reviews/"):  # [team reviews]
            return self._run(lambda: team_reviews.post_route(self.path, body))
        def changeset():
            act = body.get("action") or "set"
            if act == "rename":
                return team.changeset_rename(body.get("id"), body.get("name"))
            if act == "set":
                return team.changeset_set(body.get("id"), body.get("name"))
            raise ValueError("action must be set or rename")

        routes = {
            "/api/team/join": lambda: team.join(body.get("link"), body.get("server"), body.get("invite")),
            "/api/team/leave": team.leave,
            "/api/team/sync": team.sync,
            "/api/team/resolve": lambda: team.resolve(body["coll"], body["id"], body["field"], body["choice"], body.get("value")),
            "/api/team/changeset": changeset,
            "/api/team/settings": lambda: team.settings(body.get("auto_sync")),
        }
        if self.path in routes:
            return self._run(routes[self.path])
        self._send(404, {"error": "not found"})

    def _settings_post(self, body):
        def save_view():
            s = meta.settings()
            v = {k: body.get(k) for k in ("name", "channel", "kind", "cols", "filter", "sort")}
            if not (v["name"] or "").strip() or not v["kind"] or not v["cols"]:
                raise ValueError("a view needs a name, a type and columns")
            v["name"] = v["name"].strip()
            s["views"] = [x for x in s["views"] if not (x["name"] == v["name"] and x["channel"] == v["channel"] and x["kind"] == v["kind"])] + [v]
            meta.save_settings(s)
            return v

        def del_view():
            s = meta.settings()
            s["views"] = [x for x in s["views"] if not (x["name"] == body.get("name") and x["channel"] == body.get("channel") and x["kind"] == body.get("kind"))]
            meta.save_settings(s)
            return {"ok": True}

        routes = {
            "/api/settings/meta/add": lambda: meta.add_account(body["act"], body.get("name")),
            "/api/settings/meta/remove": lambda: meta.remove_account(body["act"]),
            "/api/settings/meta/rename": lambda: meta.rename_account(body["act"], body.get("name", "")),
            "/api/settings/meta/visible": meta.visible_accounts,
            "/api/settings/google/test": lambda: google_test(body["acct"]),
            "/api/settings/google/source": lambda: gapi.set_source(core.resolve(body["acct"]), body["source"]),
            "/api/settings/view/save": save_view,
            "/api/settings/view/delete": del_view,
            "/api/settings/currency": lambda: set_display_currency(body.get("currency")),
            "/api/settings/fx/refresh": lambda: {**fx.info(force=True), "display_currency": display_currency()},
            "/api/settings/health/check": lambda: {"summary": health.summary(health.check_all(save=True)), **(health.cached() or {})},
        }
        if self.path in routes:
            return self._run(routes[self.path])
        self._send(404, {"error": "not found"})

    def _meta_post(self, body):
        try:
            meta.use(body.get("acct"))
        except ValueError as ex:
            return self._send(400, {"error": str(ex)})
        def live_push():
            if not PUSH_LOCK.acquire(blocking=False):
                raise ValueError("a push is already running")
            try:
                return team_reviews.gated_push("ads", f"ads:{meta.acct()}", body, lambda only: meta.push(  # [team reviews]
                    False, bool(body.get("accept_spend")), bool(body.get("accept_permanent")), only), "Pushed live to Meta")
            finally:
                PUSH_LOCK.release()

        def no_editor():
            raise ValueError("Meta has no Editor: use Push live")

        routes = {
            "/api/live/preview": lambda: team_reviews.gated_preview("ads", f"ads:{meta.acct()}", body,
                                                                   lambda only: meta.preview(only=only)),
            "/api/live/push": live_push,
            "/api/live/undo": lambda: meta.undo(body["push_id"]),
            "/api/draft/add": lambda: meta.add(body["changes"], body.get("by", "you"), body.get("note")),
            "/api/draft/remove": lambda: {"removed": core.remove(meta.acct(), body["ids"])},
            "/api/draft/clear": lambda: {"removed": core.clear(meta.acct())},
            "/api/validate": lambda: meta.validate(),
            "/api/meta/refresh": lambda: {"fetched_at": meta.refresh_account()["fetched_at"]},
            "/api/meta/draft_preview": lambda: meta.draft_preview(body.get("ad_id"), body.get("placement", "facebook"),
                                                                  body.get("overrides"), body.get("draft_id")),
            "/api/meta/media": lambda: meta.store_media(body.get("filename"), body.get("data") or ""),
            "/api/push": no_editor,
        }
        if self.path in routes:
            return self._run(routes[self.path])
        self._send(404, {"error": "not found"})


def main():
    p = argparse.ArgumentParser(description="Marketing Studio")
    p.add_argument("--port", type=int, default=4830)
    p.add_argument("--no-open", action="store_true")
    a = p.parse_args()
    try:  # once per install: an older install keeps its built-in copy rules, a new one starts with none
        import team_settings  # noqa: PLC0415
        team_settings.first_run()
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    if datapolicy.install_call_log():
        print("STUDIO_CALL_LOG: counting outgoing calls at /api/data/calls")
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), H)
    # SoMe: Studio's own scheduler for Instagram/LinkedIn posts the user scheduled. The thread only publishes when the user
    # has turned "Let Studio on this Mac publish scheduled posts" on in SoMe settings (off by default).
    so = areas.AREAS.get("social")
    if so and hasattr(so, "start_scheduler"):
        try:
            so.start_scheduler()
        except Exception:  # noqa: BLE001 - the studio must start even if SoMe can't
            traceback.print_exc()
    try:
        team.start_listener()  # live updates from the team server (no-op when not in a team)
    except Exception:  # noqa: BLE001 - the studio must start even if the team server can't be reached
        traceback.print_exc()
    updater.start_background_check()  # [mac-app updater] daily check; no-op when not inside the .app
    url = f"http://localhost:{a.port}"
    print(f"Marketing Studio on {url}  (Ctrl+C to stop)")
    if not a.no_open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
