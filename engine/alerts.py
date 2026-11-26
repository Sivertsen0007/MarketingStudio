"""Alerts for Google Ads and Meta: rules the user sets, checked against fresh reads (read-only), fired alerts kept in a
history with acknowledge and snooze, shown in Studio (Alerts tab and the header badge), as a macOS notification and,
in a team, through the user's own team notification channels. Nothing here changes anything on Google or Meta.

Rules (local config, <data>/alerts_rules.json; the UI, the HTTP routes and the MCP tool alert_rule_add write them):
  {id, name, enabled, platform any|google|meta, account "*" (every account) | an account id ("8931136258",
   "meta_644..."), level account|campaign|adset (adset: Meta only), object_id (one campaign / ad set; "" = each),
   metric, op above|below|up_pct|down_pct, value, window today|yesterday|last_7_days,
   min {spend|clicks|impressions|conversions: n} (minimum volume in the window, so small numbers don't fire),
   quiet_hours (the same rule on the same object fires at most once per quiet period), starter, created_by, created_at}
  Metrics: spend, clicks, impressions, conversions (sums); cpc, ctr (%), cpa, roas (ratios); spend_vs_budget (spend
  in the window as % of the daily budget x days); and three checks without a number: no_delivery (an active campaign
  / ad set with no impressions yesterday or today, so at least 24 hours), disapproved_ads (count, Google ad group ads
  and Meta ads with DISAPPROVED), learning_limited (Meta active ad sets whose learning is "learning limited").
  up_pct / down_pct compare with the previous comparable period: last_7_days with the 7 days before, yesterday with
  the same weekday a week earlier, today (so far) with the same weekday a week earlier scaled to the share of the day
  gone (sums only; ratios are compared as they are). With a minimum volume, both periods must reach it.
  Starter rules (STARTERS) are put in the file once, all switched off.

When (scheduler.py, both refresh=True so they are held while Refresh data is manual):
  alerts_hourly   every hour: rules on spend, clicks, impressions, CPC, CTR, spend vs budget, delivery, disapproved
                  ads, learning (Google: clicks / impressions / cost have a 1 hour freshness target).
  alerts_daily    daily at 09:00 (account time zone): conversions, CPA, ROAS (Google conversions can take up to
                  15 hours for attribution models other than last click).
  With manual Refresh data: "Check now" in the Alerts tab, and a check after each Refresh of Google / Meta /
  All for the accounts refreshed (at most once per account per MIN_GAP).
  A check with no rule switched on for an account reads nothing for it.

Reads per account and check (only the parts the account's rules need):
  Google  GAQL searchStream: campaigns with status, budget and the customer's time zone / currency (1 op); metrics by
          campaign and day for the last 15 days (1 op); disapproved ads (1 op, only with a disapproved_ads rule).
  Meta    ONE Graph GET on the ad account with field expansion: campaigns, ad sets (with learning_stage_info) and
          disapproved ads only when needed, plus insights by day (level campaign or ad set). A first check on an
          account whose time zone Studio has never seen makes one small GET for it first. Meta refreshes insights
          about every 15 minutes. Back-off: the usage headers (x-business-use-case-usage, x-ad-account-usage,
          x-app-usage, X-FB-Ads-Insights-Throttle) are read after every call; at 75% or more (or a rate-limit error,
          or estimated_time_to_regain_access) Meta checks pause (BACKOFF), and a check never retries a refused call.

Files: <data>/alerts_rules.json (rules + delivery settings), alerts_history.json (fired alerts, newest first, at most
HISTORY_MAX), alerts_state.json (last fired per rule and object, snoozes, last check per account, Meta back-off,
account time zones).
"""
import contextlib
import datetime as dt
import fcntl
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from zoneinfo import ZoneInfo

import core
import datapolicy

RULES_FILE = "alerts_rules.json"
HISTORY_FILE = "alerts_history.json"
STATE_FILE = "alerts_state.json"
HISTORY_MAX = 500
DAYS_BACK = 14  # read today and the 14 days before: last 7 days + the 7 before
MIN_GAP = 300  # seconds: a Refresh-triggered check skips accounts checked this recently
BACKOFF = ((90, 60), (75, 20))  # Meta usage % at or above -> pause Meta checks for N minutes
RATE_LIMIT_PAUSE = 60  # minutes after a rate-limit error
MAX_PER_RULE = 20  # objects one rule may fire for in one check (the rest are counted in the summary)
JOB_HOURLY, JOB_DAILY = "alerts_hourly", "alerts_daily"

METRICS = {  # key: (label, kind, cadence)
    "spend": ("Spend", "sum", "hourly"),
    "clicks": ("Clicks", "sum", "hourly"),
    "impressions": ("Impressions", "sum", "hourly"),
    "conversions": ("Conversions", "sum", "daily"),
    "cpc": ("CPC", "ratio", "hourly"),
    "ctr": ("CTR", "ratio", "hourly"),
    "cpa": ("CPA", "ratio", "daily"),
    "roas": ("ROAS", "ratio", "daily"),
    "spend_vs_budget": ("Spend vs daily budget", "ratio", "hourly"),
    "no_delivery": ("No impressions for 24 hours", "check", "hourly"),
    "disapproved_ads": ("Disapproved ads", "count", "hourly"),
    "learning_limited": ("Ad sets learning limited", "count", "hourly"),
}
SUMS = ("spend", "clicks", "impressions", "conversions", "value")
OPS = {"above": "above", "below": "below", "up_pct": "up by % or more", "down_pct": "down by % or more"}
WINDOWS = {"today": "today", "yesterday": "yesterday", "last_7_days": "last 7 days"}
LEVELS = ("account", "campaign", "adset")
PLATFORMS = ("any", "google", "meta")

STARTERS = [
    {"starter": "spend_over_budget", "name": "Spend today over 120% of the daily budget", "level": "campaign",
     "metric": "spend_vs_budget", "op": "above", "value": 120, "window": "today", "quiet_hours": 24},
    {"starter": "cpa_up_week", "name": "CPA up 50% week on week (at least 10 conversions)", "level": "account",
     "metric": "cpa", "op": "up_pct", "value": 50, "window": "last_7_days", "min": {"conversions": 10}, "quiet_hours": 72},
    {"starter": "no_delivery", "name": "No impressions on an active campaign for 24 hours", "level": "campaign",
     "metric": "no_delivery", "quiet_hours": 24},
    {"starter": "disapproved_ads", "name": "Disapproved ads", "level": "account", "metric": "disapproved_ads", "op": "above",
     "value": 0, "quiet_hours": 24},
    {"starter": "learning_limited", "name": "Meta ad set stuck in learning limited", "platform": "meta", "level": "adset",
     "metric": "learning_limited", "op": "above", "value": 0, "quiet_hours": 72},
]

METHOD = ("Alerts read Google Ads (GAQL) and Meta (Graph GETs) only: nothing is ever changed. Hourly rules: spend, clicks, "
          "impressions, CPC, CTR, spend vs budget, delivery, disapproved ads, learning. Daily rules (09:00): conversions, "
          "CPA, ROAS, because Google conversions can take up to 15 hours to arrive for attribution models other than last "
          "click (clicks, impressions and cost within about an hour; Meta about every 15 minutes). 'Change' compares with "
          "the previous comparable period: last 7 days with the 7 before, yesterday with the same weekday a week earlier, "
          "today so far with the same weekday a week earlier scaled to the part of the day gone. Days are in each account's "
          "time zone.")

_lock = threading.RLock()
_check_lock = threading.Lock()
_running = {"at": None, "reason": None}


# ---------------------------------------------------------------- files

def _dir():
    return datapolicy._dir()


@contextlib.contextmanager
def _flock():
    """One writer at a time across Studio and the MCP server."""
    with _lock, open(_dir() / ".alerts.lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        yield


def _read(name, default):
    try:
        v = json.loads((_dir() / name).read_text())
        return v if isinstance(v, type(default)) else default
    except (OSError, ValueError):
        return default


def _write(name, data):
    f = _dir() / name
    tmp = f.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    tmp.replace(f)


def _state():
    s = _read(STATE_FILE, {})
    for k in ("last_fired", "snoozed", "last_check", "tz"):
        s.setdefault(k, {})
    s.setdefault("meta_backoff", {})
    return s


# ---------------------------------------------------------------- rules

def _now():
    return time.time()


def _iso(ts=None):
    return dt.datetime.fromtimestamp(ts if ts is not None else _now()).isoformat(timespec="seconds")


def _accounts():
    """Every ad account Studio knows: [{id, key, name, platform, act?}]. No API calls beyond what the account lists do."""
    out = []
    try:
        import gapi  # noqa: PLC0415
        out += [{"id": str(a["id"]), "key": a.get("key") or str(a["id"]), "name": a.get("name") or str(a["id"]), "platform": "google"}
                for a in gapi.accounts_any()]
    except Exception:  # noqa: BLE001 - no Google set up
        pass
    try:
        import meta_ads  # noqa: PLC0415
        out += [{"id": a["id"], "key": a["key"], "name": a["name"], "platform": "meta", "act": a["act"]} for a in meta_ads.accounts()]
    except Exception:  # noqa: BLE001
        pass
    return out


def _find_account(ref, accts=None):
    """An account by id ("8931136258", "meta_644...") or key ("ml", "meta")."""
    ref = str(ref or "").strip()
    for a in accts if accts is not None else _accounts():
        if ref in (a["id"], a["key"]):
            return a
    raise ValueError(f"unknown ad account {ref!r}")


def _account_ref(ref, accts=None):
    """'*' or an account id; keys ("ml", "meta") become the id. Unknown -> ValueError."""
    ref = str(ref or "*").strip()
    if ref in ("*", "", "all"):
        return "*"
    return _find_account(ref, accts if accts is not None else _accounts())["id"]


def _num(v, what):
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{what} must be a number") from None
    if x != x or x in (float("inf"), float("-inf")):
        raise ValueError(f"{what} must be a number")
    return x


def normalise(body, old=None, accts=None):
    """A rule from user / MCP input (merged over `old` when editing). Raises ValueError with a readable message."""
    r = dict(old or {})
    body = body if isinstance(body, dict) else {}
    for k in ("name", "enabled", "platform", "account", "level", "object_id", "object_name", "metric", "op", "value", "window",
              "min", "quiet_hours"):
        if k in body:
            r[k] = body[k]
    metric = r.get("metric")
    if metric not in METRICS:
        raise ValueError("metric must be one of " + ", ".join(METRICS))
    kind = METRICS[metric][1]
    r["platform"] = r.get("platform") or "any"
    if r["platform"] not in PLATFORMS:
        raise ValueError("platform must be any, google or meta")
    r["level"] = r.get("level") or "account"
    if r["level"] not in LEVELS:
        raise ValueError("level must be account, campaign or adset")
    r["account"] = _account_ref(r.get("account"), accts)
    if r["account"] != "*":
        r["platform"] = "meta" if r["account"].startswith("meta_") else "google"
    if metric == "learning_limited":
        if r["platform"] == "google":
            raise ValueError("learning limited is a Meta check")
        r["platform"] = "meta"
    if r["level"] == "adset":
        if r["platform"] == "google":
            raise ValueError("ad set level is Meta only (Google rules work per account or campaign)")
        r["platform"] = "meta"
    if kind == "check":
        r["op"], r["value"], r["window"] = "above", 0, "today"
    else:
        r["op"] = r.get("op") or "above"
        if r["op"] not in OPS:
            raise ValueError("comparison must be above, below, up_pct or down_pct")
        if kind == "count":
            if r["op"] not in ("above", "below"):
                raise ValueError(f"{METRICS[metric][0]} can only be compared above or below a number")
            r["value"] = _num(r.get("value", 0), "value")
        else:
            if r.get("value") in (None, ""):
                raise ValueError("value is required")
            r["value"] = _num(r["value"], "value")
            if r["op"] in ("up_pct", "down_pct") and r["value"] <= 0:
                raise ValueError("a change must be more than 0%")
        r["window"] = r.get("window") or "today"
        if r["window"] not in WINDOWS:
            raise ValueError("window must be today, yesterday or last_7_days")
    mins = {}
    for k, v in (r.get("min") or {}).items():
        if k not in ("spend", "clicks", "impressions", "conversions"):
            raise ValueError("minimum volume must be on spend, clicks, impressions or conversions")
        if v not in (None, ""):
            x = _num(v, "minimum volume")
            if x > 0:
                mins[k] = x
    r["min"] = mins
    q = _num(r.get("quiet_hours") if r.get("quiet_hours") not in (None, "") else 24, "quiet period")
    if not 1 <= q <= 720:
        raise ValueError("quiet period must be 1 to 720 hours")
    r["quiet_hours"] = q
    r["object_id"] = str(r.get("object_id") or "").strip()[:40]
    if r["object_id"] and not re.fullmatch(r"[0-9]+", r["object_id"]):
        raise ValueError("object_id must be a campaign or ad set id (digits)")
    if r["object_id"] and r["level"] == "account":
        raise ValueError("an object_id needs level campaign or adset")
    r["object_name"] = str(r.get("object_name") or "")[:200]
    r["enabled"] = bool(r.get("enabled", True))
    r["name"] = (str(r.get("name") or "").strip() or describe(r))[:200]
    return r


def describe(r):
    """A rule in words."""
    label, kind, _ = METRICS[r["metric"]]
    where = {"account": "account", "campaign": "each campaign", "adset": "each ad set"}[r["level"]]
    if r.get("object_id"):
        where = f"{'campaign' if r['level'] == 'campaign' else 'ad set'} {r.get('object_name') or r['object_id']}"
    if kind == "check":
        return f"{label} ({where})"
    if kind == "count":
        return f"{label} {OPS[r['op']]} {r['value']:g} ({where})"
    win = WINDOWS[r["window"]]
    if r["op"] in ("up_pct", "down_pct"):
        cmp = f"{'up' if r['op'] == 'up_pct' else 'down'} {r['value']:g}% or more on the previous period"
    else:
        cmp = f"{r['op']} {r['value']:g}{'%' if r['metric'] in ('ctr', 'spend_vs_budget') else ''}"
    mins = ", ".join(f"at least {v:g} {k}" for k, v in (r.get("min") or {}).items())
    return f"{label} {win} {cmp} ({where}{'; ' + mins if mins else ''})"


def cadence(r):
    return METRICS[r["metric"]][2]


def _load_rules():
    d = _read(RULES_FILE, {})
    d.setdefault("rules", [])
    d.setdefault("settings", {})
    d["settings"].setdefault("mac_notify", True)
    d["settings"].setdefault("team_notify", True)
    return d


def config():
    """{rules, settings}. The starter rules are put in once, switched off."""
    with _flock():
        d = _load_rules()
        if not d.get("seeded"):
            have = {r.get("starter") for r in d["rules"]}
            for s in STARTERS:
                if s["starter"] in have:
                    continue
                r = normalise({**s, "account": "*", "enabled": False}, accts=[])
                r.update(id="r_" + s["starter"], starter=s["starter"], created_by="Studio", created_at=_iso())
                d["rules"].append(r)
            d["seeded"] = True
            _write(RULES_FILE, d)
        return d


def rules():
    return config()["rules"]


def add_rule(body, by="you"):
    accts = _accounts()
    r = normalise(body, accts=accts)
    r.update(id="r_" + uuid.uuid4().hex[:10], starter="", created_by=str(by or "you")[:40], created_at=_iso())
    config()
    with _flock():
        d = _load_rules()
        if len(d["rules"]) >= 200:
            raise ValueError("at most 200 alert rules")
        d["rules"].append(r)
        _write(RULES_FILE, d)
    return r


def update_rule(rid, body):
    config()
    with _flock():
        d = _load_rules()
        for i, old in enumerate(d["rules"]):
            if old.get("id") == rid:
                if set(body) <= {"enabled", "id"}:
                    new = {**old, "enabled": bool(body.get("enabled"))}
                else:
                    new = normalise(body, old, accts=_accounts())
                new["id"] = rid
                d["rules"][i] = new
                _write(RULES_FILE, d)
                return new
    raise ValueError("no such alert rule")


def delete_rule(rid):
    config()
    with _flock():
        d = _load_rules()
        n = len(d["rules"])
        d["rules"] = [r for r in d["rules"] if r.get("id") != rid]
        if len(d["rules"]) == n:
            raise ValueError("no such alert rule")
        _write(RULES_FILE, d)
    return {"deleted": rid}


def save_settings(body):
    config()
    with _flock():
        d = _load_rules()
        for k in ("mac_notify", "team_notify"):
            if k in body:
                d["settings"][k] = bool(body[k])
        _write(RULES_FILE, d)
        return d["settings"]


# ---------------------------------------------------------------- reading Google (GAQL search only)

G_CAMPS = ("SELECT customer.time_zone, customer.currency_code, campaign.id, campaign.name, campaign.status, campaign.serving_status, "
           "campaign.start_date_time, campaign_budget.id, campaign_budget.amount_micros, campaign_budget.period FROM campaign WHERE campaign.status != 'REMOVED'")
G_CUSTOMER = "SELECT customer.time_zone, customer.currency_code FROM customer LIMIT 1"
G_DAILY = ("SELECT campaign.id, segments.date, metrics.cost_micros, metrics.clicks, metrics.impressions, metrics.conversions, "
           "metrics.conversions_value FROM campaign WHERE segments.date BETWEEN '{start}' AND '{end}'")
G_DISAPPROVED = ("SELECT campaign.id, campaign.name, ad_group.id, ad_group.name, ad_group_ad.ad.id, ad_group_ad.ad.name, "
                 "ad_group_ad.policy_summary.approval_status FROM ad_group_ad WHERE ad_group_ad.policy_summary.approval_status = "
                 "'DISAPPROVED' AND ad_group_ad.status = 'ENABLED' AND ad_group.status = 'ENABLED' AND campaign.status = 'ENABLED'")


def _local(tz_name, now=None):
    try:
        tz = ZoneInfo(tz_name) if tz_name else dt.timezone.utc
    except Exception:  # noqa: BLE001
        tz = dt.timezone.utc
    return dt.datetime.fromtimestamp(now if now is not None else _now(), tz)


def _empty(platform, acct, tz, currency, now):
    loc = _local(tz, now)
    return {"ts": now if now is not None else _now(), "platform": platform, "account_id": acct["id"], "name": acct["name"], "currency": currency, "tz": tz,
            "today": loc.date().isoformat(), "day_part": round((loc.hour * 3600 + loc.minute * 60 + loc.second) / 86400, 4),
            "campaigns": {}, "adsets": {}, "daily": {"campaign": {}, "adset": {}}, "disapproved": [], "parts": [], "calls": 0}


def fetch_google(acct, need, query=None, now=None):
    if query is None:
        import live  # noqa: PLC0415
        query = live.query
    cid = acct["id"]
    rows = query(cid, G_CAMPS)
    calls = 1
    cust = (rows[0].get("customer") if rows else None) or {}
    if not cust:
        cust = (query(cid, G_CUSTOMER) or [{}])[0].get("customer", {})
        calls += 1
    snap = _empty("google", acct, cust.get("timeZone") or "Europe/London", cust.get("currencyCode", ""), now)
    for r in rows:
        c, b = r.get("campaign", {}), r.get("campaignBudget", {})
        amt = int(b.get("amountMicros") or 0)
        snap["campaigns"][str(c.get("id"))] = {
            "id": str(c.get("id")), "name": c.get("name", ""), "status": (c.get("status") or "").title(),
            "active": c.get("status") == "ENABLED" and c.get("servingStatus") not in ("ENDED", "SUSPENDED", "PENDING"),
            "budget": round(amt / 1e6, 2) if amt and (b.get("period") or "DAILY") == "DAILY" else None,
            "budget_id": str(b.get("id") or ""), "started": _started(c.get("startDateTime"), snap["tz"])}
    today = dt.date.fromisoformat(snap["today"])
    if need.get("metrics"):
        start = today - dt.timedelta(days=DAYS_BACK)
        calls += 1
        for r in query(cid, G_DAILY.format(start=start.isoformat(), end=today.isoformat())):
            k = str(r.get("campaign", {}).get("id"))
            m = r.get("metrics", {})
            day = r.get("segments", {}).get("date")
            row = snap["daily"]["campaign"].setdefault(k, {}).setdefault(day, dict.fromkeys(SUMS, 0.0))
            row["spend"] += int(m.get("costMicros") or 0) / 1e6
            row["clicks"] += float(m.get("clicks") or 0)
            row["impressions"] += float(m.get("impressions") or 0)
            row["conversions"] += float(m.get("conversions") or 0)
            row["value"] += float(m.get("conversionsValue") or 0)
        snap["parts"].append("metrics")
    if need.get("disapproved"):
        calls += 1
        for r in query(cid, G_DISAPPROVED):
            ad = r.get("adGroupAd", {}).get("ad", {})
            snap["disapproved"].append({"id": str(ad.get("id")), "name": ad.get("name") or f"Ad {ad.get('id')}",
                                        "campaign_id": str(r.get("campaign", {}).get("id")), "campaign": r.get("campaign", {}).get("name", ""),
                                        "group": r.get("adGroup", {}).get("name", ""), "adset_id": ""})
        snap["parts"].append("disapproved")
    snap["calls"] = calls
    return snap


# ---------------------------------------------------------------- reading Meta (one GET per check)

M_CAMPS = ('campaigns.limit(500).effective_status(["ACTIVE","PAUSED","IN_PROCESS","WITH_ISSUES"])'
           "{id,name,effective_status,daily_budget,lifetime_budget,created_time,start_time}")
M_SETS = ('adsets.limit(500).effective_status(["ACTIVE","PAUSED","CAMPAIGN_PAUSED","IN_PROCESS","WITH_ISSUES"])'
          "{id,name,campaign_id,effective_status,daily_budget,learning_stage_info,created_time,start_time}")
M_ADS = 'ads.limit(200).effective_status(["DISAPPROVED"]){id,name,campaign_id,adset_id}'
M_INS = ('insights.level({level}).time_increment(1).time_range({{"since":"{since}","until":"{until}"}}).limit(2000)'
         "{{{level}_id,spend,clicks,impressions,actions,action_values}}")
PAGES = 5


def _started(text, tz_name):
    """Google's campaign.start_date_time ("YYYY-MM-DD HH:MM:SS", account time zone) -> epoch, or None."""
    try:
        d = dt.datetime.strptime(str(text)[:19], "%Y-%m-%d %H:%M:%S")
        return d.replace(tzinfo=ZoneInfo(tz_name) if tz_name else dt.timezone.utc).timestamp()
    except Exception:  # noqa: BLE001
        return None


def _started_meta(o):
    """The later of Meta's created_time and start_time (ISO 8601 with offset) -> epoch, or None."""
    out = []
    for k in ("created_time", "start_time"):
        with contextlib.suppress(Exception):
            out.append(dt.datetime.strptime(str(o.get(k)), "%Y-%m-%dT%H:%M:%S%z").timestamp())
    return max(out) if out else None


NEW_FOR = 86400  # no_delivery: a campaign / ad set that started less than this long ago is left alone


def _cents(v, currency=""):
    """A Meta budget (the currency's smallest unit) -> account currency. Offset-1 currencies (JPY, KRW, ...) have no
    minor unit on Meta, so 5000 JPY is 5000, not 50.00."""
    from meta_ads import OFFSET_1  # noqa: PLC0415
    factor = 1 if str(currency or "").upper() in OFFSET_1 else 100
    return round(int(v) / factor, 2) if v not in (None, "", "0", 0) else None


def meta_fields(need, tz, now=None):
    today = _local(tz, now).date()
    f = ["name", "currency", "timezone_name", M_CAMPS]
    if need.get("adsets"):
        f.append(M_SETS)
    if need.get("disapproved"):
        f.append(M_ADS)
    if need.get("metrics"):
        f.append(M_INS.format(level="adset" if need.get("adset_level") else "campaign",
                              since=(today - dt.timedelta(days=DAYS_BACK)).isoformat(), until=today.isoformat()))
    return ",".join(f)


class MetaBusy(Exception):
    """Meta's usage headers went over the back-off line between pages: the check stops (no alerts from part of the data)."""
    rate_limited = True


def _edge(node, key, get, counter, busy=None):
    """Every row of a nested edge, following at most PAGES next links (each one more GET). Before each next page the
    usage headers are read again (busy()); over the line, the check stops with MetaBusy."""
    e = node.get(key) or {}
    out = list(e.get("data") or [])
    nxt, n = (e.get("paging") or {}).get("next"), 0
    while nxt and n < PAGES:
        why = busy() if busy else None
        if why:
            raise MetaBusy(f"stopped before the next page of {key}: {why}")
        page = get(nxt)
        counter[0] += 1
        out += page.get("data") or []
        nxt = (page.get("paging") or {}).get("next")
        n += 1
    return out


def _meta_busy():
    pct, regain = meta_pressure(_meta_usage())
    return f"Meta usage is at {pct:.0f}%" if _backoff_minutes(pct, regain) else None


def fetch_meta(acct, need, get=None, now=None, tz_hint=None, busy=_meta_busy):
    if get is None:
        import meta_api  # noqa: PLC0415
        get = meta_api.get_once
    from meta_ads import RESULT_ACTIONS  # noqa: PLC0415
    act = acct["act"]
    counter = [0]
    tz = tz_hint
    if not tz:
        tz = (get(act, fields="timezone_name") or {}).get("timezone_name") or "Europe/London"
        counter[0] += 1
    node = get(act, fields=meta_fields(need, tz, now))
    counter[0] += 1
    tz = node.get("timezone_name") or tz
    snap = _empty("meta", acct, tz, node.get("currency", ""), now)
    for c in _edge(node, "campaigns", get, counter, busy):
        snap["campaigns"][str(c["id"])] = {"id": str(c["id"]), "name": c.get("name", ""),
                                           "status": (c.get("effective_status") or "").replace("_", " ").title(),
                                           "active": c.get("effective_status") == "ACTIVE", "budget": _cents(c.get("daily_budget"), node.get("currency")),
                                           "budget_id": str(c["id"]) if c.get("daily_budget") else "",
                                           "started": _started_meta(c)}
    for s in _edge(node, "adsets", get, counter, busy):
        snap["adsets"][str(s["id"])] = {"id": str(s["id"]), "name": s.get("name", ""), "campaign_id": str(s.get("campaign_id")),
                                        "status": (s.get("effective_status") or "").replace("_", " ").title(),
                                        "active": s.get("effective_status") == "ACTIVE", "budget": _cents(s.get("daily_budget"), node.get("currency")),
                                        "learning": (s.get("learning_stage_info") or {}).get("status"), "started": _started_meta(s)}
    if need.get("adsets"):
        snap["parts"].append("adsets")
    if need.get("disapproved"):
        for a in _edge(node, "ads", get, counter, busy):
            camp = snap["campaigns"].get(str(a.get("campaign_id")), {})
            snap["disapproved"].append({"id": str(a["id"]), "name": a.get("name") or f"Ad {a['id']}", "campaign_id": str(a.get("campaign_id")),
                                        "campaign": camp.get("name", ""), "adset_id": str(a.get("adset_id") or ""), "group": ""})
        snap["parts"].append("disapproved")
    if need.get("metrics"):
        level = "adset" if need.get("adset_level") else "campaign"
        for r in _edge(node, "insights", get, counter, busy):
            k = str(r.get(f"{level}_id"))
            acts = {a["action_type"]: float(a["value"]) for a in r.get("actions") or []}
            vals = {a["action_type"]: float(a["value"]) for a in r.get("action_values") or []}
            row = {"spend": float(r.get("spend") or 0), "clicks": float(r.get("clicks") or 0),
                   "impressions": float(r.get("impressions") or 0),
                   "conversions": sum(v for t, v in acts.items() if t in RESULT_ACTIONS),
                   "value": vals.get("offsite_conversion.fb_pixel_purchase", 0.0)}
            day = r.get("date_start")
            snap["daily"][level].setdefault(k, {})[day] = row
            if level == "adset":  # roll ad sets up into their campaign
                cid = (snap["adsets"].get(k) or {}).get("campaign_id")
                if cid:
                    cur = snap["daily"]["campaign"].setdefault(cid, {}).setdefault(day, dict.fromkeys(SUMS, 0.0))
                    for f in SUMS:
                        cur[f] += row[f]
        snap["parts"].append("metrics")
    snap["calls"] = counter[0]
    return snap


# ---------------------------------------------------------------- Meta back-off

def meta_pressure(u):
    """Usage headers -> (highest % used, minutes until access is back)."""
    pcts, regain = [0.0], 0.0

    def nums(d, keys):
        for k in keys:
            with contextlib.suppress(TypeError, ValueError):
                pcts.append(float(d.get(k) or 0))
    for v in ((u or {}).get("x-business-use-case-usage") or {}).values() if isinstance((u or {}).get("x-business-use-case-usage"), dict) else []:
        for e in v if isinstance(v, list) else []:
            if isinstance(e, dict):
                nums(e, ("call_count", "total_cputime", "total_time"))
                with contextlib.suppress(TypeError, ValueError):
                    regain = max(regain, float(e.get("estimated_time_to_regain_access") or 0))
    for h, keys in (("x-ad-account-usage", ("acc_id_util_pct",)), ("x-fb-ads-insights-throttle", ("app_id_util_pct", "acc_id_util_pct")),
                    ("x-app-usage", ("call_count", "total_cputime", "total_time"))):
        if isinstance((u or {}).get(h), dict):
            nums(u[h], keys)
    return max(pcts), regain


def _backoff_minutes(pct, regain):
    if regain > 0:
        return regain + 5
    for at, mins in BACKOFF:
        if pct >= at:
            return mins
    return 0


def _meta_usage():
    try:
        import meta_api  # noqa: PLC0415
        return meta_api.usage()
    except Exception:  # noqa: BLE001
        return {}


# ---------------------------------------------------------------- evaluating

def _days(window, as_of):
    if window == "today":
        return [as_of]
    if window == "yesterday":
        return [as_of - dt.timedelta(days=1)]
    return [as_of - dt.timedelta(days=k) for k in range(1, 8)]


def _prev_days(window, as_of):
    if window == "today":
        return [as_of - dt.timedelta(days=7)]
    if window == "yesterday":
        return [as_of - dt.timedelta(days=8)]
    return [as_of - dt.timedelta(days=k) for k in range(8, 15)]


def totals(daily, days, scale=1.0):
    t = dict.fromkeys(SUMS, 0.0)
    for d in days:
        row = daily.get(d.isoformat()) or {}
        for k in SUMS:
            t[k] += float(row.get(k) or 0) * scale
    return t


def value_of(metric, t, budget, ndays):
    if metric in ("spend", "clicks", "impressions", "conversions"):
        return t[metric]
    if metric == "cpc":
        return t["spend"] / t["clicks"] if t["clicks"] else None
    if metric == "ctr":
        return t["clicks"] / t["impressions"] * 100 if t["impressions"] else None
    if metric == "cpa":
        return t["spend"] / t["conversions"] if t["conversions"] else None
    if metric == "roas":
        return t["value"] / t["spend"] if t["spend"] else None
    if metric == "spend_vs_budget":
        return t["spend"] / (budget * ndays) * 100 if budget else None
    return None


def _sum_daily(maps):
    out = {}
    for m in maps:
        for day, row in m.items():
            cur = out.setdefault(day, dict.fromkeys(SUMS, 0.0))
            for k in SUMS:
                cur[k] += float(row.get(k) or 0)
    return out


def _objects(rule, snap):
    """[{level, id, name, active, budget, daily, campaign_id?}] the rule looks at in this account."""
    lvl = rule["level"]
    if lvl == "account":
        camps = list(snap["campaigns"].values())
        seen, budget = set(), 0.0
        for c in camps:
            if c["active"] and c.get("budget") and c.get("budget_id") not in seen:
                seen.add(c.get("budget_id") or c["id"])
                budget += c["budget"]
        for s in snap["adsets"].values():  # Meta ad set budgets (ABO) of active campaigns
            camp = snap["campaigns"].get(s["campaign_id"]) or {}
            if s["active"] and s.get("budget") and camp.get("active") and not camp.get("budget"):
                budget += s["budget"]
        return [{"level": "account", "id": snap["account_id"], "name": snap["name"], "active": any(c["active"] for c in camps),
                 "budget": budget or None, "daily": _sum_daily(snap["daily"]["campaign"].values())}]
    if lvl == "campaign":
        out = []
        for c in snap["campaigns"].values():
            budget = c.get("budget")
            if not budget and snap["platform"] == "meta":  # ABO: the sum of its active ad sets' budgets
                budget = sum(s["budget"] or 0 for s in snap["adsets"].values() if s["campaign_id"] == c["id"] and s["active"]) or None
            out.append({"level": "campaign", "id": c["id"], "name": c["name"], "active": c["active"], "budget": budget,
                        "started": c.get("started"),
                        "daily": snap["daily"]["campaign"].get(c["id"], {}), "campaign_id": c["id"]})
    else:
        out = []
        for s in snap["adsets"].values():
            camp = snap["campaigns"].get(s["campaign_id"]) or {}
            out.append({"level": "adset", "id": s["id"], "name": s["name"], "active": s["active"] and camp.get("active", True),
                        "budget": s.get("budget") or camp.get("budget"), "daily": snap["daily"]["adset"].get(s["id"], {}),
                        "campaign_id": s["campaign_id"], "learning": s.get("learning"),
                        "started": max([x for x in (s.get("started"), camp.get("started")) if x] or [None]) if (s.get("started") or camp.get("started")) else None})
    if rule.get("object_id"):
        out = [o for o in out if o["id"] == rule["object_id"]]
    else:
        out = [o for o in out if o["active"]]
    return out


def _fmt(metric, v, cur):
    if v is None:
        return "n/a"
    if metric in ("spend", "cpc", "cpa"):
        return f"{v:,.2f} {cur}".strip()
    if metric in ("ctr",):
        return f"{v:.2f}%"
    if metric == "spend_vs_budget":
        return f"{v:.0f}%"
    if metric == "roas":
        return f"{v:.2f}"
    return f"{v:,.0f}"


def _label(o):
    if o["level"] == "account":
        return ""
    return f"{'Campaign' if o['level'] == 'campaign' else 'Ad set'} {o['name']}"


def evaluate(rule, snap):
    """-> [hit] for one rule on one account's snapshot. A hit: {key, object, value, previous, text, details}."""
    metric = rule["metric"]
    label, kind, _ = METRICS[metric]
    as_of = dt.date.fromisoformat(snap["today"])
    cur = snap.get("currency", "")
    hits = []
    head = f"{snap['name']} ({'Google Ads' if snap['platform'] == 'google' else 'Meta'})"

    def hit(o, value, prev, text, details=None):
        where = _label(o)
        hits.append({"key": f"{rule['id']}|{snap['account_id']}|{o['level']}:{o['id']}",
                     "object": {"level": o["level"], "id": o["id"], "name": o["name"]}, "value": value, "previous": prev,
                     "text": f"{head}: {where + ': ' if where else ''}{text}", "details": details or []})

    if metric == "disapproved_ads":
        objs = _objects(rule, snap) if rule["level"] != "account" else [{"level": "account", "id": snap["account_id"], "name": snap["name"]}]
        for o in objs:
            ads = [a for a in snap["disapproved"] if o["level"] == "account" or
                   (o["level"] == "campaign" and a["campaign_id"] == o["id"]) or (o["level"] == "adset" and a["adset_id"] == o["id"])]
            n = len(ads)
            if (n > rule["value"]) if rule["op"] == "above" else (n < rule["value"]):
                names = [f"{a['name']} ({a['campaign'] or a['campaign_id']})" for a in ads[:5]]
                hit(o, n, None, f"{n} disapproved ad{'s' if n != 1 else ''}" + (": " + "; ".join(names) if names else "")
                    + (f" and {n - 5} more" if n > 5 else ""), names)
        return hits
    if metric == "learning_limited":
        sets = [s for s in snap["adsets"].values() if s["active"] and s.get("learning") == "FAIL"
                and (snap["campaigns"].get(s["campaign_id"]) or {}).get("active", True)]
        if rule["level"] == "adset":
            for o in _objects(rule, snap):
                if o.get("learning") == "FAIL" and o["active"] and (1 > rule["value"] if rule["op"] == "above" else 1 < rule["value"]):
                    hit(o, 1, None, "stuck in learning limited (Meta can't get enough results to finish learning)")
            return hits
        objs = _objects(rule, snap) if rule["level"] == "campaign" else [{"level": "account", "id": snap["account_id"], "name": snap["name"]}]
        for o in objs:
            mine = [s for s in sets if o["level"] == "account" or s["campaign_id"] == o["id"]]
            n = len(mine)
            if (n > rule["value"]) if rule["op"] == "above" else (n < rule["value"]):
                names = [s["name"] for s in mine[:5]]
                hit(o, n, None, f"{n} ad set{'s' if n != 1 else ''} in learning limited: " + "; ".join(names), names)
        return hits
    if metric == "no_delivery":
        y = as_of - dt.timedelta(days=1)
        ts = snap.get("ts") or _now()
        for o in _objects(rule, snap):
            if not o["active"] or (o["level"] != "account" and not o.get("budget") and snap["platform"] == "google"):
                continue
            if o.get("started") and ts - o["started"] < NEW_FOR:
                continue  # started less than 24 hours ago: no impressions yet is no news
            if o["level"] == "account" and snap["campaigns"] and all(
                    c.get("started") and ts - c["started"] < NEW_FOR for c in snap["campaigns"].values() if c["active"]):
                continue
            imp = totals(o["daily"], [y, as_of])["impressions"]
            if imp == 0:
                hit(o, 0, None, "no impressions yesterday or today (at least 24 hours) although it is active")
        return hits
    days, prev_days = _days(rule["window"], as_of), _prev_days(rule["window"], as_of)
    scale = max(snap.get("day_part") or 0, 1 / 24) if rule["window"] == "today" else 1.0
    for o in _objects(rule, snap):
        t = totals(o["daily"], days)
        if any(t[k] < v for k, v in rule["min"].items()):
            continue
        v = value_of(metric, t, o.get("budget"), len(days))
        if v is None:
            continue
        win = WINDOWS[rule["window"]]
        if rule["op"] in ("above", "below"):
            if (v > rule["value"]) if rule["op"] == "above" else (v < rule["value"]):
                extra = f" (daily budget {_fmt('spend', o['budget'], cur)})" if metric == "spend_vs_budget" else ""
                hit(o, round(v, 4), None, f"{label} {win} is {_fmt(metric, v, cur)}{extra}, {rule['op']} "
                                          f"{_fmt(metric, rule['value'], cur)}")
            continue
        pt = totals(o["daily"], prev_days, scale)
        if any(pt[k] < x for k, x in rule["min"].items()):
            continue
        p = value_of(metric, pt, o.get("budget"), len(prev_days))
        if not p:
            continue
        ch = (v - p) / p * 100
        if (ch >= rule["value"]) if rule["op"] == "up_pct" else (-ch >= rule["value"]):
            prev_txt = {"today": "the same weekday last week (same part of the day)", "yesterday": "the same weekday a week before",
                        "last_7_days": "the 7 days before"}[rule["window"]]
            hit(o, round(v, 4), round(p, 4), f"{label} {win} is {_fmt(metric, v, cur)}, {'up' if ch > 0 else 'down'} "
                                             f"{abs(ch):.0f}% on {prev_txt} ({_fmt(metric, p, cur)})")
    return hits


def needs(rule_list):
    n = {"metrics": False, "disapproved": False, "adsets": False, "adset_level": False}
    for r in rule_list:
        kind = METRICS[r["metric"]][1]
        if kind in ("sum", "ratio") or r["metric"] == "no_delivery":
            n["metrics"] = True
        if r["metric"] == "disapproved_ads":
            n["disapproved"] = True
        if r["level"] == "adset" or r["metric"] in ("learning_limited", "spend_vs_budget"):
            n["adsets"] = True  # Meta: ad sets (learning, ABO budgets); Google ignores it
        if r["level"] == "adset" and (kind in ("sum", "ratio") or r["metric"] == "no_delivery"):
            n["adset_level"] = True
    return n


def applies(rule, a):
    return rule.get("enabled") and rule["platform"] in ("any", a["platform"]) and rule["account"] in ("*", a["id"])


# ---------------------------------------------------------------- the check

def _fetch(a, need, tz_hint=None, now=None):
    if a["platform"] == "google":
        return fetch_google(a, need, now=now)
    import meta_ads  # noqa: PLC0415
    meta_ads.use(a["key"])
    return fetch_meta(a, need, now=now, tz_hint=tz_hint)


def _tz_hint(a, st):
    tz = st["tz"].get(a["id"])
    if tz:
        return tz
    try:  # Studio's own Meta cache for this ad account (no API call)
        f = core.STUDIO_DIR / f"meta_cache_{a['id']}.json"
        return (json.loads(f.read_text()).get("account") or {}).get("timezone_name")
    except (OSError, ValueError, AttributeError):
        return None


def check(which=None, accounts=None, reason="manual", fetch=None, now=None, min_gap=0):
    """Evaluate the switched-on rules (which: "hourly", "daily" or None = all) on fresh reads for every account they
    apply to (or only `accounts`, ids). Read-only towards Google and Meta. -> {at, reason, checked, fired, busy?}"""
    if not _check_lock.acquire(blocking=False):
        return {"busy": True, "at": _iso(), "reason": reason, "checked": [], "fired": [],
                "message": "A check is already running"}
    try:
        _running.update(at=_iso(), reason=reason)
        return _check(which, accounts, reason, fetch or _fetch, now, min_gap)
    finally:
        _running.update(at=None, reason=None)
        _check_lock.release()


def _check(which, accounts, reason, fetch, now, min_gap):
    t0 = now if now is not None else _now()
    live = [r for r in rules() if r.get("enabled") and (which is None or cadence(r) == which)]
    out = {"at": _iso(t0), "reason": reason, "checked": [], "fired": [], "suppressed": 0}
    if not live:
        out["message"] = "No alert rules are switched on" + ("" if which is None else f" for the {which} check")
        return out
    accts = _accounts()
    if accounts:
        want = {str(x) for x in accounts}
        accts = [a for a in accts if a["id"] in want or a["key"] in want]
    st = _state()
    fired = []
    for a in accts:
        mine = [r for r in live if applies(r, a)]
        if not mine:
            continue
        row = {"account": a["id"], "name": a["name"], "platform": a["platform"], "rules": len(mine), "at": _iso(t0)}
        last = (st["last_check"].get(a["id"]) or {}).get("ts") or 0
        if min_gap and t0 - last < min_gap:
            row.update(skipped=f"checked {int(t0 - last)}s ago")
            out["checked"].append(row)
            continue
        if a["platform"] == "meta":
            bo = st["meta_backoff"]
            if float(bo.get("until") or 0) > t0:
                row.update(skipped=f"Meta asked Studio to slow down: paused until {_iso(bo['until'])[11:16]} ({bo.get('why', '')})")
                out["checked"].append(row)
                continue
            pct, regain = meta_pressure(_meta_usage())
            mins = _backoff_minutes(pct, regain)
            if mins:
                st["meta_backoff"] = {"until": t0 + mins * 60, "why": f"usage {pct:.0f}%"}
                row.update(skipped=f"Meta usage is at {pct:.0f}%: paused for {mins:.0f} minutes")
                out["checked"].append(row)
                continue
        try:
            snap = fetch(a, needs(mine), _tz_hint(a, st), now) if a["platform"] == "meta" else fetch(a, needs(mine), None, now)
            st["tz"][a["id"]] = snap["tz"]
            row.update(ok=True, calls=snap.get("calls"), today=snap["today"])
            for r in mine:
                hs = []
                for h in evaluate(r, snap):  # quiet / snoozed ones out first, so the cap never hides objects 21+
                    if float(st["snoozed"].get(h["key"]) or 0) > t0 or t0 - float(st["last_fired"].get(h["key"]) or 0) < r["quiet_hours"] * 3600:
                        out["suppressed"] += 1
                    else:
                        hs.append(h)
                hs.sort(key=lambda h: float(st["last_fired"].get(h["key"]) or 0))  # longest waiting first
                if len(hs) > MAX_PER_RULE:  # the rest fire at the next check (their quiet period has not started)
                    row["capped"] = row.get("capped", 0) + len(hs) - MAX_PER_RULE
                    out["capped"] = out.get("capped", 0) + len(hs) - MAX_PER_RULE
                    hs = hs[:MAX_PER_RULE]
                for h in hs:
                    st["last_fired"][h["key"]] = t0
                    fired.append({"id": "a_" + uuid.uuid4().hex[:12], "at": _iso(t0), "ts": t0, "rule_id": r["id"], "rule_name": r["name"],
                                  "account": a["id"], "account_name": a["name"], "platform": a["platform"], "currency": snap.get("currency", ""),
                                  "metric": r["metric"], "op": r["op"], "threshold": r["value"], "window": r["window"], "reason": reason,
                                  **h, "acked_at": None, "snoozed_until": None, "delivered": {}})
        except Exception as ex:  # noqa: BLE001 - one account failing must not stop the rest
            row.update(ok=False, error=_scrub(ex))
            if a["platform"] == "meta" and getattr(ex, "rate_limited", False):
                _, regain = meta_pressure(_meta_usage())
                st["meta_backoff"] = {"until": t0 + max(RATE_LIMIT_PAUSE, regain + 5) * 60, "why": "rate limited"}
        if a["platform"] == "meta" and row.get("ok") is not None:
            pct, regain = meta_pressure(_meta_usage())
            mins = _backoff_minutes(pct, regain)
            if mins and float(st["meta_backoff"].get("until") or 0) < t0 + mins * 60:
                st["meta_backoff"] = {"until": t0 + mins * 60, "why": f"usage {pct:.0f}% after this check"}
            row["meta_usage_pct"] = round(pct)
        st["last_check"][a["id"]] = {k: row.get(k) for k in ("at", "ok", "error", "skipped", "calls", "reason", "capped")} | {"ts": t0, "reason": reason}
        out["checked"].append(row)
    if fired:
        _deliver(fired)
    with _flock():
        disk = _state()  # snoozes and wake-ups made while checking win: a check never changes them
        st["snoozed"] = dict(disk["snoozed"])
        cut = t0 - 31 * 86400
        st["last_fired"] = {k: v for k, v in st["last_fired"].items() if v > cut}
        st["snoozed"] = {k: v for k, v in st["snoozed"].items() if float(v or 0) > t0}
        st["last_run"] = {"at": out["at"], "reason": reason, "fired": len(fired)}
        _write(STATE_FILE, st)
        if fired:
            hist = _read(HISTORY_FILE, [])
            _write(HISTORY_FILE, (list(reversed(fired)) + hist)[:HISTORY_MAX])
    out["fired"] = fired
    return out


def _scrub(ex):
    t = re.sub(r"(access_token|token|key)=[^&\s]+", r"\1=***", str(ex))
    try:
        from health import _scrub as hs  # noqa: PLC0415
        t = hs(t)
    except Exception:  # noqa: BLE001
        pass
    return t[:400]


# ---------------------------------------------------------------- delivery

MAC_SCRIPT = "on run argv\ndisplay notification (item 2 of argv) with title (item 1 of argv)\nend run"


def _osascript(title, text):
    """macOS notification, best effort. The texts go as arguments, never into the script."""
    if sys.platform != "darwin" or os.environ.get("MSTUDIO_NO_MAC_NOTIFY"):
        return "off"
    try:
        subprocess.run(["osascript", "-e", MAC_SCRIPT, title[:120], text[:400]], timeout=10, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return "shown"
    except Exception:  # noqa: BLE001
        return "failed"


def _team_send(text, data):
    try:
        import team_notify  # noqa: PLC0415
        return team_notify.send_alert(text, data)
    except Exception as ex:  # noqa: BLE001
        return "failed: " + str(ex)[:120]


def summary_text(fired):
    if len(fired) == 1:
        return fired[0]["text"]
    return f"{len(fired)} alerts: " + " | ".join(f["text"] for f in fired[:3]) + (f" | and {len(fired) - 3} more" if len(fired) > 3 else "")


def _deliver(fired):
    """Studio itself (history + badge) always; a macOS notification and the user's own team channels when switched on.
    Several alerts in one check go as one message."""
    s = _load_rules()["settings"]
    mac = _osascript("Marketing Studio alert" + ("s" if len(fired) > 1 else ""), summary_text(fired)) if s.get("mac_notify") else "off"
    if not s.get("team_notify"):
        team = "off"
    elif len(fired) <= 3:
        team = None
        for f in fired:
            r = _team_send(f["text"], {"alert": f["id"], "rule": f["rule_id"], "account": f["account"]})
            team = r if team in (None, "sent") else team
    else:
        team = _team_send(summary_text(fired), {"alerts": [f["id"] for f in fired[:20]]})
    for f in fired:
        f["delivered"] = {"studio": "shown", "mac": mac, "team": team}


# ---------------------------------------------------------------- history: acknowledge, snooze

def history(limit=200):
    return _read(HISTORY_FILE, [])[:max(1, min(int(limit or 200), HISTORY_MAX))]


def badge():
    h = _read(HISTORY_FILE, [])
    open_ = [x for x in h if not x.get("acked_at")]
    return {"unacked": len(open_), "latest": open_[0]["text"] if open_ else "", "running": bool(_running["at"])}


def ack(ids=None, every=False):
    with _flock():
        h = _read(HISTORY_FILE, [])
        want, n = {str(i) for i in ids or []}, 0
        for x in h:
            if not x.get("acked_at") and (every or x["id"] in want):
                x["acked_at"] = _iso()
                n += 1
        _write(HISTORY_FILE, h)
    return {"acknowledged": n, **badge()}


def snooze(aid, hours):
    """Snooze one fired alert: the same rule on the same object stays quiet for `hours` (0 = wake it up again).
    Snoozing also acknowledges it."""
    hours = _num(hours, "hours")
    if not 0 <= hours <= 24 * 90:
        raise ValueError("snooze for 0 to 2160 hours")
    with _flock():
        h = _read(HISTORY_FILE, [])
        x = next((y for y in h if y["id"] == aid), None)
        if not x:
            raise ValueError("no such alert")
        until = _now() + hours * 3600 if hours else None
        x["snoozed_until"] = _iso(until) if until else None
        x["acked_at"] = x.get("acked_at") or _iso()
        _write(HISTORY_FILE, h)
        st = _state()
        if until:
            st["snoozed"][x["key"]] = until
        else:
            st["snoozed"].pop(x["key"], None)
        _write(STATE_FILE, st)
    return {"alert": x, **badge()}


# ---------------------------------------------------------------- views

def _jobs():
    try:
        import scheduler  # noqa: PLC0415
        return [j for j in scheduler.status()["jobs"] if j["name"] in (JOB_HOURLY, JOB_DAILY)]
    except Exception:  # noqa: BLE001
        return []


def view():
    """Everything the Alerts tab shows. No API calls."""
    d = config()
    st = _state()
    try:
        accts = [{"id": a["id"], "name": a["name"], "platform": a["platform"]} for a in _accounts()]
    except Exception:  # noqa: BLE001
        accts = []
    try:
        import team  # noqa: PLC0415
        joined = team.joined()
    except Exception:  # noqa: BLE001
        joined = False
    rs = [{**r, "cadence": cadence(r), "description": describe(r),
           "last_fired": _iso(max((v for k, v in st["last_fired"].items() if k.startswith(r["id"] + "|")), default=0) or None)
           if any(k.startswith(r["id"] + "|") for k in st["last_fired"]) else None} for r in d["rules"]]
    bo = st["meta_backoff"]
    return {"rules": rs, "alerts": history(200), "badge": badge(), "settings": d["settings"], "accounts": accts,
            "last_check": st["last_check"], "last_run": st.get("last_run"), "running": _running["at"],
            "meta_backoff": {"until": _iso(bo["until"]), "why": bo.get("why", "")} if float(bo.get("until") or 0) > _now() else None,
            "policy": datapolicy.mode(), "jobs": _jobs(), "team": joined, "method": METHOD,
            "catalogue": {"metrics": {k: {"label": v[0], "kind": v[1], "cadence": v[2]} for k, v in METRICS.items()},
                          "ops": OPS, "windows": WINDOWS, "levels": list(LEVELS)}}


# ---------------------------------------------------------------- scheduler + refresh hook

def job_tz():
    """The daily check's time zone: the time zone of the first account with a switched-on rule that Studio has seen
    (from earlier checks), else the Meta ad account's from Studio's cache, else the Mac's. No API call."""
    import scheduler  # noqa: PLC0415
    try:
        seen = _state()["tz"]
        live = [r for r in rules() if r.get("enabled")]
        for a in _accounts():
            if seen.get(a["id"]) and any(applies(r, a) for r in live):
                return ZoneInfo(seen[a["id"]])
        for tz in seen.values():
            return ZoneInfo(tz)
    except Exception:  # noqa: BLE001
        pass
    return scheduler.account_tz()


def register_jobs():
    """server.py main(). Both held while Refresh data is manual; a run with no rule switched on reads nothing."""
    import scheduler  # noqa: PLC0415
    scheduler.register(JOB_HOURLY, lambda: check("hourly", reason="hourly"), every=3600, enabled_default=True, refresh=True,
                       timeout=900, label="Alerts: hourly check",
                       note="Spend, clicks, impressions, CPC, CTR, budget, delivery, disapproved ads and learning rules. "
                            "Reads Google / Meta (read-only) only for accounts with a rule switched on.")
    scheduler.register(JOB_DAILY, lambda: check("daily", reason="daily"), at="09:00", tz=job_tz, enabled_default=True, refresh=True,
                       timeout=900, label="Alerts: daily check",
                       note="Conversion, CPA and ROAS rules, once a day (conversions can take up to 15 hours to arrive).")


def after_refresh(source, account=None, wait=False):
    """After the user's own Refresh (Refresh data menu, an account's Refresh button): check the alert rules for the
    accounts just refreshed, in the background. Nothing happens when no rule applies to them."""
    source = (source or "").lower()
    if source not in ("all", "google", "meta"):
        return None
    try:
        accts = _accounts()
        if source in ("google", "meta"):
            accts = [a for a in accts if a["platform"] == source]
        if account:
            accts = [_find_account(account, accts)]
        live = [r for r in rules() if r.get("enabled")]
        ids = [a["id"] for a in accts if any(applies(r, a) for r in live)]
    except Exception:  # noqa: BLE001 - never break a refresh
        return None
    if not ids:
        return None
    t = threading.Thread(target=lambda: check(None, ids, reason="refresh", min_gap=MIN_GAP), name="alerts-after-refresh", daemon=True)
    t.start()
    if wait:
        t.join(120)
    return ids
