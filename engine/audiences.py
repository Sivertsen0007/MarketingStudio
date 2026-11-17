"""Audiences for Google Ads: the Audience lists view and the draft change type "user_list" (Push live only).

What Studio does (Google Ads API v26, checked 17 November 2026 with reads and validateOnly):
- lists every user list of the account with type, size (Google's own rounded estimate and range), membership
  duration, where it is used (campaigns, ad groups, combined lists) and status
- creates and edits website-visitor lists (UserList.rule_based_user_list.flexible_rule_user_list: url__ rules,
  each rule its own operand, OR or AND between them, optional exclusions, 1-540 days, prepopulation = Google fills
  the list from recent visitors) and combined lists (UserList.logical_user_list: all of / any of / none of)
- removes a list only when nothing uses it: a list on a campaign or ad group, or inside a combined list, is refused
  and the users are named
- attaching and detaching lists to campaigns is the existing "audience" change type (core / live)

What Studio does not do: customer lists (Customer Match / CRM_BASED) are shown read-only. Studio never uploads,
reads or changes customer data. YouTube-viewer and similar-audience lists can't be made through the API (read and
target only). Lists Google marks read-only (shared from a manager account, Google's own) can't be edited.

Draft change (gae plan format, one JSON object):
  {"type":"user_list","action":"create","kind":"website","name":"...","rules":[{"op":"contains","value":"/egypt"}],
   "match":"any"|"all","exclude":[{"op":"contains","value":"/booking"}],"lifespan":30,"prepopulate":true,"description":"..."}
  {"type":"user_list","action":"create","kind":"combined","name":"...","all":["A"],"any":["B","C"],"none":["D"]}
  {"type":"user_list","action":"edit","list":"<name>","new_name"?, "description"?, "lifespan"?, "rules"/"match"/"exclude"?,
   "all"/"any"/"none"?}
  {"type":"user_list","action":"remove","list":"<name>"}
"""
import json

TYPE = "user_list"
ACTIONS = ("create", "edit", "remove")
KINDS = ("website", "combined")
URL_OPS = {"contains": "CONTAINS", "equals": "EQUALS", "starts_with": "STARTS_WITH", "ends_with": "ENDS_WITH",
           "not_contains": "NOT_CONTAINS"}
OP_LABEL = {v: k.replace("_", " ") for k, v in URL_OPS.items()}
COMBINE = {"all": "ALL", "any": "ANY", "none": "NONE"}
COMBINE_LABEL = {"ALL": "all of", "ANY": "any of", "NONE": "none of"}
LIFESPAN_MAX = 540
FLEX_FIELDS = ("inclusiveRuleOperator", "inclusiveOperands", "exclusiveOperands")
NAME_MAX = 650  # Google's limit on user list names
CUSTOMER_KEYS = ("members", "emails", "phones", "addresses", "customer_match", "crm", "upload", "crm_based_user_list", "hashed")
NO_CUSTOMER_DATA = "Studio doesn't upload or change customer data (customer lists are read-only here)"
READ_ONLY_WHY = {
    "CRM_BASED": "customer list: " + NO_CUSTOMER_DATA,
    "SIMILAR": "similar-audience list: Google no longer updates these (read and target only)",
    "REMARKETING": "Google's own or shared list: read and target only",
    "RULE_BASED": "this list is read-only in Google Ads",
    "LOGICAL": "this combined list is read-only in Google Ads",
}
SIZE_WORDS = {"FIVE_HUNDRED": 500, "ONE_THOUSAND": 1000, "TEN_THOUSAND": 10000, "FIFTY_THOUSAND": 50000,
              "ONE_HUNDRED_THOUSAND": 100000, "THREE_HUNDRED_THOUSAND": 300000, "FIVE_HUNDRED_THOUSAND": 500000,
              "ONE_MILLION": 1000000, "TWO_MILLION": 2000000, "THREE_MILLION": 3000000, "FIVE_MILLION": 5000000,
              "TEN_MILLION": 10000000, "TWENTY_MILLION": 20000000, "THIRTY_MILLION": 30000000, "FIFTY_MILLION": 50000000}
SIZE_NOTE = ("Sizes are Google's own estimates: rounded, and shown as a range. Small lists show only an upper bound "
             "(for example under 500), never an exact count.")
F_LIST = ("user_list.id, user_list.name, user_list.type, user_list.read_only, user_list.description, user_list.membership_life_span, "
          "user_list.rule_based_user_list.prepopulation_status, "
          "user_list.rule_based_user_list.flexible_rule_user_list.inclusive_rule_operator, "
          "user_list.rule_based_user_list.flexible_rule_user_list.inclusive_operands, "
          "user_list.rule_based_user_list.flexible_rule_user_list.exclusive_operands, user_list.logical_user_list.rules")


# ---------- reading ----------

def size_range(v):
    """Google's size range enum -> plain text ("1,000-10,000", "under 500")."""
    if not v or v in ("UNSPECIFIED", "UNKNOWN"):
        return ""
    if v.startswith("LESS_THAN_"):
        n = SIZE_WORDS.get(v[len("LESS_THAN_"):])
        return f"under {n:,}" if n else v.replace("_", " ").lower()
    if v.startswith("OVER_"):
        n = SIZE_WORDS.get(v[len("OVER_"):])
        return f"over {n:,}" if n else v.replace("_", " ").lower()
    if "_TO_" in v:
        a, b = v.split("_TO_", 1)
        if a in SIZE_WORDS and b in SIZE_WORDS:
            return f"{SIZE_WORDS[a]:,}-{SIZE_WORDS[b]:,}"
    return v.replace("_", " ").lower()


def size_text(n, rng):
    """'about 3,000 (1,000-10,000)'; a small list only gets its range (Google gives no exact small counts)."""
    r = size_range(rng)
    if n in (None, "", 0, "0"):
        return r or "unknown"
    if r.startswith("under"):
        return r
    return f"about {int(n):,}" + (f" ({r})" if r else "")


def _rule_items(operands):
    out = []
    for o in operands or []:
        for g in (o.get("rule") or {}).get("ruleItemGroups") or []:
            for it in g.get("ruleItems") or []:
                s = it.get("stringRuleItem") or {}
                if it.get("name") == "url__" and s:
                    out.append({"op": OP_LABEL.get(s.get("operator"), str(s.get("operator", "")).lower()), "value": s.get("value", "")})
                else:
                    out.append({"op": "other", "value": it.get("name", "")})
        if not (o.get("rule") or {}).get("ruleItemGroups"):
            out.append({"op": "all visitors", "value": ""})
    return out


def describe(u, names=None):
    """Plain-text rule of a list ('URL contains /egypt or URL contains /red-sea; not URL contains /booking')."""
    names = names or {}
    rb = (u.get("ruleBasedUserList") or {}).get("flexibleRuleUserList")
    if rb:
        joiner = " and " if rb.get("inclusiveRuleOperator") == "AND" else " or "
        inc = [("all visitors" if x["op"] == "all visitors" else f'URL {x["op"]} "{x["value"]}"') for x in _rule_items(rb.get("inclusiveOperands"))]
        exc = [f'URL {x["op"]} "{x["value"]}"' for x in _rule_items(rb.get("exclusiveOperands"))]
        return joiner.join(inc) + (f"; not {' or '.join(exc)}" if exc else "")
    lg = (u.get("logicalUserList") or {}).get("rules")
    if lg:
        parts = []
        for r in lg:
            ls = [names.get(_tail((o.get("userList") or "")), _tail(o.get("userList") or "")) for o in r.get("ruleOperands") or []]
            parts.append(f"{COMBINE_LABEL.get(r.get('operator'), r.get('operator', '').lower())} " + ", ".join(f'"{x}"' for x in ls))
        return "; ".join(parts)
    return ""


def _tail(res):
    return str(res or "").rsplit("/", 1)[-1]


def editable(u):
    """(can edit, why not). u: the API's userList dict."""
    t = u.get("type")
    if t == "CRM_BASED":
        return False, READ_ONLY_WHY["CRM_BASED"]
    if u.get("readOnly"):
        return False, READ_ONLY_WHY.get(t, "read-only in Google Ads")
    if t == "RULE_BASED" and (u.get("ruleBasedUserList") or {}).get("flexibleRuleUserList"):
        return True, ""
    if t == "LOGICAL" and (u.get("logicalUserList") or {}).get("rules"):
        return True, ""
    return False, "Studio edits website-visitor and combined lists only (target or detach this one)"


def usage(base):
    """{user list id (str): [where]} from gapi's cached base: campaigns, ad groups (with excluded) and combined lists."""
    out = {}
    for a in base["rows"].get("audiences", []):
        if not a.get("user_list_id") or a.get("status") == "Removed":
            continue
        where = (f'ad group "{a["campaign"]} / {a["ad_group"]}"' if a.get("ad_group") else f'campaign "{a["campaign"]}"') + \
            (" (excluded)" if a.get("excluded") else "")
        out.setdefault(str(a["user_list_id"]), []).append(where)
    for u in base["rows"].get("user_lists", []):
        for r in (u.get("_logical") or []):
            for o in r.get("ruleOperands") or []:
                out.setdefault(_tail(o.get("userList")), []).append(f'combined list "{u["list"]}"')
    return {k: sorted(set(v)) for k, v in out.items()}


def rows(acct):
    """The Audience lists view: every list with type, size, membership days, where used and what Studio may do."""
    import gapi  # noqa: PLC0415 - gapi imports live, which imports core
    d = gapi.base(acct)
    used = usage(d)
    names = {str(u["id"]): u["list"] for u in d["rows"].get("user_lists", [])}
    out = []
    for u in d["rows"].get("user_lists", []):
        raw = u.get("_raw") or {"type": u.get("type_code"), "readOnly": u.get("read_only")}
        ok, why = editable(raw)
        crm = raw.get("type") == "CRM_BASED"
        days = u.get("lifespan")
        out.append({"id": str(u["id"]), "name": u["list"], "type": u.get("type", ""), "type_code": raw.get("type", ""),
                    "kind": "website" if (raw.get("ruleBasedUserList") or {}).get("flexibleRuleUserList") else
                    "combined" if raw.get("type") == "LOGICAL" else "",
                    "size_search": size_text(u.get("size_search"), u.get("size_range_search")),
                    "size_display": size_text(u.get("size_display"), u.get("size_range_display")),
                    "membership_days": None if days in (None, "") else int(days),
                    "retention": "" if days in (None, "") else ("no expiry" if int(days) >= 10000 else f"{int(days)} days"),
                    "rule": describe(raw, names), "description": u.get("description", ""),
                    "used_by": used.get(str(u["id"]), []), "status": u.get("status", ""),
                    "editable": ok, "read_only_why": why, "customer_list": crm,
                    "note": NO_CUSTOMER_DATA if crm else "",
                    "rules": _rule_items(((raw.get("ruleBasedUserList") or {}).get("flexibleRuleUserList") or {}).get("inclusiveOperands")),
                    "exclude": _rule_items(((raw.get("ruleBasedUserList") or {}).get("flexibleRuleUserList") or {}).get("exclusiveOperands")),
                    "match": "all" if ((raw.get("ruleBasedUserList") or {}).get("flexibleRuleUserList") or {}).get("inclusiveRuleOperator") == "AND" else "any",
                    "combine": {COMBINE_KEY.get(r.get("operator"), "any"): [names.get(_tail(o.get("userList")), _tail(o.get("userList")))
                                                                            for o in r.get("ruleOperands") or []]
                                for r in (raw.get("logicalUserList") or {}).get("rules") or []}})
    out.sort(key=lambda r: (r["customer_list"], r["name"].lower()))
    return {"platform": "google", "rows": out, "size_note": SIZE_NOTE, "customer_note": NO_CUSTOMER_DATA,
            "fetched_at": d.get("fetched_at"), "errors": {k: v for k, v in (d.get("errors") or {}).items() if k == "user_lists"}}


COMBINE_KEY = {v: k for k, v in COMBINE.items()}


# ---------- draft checks ----------

def _rule_problems(rules, what, problems):
    if not isinstance(rules, list):
        problems.append(f"{what}: give a list of rules like {{\"op\": \"contains\", \"value\": \"/egypt\"}}")
        return
    for i, r in enumerate(rules, 1):
        if not isinstance(r, dict) or r.get("op") not in URL_OPS:
            problems.append(f"{what} {i}: op must be one of {', '.join(URL_OPS)}")
        elif not str(r.get("value") or "").strip():
            problems.append(f"{what} {i}: the URL text is empty")
        elif len(str(r["value"])) > 1024:
            problems.append(f"{what} {i}: URL text is too long")


def _names(c):
    return [n for k in COMBINE for n in (c.get(k) or [])]


def check(c):
    """-> (problems that block, warnings). Static checks; Google's own check runs in the live preview."""
    problems, warnings = [], []
    if any(c.get(k) for k in CUSTOMER_KEYS):
        return [NO_CUSTOMER_DATA], []
    action = c.get("action") or "create"
    if action not in ACTIONS:
        return [f"action must be one of {', '.join(ACTIONS)}"], []
    if action == "create":
        kind = c.get("kind")
        if kind not in KINDS:
            return [f"kind must be website or combined (customer lists: {NO_CUSTOMER_DATA})"], []
        name = str(c.get("name") or "").strip()
        if not name:
            problems.append("the new list needs a name")
        elif len(name) > NAME_MAX:
            problems.append(f"name: {len(name)} chars > {NAME_MAX}")
    else:
        if not str(c.get("list") or "").strip():
            problems.append("list: name the audience list")
        kind = None
    if action == "remove":
        return problems, warnings
    if action == "edit" and not any(c.get(k) not in (None, "", []) for k in ("new_name", "description", "lifespan", "rules", "exclude", "match")
                                    + tuple(COMBINE)):
        problems.append("nothing to change: give new_name, description, lifespan, rules or all / any / none")
    if c.get("new_name") is not None and len(str(c["new_name"])) > NAME_MAX:
        problems.append(f"new_name: {len(str(c['new_name']))} chars > {NAME_MAX}")
    website = kind == "website" or (action == "edit" and any(c.get(k) not in (None, [], "") for k in ("rules", "exclude", "match")))
    combined = kind == "combined" or (action == "edit" and any(c.get(k) for k in COMBINE))
    if website and combined:
        problems.append("a list is either a website list (rules) or a combined list (all / any / none), not both")
    if c.get("lifespan") not in (None, ""):
        try:
            n = int(c["lifespan"])
            if not 1 <= n <= LIFESPAN_MAX:
                problems.append(f"lifespan must be 1-{LIFESPAN_MAX} days")
        except (TypeError, ValueError):
            problems.append(f"lifespan must be a whole number of days (1-{LIFESPAN_MAX})")
    elif action == "create" and kind == "website":
        problems.append(f"lifespan: how many days people stay on the list (1-{LIFESPAN_MAX})")
    if website:
        if c.get("match") not in (None, "", "any", "all"):
            problems.append("match must be any (one rule is enough) or all (every rule)")
        if action == "create" or c.get("rules") is not None:
            if not c.get("rules") and not c.get("all_visitors"):
                problems.append("rules: give at least one URL rule (or all_visitors: true)")
            _rule_problems(c.get("rules") or [], "rule", problems)
        if c.get("exclude"):
            _rule_problems(c["exclude"], "exclude rule", problems)
        if c.get("prepopulate"):
            warnings.append("prepopulate: Google fills the list with people from up to the last 30 days, for Display only; "
                            "Search starts from the day the list is made")
        if action == "create" and str(c.get("match")) == "all" and len(c.get("rules") or []) > 1:
            warnings.append("match all: a person must have visited pages matching every rule (one page view each), which can "
                            "make the list small")
    if combined:
        names = _names(c)
        if action == "create" and not (c.get("all") or c.get("any")):
            problems.append("combined list: name the lists people must be on (all / any); none = lists to leave out")
        if action == "create" and len(names) < 2:
            problems.append("combined list: use at least two lists")
        if len(set(names)) != len(names):
            problems.append("combined list: the same list is named twice")
        if any(not isinstance(n, str) or not n.strip() for n in names):
            problems.append("combined list: list names must be text")
    return problems, warnings


def summary(c):
    action = c.get("action") or "create"
    if action == "remove":
        return f'Remove audience list "{c.get("list")}"'
    if action == "edit":
        bits = []
        if c.get("new_name"):
            bits.append(f'rename to "{c["new_name"]}"')
        if c.get("lifespan"):
            bits.append(f"{int(c['lifespan'])} days")
        if c.get("rules") is not None or c.get("exclude") is not None:
            bits.append("new URL rules")
        if any(c.get(k) for k in COMBINE):
            bits.append("new combination")
        if c.get("description") is not None:
            bits.append("new description")
        return f'Edit audience list "{c.get("list")}": ' + ", ".join(bits)
    if c.get("kind") == "combined":
        parts = [f"{COMBINE_LABEL[COMBINE[k]]} " + ", ".join(c[k]) for k in COMBINE if c.get(k)]
        return f'New combined list "{c.get("name")}": ' + "; ".join(parts)
    rules = "all visitors" if c.get("all_visitors") and not c.get("rules") else \
        (" and " if c.get("match") == "all" else " or ").join(f'URL {r.get("op", "").replace("_", " ")} "{r.get("value")}"' for r in c.get("rules") or [])
    exc = "; not " + " or ".join(f'URL {r.get("op", "").replace("_", " ")} "{r.get("value")}"' for r in c["exclude"]) if c.get("exclude") else ""
    return f'New website list "{c.get("name")}" ({c.get("lifespan")} days): {rules}{exc}'


def refusals(acct, changes):
    """Draft-time refusal: removing a list that something uses (from the cached account data). The push checks again live."""
    out = []
    rm = [c for c in changes if c.get("type") == TYPE and c.get("action") == "remove"]
    if not rm:
        return out
    import gapi  # noqa: PLC0415
    try:
        d = gapi.base(acct)
    except Exception:  # noqa: BLE001 - no cached account data (Editor-only account, offline): the push checks it live
        return out
    used = usage(d)
    by_name = {}
    for u in d["rows"].get("user_lists", []):
        by_name.setdefault(u["list"], []).append(str(u["id"]))
    for c in rm:
        for lid in by_name.get(c.get("list"), []):
            if used.get(lid):
                out.append(f'audience list "{c["list"]}" is in use, so it can\'t be removed. Used by: ' + "; ".join(used[lid]) +
                           ". Detach it there first")
        for u in d["rows"].get("user_lists", []):
            if u["list"] == c.get("list") and (u.get("_raw") or {}).get("type") == "CRM_BASED":
                out.append(f'audience list "{c["list"]}": {NO_CUSTOMER_DATA}')
    return out


# ---------- live: operations ----------

def _flexible(c, lifespan):
    def operand(rules):
        return {"rule": {"ruleItemGroups": [{"ruleItems": [{"name": "url__", "stringRuleItem": {"operator": URL_OPS[r["op"]], "value": str(r["value"])}}
                                                            for r in rules]}]},
                "lookbackWindowDays": str(lifespan)}
    inc = c.get("rules") or []
    if not inc:  # all visitors: one empty rule (how Google itself stores "all visitors")
        operands = [{"rule": {}, "lookbackWindowDays": str(lifespan)}]
        op = "OR"
    elif c.get("match") == "all":
        operands, op = [operand([r]) for r in inc], "AND"
    else:
        operands, op = [operand([r]) for r in inc], "OR"
    return {"inclusiveRuleOperator": op, "inclusiveOperands": operands,
            "exclusiveOperands": [operand([r]) for r in c.get("exclude") or []]}


def _logical(ids, c, refs):
    rules = []
    for k in COMBINE:
        if c.get(k):
            rules.append({"operator": COMBINE[k], "ruleOperands": [{"userList": refs(n)} for n in c[k]]})
    return {"rules": rules}


def _q(name):
    return str(name).replace("\\", "\\\\").replace("'", "\\'")


def _live_list(ids, name):
    """The live list by name (all fields Studio edits). Raises ValueError when missing or ambiguous."""
    import live  # noqa: PLC0415
    rows = live.query(ids.acct, f"SELECT {F_LIST} FROM user_list WHERE user_list.name = '{_q(name)}'")
    if not rows:
        raise ValueError(f'audience list "{name}" not found in Google Ads')
    if len(rows) > 1:
        raise ValueError(f'audience list "{name}": ambiguous in Google Ads')
    return rows[0]["userList"]


def live_users(ids, res):
    """Who uses the list right now (read live): campaigns, ad groups and combined lists."""
    import live  # noqa: PLC0415
    a = ids.acct
    out = []
    for r in live.query(a, "SELECT campaign.name, campaign_criterion.negative FROM campaign_criterion WHERE "
                           f"campaign_criterion.user_list.user_list = '{res}' AND campaign_criterion.status != REMOVED"):
        out.append(f'campaign "{r["campaign"]["name"]}"' + (" (excluded)" if r["campaignCriterion"].get("negative") else ""))
    for r in live.query(a, "SELECT campaign.name, ad_group.name, ad_group_criterion.negative FROM ad_group_criterion WHERE "
                           f"ad_group_criterion.user_list.user_list = '{res}' AND ad_group_criterion.status != REMOVED"):
        out.append(f'ad group "{r["campaign"]["name"]} / {r["adGroup"]["name"]}"' + (" (excluded)" if r["adGroupCriterion"].get("negative") else ""))
    for r in live.query(a, "SELECT user_list.name, user_list.logical_user_list.rules FROM user_list WHERE user_list.type = LOGICAL"):
        u = r["userList"]
        if any(o.get("userList") == res for x in (u.get("logicalUserList") or {}).get("rules") or [] for o in x.get("ruleOperands") or []):
            out.append(f'combined list "{u["name"]}"')
    return sorted(set(out))


def ops(ids, c):
    """Draft change -> live operations (see live.ops_for for the op dict shape)."""
    import live  # noqa: PLC0415
    a = ids.acct
    action = c.get("action") or "create"
    if action == "create":
        name = c["name"].strip()
        if ("user_list", name) in ids.new:
            raise ValueError(f'audience list "{name}" is made twice in this draft')
        if live.query(a, f"SELECT user_list.id FROM user_list WHERE user_list.name = '{_q(name)}'"):
            raise ValueError(f'an audience list called "{name}" already exists')
        res = ids.tmp("userLists")
        body = {"resourceName": res, "name": name, "membershipStatus": "OPEN"}
        if c.get("description"):
            body["description"] = c["description"]
        if c["kind"] == "website":
            days = int(c["lifespan"])
            body["membershipLifeSpan"] = str(days)
            body["ruleBasedUserList"] = {"flexibleRuleUserList": _flexible(c, days)}
            if c.get("prepopulate"):  # Google refuses NONE on create: left out = not prepopulated
                body["ruleBasedUserList"]["prepopulationStatus"] = "REQUESTED"
            line = f'new website list "{name}" ({days} days{", prepopulated" if c.get("prepopulate") else ""})'
        else:
            if c.get("lifespan"):
                body["membershipLifeSpan"] = str(int(c["lifespan"]))
            body["logicalUserList"] = _logical(ids, c, lambda n: f"customers/{a}/userLists/{ids.user_list(n)}")
            line = f'new combined list "{name}"'
        ids.new[("user_list", name)] = _tail(res)
        op = live._create("userListOperation", body, line)
        op["undo_note"] = "undo removes the list again (people collected so far are lost)"
        return [op]
    u = _live_list(ids, c["list"])
    res = u["resourceName"]
    if action == "remove":
        if u.get("type") == "CRM_BASED":
            raise ValueError(f'audience list "{c["list"]}": {NO_CUSTOMER_DATA}')
        if u.get("readOnly"):
            raise ValueError(f'audience list "{c["list"]}" is read-only in Google Ads')
        users = live_users(ids, res)
        if users:
            raise ValueError(f'audience list "{c["list"]}" is in use, so it can\'t be removed. Used by: ' + "; ".join(users)
                             + ". Detach it there first")
        op = live._remove("userListOperation", res, f'remove audience list "{c["list"]}"')
        op["undo_note"] = "a removed list can't be brought back (its members are gone)"
        return [op]
    ok, why = editable(u)
    if not ok:
        raise ValueError(f'audience list "{c["list"]}": {why}')
    new, old, mask = {}, {}, []
    if c.get("new_name") and c["new_name"] != u["name"]:
        new["name"], old["name"] = c["new_name"].strip(), u["name"]
        mask.append("name")
    if c.get("description") is not None and c["description"] != u.get("description", ""):
        new["description"], old["description"] = c["description"], u.get("description", "")
        mask.append("description")
    if c.get("lifespan") and str(int(c["lifespan"])) != str(u.get("membershipLifeSpan")):
        new["membershipLifeSpan"], old["membershipLifeSpan"] = str(int(c["lifespan"])), str(u.get("membershipLifeSpan"))
        mask.append("membershipLifeSpan")
    website = (u.get("ruleBasedUserList") or {}).get("flexibleRuleUserList")
    if any(c.get(k) is not None for k in ("rules", "exclude", "match")):
        if not website:
            raise ValueError(f'"{c["list"]}" is not a website list: change its combination (all / any / none) instead')
        cur = rows_from_flexible(website)
        merged = {"rules": c["rules"] if c.get("rules") is not None else cur["rules"],
                  "exclude": c["exclude"] if c.get("exclude") is not None else cur["exclude"],
                  "match": c.get("match") or cur["match"]}
        if any(r["op"] not in URL_OPS for r in merged["rules"] + merged["exclude"]):
            raise ValueError(f'"{c["list"]}" has rules Studio can\'t rebuild (not URL rules): edit them in Google Ads')
        days = int(c.get("lifespan") or u.get("membershipLifeSpan") or 30)
        new["ruleBasedUserList"] = {"flexibleRuleUserList": _flexible(merged, days)}
        old["ruleBasedUserList"] = {"flexibleRuleUserList": website}
        old["ruleBasedUserList"]["flexibleRuleUserList"] = {k: website.get(k, [] if k != "inclusiveRuleOperator" else "OR") for k in FLEX_FIELDS}
        mask += [f"ruleBasedUserList.flexibleRuleUserList.{k}" for k in FLEX_FIELDS]  # Google wants the leaf fields
    if any(c.get(k) for k in COMBINE):
        if u.get("type") != "LOGICAL":
            raise ValueError(f'"{c["list"]}" is not a combined list: change its URL rules instead')
        new["logicalUserList"] = _logical(ids, c, lambda n: f"customers/{a}/userLists/{ids.user_list(n)}")
        old["logicalUserList"] = u.get("logicalUserList") or {}
        mask.append("logicalUserList.rules")
    if not mask:
        raise ValueError(f'audience list "{c["list"]}" already looks like this')
    before = ", ".join(f"{k} {json.dumps(old[k])[:60]}" for k in old if k in ("name", "membershipLifeSpan"))
    op = live._upd("userListOperation", res, new, ",".join(mask), old, f'audience list "{c["list"]}"' + (f" ({before})" if before else ""),
                   "; ".join(_change_words(mask, new)))
    op["spend"] = "a longer membership or wider rules can put the list's campaigns in front of more people" \
        if _wider(u, new) else None
    return [op]


def rows_from_flexible(f):
    inc = _rule_items(f.get("inclusiveOperands"))
    return {"rules": [x for x in inc if x["op"] != "all visitors"], "exclude": _rule_items(f.get("exclusiveOperands")),
            "match": "all" if f.get("inclusiveRuleOperator") == "AND" else "any"}


def _change_words(mask, new):
    out = []
    for m in mask:
        if m == "name":
            out.append(f'name "{new["name"]}"')
        elif m == "membershipLifeSpan":
            out.append(f'{new["membershipLifeSpan"]} days')
        elif m == "description":
            out.append("description")
        elif m.startswith("ruleBasedUserList") and "rules: " not in " ".join(out):
            out.append("rules: " + describe(new))
        elif m.startswith("logicalUserList"):
            out.append("combination")
    return out


def _wider(u, new):
    try:
        if "membershipLifeSpan" in new and int(new["membershipLifeSpan"]) > int(u.get("membershipLifeSpan") or 0):
            return True
    except ValueError:
        pass
    return "ruleBasedUserList" in new or "logicalUserList" in new

