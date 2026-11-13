"""SoMe <-> Meta: read the Master Liveaboards Page posts and @master_liveaboards media with insights (cached), and the
Facebook / Instagram publishers (used only from the user's own button, or by the VPS job for posts the user scheduled).

Token: the Meta token from Setup (appconfig, via vendor_hub/meta_api.py). Page calls need the PAGE
token, fetched with GET /{page}?fields=access_token and kept in memory only: never printed, logged or saved.
DiveTravel pages are not visible to the token and are kept off.

Metric names (checked 10 November 2026 in Meta's docs, and by a live read):
- Facebook post: post_media_view (views; replaces post_impressions, deprecated 15 November 2025),
  post_total_media_view_unique (reach; replaces post_impressions_unique, deprecated 15 June 2025), post_clicks_by_type
  ("link clicks" key), post_video_views. Reactions, comments and shares come as counts on the post (summary only, no
  names: comments are personal data and are not read).
- Instagram media: reach, views (replaces impressions for media after 2 July 2024), likes, comments, shares, saved,
  total_interactions. Lifetime values.
"""
import json
import mimetypes
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

try:
    from . import _social_store as store
    from ._social_publish import Publisher, NotSupported, matches
except ImportError:
    import _social_store as store
    from _social_publish import Publisher, NotSupported, matches

import appconfig  # noqa: E402  (vendor_hub on sys.path)
import meta_api  # noqa: E402

PAGE_ID = appconfig.get("META_PAGE_ID", "703318833095953")  # default: Master Liveaboards
CACHE_TTL = 30 * 60
FB_FIELDS = ("id,created_time,message,permalink_url,full_picture,status_type,shares,comments.summary(true).limit(0),"
             "reactions.summary(true).limit(0)")
FB_METRICS = "post_media_view,post_total_media_view_unique,post_clicks_by_type,post_video_views"
IG_FIELDS = "id,caption,media_type,media_product_type,permalink,timestamp,thumbnail_url,media_url,like_count,comments_count"
IG_METRICS = "reach,views,likes,comments,shares,saved,total_interactions"
_page = {}


def _dp():
    """Studio's data policy (datapolicy.py at the studio root); None where it isn't there (e.g. the parked VPS job)."""
    try:
        import datapolicy  # noqa: PLC0415
        return datapolicy
    except ImportError:
        return None


def _fresh(age, max_age, filled_at):
    """Studio's one rule: manual mode keeps a copy until the user's Refresh; auto mode uses max_age. 0 always fetches."""
    dp = _dp()
    if dp:
        return dp.fresh("social", age, max_age, filled_at)
    return max_age != 0 and age is not None and age < max_age


def _mark():
    dp = _dp()
    if dp:
        dp.mark("social")


AUTH_CODES = (190, 102)  # Meta: the access token is not valid (any more)


def page(force=False):
    """{id, name, token, ig_id, ig_username}. The token stays in this process's memory for its whole life and is
    fetched again only after an auth error (see _call) or force. Never printed, logged or saved."""
    if _page.get("token") and not force:
        return _page
    if not meta_api.TOKEN:
        raise meta_api.MetaError({"message": "no Meta access token yet (Setup)"})
    p = meta_api.get(PAGE_ID, fields="name,access_token,instagram_business_account{id,username}")
    if not p.get("access_token"):
        raise meta_api.MetaError({"message": "the token can't get a Page access token for Master Liveaboards"})
    ig = p.get("instagram_business_account") or {}
    _page.clear()
    _page.update(id=p["id"], name=p.get("name", ""), token=p["access_token"], ig_id=ig.get("id", ""), ig_username=ig.get("username", ""),
                 at=time.time())
    return _page


def info(force=False):
    """Safe facts for the UI (no token), from the in-memory page or social_cache_page.json: no Meta call on a page
    load once they are known (same data rule as every Studio cache)."""
    if _page.get("token") and not force:
        p = _page
    else:
        f = store.data_dir() / "social_cache_page.json"
        try:
            c = json.loads(f.read_text())
            if not force and _fresh(time.time() - c["ts"], None, c["ts"]):
                return c["data"]
        except (OSError, ValueError, KeyError):
            pass
        p = page(force=force)
    out = {"page_id": p["id"], "page_name": p["name"], "ig_id": p["ig_id"], "ig_username": p["ig_username"]}
    (store.data_dir() / "social_cache_page.json").write_text(json.dumps({"ts": time.time(), "data": out}))
    return out


def _call(method, path, params=None):
    """A Graph call with the Page token. After an auth error the token is fetched once more and the call tried again
    (a refused call changed nothing)."""
    try:
        return meta_api.call(method, path, params, token=page()["token"])
    except meta_api.MetaError as ex:
        if ex.code not in AUTH_CODES and ex.status != 401:
            raise
    return meta_api.call(method, path, params, token=page(force=True)["token"])


def _get(path, **params):
    return _call("GET", path, params)


def _post(path, params):
    return _call("POST", path, params)


def _all(path, limit_pages=1, **params):
    out, r = [], _get(path, **params)
    for _ in range(limit_pages):
        out += r.get("data", [])
        nxt = (r.get("paging") or {}).get("next")
        if not nxt:
            break
        r = _call("GET", nxt)
    return out


# ---------- reading ----------

def _vals(ins):
    """{metric: lifetime value}. Meta also sends "day" rows for some metrics (e.g. post_total_media_view_unique): skip those."""
    out = {}
    for x in (ins or {}).get("data", []):
        if x.get("period") not in (None, "lifetime"):
            continue
        out.setdefault(x["name"], (x.get("values") or [{}])[0].get("value"))
    return out


def fb_row(x):
    m = _vals(x.get("insights"))
    clicks = m.get("post_clicks_by_type") or {}
    kind = {"added_video": "Video", "added_photos": "Photo", "shared_story": "Link", "mobile_status_update": "Text"}.get(x.get("status_type"), "Post")
    if "/reel/" in (x.get("permalink_url") or ""):
        kind = "Reel"
    reactions = ((x.get("reactions") or {}).get("summary") or {}).get("total_count", 0)
    comments = ((x.get("comments") or {}).get("summary") or {}).get("total_count", 0)
    shares = (x.get("shares") or {}).get("count", 0)
    return {"id": x["id"], "channel": "facebook", "when": x.get("created_time", ""), "text": (x.get("message") or "")[:500], "url": x.get("permalink_url", ""),
            "image": x.get("full_picture", ""), "format": kind, "reach": m.get("post_total_media_view_unique"), "views": m.get("post_media_view"),
            "likes": reactions, "comments": comments, "shares": shares, "saves": None, "link_clicks": clicks.get("link clicks", 0),
            "clicks": sum(v for v in clicks.values() if isinstance(v, (int, float))), "video_views": m.get("post_video_views"),
            "engagement": reactions + comments + shares}


def ig_row(x):
    m = _vals(x.get("insights"))
    kind = {"REELS": "Reel", "STORY": "Story"}.get(x.get("media_product_type")) or {"CAROUSEL_ALBUM": "Carousel", "IMAGE": "Image", "VIDEO": "Video"}.get(x.get("media_type"), "Post")
    likes, comments = m.get("likes", x.get("like_count", 0)) or 0, m.get("comments", x.get("comments_count", 0)) or 0
    shares, saves = m.get("shares") or 0, m.get("saved") or 0
    return {"id": x["id"], "channel": "instagram", "when": x.get("timestamp", "").replace("+0000", "+00:00"), "text": (x.get("caption") or "")[:500],
            "url": x.get("permalink", ""), "image": x.get("thumbnail_url") or x.get("media_url") or "", "format": kind, "reach": m.get("reach"),
            "views": m.get("views"), "likes": likes, "comments": comments, "shares": shares, "saves": saves, "link_clicks": None, "clicks": None,
            "video_views": m.get("views") if kind in ("Reel", "Video") else None, "engagement": m.get("total_interactions", likes + comments + shares + saves)}


def _fb_posts(limit):
    try:
        rows = _all(f"{PAGE_ID}/published_posts", limit=limit, fields=FB_FIELDS + f",insights.metric({FB_METRICS}){{name,values}}")
    except meta_api.MetaError:  # one post without insights can fail the whole list: read them one by one
        rows = _all(f"{PAGE_ID}/published_posts", limit=limit, fields=FB_FIELDS)
        for x in rows:
            try:
                x["insights"] = _get(f"{x['id']}/insights", metric=FB_METRICS)
            except meta_api.MetaError:
                x["insights"] = {}
    return [fb_row(x) for x in rows]


def _ig_posts(limit):
    ig = page()["ig_id"]
    if not ig:
        return []
    try:
        rows = _all(f"{ig}/media", limit=limit, fields=IG_FIELDS + f",insights.metric({IG_METRICS}){{name,values}}")
    except meta_api.MetaError:
        rows = _all(f"{ig}/media", limit=limit, fields=IG_FIELDS)
        for x in rows:
            try:
                x["insights"] = _get(f"{x['id']}/insights", metric=IG_METRICS)
            except meta_api.MetaError:
                x["insights"] = {}
    return [ig_row(x) for x in rows]


def _fb_scheduled():
    try:
        rows = _all(f"{PAGE_ID}/scheduled_posts", fields="id,message,scheduled_publish_time,permalink_url")
    except meta_api.MetaError:
        return []
    return [{"id": x["id"], "channel": "facebook", "when": store.iso(store.parse_when(int(x["scheduled_publish_time"]))) if x.get("scheduled_publish_time") else "",
             "text": (x.get("message") or "")[:300], "url": x.get("permalink_url", "")} for x in rows]


def published(max_age=CACHE_TTL, limit=100):
    """The latest `limit` published posts per channel (Facebook + Instagram) with insights, newest first, plus Facebook posts Meta has scheduled.
    Cached in social_cache_meta.json (our own posts' numbers only, no personal data)."""
    f = store.data_dir() / "social_cache_meta.json"
    try:
        c = json.loads(f.read_text())
        if _fresh(time.time() - c["ts"], max_age, c["ts"]):
            return c
    except (OSError, ValueError, KeyError):
        pass
    errors = []
    rows = []
    for fn, name in ((_fb_posts, "Facebook"), (_ig_posts, "Instagram")):
        try:
            rows += fn(limit)
        except meta_api.MetaError as ex:
            errors.append(f"{name}: {ex}")
    sched = _fb_scheduled() if not errors else []
    rows.sort(key=lambda r: r["when"], reverse=True)
    c = {"ts": time.time(), "fetched_at": store.iso(store.now()), "posts": rows, "scheduled": sched, "errors": errors}
    if not errors:
        f.write_text(json.dumps(c, ensure_ascii=False))
        _mark()
    return c


def publishing_limit():
    ig = page()["ig_id"]
    d = (_get(f"{ig}/content_publishing_limit", fields="quota_usage,config").get("data") or [{}])[0]
    return {"used": d.get("quota_usage", 0), "total": (d.get("config") or {}).get("quota_total", 100)}


# ---------- publishing (user's button only) ----------

def _multipart(url, fields, file_field=None, path=None, timeout=900):
    """POST multipart/form-data with the page token. Never retried: an upload may have gone through."""
    b = uuid.uuid4().hex
    parts = [f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
             for k, v in {**fields, "access_token": page()["token"]}.items()]
    if path:
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        parts.append(f'--{b}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{Path(path).name}"\r\n'
                     f"Content-Type: {ctype}\r\n\r\n".encode() + Path(path).read_bytes() + b"\r\n")
    parts.append(f"--{b}--\r\n".encode())
    req = urllib.request.Request(url, data=b"".join(parts), method="POST",
                                 headers={"Content-Type": f"multipart/form-data; boundary={b}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read())["error"]
        except (ValueError, KeyError, TypeError, OSError):
            err = {"message": f"HTTP {e.code}"}
        raise meta_api.MetaError(err, status=e.code) from None
    except (urllib.error.URLError, OSError) as e:
        raise meta_api.MetaError({"message": f"no answer from Meta ({getattr(e, 'reason', None) or e.__class__.__name__}); "
                                             "the upload may or may not have gone through"}) from None


def _delete(obj_id):
    q = urllib.parse.urlencode({"access_token": page()["token"]})
    req = urllib.request.Request(f"{meta_api.API}/{obj_id}?{q}", method="DELETE", headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read())["error"]
        except (ValueError, KeyError, TypeError, OSError):
            err = {"message": f"HTTP {e.code}"}
        raise meta_api.MetaError(err, status=e.code) from None


class FacebookPublisher(Publisher):
    """Page posts: /feed (text, link, several photos via attached_media), /photos (one image), /videos (one video).
    Scheduling is Meta's own: published=false + scheduled_publish_time, 10 minutes to 30 days ahead."""
    channel = "facebook"
    can_schedule = True

    def _go(self, job, report, when=None):
        pid = page()["id"]
        sched = {"published": "false", "scheduled_publish_time": int(when.timestamp())} if when else {}
        media = job.get("media") or []
        place = {"place": job["location"]} if str(job.get("location") or "").isdigit() else {}
        if not media:
            report("create", "Creating the Facebook post")
            r = _post(f"{pid}/feed", {"message": job["text"], **({"link": job["link"]} if job.get("link") else {}), **place, **sched})
            post_id = r["id"]
        elif len(media) == 1 and media[0]["kind"] == "image":
            report("upload", "Uploading the image to Facebook")
            fields = {"message": job["text"], **place, **sched}
            if job.get("alt_text"):
                fields["alt_text_custom"] = job["alt_text"]
            r = _multipart(f"{meta_api.API}/{pid}/photos", fields, "source", media[0]["path"])
            report("create", "Facebook made the photo post")
            post_id = r.get("post_id") or r["id"]
        elif len(media) == 1:
            report("upload", "Uploading the video to Facebook (this can take a few minutes)")
            r = _multipart(f"https://graph-video.facebook.com/{meta_api.GRAPH_VERSION}/{pid}/videos",
                           {"description": job["text"], **({"title": job["title"]} if job.get("title") else {}), **sched}, "source", media[0]["path"])
            report("create", "Facebook made the video post")
            return {"post_id": r["id"], "url": f"https://www.facebook.com/{pid}/videos/{r['id']}", "object": "video"}
        else:
            ids = []
            for i, m in enumerate(media, 1):
                report("upload", f"Uploading image {i} of {len(media)} to Facebook")
                f = {"published": "false", **({"temporary": "true"} if when else {})}
                ids.append(_multipart(f"{meta_api.API}/{pid}/photos", f, "source", m["path"])["id"])
            report("create", "Creating the Facebook post with the images")
            r = _post(f"{pid}/feed", {"message": job["text"], "attached_media": [{"media_fbid": x} for x in ids], **place, **sched})
            post_id = r["id"]
        try:
            url = _get(post_id, fields="permalink_url").get("permalink_url") or f"https://www.facebook.com/{post_id}"
        except meta_api.MetaError:
            url = f"https://www.facebook.com/{post_id}"
        return {"post_id": post_id, "url": url}

    def publish(self, job, report):
        return self._go(job, report)

    def find_recent(self, job, since):
        """Read-only: the Page's latest posts, matched on caption + time."""
        rows = _get(f"{page()['id']}/published_posts", limit=25, fields="id,message,created_time,permalink_url").get("data", [])
        hit = next((x for x in rows if matches(job, x.get("message"), x.get("created_time"), since)), None)
        return {"post_id": hit["id"], "url": hit.get("permalink_url", "")} if hit else None

    def schedule(self, job, when, report):
        return self._go(job, report, when)

    def reschedule(self, result, when):
        _post(result["post_id"], {"scheduled_publish_time": int(when.timestamp())})
        return {}

    def cancel(self, result):
        _delete(result["post_id"])  # a scheduled post that never went out: removing it is the cancel
        return {}

    def delete(self, result):
        _delete(result["post_id"])
        return {}


class InstagramPublisher(Publisher):
    """Container + media_publish. Image: image_url; Reel/video: media_type=REELS + video_url; carousel: children with
    is_carousel_item, then media_type=CAROUSEL. Meta fetches the file from a PUBLIC URL (see _social_publish.R2Host).
    No scheduling in the API (the VPS job publishes at the time). Limit: 100 API posts per 24 hours (checked first)."""
    channel = "instagram"
    can_schedule = False
    WAIT = {"image": 60, "video": 300}

    def __init__(self, sleep=time.sleep):
        self.sleep = sleep

    def _wait(self, cid, kind):
        t0 = time.time()
        while True:
            st = _get(cid, fields="status_code,status").get("status_code")
            if st in ("FINISHED", "PUBLISHED"):
                return
            if st in ("ERROR", "EXPIRED"):
                raise meta_api.MetaError({"message": f"Instagram could not process the file ({st})"})
            if time.time() - t0 > self.WAIT[kind]:
                raise meta_api.MetaError({"message": "Instagram was still processing the file after 5 minutes: nothing was posted"})
            self.sleep(5 if kind == "image" else 15)

    def publish(self, job, report):
        ig = page()["ig_id"]
        if not ig:
            raise NotSupported("the Page has no Instagram account linked")
        lim = publishing_limit()
        if lim["used"] >= lim["total"]:
            raise NotSupported(f"Instagram's limit of {lim['total']} API posts per 24 hours is reached")
        media = job.get("media") or []
        if not media:
            raise NotSupported("Instagram needs an image or a video")
        if any(not m.get("public_url") for m in media):
            raise NotSupported("the files have no public URL yet")
        loc = {"location_id": job["location"]} if str(job.get("location") or "").isdigit() and len(media) == 1 else {}
        report("upload", "Instagram is fetching the file(s)")
        if len(media) == 1:
            m = media[0]
            if m["kind"] == "image":
                p = {"image_url": m["public_url"], "caption": job["text"], **loc}
                if job.get("alt_text"):
                    p["alt_text"] = job["alt_text"]
            else:
                p = {"media_type": "REELS", "video_url": m["public_url"], "caption": job["text"], "share_to_feed": "true", **loc}
            cid = _post(f"{ig}/media", p)["id"]
            self._wait(cid, m["kind"])
        else:
            kids = []
            for m in media:
                p = {"image_url": m["public_url"]} if m["kind"] == "image" else {"media_type": "VIDEO", "video_url": m["public_url"]}
                k = _post(f"{ig}/media", {**p, "is_carousel_item": "true"})["id"]
                self._wait(k, m["kind"])
                kids.append(k)
            cid = _post(f"{ig}/media", {"media_type": "CAROUSEL", "children": ",".join(kids), "caption": job["text"]})["id"]
            self._wait(cid, "image")
        report("create", "Publishing on Instagram")
        mid = _post(f"{ig}/media_publish", {"creation_id": cid})["id"]
        url = _get(mid, fields="permalink").get("permalink", "")
        out = {"post_id": mid, "url": url}
        if job.get("first_comment"):
            try:
                _post(f"{mid}/comments", {"message": job["first_comment"]})
            except meta_api.MetaError as ex:
                out["warning"] = f"posted, but the first comment failed: {ex}"
        return out

    def find_recent(self, job, since):
        """Read-only: the account's latest media, matched on caption + time."""
        rows = _get(f"{page()['ig_id']}/media", limit=25, fields="id,caption,timestamp,permalink").get("data", [])
        hit = next((x for x in rows if matches(job, x.get("caption"), x.get("timestamp"), since)), None)
        return {"post_id": hit["id"], "url": hit.get("permalink", "")} if hit else None

    def delete(self, result):
        raise NotSupported("Instagram posts can't be deleted from Studio: delete it in the Instagram app")


# ---------- planning reads (read-only, cached) ----------

def _cached(name, max_age, fn):
    f = store.data_dir() / name
    try:
        c = json.loads(f.read_text())
        if _fresh(time.time() - c["ts"], max_age, c["ts"]):
            return c["data"]
    except (OSError, ValueError, KeyError):
        pass
    data = fn()
    f.write_text(json.dumps({"ts": time.time(), "data": data}, ensure_ascii=False, default=str))
    _mark()
    return data


EXTRA_CACHES = ("social_cache_online.json", "social_cache_page.json", "social_cache_acct_*.json", "social_cache_comp_*.json")


def clear_extra_caches():
    """The user's Refresh: drop the planning caches (follower activity, page facts, account facts, competitors)."""
    n = 0
    for pat in EXTRA_CACHES:
        for f in store.data_dir().glob(pat):
            f.unlink(missing_ok=True)
            n += 1
    return n


def online_followers():
    """Instagram followers online per hour, last 30 days (Meta keeps 30 days). -> [(day end ISO, {hour: followers})].
    Hours are Meta's (read as Pacific time, see _social_plan). Cached 6 hours."""
    def fetch():
        ig, now = page()["ig_id"], int(time.time())
        out = []
        for a, b in ((now - 29 * 86400, now - 15 * 86400), (now - 15 * 86400, now)):
            r = _get(f"{ig}/insights", metric="online_followers", period="lifetime", since=a, until=b)
            for x in r.get("data", []):
                out += [(v["end_time"], v.get("value") or {}) for v in x.get("values", []) if isinstance(v.get("value"), dict)]
        return sorted({e: v for e, v in out}.items())
    return [(store.parse_when(e), {int(k): n for k, n in v.items()}) for e, v in _cached("social_cache_online.json", 6 * 3600, fetch)]


def account_facts(start, end):
    """{facebook: {followers, growth}, instagram: {followers, growth}} for the period (growth only where Meta has it:
    Instagram new followers per day for the last 30 days, Facebook page_follows first vs last day)."""
    out = {}
    a, b = int(start.timestamp()), int(min(end, store.now()).timestamp())
    try:
        fb = _get(PAGE_ID, fields="followers_count")
        g = None
        try:
            r = _get(f"{PAGE_ID}/insights", metric="page_follows", period="day", since=max(a, b - 89 * 86400), until=b)
            vals = [v["value"] for x in r.get("data", []) for v in x.get("values", []) if isinstance(v.get("value"), int)]
            g = vals[-1] - vals[0] if len(vals) > 1 else None
        except meta_api.MetaError:
            pass
        out["facebook"] = {"followers": fb.get("followers_count"), "growth": g}
    except meta_api.MetaError:
        pass
    ig = page()["ig_id"]
    if ig:
        try:
            acc = _get(ig, fields="followers_count")
            g = None
            try:
                r = _get(f"{ig}/insights", metric="follower_count", period="day", since=max(a, b - 29 * 86400), until=b)
                g = sum(v.get("value") or 0 for x in r.get("data", []) for v in x.get("values", []))
            except meta_api.MetaError:
                pass
            out["instagram"] = {"followers": acc.get("followers_count"), "growth": g}
        except meta_api.MetaError:
            pass
    return out


def business_discovery(username):
    """Public facts of another Instagram business/creator account (Business Discovery, read-only)."""
    ig = page()["ig_id"]
    r = _get(ig, fields=f"business_discovery.username({username}){{username,followers_count,media_count,"
                        "media.limit(12){like_count,comments_count,timestamp}}")
    return r.get("business_discovery") or {}
