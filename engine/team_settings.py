"""Team settings and the shared library.

Settings (one value per key, each a JSON object):
  copy_rules            {"rules": [{id, level: block|warn, pattern (regex, case-insensitive), message, areas: [..]}]}
                        areas: ads (Google + Meta), email, seo, social; empty = everywhere. "block" stops the action
                        (Ads, SoMe) or is an error (Email, SEO); "warn" is shown only.
  seo_sites             {"prod": url, "staging": url, "sites": [{id, name, on, note}]}  empty = Studio's built-in sites
  email_test_addresses  {"addresses": [..]}  empty = Setup > Email > Test email addresses
  social                {"slots": {channel: ["Tue 09:00"]}, "utm": {facebook, linkedin}, "utm_campaign", "late_minutes"}
                        any subset; the fields given replace this Mac's SoMe settings (slots and utm per channel)
  calendar_limits       {"max_per_channel_day", "min_gap_hours", "same_link_days", "same_image_days", "same_topic_days",
                        "email_audience_days"} (the clash warnings in shared_calendar.py)

Where a value comes from:
  solo (not in a team)  this Mac's own settings, exactly as before: copy_rules / seo_sites / calendar_limits in
                        <data>/studio_settings.json, SoMe in social_settings.json, test addresses in Setup.
  in a team             the team's value (GET /api/v1/settings, cached in <data>/team/settings.json) wherever the team
                        has set one; otherwise this Mac's own. A value set by the team is read-only here for editors and
                        viewers ("Set by your team"); owners and admins change it for everyone (PUT /api/v1/settings/<key>).
                        The cache is refreshed on join, after every sync and on the server's SSE `settings` event.

Copy rules on a new install are empty. An install that existed before copy rules were a setting gets the rules it
had built in (_legacy_copy_rules.py), once, on its first start (first_run(), called by server.py and the MCP).

Library (draftstore collection "library", file library.json, synced with the team like any draft; viewers can't add):
  item {id: "lib_" + 10 hex, kind: headline|cta|image|utm|email_block, text, label, note, by, created}
"""
import copy
import datetime as dt
import json
import re
import threading
import uuid
from pathlib import Path

import appconfig
import draftstore
import team

KEYS = ("copy_rules", "seo_sites", "email_test_addresses", "social", "calendar_limits")
LABELS = {"copy_rules": "Copy rules", "seo_sites": "SEO sites", "email_test_addresses": "Test email addresses",
          "social": "SoMe slots, UTM and late posting", "calendar_limits": "Calendar limits"}
AREAS = ("ads", "email", "seo", "social")
LEVELS = ("block", "warn")
CALENDAR_DEFAULTS = {"max_per_channel_day": 2, "min_gap_hours": 3, "same_link_days": 7, "same_image_days": 7,
                     "same_topic_days": 7, "email_audience_days": 3}
CALENDAR_RANGES = {"max_per_channel_day": (1, 50), "min_gap_hours": (0, 72), "same_link_days": (0, 90),
                   "same_image_days": (0, 90), "same_topic_days": (0, 90), "email_audience_days": (0, 90)}
SOCIAL_FIELDS = ("slots", "utm", "utm_campaign", "late_minutes")
SOCIAL_CHANNELS = ("facebook", "instagram", "linkedin")
LOCAL_FILE = "studio_settings.json"
TEAM_FILE = "settings.json"  # in <data>/team (team.py's folder, removed when leaving the team)
MANAGERS = ("owner", "admin")
EMAIL_RE = re.compile(r"[a-z0-9._%+'-]+@[a-z0-9.-]+\.[a-z]{2,}")
MAX_RULES = 200

LIB = "library"
LIB_KINDS = {"headline": "Headline", "cta": "Call to action", "image": "Image URL", "utm": "UTM preset",
             "email_block": "Email block"}
LIB_MAX = {"text": 20000, "label": 120, "note": 300}

_lock = threading.RLock()
_listeners = []
_memo = {}


# ---------- files ----------

def _root():
    return team._root()


def _read(path, default):
    try:
        v = json.loads(Path(path).read_text())
        return v if isinstance(v, type(default)) else copy.deepcopy(default)
    except (OSError, ValueError):
        return copy.deepcopy(default)


def _write(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1))
    tmp.replace(path)


def _local_path():
    return _root() / LOCAL_FILE


def _team_path():
    return _root() / draftstore.TEAM_DIR / TEAM_FILE


def _mtime(p):
    try:
        return p.stat().st_mtime_ns
    except OSError:
        return 0


# ---------- listeners ----------

def on_change(fn):
    """fn() runs when the effective settings may have changed (team cache refreshed, joined, left, saved)."""
    if fn not in _listeners:
        _listeners.append(fn)


def _notify():
    _memo.clear()
    for fn in list(_listeners):
        try:
            fn()
        except Exception:  # noqa: BLE001 - one area failing to refresh must not break the rest
            pass


# ---------- validation ----------

def _text(v, name, maxlen, required=False):
    if v is None:
        v = ""
    if not isinstance(v, str):
        raise ValueError(f"{name} must be text")
    v = v.strip()
    if required and not v:
        raise ValueError(f"{name} is required")
    if len(v) > maxlen:
        raise ValueError(f"{name} is too long (max {maxlen})")
    return v


def _url(v, name):
    v = _text(v, name, 300)
    if v and not re.match(r"^https?://[^\s/]+", v):
        raise ValueError(f"{name} must start with https://")
    return v.rstrip("/")


def _num(v, name, lo, hi):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        try:
            v = float(str(v).strip())
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be a number") from None
    if not lo <= v <= hi:
        raise ValueError(f"{name} must be between {lo} and {hi}")
    return int(v) if float(v).is_integer() else float(v)


def clean_rule(r, n=0):
    if not isinstance(r, dict):
        raise ValueError("each copy rule must be an object")
    level = r.get("level") or "warn"
    level = {"error": "block", "problem": "block"}.get(level, level)
    if level not in LEVELS:
        raise ValueError("a rule's level must be block or warn")
    pattern = _text(r.get("pattern"), "pattern", 500, required=True)
    try:
        re.compile(pattern, re.I)
    except re.error as e:
        raise ValueError(f"pattern {pattern[:60]!r} is not a valid regular expression: {e}") from None
    areas = r.get("areas") or []
    if isinstance(areas, str):
        areas = [a.strip() for a in areas.split(",") if a.strip()]
    if not isinstance(areas, list) or any(a not in AREAS for a in areas):
        raise ValueError(f"areas must be some of {', '.join(AREAS)} (none = everywhere)")
    rid = _text(r.get("id"), "id", 50) or f"r{n + 1}_{uuid.uuid4().hex[:6]}"
    return {"id": re.sub(r"[^A-Za-z0-9_.-]", "_", rid), "level": level, "pattern": pattern,
            "message": _text(r.get("message"), "message", 300) or pattern, "areas": sorted(set(areas), key=AREAS.index)}


def clean(key, value):
    """A setting value checked and normalised, or ValueError."""
    if key not in KEYS:
        raise ValueError(f"unknown setting {key}")
    if not isinstance(value, dict):
        raise ValueError("a setting must be an object")
    if key == "copy_rules":
        rules = value.get("rules") or []
        if not isinstance(rules, list) or len(rules) > MAX_RULES:
            raise ValueError(f"rules must be a list (max {MAX_RULES})")
        out = [clean_rule(r, n) for n, r in enumerate(rules)]
        seen = set()
        for r in out:
            while r["id"] in seen:
                r["id"] += "_2"
            seen.add(r["id"])
        return {"rules": out}
    if key == "seo_sites":
        sites = value.get("sites") or []
        if not isinstance(sites, list) or len(sites) > 20:
            raise ValueError("sites must be a list (max 20)")
        cs = []
        for s in sites:
            if not isinstance(s, dict):
                raise ValueError("each site must be an object")
            sid = _text(s.get("id"), "site id", 30) or re.sub(r"[^a-z0-9]+", "", _text(s.get("name"), "name", 100).lower())[:30]
            cs.append({"id": sid or "site", "name": _text(s.get("name"), "site name", 100, required=True),
                       "on": bool(s.get("on", True)), "note": _text(s.get("note"), "note", 200)})
        return {"prod": _url(value.get("prod"), "prod"), "staging": _url(value.get("staging"), "staging"), "sites": cs}
    if key == "email_test_addresses":
        raw = value.get("addresses") or []
        if isinstance(raw, str):
            raw = re.split(r"[\s,;]+", raw)
        if not isinstance(raw, list):
            raise ValueError("addresses must be a list")
        out = []
        for a in raw:
            a = str(a or "").strip().lower()
            if not a:
                continue
            if not EMAIL_RE.fullmatch(a):
                raise ValueError(f"{a[:80]} is not an email address")
            if a not in out:
                out.append(a)
        if len(out) > 20:
            raise ValueError("at most 20 test addresses")
        return {"addresses": out}
    if key == "social":
        out = {}
        if "slots" in value:
            from areas import _social_plan as plan  # noqa: PLC0415
            slots = value["slots"] or {}
            if not isinstance(slots, dict):
                raise ValueError("slots must be {channel: [\"Tue 09:00\", ...]}")
            out["slots"] = {c: plan.fmt_slots(plan.parse_slots(v)) for c, v in slots.items() if c in SOCIAL_CHANNELS}
        if "utm" in value:
            if not isinstance(value["utm"] or {}, dict):
                raise ValueError("utm must be {facebook: true|false, linkedin: true|false}")
            out["utm"] = {c: bool(v) for c, v in (value["utm"] or {}).items() if c in ("facebook", "linkedin")}
        if "utm_campaign" in value:
            from areas import _social_plan as plan  # noqa: PLC0415
            out["utm_campaign"] = plan.campaign_slug(_text(value["utm_campaign"], "utm_campaign", 100))
        if "late_minutes" in value:
            out["late_minutes"] = int(_num(value["late_minutes"] or 0, "late posting minutes", 0, 240))
        return out
    # calendar_limits
    out = {}
    for k, (lo, hi) in CALENDAR_RANGES.items():
        if k in value and value[k] is not None and value[k] != "":
            out[k] = _num(value[k], k.replace("_", " "), lo, hi)
    return out


# ---------- this Mac's own values ----------

def _local_all():
    return _read(_local_path(), {})


def _social_local():
    try:
        from areas import _social_store as store  # noqa: PLC0415
        s = store.settings(team=False)
    except Exception:  # noqa: BLE001 - SoMe not available here
        return {}
    return {k: copy.deepcopy(s.get(k)) for k in SOCIAL_FIELDS}


def local_get(key):
    """This Mac's own value (what applies when solo)."""
    if key == "email_test_addresses":
        raw = appconfig.get("STUDIO_TEST_EMAIL") or ""
        return {"addresses": [a for a in dict.fromkeys(x.strip().lower() for x in re.split(r"[\s,;]+", raw))
                              if a and EMAIL_RE.fullmatch(a)]}
    if key == "social":
        return _social_local()
    v = _local_all().get(key)
    if key == "calendar_limits":
        try:
            return {**CALENDAR_DEFAULTS, **(clean(key, v) if isinstance(v, dict) else {})}
        except ValueError:
            return dict(CALENDAR_DEFAULTS)
    if key == "copy_rules":
        try:
            return clean(key, v) if isinstance(v, dict) else {"rules": []}
        except ValueError:  # one bad rule: keep the good ones
            good = []
            for n, r in enumerate((v or {}).get("rules") or []):
                try:
                    good.append(clean_rule(r, n))
                except ValueError:
                    pass
            return {"rules": good}
    try:
        return clean(key, v) if isinstance(v, dict) else {"prod": "", "staging": "", "sites": []}
    except ValueError:
        return {"prod": "", "staging": "", "sites": []}


def local_set(key, value):
    value = clean(key, value)
    if key == "email_test_addresses":
        appconfig.set_many({"STUDIO_TEST_EMAIL": ", ".join(value["addresses"]) or None})
    elif key == "social":
        from areas import _social_store as store  # noqa: PLC0415
        store.save_settings(value)
    else:
        with _lock:
            d = _local_all()
            d[key] = value
            _write(_local_path(), d)
    _notify()
    return local_get(key)


def first_run():
    """Once per install: decide its copy rules. An install that has a config.json (it was set up before this version)
    keeps the rules it had built in; a new install starts with none. Never overwrites a decision."""
    with _lock:
        d = _local_all()
        if "copy_rules" in d:
            return False
        legacy = appconfig.CONFIG.exists()
        if legacy:
            import _legacy_copy_rules  # noqa: PLC0415
            d["copy_rules"] = clean("copy_rules", {"rules": _legacy_copy_rules.RULES})
        else:
            d["copy_rules"] = {"rules": []}
        _write(_local_path(), d)
    _notify()
    return legacy


# ---------- the team's values ----------

def _role():
    return ((team._state().get("member") or {}).get("role") or "").lower()


def in_team():
    try:
        return team.joined() and not team._state().get("removed")
    except Exception:  # noqa: BLE001
        return False


def is_admin():
    return in_team() and _role() in MANAGERS


def _team_cache():
    if not in_team():
        return {}
    return _read(_team_path(), {})


def refresh():
    """Read the team's settings from the server into the cache. -> the cache. Offline: the old cache stays."""
    if not in_team():
        return {}
    r = team._api("GET", "/api/v1/settings", timeout=10)
    got = r.get("settings") if isinstance(r, dict) else None
    if not isinstance(got, dict):
        raise team.TeamError("The team server sent no settings")
    cache = {}
    for k in KEYS:
        s = got.get(k)
        if isinstance(s, dict) and isinstance(s.get("value"), dict):
            cache[k] = {"value": s["value"], "version": int(s.get("version") or 0), "updated_by": s.get("updated_by"),
                        "updated_at": s.get("updated_at") or ""}
        elif isinstance(s, dict):
            cache[k] = {"value": None, "version": int(s.get("version") or 0)}
    with _lock:
        old = _read(_team_path(), {})
        if old != cache:
            _write(_team_path(), cache)
    if old != cache:
        _notify()
    return cache


def _on_team(kind, data):
    if kind in ("join", "sync", "event:settings"):
        try:
            refresh()
        except Exception:  # noqa: BLE001 - offline: the cache stays
            pass
    elif kind == "leave":
        _notify()


team.on_hook(_on_team)


def team_value(key):
    """The team's value for key (checked), or None when the team has not set it (or this Mac is not in a team)."""
    v = (_team_cache().get(key) or {}).get("value")
    if not isinstance(v, dict):
        return None
    try:
        return clean(key, v)
    except ValueError:
        return None


def _stamp():
    return (in_team(), _mtime(_local_path()), _mtime(_team_path()))


def get(key):
    """The value that applies on this Mac now."""
    if key in ("copy_rules", "calendar_limits", "seo_sites"):
        st = _stamp()
        hit = _memo.get(key)
        if hit and hit[0] == st:
            return copy.deepcopy(hit[1])
    tv = team_value(key)
    lv = local_get(key)
    if tv is None:
        out = lv
    elif key == "social":
        out = merge_social(lv, tv)
    elif key == "calendar_limits":
        out = {**CALENDAR_DEFAULTS, **tv}
    else:
        out = tv
    if key in ("copy_rules", "calendar_limits", "seo_sites"):
        _memo[key] = (_stamp(), copy.deepcopy(out))
    return out


def merge_social(s, tv):
    """SoMe settings with the team's fields on top: slots and utm per channel (channels the team left out stay)."""
    out = dict(s)
    for k, v in (tv or {}).items():
        out[k] = {**(s.get(k) or {}), **v} if k in ("slots", "utm") else v
    return out


def team_fields(key):
    """The fields of key the team has set (they override this Mac's own), [] when none."""
    tv = team_value(key)
    return sorted(tv) if tv else []


def check_local_edit(key, fields=None):
    """Raise when this Mac may not change key (or these fields of it) because the team has set them."""
    tf = team_fields(key)
    if not tf:
        return
    hit = [f for f in (fields if fields is not None else tf) if f in tf]
    if hit:
        who = "Change it for everyone in Calendar > Team settings." if is_admin() else "Ask an owner or admin to change it."
        raise ValueError(f"Set by your team ({', '.join(hit)}). {who}")


# ---------- copy rules for the areas ----------

def copy_rules(area):
    """[(level "block"|"warn", compiled pattern, message)] for one area, in order."""
    st = _stamp()
    hit = _memo.get(("rules", area))
    if hit and hit[0] == st:
        return hit[1]
    out = []
    for r in get("copy_rules")["rules"]:
        if r["areas"] and area not in r["areas"]:
            continue
        try:
            out.append((r["level"], re.compile(r["pattern"], re.I), r["message"]))
        except re.error:
            continue
    _memo[("rules", area)] = (st, out)
    return out


def apply_rules(area, text):
    """-> [(level, message, matched text)] for every rule that matches."""
    out = []
    for level, rx, msg in copy_rules(area):
        m = rx.search(text or "")
        if m:
            out.append((level, msg, m.group(0)))
    return out


# ---------- the UI and the MCP ----------

def _member_name(mid):
    if mid is None:
        return ""
    m = team.members().get(str(mid))
    return m["name"] if m else f"member {mid}"


def info(refresh_now=False):
    """Every key: the value that applies, this Mac's own, the team's (if set), and whether it can be edited here."""
    joined = in_team()
    if joined and (refresh_now or not _team_path().exists()):
        try:
            refresh()
        except Exception:  # noqa: BLE001 - offline: show the cache
            pass
    admin = is_admin()
    cache = _team_cache()
    keys = {}
    for k in KEYS:
        t = cache.get(k) or {}
        tv = team_value(k)
        keys[k] = {"key": k, "label": LABELS[k], "value": get(k), "local": local_get(k),
                   "team": ({"value": tv, "version": t.get("version", 0), "updated_by": _member_name(t.get("updated_by")),
                             "updated_at": t.get("updated_at") or ""} if tv is not None else None),
                   "team_version": int(t.get("version") or 0), "source": "team" if tv is not None else "local",
                   "editable": tv is None or admin}
    return {"joined": joined, "role": _role() if joined else "", "can_admin": admin, "keys": keys, "areas": list(AREAS),
            "levels": list(LEVELS), "calendar_defaults": CALENDAR_DEFAULTS}


def save(key, value, scope="local"):
    """scope "local": this Mac's own value (refused when the team has set the key, unless you are an admin, who then
    edits the team's value). scope "team": share it with the team (owners and admins). value None + scope "team":
    stop sharing (everyone falls back to their own)."""
    if key not in KEYS:
        raise ValueError(f"unknown setting {key}")
    if scope == "local" and team_value(key) is not None:
        if not is_admin():
            check_local_edit(key)
        scope = "team"
    if scope == "team":
        if not in_team():
            raise ValueError("Join a team first (Setup > Team)")
        if not is_admin():
            raise ValueError("Only an owner or admin can change the team's settings")
        base = int((_team_cache().get(key) or {}).get("version") or 0)
        body = {"value": None if value is None else clean(key, value), "base_version": base}
        try:
            team._api("PUT", f"/api/v1/settings/{key}", body)
        except team.TeamError as e:
            if e.status == 409:
                refresh()
                raise ValueError("Someone changed this team setting just now: look again and save once more") from None
            raise ValueError(str(e)) from None
        refresh()
        return info()
    local_set(key, value)
    return info()


# ---------- library ----------

def _now():
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def _me():
    st = team._state() if in_team() else {}
    return ((st.get("member") or {}).get("name")) or "you"


def library_list(kind=None, q=None):
    """Items, newest first, with who added them. kind / q filter."""
    recs = draftstore.items(LIB, _root())
    me = _me()
    out = []
    for r in recs:
        d = r["data"]
        if kind and d.get("kind") != kind:
            continue
        if q and q.lower() not in " ".join(str(d.get(k) or "") for k in ("text", "label", "note")).lower():
            continue
        who = r.get("author") or d.get("by") or ""
        out.append({**{k: d.get(k) for k in ("id", "kind", "text", "label", "note", "created")},
                    "by": me if who in ("you", "") else who})
    out.sort(key=lambda x: x.get("created") or "", reverse=True)
    return {"items": out, "kinds": LIB_KINDS, "can_add": not (in_team() and _role() == "viewer")}


def library_add(item, by="you"):
    if not isinstance(item, dict):
        raise ValueError("item must be an object")
    if in_team() and _role() == "viewer":
        raise ValueError("Viewers can't add to the team library")
    kind = item.get("kind")
    if kind not in LIB_KINDS:
        raise ValueError(f"kind must be one of {', '.join(LIB_KINDS)}")
    text = _text(item.get("text"), "text", LIB_MAX["text"], required=True)
    if kind == "image" and not re.match(r"^https://\S+$", text):
        raise ValueError("an image must be an https:// URL")
    if kind == "utm":
        text = text.lstrip("?&")
        if not all(re.fullmatch(r"utm_[a-z_]+=[^&\s]*", p) for p in text.split("&")):
            raise ValueError("a UTM preset looks like utm_source=x&utm_medium=y&utm_campaign=z")
    iid = "lib_" + uuid.uuid4().hex[:10]
    data = {"id": iid, "kind": kind, "text": text, "label": _text(item.get("label"), "label", LIB_MAX["label"]),
            "note": _text(item.get("note"), "note", LIB_MAX["note"]), "by": by, "created": _now()}
    with draftstore.tx(LIB, _root(), by=by) as t:
        t.put(iid, data, by)
    return data


def library_delete(iid):
    if in_team() and _role() == "viewer":
        raise ValueError("Viewers can't change the team library")
    with draftstore.tx(LIB, _root()) as t:
        if not t.delete(str(iid)):
            raise ValueError("No such library item")
    return {"ok": True, "id": str(iid)}
