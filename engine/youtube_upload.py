"""Upload a video file from this Mac to YouTube through the Google Ads API (YouTubeVideoUploadService, resumable upload).

Where it goes: the Google-managed YouTube channel of the Google Ads account, as an unlisted video. That is the only
destination Studio uploads to. An advertiser's own (brand) channel takes a channel_id plus the sign-in of a Google user
who can manage that channel (Google: user authentication only, never a service account), and Studio has no YouTube
channel sign-in, so it is refused here. Linking a brand channel to the Ads account is done in the Google Ads web UI.

When it runs: only from the user's own Upload button in the Videos tab, on a video_upload item of the Ads draft, gated
like a push (the PUSH_LOCK, team approval when the team requires it, the one-line why in a team). Never from Push live
of the draft, never from MCP (core.add refuses video_upload items from Claude and MCP has no tool for it).

The protocol (Google's Upload videos guide, API v26):
  1. POST https://googleads.googleapis.com/resumable/upload/v26/customers/<cid>/youTubeVideoUploads:create
     headers X-Goog-Upload-Protocol: resumable, X-Goog-Upload-Command: start, X-Goog-Upload-Header-Content-Length: <size>
     body {"customer_id", "you_tube_video_upload": {video_title, video_description, video_privacy: UNLISTED}}
     -> response headers x-goog-upload-url (the session) and x-goog-upload-chunk-granularity
  2. PUT the bytes to the session URL in chunks (X-Goog-Upload-Command: upload, X-Goog-Upload-Offset), the last chunk
     with "upload, finalize" -> {"resourceName": "customers/<cid>/youTubeVideoUploads/<id>"}
  3. after a failed chunk: POST X-Goog-Upload-Command: query -> x-goog-upload-size-received, and go on from there
Then Google processes the video (state PENDING -> UPLOADED -> PROCESSED, or FAILED / REJECTED); status() reads it with
GAQL. Once PROCESSED it has a YouTube video id for a video_asset or a Demand Gen video ad.

The session URL carries the bearer token, so it is only ever sent to https://googleads.googleapis.com, and no request
here follows an HTTP redirect (a redirect would carry the token to wherever it points): a 3xx answer stops the upload.
An upload can't be undone from Studio: removing it (RemoveYouTubeVideoUpload) deletes the video from YouTube too and is
done in Google Ads.
"""
import datetime as dt, json, re, time, urllib.error, urllib.parse, urllib.request, uuid
from pathlib import Path

import core
import demandgen
import progress

HOST = "googleads.googleapis.com"
CHUNK = 8 * 1024 * 1024  # rounded to Google's chunk granularity
GRAN_MAX = 64 * 1024 * 1024  # the most granularity Studio takes from Google's header (one chunk is held in memory)
STAGE_KEEP_DAYS = 7  # staged copies older than this are swept at start-up (unless a draft item still points at one)
RESOURCE = re.compile(r"^customers/\d+/youTubeVideoUploads/[\d~]+$", re.ASCII)  # ASCII: \d is never another script's digit
RETRIES = 3
TIMEOUT = 300


class UploadError(RuntimeError):
    """final: Google refused the file itself (or closed the session), so trying the same bytes again won't help."""

    def __init__(self, msg, final=False):
        super().__init__(msg)
        self.final = final


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Every request here carries the bearer token: a redirect is never followed, it stops the upload."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise UploadError(f"Google answered with a redirect ({code}); Studio doesn't follow redirects with your sign-in, "
                          "so nothing more was sent")


_OPENER = urllib.request.build_opener(_NoRedirect)


def _api():
    import live  # noqa: PLC0415
    return live


def _log(acct):
    core.STUDIO_DIR.mkdir(parents=True, exist_ok=True)
    return core.STUDIO_DIR / f"youtube_uploads_{acct}.jsonl"


def start_url(cid):
    return f"https://{HOST}/resumable/upload/{_api().gads.g.API_VERSION}/customers/{cid}/youTubeVideoUploads:create"


def _safe_session(url):
    """The upload session URL Google handed back: must be https on the Google Ads host (it gets the bearer token)."""
    u = urllib.parse.urlsplit(url or "")
    if u.scheme != "https" or u.hostname != HOST or u.username or u.password or u.port not in (None, 443):
        raise UploadError("Google answered with an upload address Studio doesn't trust; nothing was sent")
    return url


def _open(req, timeout=TIMEOUT):
    """Open a request without following redirects (_NoRedirect); tests replace it."""
    return _OPENER.open(req, timeout=timeout)


def _headers(extra):
    h = dict(_api().gads.headers())
    h.pop("Content-Type", None)
    h.update(extra)
    return h


def _err(e):
    try:
        raw = e.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        raw = ""
    try:
        body = json.loads(raw)
        body = body[0] if isinstance(body, list) else body
        errs = (body["error"].get("details") or [{}])[0].get("errors") or []
        return "; ".join(x.get("message", "") for x in errs) or body["error"].get("message") or raw[:500]
    except (ValueError, KeyError, TypeError, IndexError, AttributeError):
        return raw[:500] or str(e)


def begin(cid, title, description, size, disclosure=None):
    """Step 1: open an upload session. -> (session url, chunk granularity)."""
    up = {"video_title": title, "video_description": description or "", "video_privacy": "UNLISTED"}
    if disclosure in ("non_generative", "generative"):
        up["video_content_metadata"] = {"generative_content_disclosure": f"{disclosure.upper()}_CONTENT"}
    body = json.dumps({"customer_id": cid, "you_tube_video_upload": up}).encode()
    req = urllib.request.Request(start_url(cid), body, _headers({
        "Content-Type": "application/json", "X-Goog-Upload-Protocol": "resumable", "X-Goog-Upload-Command": "start",
        "X-Goog-Upload-Header-Content-Length": str(size)}), method="POST")
    try:
        r = _open(req, 120)
    except urllib.error.HTTPError as e:
        raise UploadError("Google refused the upload: " + _err(e)) from None
    url = r.headers.get("x-goog-upload-url")
    if not url:
        raise UploadError("Google didn't open an upload session")
    try:
        gran = int(r.headers.get("x-goog-upload-chunk-granularity") or 0)
    except ValueError:
        gran = 0
    return _safe_session(url), min(max(gran, 0), GRAN_MAX)


def _chunk_size(gran):
    gran = min(gran or 0, GRAN_MAX)
    if gran > 0:
        return max(gran, (CHUNK // gran) * gran)
    return CHUNK


def _query(url):
    req = urllib.request.Request(_safe_session(url), b"", _headers({"X-Goog-Upload-Command": "query"}), method="POST")
    r = _open(req, 60)
    status = (r.headers.get("x-goog-upload-status") or "").lower()
    return int(r.headers.get("x-goog-upload-size-received") or 0), status, r


def send(url, path, size, gran, on_progress=None):
    """Step 2 (+3 on errors): send the file in chunks, the last one finalises. -> the response JSON."""
    step = _chunk_size(gran)
    offset, tries = 0, 0
    with open(path, "rb") as f:
        while True:
            f.seek(offset)
            data = f.read(step)
            last = offset + len(data) >= size
            req = urllib.request.Request(_safe_session(url), data, _headers({
                "X-Goog-Upload-Command": "upload, finalize" if last else "upload", "X-Goog-Upload-Offset": str(offset),
                "Content-Length": str(len(data))}), method="PUT")
            try:
                r = _open(req)
                tries = 0
                if last:
                    raw = r.read() or b"{}"
                    try:
                        return json.loads(raw)
                    except ValueError:
                        raise UploadError("Google's answer to the last chunk wasn't readable") from None
                offset += len(data)
                if on_progress:
                    on_progress(offset, size)
            except UploadError:
                raise
            except urllib.error.HTTPError as e:
                if e.code < 500 and e.code not in (408, 429):
                    raise UploadError("Google refused the upload: " + _err(e), final=True) from None
                tries += 1
                offset = _resume(url, tries, e)
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                tries += 1
                offset = _resume(url, tries, e)


def _resume(url, tries, why):
    if tries > RETRIES:
        raise UploadError(f"the upload stopped ({str(why)[:200]}) and didn't resume after {RETRIES} tries")
    time.sleep(min(2 ** tries, 20))
    got, status, _ = _query(url)
    if status == "final":
        raise UploadError("Google closed the upload session; start the upload again", final=True)
    return got


def status(acct, resource):
    """Processing state of an upload (GAQL, read-only): {state, video_id}."""
    cid = core.resolve(acct)
    resource = str(resource or "")
    if not RESOURCE.fullmatch(resource) or not resource.startswith(f"customers/{cid}/"):
        raise ValueError("not an upload of this account")  # fully matched before it goes into GAQL
    rows = _api().query(cid, "SELECT you_tube_video_upload.resource_name, you_tube_video_upload.video_id, you_tube_video_upload.state "
                             f"FROM you_tube_video_upload WHERE you_tube_video_upload.resource_name = '{resource}'")
    u = rows[0]["youTubeVideoUpload"] if rows else {}
    return {"resource": resource, "state": u.get("state", "UNKNOWN"), "video_id": u.get("videoId", "")}


STAGE_MAX = 10 * 1024 ** 3  # a bigger file: give its full path instead of picking it in the page


def stage_dir():
    return core.STUDIO_DIR / "youtube_staging"


def stage(stream, length, name):
    """Copy a video picked in the page (the request body, `length` bytes) into the staging folder, in 1 MB pieces.
    -> {file, name, size}. The file is deleted after a successful upload."""
    ext = Path(str(name or "")).suffix.lower()
    if ext not in demandgen.UPLOAD_EXT:
        raise ValueError(f"pick a video file ({', '.join(demandgen.UPLOAD_EXT)})")
    if length <= 0:
        raise ValueError("the file is empty")
    if length > STAGE_MAX:
        raise ValueError("files over 10 GB: give the file's full path instead (Finder: Option + right-click, Copy as Pathname)")
    d = stage_dir()
    d.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._ -]+", "_", Path(str(name)).stem)[:60].strip() or "video"
    path = d / f"{dt.datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}-{safe}{ext}"
    part = path.with_suffix(ext + ".part")
    left = length
    try:
        with open(part, "wb") as f:
            while left > 0:
                buf = stream.read(min(1024 * 1024, left))
                if not buf:
                    raise ValueError("the file didn't arrive in full: try again")
                f.write(buf)
                left -= len(buf)
        part.replace(path)
    finally:
        if part.exists():
            part.unlink()
    return {"file": str(path), "name": Path(str(name)).name, "size": length}


def _staged(path):
    """The resolved path when `path` is a file inside the staging folder (symlinks resolved), else None."""
    try:
        p = Path(str(path)).expanduser().resolve()
        return p if stage_dir().resolve() in p.parents and p.is_file() else None
    except (OSError, ValueError, RuntimeError):
        return None


def _unstage(path):
    """Delete Studio's own staged copy. A file anywhere else (one the user pointed at) is never touched."""
    p = _staged(path)
    if p:
        try:
            p.unlink()
        except OSError:
            pass
        return True
    return False


def release(acct, removed, keep=None):
    """Draft items left the draft (removed, cleared, uploaded): delete their staged copies, unless another item still
    in that draft (`keep`, else read here) points at the same file. -> how many files were deleted."""
    files = [i["change"].get("file") for i in removed
             if isinstance(i, dict) and isinstance(i.get("change"), dict) and i["change"].get("type") == "video_upload"]
    files = [f for f in files if f and _staged(f)]
    if not files:
        return 0
    if keep is None:
        try:
            keep = core.draft(acct)["items"]
        except Exception:  # noqa: BLE001 - can't tell what is still used: keep the files (the start-up sweep gets them)
            return 0
    used = {_staged(i["change"].get("file")) for i in keep if isinstance(i, dict) and isinstance(i.get("change"), dict)
            and i["change"].get("type") == "video_upload"}
    return sum(1 for f in files if _staged(f) not in used and _unstage(f))


def sweep(days=STAGE_KEEP_DAYS, now=None):
    """At engine start: delete staged copies (and stray .part files) older than `days` that no Ads draft points at.
    Only files directly inside the staging folder are looked at. -> the names deleted."""
    d = stage_dir()
    if not d.is_dir():
        return []
    root = d.resolve()
    cutoff = (now or time.time()) - days * 86400
    used = ""
    for f in core.STUDIO_DIR.glob("draft_*.json"):
        try:
            used += f.read_text(errors="replace")
        except OSError:
            pass
    gone = []
    for p in d.iterdir():
        try:
            if p.is_symlink() or not p.is_file() or p.resolve().parent != root or p.stat().st_mtime >= cutoff:
                continue
            if p.name in used:
                continue
            p.unlink()
            gone.append(p.name)
        except OSError:
            continue
    return gone


def history(acct, limit=50):
    f = _log(core.resolve(acct))
    if not f.exists():
        return []
    recs = [json.loads(x) for x in f.read_text().splitlines() if x.strip()]
    return list(reversed(recs))[:limit]


def upload(acct, item_id, body=None):
    """Upload one video_upload draft item (the user's Upload button). Removes the item when Google has the file.
    body may carry _team_expect (team_reviews.single_action): the item must still be the approved content."""
    import team_reviews  # noqa: PLC0415
    cid = core.resolve(acct)
    item = next((i for i in core.draft(cid)["items"] if i["id"] == str(item_id)), None)
    if not item or item["change"].get("type") != "video_upload":
        raise ValueError("upload not found in the draft")
    why = team_reviews.recheck(body or {}, f"ads:{cid}", item["id"], item)
    if why:
        return {"ok": False, "message": why}
    c = item["change"]
    probs = demandgen.upload_problems(c)
    if probs:
        return {"ok": False, "message": "Not uploaded: " + "; ".join(probs), "problems": probs}
    path = Path(str(c["file"])).expanduser()
    shown = str(c.get("file_name") or path.name)
    size = path.stat().st_size
    key = cid
    progress.start(key, "upload", [(item["id"], item["summary"], 100)], "checking", "Opening an upload session with Google")
    res = None
    stamp = dt.datetime.now()
    rec = {"ts": stamp.isoformat(timespec="seconds"), "item": item["id"], "file": shown, "size": size, "title": c["title"],
           "ok": False}
    try:
        url, gran = begin(cid, c["title"], c.get("description") or "", size, c.get("ai_disclosure"))
        progress.phase(key, "sending", f"Uploading {shown} to YouTube (unlisted, Google-managed channel)")
        last = [0]

        def tick(done, total):
            pct = int(done * 100 / total) if total else 100
            if pct > last[0]:
                progress.sent(key, item["id"], pct - last[0])
                last[0] = pct
        out = send(url, path, size, gran, tick)
        resource = out.get("resourceName", "")
        if not resource.startswith(f"customers/{cid}/youTubeVideoUploads/"):
            raise UploadError("Google finished the upload but didn't say where it is")
        progress.sent(key)
        rec.update(ok=True, resource=resource)
        core.remove(cid, [item["id"]])  # also deletes a staged copy (release), unless another item uses the same file
        _unstage(path)
        res = {"ok": True, "resource": resource, "applied": 1,
               "message": f"Uploaded {shown}: Google is processing it (unlisted, on the account's Google-managed channel). "
                          "It shows in Videos once it has a YouTube id."}
        return res
    except (UploadError, urllib.error.URLError, OSError) as ex:
        rec["error"] = str(ex)[:500]
        msg = f"Not uploaded: {ex}"
        if getattr(ex, "final", False) and _staged(path):
            # Google refused the bytes themselves: Studio's copy is no use any more (a file the user pointed at stays)
            _unstage(path)
            msg += ". Studio's copy of the file was deleted: pick the file again to retry"
        res = {"ok": False, "message": msg}
        return res
    finally:
        with open(_log(cid), "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        progress.finish(key, bool(res and res.get("ok")), (res or {}).get("message", "Stopped with an error"))
