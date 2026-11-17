"""Meta A/B tests and creative tests (Meta calls them ad studies). Read them, draft new ones, end them.

What Meta offers (Marketing API, checked 17 November 2026):
- Tests live on the business that owns the ad account: GET/POST /{business_id}/ad_studies.
- A/B test (type SPLIT_TEST): 2-5 cells, each with ad sets or campaigns and a share of the audience
  (treatment_percentage, at least 10, all cells summing to 100).
- Creative test (type SPLIT_TEST_V2 with creative_test_config): 2-7 cells, exactly one ad per cell, a test budget
  (daily_budget in the account currency's minor unit, or lifetime_budget_percentage); cooldown_start_time must equal
  start_time and observation_end_time must equal end_time.
- Results: GET /{study}/objectives, then the objective's "results". Creative tests name high_performer_ids with
  confidence_by_ad_object_id. A/B tests have no winner field there, so Studio compares cost per result per cell from
  insights over the test dates, and says it is Studio's comparison, not Meta's significance test.
- A test can't be deleted, and once it runs its cells can't be changed. It can be ended early by moving end_time to a
  time just ahead (only to a future time, and only while it has not ended). Ending is final.
- /ad_studies does not document execution_options=validate_only, so Studio never sends a test (or an end) to Meta
  before Push live: the preview checks what it can with reads (every object exists in this ad account and is not in
  another running test) and Meta's own validate_only on any variant ad set / ad the draft creates.

Draft change types (pushed by meta_ads like every other Meta change; team approval applies the same way):
  meta_test_split     A/B test from existing (or new, "d-<ref>") ad sets or campaigns
  meta_test_creative  creative test from 2-7 existing (or new) ads
  meta_test_end       end a running test now (permanent)
"Duplicate this ad set with one change" (variant_changes) is three ordinary draft changes: a meta_new_adset copied
from the ad set (from_adset) with the one change, a meta_new_ad copy of each of its ads, and a meta_test_split.
"""
import datetime as dt
import json
import re
import threading
import time

import appconfig
import core
import datapolicy
import meta_ads as meta

TYPES = ("meta_test_split", "meta_test_creative", "meta_test_end")
SPLIT_MAX, CREATIVE_MIN, CREATIVE_MAX = 5, 2, 7
MIN_PERCENT = 10
END_LEAD = 300  # seconds: "end now" moves the end to 5 minutes ahead (Meta only takes a future end time)
START_LEAD = 900  # seconds: a test starting "today" starts 15 minutes after the push
LONG_DAYS, SHORT_DAYS = 30, 7
THIN_RESULTS = 20  # results per cell before Studio calls a split test
CLOSE_PCT = 10     # cost per result within this % = too close to call
FINAL = ("A test can't be removed from Meta: undo leaves it in place (end it with End test). "
         "Variant ad sets and ads Studio created are deleted by undo as usual.")
F_STUDY = ("id,name,type,description,start_time,end_time,canceled_time,created_time,updated_time,cooldown_start_time,"
           "observation_end_time,cells{id,name,treatment_percentage,campaigns{id,name},adsets{id,name}},"
           "objectives{id,name,type,is_primary}")
TYPE_LABEL = {"SPLIT_TEST": "A/B test", "SPLIT_TEST_V2": "A/B test", "creative": "Creative test"}
_biz = {}
_mem = {}
_lock = threading.Lock()


# ---------- where the tests live ----------

def business_id():
    """The business that owns the current ad account (META_BUSINESS_ID in the environment / config overrides it)."""
    v = appconfig.get("META_BUSINESS_ID")
    if v:
        return str(v)
    a = meta.act()
    if a not in _biz:
        b = (meta.get(a, fields="business") or {}).get("business") or {}
        if not b.get("id"):
            raise ValueError("this ad account has no business in Meta, and tests live on a business")
        _biz[a] = str(b["id"])
    return _biz[a]


def _local_file():
    core.STUDIO_DIR.mkdir(parents=True, exist_ok=True)
    return core.STUDIO_DIR / f"meta_tests_{meta.acct()}.json"


def _local():
    """Tests Studio created: {study id: {kind, cells: [{name, ids, level}]}}. Meta does not return a cell's ads."""
    try:
        return json.loads(_local_file().read_text())
    except (OSError, ValueError):
        return {}


def _remember(study_id, rec):
    d = _local()
    d[str(study_id)] = rec
    f = _local_file()
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1, ensure_ascii=False))
    tmp.replace(f)


# ---------- reading ----------

def _ts(v):
    try:
        return dt.datetime.strptime(v, "%Y-%m-%dT%H:%M:%S%z").timestamp() if v else None
    except (TypeError, ValueError):
        return None


def status_of(s, now=None):
    now = time.time() if now is None else now
    if s.get("canceled_time"):
        return "Cancelled"
    st, en = _ts(s.get("start_time")), _ts(s.get("end_time"))
    if st and now < st:
        return "Scheduled"
    if en and now >= en:
        return "Ended"
    return "Running"


def _cell_row(c, i, local_cell=None):
    camps = (c.get("campaigns") or {}).get("data") or []
    sets = (c.get("adsets") or {}).get("data") or []
    lc = local_cell or {}
    if lc.get("level") == "ad" and lc.get("ids"):  # creative test cells: Meta does not return their ads
        level, objs = "ad", [{"id": x} for x in lc["ids"]]
    elif sets:
        level, objs = "adset", sets
    elif camps:
        level, objs = "campaign", camps
    elif lc.get("ids"):
        level, objs = lc.get("level", "ad"), [{"id": x} for x in lc["ids"]]
    else:
        level, objs = "", []
    names = [o.get("name") or _cached_name(level, o["id"]) for o in objs]
    return {"id": c.get("id"), "name": c.get("name") or lc.get("name") or (names[0] if len(names) == 1 else f"Cell {i + 1}"),
            "percent": c.get("treatment_percentage"), "level": level, "ids": [str(o["id"]) for o in objs], "names": names}


def _cached_name(level, oid):
    try:
        return meta._name(meta.load(), level, oid) if level in meta.LEVELS else str(oid)
    except Exception:  # noqa: BLE001 - a name is a nicety
        return str(oid)


def _row(s, local=None):
    lc = (local or {}).get(str(s["id"])) or {}
    kind = lc.get("kind") or ("split" if s.get("type") == "SPLIT_TEST" else "")
    lcells = lc.get("cells") or []
    cells = [_cell_row(c, i, lcells[i] if i < len(lcells) else None) for i, c in enumerate((s.get("cells") or {}).get("data") or [])]
    if not kind and cells and all(c["level"] == "ad" for c in cells):
        kind = "creative"
    objs = (s.get("objectives") or {}).get("data") or []
    return {"id": s["id"], "name": s.get("name", ""), "type": s.get("type", ""),
            "label": TYPE_LABEL["creative"] if kind == "creative" else TYPE_LABEL.get(s.get("type"), s.get("type", "")),
            "kind": kind or "split", "description": s.get("description") or "", "status": status_of(s),
            "start": (s.get("start_time") or "")[:16].replace("T", " "), "end": (s.get("end_time") or "")[:16].replace("T", " "),
            "start_time": s.get("start_time"), "end_time": s.get("end_time"), "created": (s.get("created_time") or "")[:10],
            "cells": cells, "objectives": [{"id": o["id"], "name": o.get("name", ""), "type": o.get("type", ""),
                                            "primary": bool(o.get("is_primary"))} for o in objs],
            "by_studio": bool(lc)}


def _cache_path(name):
    return core.STUDIO_DIR / f"meta_tests_{name}_{meta.acct()}.json"


def _cached(name, max_age, fetch, refresh=False):
    key = (meta.acct(), name)
    now = time.time()
    if not refresh:
        hit = _mem.get(key)
        if hit and datapolicy.fresh(meta._src(), now - hit[0], max_age, hit[0]):
            return hit[1]
        try:
            disk = json.loads(_cache_path(name).read_text())
            if datapolicy.fresh(meta._src(), now - disk["at"], max_age, disk["at"]):
                _mem[key] = (disk["at"], disk["data"])
                return disk["data"]
        except (OSError, ValueError, KeyError):
            pass
    data = fetch()
    _mem[key] = (now, data)
    core.STUDIO_DIR.mkdir(parents=True, exist_ok=True)
    f = _cache_path(name)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps({"at": now, "data": data}, ensure_ascii=False))
    tmp.replace(f)
    return data


def _raw_studies():
    return meta.get_all(f"{business_id()}/ad_studies", fields=F_STUDY, limit=100)


def studies(refresh=False):
    """Every test on the business (newest first): type, dates, status, cells. Read-only, cached like insights."""
    raw = _cached("list", meta.CACHE_MAX_AGE, _raw_studies, refresh)
    local = _local()
    rows = [_row(s, local) for s in raw]
    return sorted(rows, key=lambda r: r.get("start_time") or "", reverse=True)


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _pct(v):
    """Confidence as Meta gives it (0-1 or 0-100, number or text) -> percent, or None."""
    x = _num(v)
    if x is None:
        return None
    return round(x * 100 if x <= 1 else x, 1)


def parse_results(raw):
    """Meta's objective "results" (a list of objects or JSON strings, depending on version) -> one dict of the
    fields Studio uses: high_performer_ids, confidence_by_ad_object_id, winner_ad_object_id, winner_confidence."""
    out = {"high_performer_ids": [], "confidence": {}, "winner": None, "winner_confidence": None, "available": False}
    items = raw if isinstance(raw, list) else [raw] if raw else []
    for it in items:
        if isinstance(it, str):
            try:
                it = json.loads(it)
            except ValueError:
                continue
        if not isinstance(it, dict):
            continue
        out["available"] = True
        hp = it.get("high_performer_ids")
        if isinstance(hp, str):
            hp = [x for x in re.split(r"[,\s]+", hp) if x]
        out["high_performer_ids"] += [str(x) for x in hp or [] if str(x) not in out["high_performer_ids"]]
        conf = it.get("confidence_by_ad_object_id")
        if isinstance(conf, str):
            try:
                conf = json.loads(conf)
            except ValueError:
                conf = None
        if isinstance(conf, dict):
            out["confidence"].update({str(k): _pct(v) for k, v in conf.items()})
        elif isinstance(conf, list):
            for e in conf:
                if isinstance(e, dict):
                    k = e.get("ad_object_id") or e.get("id") or e.get("key")
                    if k is not None:
                        out["confidence"][str(k)] = _pct(e.get("confidence", e.get("value")))
        if it.get("winner_ad_object_id"):
            out["winner"] = str(it["winner_ad_object_id"])
            out["winner_confidence"] = _pct(it.get("winner_confidence"))
    return out


def _sum_cell(rows):
    """Insights rows of one cell's objects -> spend, impressions, clicks, ctr, results, cost per result."""
    spend = imp = clicks = res = 0.0
    any_res = False
    for r in rows:
        v = meta._ins_vals(r)
        spend += v["spend"] or 0
        imp += v["impressions"] or 0
        clicks += v["clicks"] or 0
        if v["results"]:
            res += v["results"]
            any_res = True
    return {"spend": round(spend, 2), "impressions": int(imp), "clicks": int(clicks),
            "ctr": round(clicks / imp * 100, 2) if imp else None, "results": int(res) if any_res else 0,
            "cost_per_result": round(spend / res, 2) if res else None}


def _cell_insights(cell, since, until):
    if not cell["ids"] or not since:
        return []
    rows = []
    for oid in cell["ids"]:
        got = meta.get(f"{oid}/insights", fields=meta.F_INS, time_range={"since": since, "until": until}).get("data") or []
        rows += got
    return rows


def _fetch_results(study_id):
    s = meta.get(str(study_id), fields=F_STUDY)
    row = _row(s, _local())
    objs = meta.get_all(f"{study_id}/objectives", fields="id,name,type,is_primary,results")
    prim = next((o for o in objs if o.get("is_primary")), objs[0] if objs else {})
    res = parse_results(prim.get("results"))
    now = dt.datetime.now(dt.timezone.utc)
    st = dt.datetime.fromtimestamp(_ts(row["start_time"]), dt.timezone.utc) if _ts(row["start_time"]) else None
    en = dt.datetime.fromtimestamp(_ts(row["end_time"]), dt.timezone.utc) if _ts(row["end_time"]) else None
    since = st.date().isoformat() if st and st <= now else None
    until = min(en or now, now).date().isoformat() if since else None
    for c in row["cells"]:
        c.update(_sum_cell(_cell_insights(c, since, until)))
    return {"study": row, "objective": {"name": prim.get("name", ""), "type": prim.get("type", "")}, "meta": res,
            "period": {"since": since, "until": until}}


def results(study_id, refresh=False):
    """Per-cell spend, results, cost per result and CTR over the test dates, Meta's own outcome where it gives one
    (creative tests: high performers with confidence), a plain verdict and honest notes. Read-only, cached."""
    if not re.fullmatch(r"\d+", str(study_id or "")):
        raise ValueError("study_id is a Meta test id (digits)")
    data = _cached(f"r{study_id}", meta.CACHE_MAX_AGE, lambda: _fetch_results(study_id), refresh)
    return {**data, **verdict(data)}


def _money(v):
    return "none" if v is None else f"{v:,.2f}"


def verdict(data):
    """-> {"verdict": one plain sentence, "notes": [...], "thin": bool, "leader": cell id or ad id or None}."""
    s, res, cells = data["study"], data["meta"], data["study"]["cells"]
    notes, thin, leader = [], False, None
    names = {i: n for c in cells for i, n in zip(c["ids"], c["names"])}
    if s["status"] == "Scheduled":
        return {"verdict": f"Not started yet: the test starts {s['start']} (UTC).", "notes": [], "thin": True, "leader": None}
    if s["status"] == "Cancelled":
        notes.append("This test was cancelled, so its numbers stop at the cancellation.")
    spend = sum(c.get("spend") or 0 for c in cells)
    if s["kind"] == "creative":
        hp = res["high_performer_ids"]
        if hp:
            bits = [f'{names.get(a, "ad " + a)}' + (f' (confidence {res["confidence"][a]:g}%)' if res["confidence"].get(a) is not None else "")
                    for a in hp]
            text = "Meta's high performer" + ("s: " if len(hp) > 1 else ": ") + ", ".join(bits) + "."
            leader = hp[0]
        elif not res["available"]:
            text = ("Meta has no results for this test yet." if s["status"] == "Running"
                    else "Meta gave no results for this test.")
            thin = True
        elif s["status"] == "Ended":
            text = "Meta named no high performer: no ad clearly beat the others."
        else:
            text = "Meta has not named a high performer yet."
            thin = True
        if res["confidence"] and not hp:
            notes.append("Confidence per ad so far: " + ", ".join(f'{names.get(k, k)} {v:g}%' for k, v in res["confidence"].items()
                                                                  if v is not None) + ".")
        notes.append("For creative tests Meta's high performers are the outcome; the numbers per cell are for context.")
    else:
        if res["winner"]:
            text = f'Meta\'s pick: {names.get(res["winner"], res["winner"])}' + \
                (f' ({res["winner_confidence"]:g}% confidence).' if res["winner_confidence"] is not None else ".")
            leader = res["winner"]
        elif not spend:
            text, thin = "No delivery yet: nothing to compare.", True
        else:
            priced = sorted([c for c in cells if c.get("cost_per_result")], key=lambda c: c["cost_per_result"])
            fewest = min((c.get("results") or 0) for c in cells) if cells else 0
            if len(priced) < 2:
                text, thin = "Too early to call: not every cell has results yet.", True
            else:
                best, nxt = priced[0], priced[1]
                diff = (nxt["cost_per_result"] - best["cost_per_result"]) / nxt["cost_per_result"] * 100
                leader = best["id"]
                if fewest < THIN_RESULTS:
                    thin = True
                    ended = s["status"] != "Running"
                    text = (f'{"Not enough data to call" if ended else "Too early to call"}: "{best["name"]}" led on cost per result '
                            f'({_money(best["cost_per_result"])} vs {_money(nxt["cost_per_result"])}), but the smallest cell has only '
                            f'{fewest} result(s). ' + ("Treat it as a hint, not a result." if ended
                                                       else f"Wait for at least {THIN_RESULTS} per cell."))
                elif diff < CLOSE_PCT:
                    text = (f"Too close to call: cost per result is within {CLOSE_PCT}% "
                            f'("{best["name"]}" {_money(best["cost_per_result"])} vs "{nxt["name"]}" {_money(nxt["cost_per_result"])}).')
                else:
                    text = (f'"{best["name"]}" gets results for less: {_money(best["cost_per_result"])} vs '
                            f'{_money(nxt["cost_per_result"])} per result ({diff:.0f}% cheaper than "{nxt["name"]}").')
        notes.append("Meta gives no winner for A/B tests through the API: this compares cost per result per cell. "
                     "It is Studio's comparison, not Meta's significance test.")
    if data["objective"].get("name"):
        notes.append(f'Meta measures this test on "{data["objective"]["name"].replace("_", " ")}". Studio counts results as '
                     "purchases, custom pixel conversions and leads, which may not match that exactly.")
    if spend and sum(c.get("impressions") or 0 for c in cells) < 1000:
        thin = True
        notes.append("Fewer than 1,000 impressions in total: treat any difference as noise for now.")
    if any(not c["ids"] for c in cells):
        notes.append("Meta does not say which ads are in some cells, so Studio has no numbers for them.")
    if data["period"]["since"]:
        notes.append(f'Numbers cover {data["period"]["since"]} to {data["period"]["until"]} (the test dates so far).')
    return {"verdict": text, "notes": notes, "thin": thin, "leader": leader}


# ---------- draft: checks, summaries, steps ----------

def _date(v, what, problems):
    try:
        return dt.date.fromisoformat(str(v))
    except ValueError:
        problems.append(f"{what} must be YYYY-MM-DD")
        return None


def _dates(c, problems, warnings):
    today = dt.date.today()
    start = _date(c["start_date"], "start date", problems) if c.get("start_date") else today
    if not c.get("end_date"):
        problems.append("the test needs an end date (YYYY-MM-DD)")
        return
    end = _date(c["end_date"], "end date", problems)
    if not start or not end:
        return
    if start < today:
        problems.append("start date can't be in the past")
    if end <= start:
        problems.append("end date must be after the start date")
        return
    days = (end - start).days + 1
    if days > LONG_DAYS:
        warnings.append(f"{days} days: Meta's tests run for up to {LONG_DAYS} days, a longer one may be refused")
    elif days < SHORT_DAYS:
        warnings.append(f"{days} days: Meta suggests at least {SHORT_DAYS} days for a reliable result")


def split_percent(n):
    """n cells -> integer shares summing to 100 (the first cells get the remainder)."""
    base, extra = divmod(100, n)
    return [base + (1 if i < extra else 0) for i in range(n)]


def _obj_ok(level, oid, d, items, problems):
    """An existing object at this level, or a new one in the draft ("d-<id>")."""
    r = meta._ref(oid)
    want = {"campaign": "meta_new_campaign", "adset": "meta_new_adset", "ad": ("meta_new_ad", "meta_boost_post")}[level]
    if r:
        it = items.get(r)
        if not it or it["change"].get("type") not in ((want,) if isinstance(want, str) else want):
            problems.append(f"{oid} is not a new {level.replace('adset', 'ad set')} in this draft")
            return None
        return {"name": it["change"].get("name", oid), "new": True}
    try:
        o = meta._find(d, level, oid)
    except ValueError as ex:
        problems.append(str(ex))
        return None
    if o.get("status") in ("DELETED", "ARCHIVED"):
        problems.append(f'{level} "{o["name"]}" is {o["status"].lower()}')
    return o


def check(c, d, items):
    t = c["type"]
    problems, warnings = [], []
    if t == "meta_test_end":
        if not re.fullmatch(r"\d+", str(c.get("study_id") or "")):
            problems.append("study_id must be a Meta test id")
        warnings.append("ending a test is final: it can't be undone or restarted")
        return problems, warnings
    if not str(c.get("name") or "").strip():
        problems.append("the test needs a name")
    _dates(c, problems, warnings)
    if t == "meta_test_split":
        lvl = c.get("level")
        if lvl not in ("adset", "campaign"):
            return problems + ["level must be adset or campaign"], warnings
        cells = c.get("cells") or []
        if not isinstance(cells, list) or not 2 <= len(cells) <= SPLIT_MAX:
            return problems + [f"an A/B test needs 2-{SPLIT_MAX} cells"], warnings
        seen, budgets = set(), []
        for i, cell in enumerate(cells):
            ids = cell.get("ids") or []
            if not ids:
                problems.append(f"cell {i + 1} has no {lvl.replace('adset', 'ad set')}s")
            for oid in ids:
                if oid in seen:
                    problems.append(f"{oid} is in more than one cell")
                seen.add(oid)
                o = _obj_ok(lvl, oid, d, items, problems)
                if o and not o.get("new"):
                    b = o.get("daily_budget") or o.get("lifetime_budget")
                    if b:
                        budgets.append(int(b))
        pcts = [cell.get("percent") for cell in cells]
        if any(not isinstance(p, int) or p < MIN_PERCENT for p in pcts):
            problems.append(f"each cell needs a whole-number share of at least {MIN_PERCENT}%")
        elif sum(pcts) != 100:
            problems.append(f"the cell shares must add up to 100 (now {sum(pcts)})")
        if len(budgets) > 1 and max(budgets) > min(budgets) * 1.2:
            warnings.append("the cells have different budgets: Meta advises similar budgets, or reach differs for that reason alone")
    elif t == "meta_test_creative":
        ads = c.get("ads") or []
        if not isinstance(ads, list) or not CREATIVE_MIN <= len(ads) <= CREATIVE_MAX:
            return problems + [f"a creative test needs {CREATIVE_MIN}-{CREATIVE_MAX} ads (one per cell)"], warnings
        if len(set(ads)) != len(ads):
            problems.append("the same ad is in the test twice")
        sets = set()
        for a in ads:
            o = _obj_ok("ad", a, d, items, problems)
            if o and not o.get("new"):
                sets.add(o.get("adset_id"))
            elif o:
                sets.add(items[meta._ref(a)]["change"].get("adset_id"))
        if len(sets) > 1:
            warnings.append("the ads are in different ad sets: Meta's creative tests compare ads in one ad set and may refuse this")
        has_b, has_p = c.get("daily_budget") is not None, c.get("budget_percent") is not None
        if has_b == has_p:
            problems.append("give either daily_budget (test budget per day) or budget_percent (share of the ad set budget)")
        elif has_b and float(c["daily_budget"]) < 1:
            problems.append("the test budget must be at least 1.00 a day")
        elif has_p and not 1 <= float(c["budget_percent"]) <= 100:
            problems.append("budget_percent must be 1-100")
    return problems, warnings


def summarise(c, d, items):
    t = c["type"]
    if t == "meta_test_end":
        return f'Meta end test "{c.get("name") or c.get("study_id")}" now (final)'
    when = f'{c.get("start_date") or "today"} to {c.get("end_date")}'
    if t == "meta_test_creative":
        names = [_label("ad", a, d, items) for a in c.get("ads") or []]
        b = f'test budget {float(c["daily_budget"]):.2f}/day' if c.get("daily_budget") is not None else f'{c.get("budget_percent")}% of the budget'
        return f'Meta new creative test "{c.get("name")}": {len(names)} ads ({", ".join(names)}), {b}, {when}'
    lvl = c.get("level", "adset")
    cells = [f'{x.get("name") or "cell " + str(i + 1)} {x.get("percent")}% ({", ".join(_label(lvl, o, d, items) for o in x.get("ids") or [])})'
             for i, x in enumerate(c.get("cells") or [])]
    return f'Meta new A/B test "{c.get("name")}" of {lvl.replace("adset", "ad set")}s: {"; ".join(cells)}; {when}'


def _label(level, oid, d, items):
    r = meta._ref(oid)
    if r:
        return f'new "{(items.get(r) or {}).get("change", {}).get("name", oid)}"'
    return f'"{meta._name(d, level, oid)}"'


def normalise(c):
    t = c.get("type")
    for k in ("study_id",):
        if c.get(k) is not None:
            c[k] = str(c[k])
    if t == "meta_test_split":
        c["level"] = {"adsets": "adset", "ad_set": "adset", "campaigns": "campaign"}.get(c.get("level"), c.get("level") or "adset")
        cells = []
        for i, x in enumerate(c.get("cells") or []):
            x = dict(x) if isinstance(x, dict) else {"ids": [x]}
            ids = x.get("ids") if x.get("ids") is not None else [x["id"]] if x.get("id") is not None else []
            x["ids"] = [str(v) for v in (ids if isinstance(ids, list) else [ids])]
            x.pop("id", None)
            x["name"] = str(x.get("name") or f"Cell {chr(65 + i)}")
            cells.append(x)
        if cells and all(x.get("percent") is None for x in cells):
            for x, p in zip(cells, split_percent(len(cells))):
                x["percent"] = p
        c["cells"] = cells
    if t == "meta_test_creative":
        c["ads"] = [str(a) for a in c.get("ads") or []]
    return c


def refs(c):
    """Draft item ids this change points at (new objects that must be in the same push)."""
    ids = [o for x in c.get("cells") or [] for o in x.get("ids") or []] + list(c.get("ads") or [])
    return [meta._ref(o) for o in ids if meta._ref(o)]


def _epoch(day, end=False):
    t = dt.datetime.combine(day, dt.time(23, 59) if end else dt.time(0, 0), dt.timezone.utc)
    return int(t.timestamp())


def _times(c, now=None):
    """-> (start, end) as Unix seconds. A start of today (or none) means 15 minutes after the push."""
    now = time.time() if now is None else now
    start_day = dt.date.fromisoformat(c["start_date"]) if c.get("start_date") else dt.date.today()
    start = _epoch(start_day) if start_day > dt.date.today() else int(now) + START_LEAD
    return start, _epoch(dt.date.fromisoformat(c["end_date"]), end=True)


def ops_for(c, items):
    t = c["type"]
    if t == "meta_test_end":
        s = meta.get(str(c["study_id"]), fields="id,name,type,start_time,end_time,canceled_time,observation_end_time")
        st = status_of(s)
        if st == "Cancelled":
            raise ValueError(f'test "{s.get("name")}" was cancelled already')
        if st == "Ended":
            raise ValueError(f'test "{s.get("name")}" has ended already')
        if st == "Scheduled":
            raise ValueError(f'test "{s.get("name")}" has not started: Meta only lets Studio move a running test\'s end. '
                             "Pause its ad sets or campaigns instead if it should not run")
        return [{"kind": "study_end", "phase": "study_end", "study": s["id"], "study_type": s.get("type"),
                 "has_observation": bool(s.get("observation_end_time")) or s.get("type") == "SPLIT_TEST_V2",
                 "permanent": True,
                 "line": f'end test "{s.get("name")}" now (planned end {(s.get("end_time") or "")[:10]}); final, can\'t be undone. '
                         "Its ad sets and ads keep running as normal unless you pause them"}]
    if t == "meta_test_split":
        cells = [{"name": x["name"], "treatment_percentage": int(x["percent"]),
                  f'{c["level"]}s': [o for o in x["ids"]]} for x in c["cells"]]
        params = {"name": c["name"], "type": "SPLIT_TEST", "cells": cells}
        new = sum(1 for x in c["cells"] for o in x["ids"] if meta._ref(o))
        line = (f'new A/B test "{c["name"]}" ({len(cells)} cells: ' +
                ", ".join(f'{x["name"]} {x["percent"]}%' for x in c["cells"]) +
                f'; {c.get("start_date") or "today"} to {c["end_date"]})')
    else:
        n = len(c["ads"])
        cells = [{"name": f"Ad {i + 1}", "treatment_percentage": p, "ads": [a]} for i, (a, p) in enumerate(zip(c["ads"], split_percent(n)))]
        cfg = ({"daily_budget": int(round(float(c["daily_budget"]) * 100))} if c.get("daily_budget") is not None
               else {"lifetime_budget_percentage": float(c["budget_percent"])})
        params = {"name": c["name"], "type": "SPLIT_TEST_V2", "cells": cells, "creative_test_config": cfg}
        new = sum(1 for a in c["ads"] if meta._ref(a))
        b = f'test budget {float(c["daily_budget"]):.2f}/day' if c.get("daily_budget") is not None else f'{c["budget_percent"]}% of the budget'
        line = f'new creative test "{c["name"]}" ({n} ads, one per cell; {b}; {c.get("start_date") or "today"} to {c["end_date"]})'
    if c.get("description"):
        params["description"] = str(c["description"])[:500]
    line += ("; Meta can't test-run a new test before it exists (its /ad_studies has no validate_only), so Studio checked "
             "that every object is in this ad account and in no other running test" +
             (f", and Meta checks the {new} new object(s) the test uses" if new else "") +
             ". A test can't be removed once made, only ended")
    return [{"kind": "study", "phase": "study", "params": params, "test": {k: c.get(k) for k in ("start_date", "end_date")},
             "study_kind": "creative" if t == "meta_test_creative" else "split", "permanent": True, "line": line,
             "spend": f'test budget {float(c["daily_budget"]):.2f}/day' if c.get("daily_budget") is not None else ""}]


def _cell_ids(params):
    return [str(o) for cell in params["cells"] for k in ("adsets", "campaigns", "ads") for o in cell.get(k) or []]


def check_step(s):
    """The preview's check of a test step. Reads only: Meta's /ad_studies has no documented validate_only, so a test or an
    end is never sent before Push live. -> error text or ''."""
    if s["kind"] == "study_end":
        return ""  # ops_for read the test live and refused anything but a running one
    ids = [o for o in _cell_ids(s["params"]) if not meta._ref(o)]
    a = meta.act().replace("act_", "")
    for oid in ids:
        o = meta.get(oid, fields="id,name,account_id,effective_status")
        if str(o.get("account_id") or "") != a:
            return f'Meta: {o.get("name", oid)} is not in this ad account'
        if o.get("effective_status") in ("DELETED", "ARCHIVED"):
            return f'Meta: {o.get("name", oid)} is {o["effective_status"].lower()}'
    busy = []
    for st in _raw_studies():
        if status_of(st) not in ("Running", "Scheduled"):
            continue
        cell_ids = {str(x["id"]) for cell in (st.get("cells") or {}).get("data") or []
                    for k in ("campaigns", "adsets") for x in (cell.get(k) or {}).get("data") or []}
        local = _local().get(str(st["id"])) or {}
        cell_ids |= {str(x) for cell in local.get("cells") or [] for x in cell.get("ids") or []}
        hit = cell_ids & set(ids)
        if hit:
            busy.append(f'{", ".join(sorted(hit))} (in test "{st.get("name")}")')
    if busy:
        return "Meta: already in a running or scheduled test: " + "; ".join(busy)
    return ""


def do(s, ctx):
    """Push time: make the test, or end it. Returns the push-log record (undo None: neither can be reversed)."""
    if s["kind"] == "study_end":
        t = int(time.time()) + END_LEAD
        p = {"end_time": t}
        if s.get("has_observation"):
            p["observation_end_time"] = t
        meta.post(s["study"], p)
        return {"line": s["line"], "id": s["study"], "undo": None, "final": "an ended test can't be restarted"}
    params = json.loads(json.dumps(s["params"]))
    for cell in params["cells"]:
        for k in ("adsets", "campaigns", "ads"):
            if k in cell:
                cell[k] = [ctx[meta._ref(o)] if meta._ref(o) else o for o in cell[k]]
    start, end = _times(s["test"])
    params.update(start_time=start, end_time=end)
    if params["type"] == "SPLIT_TEST_V2":
        params.update(cooldown_start_time=start, observation_end_time=end)
    sid = meta.post(f"{business_id()}/ad_studies", params)["id"]
    level = "ad" if s["study_kind"] == "creative" else ("adset" if "adsets" in params["cells"][0] else "campaign")
    _remember(sid, {"kind": s["study_kind"], "name": params["name"],
                    "cells": [{"name": c["name"], "level": level, "ids": c.get("ads") or c.get("adsets") or c.get("campaigns")}
                              for c in params["cells"]]})
    _mem.pop((meta.acct(), "list"), None)
    return {"line": s["line"], "id": sid, "undo": None, "created": "study", "final": FINAL}


# ---------- building drafts (UI dialogs and MCP) ----------

CHANGES = ("audience", "placement", "creative", "optimisation")


def variant_changes(adset_id, change, test_name=None, variant_name=None, start_date=None, end_date=None, percent=50):
    """"Duplicate this ad set with one change" -> draft changes: the variant ad set (Paused, copied from the ad set),
    a Paused copy of each of its ads, and an A/B test of the two. change: {"what": audience|placement|creative|
    optimisation, ...}: audience: countries, age_min, age_max; placement: the meta_placements fields; creative:
    primary_text, headline, description, link, cta, media_id (applied to every copied ad); optimisation:
    optimization_goal, conversion_event."""
    d = meta.load()
    src = meta._find(d, "adset", adset_id)
    what = (change or {}).get("what")
    if what not in CHANGES:
        raise ValueError(f"change.what must be one of {', '.join(CHANGES)}")
    ch = {k: v for k, v in change.items() if k != "what"}
    if not ch:
        raise ValueError("say what the one change is")
    vname = variant_name or f'{src["name"]} - variant ({what})'
    new_set = {"type": "meta_new_adset", "ref": "v1", "from_adset": src["id"], "campaign_id": src["campaign_id"], "name": vname}
    if what == "audience":
        bad = set(ch) - {"countries", "age_min", "age_max"}
        if bad:
            raise ValueError(f"an audience change takes countries, age_min, age_max (not {', '.join(sorted(bad))})")
        new_set.update(ch)
    elif what == "placement":
        new_set["placements"] = ch
    elif what == "optimisation":
        if not ch.get("optimization_goal"):
            raise ValueError("an optimisation change needs optimization_goal")
        new_set.update({k: ch[k] for k in ("optimization_goal", "conversion_event") if ch.get(k)})
    ads = [a for a in d["ads"] if a.get("adset_id") == src["id"] and a.get("status") not in ("DELETED", "ARCHIVED")]
    if not ads:
        raise ValueError(f'ad set "{src["name"]}" has no ads to copy into the variant')
    out = [new_set]
    for a in ads:
        ad = {"type": "meta_new_ad", "adset_id": "d-v1", "name": a["name"], "from_ad": a["id"]}
        if what == "creative":
            ad.update({k: ch[k] for k in ("primary_text", "headline", "description", "link", "cta", "media_id") if ch.get(k) is not None})
        out.append(ad)
    p = int(percent)
    out.append({"type": "meta_test_split", "name": test_name or f'{src["name"]}: {what} test', "level": "adset",
                "start_date": start_date, "end_date": end_date,
                "cells": [{"name": "Original", "ids": [src["id"]], "percent": 100 - p}, {"name": "Variant", "ids": ["d-v1"], "percent": p}]})
    return out


def end_changes(study_id, pause=False):
    """End a test now (meta_test_end); pause=True also pauses the ad sets / campaigns in its cells (meta_status)."""
    st = next((s for s in studies() if s["id"] == str(study_id)), None)
    out = [{"type": "meta_test_end", "study_id": str(study_id), "name": (st or {}).get("name", "")}]
    if pause and st:
        for cell in st["cells"]:
            if cell["level"] in ("adset", "campaign", "ad"):
                out += [{"type": "meta_status", "level": cell["level"], "id": i, "status": "paused"} for i in cell["ids"]]
    return out


def build(mode, a):
    """One entry point for the UI and MCP: mode split | creative | variant | end -> draft changes (not added yet)."""
    dates = {k: a.get(k) for k in ("start_date", "end_date")}
    if mode == "split":
        cells = a.get("cells")
        if not cells and a.get("ids"):
            cells = [{"name": n, "ids": [i]} for i, n in zip(a["ids"], a.get("names") or [None] * len(a["ids"]))]
        return [{"type": "meta_test_split", "name": a.get("name"), "level": a.get("level", "adset"), "cells": cells or [],
                 "description": a.get("description"), **dates}]
    if mode == "creative":
        c = {"type": "meta_test_creative", "name": a.get("name"), "ads": a.get("ads") or [], "description": a.get("description"), **dates}
        for k in ("daily_budget", "budget_percent"):
            if a.get(k) not in (None, ""):
                c[k] = float(a[k])
        return [c]
    if mode == "variant":
        return variant_changes(a.get("adset_id"), a.get("change") or {}, a.get("name"), a.get("variant_name"),
                               a.get("start_date"), a.get("end_date"), a.get("percent") or 50)
    if mode == "end":
        return end_changes(a.get("study_id"), bool(a.get("pause")))
    raise ValueError("mode must be split, creative, variant or end")


def add(mode, a, by="you", note=None):
    return meta.add(build(mode, a), by=by, note=note)


CHANGE_REFERENCE = """Meta tests (A/B and creative tests; Meta calls them ad studies). Draft only; pushed with Push live.
  {"type":"meta_test_split","name":"..","level":"adset|campaign","start_date":"YYYY-MM-DD (optional, default today)",
   "end_date":"YYYY-MM-DD","cells":[{"name":"A","ids":["<ad set id>"],"percent":50},{"name":"B","ids":["d-s1"],"percent":50}]}
     (2-5 cells, whole-number shares of at least 10 adding up to 100; leave all percents out for equal shares)
  {"type":"meta_test_creative","name":"..","ads":["<ad id>","<ad id>"],"daily_budget":30.0,"end_date":".."}
     (2-7 ads, one per cell, ideally in one ad set; daily_budget = test budget per day, or "budget_percent":20 instead)
  {"type":"meta_test_end","study_id":".."}   (ends a running test now: final, can't be undone)
  A test can't be removed from Meta once made, and Meta can't test-run one first: Push live checks the objects with reads.
  Variant: meta_new_adset takes "from_adset":"<ad set id>" to copy that ad set (targeting, optimisation, bidding, budget)
  and change only the fields given (countries, age_min/age_max, placements, optimization_goal, conversion_event)."""
