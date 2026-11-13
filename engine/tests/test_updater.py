#!/usr/bin/env python3
"""Real end-to-end test for in-app updates (adapted from Mattdaemon's test_updater.py).

No mocks of the parts that matter: real app bundles signed with the real
signing certificate (and ad-hoc, for the ones that must be refused), real zips,
a real HTTP server standing in for GitHub's releases API, the real codesign
checks, and the real swap script swapping a real bundle. It proves:

  running from source (not inside a .app) is "nothing to do",
  a newer release is offered, an older one or none is not,
  a download that does not match GitHub's checksum is refused,
  a build signed by any other certificate is refused,
  a build that is not the version the release claims is refused,
  a build of a different app (bundle id) is refused,
  a copy with no certificate of its own is told it cannot self-update,
  a good release is installed: old bundle kept as backup, new one in place,
  the swap waits for the app to quit first,
  a failed swap puts the old app back.

Run (from engine/):  python3 tests/test_updater.py
            or:      python3 -m unittest tests/test_updater.py
macOS only; needs the signing identity on this Mac (MSTUDIO_SIGN_ID, default
"Mattdaemon Local Signing"), else it skips.
"""
import hashlib
import importlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ENGINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ENGINE)

SIGN_ID = os.environ.get("MSTUDIO_SIGN_ID", "Mattdaemon Local Signing")
BUNDLE_ID = "com.sivertsen.marketingstudio"
APP_NAME = "Marketing Studio.app"
ASSET = "MarketingStudio-macOS-arm64.zip"

passed = failed = 0


def check(cond, label):
    global passed, failed
    if cond:
        passed += 1
        print("  ok   " + label)
    else:
        failed += 1
        print("  FAIL " + label)


def make_app(path, version, sign, bundle_id=BUNDLE_ID):
    """A minimal real bundle: Info.plist + a Mach-O executable, signed."""
    shutil.rmtree(path, ignore_errors=True)
    os.makedirs(os.path.join(path, "Contents", "MacOS"))
    with open(os.path.join(path, "Contents", "Info.plist"), "wb") as f:
        plistlib.dump({"CFBundleIdentifier": bundle_id, "CFBundleExecutable": "MarketingStudio",
                       "CFBundleName": "Marketing Studio", "CFBundlePackageType": "APPL",
                       "CFBundleShortVersionString": version, "CFBundleVersion": version}, f)
    exe = os.path.join(path, "Contents", "MacOS", "MarketingStudio")
    shutil.copy("/usr/bin/true", exe)
    subprocess.run(["codesign", "--force", "--sign", sign, path],
                   check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def app_version(path):
    with open(os.path.join(path, "Contents", "Info.plist"), "rb") as f:
        return plistlib.load(f)["CFBundleShortVersionString"]


def zip_app(app, out):
    if os.path.exists(out):
        os.remove(out)
    subprocess.run(["ditto", "-c", "-k", "--keepParent", app, out], check=True)
    with open(out, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class Feed:
    """What GitHub's /releases/latest would say. Tests set it per case."""
    release = None          # dict, or None for 404
    files = {}              # url path -> local file


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/latest":
            if Feed.release is None:
                self.send_response(404)
                self.end_headers()
                return
            body = json.dumps(Feed.release).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/down":
            self.send_response(504)
            self.end_headers()
            return
        local = Feed.files.get(self.path)
        if not local:
            self.send_response(404)
            self.end_headers()
            return
        with open(local, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def release(base, version, url_path, sha, size, api_path=None):
    rel = {"tag_name": "v" + version, "draft": False, "prerelease": False,
           "body": "- Test notes for %s" % version, "html_url": base + "/page",
           "published_at": "2026-11-13T00:00:00Z",
           "assets": [{"name": ASSET, "size": size, "digest": "sha256:" + sha,
                       "browser_download_url": base + url_path}]}
    if api_path:
        rel["assets"][0]["url"] = base + api_path
    return rel


ENV_KEYS = ("MSTUDIO_UPDATE_FEED", "MSTUDIO_APP_PATH", "MSTUDIO_UPDATES_DIR",
            "MSTUDIO_UPDATE_NO_QUIT", "MSTUDIO_OPEN_CMD", "MSTUDIO_VERSION", "MSTUDIO_APP")


def main():
    if sys.platform != "darwin":
        print("SKIP: macOS only")
        return 0
    ids = subprocess.run(["security", "find-identity", "-v", "-p", "codesigning"],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         universal_newlines=True).stdout
    if SIGN_ID not in ids:
        print("SKIP: signing identity %r not on this Mac" % SIGN_ID)
        return 0

    saved_env = {k: os.environ.get(k) for k in ENV_KEYS}
    work = tempfile.mkdtemp(prefix="mstudio-updtest-")
    apps = os.path.join(work, "Applications")
    os.makedirs(apps)
    installed = os.path.join(apps, APP_NAME)
    upd_dir = os.path.join(work, "updates")
    fixtures = os.path.join(work, "fixtures")
    os.makedirs(fixtures)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % httpd.server_address[1]

    for k in ENV_KEYS:
        os.environ.pop(k, None)
    os.environ.update({
        "MSTUDIO_UPDATE_FEED": base + "/latest",
        "MSTUDIO_UPDATES_DIR": upd_dir,
        "MSTUDIO_UPDATE_NO_QUIT": "1",     # never ask a real app to quit
        "MSTUDIO_OPEN_CMD": "true",        # and never open one
        "MSTUDIO_VERSION": "0.1.0",
    })
    import updater as up
    importlib.reload(up)

    try:
        print("\nRUNNING FROM SOURCE")
        Feed.release = None
        check(up.app_bundle_path() is None, "the source checkout is not an app bundle")
        st = up.status(force=True)
        check(st["source"] and not st["available"] and st["canInstall"] is False,
              "status says nothing to do (and does not ask GitHub)")
        r = up.apply_update(app_pid=1)
        check(not r["ok"] and "nothing to do" in r["error"], "apply refuses: nothing to do")
        v = up.version_info()
        check(v["version"] == "0.1.0" and v["app"] is False, "version route: 0.1.0, not in the app")

        os.environ["MSTUDIO_APP_PATH"] = installed
        cur = up.current_version()
        pv = up.parse_version(cur)
        newer = "%d.%d.%d" % (pv[0], pv[1] + 1, 0)
        older = "0.0.1"

        def reset():
            up._job.update(phase="idle", progress=0, error=None, version=None)

        # fixtures: the installed copy, a good newer build, and the bad ones
        make_app(installed, cur, SIGN_ID)
        good = os.path.join(fixtures, "good", APP_NAME)
        make_app(good, newer, SIGN_ID)
        good_zip = os.path.join(fixtures, "good.zip")
        good_sha = zip_app(good, good_zip)
        adhoc = os.path.join(fixtures, "adhoc", APP_NAME)
        make_app(adhoc, newer, "-")
        adhoc_zip = os.path.join(fixtures, "adhoc.zip")
        adhoc_sha = zip_app(adhoc, adhoc_zip)
        liar = os.path.join(fixtures, "liar", APP_NAME)
        make_app(liar, "9.9.9", SIGN_ID)
        liar_zip = os.path.join(fixtures, "liar.zip")
        liar_sha = zip_app(liar, liar_zip)
        other = os.path.join(fixtures, "other", APP_NAME)
        make_app(other, newer, SIGN_ID, bundle_id="com.example.notstudio")
        other_zip = os.path.join(fixtures, "other.zip")
        other_sha = zip_app(other, other_zip)
        Feed.files = {"/good.zip": good_zip, "/adhoc.zip": adhoc_zip, "/liar.zip": liar_zip,
                      "/other.zip": other_zip}
        size = os.path.getsize

        print("\nCHECKING (running %s)" % cur)
        Feed.release = None
        st = up.status(force=True)
        check(not st["available"] and not st["error"], "no releases: nothing offered, no error")
        Feed.release = release(base, older, "/good.zip", good_sha, size(good_zip))
        st = up.status(force=True)
        check(not st["available"], "an older release is not offered")
        Feed.release = release(base, newer, "/good.zip", good_sha, size(good_zip))
        st = up.status(force=True)
        check(st["available"] and st["latest"] == newer, "a newer release is offered")
        check(st["canInstall"] is True, "a copy signed with the certificate can install it")
        check(st["release"]["notes"].startswith("- Test notes"), "the notes come along")

        print("\nREFUSING WHAT MUST BE REFUSED")
        reset()
        Feed.release = release(base, newer, "/good.zip", "0" * 64, size(good_zip))
        up.status(force=True)
        up.apply_update(app_pid=1, wait=True)
        check(up._job["phase"] == "error" and "checksum" in up._job["error"],
              "a download that does not match the checksum is refused")
        check(app_version(installed) == cur, "and the installed app is untouched")

        reset()
        Feed.release = release(base, newer, "/adhoc.zip", adhoc_sha, size(adhoc_zip))
        up.status(force=True)
        up.apply_update(app_pid=1, wait=True)
        check(up._job["phase"] == "error" and "certificate" in up._job["error"],
              "a build signed by anyone else is refused (%s)" % up._job["error"])
        check(app_version(installed) == cur, "and the installed app is untouched")

        reset()
        Feed.release = release(base, newer, "/liar.zip", liar_sha, size(liar_zip))
        up.status(force=True)
        up.apply_update(app_pid=1, wait=True)
        check(up._job["phase"] == "error" and "says it is" in up._job["error"],
              "a build that is not the version the release claims is refused")

        reset()
        Feed.release = release(base, newer, "/other.zip", other_sha, size(other_zip))
        up.status(force=True)
        up.apply_update(app_pid=1, wait=True)
        check(up._job["phase"] == "error" and "different app" in up._job["error"],
              "a build of a different app is refused")

        reset()
        make_app(installed, cur, "-")
        Feed.release = release(base, newer, "/good.zip", good_sha, size(good_zip))
        st = up.status(force=True)
        check(st["canInstall"] is False and "ad-hoc" in (st["cannotInstall"] or ""),
              "an ad-hoc copy is told it cannot update itself")
        r = up.apply_update(app_pid=1)
        check(not r["ok"], "and apply refuses for it")
        make_app(installed, cur, SIGN_ID)

        print("\nGITHUB HAVING A BAD MOMENT")
        reset()
        Feed.release = release(base, newer, "/down", good_sha, size(good_zip), api_path="/good.zip")
        up.status(force=True)
        with up._open_download(up._check_cache["release"], pause=0) as r:
            got = r.read()
        check(hashlib.sha256(got).hexdigest() == good_sha,
              "a 504 on the browser link falls back to the API route")
        Feed.release = release(base, newer, "/down", good_sha, size(good_zip), api_path="/down")
        up.status(force=True)
        try:
            up._open_download(up._check_cache["release"], pause=0)
            check(False, "both routes down: the download reports the error")
        except Exception as e:  # noqa: BLE001
            check("504" in str(e), "both routes down: the download reports the error")
        Feed.release = release(base, newer, "/good.zip", good_sha, size(good_zip))

        print("\nINSTALLING A GOOD RELEASE")
        reset()
        fake_app = subprocess.Popen(["sleep", "30"])   # stands in for the running app
        up.status(force=True)
        r = up.apply_update(app_pid=fake_app.pid, wait=True)
        check(r["ok"] and up._job["phase"] == "installing",
              "download + checks pass, the swap is handed over (%s)" % up._job.get("error"))
        time.sleep(2.5)
        check(app_version(installed) == cur, "the swap waits while the app is still running")
        fake_app.terminate()
        fake_app.wait()
        end = time.time() + 20
        while time.time() < end and app_version(installed) != newer:
            time.sleep(0.2)
        check(app_version(installed) == newer, "after it quits, the new version is installed")
        ok = subprocess.run(["codesign", "--verify", "--deep", "--strict", installed]).returncode == 0
        check(ok, "the installed app's signature is intact")
        backup = os.path.join(upd_dir, "MarketingStudio-previous.app")
        check(os.path.isdir(backup) and app_version(backup) == cur, "the old app is kept as a backup")
        with open(os.path.join(upd_dir, "state.json")) as f:
            state = json.load(f)
        check(state.get("justUpdated", {}).get("version") == newer,
              "the new copy will say what it was updated to")

        print("\nA FAILED SWAP PUTS THE OLD APP BACK")
        make_app(installed, cur, SIGN_ID)
        script = os.path.join(upd_dir, "swap.sh")
        r = subprocess.run(["/bin/bash", script, installed, os.path.join(work, "nope.app"),
                            backup, "999999", ""], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, env=dict(os.environ, MSTUDIO_OPEN_CMD="true"))
        check(r.returncode != 0, "the swap reports failure")
        check(os.path.isdir(installed) and app_version(installed) == cur,
              "and the old app is back in place")
    finally:
        httpd.shutdown()
        shutil.rmtree(work, ignore_errors=True)
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    print("\n%d of %d checks passed." % (passed, passed + failed) if not failed
          else "\n%d of %d checks FAILED." % (failed, passed + failed))
    return 1 if failed else 0


class UpdaterEndToEnd(unittest.TestCase):
    """So `python3 -m unittest tests/test_updater.py` runs it too."""

    def test_updater(self):
        self.assertEqual(main(), 0)


if __name__ == "__main__":
    sys.exit(main())
