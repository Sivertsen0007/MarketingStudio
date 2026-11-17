"""Team notifications on the Studio side (teamserver/PROTOCOL.md, "Notifications"). Only active when this Studio is in
a team; a solo user never calls any of this.

  Bell         notifications(): everything the server put on my bell (reviews for me, @mentions, my requests
               decided, my pending items changed by someone, claims on what I own, pushes in areas I follow), unread
               first, each with who did it and a readable label; read() marks them read. The top bar shows unread().
  Settings     prefs() / save_prefs(): what pings me, how (bell, email, a webhook) and when (instant, hourly, daily),
               Focus (until a time or until I turn it off) and Away (with a backup approver).
  Live         the server's SSE `notify` event bumps team_live's "notify" counter, which the page polls (ping).

The server does the rules and the sending (email, webhooks, digests, retries); Studio only shows and sets them.
"""
import contextlib

import team
import team_live

PREF_KEYS = ("rules", "follow", "bell", "email", "webhook", "daily_at", "tz_offset", "focus", "away")


def _api(method, path, body=None):
    return team_live._api(method, path, body)


def _decorate(n):
    who = team_live._member(n.get("actor")) if n.get("actor") is not None else {"id": None, "name": "", "colour": "#888888"}
    coll, iid = n.get("coll"), n.get("item_id")
    if coll == "review":
        label = f"Review {iid}"
    elif coll:
        label = ""
        with contextlib.suppress(Exception):
            label = team_live.label(coll, iid)
    else:
        label = ""
    return {"id": n.get("id"), "kind": str(n.get("kind") or ""), "text": str(n.get("text") or ""), "at": n.get("at"),
            "read": bool(n.get("read")), "area": str(n.get("area") or ""), "coll": coll, "item_id": iid,
            "data": n.get("data") if isinstance(n.get("data"), dict) else {}, "who": who, "label": label}


def notifications(unread=False, limit=50, before=None):
    """My bell: unread first, then newest first."""
    q = team_live._q(unread="1" if unread else None, limit=max(1, min(int(limit or 50), 200)),
                     before=int(before) if before else None)
    r = _api("GET", "/api/v1/notifications?" + q)
    out = [_decorate(n) for n in r.get("notifications") or [] if isinstance(n, dict)]
    out.sort(key=lambda n: (n["read"], -(n["id"] or 0)))
    return {"notifications": out, "unread": int(r.get("unread") or 0), "more": bool(r.get("more")),
            "next_before": r.get("next_before")}


def unread():
    """The bell's number. A server from before notifications: unread @mentions, as before."""
    try:
        return int(_api("GET", "/api/v1/notifications?unread=1&limit=1").get("unread") or 0)
    except team.TeamError as e:
        if e.status != 404:
            raise
    return int(team_live.mentions(True)["unread"])


def read(ids=None, every=False):
    body = {"all": True} if every else {"ids": [int(i) for i in ids or []]}
    r = _api("POST", "/api/v1/notifications/read", body)
    team_live._bump("notify")
    team_live._bump("mention")  # mention notifications mark their mentions read too
    return r


def _backups():
    """Members I could pick as my Away backup (not me), and whether they may approve reviews."""
    me = str((team_live._me() or {}).get("id"))
    out = []
    with contextlib.suppress(team.TeamError):
        for m in _api("GET", "/api/v1/members").get("members") or []:
            if not isinstance(m, dict) or str(m.get("id")) == me or not m.get("active", True):
                continue
            out.append({"id": m.get("id"), "name": str(m.get("name") or "")[:100], "role": str(m.get("role") or ""),
                        "may_approve": bool(m.get("approver")) or m.get("role") in ("owner", "admin")})
    return out


def prefs():
    r = _api("GET", "/api/v1/notify/prefs")
    r["backups"] = _backups()
    return r


def save_prefs(body):
    """Send only the known parts of the settings (the server checks each one)."""
    body = body if isinstance(body, dict) else {}
    out = _api("PUT", "/api/v1/notify/prefs", {k: body[k] for k in PREF_KEYS if k in body})
    out["backups"] = _backups()
    return out


def test(channel):
    return _api("POST", "/api/v1/notify/test", {"channel": channel})


def send_alert(text, data=None):
    """One of my own Ads alerts (alerts.py) to my own team notification channels (bell, and email / webhook per my
    prefs). The server only ever delivers it to me. -> "sent", "off" (my alert rule is off), "solo" (not in a team) or
    "unsupported" (a team server from before alerts)."""
    if not team.joined():
        return "solo"
    try:
        r = _api("POST", "/api/v1/notify/alert", {"text": str(text)[:300], "data": data if isinstance(data, dict) else {}})
    except team.TeamError as e:
        if e.status in (404, 405):
            return "unsupported"
        raise
    team_live._bump("notify")
    return "sent" if r.get("sent") else "off"


def on_event(kind, data):
    """team.py's stream listener: SSE `notify` -> the page reloads the bell."""
    if kind == "notify":
        team_live._bump("notify")


team.EVENT_HOOKS.append(on_event)


# ---------- HTTP routes (server.py: /api/team/notify/<name>) ----------

def get(path, q):
    n = team_live._name(path)
    routes = {
        "list": lambda: notifications(q.get("unread") in ("1", "true"), q.get("limit") or 50, q.get("before")),
        "unread": lambda: {"unread": unread()},
        "prefs": prefs,
    }
    if n not in routes:
        raise ValueError("no such team route")
    return routes[n]()


def post(path, body):
    n = team_live._name(path)
    routes = {
        "read": lambda: read(body.get("ids"), body.get("all") is True),
        "prefs": lambda: save_prefs(body),
        "test": lambda: test(body.get("channel")),
    }
    if n not in routes:
        raise ValueError("no such team route")
    return routes[n]()
