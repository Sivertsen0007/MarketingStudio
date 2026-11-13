"""SoMe publishing: the adapter interface, FakePublisher (all tests), the public media host for Instagram and the
run() that drives one button press (Post now, Schedule, Cancel, Delete) across channels with a step report.

Only the user's own button reaches run() (POST /api/area/social/post_now etc. from the UI, or the VPS job for items the
user scheduled). Claude's MCP has no tool for it.

Adapter (one per channel):
  publish(job) -> {"post_id", "url"}              post now
  schedule(job, when) -> {"post_id", "url"}       only where the channel schedules itself (Facebook)
  reschedule(result, when) -> {}                  move a channel-scheduled post
  cancel(result) -> {}                            remove a channel-scheduled post before it goes out
  delete(result) -> {}                            delete a published post (Facebook, LinkedIn)
  job: {channel, text, link, title, alt_text, location, first_comment, format, media: [{media_id, kind, ext, path, public_url}]}
  report(step, message): step is "upload", "create" or "done"
"""
import itertools
import mimetypes

try:
    from . import _social_store as store
except ImportError:  # loaded as a plain module (VPS job, tests)
    import _social_store as store

PUBLIC_BASE = "https://media.mlmediahub.com"
PUBLIC_PREFIX = "social"


class NotSupported(Exception):
    """The channel can't do this through its API (the message says what to do instead)."""


class Publisher:
    channel = ""
    can_schedule = False

    def publish(self, job, report):
        raise NotImplementedError

    def schedule(self, job, when, report):
        raise NotSupported(f"{store.CHANNEL_LABEL[self.channel]} has no scheduling of its own")

    def reschedule(self, result, when):
        raise NotSupported("move it by cancelling and scheduling again")

    def cancel(self, result):
        raise NotSupported("nothing to cancel on the channel")

    def delete(self, result):
        raise NotSupported(f"{store.CHANNEL_LABEL[self.channel]} posts can't be deleted from Studio")

    def find_recent(self, job, since):
        """Read-only: is this job's post already live (posted at or after `since`)? -> {"post_id", "url"} or None.
        Raises when the channel can't be searched (then the caller must treat it as unsure)."""
        raise NotSupported(f"{store.CHANNEL_LABEL[self.channel]} posts can't be looked up from Studio")


def norm(text):
    """Caption compare key: lower case, single spaces, first 120 characters."""
    return " ".join(str(text or "").lower().split())[:120]


def matches(job, text, at, since, slack=180):
    """Same caption and posted from `since` (minus a few minutes for clock drift) on."""
    a, b = norm(job.get("text")), norm(text)
    if not a or not b or not (a.startswith(b[:80]) or b.startswith(a[:80])):
        return False
    t = store.parse_when(at) if at else None
    return bool(t and t >= since - __import__("datetime").timedelta(seconds=slack))


class FakePublisher(Publisher):
    """Pretends to post. Records every call; never touches the network. fail="publish" etc. makes that call fail."""
    _n = itertools.count(1)
    live = []  # (channel, text, at, result) of everything "posted", shared by all fakes, for find_recent

    def __init__(self, channel, fail=None, can_schedule=None):
        self.channel = channel
        self.can_schedule = channel == "facebook" if can_schedule is None else can_schedule
        self.fail = fail
        self.calls = []

    def _maybe_fail(self, what):
        if self.fail == what:
            raise RuntimeError(f"fake {self.channel} {what} failed")

    def _result(self):
        n = next(self._n)
        return {"post_id": f"fake-{self.channel}-{n}", "url": f"https://example.invalid/{self.channel}/{n}"}

    def publish(self, job, report):
        self.calls.append(("publish", job))
        if job.get("media"):
            report("upload", f"Uploading {len(job['media'])} file(s) (test mode)")
        self._maybe_fail("publish")
        report("create", "Creating the post (test mode)")
        r = self._result()
        FakePublisher.live.append((self.channel, job.get("text"), store.iso(store.now()), r))
        if self.fail == "publish_timeout":  # it went out, but the answer never came back
            raise RuntimeError("no answer (timed out); the change may or may not have been applied, check before retrying")
        return r

    def find_recent(self, job, since):
        self.calls.append(("find_recent", store.iso(since)))
        if self.fail == "find":
            raise RuntimeError("fake lookup failed")
        hit = next((r for ch, t, at, r in reversed(FakePublisher.live) if ch == self.channel and matches(job, t, at, since)), None)
        return dict(hit) if hit else None

    def schedule(self, job, when, report):
        if not self.can_schedule:
            return super().schedule(job, when, report)
        self.calls.append(("schedule", job, store.iso(when)))
        self._maybe_fail("schedule")
        report("create", "Scheduling (test mode)")
        return {**self._result(), "scheduled_for": store.iso(when)}

    def reschedule(self, result, when):
        self.calls.append(("reschedule", result.get("post_id"), store.iso(when)))
        self._maybe_fail("reschedule")
        return {}

    def cancel(self, result):
        self.calls.append(("cancel", result.get("post_id")))
        self._maybe_fail("cancel")
        return {}

    def delete(self, result):
        if self.channel == "instagram":
            return super().delete(result)
        self.calls.append(("delete", result.get("post_id")))
        self._maybe_fail("delete")
        return {}


# ---------- public URLs for Instagram (Meta fetches the file by URL) ----------

class R2Host:
    """Puts a stored file on the Media Hub bucket (mlmediahub, served at media.mlmediahub.com) under social/<sha>.<ext>.
    Called only inside a Post now / Schedule press that includes Instagram. The file is public from then on
    (as the post itself is). Credentials: ~/.config/rclone/rclone.conf [cloudflare-r2], read by r2_client."""

    def put(self, path, key):
        import sys
        from pathlib import Path
        import appconfig  # noqa: PLC0415
        hub = appconfig.aihub_path()
        if not hub:
            raise NotSupported("Uploading local media needs the R2 image library (AI-Hub) on this Mac.")
        lib = hub / "shared" / "image_library"
        if str(lib.parent) not in sys.path:
            sys.path.insert(0, str(lib.parent))
        from image_library.r2_client import R2Client  # noqa: PLC0415 - boto3 only when really needed
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        return R2Client(public_base=PUBLIC_BASE).upload(Path(path), key, content_type=ctype)


class FakeHost:
    def __init__(self):
        self.put_calls = []

    def put(self, path, key):
        self.put_calls.append((str(path), key))
        return f"https://example.invalid/{key}"


def ensure_public(job, host, report=None):
    """Give every media item in the job a public URL (uploads only what has none yet)."""
    todo = [m for m in job.get("media") or [] if not m.get("public_url")]
    if todo and report:
        report("upload", f"Putting {len(todo)} file(s) on media.mlmediahub.com for Instagram")
    for m in todo:
        m["public_url"] = host.put(m["path"], f"{PUBLIC_PREFIX}/{m['media_id']}.{m['ext']}")
    return job


# ---------- jobs ----------

def build_job(post, channel, media_lookup, track=None):
    """media_lookup(media_id) -> (path, ext, kind). track(channel, link) -> link with UTMs (or None).
    -> the job one channel needs, from the post."""
    media = []
    for m in post.get("media") or []:
        path, ext, kind = media_lookup(m["media_id"])
        media.append({"media_id": m["media_id"], "kind": kind, "ext": ext, "path": str(path), "public_url": m.get("public_url", "")})
    text, link = store.caption_for(post, channel), post.get("link", "") if channel != "instagram" else ""
    if track:
        text, link = track(channel, text, link)
    return {"channel": channel, "post_id": post["id"], "text": text,
            "link": link, "title": post.get("title", ""),
            "alt_text": post.get("alt_text", ""), "location": post.get("location", ""),
            "first_comment": post.get("first_comment", "") if channel == "instagram" else "",
            "format": post.get("format") or "text", "media": media}


def run(action, jobs, publishers, report, when=None, results=None):
    """One button press across channels. action: publish | schedule | reschedule | cancel | delete.
    jobs: {channel: job} (publish/schedule), results: {channel: earlier result} (reschedule/cancel/delete).
    report(channel, step, message). -> {channel: {"status", "post_id", "url", "error", "at"}}. One channel failing
    does not stop the others (each is its own post on its own channel)."""
    out = {}
    for ch in (jobs or results or {}):
        pub = publishers[ch]
        at = store.iso(store.now())
        rep = lambda step, msg, ch=ch: report(ch, step, msg)  # noqa: E731
        try:
            if action == "publish":
                r = pub.publish(jobs[ch], rep)
                out[ch] = {"status": "published", "error": "", "at": at, "via": "now", **r}
            elif action == "schedule":
                r = pub.schedule(jobs[ch], when, rep)
                out[ch] = {"status": "scheduled", "error": "", "at": at, "via": "meta", "scheduled_for": store.iso(when), **r}
            elif action == "reschedule":
                pub.reschedule(results[ch], when)
                out[ch] = {**results[ch], "scheduled_for": store.iso(when), "at": at}
            elif action == "cancel":
                pub.cancel(results[ch])
                out[ch] = {**results[ch], "status": "cancelled", "at": at}
            elif action == "delete":
                pub.delete(results[ch])
                out[ch] = {**results[ch], "status": "deleted", "at": at}
            else:
                raise ValueError(f"unknown action {action}")
            rep("done", {"publish": "Published", "schedule": "Scheduled", "reschedule": "Moved", "cancel": "Cancelled",
                         "delete": "Deleted"}[action])
        except Exception as ex:  # noqa: BLE001 - every failure is shown per channel
            msg = str(ex) or ex.__class__.__name__
            out[ch] = {**((results or {}).get(ch) or {}), "status": "failed" if action in ("publish", "schedule") else (results or {}).get(ch, {}).get("status", "failed"),
                       "error": msg[:500], "at": at}
            report(ch, "failed", msg[:300])
    return out
