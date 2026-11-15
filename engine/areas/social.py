"""SoMe area of Marketing Studio: organic Facebook, Instagram and LinkedIn posts for Master Liveaboards.

See it: calendar (month / week) of Studio posts + what is live on the Page and Instagram, and a published grid with insights.
Build a draft: the post editor (you), or Claude through list_drafts() / add_draft() (drafts only).
Check it: copy rules, limits, links, media rules per channel, a phone preview per channel.
Make it real: Post now, Schedule, Cancel, Delete. These run ONLY from the user's own button in the UI (the POST routes
below that need "pressed": "user"); there is no MCP tool for them and Claude never calls them.

Test mode: SOCIAL_FAKE_PUBLISH=1 makes every publisher a FakePublisher and the media host a FakeHost (nothing leaves the
Mac). SOCIAL_DIR=<folder> keeps SoMe data apart (tests and the test server).
Scheduling: Facebook through Meta's own scheduling (10 min to 30 days ahead). Instagram and LinkedIn need the VPS publisher
job (social_publisher.py), which is OFF until the user approves it . Nothing is copied to the VPS while off.
"""
import datetime as dt
import json
import os
import threading
import time
import urllib.error
import urllib.request

from . import _social_checks as checks
from . import _social_plan as plan
from . import _social_publish as pub
from . import _social_store as store

LABEL = "SoMe"
KEY = "social"  # progress key: GET /api/progress?key=social
_live_lock = threading.Lock()
_links = {}


def test_mode():
    return os.environ.get("SOCIAL_FAKE_PUBLISH") == "1"


# ---------- connections (lazy, so the area loads even without Meta) ----------

def _meta_ads():
    import meta_ads  # noqa: PLC0415 - Studio's Meta module (media store, ads draft)
    return meta_ads


def _sm():
    from . import _social_meta  # noqa: PLC0415
    return _social_meta


_fakes = {}


def publishers():
    if test_mode():
        if not _fakes:
            _fakes.update({c: pub.FakePublisher(c) for c in store.CHANNELS})
        return _fakes
    from . import _social_linkedin as li  # noqa: PLC0415
    sm = _sm()
    return {"facebook": sm.FacebookPublisher(), "instagram": sm.InstagramPublisher(), "linkedin": li.LinkedInPublisher()}


_fake_host = pub.FakeHost()


def host():
    return _fake_host if test_mode() else pub.R2Host()


_shipper = None  # tests set a fake


VPS_PARKED = "VPS publisher: parked, not available yet. See the README"


def shipper():
    """The VPS runner is PARKED (not installed, not approved): Studio never ships anything to the VPS."""
    raise RuntimeError(VPS_PARKED)


def grace():
    """Minutes a queue item may still go out late: Studio's late-posting setting, at least 2 (the scheduler's round).
    It is stored on the item when the user presses Schedule, so Studio's scheduler and the VPS job use the same rule."""
    return max(2, int(store.settings()["late_minutes"] or 0))


def queue_mode():
    """Who publishes queue items: "local" (Studio's scheduler on this Mac, when its setting is on) or None.
    The VPS runner is parked, so it is never chosen (whatever the settings file says)."""
    return "local" if store.settings()["local_scheduler"] else None


def tracker(p):
    """track(channel, text, link) for build_job: UTMs on Facebook / LinkedIn links when the setting and the post allow it."""
    s = store.settings()
    camp = plan.campaign_slug(p.get("title")) if p.get("title") else s["utm_campaign"]

    def track(ch, text, link):
        if not p.get("utm", True) or not (s.get("utm") or {}).get(ch) or ch == "instagram":
            return text, link
        return plan.tracked_text(text, ch, camp), plan.add_utm(link, ch, camp)
    return track


# ---------- media + links for the checks ----------

def media_lookup(media_id):
    m = _meta_ads().media_info(media_id)
    return _meta_ads().MEDIA_DIR / m["file"], m["ext"], m["kind"]


def media_facts(post):
    out = []
    for m in post.get("media") or []:
        try:
            path, ext, kind = media_lookup(m["media_id"])
            out.append(checks.media_facts(path, kind, ext))
        except (ValueError, OSError):
            out.append({"kind": m.get("kind", "image"), "ext": "", "size": 0, "missing": True})
    return out


def link_check(url):
    """'' when the link answers 2xx/3xx, else the problem. Cached 10 minutes."""
    hit = _links.get(url)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    err = ""
    if not url.startswith(("http://", "https://")):
        err = f"link {url}: must start with https://"
    else:
        for method in ("HEAD", "GET"):
            req = urllib.request.Request(url, method=method, headers={"User-Agent": "Mozilla/5.0 (Marketing Studio link check)"})
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    err = "" if r.status < 400 else f"link {url} answers {r.status}"
                    break
            except urllib.error.HTTPError as e:
                err = f"link {url} answers {e.code}"
                if e.code not in (403, 405):
                    break
            except (urllib.error.URLError, OSError) as e:
                err = f"link {url} does not answer ({getattr(e, 'reason', e)})"
                break
    _links[url] = (time.time(), err)
    return err


def check(post, links=True):
    res = checks.check_post(post, store.caption_for, media_facts(post), link_check if links else None)
    missing = [i + 1 for i, f in enumerate(media_facts(post)) if f.get("missing")]
    if missing:
        res["problems"].append(f"media item(s) {', '.join(map(str, missing))} not found on this Mac: add the file again")
        res["ok"] = False
    return res


def _view(p, links=False):
    tr = tracker(p)
    final = {c: tr(c, "", p.get("link") or "")[1] for c in p.get("channels") or [] if c != "instagram" and p.get("link")}
    return {**p, "status": store.status(p), "check": check(p, links), "final_links": final}


# ---------- reading ----------

def _published(force=False):
    try:
        return _sm().published(0 if force else _sm().CACHE_TTL)
    except Exception as ex:  # noqa: BLE001 - no Meta: the calendar still shows Studio posts
        return {"posts": [], "scheduled": [], "errors": [str(ex)], "fetched_at": None}


def calendar_events(posts, live_rows, live_sched, start, end):
    """Pure: events between start and end (aware datetimes). One event per post and channel.
    -> {"events": [...], "unplanned": [Studio posts with no time]}"""
    ev, unplanned, studio_ids = [], [], set()
    for p in posts:
        for r in (p.get("results") or {}).values():
            if r.get("post_id"):
                studio_ids.add(r["post_id"])
        when = (p.get("move") or {}).get("when") or p.get("when")
        if not when:
            unplanned.append({"id": p["id"], "title": p.get("title") or (p.get("caption") or "")[:60], "channels": p.get("channels"),
                              "status": store.status(p)})
            continue
        t = store.parse_when(when)
        if not start <= t < end:
            continue
        for ch in p.get("channels") or []:
            r = (p.get("results") or {}).get(ch) or {}
            st = r.get("status") or "draft"
            st = "draft" if st in ("cancelled", "deleted") else st
            ev.append({"key": f"{p['id']}:{ch}", "post": p["id"], "channel": ch, "when": store.iso(t), "status": st,
                       "title": p.get("title") or (store.caption_for(p, ch) or "")[:80], "url": r.get("url", ""),
                       "moved": bool(p.get("move")), "orig_when": p.get("when") if p.get("move") else "", "source": "studio"})
    for rows, st in ((live_rows, "published"), (live_sched, "scheduled")):
        for x in rows or []:
            if x["id"] in studio_ids or not x.get("when"):
                continue
            t = store.parse_when(x["when"])
            if start <= t < end:
                ev.append({"key": f"live:{x['id']}", "post": None, "live_id": x["id"], "channel": x["channel"], "when": store.iso(t),
                           "status": st, "title": (x.get("text") or x.get("format") or "")[:80], "url": x.get("url", ""), "moved": False,
                           "source": "meta", "image": x.get("image", "")})
    ev.sort(key=lambda e: e["when"])
    return {"events": ev, "unplanned": unplanned}


def get_calendar(q):
    sync_queue()
    start = store.parse_when(q.get("start")) if q.get("start") else store.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = store.parse_when(q.get("end")) if q.get("end") else start + dt.timedelta(days=42)
    live = _published()
    out = calendar_events(store.posts(), live["posts"], live.get("scheduled"), start, end)
    out["errors"] = live.get("errors", [])
    return out


def get_state(q):
    s = store.settings()
    try:
        meta = _sm().info()
        meta_err = ""
    except Exception as ex:  # noqa: BLE001
        meta, meta_err = {}, str(ex)
    from . import _social_linkedin as li  # noqa: PLC0415
    sync_queue()
    return {"test_mode": test_mode(), "settings": {k: s[k] for k in ("vps_job", "local_scheduler", "late_minutes", "slots", "utm", "utm_campaign", "competitors")},
            "team_fields": _team_fields(),
            "vps_installed": False, "vps_parked": VPS_PARKED, "queue_mode": queue_mode(),
            "scheduler_running": bool(_sched.get("thread") and _sched["thread"].is_alive()), "last_tick": _sched.get("last"),
            "meta": meta, "meta_error": meta_err, "linkedin_connected": li.connected(),
            "posts": [_view(p) for p in sorted(store.posts(), key=lambda p: p.get("when") or p["updated"], reverse=True)]}


def get_post(q):
    return _view(store.get(q["id"]), links=q.get("links") == "1")


def get_published(q):
    live = _published(q.get("refresh") == "1")
    rows = live["posts"]
    if q.get("channel") in ("facebook", "instagram"):
        rows = [r for r in rows if r["channel"] == q["channel"]]
    if q.get("days"):
        cut = store.now() - dt.timedelta(days=int(q["days"]))
        rows = [r for r in rows if store.parse_when(r["when"]) >= cut]
    by_live = {}
    for p in store.posts():
        for r in (p.get("results") or {}).values():
            if r.get("post_id"):
                by_live[r["post_id"]] = p["id"]
    return {"rows": [{**r, "studio_post": by_live.get(r["id"])} for r in rows], "fetched_at": live.get("fetched_at"), "errors": live.get("errors", [])}


def get_adsets(q):
    m = _meta_ads()
    m.use("meta")
    d = m.load()
    camps = {c["id"]: c["name"] for c in d["campaigns"]}
    return [{"id": a["id"], "name": a["name"], "campaign": camps.get(a["campaign_id"], ""), "status": a.get("effective_status") or a.get("status")}
            for a in d["adsets"] if (a.get("effective_status") or a.get("status")) in ("ACTIVE", "PAUSED", "CAMPAIGN_PAUSED")]


def get_media(q):
    m = _meta_ads()
    return [{"media_id": k, "name": v["name"], "kind": v["kind"], "size": v["size"], "stored": v.get("stored")}
            for k, v in m._media_index().items() if v.get("source") == "social"]


GET = {"state": get_state, "calendar": get_calendar, "post": get_post, "published": get_published, "adsets": get_adsets, "media": get_media,
       "slot": lambda q: get_slot(q), "quick_times": lambda q: get_quick_times(q), "heatmap": lambda q: get_heatmap(q),
       "library": lambda q: get_library(q), "hashtags": lambda q: get_hashtags(q), "feed": lambda q: get_feed(q),
       "analytics": lambda q: get_analytics(q), "competitors": lambda q: get_competitors(q)}


# ---------- drafts (you and Claude) ----------

CHECK_FIRST = "Check this channel first (Check again)"


def _checking(p, chs=None):
    """Channels of the post that are Needs checking (they may or may not be live)."""
    return [c for c, r in (p.get("results") or {}).items() if r.get("status") == "needs_checking" and (chs is None or c in chs)]


def _block_checking(p, chs=None):
    bad = _checking(p, chs)
    if bad:
        raise ValueError(f"{CHECK_FIRST}: " + ", ".join(store.CHANNEL_LABEL[c] for c in bad))


def _editable(p):
    _block_checking(p)
    busy = [c for c, r in (p.get("results") or {}).items() if r.get("status") in ("scheduled", "publishing")]
    if busy:
        raise ValueError("this post is scheduled on " + ", ".join(store.CHANNEL_LABEL[c] for c in busy) + ": cancel it first to edit it")


def save(body):
    f = dict(body.get("post") or body)
    top, inner = body.get("id"), (body.get("post") or {}).get("id")
    if top and inner and top != inner:
        raise ValueError("the post id is given twice and they differ")
    pid = top or inner
    if pid:
        f["id"] = pid
    old = store.get(pid) if pid else {}
    if old:
        _editable(old)
    if f.get("approval") == "approved":  # only approve() approves; keep what the post had
        f["approval"] = old.get("approval", "needed") if old.get("approval") else "needed"
    return _view(store.save(f, body.get("by", "you")))


def list_drafts():
    """For the MCP (lead adds the tool): every Studio post with status and checks. Read-only."""
    return [{"id": p["id"], "title": p.get("title"), "channels": p.get("channels"), "when": p.get("when"), "status": store.status(p),
             "caption": p.get("caption"), "captions": p.get("captions"), "problems": check(p, links=False)["problems"]} for p in store.posts()]


def add_draft(change, by="claude"):
    """For the MCP: create a draft post (no id) or change a draft (id). Never publishes or schedules; posts that are
    scheduled or published can't be changed this way. change: the post fields (see _social_store)."""
    change = dict(change or {})
    for k in ("results", "move", "approved_by", "approved_at"):
        change.pop(k, None)
    ap = change.pop("approval", None)
    if ap in ("needed", "needs_approval"):  # Claude may ask for approval, never give it or remove it
        change["approval"] = "needed"
    old = store.get(change["id"]) if change.get("id") else None
    if old:
        if store.status(old) != "draft":
            raise ValueError("only draft posts can be changed")
        if old.get("approval") == "approved":  # changed after approval: it needs approving again
            change["approval"] = "needed"
    p = store.save(change, by)
    if old and change.get("approval") == "needed" and old.get("approved_by"):
        p = store.update(p["id"], lambda x: (x.pop("approved_by", None), x.pop("approved_at", None)))
    return {"id": p["id"], "status": store.status(p), "check": check(p, links=False)}


def delete_draft(body):
    store.remove(body["id"])
    return {"ok": True}


VPS_NO_MOVE = "Posts queued on the VPS can't be moved. Cancel it, then schedule it again."


def _on_vps(p):
    return [c for c, r in (p.get("results") or {}).items() if r.get("status") == "scheduled" and r.get("via") == "vps"]


def move(body):
    """A drag in the calendar: kept as a pending move until confirmed. Not for posts queued on the VPS."""
    p0 = store.get(body["id"])
    _block_checking(p0)
    if _on_vps(p0):
        raise ValueError(VPS_NO_MOVE)
    when = store.iso(store.parse_when(body["when"]))
    return _view(store.update(body["id"], lambda p: p.__setitem__("move", {"when": when, "at": store.iso(store.now())})))


def discard_move(body):
    return _view(store.update(body["id"], lambda p: p.__setitem__("move", None)))


def media_upload(body):
    """Store a file for a post in Studio's local media store (nothing goes anywhere). New files are marked
    source "social" in the media index so the Ads media list can leave them out; a file Ads already had stays as it is."""
    m = _meta_ads()
    before = set(m._media_index())
    r = m.store_media(body.get("filename"), body.get("data") or "")
    if r["media_id"] not in before:
        m._media_save(lambda idx: idx[r["media_id"]].__setitem__("source", "social"))
    return r


USERNAME = __import__("re").compile(r"^[A-Za-z0-9._]{1,30}$")


def _team_fields():
    """SoMe settings the team has set: shown as "Set by your team" and read-only here."""
    try:
        import team_settings  # noqa: PLC0415
        return team_settings.team_fields("social")
    except Exception:  # noqa: BLE001
        return []


def _team_check(body):
    """Fields the team has set (Calendar > Team settings) can't be changed here."""
    try:
        import team_settings  # noqa: PLC0415
    except ImportError:
        return
    team_settings.check_local_edit("social", [k for k in body if k in team_settings.SOCIAL_FIELDS])


def save_settings(body):
    """Only the keys given are changed. Slots are checked; the VPS job can only be ticked once it is installed."""
    s = store.settings()
    ch = {}
    _team_check(body)
    if body.get("vps_job"):
        raise ValueError(VPS_PARKED)
    if "local_scheduler" in body:
        ch["local_scheduler"] = bool(body["local_scheduler"])
    if "late_minutes" in body:
        n = int(body["late_minutes"] or 0)
        if not 0 <= n <= 240:
            raise ValueError("late posting: 0 to 240 minutes")
        ch["late_minutes"] = n
    if "slots" in body:
        ch["slots"] = {c: plan.fmt_slots(plan.parse_slots(v)) for c, v in (body["slots"] or {}).items() if c in store.CHANNELS}
    if "utm" in body:
        ch["utm"] = {c: bool(v) for c, v in (body["utm"] or {}).items() if c in ("facebook", "linkedin")}
    if "utm_campaign" in body:
        ch["utm_campaign"] = plan.campaign_slug(body["utm_campaign"])
    if "competitors" in body:
        names = [str(x).strip().lstrip("@") for x in body["competitors"] or [] if str(x).strip()]
        bad = [x for x in names if not USERNAME.match(x)]
        if bad:
            raise ValueError("not an Instagram username: " + ", ".join(bad))
        ch["competitors"] = list(dict.fromkeys(names))[:20]
    return store.save_settings(ch)


# ---------- live actions: the user's own button only ----------

def _pressed(body):
    if body.get("pressed") != "user":
        raise PermissionError("this runs only from your own button in Studio")


def _reporter(chs):
    steps = {c: 0 for c in chs}

    def report(ch, step, msg):
        lab = store.CHANNEL_LABEL[ch]
        if step == "failed":
            import progress  # noqa: PLC0415
            progress.mark(KEY, ch, "failed")
            progress.phase(KEY, "sending", f"{lab}: {msg}")
            return
        import progress  # noqa: PLC0415
        n = {"upload": 1, "create": 2, "done": 3}[step]
        if n > steps[ch]:
            progress.sent(KEY, ch, n - steps[ch])
            steps[ch] = n
        progress.phase(KEY, "sending", f"{lab}: {msg}")
    return report


def _start(title, chs, what):
    import progress  # noqa: PLC0415
    progress.start(KEY, "push", [(c, f"{store.CHANNEL_LABEL[c]}: {what}", 3) for c in chs], "sending", title)


def _finish(ok, msg):
    import progress  # noqa: PLC0415
    progress.finish(KEY, ok, msg)


def _jobs(p, chs):
    jobs = {c: pub.build_job(p, c, media_lookup, tracker(p)) for c in chs}
    if "instagram" in jobs:
        try:
            pub.ensure_public(jobs["instagram"], host())
        except Exception as ex:  # noqa: BLE001
            raise RuntimeError(f"could not put the files on media.mlmediahub.com for Instagram: {ex}") from None
        urls = {m["media_id"]: m["public_url"] for m in jobs["instagram"]["media"]}
        store.update(p["id"], lambda x: [m.__setitem__("public_url", urls.get(m["media_id"], m.get("public_url", ""))) for m in x["media"]])
    return jobs


def _save_results(post_id, res, extra=None):
    def upd(p):
        p.setdefault("results", {})
        for c, r in res.items():
            p["results"][c] = r
        if extra:
            extra(p)
    return store.update(post_id, upd)


def _message(res, verb):
    ok = [store.CHANNEL_LABEL[c] for c, r in res.items() if r["status"] not in ("failed",) and not r.get("error")]
    bad = [f"{store.CHANNEL_LABEL[c]}: {r['error']}" for c, r in res.items() if r.get("error")]
    return (f"{verb} on {', '.join(ok)}." if ok else "") + (" Not done: " + "; ".join(bad) if bad else "")


def _guard(fn):
    if not _live_lock.acquire(blocking=False):
        raise ValueError("another post is being sent right now")
    try:
        return fn()
    finally:
        _live_lock.release()


UNSURE = ("may or may not", "no answer", "timed out", "timeout")


def retry_lookup(p, chs):
    """Before posting again on a channel whose last try failed: look it up on the channel (read-only).
    -> (found {channel: result marked published}, unsure [channels]). Unsure = the lookup could not run, or found
    nothing although the last error says the post may have gone through (Meta can take a moment to list it)."""
    found, unsure = {}, []
    for ch in chs:
        prev = (p.get("results") or {}).get(ch) or {}
        if prev.get("status") != "failed":
            continue
        since = store.parse_when(prev.get("at")) or store.now() - dt.timedelta(hours=1)
        try:
            hit = publishers()[ch].find_recent(pub.build_job(p, ch, media_lookup, tracker(p)), since)
        except Exception:  # noqa: BLE001 - can't look: let the user decide
            unsure.append(ch)
            continue
        if hit:
            found[ch] = {"status": "published", "via": "found", "error": "", "at": store.iso(store.now()),
                         "warning": "found live after the earlier error, so it was not posted again", **hit}
        elif any(s in (prev.get("error") or "").lower() for s in UNSURE):
            unsure.append(ch)
    return found, unsure


def _team(body, what, fn):
    """[team reviews] In a team: an approval when the team requires one for SoMe, a why note, a feed event."""
    import team_reviews  # noqa: PLC0415
    return team_reviews.single_action("social", "social", str(body["id"]), body, what, fn)


def _team_recheck(body, p):
    """[team reviews] Still the approved post, now that it is read under the lock?"""
    if not body.get("_team_expect"):
        return None
    import team_reviews  # noqa: PLC0415
    why = team_reviews.recheck(body, "social", str(p["id"]), p)
    return {"ok": False, "needs_team_approval": True, "message": why, "post": _view(p)} if why else None


def post_now(body):
    """Publish now on the post's ticked channels (minus ones already published). User's button only."""
    _pressed(body)

    def go():
        p = store.get(body["id"])
        _block_checking(p, body.get("channels") or p["channels"])
        stale = _team_recheck(body, p)
        if stale:
            return stale
        chs = [c for c in (body.get("channels") or p["channels"]) if c in p["channels"]
               and ((p.get("results") or {}).get(c) or {}).get("status") not in ("published", "scheduled", "publishing")]
        if not chs:
            raise ValueError("nothing to post: every ticked channel is already published or scheduled")
        if "linkedin" in chs and not test_mode():
            from . import _social_linkedin as li  # noqa: PLC0415
            if not li.connected():
                raise ValueError("LinkedIn is not connected: untick LinkedIn or connect it first (SoMe settings)")
        if approval_needed(p):
            return _needs_approval(p)
        c = check(p)
        if not c["ok"]:
            return {"ok": False, "message": "Not posted: fix the problems first.", "problems": c["problems"]}
        found, unsure = retry_lookup(p, chs)
        if found:
            p = _save_results(p["id"], found)
            chs = [x for x in chs if x not in found]
        if unsure and not body.get("confirm_repost"):
            names = ", ".join(store.CHANNEL_LABEL[x] for x in unsure)
            return {"ok": False, "needs_confirm": unsure, "results": found, "post": _view(p),
                    "message": f"It may already be live on {names}: the last try failed in a way that can still have posted. "
                               "Check the channel. Post again anyway?"}
        if not chs:
            return {"ok": True, "message": _message(found, "Already live (found after the earlier error, not posted again)"),
                    "results": found, "post": _view(p)}
        _start("Posting" + (" (test mode: nothing leaves the Mac)" if test_mode() else ""), chs, "Uploading media, Creating post, Published")
        res = {}
        try:
            jobs = _jobs(p, chs)
            res = pub.run("publish", jobs, publishers(), _reporter(chs))
        finally:
            ok = bool(res) and all(not r.get("error") for r in res.values())
            _finish(ok, _message(res, "Published") if res else "Stopped with an error")
        stamp = store.iso(store.now())
        p = _save_results(p["id"], res, lambda x: x.__setitem__("when", x.get("when") or stamp))  # shows on the calendar when it went out
        res = {**found, **res}
        return {"ok": ok, "message": _message(res, "Published"), "results": res, "post": _view(p)}
    return _team(body, "Posted", lambda: _guard(go))


def schedule(body):
    """Schedule at the post's time: Facebook through Meta (10 min to 30 days), Instagram / LinkedIn (and Facebook outside
    Meta's window) through the VPS job when it is installed. Refuses as a whole when any ticked channel can't."""
    _pressed(body)

    def go():
        p = store.get(body["id"])
        _block_checking(p, body.get("channels") or p["channels"])
        stale = _team_recheck(body, p)
        if stale:
            return stale
        when = store.parse_when(p.get("when"))
        s = store.settings()
        chs = [c for c in (body.get("channels") or p["channels"]) if c in p["channels"]
               and ((p.get("results") or {}).get(c) or {}).get("status") not in ("published", "scheduled", "publishing")]
        if not chs:
            raise ValueError("nothing to schedule")
        if approval_needed(p):
            return _needs_approval(p)
        mode = queue_mode()
        routes, probs = {}, []
        for c in chs:
            r, pr = checks.schedule_route(c, when, store.now(), mode is not None)
            routes[c] = r
            probs += [f"{store.CHANNEL_LABEL[c]}: {x}" if not x.startswith(store.CHANNEL_LABEL[c]) else x for x in pr]
        c = check(p)
        if probs or not c["ok"]:
            return {"ok": False, "message": "Not scheduled.", "problems": probs + c["problems"],
                    "can_fb_only": "facebook" in chs and routes.get("facebook") is not None and len(chs) > 1}
        clash = _clashes(p, chs)  # warnings only (shared calendar); never stops the schedule
        _start("Scheduling" + (" (test mode)" if test_mode() else ""), chs, "Uploading media, Scheduling, Scheduled")
        res = {}
        rep = _reporter(chs)
        try:
            jobs = _jobs(p, chs)
            meta = {k: v for k, v in jobs.items() if routes[k] == "meta"}
            if meta:
                res.update(pub.run("schedule", meta, publishers(), rep, when=when))
            for ch in [k for k in jobs if routes[k] == "queue"]:
                it = None
                try:
                    rep(ch, "upload", "Adding it to Studio's queue on this Mac")
                    it = store.queue_add({"post_id": p["id"], "channel": ch, "due": store.iso(when), "payload": jobs[ch], "runner": "local",
                                          "max_late": grace()})  # only Studio's scheduler on this Mac (the VPS runner is parked)
                    rep(ch, "done", f"Queued for {when:%d %B %Y %H:%M}")
                    res[ch] = {"status": "scheduled", "via": "local", "queue_id": it["id"], "scheduled_for": store.iso(when), "error": "",
                               "at": store.iso(store.now()), "post_id": "", "url": ""}
                except Exception as ex:  # noqa: BLE001
                    if it:
                        store.queue_set(it["id"], status="cancelled", result={"error": str(ex)})
                    res[ch] = {"status": "failed", "error": str(ex)[:300], "at": store.iso(store.now())}
                    rep(ch, "failed", str(ex)[:300])
        finally:
            ok = bool(res) and all(not r.get("error") for r in res.values())
            _finish(ok, _message(res, "Scheduled") if res else "Stopped with an error")
        p = _save_results(p["id"], res)
        return {"ok": ok, "message": _message(res, "Scheduled"), "results": res, "post": _view(p), "clashes": clash}
    return _team(body, "Scheduled a post", lambda: _guard(go))


def _clashes(p, chs):
    try:
        import shared_calendar  # noqa: PLC0415
        return shared_calendar.social_clashes(p, chs)
    except Exception:  # noqa: BLE001 - the calendar must never stop a schedule
        return []


def cancel(body):
    """Cancel a scheduled channel (Meta: the scheduled post is removed; VPS: the queue item is cancelled)."""
    _pressed(body)

    def go():
        p = store.get(body["id"])
        res_all = p.get("results") or {}
        chs = [c for c in (body.get("channels") or p["channels"]) if (res_all.get(c) or {}).get("status") == "scheduled"]
        if not chs:
            raise ValueError("nothing is scheduled")
        _start("Cancelling", chs, "Cancel")
        out = {}
        rep = _reporter(chs)
        try:
            meta = {c: res_all[c] for c in chs if res_all[c].get("via") == "meta"}
            if meta:
                out.update(pub.run("cancel", None, publishers(), rep, results=meta))
            for c in [c for c in chs if res_all[c].get("via") == "vps"]:  # parked runner: Studio can't reach it
                out[c] = {**res_all[c], "error": f"{VPS_PARKED}. Check the channel by hand."}
                rep(c, "failed", out[c]["error"])
            for c in [c for c in chs if res_all[c].get("via") == "local"]:
                try:
                    r = store.queue_cancel(p["id"], c)  # under the queue lock: either this wins, or the publisher already took it
                    if r["busy"]:
                        raise RuntimeError("too late to cancel: it is being posted now. Check the channel in a minute")
                    if not r["cancelled"] and res_all[c].get("via") == "local":
                        raise RuntimeError("nothing left to cancel: it has already gone out or finished (see the post's status)")
                    out[c] = {**res_all[c], "status": "cancelled", "at": store.iso(store.now())}
                    rep(c, "done", "Cancelled")
                except Exception as ex:  # noqa: BLE001
                    out[c] = {**res_all[c], "error": str(ex)[:300]}
                    rep(c, "failed", str(ex)[:300])
        finally:
            ok = bool(out) and all(not r.get("error") for r in out.values())
            _finish(ok, _message(out, "Cancelled") if out else "Stopped with an error")
        # a refused cancel leaves the post as it was; cancelled and needs-checking channels are shown as they are
        p = _save_results(p["id"], {c: r for c, r in out.items() if not r.get("error") or r.get("status") == "needs_checking"})
        sync_queue()
        p = store.get(p["id"])
        return {"ok": ok, "message": _message(out, "Cancelled"), "results": out, "post": _view(p)}
    return _guard(go)


def delete_post(body):
    """Delete a published post on Facebook or LinkedIn (Instagram: not through the API)."""
    _pressed(body)

    def go():
        p = store.get(body["id"])
        res_all = p.get("results") or {}
        chs = [c for c in (body.get("channels") or []) if (res_all.get(c) or {}).get("status") == "published"]
        if not chs:
            raise ValueError("pick a published channel to delete")
        if "instagram" in chs:
            raise ValueError("Instagram posts can't be deleted from Studio: delete it in the Instagram app")
        _start("Deleting", chs, "Delete")
        out = {}
        try:
            out = pub.run("delete", None, publishers(), _reporter(chs), results={c: res_all[c] for c in chs})
        finally:
            ok = bool(out) and all(r.get("status") == "deleted" for r in out.values())
            _finish(ok, _message(out, "Deleted") if out else "Stopped with an error")
        p = _save_results(p["id"], out)
        return {"ok": ok, "message": _message(out, "Deleted"), "results": out, "post": _view(p)}
    return _guard(go)


def check_again(body):
    """Check again on a Needs checking channel (any runner): the only way it is cleared by hand.
    Read-only look-up on the channel: found -> published; not found -> failed (Post now works again then);
    the look-up can't run, or a publisher round is still busy -> stays Needs checking."""
    p = store.get(body["id"])
    ch = body["channel"]
    r = (p.get("results") or {}).get(ch) or {}
    if r.get("status") != "needs_checking":
        raise ValueError("this channel doesn't need checking")
    at = store.now()
    note = (" The VPS item, if there is one, is not managed by Studio any more (the VPS runner is parked)."
            if r.get("via") == "vps" else "")
    it = next((x for x in store.queue() if x["id"] == r.get("queue_id")), None)
    if _publisher_busy():
        return {"ok": False, "message": "A publisher round is running: check again in a minute.", "post": _view(p)}
    job = (it or {}).get("payload") or pub.build_job(p, ch, media_lookup, tracker(p))
    since = store.parse_when((it or {}).get("claimed_at") or r.get("at")) or at - dt.timedelta(hours=1)
    try:
        hit = publishers()[ch].find_recent(job, since - dt.timedelta(minutes=5))
    except Exception as ex:  # noqa: BLE001
        return {"ok": False, "message": f"Still needs checking: the look-up on the channel failed ({str(ex)[:150]}).", "post": _view(p)}
    if hit:
        new = {**r, "status": "published", "error": "", "at": store.iso(at), "warning": "found live on the channel", **hit}
    else:
        new = {**r, "status": "failed", "at": store.iso(at),
               "error": "Not found on the channel, so it was not posted. Use Post now if you still want it."}
    if it:
        store.queue_set(it["id"], status=new["status"], result={k: new.get(k) for k in ("post_id", "url", "error", "at")}, synced=True)
    p = _save_results(p["id"], {ch: new})
    return {"ok": True, "message": f"{store.CHANNEL_LABEL[ch]}: {new['status']}.{note}", "post": _view(p)}


def fmt_when(v):
    t = store.parse_when(v) if v else None
    return f"{t:%d %B %Y %H:%M}" if t else "the old time"


def _move_queued(p, ch, r, when):
    """Move one channel queued for Studio's scheduler on this Mac, as a transaction: build the new item first, then swap
    it in for the old one under the queue lock. If any step fails, the old schedule stays as it was. -> the new result.
    (Posts queued on the VPS are never moved: cancel and schedule again.)
    Only the time changes: the queued payload is what was checked (and, in a team, approved) when it was scheduled, so
    it is kept as it is and never rebuilt from the post, which may have taken in other changes since."""
    old = next((x for x in store.queue() if x.get("id") == r.get("queue_id")), None)
    if not old or old.get("status") != "scheduled" or not old.get("payload"):
        raise RuntimeError("too late to move: it is being posted now" if (old or {}).get("status") == "publishing"
                           else "nothing left to move: it has already gone out or finished")
    payload = old["payload"]
    new = store.queue_new({"post_id": p["id"], "channel": ch, "due": store.iso(when), "payload": payload, "runner": "local",
                           "max_late": grace()})
    it, st = store.queue_replace(r.get("queue_id", ""), new)
    if not it:
        raise RuntimeError("too late to move: it is being posted now" if st == "publishing"
                           else "nothing left to move: it has already gone out or finished")
    return {**r, "queue_id": it["id"], "scheduled_for": store.iso(when), "error": ""}


def confirm_move(body):
    """Make a calendar drag real. Drafts: just the new time. Scheduled channels are moved too (Meta: new publish time;
    VPS: the queue item's time), so this is the user's button."""
    _pressed(body)

    def go():
        p = store.get(body["id"])
        if not p.get("move"):
            raise ValueError("nothing to confirm")
        if _checking(p):
            m = f"{CHECK_FIRST}: " + ", ".join(store.CHANNEL_LABEL[c] for c in _checking(p))
            return {"ok": False, "message": m, "problems": [m]}
        if _on_vps(p):
            return {"ok": False, "message": VPS_NO_MOVE, "problems": [VPS_NO_MOVE]}
        when = store.parse_when(p["move"]["when"])
        res_all = p.get("results") or {}
        sched = {c: r for c, r in res_all.items() if r.get("status") == "scheduled"}
        out = {}
        if sched:
            s = store.settings()
            probs = []
            for c, r in sched.items():
                route, pr = checks.schedule_route(c, when, store.now(), queue_mode() is not None)
                if r.get("via") == "meta" and route != "meta":
                    pr = pr or ["the new time is outside Facebook's 10 minutes to 30 days"]
                probs += pr
            if probs:
                return {"ok": False, "message": "Not moved.", "problems": probs}
            _start("Moving the scheduled post", list(sched), "Move")
            try:
                meta = {c: r for c, r in sched.items() if r.get("via") == "meta"}
                if meta:
                    out.update(pub.run("reschedule", None, publishers(), _reporter(list(sched)), when=when, results=meta))
                for c, r in sched.items():
                    if r.get("via") != "local":
                        continue
                    try:
                        out[c] = _move_queued(p, c, r, when)
                        _reporter([c])(c, "done", "Moved")
                    except Exception as ex:  # noqa: BLE001 - the old schedule is kept: say so
                        out[c] = {**r, "error": f"not moved, still scheduled for {fmt_when(r.get('scheduled_for'))}: {str(ex)[:250]}"}
            finally:
                _finish(all(not r.get("error") for r in out.values()), _message(out, "Moved") if out else "Stopped")
            if any(r.get("error") for r in out.values()):  # keep what did move, keep the old schedule where it didn't
                p = _save_results(p["id"], out)
                return {"ok": False, "message": _message(out, "Moved"), "results": out, "post": _view(p)}
        p = _save_results(p["id"], out, lambda x: (x.__setitem__("when", store.iso(when)), x.__setitem__("move", None)))
        return {"ok": True, "message": f"Moved to {when:%d %B %Y %H:%M}.", "post": _view(p)}
    return _guard(go)


# ---------- approvals ----------

def _needs_approval(p):
    return {"ok": False, "needs_approval": True, "note": p.get("note", ""),
            "message": "This post is marked Needs approval: approve it first."}


def approval_needed(p):
    """An approval only counts when the user gave it with their own button (approved_by "you")."""
    return p.get("approval") == "needed" or (p.get("approval") == "approved" and p.get("approved_by") != "you")


def approve(body):
    """One click for a single user: the post may now be scheduled or posted. Only the user's own button (pressed)."""
    _pressed(body)

    def upd(p):
        p.update(approval="approved", approved_by="you", approved_at=store.iso(store.now()))
    return _view(store.update(body["id"], upd))


# ---------- the queue, Studio's scheduler ----------

_sched = {}


STALE_MIN = 10  # a queue item still "publishing" after this long: the publisher stopped mid-post


def _publisher_busy():
    """Is a publisher round running right now (Studio's scheduler or the job, same lock file)?"""
    import fcntl  # noqa: PLC0415
    with open(store.data_dir() / "social_publisher.lock", "w") as lk:
        try:
            fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(lk, fcntl.LOCK_UN)
    return False


def _resolve_stale(x, at):
    """An item left at publishing: look it up on the channel (read-only). Found -> published; else needs_checking."""
    since = store.parse_when(x.get("claimed_at")) or at - dt.timedelta(hours=1)
    try:
        hit = publishers()[x["channel"]].find_recent(x["payload"], since)
        why = "it was not found on the channel"
    except Exception as ex:  # noqa: BLE001
        hit, why = None, f"the look-up on the channel failed ({str(ex)[:120]})"
    if hit:
        store.queue_set(x["id"], status="published", result={**hit, "at": store.iso(at),
                                                             "warning": "found live after the publisher stopped mid-post"})
    else:
        store.queue_set(x["id"], status="needs_checking", result={"at": store.iso(at), "error":
                        f"Needs checking: the publisher stopped while posting this and {why}. It may or may not have been posted: "
                        "check the channel, then use Post now (it asks before posting again)"})


def sync_queue(at=None):
    """Copy finished queue items (published / failed / missed / needs_checking) back onto their posts, once.
    Items stuck at publishing for more than STALE_MIN minutes are resolved first (see _resolve_stale).
    A result only lands on the post when the post still points at that queue item (a moved post keeps its new item)."""
    at = at or store.now()
    if not _publisher_busy():
        for x in store.queue():
            c = store.parse_when(x.get("claimed_at")) if x["status"] == "publishing" else None
            if c and (at - c).total_seconds() > STALE_MIN * 60:
                _resolve_stale(x, at)
    done = [x for x in store.queue() if x["status"] in ("published", "failed", "missed", "needs_checking") and not x.get("synced")]
    for x in done:
        r = x.get("result") or {}
        res = {"status": x["status"], "via": x.get("runner", "vps"), "post_id": r.get("post_id", ""), "url": r.get("url", ""),
               "error": r.get("error", ""), "warning": r.get("warning", ""), "at": r.get("at") or store.iso(store.now()),
               "scheduled_for": x.get("due"), "queue_id": x["id"]}
        try:
            cur = (store.get(x["post_id"]).get("results") or {}).get(x["channel"]) or {}
            if not cur.get("queue_id") or cur["queue_id"] == x["id"]:
                _save_results(x["post_id"], {x["channel"]: res})
        except ValueError:
            pass  # the post was deleted in Studio
        store.queue_set(x["id"], synced=True)
    _unstick()
    return len(done)


def _unstick():
    """Safety net: a channel shown as scheduled through our queue whose queue item is gone or cancelled would block the
    post for ever. It becomes "not_scheduled" (unblocked) with a plain message. Meta-native schedules are not touched."""
    q = {x["id"]: x for x in store.queue()}
    for p in store.posts():
        bad = {}
        for ch, r in (p.get("results") or {}).items():
            if r.get("status") != "scheduled" or r.get("via") not in ("local", "vps"):
                continue
            it = q.get(r.get("queue_id") or "")
            nxt = q.get(((it or {}).get("result") or {}).get("moved_to") or "")
            if nxt and nxt["status"] in ("scheduled", "publishing"):
                continue  # a move is just being saved
            if not it or it["status"] not in ("scheduled", "publishing", "published", "failed", "missed", "needs_checking"):
                bad[ch] = {**r, "status": "not_scheduled", "error": "Not scheduled: please schedule again (its queue item was gone)",
                           "at": store.iso(store.now())}
        if bad:
            try:
                _save_results(p["id"], bad)
            except ValueError:
                pass


def tick(at=None):
    """One scheduler round: publish the LOCAL queue items whose time has come (the same run_once as the VPS job, same lock
    file), then sync results. Only what the user scheduled is in the queue. Does nothing while the setting is off."""
    s = store.settings()
    _sched["last"] = store.iso(store.now())
    if not s["local_scheduler"]:
        return {"off": True, "synced": sync_queue(at)}
    import social_publisher  # noqa: PLC0415 - studio root module
    # grace: each item's own (stored at Schedule); items without one get 2 minutes, as in the VPS job
    r = social_publisher.run_once(store, pub, publishers(), live=True, at=at, runner="local", default_late=2)
    r["synced"] = sync_queue(at)
    return r


def start_scheduler(interval=30):
    """Start Studio's scheduler thread (daemon). Called once from server.py main(); never on import (the MCP server imports
    the areas too). Safe to call twice, also at the same moment (a lock)."""
    with _sched_lock:
        if _sched.get("thread") and _sched["thread"].is_alive():
            return False
        return _start_thread(interval)


_sched_lock = threading.Lock()


def _start_thread(interval):
    stop = threading.Event()

    def loop():
        while not stop.wait(interval):
            try:
                tick()
            except Exception as ex:  # noqa: BLE001 - keep the scheduler alive, show the error in settings
                _sched["error"] = str(ex)[:300]
    t = threading.Thread(target=loop, name="social-scheduler", daemon=True)
    _sched.update(thread=t, stop=stop)
    t.start()
    return True


def test_tick(body):
    """Test mode only: run one scheduler round at a given (fake) time."""
    if not test_mode():
        raise PermissionError("only in test mode")
    return tick(store.parse_when(body["at"]) if body.get("at") else None)


# ---------- slots, quick picks, best time ----------

def _chs(q):
    c = [x for x in str(q.get("channels") or "facebook,instagram").split(",") if x in store.CHANNELS]
    return c or ["facebook", "instagram"]


def get_slot(q):
    """A time for a new post on a day (calendar click): the next free queue slot that day, else 09:00 (or the hour given)."""
    chs = _chs(q)
    day = dt.date.fromisoformat(q["day"])
    now = store.now()
    if q.get("hour") not in (None, ""):
        t = dt.datetime(day.year, day.month, day.day, int(q["hour"]), 0).astimezone()
        return {"when": store.iso(t), "from": "slot"}
    t = plan.next_free_slot(store.settings()["slots"], chs, store.posts(), max(now, dt.datetime(day.year, day.month, day.day).astimezone()), day=day)
    if t:
        return {"when": store.iso(t), "from": "queue"}
    return {"when": store.iso(dt.datetime(day.year, day.month, day.day, 9, 0).astimezone()), "from": "default"}


def _best(chs):
    try:
        h = heatmap_for("instagram" if "instagram" in chs else "facebook")
        return plan.next_best(h["top"], store.now())
    except Exception:  # noqa: BLE001
        return []


def get_quick_times(q):
    chs = _chs(q)
    slot = plan.next_free_slot(store.settings()["slots"], chs, store.posts(), store.now())
    return plan.quick_times(store.now(), slot, _best(chs))


def add_to_queue(body):
    """Give the post the next free slot of its channels (it stays a draft until you press Schedule)."""
    p = store.get(body["id"])
    _editable(p)
    t = plan.next_free_slot(store.settings()["slots"], p["channels"], [x for x in store.posts() if x["id"] != p["id"]], store.now())
    if not t:
        raise ValueError("no posting slots set for these channels: add some in SoMe settings")
    return _view(store.update(p["id"], lambda x: (x.__setitem__("when", store.iso(t)), x.__setitem__("move", None))))


def heatmap_for(channel):
    live = _published()
    online = []
    if channel == "instagram":
        try:
            online = _sm().online_followers()  # read-only, cached 6 hours
        except Exception:  # noqa: BLE001 - engagement alone then
            online = []
    return plan.heatmap(live["posts"], channel, online)


def get_heatmap(q):
    ch = q.get("channel") if q.get("channel") in ("facebook", "instagram") else "instagram"
    h = heatmap_for(ch)
    return {**h, "channel": ch, "days": list(plan.DAYS)}


# ---------- bulk CSV import ----------

def _resolve_media(ref, store_it):
    """A path on this Mac or an https URL -> (kind, name) and, when store_it, the stored media dict. Reads only."""
    from pathlib import Path  # noqa: PLC0415
    name = ref.rsplit("/", 1)[-1].split("?")[0]
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    ext = {"jpeg": "jpg"}.get(ext, ext)
    if ext not in ("jpg", "png", "mp4", "mov"):
        raise ValueError(f"{name}: only .jpg, .png, .mp4 and .mov")
    if ref.startswith(("http://", "https://")):
        if not store_it:
            return {"name": name, "kind": "video" if ext in ("mp4", "mov") else "image"}
        req = urllib.request.Request(ref, headers={"User-Agent": "Mozilla/5.0 (Marketing Studio import)"})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read(260 * 1024 * 1024)
    else:
        f = Path(ref).expanduser()
        if not f.is_file():
            raise ValueError(f"{ref}: file not found on this Mac")
        if not store_it:
            return {"name": name, "kind": "video" if ext in ("mp4", "mov") else "image"}
        data = f.read_bytes()
    import base64  # noqa: PLC0415
    m = media_upload({"filename": name, "data": base64.b64encode(data).decode()})
    return {"media_id": m["media_id"], "kind": m["kind"], "name": m["name"]}


def _csv_facts(ref):
    """Media facts for a CSV row, read-only: a file on this Mac is measured; for an address only the file type is known."""
    from pathlib import Path  # noqa: PLC0415
    name = ref.rsplit("/", 1)[-1].split("?")[0]
    ext = {"jpeg": "jpg"}.get(name.rsplit(".", 1)[-1].lower(), name.rsplit(".", 1)[-1].lower()) if "." in name else ""
    if ext not in ("jpg", "png", "mp4", "mov"):
        return None
    kind = "video" if ext in ("mp4", "mov") else "image"
    f = Path(ref).expanduser()
    if not ref.startswith(("http://", "https://")) and f.is_file():
        return checks.media_facts(f, kind, ext)
    return {"kind": kind, "ext": ext, "size": 0}


def csv_rows(text, store_media=False):
    rows = plan.parse_csv(text or "")
    for r in rows:
        media = []
        for ref in r["media"]:
            try:
                media.append(_resolve_media(ref, store_media))
            except Exception as ex:  # noqa: BLE001
                r["problems"].append(str(ex)[:200])
        facts = [_csv_facts(ref) for ref in r["media"]]
        p = {**store.blank(), **store.clean(r["fields"]),
             "media": [{"media_id": m.get("media_id") or f"csv{i}", "kind": m.get("kind", "image")} for i, m in enumerate(media)]}
        c = checks.check_post(p, store.caption_for, [f for f in facts if f], None)  # the editor's checks, media rules included
        r["problems"] += c["problems"]
        r["warnings"] = c["warnings"]
        r["media_found"] = media
    return rows


def csv_preview(body):
    """Dry run: what each CSV row would become, with its problems. Nothing is saved."""
    rows = csv_rows(body.get("text"))
    return {"rows": rows, "ok": sum(1 for r in rows if not r["problems"]), "total": len(rows)}


def csv_import(body):
    """Make DRAFTS from the rows without problems (or all rows when include_problems). Nothing is scheduled."""
    rows = csv_rows(body.get("text"), store_media=True)
    made = []
    for r in rows:
        if r["problems"] and not body.get("include_problems"):
            continue
        f = {**r["fields"], "media": [m for m in r["media_found"] if m.get("media_id")]}
        made.append(store.save(f, "csv import")["id"])
    return {"created": made, "skipped": len(rows) - len(made), "message": f"{len(made)} draft(s) made. Nothing is scheduled: press Schedule on each."}


# ---------- library, autolists, hashtags ----------

def get_library(q):
    return store.library()


def library_save(body):
    kind, item = body["kind"], dict(body.get("item") or {})
    if not (item.get("name") or "").strip():
        raise ValueError("give it a name")
    if kind == "hashtag_sets":
        item["tags"] = " ".join(t if t.startswith("#") else "#" + t for t in str(item.get("tags", "")).replace(",", " ").split() if t.strip("#"))
    if kind == "templates":
        item = {k: str(item.get(k) or "") for k in ("id", "name", "caption", "hashtags", "link", "first_comment")}
        a, b = checks.copy_problems("template", item["caption"])
        if a:
            raise ValueError("; ".join(a))
    if kind == "autolists":
        item = {"id": item.get("id"), "name": item["name"], "post_ids": [str(x) for x in item.get("post_ids") or []],
                "channels": [c for c in item.get("channels") or [] if c in store.CHANNELS], "next": int(item.get("next") or 0)}
    return store.library_save(kind, item)


def library_remove(body):
    return {"ok": store.library_remove(body["kind"], body["id"])}


def autolist_fill(body):
    """Fill the free posting slots of the next `days` days with copies of the list's posts, in turn, as DRAFTS."""
    lst = next((x for x in store.library()["autolists"] if x["id"] == body["id"]), None)
    if not lst:
        raise ValueError("autolist not found")
    posts = store.posts()
    by_id = {p["id"]: p for p in posts}
    chs = lst.get("channels") or ["facebook", "instagram"]
    end = store.now() + dt.timedelta(days=int(body.get("days") or 7))
    free, after, pool = [], store.now(), list(posts)
    while True:
        t = plan.next_free_slot(store.settings()["slots"], chs, pool, after)
        if not t or t > end:
            break
        free.append(t)
        pool.append({"channels": chs, "when": store.iso(t)})
        after = t
    new, nxt = plan.autolist_fill(lst, by_id, free)
    made = [store.save(f, "autolist")["id"] for f in new]
    store.library_save("autolists", {**lst, "next": nxt})
    return {"created": made, "message": f"{len(made)} draft(s) made on free slots. Press Schedule on each when you are happy."}


def get_hashtags(q):
    return plan.hashtag_rank(_published()["posts"])[:40]


# ---------- feed preview, analytics, report, competitors ----------

def get_feed(q):
    """Instagram grid, newest first: planned Studio posts (not published yet) then what is live."""
    planned = []
    for p in store.posts():
        r = (p.get("results") or {}).get("instagram") or {}
        if "instagram" not in (p.get("channels") or []) or r.get("status") == "published" or not p.get("media"):
            continue
        m = p["media"][0]
        planned.append({"post": p["id"], "when": p.get("when") or "", "status": r.get("status") or "draft",
                        "image": f"/api/meta/media?acct=meta&id={m['media_id']}" if m["kind"] == "image" else "", "kind": m["kind"],
                        "text": store.caption_for(p, "instagram")[:120]})
    planned.sort(key=lambda x: x["when"] or "9999", reverse=True)
    live = [{"post": None, "when": r["when"], "status": "published", "image": r.get("image", ""), "kind": r.get("format"), "text": r.get("text", "")[:120],
             "url": r.get("url")} for r in _published()["posts"] if r["channel"] == "instagram"]
    return {"items": planned + live[:30]}


def _period(q):
    end = store.now()
    return end - dt.timedelta(days=int(q.get("days") or 30)), end


def get_analytics(q):
    start, end = _period(q)
    days = str(q.get("days") or 30)
    try:  # followers + growth: cached like every SoMe read (kept until Refresh in manual mode)
        acc = _sm()._cached(f"social_cache_acct_{days}.json", 6 * 3600, lambda: _sm().account_facts(start, end))
    except Exception:  # noqa: BLE001
        acc = {}
    return plan.analytics(_published()["posts"], start, end, acc)


def report(body):
    """The analytics as a simple HTML page: saved in Studio's data folder and returned for the browser to download."""
    a = get_analytics(body)
    page = plan.report_html(a)
    f = store.data_dir() / f"social_report_{store.now():%Y%m%d-%H%M}.html"
    f.write_text(page)
    return {"html": page, "path": str(f), "name": f.name}


def get_competitors(q):
    out = []
    for u in store.settings()["competitors"]:
        try:
            bd = _sm()._cached(f"social_cache_comp_{u}.json", 24 * 3600, lambda u=u: _sm().business_discovery(u))
            out.append(plan.competitor_facts(u, bd))
        except Exception as ex:  # noqa: BLE001
            out.append({"username": u, "error": str(ex)[:200]})
    note = ("Public facts from Instagram Business Discovery (business and creator accounts only), read-only. "
            "Averages are over each account's latest 12 posts.")
    return {"rows": out, "note": note}


def boost(body):
    """A published post -> a PAUSED ad draft in the Meta ads draft (meta_boost_post). Draft only: pushed in the Ads area."""
    m = _meta_ads()
    m.use("meta")
    items = m.add([{"type": "meta_boost_post", "adset_id": body["adset_id"], "post_id": body["post_id"],
                    "channel": body.get("channel", "facebook"), "name": body.get("name") or f"Boost {body['post_id']}"}],
                  body.get("by", "you"), body.get("note") or "Boost from SoMe")
    it = items[0]
    return {"ok": not it["problems"], "summary": it["summary"], "problems": it["problems"], "warnings": it["warnings"]}


def linkedin_test(body):
    from . import _social_linkedin as li  # noqa: PLC0415
    if not li.connected():
        return {"ok": False, "message": "Not connected: LinkedIn is not set up (Setup > LinkedIn)."}
    try:
        r = li.LinkedInPublisher().test()
        return {"ok": True, "message": f"Connected to {r.get('name') or 'the company page'}."}
    except Exception as ex:  # noqa: BLE001
        return {"ok": False, "message": str(ex)}


def refresh(body):
    """The user's Refresh for SoMe (Studio's data policy): read the posts again from Meta, drop the planning caches and
    mark every older SoMe copy as stale (also in the MCP process). -> {"fetched_at", "errors"}."""
    import time as _t  # noqa: PLC0415
    t0 = _t.time()
    try:
        _sm().clear_extra_caches()
        _sm().info(force=True)
    except Exception:  # noqa: BLE001 - the posts read below reports Meta problems
        pass
    c = _published(True)
    try:
        import datapolicy  # noqa: PLC0415
        datapolicy.invalidate("social", at=t0)
    except ImportError:
        pass
    return {"fetched_at": c.get("fetched_at"), "errors": c.get("errors", [])}


POST = {"save": save, "delete_draft": delete_draft, "move": move, "discard_move": discard_move, "confirm_move": confirm_move,
        "media": media_upload, "settings": save_settings, "post_now": post_now, "schedule": schedule, "cancel": cancel,
        "delete_post": delete_post, "boost": boost, "linkedin_test": linkedin_test, "refresh": refresh,
        "approve": approve, "add_to_queue": add_to_queue, "csv_preview": csv_preview, "csv_import": csv_import,
        "library_save": library_save, "library_remove": library_remove, "autolist_fill": autolist_fill, "report": report,
        "test_tick": test_tick, "check_again": check_again,
        "check": lambda b: check(store.get(b["id"]) if b.get("id") and not b.get("post") else {**store.blank(), **store.clean(b.get("post") or {})})}


# ---------- health + overview ----------

def _row(channel, account, chk, ok, detail, fix=""):
    try:
        import health  # noqa: PLC0415
        return health._row(channel, account, chk, ok, detail, fix)
    except ImportError:
        return {"channel": channel, "account": account, "check": chk, "ok": ok, "detail": detail, "fix": fix}


def health(today=None):
    rows = []
    try:
        p = _sm().page(force=True)
        rows.append(_row("SoMe Facebook", p["name"], "Page token", True, "Page access token reachable (kept in memory, never shown)."))
        if p["ig_id"]:
            ig = _sm()._get(p["ig_id"], fields="username")
            rows.append(_row("SoMe Instagram", "@" + ig.get("username", p["ig_username"]), "Account", True, "Instagram account reachable through the Page."))
        else:
            rows.append(_row("SoMe Instagram", "", "Account", False, "No Instagram account linked to the Page.", "Link it in Meta Business Settings."))
    except Exception as ex:  # noqa: BLE001
        rows.append(_row("SoMe Facebook", "Master Liveaboards", "Page token", False, f"Page token not reachable: {ex}",
                         "Check the system user still has the Page asset with Create content rights."))
    from . import _social_linkedin as li  # noqa: PLC0415
    rows.append(_row("SoMe LinkedIn", "", "Connection", True if li.connected() else None,
                     "Connected." if li.connected() else "Not connected.", "See the README: LinkedIn developer app + Community Management API."))
    s = store.settings()
    rows.append(_row("SoMe scheduling", "Studio on this Mac", "Scheduler", True if s["local_scheduler"] else None,
                     ("On: scheduled Instagram and LinkedIn posts go out while Studio is open" + ("" if _sched.get("thread") else " (the thread is not running yet)"))
                     if s["local_scheduler"] else "Off: Instagram and LinkedIn can't be scheduled (Facebook uses Meta's own scheduling).",
                     "Turn it on in SoMe settings if this Mac is on and Studio open at the times you schedule."))
    rows.append(_row("SoMe scheduling", "VPS publisher job", "Parked", None, "Parked: not available yet.",
                     "Bringing it back needs a separate build, a review and your approval: see the README."))
    return rows


def overview(days="7"):
    live = _published()
    cut = store.now() - dt.timedelta(days=int(days))
    rows = [r for r in live["posts"] if store.parse_when(r["when"]) >= cut]
    fb = [r for r in rows if r["channel"] == "facebook"]
    ig = [r for r in rows if r["channel"] == "instagram"]
    s = lambda rs, k: sum(r.get(k) or 0 for r in rs)  # noqa: E731
    tiles = [{"k": "Posts published", "v": f"{len(rows)}", "hi": f"Facebook {len(fb)} · Instagram {len(ig)}"},
             {"k": "Reach", "v": f"{s(rows, 'reach'):,}", "hi": f"Facebook {s(fb, 'reach'):,} · Instagram {s(ig, 'reach'):,}"},
             {"k": "Engagement", "v": f"{s(rows, 'engagement'):,}", "hi": "reactions/likes, comments, shares, saves"},
             {"k": "Link clicks", "v": f"{s(fb, 'link_clicks'):,}", "hi": "Facebook (Instagram captions have no links)"}]
    note = ("Organic posts on the Master Liveaboards Page and @master_liveaboards, by publish date. Reach is added up per post, "
            "so one person can count more than once. Select Cabin from social is not shown yet (needs a GA4 filter for organic social).")
    if live.get("errors"):
        note += " Meta: " + "; ".join(live["errors"])
    return {"title": "SoMe", "tiles": tiles, "note": note}
