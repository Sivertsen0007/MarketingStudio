"""YouTube upload from the Mac (youtube_upload.py): the resumable protocol against a fake Google, the session URL check,
resume after a failed chunk, the draft item flow, staging a picked file, and the HTTP routes (guards, push lock).

Run from the engine folder:  python3 -m unittest tests/test_youtube_upload.py
Fakes only: every real network call is blocked for the whole module (urllib.request.urlopen raises), a temp MSTUDIO_HOME,
no Keychain. Nothing is ever uploaded.
"""
import os as _os, tempfile as _tf
_os.environ["MSTUDIO_HOME"] = _tf.mkdtemp(prefix="mstudio-test-")
_os.environ["MSTUDIO_KEYCHAIN"] = "0"
_os.environ["GAE_EDITOR_DIR"] = _tf.mkdtemp(prefix="gae-editor-test-")

import io
import json
import os
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core  # noqa: E402
import live  # noqa: E402
import progress  # noqa: E402
import youtube_upload as yu  # noqa: E402

A = "4444444444"
SESSION = "https://googleads.googleapis.com/resumable/upload/session/xyz"
TOKEN_HEADERS = {"Authorization": "Bearer TEST-TOKEN", "developer-token": "dev", "login-customer-id": "1", "Content-Type": "application/json"}


def _no_network(*a, **k):
    raise AssertionError("a test tried to reach the network")


class Resp:
    def __init__(self, headers=None, body=b""):
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.headers = type("H", (), {"get": lambda _s, k, d=None, h=self.headers: h.get(k.lower(), d)})()
        self.body = body

    def read(self):
        return self.body


class FakeUpload:
    """Google's side of a resumable upload. fail: {chunk number: HTTP code} answered once."""

    def __init__(self, gran=256, session=SESSION, fail=None, start_error=None):
        self.reqs, self.got, self.gran, self.session, self.fail, self.start_error = [], bytearray(), gran, session, dict(fail or {}), start_error
        self.puts = 0

    def __call__(self, req, timeout=None):
        hd = {k.lower(): v for k, v in req.header_items()}
        self.reqs.append((req.get_method(), req.full_url, hd, len(req.data or b"")))
        if req.full_url == yu.start_url(A):
            if self.start_error:
                raise urllib.error.HTTPError(req.full_url, self.start_error, "x", {}, io.BytesIO(json.dumps(
                    {"error": {"message": "bad", "details": [{"errors": [{"message": "Title is too long"}]}]}}).encode()))
            return Resp({"X-Goog-Upload-URL": self.session, "X-Goog-Upload-Chunk-Granularity": str(self.gran), "X-Goog-Upload-Status": "active"})
        assert req.full_url == self.session, req.full_url
        cmd = hd["x-goog-upload-command"]
        if cmd == "query":
            return Resp({"X-Goog-Upload-Size-Received": str(len(self.got)), "X-Goog-Upload-Status": "active"})
        self.puts += 1
        if self.puts in self.fail:
            code = self.fail.pop(self.puts)
            raise urllib.error.HTTPError(req.full_url, code, "x", {}, io.BytesIO(b'{"error":{"message":"busy"}}'))
        assert int(hd["x-goog-upload-offset"]) == len(self.got), (hd["x-goog-upload-offset"], len(self.got))
        self.got += req.data
        if "finalize" in cmd:
            return Resp({"X-Goog-Upload-Status": "final"}, json.dumps({"resourceName": f"customers/{A}/youTubeVideoUploads/77"}).encode())
        return Resp({"X-Goog-Upload-Status": "active"})


class Base(unittest.TestCase):
    def setUp(self):
        for p in (mock.patch.object(urllib.request, "urlopen", _no_network), mock.patch.object(live.gads, "headers", lambda: dict(TOKEN_HEADERS)),
                  mock.patch.object(yu.time, "sleep", lambda s: None), mock.patch.object(yu, "CHUNK", 1000)):
            p.start()
            self.addCleanup(p.stop)
        core.clear(A)
        self.dir = Path(_tf.mkdtemp())
        self.file = self.dir / "teaser.mp4"
        self.data = bytes(range(256)) * 10  # 2560 bytes: chunks of 768, 768, 768, 256
        self.file.write_bytes(self.data)

    def fake(self, **kw):
        f = FakeUpload(**kw)
        p = mock.patch.object(yu, "_open", f)
        p.start()
        self.addCleanup(p.stop)
        return f


class Protocol(Base):
    def test_begin(self):
        f = self.fake()
        url, gran = yu.begin(A, "Teaser", "About it", 2560, "non_generative")
        self.assertEqual((url, gran), (SESSION, 256))
        method, u, hd, n = f.reqs[0]
        self.assertEqual((method, u), ("POST", f"https://googleads.googleapis.com/resumable/upload/v26/customers/{A}/youTubeVideoUploads:create"))
        self.assertEqual((hd["x-goog-upload-protocol"], hd["x-goog-upload-command"], hd["x-goog-upload-header-content-length"]),
                         ("resumable", "start", "2560"))
        self.assertEqual(hd["content-type"], "application/json")
        self.assertEqual(hd["authorization"], "Bearer TEST-TOKEN")
        self.assertGreater(n, 0)

    def test_begin_body(self):
        seen = {}

        def opener(req, timeout=None):
            seen["body"] = json.loads(req.data)
            return Resp({"X-Goog-Upload-URL": SESSION})
        with mock.patch.object(yu, "_open", opener):
            yu.begin(A, "Teaser", "", 10)
        self.assertEqual(seen["body"], {"customer_id": A, "you_tube_video_upload": {"video_title": "Teaser", "video_description": "",
                                                                                     "video_privacy": "UNLISTED"}})
        with mock.patch.object(yu, "_open", opener):
            yu.begin(A, "Teaser", "", 10, "generative")
        self.assertEqual(seen["body"]["you_tube_video_upload"]["video_content_metadata"], {"generative_content_disclosure": "GENERATIVE_CONTENT"})
        self.assertNotIn("channel_id", json.dumps(seen["body"]))

    def test_untrusted_session_url_is_never_used(self):
        for bad in ("http://googleads.googleapis.com/x", "https://evil.example/x", "https://googleads.googleapis.com.evil.example/x",
                    "https://user:pw@googleads.googleapis.com/x", "https://googleads.googleapis.com:8443/x"):
            f = self.fake(session=bad)
            with self.assertRaises(yu.UploadError):
                yu.begin(A, "T", "", 10)
            self.assertEqual([r[1] for r in f.reqs], [yu.start_url(A)], "only the start request went out")

    def test_start_refused(self):
        self.fake(start_error=400)
        with self.assertRaises(yu.UploadError) as e:
            yu.begin(A, "T", "", 10)
        self.assertIn("Title is too long", str(e.exception))

    def test_chunks_and_finalize(self):
        f = self.fake()
        seen = []
        out = yu.send(SESSION, self.file, 2560, 256, lambda d, t: seen.append(d))
        self.assertEqual(out["resourceName"], f"customers/{A}/youTubeVideoUploads/77")
        self.assertEqual(bytes(f.got), self.data)
        puts = [(r[2]["x-goog-upload-command"], r[2]["x-goog-upload-offset"], r[3]) for r in f.reqs if r[0] == "PUT"]
        self.assertEqual(puts, [("upload", "0", 768), ("upload", "768", 768), ("upload", "1536", 768), ("upload, finalize", "2304", 256)])
        self.assertEqual(seen, [768, 1536, 2304])
        self.assertTrue(all(r[1] == SESSION for r in f.reqs))

    def test_no_granularity_uses_whole_chunks(self):
        f = self.fake()
        yu.send(SESSION, self.file, 2560, 0)
        self.assertEqual([r[3] for r in f.reqs], [1000, 1000, 560])

    def test_resume_after_a_failed_chunk(self):
        f = self.fake(fail={2: 503, 3: 500})
        out = yu.send(SESSION, self.file, 2560, 256)
        self.assertEqual(bytes(f.got), self.data)
        self.assertEqual(out["resourceName"], f"customers/{A}/youTubeVideoUploads/77")
        self.assertEqual([r[2]["x-goog-upload-command"] for r in f.reqs].count("query"), 2)

    def test_gives_up_after_retries(self):
        self.fake(fail={i: 503 for i in range(2, 10)})
        with self.assertRaises(yu.UploadError) as e:
            yu.send(SESSION, self.file, 2560, 256)
        self.assertIn("didn't resume", str(e.exception))

    def test_client_error_is_final(self):
        f = self.fake(fail={1: 403})
        with self.assertRaises(yu.UploadError):
            yu.send(SESSION, self.file, 2560, 256)
        self.assertEqual(len([r for r in f.reqs if r[0] == "PUT"]), 1)


class Flow(Base):
    def add(self, **kw):
        c = {"type": "video_upload", "file": str(self.file), "title": "Teaser", **kw}
        return core.add(A, [c])[0]["id"]

    def test_upload_removes_the_item_and_logs(self):
        f = self.fake()
        iid = self.add(description="Reef film")
        r = yu.upload(A, iid)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["resource"], f"customers/{A}/youTubeVideoUploads/77")
        self.assertEqual(core.draft(A)["items"], [])
        h = yu.history(A)[0]
        self.assertEqual((h["ok"], h["file"], h["size"], h["title"]), (True, "teaser.mp4", 2560, "Teaser"))
        self.assertNotIn("TEST-TOKEN", (core.STUDIO_DIR / f"youtube_uploads_{A}.jsonl").read_text())
        self.assertTrue(self.file.exists(), "a file the user pointed at is never deleted")
        pr = progress.get(A)
        self.assertEqual((pr["running"], pr["ok"], pr["kind"]), (False, True, "upload"))
        self.assertEqual(bytes(f.got), self.data)

    def test_failed_upload_keeps_the_item(self):
        self.fake(start_error=400)
        iid = self.add()
        r = yu.upload(A, iid)
        self.assertFalse(r["ok"])
        self.assertIn("Title is too long", r["message"])
        self.assertEqual([i["id"] for i in core.draft(A)["items"]], [iid])
        self.assertFalse(yu.history(A)[0]["ok"])
        self.assertFalse(progress.get(A)["ok"])

    def test_problems_stop_before_google(self):
        f = self.fake()
        iid = self.add(file=str(self.dir / "gone.mp4"))
        r = yu.upload(A, iid)
        self.assertFalse(r["ok"])
        self.assertIn("not found on this Mac", r["message"])
        self.assertEqual(f.reqs, [])

    def test_only_upload_items(self):
        self.fake()
        iid = core.add(A, [{"type": "dg_ad", "campaign": "C", "ad_group": "G", "ad_id": "1", "status": "paused"}])[0]["id"]
        with self.assertRaises(ValueError):
            yu.upload(A, iid)
        with self.assertRaises(ValueError):
            yu.upload(A, "nope")

    def test_changed_after_approval(self):
        f = self.fake()
        iid = self.add()
        r = yu.upload(A, iid, {"_team_expect": "not-the-hash"})
        self.assertFalse(r["ok"])
        self.assertIn("changed after it was approved", r["message"])
        self.assertEqual(f.reqs, [])

    def test_staged_file_is_cleaned_up(self):
        self.fake()
        st = yu.stage(io.BytesIO(self.data), len(self.data), "My clip.MP4")
        p = Path(st["file"])
        self.assertTrue(p.is_file() and p.parent == yu.stage_dir())
        self.assertEqual((p.read_bytes(), st["name"], st["size"]), (self.data, "My clip.MP4", 2560))
        iid = core.add(A, [{"type": "video_upload", "file": st["file"], "title": "Clip"}])[0]["id"]
        self.assertTrue(yu.upload(A, iid)["ok"])
        self.assertFalse(p.exists())

    def test_status(self):
        seen = []

        def q(acct, g):
            seen.append(g)
            return [{"youTubeVideoUpload": {"state": "PROCESSED", "videoId": "AAAAAAAAAAA"}}]
        with mock.patch.object(live, "query", q):
            self.assertEqual(yu.status(A, f"customers/{A}/youTubeVideoUploads/77"),
                             {"resource": f"customers/{A}/youTubeVideoUploads/77", "state": "PROCESSED", "video_id": "AAAAAAAAAAA"})
            for bad in ("customers/1/youTubeVideoUploads/77", f"customers/{A}/youTubeVideoUploads/7' OR 1=1", f"customers/{A}/campaigns/1", ""):
                with self.assertRaises(ValueError):
                    yu.status(A, bad)
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0].startswith("SELECT "))


class Security(Base):
    """a10: no redirects with the token, GAQL resource fully matched, staged copies cleaned up, granularity capped."""

    def setUp(self):
        super().setUp()
        import shutil
        shutil.rmtree(yu.stage_dir(), ignore_errors=True)
        self.addCleanup(shutil.rmtree, yu.stage_dir(), True)
        for f in ("outside.mp4", "keep.mp4"):
            self.addCleanup(lambda f=f: (yu.stage_dir().parent / f).unlink() if (yu.stage_dir().parent / f).exists() else None)

    def test_redirect_handler_refuses(self):
        h = [x for x in yu._OPENER.handlers if isinstance(x, urllib.request.HTTPRedirectHandler)]
        self.assertEqual([type(x) for x in h], [yu._NoRedirect])
        req = urllib.request.Request(SESSION, b"x", {"Authorization": "Bearer TEST-TOKEN"}, method="PUT")
        with self.assertRaises(yu.UploadError):
            h[0].redirect_request(req, None, 302, "Found", {}, "https://evil.example/steal")

    def test_a_redirect_is_never_followed(self):
        """A real local HTTP server (127.0.0.1 only) answers 302/307 to another local path: the opener stops, the second
        path is never asked and the token never goes anywhere else."""
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        hits = []

        class Redirector(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _go(self):
                hits.append((self.command, self.path, self.headers.get("Authorization")))
                if self.path.startswith("/start"):
                    self.send_response(307 if self.command == "PUT" else 302)
                    self.send_header("Location", "/stolen")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.send_header("Content-Length", "2")
                    self.end_headers()
                    self.wfile.write(b"{}")
            do_GET = do_POST = do_PUT = _go
        srv = ThreadingHTTPServer(("127.0.0.1", 0), Redirector)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        for method in ("POST", "PUT", "GET"):
            req = urllib.request.Request(base + "/start", b"" if method != "GET" else None, {"Authorization": "Bearer TEST-TOKEN"}, method=method)
            with self.assertRaises(yu.UploadError):
                yu._open(req, 5)
        self.assertEqual([h[1] for h in hits], ["/start"] * 3)

    def test_redirect_during_upload_is_an_upload_error(self):
        def redirected(req, timeout=None):
            raise yu.UploadError("Google answered with a redirect (302)")
        with mock.patch.object(yu, "_open", redirected):
            with self.assertRaises(yu.UploadError):
                yu.begin(A, "T", "", 10)
            with self.assertRaises(yu.UploadError):
                yu.send(SESSION, self.file, 2560, 256)
            with self.assertRaises(yu.UploadError):
                yu._query(SESSION)

    def test_status_resource_fully_matched(self):
        seen = []
        with mock.patch.object(live, "query", lambda acct, g: seen.append(g) or []):
            self.assertEqual(yu.status(A, f"customers/{A}/youTubeVideoUploads/12~34")["state"], "UNKNOWN")
            for bad in (f"customers/{A}/youTubeVideoUploads/77\n", f"customers/{A}/youTubeVideoUploads/77' OR '1'='1",
                        f"customers/{A}/youTubeVideoUploads/", f"customers/{A}/youTubeVideoUploads/7/8", f"customers/{A}x/youTubeVideoUploads/7",
                        f" customers/{A}/youTubeVideoUploads/7", f"customers/{A}/youTubeVideoUploads/\u0661", None):
                with self.assertRaises(ValueError, msg=repr(bad)):
                    yu.status(A, bad)
        self.assertEqual(len(seen), 1)

    def test_granularity_capped(self):
        def big(req, timeout=None):
            return Resp({"X-Goog-Upload-URL": SESSION, "X-Goog-Upload-Chunk-Granularity": str(4 * 1024 ** 3)})
        with mock.patch.object(yu, "_open", big):
            self.assertEqual(yu.begin(A, "T", "", 10), (SESSION, yu.GRAN_MAX))
        self.assertEqual(yu._chunk_size(10 * 1024 ** 3), yu.GRAN_MAX)
        self.assertEqual(yu._chunk_size(-5), yu.CHUNK)
        with mock.patch.object(yu, "_open", lambda req, timeout=None: Resp({"X-Goog-Upload-URL": SESSION, "X-Goog-Upload-Chunk-Granularity": "-9"})):
            self.assertEqual(yu.begin(A, "T", "", 10)[1], 0)

    def staged(self, name="clip.mp4"):
        return Path(yu.stage(io.BytesIO(self.data), len(self.data), name)["file"])

    def test_removing_the_item_deletes_the_staged_copy(self):
        p = self.staged()
        iid = core.add(A, [{"type": "video_upload", "file": str(p), "title": "Clip"}])[0]["id"]
        core.remove(A, [iid])
        self.assertFalse(p.exists())
        p2 = self.staged()
        core.add(A, [{"type": "video_upload", "file": str(p2), "title": "Clip"}, {"type": "video_upload", "file": str(p2), "title": "Same file"}])
        first = core.draft(A)["items"][0]["id"]
        core.remove(A, [first])
        self.assertTrue(p2.exists(), "another item still uses the file")
        core.clear(A)
        self.assertFalse(p2.exists())

    def test_files_outside_staging_are_never_deleted(self):
        iid = core.add(A, [{"type": "video_upload", "file": str(self.file), "title": "Mine"}])[0]["id"]
        core.remove(A, [iid])
        self.assertTrue(self.file.exists())
        sneaky = yu.stage_dir() / ".." / "youtube_staging" / ".." / "keep.mp4"
        sneaky.resolve().write_bytes(b"x")
        self.assertEqual(yu.release(A, [{"change": {"type": "video_upload", "file": str(sneaky)}}], []), 0)
        self.assertTrue(sneaky.resolve().exists())
        yu.stage_dir().mkdir(parents=True, exist_ok=True)
        link = yu.stage_dir() / "link.mp4"
        link.symlink_to(self.file)
        self.assertEqual(yu.release(A, [{"change": {"type": "video_upload", "file": str(link)}}], []), 0)
        self.assertTrue(self.file.exists())

    def test_final_refusal_deletes_the_staged_copy(self):
        p = self.staged()
        iid = core.add(A, [{"type": "video_upload", "file": str(p), "title": "Clip"}])[0]["id"]
        self.fake(fail={1: 400})
        r = yu.upload(A, iid)
        self.assertFalse(r["ok"])
        self.assertIn("pick the file again", r["message"])
        self.assertFalse(p.exists())
        self.assertEqual(len(core.draft(A)["items"]), 1)

    def test_auth_and_rate_limit_refusals_keep_the_staged_copy(self):
        p = self.staged()
        iid = core.add(A, [{"type": "video_upload", "file": str(p), "title": "Clip"}])[0]["id"]
        for code in (401, 403):
            self.fake(fail={1: code})
            r = yu.upload(A, iid)
            self.assertFalse(r["ok"])
            self.assertNotIn("pick the file again", r["message"])
            self.assertTrue(p.exists(), code)
        self.fake(fail={i: 429 for i in range(1, 10)})  # rate limited every time: retried, then given up, not final
        self.assertFalse(yu.upload(A, iid)["ok"])
        self.assertTrue(p.exists())
        f = self.fake()  # after signing in again the same staged file uploads
        self.assertTrue(yu.upload(A, iid)["ok"])
        self.assertFalse(p.exists())

    def test_success_keeps_a_file_another_item_uses(self):
        p = self.staged()
        items = core.add(A, [{"type": "video_upload", "file": str(p), "title": "One"}, {"type": "video_upload", "file": str(p), "title": "Two"}])
        self.fake()
        self.assertTrue(yu.upload(A, items[0]["id"])["ok"])
        self.assertTrue(p.exists(), "item Two still needs the file")
        self.fake()
        self.assertTrue(yu.upload(A, items[1]["id"])["ok"])
        self.assertFalse(p.exists())

    def test_temporary_failures_keep_the_staged_copy(self):
        p = self.staged()
        iid = core.add(A, [{"type": "video_upload", "file": str(p), "title": "Clip"}])[0]["id"]
        self.fake(start_error=400)  # refused at the start: the title can be fixed, the file is still good
        self.assertFalse(yu.upload(A, iid)["ok"])
        self.assertTrue(p.exists())
        self.fake(fail={i: 503 for i in range(1, 10)})  # the connection kept dropping
        self.assertFalse(yu.upload(A, iid)["ok"])
        self.assertTrue(p.exists())

    def test_sweep_old_unused_staging_files(self):
        import time as _t
        old, used, fresh = self.staged("old.mp4"), self.staged("used.mp4"), self.staged("fresh.mp4")
        part = yu.stage_dir() / "x.mp4.part"
        part.write_bytes(b"x")
        outside = yu.stage_dir().parent / "outside.mp4"
        outside.write_bytes(b"x")
        link = yu.stage_dir() / "link.mp4"
        link.symlink_to(outside)
        week = _t.time() - 8 * 86400
        for f in (old, used, part, outside):
            os.utime(f, (week, week))
        os.utime(link, (week, week), follow_symlinks=False)
        core.add(A, [{"type": "video_upload", "file": str(used), "title": "Still waiting"}])
        gone = yu.sweep()
        self.assertEqual(sorted(gone), sorted([old.name, part.name]))
        self.assertFalse(old.exists())
        self.assertTrue(used.exists() and fresh.exists() and outside.exists() and link.is_symlink())

    def test_meta_post_has_no_video_routes(self):
        src = (Path(__file__).resolve().parents[1] / "server.py").read_text()
        meta_post = src.split("def _meta_post", 1)[1].split("\ndef ", 1)[0]
        self.assertNotIn("/api/videos", meta_post)
        self.assertNotIn("youtube_upload", meta_post)
        self.assertIn("youtube_upload.sweep()", src)


class Staging(Base):
    def test_rules(self):
        with self.assertRaises(ValueError):
            yu.stage(io.BytesIO(b"x"), 1, "notes.txt")
        with self.assertRaises(ValueError):
            yu.stage(io.BytesIO(b""), 0, "a.mp4")
        with self.assertRaises(ValueError):
            yu.stage(io.BytesIO(b"x"), yu.STAGE_MAX + 1, "a.mp4")
        with self.assertRaises(ValueError):
            yu.stage(io.BytesIO(b"short"), 100, "a.mp4")
        self.assertEqual([p.name for p in yu.stage_dir().glob("*.part")], [])
        st = yu.stage(io.BytesIO(b"abc"), 3, "../../evil/../x y?.mov")
        self.assertEqual(Path(st["file"]).parent, yu.stage_dir())


class Routes(Base):
    def test_http(self):
        import http.client
        import threading
        from http.server import ThreadingHTTPServer
        import server
        srv = ThreadingHTTPServer(("127.0.0.1", 0), server.H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_address[1]
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)

        def call(method, path, body=None, headers=None):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            try:
                c.request(method, path, body, headers or {})
                r = c.getresponse()
                return r.status, json.loads(r.read())
            except (ConnectionResetError, BrokenPipeError):
                # a refused upload closes the connection without reading the body: the client may see a reset
                # instead of the 403 / 400 (a race, not a pass): only refusals may end this way
                return "refused", {}
        js = lambda b: (json.dumps(b), {"Content-Type": "application/json", "X-Studio-Press": server.PRESS})  # noqa: E731
        # staging: Studio's own page only
        st, _ = call("POST", "/api/videos/stage?name=a.mp4", self.data, {"Content-Type": "application/octet-stream"})
        self.assertIn(st, (403, "refused"))
        st, _ = call("POST", "/api/videos/stage?name=a.mp4", self.data, {"Content-Type": "application/json", "X-Studio-Press": server.PRESS})
        self.assertIn(st, (403, "refused"))
        st, _ = call("POST", "/api/videos/stage?name=a.mp4", self.data, {"Content-Type": "application/octet-stream", "X-Studio-Press": server.PRESS,
                                                                         "Origin": "https://evil.example"})
        self.assertIn(st, (403, "refused"))
        st, res = call("POST", "/api/videos/stage?name=a.mp4", self.data, {"Content-Type": "application/octet-stream", "X-Studio-Press": server.PRESS})
        self.assertEqual((st, Path(res["file"]).read_bytes()), (200, self.data))
        st, _ = call("POST", "/api/videos/stage?name=a.exe", b"x", {"Content-Type": "application/octet-stream", "X-Studio-Press": server.PRESS})
        self.assertIn(st, (400, "refused"))
        # the upload button: through the push lock and team gate (solo here), then the fake Google
        st, items = call("POST", "/api/draft/add", *js({"acct": A, "changes": [{"type": "video_upload", "file": res["file"], "title": "T"}]}))
        self.assertEqual(st, 200)
        f = self.fake()
        server.PUSH_LOCK.acquire()
        try:
            st, r = call("POST", "/api/videos/upload", *js({"acct": A, "id": items[0]["id"]}))
            self.assertEqual(st, 400)
            self.assertIn("already running", r["error"])
            self.assertEqual(f.reqs, [])
        finally:
            server.PUSH_LOCK.release()
        st, r = call("POST", "/api/videos/upload", *js({"acct": A, "id": items[0]["id"]}))
        self.assertEqual((st, r["ok"]), (200, True), r)
        self.assertEqual(bytes(f.got), self.data)
        st, h = call("GET", f"/api/videos/uploads?acct={A}")
        self.assertEqual((st, h[0]["ok"]), (200, True))
        with mock.patch.object(server.demandgen, "library", lambda acct, refresh=False: {"account": acct, "refresh": refresh}):
            self.assertEqual(call("GET", f"/api/videos?acct={A}")[1], {"account": A, "refresh": False})
            self.assertEqual(call("POST", "/api/videos/refresh", *js({"acct": A}))[1], {"account": A, "refresh": True})
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request("GET", "/static/videos.js")
        self.assertEqual(c.getresponse().status, 200)

    def test_push_live_never_uploads(self):
        f = self.fake()
        core.add(A, [{"type": "video_upload", "file": str(self.file), "title": "T"}])
        with mock.patch.object(live, "mutate", lambda *a: self.fail("nothing to push")), mock.patch.object(live, "query", lambda *a: []):
            r = live.push(A)
        self.assertFalse(r["ok"])
        self.assertEqual(f.reqs, [])
        self.assertEqual(len(core.draft(A)["items"]), 1)


if __name__ == "__main__":
    unittest.main()
