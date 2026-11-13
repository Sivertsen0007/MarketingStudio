"""SoMe <-> LinkedIn company page (Posts API + Images API, version li-lms-2026-10). NOT CONNECTED: we have no LinkedIn
developer app or token yet, so this is built and tested against a fake HTTP transport only.

Connect (admin, see ADMIN.md section J): a LinkedIn developer app with the Community Management API approved, a page admin
signs in once (OAuth, scope w_organization_social + r_organization_social), and the token + the organisation URN go in
Setup (Keychain) as LINKEDIN_ACCESS_TOKEN and LINKEDIN_ORG_URN (urn:li:organization:<id>). Studio never shows the token.

Calls (all need headers LinkedIn-Version: YYYYMM and X-Restli-Protocol-Version: 2.0.0):
- image: POST /rest/images?action=initializeUpload {initializeUploadRequest: {owner}} -> uploadUrl + image URN; PUT the bytes
- post: POST /rest/posts {author, commentary, visibility PUBLIC, distribution MAIN_FEED, lifecycleState PUBLISHED, content}
  -> 201, post URN in the x-restli-id header. content: media {id, altText} | multiImage {images} | article {source, title}
- delete: DELETE /rest/posts/{encoded urn}
LinkedIn has no scheduling in the API (the VPS job publishes at the time). Organic carousels are not supported (multi-image is).
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

try:
    from ._social_publish import Publisher, NotSupported
except ImportError:
    from _social_publish import Publisher, NotSupported

API = "https://api.linkedin.com/rest"
VERSION = "202610"


class NotConnected(NotSupported):
    pass


def creds():
    """(token, org_urn) from Setup (Keychain / config) or the environment; ("", "") when not connected."""
    import appconfig  # noqa: PLC0415
    return appconfig.get("LINKEDIN_ACCESS_TOKEN"), appconfig.get("LINKEDIN_ORG_URN")


def connected():
    t, o = creds()
    return bool(t and o)


def urllib_http(method, url, headers, body=None):
    """-> (status, headers dict lower-case, body bytes). Errors come back as a status, never with the token."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, e.read()
    except (urllib.error.URLError, OSError) as e:
        raise RuntimeError(f"no answer from LinkedIn ({getattr(e, 'reason', None) or e.__class__.__name__})") from None


class LinkedInPublisher(Publisher):
    channel = "linkedin"
    can_schedule = False

    def __init__(self, token=None, org=None, http=None):
        t, o = creds()
        self.token, self.org = token if token is not None else t, org if org is not None else o
        self.http = http or urllib_http

    def _need(self):
        if not (self.token and self.org):
            raise NotConnected("LinkedIn is not connected: see Connect LinkedIn in SoMe settings")

    def _h(self, extra=None):
        return {"Authorization": f"Bearer {self.token}", "LinkedIn-Version": VERSION, "X-Restli-Protocol-Version": "2.0.0",
                "Content-Type": "application/json", **(extra or {})}

    def _call(self, method, path, body=None, extra=None, ok=(200, 201, 204)):
        st, hd, raw = self.http(method, f"{API}/{path}", self._h(extra), json.dumps(body).encode() if body is not None else None)
        if st not in ok:
            try:
                msg = json.loads(raw or b"{}").get("message") or f"HTTP {st}"
            except ValueError:
                msg = f"HTTP {st}"
            raise RuntimeError(f"LinkedIn: {msg}")
        return hd, (json.loads(raw) if raw and raw.strip().startswith(b"{") else {})

    def test(self):
        """Read-only: can the token see the organisation?"""
        self._need()
        oid = self.org.rsplit(":", 1)[-1]
        _, b = self._call("GET", f"organizations/{oid}")
        return {"ok": True, "name": b.get("localizedName", "")}

    def _image(self, path):
        _, b = self._call("POST", "images?action=initializeUpload", {"initializeUploadRequest": {"owner": self.org}})
        v = b["value"]
        st, _, _ = self.http("PUT", v["uploadUrl"], {"Authorization": f"Bearer {self.token}"}, Path(path).read_bytes())
        if st not in (200, 201):
            raise RuntimeError(f"LinkedIn: image upload answered HTTP {st}")
        return v["image"]

    def body(self, job, images=()):
        b = {"author": self.org, "commentary": job["text"], "visibility": "PUBLIC",
             "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
             "lifecycleState": "PUBLISHED", "isReshareDisabledByAuthor": False}
        if len(images) == 1:
            b["content"] = {"media": {"id": images[0], **({"altText": job["alt_text"]} if job.get("alt_text") else {})}}
        elif images:
            b["content"] = {"multiImage": {"images": [{"id": i, **({"altText": job["alt_text"]} if job.get("alt_text") else {})} for i in images]}}
        elif job.get("link"):
            b["content"] = {"article": {"source": job["link"], "title": job.get("title") or job["link"]}}
        return b

    def publish(self, job, report):
        self._need()
        media = job.get("media") or []
        if any(m["kind"] == "video" for m in media):
            raise NotSupported("LinkedIn video posts are not built yet: post the video on LinkedIn itself")
        imgs = []
        for i, m in enumerate(media, 1):
            report("upload", f"Uploading image {i} of {len(media)} to LinkedIn")
            imgs.append(self._image(m["path"]))
        report("create", "Creating the LinkedIn post")
        hd, _ = self._call("POST", "posts", self.body(job, imgs), ok=(201,))
        urn = hd.get("x-restli-id", "")
        return {"post_id": urn, "url": f"https://www.linkedin.com/feed/update/{urn}/" if urn else ""}

    def delete(self, result):
        self._need()
        self._call("DELETE", "posts/" + urllib.parse.quote(result["post_id"], safe=""), extra={"X-RestLi-Method": "DELETE"})
        return {}
