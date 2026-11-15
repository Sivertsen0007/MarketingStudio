"""Where Marketing Studio keeps its data and its keys - per Mac user, never inside the app or the repo.

  ~/Library/Application Support/MarketingStudio/
      data/          drafts, push logs, caches, settings (Studio's working files)
      config.json    non-secret settings: account ids, URLs
  macOS Keychain     the secrets (service "Marketing Studio"): tokens, passwords, Google sign-in

Every module asks get("NAME"). Lookup order: the process environment (tests, dev), the Keychain
(secrets) or config.json (the rest). Nothing here ever logs or returns a secret's value to the UI;
status() only says whether each key is set.

Google: each person signs in with Google once (OAuth, loopback redirect to this engine). The refresh
token goes into the Keychain and access tokens are minted with the standard library - no Google
libraries needed. A service-account key (the old AI-Hub way) still works when google-auth is installed.

MSTUDIO_HOME moves the whole folder (tests). MSTUDIO_KEYCHAIN=0 keeps secrets in a chmod-600
secrets.json in that folder instead of the Keychain (tests, or a Mac without `security`).
"""
import json
import os
import secrets as _secrets
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import sys
from pathlib import Path

_VENDOR = str(Path(__file__).resolve().parent / "vendor_hub")
if _VENDOR not in sys.path:
    sys.path.insert(0, _VENDOR)

HOME = Path(os.environ.get("MSTUDIO_HOME") or Path.home() / "Library/Application Support/MarketingStudio")
DATA = HOME / "data"
CONFIG = HOME / "config.json"
SECRETS_FILE = HOME / "secrets.json"
SERVICE = "Marketing Studio"
_lock = threading.RLock()
_cache = {}
_listeners = []

# name, label, group, secret, help
KEYS = [
    ("META_ACCESS_TOKEN", "Access token", "Meta", True, "A system user token with ads_management (Business Settings > System users), or your own long-lived token."),
    ("META_AD_ACCOUNT_ID", "Ad account id", "Meta", False, "act_ followed by the number, e.g. act_1234567890. More accounts can be added in Settings."),
    ("META_PAGE_ID", "Facebook Page id", "Meta", False, "The Page your ads and posts run as (optional)."),
    ("GADS_DEVELOPER_TOKEN", "Developer token", "Google Ads", True, "From the manager account: Tools > API Center."),
    ("GADS_LOGIN_CUSTOMER_ID", "Manager account id (MCC)", "Google Ads", False, "Digits only, no dashes."),
    ("GADS_OPERATING_CUSTOMER_ID", "Default account id", "Google Ads", False, "The account Studio opens first. Digits only."),
    ("GOOGLE_CLIENT_ID", "OAuth client id", "Google sign-in", False, "A Google Cloud OAuth client of type Desktop app. Your admin can give you this."),
    ("GOOGLE_CLIENT_SECRET", "OAuth client secret", "Google sign-in", True, "Belongs to the client id above."),
    ("EMAIL_PLATFORM_NAME", "Platform name", "Email", False, "Your email platform's name, shown in buttons (Save to ...). Optional."),
    ("EMAIL_API_URL", "Email API URL", "Email", False, "The platform's Marketing Studio Email API, e.g. https://mail.example.com/api/studio/email. Full access: edit, test, schedule, send."),
    ("EMAIL_API_TOKEN", "Email API token", "Email", True, "Bearer token for the Email API."),
    ("EMAIL_MCP_URL", "MCP URL", "Email", False, "The platform's MCP server over HTTP, e.g. https://mail.example.com/api/mcp. Read access, plus new drafts."),
    ("EMAIL_MCP_TOKEN", "MCP token", "Email", True, "Bearer token for the MCP server."),
    ("EMAIL_FROM_ADDRESS", "From address", "Email", False, "Optional: every campaign must use this From and Reply-to (a check). Empty = the platform decides."),
    ("EMAIL_OWN_DOMAINS", "Own domains", "Email", False, "Optional, comma separated: domains whose addresses may be shown (the From domain always is)."),
    ("STUDIO_TEST_EMAIL", "Test email addresses", "Email", False, "One or more, separated by commas. With one, Send test goes there; with several you pick one when you send. The platform may only accept internal addresses."),
    ("EMAIL_TIMEZONE", "Send timezone", "Email", False, "IANA name, e.g. Europe/London (default)."),
    ("EMAIL_IMAGE_HOST", "Image host", "Email", False, "Optional: warn when an image is not on this host, e.g. media.example.com."),
    ("WP_STUDIO_USER", "WordPress user", "WordPress (SEO)", False, "The user that owns the application password."),
    ("WP_STUDIO_APP_PASSWORD", "Application password", "WordPress (SEO)", True, "Users > Profile > Application passwords."),
    ("LINKEDIN_ACCESS_TOKEN", "Access token", "LinkedIn", True, "Optional."),
    ("LINKEDIN_ORG_URN", "Organisation URN", "LinkedIn", False, "urn:li:organization:<id>"),
    ("STUDIO_EMAIL_FOLDER", "Folder for saved email HTML", "Advanced", False, "Where Save HTML to folder writes. Default: Studio's data folder."),
    ("STUDIO_IMAGE_LIBRARY", "R2 image library folder", "Advanced", False, "Optional: a folder with r2_client.py, for uploading local SoMe media."),
]
# set by Sign in with Google / Import, not typed in the form
HIDDEN_SECRETS = ("GOOGLE_REFRESH_TOKEN", "GOOGLE_SA_JSON", "TEAM_DEVICE_TOKEN")  # TEAM_: team.py (join a team)
SECRET_NAMES = {k[0] for k in KEYS if k[3]} | set(HIDDEN_SECRETS)
GOOGLE_SCOPES = ["https://www.googleapis.com/auth/adwords", "https://www.googleapis.com/auth/webmasters.readonly",
                 "https://www.googleapis.com/auth/analytics.readonly", "openid", "email"]


# ---------- storage ----------

def _use_keychain():
    return os.environ.get("MSTUDIO_KEYCHAIN", "1") != "0" and shutil.which("security") is not None


def _read_json(p):
    try:
        return json.loads(Path(p).read_text())
    except (OSError, ValueError):
        return {}


def _write_json(p, data, private=False):
    HOME.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(p) + ".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    if private:
        os.chmod(tmp, 0o600)
    tmp.replace(p)


def _kc_quote(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _kc_get(name):
    try:
        r = subprocess.run(["security", "find-generic-password", "-s", SERVICE, "-a", name, "-w"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.rstrip("\n") if r.returncode == 0 else None


def _kc_set(name, value):
    # through `security -i` on stdin, so the secret never shows up in the process list
    if value is None:
        subprocess.run(["security", "delete-generic-password", "-s", SERVICE, "-a", name], capture_output=True, timeout=10)
        return
    if "\n" in value:
        raise ValueError("a key can't contain a line break")
    cmd = f"add-generic-password -U -s {_kc_quote(SERVICE)} -a {_kc_quote(name)} -w {_kc_quote(value)}\n"
    r = subprocess.run(["security", "-i"], input=cmd, capture_output=True, text=True, timeout=10)
    if r.returncode != 0 or "error" in (r.stderr or "").lower():
        raise RuntimeError("macOS Keychain refused to save the key")


def _secret_get(name):
    if _use_keychain():
        return _kc_get(name)
    return _read_json(SECRETS_FILE).get(name)


def _secret_set(name, value):
    if _use_keychain():
        _kc_set(name, value)
        return
    d = _read_json(SECRETS_FILE)
    if value is None:
        d.pop(name, None)
    else:
        d[name] = value
    _write_json(SECRETS_FILE, d, private=True)


# ---------- public API ----------

def get(name, default=""):
    """The value of a key, or default. Environment first, then Keychain / config.json."""
    v = os.environ.get(name)
    if v:
        return v
    with _lock:
        if name not in _cache:
            _cache[name] = _secret_get(name) if name in SECRET_NAMES else _read_json(CONFIG).get(name)
        v = _cache[name]
    return v if v else default


def set_many(values):
    """{name: value or None (remove)}. Blank secrets are ignored (the form leaves them blank to keep them)."""
    with _lock:
        cfg = _read_json(CONFIG)
        for k, v in values.items():
            v = None if v is None else str(v).strip()
            if k in SECRET_NAMES:
                if v == "":
                    continue
                _secret_set(k, v)
            else:
                if v:
                    cfg[k] = v
                else:
                    cfg.pop(k, None)
            _cache.pop(k, None)
        _write_json(CONFIG, cfg)
    _notify()


def set_secret(name, value):
    """Save (or with None remove) one hidden secret, e.g. the team device token. Never logged or returned."""
    if name not in HIDDEN_SECRETS:
        raise ValueError(f"{name} is not a hidden secret")
    with _lock:
        _secret_set(name, value)
        _cache.pop(name, None)


def on_change(fn):
    """fn() runs after keys change (modules that read a key at import refresh themselves)."""
    _listeners.append(fn)


def _notify():
    _GOOGLE["token"], _GOOGLE["exp"] = None, 0
    _SA["token"], _SA["exp"] = None, 0
    for fn in list(_listeners):
        try:
            fn()
        except Exception:  # noqa: BLE001 - one module failing to refresh must not block saving
            pass


def status():
    """For the setup screen: every key with whether it is set. Secrets: only set / not set, never the value."""
    out = []
    for name, label, group, secret, help_ in KEYS:
        v = get(name)
        out.append({"name": name, "label": label, "group": group, "secret": secret, "help": help_,
                    "set": bool(v), "value": "" if secret else v, "from_env": bool(os.environ.get(name))})
    return {"keys": out, "google_signed_in": bool(get("GOOGLE_REFRESH_TOKEN")), "google_account": get("GOOGLE_ACCOUNT_EMAIL"),
            "google_service_account": bool(get("GOOGLE_SA_JSON")), "home": str(HOME), "keychain": _use_keychain(),
            "ready": bool(get("META_ACCESS_TOKEN") or google_ready())}


def google_ready():
    return bool(get("GADS_DEVELOPER_TOKEN") and (get("GOOGLE_REFRESH_TOKEN") or get("GOOGLE_SA_JSON")))


# ---------- Google access tokens ----------

class NotConnected(RuntimeError):
    pass


_GOOGLE = {"token": None, "exp": 0}
_glock = threading.Lock()


def _post_form(url, fields):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(),
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error_description") or e.reason
        except ValueError:
            msg = e.reason
        raise RuntimeError(f"Google sign-in: {msg}") from None


_SA = {"token": None, "exp": 0}


def _from_refresh():
    if _GOOGLE["token"] and time.time() < _GOOGLE["exp"] - 120:
        return _GOOGLE["token"]
    r = _post_form("https://oauth2.googleapis.com/token", {
        "client_id": get("GOOGLE_CLIENT_ID"), "client_secret": get("GOOGLE_CLIENT_SECRET"),
        "refresh_token": get("GOOGLE_REFRESH_TOKEN"), "grant_type": "refresh_token"})
    _GOOGLE["token"], _GOOGLE["exp"] = r["access_token"], time.time() + int(r.get("expires_in", 3600))
    return _GOOGLE["token"]


def _from_service_account():
    if _SA["token"] and time.time() < _SA["exp"] - 120:
        return _SA["token"]
    try:
        from google.oauth2 import service_account  # noqa: PLC0415
        from google.auth.transport.requests import Request  # noqa: PLC0415
    except ImportError:
        raise NotConnected("This Mac has a service-account key but not the google-auth library (pip3 install google-auth). Or sign in with Google instead.") from None
    c = service_account.Credentials.from_service_account_info(json.loads(get("GOOGLE_SA_JSON")), scopes=[GOOGLE_SCOPES[0]])
    c.refresh(Request())
    _SA["token"], _SA["exp"] = c.token, time.time() + 3000
    return _SA["token"]


def google_token(purpose="ads"):
    """A Google access token, cached until it expires.
    purpose "ads": a service-account key wins when there is one (it is set up for Google Ads), else the sign-in.
    purpose "seo" (Search Console, GA4): the sign-in wins, else the service-account key."""
    with _glock:
        have_rt, have_sa = bool(get("GOOGLE_REFRESH_TOKEN")), bool(get("GOOGLE_SA_JSON"))
        order = [_from_service_account, _from_refresh] if purpose == "ads" else [_from_refresh, _from_service_account]
        for fn in order:
            if (fn is _from_refresh and have_rt) or (fn is _from_service_account and have_sa):
                return fn()
        raise NotConnected("Not signed in to Google. Open Setup and press Sign in with Google.")


_oauth_states = {}


def google_auth_url(redirect_uri):
    cid = get("GOOGLE_CLIENT_ID")
    if not cid or not get("GOOGLE_CLIENT_SECRET"):
        raise ValueError("Enter the Google OAuth client id and secret first (Setup > Google sign-in).")
    st = _secrets.token_urlsafe(16)
    _oauth_states[st] = (redirect_uri, time.time())
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": cid, "redirect_uri": redirect_uri, "response_type": "code", "scope": " ".join(GOOGLE_SCOPES),
        "access_type": "offline", "prompt": "consent", "state": st, "include_granted_scopes": "true"})


def google_finish(code, state):
    redirect_uri, at = _oauth_states.pop(state, (None, 0))
    if not redirect_uri or time.time() - at > 900:
        raise ValueError("This sign-in link has expired. Start again from Setup.")
    r = _post_form("https://oauth2.googleapis.com/token", {
        "client_id": get("GOOGLE_CLIENT_ID"), "client_secret": get("GOOGLE_CLIENT_SECRET"), "code": code,
        "redirect_uri": redirect_uri, "grant_type": "authorization_code"})
    if not r.get("refresh_token"):
        raise RuntimeError("Google gave no refresh token. Remove Marketing Studio at myaccount.google.com/permissions and sign in again.")
    email = ""
    try:
        import base64  # noqa: PLC0415
        part = r.get("id_token", "").split(".")[1]
        email = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))).get("email", "")
    except (IndexError, ValueError):
        pass
    _secret_set("GOOGLE_REFRESH_TOKEN", r["refresh_token"])
    with _lock:
        cfg = _read_json(CONFIG)
        cfg["GOOGLE_ACCOUNT_EMAIL"] = email
        _write_json(CONFIG, cfg)
        _cache.clear()
    _notify()
    return email


def google_sign_out():
    with _lock:
        _secret_set("GOOGLE_REFRESH_TOKEN", None)
        cfg = _read_json(CONFIG)
        cfg.pop("GOOGLE_ACCOUNT_EMAIL", None)
        _write_json(CONFIG, cfg)
        _cache.clear()
    _notify()


# ---------- importing keys from a .env file (Setup button, MCP tool, CLI) ----------

ALIASES = {  # other common spellings -> Studio's key
    "FB_ACCESS_TOKEN": "META_ACCESS_TOKEN", "FACEBOOK_ACCESS_TOKEN": "META_ACCESS_TOKEN",
    "META_ACCOUNT_ID": "META_AD_ACCOUNT_ID", "FB_AD_ACCOUNT_ID": "META_AD_ACCOUNT_ID",
    "GOOGLE_ADS_DEVELOPER_TOKEN": "GADS_DEVELOPER_TOKEN", "GOOGLE_ADS_LOGIN_CUSTOMER_ID": "GADS_LOGIN_CUSTOMER_ID",
    "GOOGLE_ADS_CUSTOMER_ID": "GADS_OPERATING_CUSTOMER_ID", "GOOGLE_ADS_CLIENT_ID": "GOOGLE_CLIENT_ID",
    "GOOGLE_ADS_CLIENT_SECRET": "GOOGLE_CLIENT_SECRET", "GOOGLE_ADS_REFRESH_TOKEN": "GOOGLE_REFRESH_TOKEN",
}
FILE_KEYS = ("GOOGLE_SERVICE_ACCOUNT_FILE", "GOOGLE_APPLICATION_CREDENTIALS", "GADS_DM_SA_KEY")  # path to a service-account JSON


def parse_env(text):
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:]
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def import_env(text, base_dir=None):
    """Save every key Studio knows from .env text into the Keychain / config.json.
    -> {"imported": [names], "ignored": [names]}. Values are never returned."""
    env, vals, ignored = parse_env(text), {}, []
    known = {k[0] for k in KEYS} | {"GOOGLE_REFRESH_TOKEN"}
    for k, v in env.items():
        name = ALIASES.get(k, k)
        if not v:
            continue
        if name in known:
            vals[name] = v
        elif k in FILE_KEYS:
            f = Path(v).expanduser()
            if not f.is_absolute() and base_dir:
                f = Path(base_dir) / f
            try:
                info = json.loads(f.read_text())
                if info.get("type") == "service_account":
                    vals["GOOGLE_SA_JSON"] = json.dumps(info)
                    continue
            except (OSError, ValueError):
                pass
            ignored.append(k)
        else:
            ignored.append(k)
    hidden = {k: vals.pop(k) for k in list(vals) if k in HIDDEN_SECRETS}
    set_many(vals)
    for k, v in hidden.items():
        _secret_set(k, v)
    with _lock:
        _cache.clear()
    _notify()
    return {"imported": sorted(list(vals) + list(hidden)), "ignored": sorted(ignored)}


def import_env_file(path):
    f = Path(path).expanduser()
    if not f.is_file():
        raise ValueError(f"No file at {f}")
    return import_env(f.read_text(), base_dir=f.parent)
