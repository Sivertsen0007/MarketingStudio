"""The shared calendar (every channel in one view) and the clash warnings shown when something is scheduled.

Entries come from what this Mac has, read locally (no extra calls to the team server):
  SoMe    every Studio post with a time (synced drafts: teammates' posts too), one entry per ticked channel, with its
          status (planned, review = approval needed, scheduled, publishing, published, ...).
  Email   campaigns on the email platform that are scheduled, sending or sent (the platform is shared, so this is the
          whole team's), plus marks in the synced collection "calendar" ({id: "email_<campaign id>", campaign_id, when,
          audience, subject, by}) that say who scheduled each one from Studio.
  SEO     new pages drafted with a publish time (publish_at / when), if any.
Each entry has an owner (the member who made it, in their team colour; "you" when solo).

Clash warnings (team_settings key calendar_limits; warnings only, nothing is ever blocked):
  per_day   more than max_per_channel_day items on one channel on one day
  gap       another item on the same channel within min_gap_hours
  link      the same link (UTMs and a trailing slash ignored) within same_link_days
  image     the same image within same_image_days
  topic     the same topic (the post's name or the email's subject) within same_topic_days
  audience  an email to the same list or segment within email_audience_days
Each warning names the other item, who scheduled it and when.
"""
import datetime as dt
import re
import urllib.parse

import draftstore
import team
import team_settings

CAL = "calendar"
IGNORED = ("cancelled", "deleted", "failed", "draft_cancelled")
DEFAULT_COLOUR = "#1a73e8"
URL_RE = re.compile(r"https?://[^\s<>\"')]+")


def _now():
    return dt.datetime.now(dt.timezone.utc)


def parse(v):
    if not v:
        return None
    if isinstance(v, dt.datetime):
        t = v
    else:
        s = re.sub(r"([+-]\d\d)(\d\d)$", r"\1:\2", str(v).strip().replace("Z", "+00:00"))
        try:
            t = dt.datetime.fromisoformat(s)
        except ValueError:
            try:
                t = dt.datetime.fromisoformat(s[:10])
            except ValueError:
                return None
    return t if t.tzinfo else t.astimezone()


def iso(t):
    return t.astimezone().isoformat(timespec="minutes")


# ---------- owners ----------

def _who():
    """-> (fn(author) -> {name, colour, me}, me)."""
    joined = team_settings.in_team()
    st = team.status() if joined else {}
    me = st.get("me") or {}
    my_name = me.get("name") or "You"
    my_colour = me.get("colour") or DEFAULT_COLOUR
    by_name = {m["name"]: m["colour"] for m in (team.members() or {}).values()} if joined else {}

    def owner(author):
        a = str(author or "").strip()
        if not a or a in ("you", "claude", "team sync") or a == my_name:
            return {"name": my_name, "colour": my_colour, "me": True}
        return {"name": a, "colour": by_name.get(a, "#888888"), "me": False}
    return owner, {"name": my_name, "colour": my_colour}


# ---------- normalising ----------

def norm_link(u):
    if not u:
        return ""
    try:
        p = urllib.parse.urlsplit(u.strip())
    except ValueError:
        return ""
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True) if not k.lower().startswith("utm_")]
    host = (p.hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    return f"{host}{p.path.rstrip('/')}" + ("?" + urllib.parse.urlencode(sorted(q)) if q else "")


def norm_topic(t):
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", str(t or "").lower())).strip()


# ---------- entries ----------

def _social(start, end, owner):
    try:
        from areas import _social_store as store  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return []
    root = store.data_dir()
    recs = {r["id"]: r for r in draftstore.items("social", root)}
    out = []
    for p in store.posts():
        when = (p.get("move") or {}).get("when") or p.get("when")
        t = parse(when)
        if not t or not start <= t < end:
            continue
        rec = recs.get(p["id"]) or {}
        who = owner(rec.get("author") or p.get("by"))
        link = p.get("link") or ""
        if not link:
            m = URL_RE.search(p.get("caption") or "")
            link = m.group(0) if m else ""
        images = [str(m.get("media_id") or m.get("url") or "") for m in p.get("media") or [] if isinstance(m, dict)]
        title = p.get("title") or (p.get("caption") or "")[:80]
        for ch in p.get("channels") or []:
            r = (p.get("results") or {}).get(ch) or {}
            st = r.get("status") or ""
            if st in ("", "not_scheduled", "cancelled"):
                st = "review" if p.get("approval") == "needed" else "planned"
            out.append({"key": f"social:{p['id']}:{ch}", "kind": "social", "id": p["id"], "channel": ch,
                        "when": iso(parse(r.get("scheduled_for")) or t), "title": title, "status": st, "owner": who,
                        "link": link, "images": [i for i in images if i], "topic": norm_topic(p.get("title")),
                        "audience": ""})
    return out


def _marks(root=None):
    return {r["id"]: {**r["data"], "_author": r.get("author")} for r in draftstore.items(CAL, root or team._root())}


def _email(start, end, owner):
    marks = _marks()
    out, seen = [], set()
    try:
        from areas import email as em  # noqa: PLC0415
        rows = em.adapter().list_campaigns()
    except Exception:  # noqa: BLE001 - no platform reachable: the marks alone
        rows = []
    for c in rows:
        if c.get("status") not in ("scheduled", "sending", "sent"):
            continue
        t = parse(c.get("scheduled_for") or c.get("sent_at"))
        if not t or not start <= t < end:
            continue
        cid = str(c.get("id"))
        seen.add(cid)
        mk = marks.get(f"email_{cid}") or {}
        who = owner(mk.get("_author") or mk.get("by")) if mk else {"name": "the email platform", "colour": "#888888",
                                                                  "me": False}
        out.append({"key": f"email:{cid}", "kind": "email", "id": cid, "channel": "email", "when": iso(t),
                    "title": c.get("subject") or c.get("name") or cid, "status": c["status"], "owner": who, "link": "",
                    "images": [], "topic": norm_topic(c.get("subject") or c.get("name")),
                    "audience": audience_key(c), "audience_name": c.get("segment_name") or c.get("list_name") or ""})
    for mid, mk in marks.items():
        cid = str(mk.get("campaign_id") or "")
        t = parse(mk.get("when"))
        if mk.get("kind") != "email" or not cid or cid in seen or not t or not start <= t < end:
            continue
        out.append({"key": f"email:{cid}", "kind": "email", "id": cid, "channel": "email", "when": iso(t),
                    "title": mk.get("subject") or cid, "status": "scheduled", "owner": owner(mk.get("_author") or mk.get("by")),
                    "link": "", "images": [], "topic": norm_topic(mk.get("subject")), "audience": mk.get("audience") or "",
                    "audience_name": mk.get("audience_name") or ""})
    return out


def _seo(start, end, owner):
    out = []
    try:
        recs = draftstore.items("seo:new_posts", _seo_root())
    except Exception:  # noqa: BLE001
        return out
    for r in recs:
        d = r["data"]
        t = parse(d.get("publish_at") or d.get("when"))
        if not t or not start <= t < end:
            continue
        out.append({"key": f"seo:{r['id']}", "kind": "seo", "id": r["id"], "channel": "blog", "when": iso(t),
                    "title": d.get("title") or d.get("slug") or r["id"], "status": d.get("status") or "planned",
                    "owner": owner(r.get("author")), "link": "", "images": [], "topic": norm_topic(d.get("title")),
                    "audience": ""})
    return out


def _seo_root():
    from areas import _seo_common as c  # noqa: PLC0415
    return c.data_dir()


def audience_key(c):
    if c.get("segment_id"):
        return f"segment:{c['segment_id']}"
    if c.get("list_id"):
        return f"list:{c['list_id']}"
    return ""


def entries(start, end, kinds=None):
    """Every entry between start and end (aware datetimes), sorted by time."""
    owner, _ = _who()
    out = []
    for kind, fn in (("social", _social), ("email", _email), ("seo", _seo)):
        if kinds and kind not in kinds:
            continue
        try:
            out += fn(start, end, owner)
        except Exception:  # noqa: BLE001 - one source failing leaves the others
            pass
    out.sort(key=lambda e: e["when"])
    return out


def calendar_list(start=None, end=None):
    """For the UI and the MCP. start / end: ISO dates or datetimes (default: this week and the next three)."""
    s = parse(start) if start else None
    if not s:
        n = _now().astimezone()
        s = (n - dt.timedelta(days=n.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    e = parse(end) if end else None
    e = e or s + dt.timedelta(days=28)
    if e <= s:
        raise ValueError("end must be after start")
    if (e - s).days > 400:
        raise ValueError("pick a range of at most 400 days")
    _, me = _who()
    rows = entries(s, e)
    people = {}
    for r in rows:
        people.setdefault(r["owner"]["name"], r["owner"]["colour"])
    if team_settings.in_team():
        for m in (team.members() or {}).values():
            people.setdefault(m["name"], m["colour"])
    return {"start": iso(s), "end": iso(e), "entries": rows, "me": me,
            "people": [{"name": k, "colour": v} for k, v in people.items()], "limits": team_settings.get("calendar_limits")}


# ---------- clash warnings ----------

LABEL = {"facebook": "Facebook", "instagram": "Instagram", "linkedin": "LinkedIn", "email": "Email", "blog": "Blog"}


def _fmt(t):
    return t.astimezone().strftime("%a %d %b %H:%M")


def _span(td):
    m = int(abs(td.total_seconds()) // 60)
    return f"{m // 60} h {m % 60} min" if m >= 60 else f"{m} min"


def clashes(cands, exclude_ids=()):
    """Warnings for candidate entries (same shape as entries(); kind, id, channel, when, link, images, topic,
    audience) against everything else on the calendar. -> [{rule, message, other}]."""
    if not cands:
        return []
    lim = team_settings.get("calendar_limits")
    days = max(lim["same_link_days"], lim["same_image_days"], lim["same_topic_days"], lim["email_audience_days"], 1) + 1
    times = [parse(c["when"]) for c in cands]
    others = [o for o in entries(min(times) - dt.timedelta(days=days), max(times) + dt.timedelta(days=days))
              if o["id"] not in set(map(str, exclude_ids)) and o["status"] not in IGNORED]
    out, seen = [], set()

    def warn(rule, o, msg, per_channel=False):
        k = (rule, o["kind"], o["id"], o["channel"] if per_channel else "")
        if k in seen:
            return
        seen.add(k)
        out.append({"rule": rule, "message": msg, "other": {x: o[x] for x in ("kind", "id", "channel", "title", "owner", "when", "status")}})

    for c, t in zip(cands, times):
        ch = LABEL.get(c["channel"], c["channel"])
        same = [o for o in others if o["channel"] == c["channel"]]
        day = [o for o in same if parse(o["when"]).astimezone().date() == t.astimezone().date()]
        if len(day) + 1 > lim["max_per_channel_day"]:
            o = day[0]
            names = "; ".join(f'"{x["title"][:50]}" by {x["owner"]["name"]} at {parse(x["when"]).astimezone():%H:%M}' for x in day[:4])
            warn("per_day", o, f"{ch}: {t.astimezone():%a %d %b} would have {len(day) + 1} items (the limit is "
                                 f"{lim['max_per_channel_day']} a day): {names}", True)
        for o in same:
            gap = parse(o["when"]) - t
            if abs(gap) < dt.timedelta(hours=lim["min_gap_hours"]):
                warn("gap", o, f'{ch}: "{o["title"][:60]}" by {o["owner"]["name"]} is at {_fmt(parse(o["when"]))}, '
                               f'{_span(gap)} from this one (keep {lim["min_gap_hours"]} h apart)', True)
        for o in others:
            apart = abs(parse(o["when"]) - t)
            where = f'"{o["title"][:60]}" by {o["owner"]["name"]} ({LABEL.get(o["channel"], o["channel"])}, {_fmt(parse(o["when"]))})'
            if c.get("link") and o.get("link") and norm_link(c["link"]) == norm_link(o["link"]) \
                    and apart <= dt.timedelta(days=lim["same_link_days"]):
                warn("link", o, f"The same link as {where}, within {lim['same_link_days']} days")
            if set(c.get("images") or []) & set(o.get("images") or []) and apart <= dt.timedelta(days=lim["same_image_days"]):
                warn("image", o, f"The same image as {where}, within {lim['same_image_days']} days")
            if c.get("topic") and c["topic"] == o.get("topic") and apart <= dt.timedelta(days=lim["same_topic_days"]):
                warn("topic", o, f"The same topic as {where}, within {lim['same_topic_days']} days")
            if c["kind"] == "email" and o["kind"] == "email" and c.get("audience") and c["audience"] == o.get("audience") \
                    and apart <= dt.timedelta(days=lim["email_audience_days"]):
                aud = o.get("audience_name") or c.get("audience_name") or c["audience"]
                warn("audience", o, f"The same audience ({aud}) gets {where}, within {lim['email_audience_days']} days")
    return out


def social_clashes(post, channels=None):
    """Warnings for scheduling a SoMe post (its time, the channels being scheduled)."""
    t = parse((post.get("move") or {}).get("when") or post.get("when"))
    if not t:
        return []
    link = post.get("link") or ""
    if not link:
        m = URL_RE.search(post.get("caption") or "")
        link = m.group(0) if m else ""
    images = [str(m.get("media_id") or m.get("url") or "") for m in post.get("media") or [] if isinstance(m, dict)]
    cands = [{"kind": "social", "id": post.get("id"), "channel": ch, "when": iso(t), "link": link,
              "images": [i for i in images if i], "topic": norm_topic(post.get("title")), "audience": ""}
             for ch in (channels or post.get("channels") or [])]
    return clashes(cands, exclude_ids=[post.get("id")])


def email_clashes(campaign, when):
    """Warnings for scheduling an email campaign ({id, subject, list_id / segment_id, ...}) at when."""
    t = parse(when)
    if not t:
        return []
    c = {"kind": "email", "id": str(campaign.get("id")), "channel": "email", "when": iso(t), "link": "", "images": [],
         "topic": norm_topic(campaign.get("subject") or campaign.get("name")), "audience": audience_key(campaign),
         "audience_name": campaign.get("segment_name") or campaign.get("list_name") or ""}
    return clashes([c], exclude_ids=[c["id"]])


def mark_email(campaign, when, by="you"):
    """Record (synced with the team) that this campaign was scheduled from Studio, so teammates see who did it."""
    cid = str(campaign.get("id"))
    iid = f"email_{re.sub(r'[^A-Za-z0-9_.:-]', '_', cid)}"[:200]
    data = {"id": iid, "kind": "email", "campaign_id": cid, "when": iso(parse(when)), "audience": audience_key(campaign),
            "audience_name": campaign.get("segment_name") or campaign.get("list_name") or "",
            "subject": campaign.get("subject") or campaign.get("name") or "", "by": by}
    with draftstore.tx(CAL, team._root(), by=by) as t:
        t.put(iid, data, by)
    return data


def unmark_email(cid):
    iid = f"email_{re.sub(r'[^A-Za-z0-9_.:-]', '_', str(cid))}"[:200]
    with draftstore.tx(CAL, team._root()) as t:
        t.delete(iid)
