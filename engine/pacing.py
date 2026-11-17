"""Budget pacing for Google Ads and Meta: month-to-date spend, a forecast to month end, over/under flags against a
monthly target, and draft budget changes that would land each account on its target. Nothing here changes a budget:
recommendations only ever go into the normal Ads draft (core.add / meta_ads.add), where they get the usual checks,
the live preview, team approval gating and Push live with undo (the user's own buttons).

Targets (per account, optionally per campaign; account currency, per calendar month) are the team setting
"pacing_targets" (team_settings.py): this Mac's own value when solo, the team's when the team has shared one.
  {"accounts": {"<account id>": {"monthly": 5000, "campaigns": {"<campaign id>": 1200}}}, "tolerance_pct": 5,
   "meta_step_pct": 20}
Account ids: the Google customer id ("8931136258") or the Meta key "meta_<ad account number>".

Reads (cached per account in <data>/pacing_<account id>.json, kept until a Refresh, datapolicy source
"pacing:<account id>"):
  Google  GAQL search: customer time zone + currency; campaigns with their budget (amount, period, shared, total
          amount) and status; metrics.cost_micros by segments.date from the earlier of the 1st of the month and 28
          days ago to today (account time zone).
  Meta    Graph GETs: the ad account (currency, time zone), campaigns (CBO budget, budget_remaining), ad sets (ABO
          budget, learning_stage_info), insights spend by day (time_increment=1, the advertiser's time zone).

Forecast (per campaign, account = sum of its campaigns), see forecast():
  month-to-date (completed days) + today's expected spend + the rest of the month at a weekday-aware run-rate.
  Run-rate: the last 7 completed days, adjusted by weekday factors learned from the last 28 completed days (shrunk
  towards 1 and only used with 14+ days of history). A paused campaign forecasts nothing more. A campaign with a
  daily budget is capped at that budget per remaining day (Google charges at most about 30.4x the daily budget in a
  month; Meta keeps a week within 7x).

Recommendations (recommend()): "set the daily budget to X" so the campaign lands on its target (its own, or its share
of the account target). Google: rules on its 2x day / 30.4x month behaviour and big jumps; shared and total budgets
are left alone. Meta: one step changes the budget by at most meta_step_pct (default 20%) because big or frequent
budget changes restart learning, ad sets in learning are flagged, and at most one budget change per object goes into
the draft (Meta allows 4 budget changes per ad set per hour).

Scheduler job "pacing" (off by default, refresh=True so it is held while Refresh data is manual): daily, refreshes
every account and records drift (accounts forecast to miss their target by more than the tolerance) in
<data>/pacing_drift.json, shown in the Pacing view. It never changes a budget.
"""
import calendar
import concurrent.futures
import datetime as dt
import json
import math
import re
import threading
import time
from zoneinfo import ZoneInfo

import core
import datapolicy

HISTORY_DAYS = 28
RECENT_DAYS = 7
MIN_DAYS_FOR_WEEKDAYS = 14
SHRINK = 2.0  # weekday factors: pseudo-observations pulling each factor towards 1
TOLERANCE_PCT = 5.0
META_STEP_PCT = 20.0
GOOGLE_DAY_MAX = 2.0  # Google may spend up to 2x the daily budget on one day
GOOGLE_MONTH_DAYS = 30.4  # and charges at most about 30.4x the daily budget in a calendar month
BIG_JUMP = 0.5  # Google: a change of more than 50% is called out
UNDER_USED = 0.8  # recent spend under 80% of the budget: the budget is not what limits it
MIN_BUDGET = 1.0
DRIFT_FILE = "pacing_drift.json"
SOURCE = "pacing"
PACING_NOTE = "Pacing:"
_locks = {}
_locks_lock = threading.Lock()

G_CAMPAIGNS = ("SELECT campaign.id, campaign.name, campaign.status, campaign.serving_status, campaign.advertising_channel_type, "
               "campaign.bidding_strategy_type, campaign.primary_status_reasons, campaign_budget.amount_micros, "
               "campaign_budget.period, campaign_budget.explicitly_shared, campaign_budget.total_amount_micros "
               "FROM campaign WHERE campaign.status != 'REMOVED'")
G_COST = ("SELECT campaign.id, campaign.name, campaign.status, segments.date, metrics.cost_micros FROM campaign "
          "WHERE segments.date BETWEEN '{start}' AND '{end}' AND metrics.cost_micros > 0")
G_CUSTOMER = "SELECT customer.time_zone, customer.currency_code, customer.descriptive_name FROM customer LIMIT 1"
SMART = {"MAXIMIZE_CONVERSIONS", "MAXIMIZE_CONVERSION_VALUE", "TARGET_CPA", "TARGET_ROAS", "TARGET_SPEND", "TARGET_IMPRESSION_SHARE"}
M_CAMP = "id,name,status,effective_status,daily_budget,lifetime_budget,budget_remaining,stop_time"
M_ADSET = "id,name,campaign_id,status,effective_status,daily_budget,lifetime_budget,budget_remaining,learning_stage_info,end_time"
M_LIVE_ADSET = ["ACTIVE", "PAUSED", "CAMPAIGN_PAUSED", "IN_PROCESS", "WITH_ISSUES"]  # not DELETED / ARCHIVED
LEARNING = {"LEARNING": "in the learning phase", "FAIL": "learning limited"}


# ---------------------------------------------------------------- small helpers

def _d(s):
    return s if isinstance(s, dt.date) else dt.date.fromisoformat(str(s)[:10])


def _r2(v):
    return None if v is None else round(float(v) + 0.0, 2)


def _lock(acct_id):
    with _locks_lock:
        return _locks.setdefault(acct_id, threading.Lock())


def month_bounds(as_of):
    first = as_of.replace(day=1)
    last = as_of.replace(day=calendar.monthrange(as_of.year, as_of.month)[1])
    return first, last


def _days(a, b):
    """Every date from a to b inclusive."""
    return [a + dt.timedelta(days=i) for i in range((b - a).days + 1)] if b >= a else []


def _today(tz_name, now=None):
    try:
        tz = ZoneInfo(tz_name) if tz_name else dt.timezone.utc
    except Exception:  # noqa: BLE001 - unknown zone name: UTC
        tz = dt.timezone.utc
    return (now or dt.datetime.now(dt.timezone.utc)).astimezone(tz).date()


def _scrub(ex):
    try:
        from health import _scrub as hs  # noqa: PLC0415
        return hs(str(ex))[:400]
    except Exception:  # noqa: BLE001
        return str(ex)[:400]


# ---------------------------------------------------------------- forecast

def weekday_factors(daily, days, shrink=SHRINK):
    """{weekday 0-6: factor} from spend on `days`; mean factor 1. Flat (all 1) when there is no spend or under
    MIN_DAYS_FOR_WEEKDAYS days of history."""
    flat = {w: 1.0 for w in range(7)}
    if len(days) < MIN_DAYS_FOR_WEEKDAYS:
        return flat
    vals = [float(daily.get(d, 0.0)) for d in days]
    mean = sum(vals) / len(vals)
    if mean <= 0:
        return flat
    out = {}
    for w in range(7):
        xs = [float(daily.get(d, 0.0)) for d in days if d.weekday() == w]
        raw = (sum(xs) / len(xs)) / mean if xs else 1.0
        out[w] = (len(xs) * raw + shrink) / (len(xs) + shrink)
    m = sum(out.values()) / 7
    return {w: v / m for w, v in out.items()} if m > 0 else flat


def forecast(daily, as_of, budget=None, active=True):
    """Forecast one campaign (or ad set) to month end. daily: {date: spend} (dates or ISO strings), as_of: the
    account's today. budget: its daily budget when it has one (caps the rest of the month), else None.
    -> {mtd, mtd_done, today, today_expected, rest, total, base, days_left, future_days, history_days, weekday, enough}
    enough: False for an active campaign with no spend in the last HISTORY_DAYS completed days (new, or just switched on):
    there is no run-rate, so its "forecast" would only be what it spent so far. The view shows "not enough data" then."""
    daily = {_d(k): float(v or 0) for k, v in (daily or {}).items()}
    first, last = month_bounds(as_of)
    mtd_done = sum(daily.get(d, 0.0) for d in _days(first, as_of - dt.timedelta(days=1)))
    today = daily.get(as_of, 0.0)
    hist = [as_of - dt.timedelta(days=k) for k in range(1, HISTORY_DAYS + 1)]  # newest first
    spent = [d for d in hist if daily.get(d, 0.0) > 0]
    future = _days(as_of + dt.timedelta(days=1), last)
    out = {"mtd_done": _r2(mtd_done), "today": _r2(today), "mtd": _r2(mtd_done + today), "days_left": len(future) + 1,
           "future_days": len(future), "history_days": 0, "weekday": False, "base": 0.0, "enough": bool(spent) or not active}
    if not active or not spent:
        out.update(today_expected=_r2(today), rest=0.0, total=_r2(mtd_done + today))
        return out
    since = min(spent)
    used = [d for d in hist if d >= since]
    f = weekday_factors(daily, used)
    recent = used[:RECENT_DAYS]
    wsum = sum(f[d.weekday()] for d in recent)
    base = sum(daily.get(d, 0.0) for d in recent) / wsum if wsum else 0.0
    exp_today = base * f[as_of.weekday()]
    rest = sum(base * f[d.weekday()] for d in future)
    if budget:
        exp_today = min(exp_today, float(budget))
        rest = min(rest, float(budget) * len(future))
    exp_today = max(today, exp_today)
    out.update(base=_r2(base), today_expected=_r2(exp_today), rest=_r2(rest), total=_r2(mtd_done + exp_today + rest),
               history_days=len(used), weekday=len(used) >= MIN_DAYS_FOR_WEEKDAYS)
    return out


def needed_daily(target, mtd_done, days_left):
    """Spend per day from today (today included) that lands month-to-date spend on target."""
    if target is None or days_left <= 0:
        return None
    return max(0.0, (float(target) - float(mtd_done)) / days_left)


def flag(total, target, tol_pct):
    """-> (state over|under|on_track|no_target, amount forecast - target, pct)."""
    if not target:
        return "no_target", None, None
    diff = float(total) - float(target)
    pct = diff / float(target) * 100
    if pct > tol_pct:
        return "over", _r2(diff), round(pct, 1)
    if pct < -tol_pct:
        return "under", _r2(diff), round(pct, 1)
    return "on_track", _r2(diff), round(pct, 1)


# ---------------------------------------------------------------- reading Google

def fetch_google(cid, query=None, now=None):
    """One Google account's pacing snapshot (read-only GAQL)."""
    if query is None:
        import live  # noqa: PLC0415
        query = live.query
    cust = (query(cid, G_CUSTOMER) or [{}])[0].get("customer", {})
    tz = cust.get("timeZone") or "Europe/London"
    as_of = _today(tz, now)
    start = min(as_of.replace(day=1), as_of - dt.timedelta(days=HISTORY_DAYS))
    camps = {}
    for r in query(cid, G_CAMPAIGNS):
        c, b = r.get("campaign", {}), r.get("campaignBudget", {})
        period = b.get("period") or "DAILY"
        total = b.get("totalAmountMicros")
        kind = ("shared" if b.get("explicitlyShared") else "total" if total and str(total) != "0" else
                "daily" if period == "DAILY" else period.lower())
        status = c.get("status") or ""
        camps[str(c.get("id"))] = {
            "id": str(c.get("id")), "name": c.get("name", ""), "status": status.title(),
            "active": status == "ENABLED" and c.get("servingStatus") not in ("ENDED", "SUSPENDED", "PENDING"),
            "budget": _r2(int(b.get("amountMicros")) / 1e6) if int(b.get("amountMicros") or 0) else None,
            "budget_kind": kind, "total_budget": _r2(int(total) / 1e6) if total and str(total) != "0" else None,
            "channel": (c.get("advertisingChannelType") or "").replace("_", " ").title(),
            "smart_bidding": (c.get("biddingStrategyType") or "") in SMART,
            "budget_limited": "BUDGET_CONSTRAINED" in (c.get("primaryStatusReasons") or []),
            "daily": {}, "units": []}
    for r in query(cid, G_COST.format(start=start.isoformat(), end=as_of.isoformat())):
        c = r.get("campaign", {})
        k = str(c.get("id"))
        if k not in camps:  # removed this month: its spend still counts
            camps[k] = {"id": k, "name": c.get("name", ""), "status": (c.get("status") or "").title(), "active": False,
                        "budget": None, "budget_kind": "none", "total_budget": None, "channel": "", "smart_bidding": False,
                        "budget_limited": False, "daily": {}, "units": []}
        day = r.get("segments", {}).get("date")
        camps[k]["daily"][day] = round(camps[k]["daily"].get(day, 0.0) + int(r.get("metrics", {}).get("costMicros") or 0) / 1e6, 2)
    for c in camps.values():
        c["units"] = [{"level": "campaign", "id": c["id"], "name": c["name"], "budget": c["budget"], "budget_kind": c["budget_kind"],
                       "active": c["active"], "daily": c["daily"], "learning": None}]
    return {"platform": "google", "account_id": str(cid), "currency": cust.get("currencyCode", ""), "tz": tz,
            "today": as_of.isoformat(), "fetched_at": dt.datetime.now().isoformat(timespec="seconds"), "_at": time.time(),
            "campaigns": sorted(camps.values(), key=lambda c: c["name"].lower())}


# ---------------------------------------------------------------- reading Meta

def _meta_money(v):
    return round(int(v) / 100, 2) if v not in (None, "", "0", 0) else None


def _insights_by_day(get_all, act, level, start, end):
    rows = get_all(f"{act}/insights", level=level, time_increment=1, fields=f"{level}_id,spend",
                   time_range={"since": start.isoformat(), "until": end.isoformat()})
    out = {}
    for r in rows:
        k = str(r.get(f"{level}_id"))
        day = r.get("date_start")
        out.setdefault(k, {})
        out[k][day] = round(out[k].get(day, 0.0) + float(r.get("spend") or 0), 2)
    return out


def fetch_meta(act, get=None, get_all=None, now=None):
    """One Meta ad account's pacing snapshot (read-only Graph GETs). act: "act_<number>"."""
    if get is None or get_all is None:
        import meta_ads  # noqa: PLC0415
        get, get_all = get or meta_ads.get, get_all or meta_ads.get_all
    acc = get(act, fields="name,currency,timezone_name")
    tz = acc.get("timezone_name") or "Europe/London"
    as_of = _today(tz, now)
    start = min(as_of.replace(day=1), as_of - dt.timedelta(days=HISTORY_DAYS))
    # campaigns unfiltered: an archived or deleted campaign that spent this month still counts towards the month
    camps = get_all(f"{act}/campaigns", fields=M_CAMP)
    sets = get_all(f"{act}/adsets", fields=M_ADSET, effective_status=M_LIVE_ADSET)
    by_c = _insights_by_day(get_all, act, "campaign", start, as_of)
    abo = {str(c["id"]) for c in camps if not (c.get("daily_budget") or c.get("lifetime_budget"))}
    by_s = _insights_by_day(get_all, act, "adset", start, as_of) if abo else {}
    out = []
    for c in camps:
        cid = str(c["id"])
        daily = by_c.get(cid, {})
        if c.get("status") in ("DELETED", "ARCHIVED") and not any(_d(k) >= as_of.replace(day=1) for k in daily):
            continue
        mine = [s for s in sets if str(s.get("campaign_id")) == cid and s.get("status") not in ("DELETED", "ARCHIVED")]
        live_sets = [s for s in mine if s.get("effective_status") == "ACTIVE" and not _ended(s.get("end_time"), as_of)]
        active = c.get("effective_status") == "ACTIVE" and bool(live_sets) and not _ended(c.get("stop_time"), as_of)
        learn = [LEARNING.get((s.get("learning_stage_info") or {}).get("status")) for s in live_sets]
        row = {"id": cid, "name": c.get("name", ""), "status": (c.get("effective_status") or c.get("status") or "").replace("_", " ").title(),
               "active": active, "channel": "Meta", "smart_bidding": False, "budget_limited": False, "daily": daily, "units": []}
        if cid in abo:
            row.update(budget_kind="adsets", budget=_r2(sum(_meta_money(s.get("daily_budget")) or 0 for s in live_sets)) or None,
                       total_budget=None)
            for s in mine:
                sk = "daily" if s.get("daily_budget") else "lifetime" if s.get("lifetime_budget") else "none"
                row["units"].append({"level": "adset", "id": str(s["id"]), "name": s.get("name", ""),
                                     "budget": _meta_money(s.get("daily_budget")), "budget_kind": sk,
                                     "lifetime_budget": _meta_money(s.get("lifetime_budget")),
                                     "budget_remaining": _meta_money(s.get("budget_remaining")),
                                     "active": s in live_sets, "daily": by_s.get(str(s["id"]), {}),
                                     "learning": LEARNING.get((s.get("learning_stage_info") or {}).get("status"))})
        else:
            kind = "daily" if c.get("daily_budget") else "lifetime"
            row.update(budget_kind=kind, budget=_meta_money(c.get("daily_budget")), total_budget=_meta_money(c.get("lifetime_budget")))
            row["units"].append({"level": "campaign", "id": cid, "name": row["name"], "budget": row["budget"], "budget_kind": kind,
                                 "budget_remaining": _meta_money(c.get("budget_remaining")), "active": active, "daily": daily,
                                 "learning": next((x for x in learn if x), None)})
        out.append(row)
    return {"platform": "meta", "account_id": "meta_" + act.replace("act_", ""), "act": act, "currency": acc.get("currency", ""),
            "tz": tz, "today": as_of.isoformat(), "fetched_at": dt.datetime.now().isoformat(timespec="seconds"), "_at": time.time(),
            "campaigns": sorted(out, key=lambda c: c["name"].lower())}


def _ended(iso, as_of):
    if not iso:
        return False
    try:
        return _d(iso) < as_of
    except ValueError:
        return False


# ---------------------------------------------------------------- accounts, cache

def accounts():
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


def find_account(ref, accts=None):
    """An account by id ("8931136258", "meta_644...") or key ("ml", "meta")."""
    ref = str(ref or "").strip()
    for a in accts if accts is not None else accounts():
        if ref in (a["id"], a["key"]):
            return a
    raise ValueError(f"unknown ad account {ref!r}")


def _file(acct_id):
    return core.STUDIO_DIR / f"pacing_{re.sub(r'[^A-Za-z0-9_]', '_', acct_id)}.json"


def _src(acct_id):
    return f"{SOURCE}:{acct_id}"


def cached(acct_id):
    try:
        return json.loads(_file(acct_id).read_text())
    except (OSError, ValueError):
        return None


def _fetch(a, now=None):
    if a["platform"] == "google":
        return fetch_google(a["id"], now=now)
    import meta_ads  # noqa: PLC0415
    meta_ads.use(a["key"])
    return fetch_meta(a["act"], now=now)


def refresh(account=None):
    """The Refresh menu's Pacing item and the Pacing Refresh button: read every account (or one) again now. Read-only."""
    datapolicy.invalidate(SOURCE)
    v = status(account, refresh=True)
    return {"accounts": [{"account": x["account"], "fetched_at": x["fetched_at"]} for x in v["accounts"]], "errors": v["errors"]}


def snapshot(a, force=False, fetch=None):
    """The account's cached snapshot, or a new one: when there is none yet, after a Refresh (datapolicy), or force."""
    with _lock(a["id"]):
        d = cached(a["id"])
        if d and not force:
            age = time.time() - float(d.get("_at") or 0)
            if datapolicy.fresh(_src(a["id"]), age, None, d.get("_at")):
                return d
        d = (fetch or _fetch)(a)
        d["name"] = a["name"]
        f = _file(a["id"])
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False))
        tmp.replace(f)
        datapolicy.mark(_src(a["id"]))
        return d


def invalidate(acct_id):
    """After a push or undo on this account: its pacing is read again when next shown."""
    try:
        datapolicy.invalidate(_src(str(acct_id)))
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------- targets (team setting "pacing_targets")

def targets():
    try:
        import team_settings  # noqa: PLC0415
        v = team_settings.get("pacing_targets")
    except Exception:  # noqa: BLE001 - team settings unavailable: nothing set
        v = None
    v = dict(v or {})
    v.setdefault("accounts", {})
    v["tolerance_pct"] = float(v.get("tolerance_pct") or TOLERANCE_PCT)
    v["meta_step_pct"] = float(v.get("meta_step_pct") or META_STEP_PCT)
    return v


def targets_info():
    try:
        import team_settings  # noqa: PLC0415
        i = team_settings.info()
        k = i["keys"]["pacing_targets"]
        return {"source": k["source"], "editable": k["editable"], "joined": i["joined"], "can_admin": i["can_admin"],
                "team": bool(k["team"]), "updated_by": (k["team"] or {}).get("updated_by", "")}
    except Exception:  # noqa: BLE001
        return {"source": "local", "editable": True, "joined": False, "can_admin": False, "team": False, "updated_by": ""}


def save_targets(body):
    """body: {account: id, monthly: number|null, campaigns: {id: number|null}} for one account, or
    {tolerance_pct, meta_step_pct}; share: true saves it as the team's value (owners and admins); unshare: true stops
    sharing (everyone goes back to their own). Without share, targets stay on this Mac (team_settings "local"); only
    when the team already shares them does an owner's or admin's edit change the team's value (as the view says)."""
    import team_settings  # noqa: PLC0415
    if body.get("unshare"):
        team_settings.save("pacing_targets", None, "team")
        return {"targets": targets(), "info": targets_info()}
    cur = targets()
    val = {"accounts": json.loads(json.dumps(cur["accounts"])), "tolerance_pct": cur["tolerance_pct"], "meta_step_pct": cur["meta_step_pct"]}
    if body.get("account"):
        aid = str(body["account"])
        acc = val["accounts"].get(aid) or {}
        if "monthly" in body:
            acc["monthly"] = None if body["monthly"] in (None, "") else float(body["monthly"])
        for cid, v in (body.get("campaigns") or {}).items():
            camps = acc.setdefault("campaigns", {})
            if v in (None, ""):
                camps.pop(str(cid), None)
            else:
                camps[str(cid)] = float(v)
        if acc.get("monthly") is None and not acc.get("campaigns"):
            val["accounts"].pop(aid, None)
        else:
            val["accounts"][aid] = acc
    for k in ("tolerance_pct", "meta_step_pct"):
        if body.get(k) not in (None, ""):
            val[k] = float(body[k])
    team_settings.save("pacing_targets", val, "team" if body.get("share") is True else "local")
    return {"targets": targets(), "info": targets_info()}


# ---------------------------------------------------------------- one account's view

def _unit_rec(platform, unit, camp, need, tg, as_of):
    """One budget recommendation for a campaign (Google, Meta CBO) or ad set (Meta ABO). need: spend per day it should
    make from today. -> dict or None when nothing should change."""
    if need is None or unit["budget_kind"] != "daily" or not unit.get("budget") or not unit.get("active"):
        return None
    cur = float(unit["budget"])
    f = forecast(unit["daily"], as_of, cur, True)
    base = float(f["base"] or 0)
    use = base / cur if cur else 0
    tol = tg["tolerance_pct"] / 100
    want = max(MIN_BUDGET, need)
    if abs(want - cur) <= cur * tol:
        return None
    if want < cur and base and base <= want * (1 + tol):
        return None  # it already spends about what is needed: its budget is not what limits it
    why, warn = [], []
    goal = _r2(want)
    if platform == "meta":
        step = tg["meta_step_pct"] / 100
        lo, hi = cur * (1 - step), cur * (1 + step)
        new = min(max(want, lo), hi)
        if new != want:
            n = math.ceil(abs(math.log(want / cur)) / math.log(1 + step)) if want > 0 else 1
            why.append(f"Needs about {goal:.2f}/day to land on target; this step is capped at {tg['meta_step_pct']:g}% "
                       f"because big budget changes restart Meta's learning. About {n} steps of {tg['meta_step_pct']:g}%: "
                       "push this one, wait about 3 days, then check Pacing again.")
        if unit.get("learning"):
            warn.append(f"{unit['name']} is {unit['learning']}: a budget change now can restart learning.")
        why.append("Meta allows at most 4 budget changes per ad set per hour; Pacing puts one change per object in the draft.")
    else:
        new = want
        if new > cur * (1 + BIG_JUMP) or new < cur * (1 - BIG_JUMP):
            warn.append(f"Large change ({(new / cur - 1) * 100:+.0f}%)" + (": Smart Bidding may take a few days to settle."
                                                                         if camp.get("smart_bidding") else "."))
        why.append(f"Google may spend up to {GOOGLE_DAY_MAX:g}x the daily budget on one day and charges at most about "
                   f"{GOOGLE_MONTH_DAYS:g}x it in a calendar month, so single days can run over.")
    if new > cur and use < UNDER_USED and base:
        warn.append(f"Spends only {use * 100:.0f}% of its current budget: a higher budget may not raise spend "
                    "(bids, targeting or demand limit it).")
    new = _r2(max(MIN_BUDGET, new))
    pct = (new / cur - 1) * 100
    return {"level": unit["level"], "id": unit["id"], "name": unit["name"], "campaign": camp["name"], "campaign_id": camp["id"],
            "current": _r2(cur), "proposed": new, "goal": goal, "change_pct": round(pct, 1), "raise": new > cur,
            "capped": abs(new - goal) > 0.005 and new != MIN_BUDGET, "why": " ".join(why), "warnings": warn}


def account_view(a, snap, tg=None):
    """Pacing for one account from its snapshot. -> {account, currency, as_of, month, totals, flag, campaigns: [...]}"""
    tg = tg or targets()
    as_of = _d(snap["today"])
    first, last = month_bounds(as_of)
    acct_t = (tg["accounts"].get(a["id"]) or {})
    monthly = acct_t.get("monthly")
    own = {str(k): float(v) for k, v in (acct_t.get("campaigns") or {}).items()}
    tol = tg["tolerance_pct"]
    rows = []
    for c in snap["campaigns"]:
        f = forecast(c["daily"], as_of, c["budget"] if c["budget_kind"] in ("daily", "adsets") else None, c["active"])
        rows.append({"c": c, "f": f})
    # account target -> each campaign without its own target gets a share (adjustable campaigns only)
    adjustable = [r for r in rows if r["c"]["id"] not in own and r["c"]["active"] and r["c"]["budget_kind"] in ("daily", "adsets")]
    fixed = [r for r in rows if r["c"]["id"] not in own and r not in adjustable]
    alloc = {}
    if monthly:
        left = float(monthly) - sum(own.values()) - sum(r["f"]["total"] for r in fixed)
        mtd_adj = sum(r["f"]["mtd_done"] for r in adjustable)
        pool = left - mtd_adj
        wts = [max(float(r["f"]["rest"]) + float(r["f"]["today_expected"]) - float(r["f"]["today"]), 0.0) for r in adjustable]
        if not sum(wts):
            wts = [float(r["c"]["budget"] or 0) for r in adjustable]
        tw = sum(wts)
        for r, w in zip(adjustable, wts):
            alloc[r["c"]["id"]] = r["f"]["mtd_done"] + (pool * w / tw if tw else 0)
    out = []
    for r in rows:
        c, f = r["c"], r["f"]
        t = own.get(c["id"])
        t_src = "own" if t is not None else "share" if c["id"] in alloc else None
        t = t if t is not None else alloc.get(c["id"])
        st, amt, pct = flag(f["total"], t, tol) if t_src == "own" else ("no_target", None, None)
        if t_src == "own" and not f["enough"]:
            st, amt, pct = "no_data", None, None
        need = needed_daily(t, f["mtd_done"], f["days_left"]) if t is not None else None
        notes = []
        if c["budget_kind"] == "shared":
            notes.append("Shared budget: change it in Google Ads (Pacing leaves shared budgets alone).")
        elif c["budget_kind"] == "total":
            notes.append(f"Campaign total budget ({float(c.get('total_budget') or 0):.2f}): Google paces it to its end date.")
        elif c["budget_kind"] == "lifetime":
            notes.append("Lifetime budget: Meta paces it over the schedule.")
        if c.get("budget_limited"):
            notes.append("Google says it is limited by budget.")
        recs = []
        if need is not None and c["active"]:
            units = [u for u in c["units"] if u.get("active") and u["budget_kind"] == "daily" and u.get("budget")]
            if c["budget_kind"] == "adsets" and units:  # ABO: split the campaign's need over its ad sets
                shares = [max(float(forecast(u["daily"], as_of, u["budget"], True)["base"] or 0), 0.0) for u in units]
                if not sum(shares):
                    shares = [float(u["budget"]) for u in units]
                for u, s in zip(units, shares):
                    rec = _unit_rec(snap["platform"], u, c, need * s / sum(shares), tg, as_of)
                    if rec:
                        recs.append(rec)
            elif units:
                rec = _unit_rec(snap["platform"], units[0], c, need, tg, as_of)
                if rec:
                    recs.append(rec)
        out.append({"id": c["id"], "name": c["name"], "status": c["status"], "active": c["active"], "channel": c.get("channel", ""),
                    "budget": c["budget"], "budget_kind": c["budget_kind"], "mtd": f["mtd"], "today": f["today"],
                    "forecast": f["total"] if f["enough"] else None, "enough_data": f["enough"], "run_rate": f["base"], "target": _r2(t), "target_source": t_src,
                    "needed_daily": _r2(need), "flag": st, "flag_amount": amt, "flag_pct": pct, "notes": notes,
                    "learning": next((u["learning"] for u in c["units"] if u.get("learning") and u.get("active")), None),
                    "recommendations": recs,
                    "adsets": [{"id": u["id"], "name": u["name"], "budget": u["budget"], "active": u["active"], "learning": u.get("learning"),
                                "budget_kind": u["budget_kind"]} for u in c["units"]] if c["budget_kind"] == "adsets" else []})
    out = [x for x in out if x["active"] or x["mtd"] or x["target_source"] == "own"]
    mtd = sum(x["mtd"] for x in out)
    known = [x for x in out if x["enough_data"]]
    no_data = [x["name"] for x in out if not x["enough_data"]]
    total = sum(x["forecast"] for x in known)
    enough = bool(known) or not no_data  # no campaign has a run-rate: no account forecast either
    st, amt, pct = flag(total, monthly, tol) if enough else (("no_data" if monthly else "no_target"), None, None)
    days_in = (last - first).days + 1
    elapsed = (as_of - first).days + 1
    stale = _today(snap["tz"]) != as_of
    return {"account": a["id"], "key": a["key"], "name": a["name"], "platform": snap["platform"], "currency": snap["currency"],
            "tz": snap["tz"], "as_of": as_of.isoformat(), "month": f"{as_of:%B %Y}", "fetched_at": snap.get("fetched_at"),
            "stale": stale, "days_in_month": days_in, "days_left": (last - as_of).days + 1,
            "target": _r2(monthly), "mtd": _r2(mtd), "expected_to_date": _r2(float(monthly) * elapsed / days_in) if monthly else None,
            "forecast": _r2(total) if enough else None, "enough_data": enough, "no_data_campaigns": no_data,
            "needed_daily": _r2(needed_daily(monthly, mtd - sum(x["today"] for x in out), (last - as_of).days + 1)),
            "daily_budgets": _r2(sum(float(x["budget"] or 0) for x in out if x["active"] and x["budget_kind"] in ("daily", "adsets"))),
            "flag": st, "flag_amount": amt, "flag_pct": pct, "campaigns": out,
            "recommendations": sum(len(x["recommendations"]) for x in out)}


METHOD = ("Forecast = spend so far this month + today's expected spend + the rest of the month at a run-rate. Run-rate: "
          f"the last {RECENT_DAYS} completed days, adjusted for weekday (factors from the last {HISTORY_DAYS} days, used with "
          f"{MIN_DAYS_FOR_WEEKDAYS}+ days of history). Paused campaigns add nothing more; a daily budget caps each remaining day. "
          "A live campaign with no spend in that time has no run-rate yet: it shows \"not enough data\" and is left out of "
          "the account forecast. Needed per day = (target - spend before today) / days left including today. Days are in "
          "each account's time zone.")


def status(account=None, refresh=False, now=None):
    """Pacing for every account (or one). refresh: read each again now (read-only). -> {accounts, errors, drift, method, ...}"""
    accts = accounts()
    if account:
        accts = [find_account(account, accts)]
    tg = targets()
    res, errs = {}, []

    def one(a):
        return a, snapshot(a, force=refresh)

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        futs = [ex.submit(one, a) for a in accts]
        for fu in futs:
            try:
                a, snap = fu.result()
                res[a["id"]] = account_view(a, snap, tg)
            except Exception as e:  # noqa: BLE001 - one account failing must not hide the rest
                a = accts[futs.index(fu)]
                errs.append({"account": a["id"], "name": a["name"], "error": _scrub(e)})
    return {"accounts": [res[a["id"]] for a in accts if a["id"] in res], "errors": errs, "targets": tg, "target_info": targets_info(),
            "drift": last_drift(), "method": METHOD}


# ---------------------------------------------------------------- recommendations -> the draft

def _google_change(rec):
    return {"type": "campaign", "campaign": rec["campaign"], "budget": rec["proposed"]}


def _meta_change(rec):
    return {"type": "meta_budget", "level": rec["level"], "id": rec["id"], "budget": rec["proposed"]}


def _same(platform, it, rec):
    c = it.get("change") or {}
    if platform == "google":
        return c.get("type") == "campaign" and not c.get("new") and str(c.get("campaign", "")).strip().lower() == rec["campaign"].strip().lower() \
            and c.get("budget") is not None
    return c.get("type") == "meta_budget" and str(c.get("id")) == str(rec["id"])


def add_to_draft(account, ids=None, by="you", note=None, status_now=None):
    """Put this account's budget recommendations (all, or those whose campaign / ad set id is in ids) into its Ads
    draft. Never pushes. A pacing item already in the draft for the same object is replaced; any other budget change
    for it is left alone and that recommendation is skipped. -> {added, skipped, draft_account}"""
    a = find_account(account)
    v = status_now or status(a["id"])
    view = next((x for x in v["accounts"] if x["account"] == a["id"]), None)
    if not view:
        raise ValueError("; ".join(e["error"] for e in v["errors"]) or "no pacing data for this account")
    want = {str(i) for i in ids} if ids else None
    recs = [r for c in view["campaigns"] for r in c["recommendations"]
            if want is None or str(r["id"]) in want or str(r["campaign_id"]) in want]
    if not recs:
        return {"added": [], "skipped": [], "draft_account": a["key"], "message": "No budget changes to suggest."}
    if a["platform"] == "meta":
        import meta_ads  # noqa: PLC0415
        meta_ads.use(a["key"])
        dkey = meta_ads.acct()
    else:
        dkey = core.resolve(a["key"])
    items = core.draft(dkey)["items"]
    added, skipped = [], []
    for rec in recs:
        hits = [it for it in items if _same(a["platform"], it, rec)]
        others = [it for it in hits if not str(it.get("note", "")).startswith(PACING_NOTE)]
        if others:
            skipped.append({"name": rec["name"], "why": "the draft already has a budget change for it"})
            continue
        if hits:
            core.remove(dkey, [it["id"] for it in hits])
        text = f"{PACING_NOTE} {rec['current']:.2f} -> {rec['proposed']:.2f}/day ({rec['change_pct']:+.0f}%) to land on the " \
               f"{view['month']} target. {rec['why']}" + (f" {' '.join(rec['warnings'])}" if rec["warnings"] else "") + \
               (f" {note}" if note else "")
        try:
            if a["platform"] == "meta":
                got = meta_ads.add([_meta_change(rec)], by=by, note=text.strip())
            else:
                got = core.add(dkey, [_google_change(rec)], by=by, note=text.strip())
            added += [{"id": it["id"], "summary": it.get("summary"), "problems": it.get("problems"), "warnings": it.get("warnings")} for it in got]
        except ValueError as ex:  # e.g. an experiment's trial campaign
            skipped.append({"name": rec["name"], "why": str(ex)[:300]})
    return {"added": added, "skipped": skipped, "draft_account": a["key"]}


# ---------------------------------------------------------------- the daily check (scheduler)

def _drift_file():
    return core.STUDIO_DIR / DRIFT_FILE


def last_drift():
    try:
        return json.loads(_drift_file().read_text())
    except (OSError, ValueError):
        return None


def drift_check(refresh=True):
    """Refresh every account (read-only) and record the ones drifting from their monthly target. -> the record."""
    v = status(refresh=refresh)
    items = [{"account": x["account"], "name": x["name"], "flag": x["flag"], "amount": x["flag_amount"], "pct": x["flag_pct"],
              "currency": x["currency"], "target": x["target"], "forecast": x["forecast"], "recommendations": x["recommendations"]}
             for x in v["accounts"] if x["flag"] in ("over", "under")]
    rec = {"at": dt.datetime.now().isoformat(timespec="seconds"), "drifting": items, "errors": v["errors"],
           "checked": len(v["accounts"])}
    f = _drift_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=1))
    tmp.replace(f)
    return rec


JOB = "pacing"


def register_job():
    """server.py main(): the daily pacing check, off until switched on in Settings."""
    import scheduler  # noqa: PLC0415
    return scheduler.register(JOB, lambda: drift_check(True), at="07:00", enabled_default=False, refresh=True, timeout=600,
                              label="Budget pacing check",
                              note="Daily: reads month-to-date spend for every ad account (read-only) and flags accounts "
                                   "drifting from their monthly target. Never changes a budget.")
