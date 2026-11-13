#!/usr/bin/env python3
"""gae - Google Ads Editor bridge for Claude.

Read side: reads Google Ads Editor's local SQLite database (read-only, never
written) so Claude sees the account exactly as Editor has it.

Write side: Claude writes a plan (JSON), `gae build` turns it into an Editor
import file. You import it in Editor (Account > Import > From file...), review
the pending changes there, and press Post yourself. Nothing goes live without you.

History: every built plan is logged per campaign; `gae history` merges that log
with Google's own change history (what was actually posted, by whom, from where).

Usage:
  gae accounts
  gae campaigns [--all]
  gae show "<campaign>"                 campaign detail: settings, ad groups, keywords, ads, negatives
  gae search <text>                     find campaigns/ad groups/keywords/ads containing text
  gae geo <name>                        look up a location ID for targeting
  gae pending                           unposted local changes sitting in Editor
  gae build <plan.json> [--dry-run --out DIR]   plan -> Editor import file + changelog entry
  gae apply <plan.json> [--restart] [--replace PLAN_ID]
                                        write the plan into Editor as pending changes; --restart closes
                                        a running Editor, writes, and reopens it
  gae discard <plan-id>... [--restart]  remove plans' pending, never-posted rows from Editor
  gae history ["<campaign>"] [--days N] what Claude proposed + what was posted live
  Global: --account ml|mluk|<customer id>   (default ml)
"""
import argparse, csv, datetime as dt, json, os, re, sqlite3, sys
from collections import Counter, defaultdict
from pathlib import Path

import appconfig  # noqa: E402

HUB = appconfig.DATA  # Studio keeps everything gae writes in its own data folder
EDITOR_DIR = Path(os.environ.get("GAE_EDITOR_DIR", Path.home() / "Library/Application Support/Google/Google-AdWords-Editor"))
OUT_DIR = appconfig.DATA
CHANGELOG = OUT_DIR / "changelog.jsonl"
ACCOUNTS = {"ml": "8931136258", "mluk": "4387091070"}

# Editor's internal enum codes, verified against the Google Ads API by joining IDs (2026-10-08)
STATUS = {0: "Enabled", 1: "Paused", 2: "Removed"}
CAMPAIGN_TYPE = {0: "Search", 2: "Performance Max", 3: "Display", 16: "Demand Gen"}
MATCH = {0: "Broad", 1: "Phrase", 2: "Exact"}
BIDDING = {1: "Manual CPC", 9: "Maximize clicks", 10: "Maximize conversions", 11: "Maximize conversion value",
           12: "Maximize conversions", 13: "Maximize conversion value", 15: "Target impression share"}
PARENT_CAMPAIGN, PARENT_ADGROUP = 2, 4  # high 32 bits of parentId
LOW32 = 0xFFFFFFFF


# ---------- reading Editor's database ----------

def editor_version_dir():
    dirs = [d for d in EDITOR_DIR.iterdir() if d.name.isdigit() and (d / "ape.db").exists()] if EDITOR_DIR.exists() else []
    if not dirs:
        sys.exit("Google Ads Editor data not found. Is Editor installed and has it downloaded an account?")
    return max(dirs, key=lambda d: int(d.name))


def ro(path):
    # immutable=1: read the file as-is, never take locks or write - safe while Editor is open
    return sqlite3.connect(f"file:{path}?immutable=1", uri=True)


def accounts():
    db = ro(editor_version_dir() / "ape.db")
    rows = db.execute("select remoteId, description, lastDownload from Account").fetchall()
    out = []
    for rid, desc, last in rows:
        f = editor_version_dir() / f"ape_{rid}.db"
        if f.exists():
            out.append({"id": str(rid), "name": desc, "last_download": dt.datetime.fromtimestamp(last / 1e6) if last else None})
    return out


def resolve_account(a):
    return ACCOUNTS.get(a.lower(), a.replace("-", ""))


def open_account(acct):
    f = editor_version_dir() / f"ape_{acct}.db"
    if not f.exists():
        sys.exit(f"Account {acct} is not downloaded in Editor. Run `gae accounts` to see which are.")
    info = next((a for a in accounts() if a["id"] == acct), None)
    if info and info["last_download"]:
        age = (dt.datetime.now() - info["last_download"]).days
        if age >= 2:
            print(f"! Editor data for {info['name']} is {age} days old (last download {info['last_download']:%Y-%m-%d %H:%M}).\n"
                  f"  In Editor: Get recent changes, then re-run for a current view.\n", file=sys.stderr)
    db = ro(f)
    db.row_factory = sqlite3.Row
    return db


def money(micros):
    return f"{micros / 1e6:,.2f}" if micros else "-"


def find_campaigns(db, needle):
    rows = db.execute("select * from Campaign where status != 2").fetchall()
    exact = [r for r in rows if r["name"].lower() == needle.lower()]
    return exact or [r for r in rows if needle.lower() in r["name"].lower()]


def one_campaign(db, needle):
    hits = find_campaigns(db, needle)
    if not hits:
        sys.exit(f'No campaign matches "{needle}".')
    if len(hits) > 1:
        print(f'"{needle}" matches {len(hits)} campaigns - be more specific:')
        for r in hits:
            print("  ", r["name"])
        sys.exit(1)
    return hits[0]


def table(rows, headers):
    if not rows:
        print("  (none)")
        return
    w = [min(max(len(str(h)), *(len(str(r[i])) for r in rows)), 60) for i, h in enumerate(headers)]
    print("  " + "  ".join(str(h).ljust(w[i]) for i, h in enumerate(headers)))
    for r in rows:
        print("  " + "  ".join(str(r[i])[:60].ljust(w[i]) for i in range(len(headers))))


# ---------- read commands ----------

def cmd_accounts(args):
    table([(a["id"], a["name"], f"{a['last_download']:%Y-%m-%d %H:%M}" if a["last_download"] else "never")
           for a in accounts()], ["customer id", "name", "last download"])


def cmd_campaigns(args):
    db = open_account(args.acct)
    ag = Counter(r[0] for r in db.execute("select parentId & ? from AdGroup where status != 2", (LOW32,)))
    rows = []
    for c in db.execute("select * from Campaign order by status, name"):
        if not args.all and c["status"] == 2:
            continue
        rows.append((STATUS.get(c["status"], c["status"]), CAMPAIGN_TYPE.get(c["campaignType"], c["campaignType"]),
                     money(c["budgetAmount"]), BIDDING.get(c["biddingStrategyType"], c["biddingStrategyType"]),
                     ag.get(c["localId"], 0), "local edit" if c["state"] else "", c["name"]))
    table(rows, ["status", "type", "budget/day", "bidding", "ad groups", "editor", "campaign"])


def cmd_show(args):
    db = open_account(args.acct)
    c = one_campaign(db, args.campaign)
    cid = c["localId"]
    print(f"\n{c['name']}  (id {c['remoteId']})")
    print(f"  status {STATUS.get(c['status'])} | type {CAMPAIGN_TYPE.get(c['campaignType'], c['campaignType'])} | "
          f"budget {money(c['budgetAmount'])}/day | bidding {BIDDING.get(c['biddingStrategyType'], c['biddingStrategyType'])}"
          + (f" | tCPA {money(c['maxCpa'])}" if c["maxCpa"] else ""))
    nets = [n for n, k in (("Google search", "includeGoogleSearch"), ("Search partners", "includeSearchPartners"),
                           ("Display", "includeDisplayNetwork")) if c[k]]
    print(f"  networks {', '.join(nets) or '-'} | languages {c['languages'] or '-'}")
    geo = dict(db.execute("select remoteId, locationShortName from GeoLocation"))
    for label, t in (("locations", "GeoTarget"), ("excluded", "GeoTargetNegative")):
        ids = [r[0] for r in db.execute(f"select locationId from {t} where parentId >> 32 = ? and (parentId & ?) = ?",
                                        (PARENT_CAMPAIGN, LOW32, cid))]
        names = sorted(geo.get(i, str(i)) for i in ids)
        if names:
            print(f"  {label} ({len(names)}): {', '.join(names[:30])}" + (" ..." if len(names) > 30 else ""))
    if c["comment"]:
        print(f"  editor comment: {c['comment']}")

    groups = db.execute("select * from AdGroup where parentId >> 32 = ? and (parentId & ?) = ? and status != 2 order by name",
                        (PARENT_CAMPAIGN, LOW32, cid)).fetchall()
    for g in groups:
        print(f"\n  Ad group: {g['name']}  [{STATUS.get(g['status'])}]")
        kws = db.execute("select * from Keyword where parentId >> 32 = ? and (parentId & ?) = ? and status != 2 order by text",
                         (PARENT_ADGROUP, LOW32, g["localId"])).fetchall()
        table([(STATUS.get(k["status"]), MATCH.get(k["criterionType"]), money(k["maxCpc"]), k["qualityScore"] or "-", k["text"])
               for k in kws], ["status", "match", "max cpc", "QS", "keyword"])
        for a in db.execute("select * from ResponsiveSearchAd where parentId >> 32 = ? and (parentId & ?) = ? and status != 2",
                            (PARENT_ADGROUP, LOW32, g["localId"])):
            hl = [a[f"headline{i}"] for i in range(1, 16) if a[f"headline{i}"]]
            ds = [a[f"description{i}"] for i in range(1, 5) if a[f"description{i}"]]
            print(f"    RSA [{STATUS.get(a['status'])}] {len(hl)} headlines, {len(ds)} descriptions")
            print("      H: " + " | ".join(hl))
            print("      D: " + " | ".join(ds))
        negs = db.execute("select text, criterionType from KeywordNegative where parentId >> 32 = ? and (parentId & ?) = ?",
                          (PARENT_ADGROUP, LOW32, g["localId"])).fetchall()
        if negs:
            print(f"    ad group negatives ({len(negs)}): " + ", ".join(f"{t} ({MATCH.get(m)})" for t, m in negs[:25])
                  + (" ..." if len(negs) > 25 else ""))

    negs = db.execute("select text, criterionType from KeywordNegative where parentId >> 32 = ? and (parentId & ?) = ?",
                      (PARENT_CAMPAIGN, LOW32, cid)).fetchall()
    print(f"\n  Campaign negatives ({len(negs)}): " + ", ".join(f"{t} ({MATCH.get(m)})" for t, m in negs[:40])
          + (" ..." if len(negs) > 40 else ""))
    lists = db.execute("select l.name from NegativeKeywordListEdge e join NegativeKeywordList l on l.localId = e.sourceId "
                       "where e.parentId >> 32 = ? and (e.parentId & ?) = ?", (PARENT_CAMPAIGN, LOW32, cid)).fetchall()
    print(f"  Shared negative lists: {', '.join(r[0] for r in lists) or '-'}")


def cmd_search(args):
    db = open_account(args.acct)
    n = f"%{args.text}%"
    camps = {r["localId"]: r["name"] for r in db.execute("select localId, name from Campaign")}
    groups = {r["localId"]: (r["name"], camps.get(r["parentId"] & LOW32, "?")) for r in db.execute("select localId, name, parentId from AdGroup")}
    print("Campaigns:")
    table([(STATUS.get(r["status"]), r["name"]) for r in db.execute("select * from Campaign where name like ?", (n,))], ["status", "campaign"])
    print("Ad groups:")
    table([(STATUS.get(r["status"]), camps.get(r["parentId"] & LOW32, "?"), r["name"])
           for r in db.execute("select * from AdGroup where name like ?", (n,))], ["status", "campaign", "ad group"])
    print("Keywords:")
    table([(STATUS.get(r["status"]), MATCH.get(r["criterionType"]), r["text"], *groups.get(r["parentId"] & LOW32, ("?", "?"))[::-1])
           for r in db.execute("select * from Keyword where text like ? and status != 2", (n,))], ["status", "match", "keyword", "campaign", "ad group"])
    print("Negatives:")
    table([(MATCH.get(r["criterionType"]), r["text"],
            camps.get(r["parentId"] & LOW32, "?") if r["parentId"] >> 32 == PARENT_CAMPAIGN else " / ".join(groups.get(r["parentId"] & LOW32, ("?", "?"))[::-1]))
           for r in db.execute("select * from KeywordNegative where text like ?", (n,))], ["match", "negative", "where"])
    cols = " or ".join([f"headline{i} like :n" for i in range(1, 16)] + [f"description{i} like :n" for i in range(1, 5)])
    print("Ads (RSA):")
    table([(STATUS.get(r["status"]), *groups.get(r["parentId"] & LOW32, ("?", "?"))[::-1])
           for r in db.execute(f"select * from ResponsiveSearchAd where {cols}", {"n": n})], ["status", "campaign", "ad group"])


def cmd_geo(args):
    db = open_account(args.acct)
    table([(r["remoteId"], r["locationPath"], r["countryCode"], f"{r['reach'] or 0:,}")
           for r in db.execute("select * from GeoLocation where locationPath like ? order by reach desc limit 25", (f"%{args.name}%",))],
          ["location id", "location", "country", "reach"])


PENDING_TABLES = {"Campaign": "name", "AdGroup": "name", "Keyword": "text", "KeywordNegative": "text",
                  "ResponsiveSearchAd": "headline1", "SitelinkV2": "displayText", "CalloutV2": "displayText",
                  "NegativeKeywordList": "name", "Budget": "name", "Label": "name", "GeoTarget": "locationId",
                  "PriceExtensionV2": "header1", "StructuredSnippetV2": "header", "Audience": "userListId"}


def cmd_pending(args):
    db = open_account(args.acct)
    total = 0
    for t, col in PENDING_TABLES.items():
        try:
            rows = db.execute(f"select state, {col} from {t} where state != 0").fetchall()
        except sqlite3.OperationalError:
            continue
        if rows:
            total += len(rows)
            print(f"{t}: {len(rows)} unposted")
            for s, label in rows[:20]:
                print(f"   (state {s}) {label}")
    print("No unposted changes in Editor." if not total else f"\n{total} unposted change(s) in Editor - review and Post there.")


# ---------- write side: plan -> Editor import file ----------
# Column names and enum values are Editor's own (pulled from the Editor binary's
# validation messages, e.g. "expects exactly: 'Negative Broad', 'Negative Phrase', or 'Negative Exact'").

def _status(v, default=None):
    if v is None:
        return default
    return {"enabled": "Enabled", "paused": "Paused", "removed": "Removed"}[str(v).lower()]


def _match(v, negative=False):
    m = {"broad": "Broad", "phrase": "Phrase", "exact": "Exact"}[str(v).lower()]
    return f"Negative {m}" if negative else m


def _amt(v):
    return f"{float(v):.2f}" if v is not None else None


def plan_rows(change):
    """One plan change -> list of Editor rows (dicts). Unknown Editor columns can be passed via `columns`."""
    t = change["type"]
    if change.get("edit") and t == "rsa" and change.get("headlines"):
        r = {"Campaign": change["campaign"]}  # lint the new text
        for i, h in enumerate(change["headlines"], 1):
            r[f"Headline {i}"] = h["text"] if isinstance(h, dict) else h
        for i, d in enumerate(change["descriptions"], 1):
            r[f"Description {i}"] = d["text"] if isinstance(d, dict) else d
        return [r]
    if t == "dsa_exclusion" or (t in ("callout", "sitelink") and change.get("remove")):
        return []  # direct-write only (gae apply)
    if change.get("edit") and t in ("rsa", "sitelink", "price"):
        return []  # final URL edits of existing rows: direct-write only (gae apply)
    base = {"Campaign": change["campaign"]}
    rows = []
    if t == "campaign":
        r = dict(base)
        r.update({"Campaign Type": change.get("campaign_type", "Search") if change.get("new") else None,
                  "Campaign Status": _status(change.get("status"), "Paused" if change.get("new") else None),
                  "Budget": _amt(change.get("budget")),
                  "Budget type": "Daily" if change.get("budget") is not None else None,
                  "Bid Strategy Type": change.get("bid_strategy"),
                  "Target CPA": _amt(change.get("target_cpa")),
                  "Networks": change.get("networks"),
                  "Languages": change.get("languages"),
                  "Start Date": change.get("start_date"),
                  "End Date": change.get("end_date"),
                  "Ad Schedule": change.get("ad_schedule")})
        rows.append(r)
        for loc in change.get("locations", []):
            rows.append({**base, "Location ID": str(loc["id"]), "Location": loc.get("name")})
        for loc in change.get("excluded_locations", []):
            rows.append({**base, "Location ID": str(loc["id"]), "Location": loc.get("name"), "Status": "Excluded"})
    elif t == "ad_group":
        rows.append({**base, "Ad Group": change["ad_group"], "Ad Group Status": _status(change.get("status"), "Enabled" if change.get("new") else None),
                     "Ad Group Type": "Standard" if change.get("new") else None, "Max CPC": _amt(change.get("max_cpc"))})
    elif t == "keyword":
        for kw in change.get("keywords", [change.get("keyword")]):
            rows.append({**base, "Ad Group": change["ad_group"], "Keyword": kw, "Criterion Type": _match(change["match"]),
                         "Status": _status(change.get("status")), "Max CPC": _amt(change.get("max_cpc")),
                         "Final URL": change.get("final_url")})
    elif t == "negative":
        for kw in change.get("keywords", [change.get("keyword")]):
            rows.append({**base, "Ad Group": change.get("ad_group"), "Keyword": kw,
                         "Criterion Type": _match(change.get("match", "phrase"), negative=True),
                         "Status": "Removed" if change.get("remove") else None})
    elif t == "rsa":
        r = {**base, "Ad Group": change["ad_group"], "Ad type": "Responsive search ad",
             "Status": _status(change.get("status"), "Paused"), "Final URL": change["final_url"],
             "Path 1": change.get("path1"), "Path 2": change.get("path2")}
        hl, ds = change["headlines"], change["descriptions"]
        if not 3 <= len(hl) <= 15 or not 2 <= len(ds) <= 4:
            raise ValueError("RSA needs 3-15 headlines and 2-4 descriptions")
        for i, h in enumerate(hl, 1):
            if isinstance(h, dict):
                r[f"Headline {i}"] = h["text"]
                if h.get("pin"):
                    r[f"Headline {i} position"] = str(h["pin"])
            else:
                r[f"Headline {i}"] = h
        for i, d in enumerate(ds, 1):
            r[f"Description {i}"] = d
        rows.append(r)
    elif t == "sitelink":
        rows.append({**base, "Link Text": change["text"], "Description line 1": change.get("line1"),
                     "Description line 2": change.get("line2"), "Final URL": change["final_url"]})
    elif t == "callout":
        for txt in change.get("texts", [change.get("text")]):
            rows.append({**base, "Callout text": txt})
    elif t == "raw":
        rows.append(dict(base))
    elif t in ("shared_list", "audience", "excluded_location", "location"):
        return []  # direct-write only (gae apply); nothing to lint
    elif t == "promotion":
        rows.append({**base, "Item": change["target"]})  # lint the promotion target (max 20 in Google; Item limit 25)
    elif t == "snippet":
        rows.append({**base, "Header": c_header(change), "Snippet Values": ";".join(change["values"])})
    elif t == "price":
        r = dict(base)
        for i, it in enumerate(change["items"], 1):
            r[f"Item {i} header"] = it["header"]
            r[f"Item {i} description"] = it["description"]
        rows.append(r)
    else:
        raise ValueError(f"unknown change type: {t}")
    if change.get("columns"):
        rows[0].update(change["columns"])
    return rows


def c_header(change):
    return change["header"]


LIMITS = {"Headline": 30, "Description": 90, "Path": 15, "Link Text": 25, "Description line": 35, "Callout text": 25,
          "Snippet Values": 999, "Item": 25}


def lint(rows):
    problems = []
    for i, r in enumerate(rows, 1):
        for k, v in r.items():
            if not v:
                continue
            if "—" in v:
                problems.append(f"row {i} {k}: contains an em dash")
            for prefix, lim in LIMITS.items():
                if k.startswith(prefix) and not k.endswith("position") and len(v) > lim:
                    problems.append(f"row {i} {k}: {len(v)} chars > {lim} ({v!r})")
            if re.search(r"bahamas", v, re.I):
                problems.append(f"row {i} {k}: mentions Bahamas (ML ban)")
    return problems


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:50]


def cmd_build(args):
    plan = json.loads(Path(args.plan).read_text())
    changes = plan["changes"]
    rows = [r for c in changes for r in plan_rows(c)]
    problems = lint(rows)
    if problems:
        print("Plan has problems, nothing written:")
        for p in problems:
            print("  -", p)
        sys.exit(1)

    stamp = dt.datetime.now()
    pid = f"{stamp:%Y%m%d-%H%M}-{slug(plan['title'])}"
    for r in rows:
        r.setdefault("Comment", f"Claude {pid}")  # Editor-local note so you can spot Claude's rows in Editor

    headers = ["Campaign", "Ad Group"]
    for r in rows:
        for k, v in r.items():
            if v is not None and k not in headers:
                headers.append(k)
    out_dir = Path(args.out) if args.out else OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{pid}.csv"
    # UTF-16 + tab = the same format Editor itself exports, so its importer reads it natively
    with open(out, "w", encoding="utf-16", newline="") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(headers)
        for r in rows:
            w.writerow(["" if r.get(h) is None else r[h] for h in headers])

    summary = [summarise(c) for c in changes]
    if args.dry_run:
        print(f"(dry run - written to {out}, not logged to history)")
        for s in summary:
            print("  -", s)
        return
    entry = {"id": pid, "ts": stamp.isoformat(timespec="seconds"), "account": args.acct, "title": plan["title"],
             "why": plan.get("why"), "file": str(out.relative_to(HUB)) if out.is_relative_to(HUB) else str(out), "plan": plan, "changes": summary,
             "campaigns": sorted({c["campaign"] for c in changes})}
    with open(CHANGELOG, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    print(f"\n{plan['title']}  ({len(rows)} Editor rows)\n")
    for s in summary:
        print("  -", s)
    print(f"\nImport file: {out}")
    print("In Editor: Account > Import > From file...  -> review the changes -> Post when you're happy.")


def summarise(c):
    t, camp = c["type"], c["campaign"]
    ag = f" / {c['ad_group']}" if c.get("ad_group") else ""
    verb = "New" if c.get("new") else "Update"
    if t == "campaign":
        bits = [f"{k}={c[k]}" for k in ("status", "budget", "bid_strategy", "tis_percent", "max_cpc_cap", "target_cpa", "networks", "languages") if c.get(k) is not None]
        if c.get("locations"):
            bits.append("locations=" + ", ".join(l.get("name", str(l["id"])) for l in c["locations"]))
        return f"{verb} campaign {camp}: " + "; ".join(bits)
    if t == "ad_group":
        return f"{verb} ad group {camp}{ag}" + (f": max CPC {c['max_cpc']}" if c.get("max_cpc") else "") + (f", {c['status']}" if c.get("status") else "")
    if t == "keyword" and c.get("edit"):
        bits = [f"max CPC -> {c['max_cpc']}"] if c.get("max_cpc") is not None else []
        bits += [f"status -> {c['status']}"] if c.get("status") else []
        kws = c.get("keywords", [c.get("keyword")])
        return f"Keywords ({c['match']}) in {camp}{ag}: {', '.join(kws)}: " + ", ".join(bits)
    if t == "keyword":
        kws = c.get("keywords", [c.get("keyword")])
        return f"Keywords ({c['match']}) in {camp}{ag}: " + ", ".join(kws) + (f" -> {c['status']}" if c.get("status") else "")
    if t == "negative":
        kws = c.get("keywords", [c.get("keyword")])
        return f"{'Remove' if c.get('remove') else 'Add'} negatives ({c.get('match', 'phrase')}) on {camp}{ag}: " + ", ".join(kws)
    if t in ("rsa", "sitelink") and c.get("edit"):
        what = f"RSAs in {camp}{ag}" if t == "rsa" else f"Sitelink '{c['text']}' on {camp}"
        bits = ([f"final URL -> {c['final_url']}"] if c.get("final_url") else []) + \
               ([f"status -> {c['status']}"] if c.get("status") else []) + \
               ([f"text rewritten ({len(c['headlines'])} headlines)"] if c.get("headlines") else []) + \
               ([f"text -> '{c['new_text']}'"] if c.get("new_text") else [])
        return f"{what}: " + ", ".join(bits)
    if t == "rsa":
        return f"RSA in {camp}{ag}: {len(c['headlines'])} headlines -> {c['final_url']}"
    if t == "sitelink" and c.get("remove"):
        return f"Remove sitelinks on {camp}: " + ", ".join(c.get("texts", [c.get("text")]))
    if t == "sitelink":
        return f"Sitelink on {camp}: {c['text']} -> {c['final_url']}"
    if t == "snippet":
        return f"Structured snippet on {camp}: {c['header']}: " + ", ".join(c["values"])
    if t == "price" and c.get("edit"):
        return f"Price asset on {camp}: " + ", ".join(f"{h} -> {v}" for h, v in c["prices"].items())
    if t == "price":
        return f"Price asset on {camp}: " + ", ".join(f"{i['header']} {c.get('qualifier', 'From')} {i['price']}" for i in c["items"])
    if t == "excluded_location":
        return f"Exclude locations on {camp}: " + ", ".join(l.get("name", str(l["id"])) for l in c["locations"])
    if t == "shared_list":
        return f"Attach shared negative lists to {camp}: " + ", ".join(c["lists"])
    if t == "audience" and c.get("edit"):
        return f"Audience bid adjustment on {camp}: " + ", ".join(c["lists"]) + f" -> {c['bid_modifier']:+d}%"
    if t == "audience":
        return f"Audiences (observation) on {camp}: " + ", ".join(c["lists"])
    if t == "location":
        return f"Add locations on {camp}: " + ", ".join(l.get("name", str(l["id"])) for l in c["locations"])
    if t == "promotion":
        mod = "Up to " if c.get("up_to") else ""
        return f"Promotion on {camp}: {mod}{c['percent']}% off {c['target']} -> {c['final_url']}"
    if t == "callout":
        return f"{'Remove callouts' if c.get('remove') else 'Callouts'} on {camp}: " + ", ".join(c.get("texts", [c.get("text")]))
    if t == "dsa_exclusion":
        return f"DSA page exclusions (URL contains) on {camp}: " + ", ".join(c["url_contains"])
    return f"{t} on {camp}{ag}: {c.get('columns')}"


# ---------- write side: apply a plan straight into Editor's database ----------
# Rows are cloned from templates Editor itself wrote (templates_<schema>.json) and saved as
# state=2 ("new, not posted"), exactly like an import. Editor must be closed; a backup is
# taken first; nothing reaches Google until Mathias reviews in Editor and presses Post.

import shutil, subprocess, time

SCHEMA = 749
STATE_NEW = 2
TEMPLATES = Path(__file__).with_name(f"templates_{SCHEMA}.json")
BACKUP_DIR = Path(os.environ.get("GAE_BACKUP_DIR", OUT_DIR / "backups"))
ENTITY_TABLES = ["Campaign", "AdGroup", "Keyword", "KeywordNegative", "GeoTarget", "GeoTargetNegative", "NegativeKeywordListEdge", "Audience",
                 "SitelinkV2Edge", "SitelinkV2", "CalloutV2Edge", "CalloutV2", "StructuredSnippetV2Edge", "StructuredSnippetV2",
                 "PriceExtensionV2Edge", "PriceExtensionV2", "ResponsiveSearchAd", "DynamicTargetNegative"]


def editor_running():
    if os.environ.get("GAE_EDITOR_DIR"):  # working on a copy (tests) - the real Editor is irrelevant
        return False
    # match the app binary itself, not a leftover crashpad_handler
    # the versioned app binary (launcher start, may carry extra arguments) or the app bundle binary itself
    # (macOS App Translocation start: .../Google Ads Editor.app/Contents/MacOS/Google Ads Editor)
    return subprocess.run(["pgrep", "-f", "MacOS/Google Ads Editor( |$)"], capture_output=True).returncode == 0


def quit_editor(timeout=60):
    """Ask Editor to quit normally (it saves as it goes) and wait until it is gone."""
    subprocess.run(["osascript", "-e", 'tell application "Google Ads Editor" to quit'], capture_output=True)
    for _ in range(timeout * 2):
        if not editor_running():
            return
        time.sleep(0.5)
    sys.exit("Google Ads Editor did not close (an open dialog?). Nothing written.")


def launch_editor():
    # macOS ignores `open` while the quitting app is still deregistering - retry until it is really up
    time.sleep(2)
    for _ in range(4):
        subprocess.run(["open", "-a", "Google Ads Editor"], capture_output=True)
        for _ in range(20):
            time.sleep(0.5)
            if editor_running():
                return True
    print("! Could not reopen Google Ads Editor - open it yourself.")
    return False


def open_for_write(acct, restart=False):
    """Returns (db, version dir, backup dir, relaunch?). With restart, a running Editor is quit first."""
    vdir = editor_version_dir()
    if int(vdir.name) != SCHEMA:
        sys.exit(f"Editor's data format changed ({vdir.name}, gae knows {SCHEMA}). Not writing - gae needs re-learning first.")
    relaunch = False
    if editor_running():
        if not restart:
            sys.exit("Google Ads Editor is open. Close it, or use --restart to let gae close and reopen it.")
        print("Closing Google Ads Editor ...")
        quit_editor()
        relaunch = True
    f = vdir / f"ape_{acct}.db"
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    bdir = BACKUP_DIR / stamp
    bdir.mkdir(parents=True, exist_ok=True)
    for name in (f.name, f"ape_undo_{acct}.db", "ape.db"):
        if (vdir / name).exists():
            shutil.copy2(vdir / name, bdir / name)
    db = sqlite3.connect(str(f))
    db.row_factory = sqlite3.Row
    if db.execute("pragma user_version").fetchone()[0] != SCHEMA:
        sys.exit("Unexpected database version - not writing.")
    return db, vdir, bdir, relaunch


def _tpl(name):
    raw = json.loads(TEMPLATES.read_text())[name]
    return {k: (bytes.fromhex(v["hex"]) if isinstance(v, dict) else v) for k, v in raw.items()}


def _insert(db, table, overrides):
    row = _tpl(table)
    row.pop("localId")
    # preAdvisoryState=NULL: a normal local change you can Post. The template rows came from a CSV
    # import, where Editor marks rows as "proposals" (3) that can't be posted until accepted.
    row.update(remoteId=0, state=STATE_NEW, preAdvisoryState=None)
    row.update(overrides)
    cols = list(row)
    cur = db.execute(f"insert into {table} ({','.join(cols)}) values ({','.join('?' * len(cols))})", [row[c] for c in cols])
    return cur.lastrowid  # AUTOINCREMENT keeps sqlite_sequence in step, as Editor's own import does


def _campaign_id(db, name, created):
    if name in created["campaign"]:
        return created["campaign"][name]
    rows = db.execute("select localId from Campaign where name = ? and status != 2", (name,)).fetchall()
    if len(rows) != 1:
        raise ValueError(f'campaign "{name}": {"not found" if not rows else "ambiguous"} in Editor')
    return rows[0][0]


def _adgroup_id(db, camp_id, name, created):
    key = (camp_id, name)
    if key in created["ad_group"]:
        return created["ad_group"][key]
    rows = db.execute("select localId from AdGroup where name = ? and parentId = ? and status != 2",
                      (name, (PARENT_CAMPAIGN << 32) | camp_id)).fetchall()
    if len(rows) != 1:
        raise ValueError(f'ad group "{name}": {"not found" if not rows else "ambiguous"} in Editor')
    return rows[0][0]


def _insert_clone(db, table, where, overrides):
    """Insert a new pending row cloned from an existing row of the same table (for tables without a
    stored template). Only link-style tables, whose rows are ids + status, are written this way."""
    src = db.execute(f"select * from {table} where {where} limit 1").fetchone()
    if not src:
        raise ValueError(f"no existing {table} row to clone")
    row = {k: src[k] for k in src.keys()}
    row.pop("localId")
    for k in [k for k in row if k.endswith("_revert") or k.endswith("_history")]:
        row[k] = None
    row.update(remoteId=0, state=STATE_NEW, preAdvisoryState=None, found=0, status=0)
    row.update(overrides)
    cols = list(row)
    return db.execute(f"insert into {table} ({','.join(cols)}) values ({','.join('?' * len(cols))})", [row[c] for c in cols]).lastrowid


def qstringlist(items):
    """Editor stores string lists as a Qt QStringList: uint32 count, then per item uint32 byte length + UTF-16BE."""
    out = len(items).to_bytes(4, "big")
    for it in items:
        enc = it.encode("utf-16-be")
        out += len(enc).to_bytes(4, "big") + enc
    return out


def _defaults(db, table):
    out = {}
    for _, name, typ, _, dflt, _ in db.execute(f"pragma table_info({table})"):
        if dflt is None:
            out[name] = None
        elif dflt.upper().startswith("X'"):
            out[name] = bytes.fromhex(dflt[2:-1])
        elif dflt.startswith("'"):
            out[name] = dflt.strip("'")
        else:
            out[name] = float(dflt) if "." in dflt else int(dflt)
    return out


KEEP_FROM_EXISTING = {"assetSource", "linkAssetSource", "suggestionIds", "adSchedule", "finalMobileUrls"}


def _new_row(db, table, overrides):
    """A pending row: schema defaults + the few house-keeping values existing rows of this table carry."""
    row = _defaults(db, table)
    src = db.execute(f"select * from {table} limit 1").fetchone()
    if src:
        for k in KEEP_FROM_EXISTING & set(src.keys()):
            row[k] = src[k]
    row.pop("localId", None)
    row.update(remoteId=0, state=STATE_NEW, preAdvisoryState=None, found=0, status=0)
    row.update(overrides)
    cols = list(row)
    return db.execute(f"insert into {table} ({','.join(cols)}) values ({','.join('?' * len(cols))})", [row[c] for c in cols]).lastrowid


def _ymd(d):
    return int(str(d).replace("-", "")) if d else 0


ACCOUNT_PARENT = (1 << 32) | 1  # how Editor parents account-level asset links


def _asset_with_link(db, table, fields, camp, comment):
    """Create an asset-library row and link it through <table>Edge to the campaign (camp=None: account level)."""
    aid = _new_row(db, table, {**fields, "comment": comment, "parentId": 0})
    parent = ACCOUNT_PARENT if camp is None else (PARENT_CAMPAIGN << 32) | camp
    _new_row(db, table + "Edge", {"parentId": parent, "sourceId": aid, "comment": comment})
    return aid


STATUS_CODE = {"enabled": 0, "paused": 1}
EDIT_STATUS = {"enabled": 0, "paused": 1, "removed": 2}  # status edits of existing rows (Removed = Editor's own Remove)
BID_CODE = {"manual cpc": 1, "maximize clicks": 9}  # 9 + maxCpcBidCeiling = how Editor stores [6] DSA's Max Clicks with a cap
MATCH_CODE = {"broad": 0, "phrase": 1, "exact": 2}
SUPPORTED = {"campaign", "ad_group", "keyword", "negative", "shared_list", "audience", "sitelink", "callout", "snippet", "price",
             "excluded_location", "rsa", "dsa_exclusion", "location", "promotion"}


STATE_EDITED = 1  # how Editor marks a changed existing row: new value in the field, old value in <field>_revert


def _jsonable(v):
    return {"hex": v.hex()} if isinstance(v, (bytes, bytearray)) else v


def _unjson(v):
    return bytes.fromhex(v["hex"]) if isinstance(v, dict) and "hex" in v else v


def _edit_row(db, table, local_id, fields, edits):
    """Edit an existing row the way Editor does it by hand (verified on a manual Editor edit): state=1, the new
    value in the field, the original value in <field>_revert (kept if already set). Each change is recorded in
    `edits` so `gae discard` can put the old value back."""
    row = db.execute(f"select * from {table} where localId = ?", (local_id,)).fetchone()
    changed = 0
    for field, new in fields.items():
        old = row[field]
        if old == new:
            continue
        sets = {field: new}
        if row["state"] != STATE_NEW and field + "_revert" in row.keys() and row[field + "_revert"] is None:
            sets[field + "_revert"] = old
        if row["state"] == 0:
            sets["state"] = STATE_EDITED
            sets["preAdvisoryState"] = None
        db.execute(f"update {table} set {', '.join(k + ' = ?' for k in sets)} where localId = ?", [*sets.values(), local_id])
        edits.append({"table": table, "localId": local_id, "field": field, "old": _jsonable(old), "new": _jsonable(new),
                      "state_before": row["state"]})
        row = db.execute(f"select * from {table} where localId = ?", (local_id,)).fetchone()
        changed += 1
    return changed


# Editor targetImpressionShareGoal. 1 = TOP_OF_PAGE (verified against the API on the TIS Test campaign).
# 2 = ABSOLUTE_TOP_OF_PAGE: API-verified 2026-10-24 on [3] [1] [2] [12] after posting.
TIS_LOCATION = {"top": 1, "absolute_top": 2}


def _edit_campaign(db, c, edits):
    camp = _campaign_id(db, c["campaign"], {"campaign": {}})
    row = db.execute("select * from Campaign where localId = ?", (camp,)).fetchone()
    fields = {}
    if c.get("status") is not None:
        fields["status"] = STATUS_CODE[str(c["status"]).lower()]
    if c.get("budget") is not None:
        if row["sharedBudgetLocalId"]:
            raise ValueError(f'"{c["campaign"]}" uses a shared budget - not edited directly')
        fields["budgetAmount"] = int(round(float(c["budget"]) * 1e6))
    if c.get("bid_strategy") is not None:
        if str(c["bid_strategy"]).lower() != "target impression share":
            raise ValueError("campaign edit: only bid_strategy 'Target impression share' is supported directly")
        if not c.get("max_cpc_cap") or not c.get("tis_percent"):
            raise ValueError("Target impression share needs tis_percent and max_cpc_cap")
        fields.update(biddingStrategyType=15, apiBiddingStrategyType=15,
                      targetImpressionShareGoal=TIS_LOCATION[c.get("tis_location", "top")],
                      targetImpressionShare=float(c["tis_percent"]) / 100,
                      maxCpcBidCeiling=int(round(float(c["max_cpc_cap"]) * 1e6)))
    if c.get("mobile_bid_adjustment") is not None:  # whole percent, e.g. -30 (Editor's int; "none" = -2147483647)
        fields["mobileBidModifier"] = int(c["mobile_bid_adjustment"])
    if c.get("languages") is not None:  # "all" -> '' (how Editor stores All languages), else "en" / "de;en"
        fields["languages"] = "" if str(c["languages"]).lower() == "all" else str(c["languages"])
    unknown = set(c) - {"type", "campaign", "status", "budget", "bid_strategy", "tis_location", "tis_percent", "max_cpc_cap",
                        "mobile_bid_adjustment", "languages"}
    if unknown or not fields:
        raise ValueError(f"campaign edit supports status, budget, Target impression share, mobile_bid_adjustment "
                         f"(got {sorted(unknown) or 'nothing'})")
    return _edit_row(db, "Campaign", camp, fields, edits)


def _keyword_row(db, table, parent, text, match):
    rows = db.execute(f"select localId from {table} where parentId = ? and text = ? and criterionType = ? and status != 2",
                      (parent, text, MATCH_CODE[match.lower()])).fetchall()
    if len(rows) != 1:
        raise ValueError(f'{table} "{text}" ({match}): {"not found" if not rows else "ambiguous"} in Editor')
    return rows[0][0]


def _rsa(db, c, camp, created, comment):
    """A new responsive search ad, written the way Editor stores one: 15 headline + 4 description columns,
    <field>Pos = pinned position (0 = unpinned), final URL as a QStringList."""
    hl, ds = c["headlines"], c["descriptions"]
    if not 3 <= len(hl) <= 15 or not 2 <= len(ds) <= 4:
        raise ValueError("RSA needs 3-15 headlines and 2-4 descriptions")
    fields = {"parentId": (PARENT_ADGROUP << 32) | _adgroup_id(db, camp, c["ad_group"], created), "comment": comment,
              "status": STATUS_CODE[str(c.get("status", "paused")).lower()], "finalUrls": qstringlist([c["final_url"]]),
              "path1": c.get("path1", ""), "path2": c.get("path2", "")}
    for i, h in enumerate(hl, 1):
        h = h if isinstance(h, dict) else {"text": h}
        fields[f"headline{i}"], fields[f"headline{i}Pos"] = h["text"], int(h.get("pin") or 0)
    for i, d in enumerate(ds, 1):
        d = d if isinstance(d, dict) else {"text": d}
        fields[f"description{i}"], fields[f"description{i}Pos"] = d["text"], int(d.get("pin") or 0)
    return _new_row(db, "ResponsiveSearchAd", fields)


def apply_changes(db, changes, comment, edits=None):
    created = {"campaign": {}, "ad_group": {}}
    n = Counter()
    edits = [] if edits is None else edits
    geo = {r["remoteId"]: (r["locationShortName"], r["countryCode"]) for r in db.execute("select * from GeoLocation")}
    for c in changes:
        t = c["type"]
        if t not in SUPPORTED:
            raise ValueError(f"'{t}' can't be written directly yet - use `gae build` (import file) for it")
        if t == "campaign":
            if not c.get("new"):
                n["campaign_edit"] += _edit_campaign(db, c, edits)
                continue
            locs = c.get("locations", [])
            names = [geo.get(l["id"], (l.get("name", str(l["id"])), ""))[0] for l in locs]
            cid = _insert(db, "Campaign", {
                "name": c["campaign"], "comment": comment, "status": STATUS_CODE[str(c.get("status", "paused")).lower()],
                "budgetAmount": int(round(float(c["budget"]) * 1e6)),
                "biddingStrategyType": BID_CODE[c.get("bid_strategy", "Manual CPC").lower()],
                "apiBiddingStrategyType": BID_CODE[c.get("bid_strategy", "Manual CPC").lower()],
                "maxCpcBidCeiling": int(round(float(c["max_cpc_cap"]) * 1e6)) if c.get("max_cpc_cap") else 0,
                "languages": c.get("languages", "en"),
                "geoTargets": f"{len(names)};0;" + "; ".join(names),
                "touchedCountries": ";".join(sorted({geo.get(l["id"], ("", ""))[1] for l in locs} - {""})),
                **({"startDate": _ymd(c["start_date"])} if c.get("start_date") else {}),
                **({"endDate": _ymd(c["end_date"])} if c.get("end_date") else {})})
            created["campaign"][c["campaign"]] = cid
            n["campaign"] += 1
            for l in locs:
                _insert(db, "GeoTarget", {"parentId": (PARENT_CAMPAIGN << 32) | cid, "comment": comment, "locationId": int(l["id"])})
                n["location"] += 1
            continue
        camp = None if c.get("level") == "account" else _campaign_id(db, c["campaign"], created)
        if t == "ad_group" and not c.get("new"):  # edit an existing ad group: status and/or default max CPC
            aid = _adgroup_id(db, camp, c["ad_group"], created)
            fields = {}
            if c.get("status") is not None:
                fields["status"] = EDIT_STATUS[str(c["status"]).lower()]
            if c.get("max_cpc") is not None:
                fields["maxCpc"] = int(round(float(c["max_cpc"]) * 1e6))
            if not fields:
                raise ValueError("ad group edit needs status and/or max_cpc")
            n["ad_group_edit"] += _edit_row(db, "AdGroup", aid, fields, edits)
        elif t == "ad_group":
            aid = _insert(db, "AdGroup", {"parentId": (PARENT_CAMPAIGN << 32) | camp, "name": c["ad_group"], "comment": comment,
                                          "status": STATUS_CODE[str(c.get("status", "enabled")).lower()],
                                          "maxCpc": int(round(float(c["max_cpc"]) * 1e6)) if c.get("max_cpc") else 0})
            created["ad_group"][(camp, c["ad_group"])] = aid
            n["ad_group"] += 1
        elif t == "keyword" and c.get("edit"):  # change the max CPC and/or status of existing keywords
            parent = (PARENT_ADGROUP << 32) | _adgroup_id(db, camp, c["ad_group"], created)
            fields = {}
            if c.get("max_cpc") is not None:
                fields["maxCpc"] = int(round(float(c["max_cpc"]) * 1e6))
            if c.get("status") is not None:
                fields["status"] = EDIT_STATUS[str(c["status"]).lower()]
            if not fields:
                raise ValueError("keyword edit needs max_cpc and/or status")
            for kw in c.get("keywords", [c.get("keyword")]):
                kid = _keyword_row(db, "Keyword", parent, kw, c["match"])
                n["keyword_edit"] += _edit_row(db, "Keyword", kid, fields, edits)
        elif t == "rsa" and c.get("edit"):  # edit the existing (non-removed) RSAs of an ad group: final URL and/or all text
            parent = (PARENT_ADGROUP << 32) | _adgroup_id(db, camp, c["ad_group"], created)
            ids = [r[0] for r in db.execute("select localId from ResponsiveSearchAd where parentId = ? and status != 2", (parent,))]
            if c.get("local_id") is not None:  # one specific ad (ad groups with several RSAs)
                if int(c["local_id"]) not in ids:
                    raise ValueError(f'RSA {c["local_id"]} is not in "{c["campaign"]}" / "{c["ad_group"]}"')
                ids = [int(c["local_id"])]
            if not ids:
                raise ValueError(f'no RSAs in "{c["campaign"]}" / "{c["ad_group"]}"')
            if c.get("headlines") and len(ids) != 1:
                raise ValueError(f'"{c["ad_group"]}" has {len(ids)} RSAs - text edits need exactly one')
            fields = {}
            if c.get("final_url"):
                fields["finalUrls"] = qstringlist([c["final_url"]])
            if c.get("status") is not None:
                fields["status"] = EDIT_STATUS[str(c["status"]).lower()]
            if c.get("headlines"):
                hl, ds = c["headlines"], c["descriptions"]
                if not 3 <= len(hl) <= 15 or not 2 <= len(ds) <= 4:
                    raise ValueError("RSA needs 3-15 headlines and 2-4 descriptions")
                for i in range(1, 16):
                    h = hl[i - 1] if i <= len(hl) else {"text": None}
                    h = h if isinstance(h, dict) else {"text": h}
                    fields[f"headline{i}"], fields[f"headline{i}Pos"] = h["text"], int(h.get("pin") or 0)
                for i in range(1, 5):
                    d = ds[i - 1] if i <= len(ds) else {"text": None}
                    d = d if isinstance(d, dict) else {"text": d}
                    fields[f"description{i}"], fields[f"description{i}Pos"] = d["text"], int(d.get("pin") or 0)
            for i in ids:
                n["rsa_edit"] += _edit_row(db, "ResponsiveSearchAd", i, fields, edits)
        elif t == "sitelink" and c.get("remove"):  # unlink existing sitelinks (by link text): edge status -> Removed
            parent = ACCOUNT_PARENT if camp is None else (PARENT_CAMPAIGN << 32) | camp
            for txt in c.get("texts", [c.get("text")]):
                ids = [r[0] for r in db.execute("select e.localId from SitelinkV2Edge e join SitelinkV2 a on a.localId = e.sourceId "
                                                "where e.parentId = ? and e.status != 2 and a.displayText = ?", (parent, txt))]
                if not ids:
                    raise ValueError(f'sitelink "{txt}" not found on {c["campaign"]}')
                for i in ids:
                    n["sitelink_removed"] += _edit_row(db, "SitelinkV2Edge", i, {"status": 2}, edits)
        elif t == "sitelink" and c.get("edit"):  # new final URL on an existing sitelink (by link text)
            parent = ACCOUNT_PARENT if camp is None else (PARENT_CAMPAIGN << 32) | camp
            ids = [r[0] for r in db.execute("select a.localId from SitelinkV2Edge e join SitelinkV2 a on a.localId = e.sourceId "
                                            "where e.parentId = ? and e.status != 2 and a.status != 2 and a.displayText = ?",
                                            (parent, c["text"]))]
            if len(ids) != 1:
                raise ValueError(f'sitelink "{c["text"]}": {"not found" if not ids else "ambiguous"} on {c["campaign"]}')
            fields = {"finalUrls": qstringlist([c["final_url"]])} if c.get("final_url") else {}
            for k, col in (("new_text", "displayText"), ("line1", "description1"), ("line2", "description2")):
                if c.get(k) is not None:
                    fields[col] = c[k]
            n["sitelink_edit"] += _edit_row(db, "SitelinkV2", ids[0], fields, edits)
        elif t == "price" and c.get("edit"):  # new prices on the campaign's existing price asset (items matched by header)
            parent = ACCOUNT_PARENT if camp is None else (PARENT_CAMPAIGN << 32) | camp
            ids = [r[0] for r in db.execute("select a.localId from PriceExtensionV2Edge e join PriceExtensionV2 a on a.localId = e.sourceId "
                                            "where e.parentId = ? and e.status != 2 and a.status != 2", (parent,))]
            if len(ids) != 1:
                raise ValueError(f'price asset: {"not found" if not ids else "ambiguous"} on {c["campaign"]}')
            row = db.execute("select * from PriceExtensionV2 where localId = ?", (ids[0],)).fetchone()
            fields, left = {}, dict(c["prices"])
            for i in range(1, 9):
                if row[f"header{i}"] in left:
                    fields[f"price{i}"] = int(round(float(left.pop(row[f"header{i}"])) * 1e6))
            if left:
                raise ValueError(f'price asset on {c["campaign"]} has no item {sorted(left)}')
            n["price_edit"] += _edit_row(db, "PriceExtensionV2", ids[0], fields, edits)
        elif t == "rsa":
            _rsa(db, c, camp, created, comment)
            n["rsa"] += 1
        elif t == "keyword":
            if c.get("status") or c.get("final_url"):
                raise ValueError("keyword status/final URL changes aren't supported directly yet")
            aid = _adgroup_id(db, camp, c["ad_group"], created)
            for kw in c.get("keywords", [c.get("keyword")]):
                _insert(db, "Keyword", {"parentId": (PARENT_ADGROUP << 32) | aid, "comment": comment, "text": kw,
                                        "criterionType": MATCH_CODE[c["match"].lower()],
                                        "maxCpc": int(round(float(c["max_cpc"]) * 1e6)) if c.get("max_cpc") else 0})
                n["keyword"] += 1
        elif t == "sitelink":
            _asset_with_link(db, "SitelinkV2", {"displayText": c["text"], "description1": c.get("line1"), "description2": c.get("line2"),
                                                "finalUrls": qstringlist([c["final_url"]]), "startDate": _ymd(c.get("start_date")),
                                                "endDate": _ymd(c.get("end_date"))}, camp, comment)
            n["sitelink"] += 1
        elif t == "callout" and c.get("remove"):  # unlink existing callouts from the campaign: edge status -> Removed
            parent = ACCOUNT_PARENT if camp is None else (PARENT_CAMPAIGN << 32) | camp
            for txt in c.get("texts", [c.get("text")]):
                ids = [r[0] for r in db.execute("select e.localId from CalloutV2Edge e join CalloutV2 a on a.localId = e.sourceId "
                                                "where e.parentId = ? and e.status != 2 and a.displayText = ?", (parent, txt))]
                if not ids:
                    raise ValueError(f'callout "{txt}" not found on {c["campaign"]}')
                for i in ids:
                    n["callout_removed"] += _edit_row(db, "CalloutV2Edge", i, {"status": 2}, edits)
        elif t == "dsa_exclusion":  # campaign-level DSA page exclusion; Editor condition 2 = URL contains (API-verified on [6])
            for u in c["url_contains"]:
                _new_row(db, "DynamicTargetNegative", {"parentId": (PARENT_CAMPAIGN << 32) | camp, "comment": comment,
                                                       "condition1": 2, "value1": u, "condition2": 0, "value2": "",
                                                       "condition3": 0, "value3": ""})
                n["dsa_exclusion"] += 1
        elif t == "callout":
            for txt in c.get("texts", [c.get("text")]):
                _asset_with_link(db, "CalloutV2", {"displayText": txt, "startDate": _ymd(c.get("start_date")),
                                                   "endDate": _ymd(c.get("end_date"))}, camp, comment)
                n["callout"] += 1
        elif t == "snippet":
            _asset_with_link(db, "StructuredSnippetV2", {"header": c["header"], "snippetValues": qstringlist(c["values"])}, camp, comment)
            n["snippet"] += 1
        elif t == "price":
            f = {"type": c["price_type"], "priceQualifier": c.get("qualifier", "From"), "language": c.get("language", "en"),
                 "currencyCode": c.get("currency", "USD")}
            for i, it in enumerate(c["items"], 1):
                f.update({f"header{i}": it["header"], f"description{i}": it["description"],
                          f"price{i}": int(round(float(it["price"]) * 1e6)), f"finalUrl{i}": it["final_url"]})
                if it.get("unit"):
                    f[f"unit{i}"] = it["unit"]
            _asset_with_link(db, "PriceExtensionV2", f, camp, comment)
            n["price"] += 1
        elif t == "excluded_location":  # exclude countries/regions from an existing or new campaign
            for loc in c["locations"]:
                _insert_clone(db, "GeoTargetNegative", "1=1", {"parentId": (PARENT_CAMPAIGN << 32) | camp,
                              "locationId": int(loc["id"]), "comment": comment})
                n["excluded_location"] += 1
        elif t == "shared_list":  # attach shared negative keyword lists to the campaign
            for name in c["lists"]:
                row = db.execute("select localId from NegativeKeywordList where name = ?", (name,)).fetchone()
                if not row:
                    raise ValueError(f'negative keyword list "{name}" not found in Editor')
                _insert_clone(db, "NegativeKeywordListEdge", "1=1",
                              {"parentId": (PARENT_CAMPAIGN << 32) | camp, "sourceId": row[0], "comment": comment})
                n["shared_list"] += 1
        elif t == "audience" and c.get("edit"):  # bid adjustment (whole percent, e.g. 50) on audiences already on the campaign
            for name in c["lists"]:
                row = db.execute("select a.localId from Audience a join UserList u on u.remoteId = a.userListId "
                                 "where a.parentId = ? and u.name = ? and a.status != 2", ((PARENT_CAMPAIGN << 32) | camp, name)).fetchone()
                if not row:
                    raise ValueError(f'audience "{name}" is not on {c["campaign"]} in Editor')
                n["audience_edit"] += _edit_row(db, "Audience", row[0], {"bidModifier": int(c["bid_modifier"])}, edits)
        elif t == "location":  # add targeted locations to an existing campaign (GeoTarget rows, like a new campaign's)
            have = {r[0] for r in db.execute("select locationId from GeoTarget where parentId = ? and status != 2", ((PARENT_CAMPAIGN << 32) | camp,))}
            for loc in c["locations"]:
                if int(loc["id"]) in have:
                    continue
                _insert(db, "GeoTarget", {"parentId": (PARENT_CAMPAIGN << 32) | camp, "comment": comment, "locationId": int(loc["id"])})
                n["location"] += 1
        elif t == "promotion":  # promotion asset (percent off), format copied from the account's posted promotion rows
            f = {"promotionTarget": c["target"], "discountModifier": "Up to" if c.get("up_to") else "",
                 "percentDiscount": float(c["percent"]) / 100, "monetaryDiscount": -1, "ordersOverAmount": -1,
                 "finalUrls": qstringlist([c["final_url"]]), "finalMobileUrls": qstringlist([]), "adSchedule": qstringlist([]),
                 "language": c.get("language", "en"), "occasion": c.get("occasion", "")}
            if c.get("end_date"):
                f.update(promotionEndDate=_ymd(c["end_date"]), endDate=_ymd(c["end_date"]))
            if c.get("start_date"):
                f.update(promotionStartDate=_ymd(c["start_date"]), startDate=_ymd(c["start_date"]))
            _asset_with_link(db, "PromotionExtensionV2", f, camp, comment)
            n["promotion"] += 1
        elif t == "audience":  # campaign-level audience segments (observation follows the campaign's setting)
            for name in c["lists"]:
                row = db.execute("select remoteId from UserList where name = ?", (name,)).fetchone()
                if not row:
                    raise ValueError(f'audience list "{name}" not found in Editor')
                _insert_clone(db, "Audience", f"parentId >> 32 = {PARENT_CAMPAIGN} and userListId != 0",
                              {"parentId": (PARENT_CAMPAIGN << 32) | camp, "userListId": row[0], "comment": comment,
                               "bidModifier": -2147483647, "maxCpc": 0, "maxCpm": 0})
                n["audience"] += 1
        elif t == "negative":
            parent = ((PARENT_ADGROUP << 32) | _adgroup_id(db, camp, c["ad_group"], created)) if c.get("ad_group") \
                else ((PARENT_CAMPAIGN << 32) | camp)
            if c.get("remove"):  # remove existing negatives: status -> Removed, like Editor's own Remove
                for kw in c.get("keywords", [c.get("keyword")]):
                    nid = _keyword_row(db, "KeywordNegative", parent, kw, c.get("match", "phrase"))
                    n["negative_removed"] += _edit_row(db, "KeywordNegative", nid, {"status": 2}, edits)
                continue
            for kw in c.get("keywords", [c.get("keyword")]):
                if db.execute("select 1 from KeywordNegative where parentId = ? and text = ? and criterionType = ? and status != 2",
                              (parent, kw, MATCH_CODE[c.get("match", "phrase").lower()])).fetchone():
                    continue  # already there: a duplicate posts fine but comes back as a twin row Editor wants to remove
                _insert(db, "KeywordNegative", {"parentId": parent, "comment": comment, "text": kw,
                                                "criterionType": MATCH_CODE[c.get("match", "phrase").lower()]})
                n["negative"] += 1
    return n


def _bump_delta(vdir, acct, delta):
    db = sqlite3.connect(str(vdir / "ape.db"))
    db.execute("update Account set deltaCount = max(0, deltaCount + ?) where remoteId = ?", (delta, int(acct)))
    db.commit()


def cmd_apply(args):
    plan = json.loads(Path(args.plan).read_text())
    rows = [r for c in plan["changes"] for r in plan_rows(c)]
    problems = lint(rows)
    if problems:
        print("Plan has problems, nothing written:")
        for p in problems:
            print("  -", p)
        sys.exit(1)
    stamp = dt.datetime.now()
    pid = f"{stamp:%Y%m%d-%H%M}-{slug(plan['title'])}"
    comment = f"Claude {pid}"
    db, vdir, bdir, relaunch = open_for_write(args.acct, args.restart)
    removed = Counter()
    edits = []
    try:
        for old in args.replace or []:
            removed += discard_rows(db, old)
        with db:  # one transaction: all or nothing
            n = apply_changes(db, plan["changes"], comment, edits)
    except Exception as ex:
        if relaunch:
            launch_editor()
        sys.exit(f"Nothing new written: {ex}")
    _bump_delta(vdir, args.acct, sum(n.values()) - sum(removed.values()))
    if removed:
        print(f"Replaced earlier pending rows: {dict(+removed)}")
    summary = [summarise(c) for c in plan["changes"]]
    with open(CHANGELOG, "a") as f:
        f.write(json.dumps({"id": pid, "ts": stamp.isoformat(timespec="seconds"), "account": args.acct, "title": plan["title"],
                            "why": plan.get("why"), "file": "written directly into Editor", "plan": plan, "changes": summary,
                            "edits": edits,
                            "campaigns": sorted({c["campaign"] for c in plan["changes"]})}, ensure_ascii=False) + "\n")
    print(f"\n{plan['title']} - written into Editor as pending changes ({dict(n)})\n")
    for s in summary:
        print("  -", s)
    print(f"\nBackup: {bdir}")
    if relaunch:
        launch_editor()
        print("Reopened Google Ads Editor.")
    print(f"In Editor: rows have comment \"{comment}\" - review, then Post. Undo before posting: gae discard {pid}")


def cmd_discard(args):
    """Remove pending (never posted) rows a plan created. Posted rows are never touched."""
    db, vdir, bdir, relaunch = open_for_write(args.acct, args.restart)
    total = Counter()
    for pid in args.plan_id:
        n = discard_rows(db, pid)
        total += n
        print(f"Discarded {sum(n.values())} pending rows from {pid}: {dict(+n)}")
    _bump_delta(vdir, args.acct, -sum(total.values()))
    print(f"Backup: {bdir}")
    if relaunch:
        launch_editor()
        print("Reopened Google Ads Editor.")


def discard_rows(db, plan_id):
    comment = f"Claude {plan_id}"
    n = Counter()
    with db:
        camps = [r[0] for r in db.execute("select localId from Campaign where comment = ? and state = ? and remoteId = 0", (comment, STATE_NEW))]
        groups = [r[0] for r in db.execute("select localId from AdGroup where comment = ? and state = ? and remoteId = 0", (comment, STATE_NEW))]
        for t in ENTITY_TABLES:
            n[t] = db.execute(f"delete from {t} where comment = ? and state = ? and remoteId = 0", (comment, STATE_NEW)).rowcount
        for tid, ids in ((PARENT_CAMPAIGN, camps), (PARENT_ADGROUP, groups)):
            for i in ids:
                db.execute("delete from Error where tableId = ? and entityId = ?", (tid, i))
        # keyword errors (table 38) left behind by deleted keywords still show as errors in Editor
        db.execute("delete from Error where tableId = 38 and entityId not in (select localId from Keyword)")
        for i in camps:
            db.execute("delete from WorkflowEvent where campaignEntityId = ?", (i,))
        # edits of existing rows: put the old values back (newest first), unless already posted
        for e in reversed(_plan_edits(plan_id)):
            t, lid, f = e["table"], e["localId"], e["field"]
            row = db.execute(f"select * from {t} where localId = ?", (lid,)).fetchone()
            if row is None or row["state"] == 0 or row[f] != _unjson(e["new"]):
                continue  # posted, gone, or changed since - leave it alone
            if f + "_revert" in row.keys():
                db.execute(f"update {t} set {f} = ?, {f}_revert = NULL where localId = ?", (_unjson(e["old"]), lid))
            else:
                db.execute(f"update {t} set {f} = ? where localId = ?", (_unjson(e["old"]), lid))
            if e["state_before"] == 0:
                row = db.execute(f"select * from {t} where localId = ?", (lid,)).fetchone()
                if all(row[k] is None for k in row.keys() if k.endswith("_revert")):
                    db.execute(f"update {t} set state = 0 where localId = ?", (lid,))
            n[t + "_edit"] += 1
    return n


def _plan_edits(plan_id):
    if not CHANGELOG.exists():
        return []
    for line in CHANGELOG.read_text().splitlines():
        rec = json.loads(line)
        if rec.get("id") == plan_id:
            return rec.get("edits") or []
    return []


# ---------- history ----------

def live_changes(acct, campaign, days):
    import google_ads_api as g
    days = min(days, 29)  # change_event only keeps the last 30 days
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    until = (dt.date.today() + dt.timedelta(days=1)).isoformat()
    q = ("SELECT change_event.change_date_time, change_event.client_type, change_event.user_email, "
         "change_event.change_resource_type, change_event.resource_change_operation, change_event.changed_fields, "
         "campaign.name, ad_group.name FROM change_event "
         f"WHERE change_event.change_date_time >= '{since}' AND change_event.change_date_time <= '{until}' "
         "ORDER BY change_event.change_date_time DESC LIMIT 10000")
    if g._env("GADS_OPERATING_CUSTOMER_ID") != acct:
        return None
    rows = g._search_stream(q, g._token())
    out = []
    for r in rows:
        name = r.get("campaign", {}).get("name", "")
        if campaign and campaign.lower() not in name.lower():
            continue
        e = r["changeEvent"]
        out.append({"when": e["changeDateTime"][:16], "via": e.get("clientType", "").replace("GOOGLE_ADS_", "").replace("_", " ").title(),
                    "who": e.get("userEmail", ""), "what": f"{e['resourceChangeOperation'].title()} {e['changeResourceType'].replace('_', ' ').lower()}",
                    "fields": e.get("changedFields", ""), "campaign": name, "ad_group": r.get("adGroup", {}).get("name", "")})
    return out


def cmd_history(args):
    print("What Claude proposed (gae build):")
    found = False
    if CHANGELOG.exists():
        for line in CHANGELOG.read_text().splitlines():
            e = json.loads(line)
            if e["account"] != args.acct:
                continue
            if args.campaign and not any(args.campaign.lower() in c.lower() for c in e["campaigns"]):
                continue
            found = True
            print(f"\n  {e['ts'][:16]}  {e['title']}  [{e['id']}]")
            if e.get("why"):
                print(f"    why: {e['why']}")
            for s in e["changes"]:
                if not args.campaign or args.campaign.lower() in s.lower():
                    print(f"    - {s}")
            print(f"    file: {e['file']}")
    if not found:
        print("  (nothing yet)")

    print(f"\nWhat was actually posted live (Google change history, last {min(args.days, 29)} days):")
    try:
        live = live_changes(args.acct, args.campaign, args.days)
    except Exception as ex:
        print(f"  could not read change history: {ex}")
        return
    if live is None:
        print("  (live history only wired for the ML account)")
        return
    grouped = defaultdict(list)
    for c in live:
        grouped[(c["when"], c["via"], c["who"], c["campaign"])].append(c)
    rows = []
    for (when, via, who, camp), items in list(grouped.items())[: args.limit]:
        kinds = Counter(i["what"] for i in items)
        rows.append((when, via, who, camp[:40], "; ".join(f"{n}x {k}" if n > 1 else k for k, n in kinds.items())))
    table(rows, ["when", "via", "who", "campaign", "what"])
    if len(grouped) > args.limit:
        print(f"  ... {len(grouped) - args.limit} more (use --limit)")


def main():
    p = argparse.ArgumentParser(prog="gae", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--account", default="ml")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("accounts")
    s = sub.add_parser("campaigns"); s.add_argument("--all", action="store_true")
    s = sub.add_parser("show"); s.add_argument("campaign")
    s = sub.add_parser("search"); s.add_argument("text")
    s = sub.add_parser("geo"); s.add_argument("name")
    sub.add_parser("pending")
    s = sub.add_parser("build"); s.add_argument("plan"); s.add_argument("--dry-run", action="store_true"); s.add_argument("--out")
    s = sub.add_parser("apply"); s.add_argument("plan"); s.add_argument("--restart", action="store_true")
    s.add_argument("--replace", action="append", metavar="PLAN_ID", help="discard this earlier plan's pending rows first")
    s = sub.add_parser("discard"); s.add_argument("plan_id", nargs="+"); s.add_argument("--restart", action="store_true")
    s = sub.add_parser("history"); s.add_argument("campaign", nargs="?"); s.add_argument("--days", type=int, default=29); s.add_argument("--limit", type=int, default=40)
    args = p.parse_args()
    args.acct = resolve_account(args.account)
    globals()[f"cmd_{args.cmd}"](args)


if __name__ == "__main__":
    main()
