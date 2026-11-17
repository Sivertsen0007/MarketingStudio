"""Audiences for Meta: the Audiences view and the draft change type "meta_audience" (pushed by meta_ads like every
other Meta change; team approval applies the same way).

What Meta offers (Marketing API v27, checked 17 November 2026 with reads only):
- custom audiences live on the ad account: GET act_<id>/customaudiences (subtype, approximate_count_lower_bound /
  upper_bound, retention_days, lookalike_spec, operation_status, data_source). Small audiences only show a floor
  value (for example 20 or 1,000 for both bounds), a lookalike that is still being made shows -1.
- website audiences: rule {"inclusions": {"operator": "or", "rules": [{"event_sources": [{"type": "pixel", "id": <pixel>}],
  "retention_seconds": <= 180 days, "filter": {"operator": "and", "filters": [{"field": "url", "operator": "i_contains",
  "value": "/egypt"}]}}]}}; pixel events use {"field": "event", "operator": "eq", "value": "SelectCabin"}
- engagement audiences: the same rule shape with event_sources of type page, ig_business, lead or video and an
  event filter (for example page_visited, seen on the account's own engagement audiences), up to 365 days
- lookalikes: POST act_<id>/customaudiences {name, subtype LOOKALIKE, origin_audience_id, lookalike_spec {ratio} or
  {starting_ratio, ratio}}. Studio never sends a country or location_spec: in v27 the ad set's location decides
  where a lookalike is used. A seed needs at least 100 people.
- an audience is used through ad set targeting (custom_audiences / excluded_custom_audiences) and as a lookalike seed;
  custom audiences have no "adsets" edge in v27, so Studio reads the ad sets' targeting to find the users.

Safety:
- customer lists (subtype CUSTOM) are read-only: Studio never uploads, reads or changes customer data, and does not
  use them as lookalike seeds
- creating, editing and deleting audiences has no proven validate_only on Meta, so Studio never sends an audience
  to Meta before Push live: the preview checks what it can locally and with reads
- new audiences are undone by deleting them; edits are undone by writing the old values back; deleting an audience
  is final and refused while anything uses it (the users are named)

Draft change (one JSON object):
  {"type":"meta_audience","action":"create","kind":"website","name":"...","retention_days":30,
   "rules":[{"field":"url","op":"contains","value":"/egypt"},{"field":"event","value":"SelectCabin"}],"match":"any"|"all",
   "exclude":[...],"prefill":true,"description":"..."}
  {"type":"meta_audience","action":"create","kind":"engagement","source":"page"|"instagram"|"video"|"lead_form",
   "event":"engaged"|..., "retention_days":365, "video_ids":[...] (video), "source_id"?: page / Instagram account id}
  {"type":"meta_audience","action":"create","kind":"lookalike","name":"...","seed_id":"<audience id>","ratio":1..10
   (percent) or "from":0..9,"to":1..10}
  {"type":"meta_audience","action":"edit","id":"<audience id>","name"?,"description"?,"retention_days"?,
   "rules"/"exclude"/"match"? (website)}
  {"type":"meta_audience","action":"remove","id":"<audience id>"}
Attach / detach on ad sets: meta_targeting with audiences_add / audiences_remove / excluded_add / excluded_remove.
"""
import json
import re

TYPE = "meta_audience"
ACTIONS = ("create", "edit", "remove")
KINDS = ("website", "engagement", "lookalike")
URL_OPS = {"contains": "i_contains", "equals": "eq", "not_contains": "i_not_contains"}
URL_LABEL = {v: k.replace("_", " ") for k, v in URL_OPS.items()}
WEBSITE_DAYS, ENGAGEMENT_DAYS = 180, 365
DAY = 86400
# source -> (event_sources type, {Studio key: Meta event}, label)
ENGAGEMENT = {
    "page": ("page", {"engaged": "page_engaged", "visited": "page_visited", "messaged": "page_messaged",
                      "cta": "page_cta_clicked", "saved": "page_or_post_save", "post": "page_post_interaction"}, "Facebook Page"),
    "instagram": ("ig_business", {"engaged": "ig_business_profile_engaged", "all": "ig_business_profile_all",
                                  "visited": "ig_business_profile_visit", "messaged": "ig_user_messaged_business"}, "Instagram account"),
    "video": ("video", {"3s": "video_watched", "thruplay": "thruplay_watched", "25": "video_view_25_percent",
                        "50": "video_view_50_percent", "75": "video_view_75_percent", "95": "video_completed"}, "video viewers"),
    "lead_form": ("lead", {"opened": "lead_generation_opened", "not_submitted": "lead_generation_dropoff",
                           "submitted": "lead_generation_submitted"}, "lead form"),
}
EVENT_WORDS = {"engaged": "engaged with", "visited": "visited", "messaged": "messaged", "cta": "clicked a call-to-action on",
               "saved": "saved the Page or a post of", "post": "engaged with a post or ad of", "all": "engaged in any way with",
               "3s": "watched at least 3 seconds of", "thruplay": "watched (ThruPlay) ", "25": "watched 25% of", "50": "watched 50% of",
               "75": "watched 75% of", "95": "watched 95% of", "opened": "opened", "not_submitted": "opened but did not submit",
               "submitted": "submitted"}
SUBTYPE = {"WEBSITE": "Website", "ENGAGEMENT": "Engagement", "IG_BUSINESS": "Instagram engagement", "LOOKALIKE": "Lookalike",
           "CUSTOM": "Customer list", "VIDEO": "Video viewers", "OFFLINE_CONVERSION": "Offline", "APP": "App activity"}
EDITABLE = ("WEBSITE", "ENGAGEMENT", "IG_BUSINESS", "VIDEO", "LOOKALIKE")
NO_CUSTOMER_DATA = "Studio doesn't upload, read or change customer data (customer lists are read-only here)"
NO_COUNTRY = "Studio never sends a country or location for a lookalike: the ad set's location decides where it is used"
CUSTOMER_KEYS = ("schema", "payload", "emails", "phones", "members", "customer_file_source", "users", "hashed")
EVENT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,49}$")
SEED_MIN = 100
SIZE_NOTE = ("Sizes are Meta's own estimates. Small audiences show only a floor value (Meta hides exact sizes under "
             "about 1,000); a lookalike shows no size until Meta has made it.")


# ---------- reading ----------

def size_text(a):
    lo, hi = a.get("approximate_count_lower_bound"), a.get("approximate_count_upper_bound")
    if not isinstance(lo, int) or lo < 0:
        return "not available yet" if a.get("subtype") == "LOOKALIKE" else "not available"
    if hi == lo and lo <= 1000:
        return f"small (Meta shows {lo:,}; exact sizes under about 1,000 are hidden)"
    if isinstance(hi, int) and hi > lo:
        return f"{lo:,}-{hi:,}"
    return f"about {lo:,}"


def _rule(a):
    try:
        return json.loads(a["rule"]) if isinstance(a.get("rule"), str) else (a.get("rule") or {})
    except ValueError:
        return {}


def seed_people(a):
    """(upper bound of the seed's size or None, text). None = unknown."""
    lo, hi = a.get("approximate_count_lower_bound"), a.get("approximate_count_upper_bound")
    if not isinstance(lo, int) or lo < 0:
        return None, "Meta shows no size for the seed"
    if hi == lo and lo <= 1000:
        return (lo if lo < SEED_MIN else None), f"Meta only shows a floor value ({lo:,}) for the seed"
    return hi if isinstance(hi, int) else lo, f"{lo:,}-{hi:,}" if isinstance(hi, int) else f"{lo:,}"


def users(d):
    """{audience id: [where]} from cached ad sets (targeting) and lookalikes (their seed)."""
    out = {}
    for s in d.get("adsets") or []:
        if s.get("effective_status") in ("DELETED", "ARCHIVED"):
            continue
        t = s.get("targeting") or {}
        for a in t.get("custom_audiences") or []:
            out.setdefault(str(a.get("id")), []).append(f'ad set "{s.get("name", s.get("id"))}"')
        for a in t.get("excluded_custom_audiences") or []:
            out.setdefault(str(a.get("id")), []).append(f'ad set "{s.get("name", s.get("id"))}" (excluded)')
    for a in d.get("audiences") or []:
        for o in (a.get("lookalike_spec") or {}).get("origin") or []:
            out.setdefault(str(o.get("id")), []).append(f'seed of lookalike "{a.get("name", a.get("id"))}"')
    return {k: sorted(set(v)) for k, v in out.items()}


def rows(d):
    """The Audiences view for the current Meta ad account (cached data, no extra calls)."""
    used = users(d)
    names = {str(a["id"]): a.get("name", "") for a in d.get("audiences") or []}
    out = []
    for a in d.get("audiences") or []:
        st = a.get("subtype", "")
        crm = st == "CUSTOM"
        spec = a.get("lookalike_spec") or {}
        seed = ", ".join(o.get("name") or names.get(str(o.get("id")), str(o.get("id"))) for o in spec.get("origin") or [])
        ratio = spec.get("ratio")
        days = a.get("retention_days")
        status = (a.get("operation_status") or {})
        out.append({"id": str(a["id"]), "name": a.get("name", ""), "type": SUBTYPE.get(st, st.replace("_", " ").title()), "type_code": st,
                    "size": size_text(a), "retention": f"{days} days" if days else "",
                    "retention_days": days or None, "description": a.get("description", ""),
                    "lookalike": (f"{round(float(spec['starting_ratio']) * 100)}-{round(float(ratio) * 100)}% of " if spec.get("starting_ratio")
                                  else f"{round(float(ratio) * 100)}% of " if ratio else "") + seed if st == "LOOKALIKE" else "",
                    "used_by": used.get(str(a["id"]), []), "status": status.get("description", ""),
                    "status_ok": status.get("code") in (None, 200),
                    "editable": st in EDITABLE, "rules_editable": st == "WEBSITE",
                    "read_only_why": NO_CUSTOMER_DATA if crm else ("" if st in EDITABLE else "Studio can't edit this kind of audience"),
                    "customer_list": crm, "note": NO_CUSTOMER_DATA if crm else "",
                    "seed_ok": st not in ("LOOKALIKE", "CUSTOM")})
    out.sort(key=lambda r: (r["customer_list"], r["type"], r["name"].lower()))
    return {"platform": "meta", "rows": out, "size_note": SIZE_NOTE, "customer_note": NO_CUSTOMER_DATA,
            "fetched_at": d.get("fetched_at"), "engagement": {k: sorted(v[1]) for k, v in ENGAGEMENT.items()}}


# ---------- checks ----------

def _find(d, aid):
    return next((a for a in d.get("audiences") or [] if str(a.get("id")) == str(aid)), None)


def _rules_problems(rules, what, problems, allow_event=True):
    if not isinstance(rules, list):
        problems.append(f"{what}: give a list of rules")
        return
    for i, r in enumerate(rules, 1):
        f = (r or {}).get("field", "url")
        if f == "url":
            if r.get("op") not in URL_OPS:
                problems.append(f"{what} {i}: op must be one of {', '.join(URL_OPS)}")
            elif not str(r.get("value") or "").strip():
                problems.append(f"{what} {i}: the URL text is empty")
        elif f == "event" and allow_event:
            if not EVENT_RE.match(str(r.get("value") or "")):
                problems.append(f"{what} {i}: event must be a pixel event name like SelectCabin, ViewContent or Lead")
        else:
            problems.append(f"{what} {i}: field must be url{' or event' if allow_event else ''}")


def _days(c, limit, problems, need):
    v = c.get("retention_days")
    if v in (None, ""):
        if need:
            problems.append(f"retention_days: how long people stay in the audience (1-{limit} days)")
        return
    try:
        n = int(v)
        if not 1 <= n <= limit:
            problems.append(f"retention_days must be 1-{limit}")
    except (TypeError, ValueError):
        problems.append(f"retention_days must be a whole number of days (1-{limit})")


def check(c, d):
    """-> (problems, warnings). Local checks on cached data; nothing is sent to Meta."""
    problems, warnings = [], []
    if any(c.get(k) for k in CUSTOMER_KEYS) or c.get("kind") in ("custom", "customer", "crm"):
        return [NO_CUSTOMER_DATA], []
    action = c.get("action") or "create"
    if action not in ACTIONS:
        return [f"action must be one of {', '.join(ACTIONS)}"], []
    if action in ("edit", "remove"):
        a = _find(d, c.get("id"))
        if not a:
            return [f"audience {c.get('id')} not found in this ad account (refresh?)"], []
        if a.get("subtype") == "CUSTOM":
            return [NO_CUSTOMER_DATA], []
        if action == "remove":
            return problems, warnings
        if a.get("subtype") not in EDITABLE:
            return ["Studio can't edit this kind of audience"], []
        if not any(c.get(k) not in (None, "", []) for k in ("name", "description", "retention_days", "rules", "exclude", "match")):
            problems.append("nothing to change: give name, description, retention_days or rules")
        if a.get("subtype") == "LOOKALIKE" and any(c.get(k) not in (None, "", []) for k in ("retention_days", "rules", "exclude", "match")):
            problems.append("a lookalike can only be renamed or get a new description (make a new one for another size or seed)")
        if any(c.get(k) not in (None, "", []) for k in ("rules", "exclude", "match")) and a.get("subtype") != "WEBSITE":
            problems.append("only website audiences get new rules from Studio")
        _days(c, WEBSITE_DAYS if a.get("subtype") == "WEBSITE" else ENGAGEMENT_DAYS, problems, False)
        if c.get("rules") is not None:
            if not c["rules"]:
                problems.append("rules: give at least one rule")
            _rules_problems(c["rules"], "rule", problems)
        if c.get("exclude"):
            _rules_problems(c["exclude"], "exclude rule", problems)
        return problems, warnings
    kind = c.get("kind")
    if kind not in KINDS:
        return [f"kind must be one of {', '.join(KINDS)} (customer lists: {NO_CUSTOMER_DATA})"], []
    if not str(c.get("name") or "").strip():
        problems.append("the new audience needs a name")
    if kind == "website":
        _days(c, WEBSITE_DAYS, problems, True)
        if not c.get("rules") and not c.get("all_visitors"):
            problems.append("rules: give at least one URL or event rule (or all_visitors: true)")
        _rules_problems(c.get("rules") or [], "rule", problems)
        if c.get("exclude"):
            _rules_problems(c["exclude"], "exclude rule", problems)
        if c.get("match") not in (None, "", "any", "all"):
            problems.append("match must be any or all")
    elif kind == "engagement":
        src = ENGAGEMENT.get(c.get("source"))
        if not src:
            problems.append(f"source must be one of {', '.join(ENGAGEMENT)}")
        else:
            if c.get("event") not in src[1]:
                problems.append(f"event for {c['source']} must be one of {', '.join(src[1])}")
            if c["source"] == "video":
                vids = c.get("video_ids") or []
                if not vids or not all(re.fullmatch(r"\d+", str(v)) for v in vids):
                    problems.append("video_ids: give the ids of the videos (numbers)")
            elif c.get("source_id") is not None and not re.fullmatch(r"\d+", str(c["source_id"])):
                problems.append("source_id must be a Page or Instagram account id (numbers)")
        _days(c, ENGAGEMENT_DAYS, problems, True)
    else:  # lookalike
        if any(c.get(k) for k in ("country", "countries", "location_spec", "location")):
            problems.append(NO_COUNTRY)
        seed = _find(d, c.get("seed_id"))
        if not seed:
            problems.append(f"seed audience {c.get('seed_id')} not found in this ad account")
        elif seed.get("subtype") == "CUSTOM":
            problems.append("seed: a customer list can't be a seed here. " + NO_CUSTOMER_DATA)
        elif seed.get("subtype") == "LOOKALIKE":
            problems.append("seed: a lookalike can't be the seed of another lookalike")
        else:
            n, txt = seed_people(seed)
            if n is not None and n < SEED_MIN:
                warnings.append(f"the seed may have fewer than {SEED_MIN} people ({txt}): Meta needs at least {SEED_MIN} to make a lookalike")
            elif n is None:
                warnings.append(f"{txt}: Meta needs at least {SEED_MIN} people in the seed (it may refuse a smaller one)")
            if (seed.get("operation_status") or {}).get("code") not in (None, 200):
                warnings.append(f"seed status: {(seed.get('operation_status') or {}).get('description', '')}")
        lo, hi = _ratio(c)
        if hi is None:
            problems.append("give ratio (1-10, percent) or from (0-9) and to (1-10)")
        elif not (0 <= lo < hi <= 10 and hi >= 1):
            problems.append("lookalike size: from 0-9 and to 1-10 percent, from below to")
    return problems, warnings


def _ratio(c):
    try:
        if c.get("ratio") not in (None, ""):
            return 0, int(c["ratio"])
        if c.get("to") not in (None, ""):
            return int(c.get("from") or 0), int(c["to"])
    except (TypeError, ValueError):
        return 0, None
    return 0, None


def refusals(changes, d):
    """Removing an audience that something uses is refused before it reaches the draft (users named)."""
    used = users(d)
    out = []
    for c in changes:
        if c.get("type") == TYPE and c.get("action") == "remove" and used.get(str(c.get("id"))):
            a = _find(d, c["id"]) or {}
            out.append(f'audience "{a.get("name", c["id"])}" is in use, so it can\'t be removed. Used by: ' + "; ".join(used[str(c["id"])])
                       + ". Detach it there first")
    return out


def normalise(c):
    for k in ("id", "seed_id", "source_id"):
        if c.get(k) is not None and not isinstance(c[k], str):
            c[k] = str(c[k])
    if c.get("video_ids"):
        c["video_ids"] = [str(v) for v in c["video_ids"]]
    c.setdefault("action", "create")
    for k in ("rules", "exclude"):
        for r in c.get(k) or []:
            if isinstance(r, dict):
                r.setdefault("field", "url")
    return c


def _rule_text(r):
    if r.get("field") == "event":
        return f'event {r.get("value")}'
    return f'URL {str(r.get("op", "")).replace("_", " ")} "{r.get("value")}"'


def summarise(c, d):
    action = c.get("action") or "create"
    if action in ("edit", "remove"):
        a = _find(d, c.get("id")) or {}
        nm = a.get("name", c.get("id"))
        if action == "remove":
            return f'Meta delete audience "{nm}"'
        bits = [f'rename to "{c["name"]}"' if c.get("name") else "", f"{c['retention_days']} days" if c.get("retention_days") else "",
                "new rules" if c.get("rules") is not None or c.get("exclude") is not None else "",
                "new description" if c.get("description") is not None else ""]
        return f'Meta edit audience "{nm}": ' + ", ".join(b for b in bits if b)
    if c.get("kind") == "lookalike":
        lo, hi = _ratio(c)
        seed = (_find(d, c.get("seed_id")) or {}).get("name", c.get("seed_id"))
        return f'Meta new lookalike "{c.get("name")}": {f"{lo}-{hi}" if lo else hi}% like "{seed}"'
    if c.get("kind") == "engagement":
        src = ENGAGEMENT.get(c.get("source"), (None, {}, c.get("source")))
        return (f'Meta new engagement audience "{c.get("name")}": people who {EVENT_WORDS.get(c.get("event"), c.get("event"))} '
                f'the {src[2]} ({c.get("retention_days")} days)')
    joiner = " and " if c.get("match") == "all" else " or "
    rules = "all website visitors" if c.get("all_visitors") and not c.get("rules") else joiner.join(_rule_text(r) for r in c.get("rules") or [])
    exc = ("; not " + " or ".join(_rule_text(r) for r in c["exclude"])) if c.get("exclude") else ""
    return f'Meta new website audience "{c.get("name")}" ({c.get("retention_days")} days): {rules}{exc}'


# ---------- building Meta's rule ----------

def _filter(r):
    if r.get("field") == "event":
        return {"field": "event", "operator": "eq", "value": str(r["value"])}
    return {"field": "url", "operator": URL_OPS[r["op"]], "value": str(r["value"])}


def website_rule(c, pixel, days):
    src = [{"type": "pixel", "id": str(pixel)}]
    secs = int(days) * DAY

    def block(rules, match):
        if not rules:
            return [{"event_sources": src, "retention_seconds": secs,
                     "filter": {"operator": "and", "filters": [{"field": "url", "operator": "i_contains", "value": ""}]}}]
        if match == "all":
            return [{"event_sources": src, "retention_seconds": secs, "filter": {"operator": "and", "filters": [_filter(r) for r in rules]}}]
        return [{"event_sources": src, "retention_seconds": secs, "filter": {"operator": "and", "filters": [_filter(r)]}} for r in rules]
    rule = {"inclusions": {"operator": "or", "rules": block(c.get("rules") or [], c.get("match"))}}
    if c.get("exclude"):
        rule["exclusions"] = {"operator": "or", "rules": block(c["exclude"], "any")}
    return rule


def engagement_rule(c, source_id):
    typ, events, _ = ENGAGEMENT[c["source"]]
    ids = c.get("video_ids") if c["source"] == "video" else [source_id]
    return {"inclusions": {"operator": "or", "rules": [{
        "event_sources": [{"type": typ, "id": str(i)} for i in ids], "retention_seconds": int(c["retention_days"]) * DAY,
        "filter": {"operator": "and", "filters": [{"field": "event", "operator": "eq", "value": events[c["event"]]}]}}]}}


def _with_days(rule, days):
    """The same rule with every retention_seconds set to `days`."""
    r = json.loads(json.dumps(rule))
    for part in ("inclusions", "exclusions"):
        for x in (r.get(part) or {}).get("rules") or []:
            x["retention_seconds"] = int(days) * DAY
    return r


def rule_rows(rule):
    """A website rule Studio can rebuild -> {rules, exclude, match}, or None when it has anything else in it."""
    def parse(block):
        rs = (block or {}).get("rules") or []
        out, match = [], "any"
        for x in rs:
            raw = (x.get("filter") or {}).get("filters") or []
            groups = [f for f in raw if f.get("filters") is not None]  # Ads Manager nests "any of these URLs" as a group
            fs = [f for f in raw if f.get("filters") is None and not (f.get("field") == "url" and f.get("operator") == "i_contains"
                                                                       and f.get("value") == "")]  # i_contains "" = any URL
            if groups:
                if len(groups) > 1 or fs or any(g.get("filters") is not None for g in groups[0]["filters"]):
                    return None, None
                fs = groups[0]["filters"]
                op = groups[0].get("operator")
                if op not in ("or", "and"):
                    return None, None
            else:
                op = "and"
            if op == "and" and len(fs) > 1:
                if len(rs) > 1:
                    return None, None
                match = "all"
            for f in fs:
                if f.get("field") == "event" and f.get("operator") == "eq":
                    out.append({"field": "event", "value": f.get("value")})
                elif f.get("field") == "url" and f.get("operator") in URL_LABEL:
                    if f.get("value") == "" and f.get("operator") == "i_contains":
                        continue
                    out.append({"field": "url", "op": URL_LABEL[f["operator"]], "value": f.get("value")})
                else:
                    return None, None
        return out, match
    inc, match = parse(rule.get("inclusions"))
    exc, _ = parse(rule.get("exclusions"))
    if inc is None or exc is None:
        return None
    return {"rules": inc, "exclude": exc, "match": match}


# ---------- live steps (no Meta call before Push live) ----------

def ops_for(c, meta):
    """Draft change -> steps for meta_ads.plan / _push. Reads only (the live audience, the ad sets' targeting)."""
    d = meta.load()
    action = c.get("action") or "create"
    if action == "create":
        if c["kind"] == "lookalike":
            lo, hi = _ratio(c)
            spec = {"ratio": round(hi / 100, 2)} if not lo else {"starting_ratio": round(lo / 100, 2), "ratio": round(hi / 100, 2)}
            p = {"name": c["name"], "subtype": "LOOKALIKE", "origin_audience_id": c["seed_id"], "lookalike_spec": json.dumps(spec)}
            line = f'new lookalike "{c["name"]}" ({f"{lo}-{hi}" if lo else hi}% like audience {c["seed_id"]}; location from the ad set)'
        elif c["kind"] == "website":
            pixel = meta._pixel()
            if not pixel:
                raise ValueError("no Meta pixel found for this ad account")
            p = {"name": c["name"], "rule": json.dumps(website_rule(c, pixel, c["retention_days"])), "prefill": bool(c.get("prefill", True))}
            line = f'new website audience "{c["name"]}" ({int(c["retention_days"])} days, pixel {pixel})'
        else:
            src = c.get("source_id")
            if c["source"] == "page":
                src = src or meta._page_for_account(d)
            elif c["source"] in ("instagram",):
                src = src or meta._identity(need_ig=True).get("instagram_user_id")
            elif c["source"] == "lead_form":
                src = src or meta._page_for_account(d)
            if c["source"] != "video" and not src:
                raise ValueError(f"no {ENGAGEMENT[c['source']][2]} found for this ad account: give source_id")
            p = {"name": c["name"], "rule": json.dumps(engagement_rule(c, src)), "prefill": True}
            line = f'new engagement audience "{c["name"]}" ({ENGAGEMENT[c["source"]][2]}, {c["event"]}, {int(c["retention_days"])} days)'
        if c.get("description"):
            p["description"] = c["description"]
        return [{"kind": "audience_create", "phase": "audience", "params": p, "line": line + ", created on Meta at push"}]
    a = _find(d, c["id"])
    if not a:
        raise ValueError(f"audience {c['id']} not found in this ad account")
    if a.get("subtype") == "CUSTOM":
        raise ValueError(NO_CUSTOMER_DATA)
    if action == "remove":
        live_users = users({"adsets": meta.get_all(f"{meta.act()}/adsets", fields="id,name,effective_status,targeting{custom_audiences,excluded_custom_audiences}"),
                            "audiences": d.get("audiences")})
        who = live_users.get(str(c["id"]))
        if who:
            raise ValueError(f'audience "{a.get("name")}" is in use, so it can\'t be removed. Used by: ' + "; ".join(who) + ". Detach it there first")
        return [{"kind": "audience_delete", "phase": "audience_delete", "id": str(c["id"]), "permanent": True,
                 "line": f'delete audience "{a.get("name")}" (final: a deleted audience can\'t be brought back)'}]
    cur = meta.get(str(c["id"]), fields="name,description,subtype,rule,retention_days")
    new, old, bits = {}, {}, []
    if c.get("name") and c["name"] != cur.get("name"):
        new["name"], old["name"] = c["name"], cur.get("name", "")
        bits.append(f'name "{cur.get("name")}" -> "{c["name"]}"')
    if c.get("description") is not None and c["description"] != cur.get("description", ""):
        new["description"], old["description"] = c["description"], cur.get("description", "")
        bits.append("description")
    rule = _rule(cur)
    if any(c.get(k) is not None for k in ("rules", "exclude", "match")) or c.get("retention_days"):
        if not rule:
            raise ValueError("this audience has no rule Studio can change")
        days = int(c.get("retention_days") or cur.get("retention_days") or 30)
        if any(c.get(k) is not None for k in ("rules", "exclude", "match")):
            have = rule_rows(rule)
            if have is None:
                raise ValueError("this audience's rule has parts Studio can't rebuild: change it in Ads Manager")
            merged = {"rules": c["rules"] if c.get("rules") is not None else have["rules"],
                      "exclude": c["exclude"] if c.get("exclude") is not None else have["exclude"],
                      "match": c.get("match") or have["match"]}
            pixel = next((s.get("id") for x in (rule.get("inclusions") or {}).get("rules") or []
                          for s in x.get("event_sources") or [] if s.get("type") == "pixel"), None) or meta._pixel()
            nr = website_rule(merged, pixel, days)
            bits.append("rules")
        else:
            nr = _with_days(rule, days)
        if c.get("retention_days"):
            bits.append(f'{cur.get("retention_days")} -> {days} days')
        new["rule"], old["rule"] = json.dumps(nr), json.dumps(rule)
    if not new:
        raise ValueError(f'audience "{cur.get("name")}" already looks like this')
    longer = c.get("retention_days") and int(c["retention_days"]) > int(cur.get("retention_days") or 0)
    return [{"kind": "audience_update", "id": str(c["id"]), "params": new, "undo": old,
             "line": f'audience "{cur.get("name")}": ' + "; ".join(bits),
             "spend": "longer retention or new rules can put the ad sets that use it in front of more people"
             if (longer or "rules" in " ".join(bits)) and users(d).get(str(c["id"])) else ""}]


def check_step(s):
    """No Meta call: creating or changing an audience has no proven validate_only on Meta."""
    if s["kind"] == "audience_create" and "country" in json.dumps(s.get("params", {}).get("lookalike_spec", "")):
        return NO_COUNTRY
    return ""


def do(s, meta):
    """Apply one step for real (Push live). Returns the record needed to undo it."""
    if s["kind"] == "audience_create":
        new = meta.post(f"{meta.act()}/customaudiences", s["params"])["id"]
        return {"line": s["line"], "id": new, "undo": "delete-audience", "created": "customaudience"}
    if s["kind"] == "audience_update":
        meta.post(s["id"], s["params"])
        return {"line": s["line"], "id": s["id"], "undo": s["undo"]}
    if s["kind"] == "audience_delete":
        meta.delete(s["id"])
        return {"line": s["line"], "id": s["id"], "undo": None, "final": "a deleted audience can't be brought back"}
    raise ValueError(s["kind"])


STEP_KINDS = ("audience_create", "audience_update", "audience_delete")


# ---------- ad set targeting: attach / detach (meta_targeting) ----------

AUD_KEYS = ("audiences_add", "audiences_remove", "excluded_add", "excluded_remove")


def targeting_problems(c, d):
    problems = []
    for k in AUD_KEYS:
        for aid in c.get(k) or []:
            if not _find(d, aid):
                problems.append(f"{k}: audience {aid} not found in this ad account")
    both = set(map(str, c.get("audiences_add") or [])) & set(map(str, c.get("excluded_add") or []))
    if both:
        problems.append("the same audience can't be both included and excluded")
    return problems


def apply_targeting(tg, c, d):
    """Ad set targeting with the audience changes applied -> (new targeting, wider?)."""
    names = {str(a["id"]): a.get("name", "") for a in d.get("audiences") or []}
    wider = False
    for key, add, rem in (("custom_audiences", "audiences_add", "audiences_remove"),
                          ("excluded_custom_audiences", "excluded_add", "excluded_remove")):
        cur = [x for x in tg.get(key) or []]
        ids = [str(x.get("id")) for x in cur]
        for aid in map(str, c.get(rem) or []):
            if aid not in ids:
                raise ValueError(f'audience "{names.get(aid, aid)}" is not {"included in" if key == "custom_audiences" else "excluded from"} this ad set')
            cur = [x for x in cur if str(x.get("id")) != aid]
            wider = True
        for aid in map(str, c.get(add) or []):
            if aid in ids:
                raise ValueError(f'audience "{names.get(aid, aid)}" is already {"included in" if key == "custom_audiences" else "excluded from"} this ad set')
            cur.append({"id": aid, "name": names.get(aid, "")})
        if cur:
            tg[key] = cur
        else:
            tg.pop(key, None)
    return tg, wider


def targeting_words(c, d):
    names = {str(a["id"]): a.get("name", "") for a in d.get("audiences") or []}
    lab = {"audiences_add": "include", "audiences_remove": "stop including", "excluded_add": "exclude", "excluded_remove": "stop excluding"}
    return "; ".join(f"{lab[k]} " + ", ".join(f'"{names.get(str(x), x)}"' for x in c[k]) for k in AUD_KEYS if c.get(k))


def detail(aid, meta):
    """One audience with its rule as Studio rows (for the edit dialog). One read."""
    d = meta.load()
    a = _find(d, aid)
    if not a:
        raise ValueError(f"audience {aid} not found in this ad account")
    if a.get("subtype") == "CUSTOM":
        return {"id": str(aid), "name": a.get("name", ""), "customer_list": True, "note": NO_CUSTOMER_DATA}
    cur = meta.get(str(aid), fields="name,description,subtype,rule,retention_days")
    rr = rule_rows(_rule(cur)) if cur.get("subtype") == "WEBSITE" else None
    return {"id": str(aid), "name": cur.get("name", ""), "description": cur.get("description", ""), "subtype": cur.get("subtype"),
            "retention_days": cur.get("retention_days"), "rules": rr, "rules_editable": rr is not None}
