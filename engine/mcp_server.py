#!/usr/bin/env python3
"""Marketing Studio MCP server (stdio). Gives Claude the same view and the same drafts as the Marketing Studio UI
(Ads, Email, SEO, SoMe). Ads: read everything Google Ads Editor holds, add/remove draft changes (they show up live in
the UI), check the draft against a copy of Editor's data, and - only after the user says so - send the draft into Editor.
Email, SEO, SoMe: read tools plus list/add draft tools only. There are no tools to send, schedule, publish, post,
delete or undo anything: live actions are only ever the user's own buttons in the Studio page.

No dependencies: plain JSON-RPC 2.0 over stdin/stdout, one message per line (the MCP stdio transport).
"""
import json, sys, traceback

import appconfig  # noqa: F401  (keys + vendor modules; same Keychain and data folder as the app)

import core
import datasources
import gapi
import meta_ads as meta
import areas

PROTOCOL = "2025-06-18"
ACCT = {"type": "string", "description": "Google Ads account key or id (see list_accounts; the first one is the default) or meta / meta_<id> for a Meta ad account"}
GOOGLE_KINDS = tuple(gapi.KINDS) + tuple(gapi.EXTRA_KINDS)
KINDS = sorted(set(GOOGLE_KINDS) | set(meta.GRIDS))

TOOLS = [
    {"name": "list_accounts", "description": "Google Ads accounts downloaded in Editor (with when Editor last downloaded each) and the Meta ad account.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "list_entities",
     "description": "Rows of one entity type, with any draft changes overlaid (_draft = pending draft value, _new = row added "
                    "in the draft). Google (read from the Google Ads API, or Editor's copy if the account is set to Editor in Studio "
                    "Settings), with stats for the period: cost, impressions, clicks, ctr, cpc, conversions, conv_value, cost_per_conv. "
                    "Google kinds: " + ", ".join(GOOGLE_KINDS) + " (asset_group_assets, user_lists and recommendations are API only). "
                    "Meta kinds (from the Meta API, cached until a refresh_data): " + ", ".join(meta.GRIDS) + "; stats for the period on spend, "
                    "impressions, reach, frequency, clicks, ctr, cpc, cpm, results, cost_per_result. audiences and leadforms are read "
                    "only and personal data: UK GDPR check needed before any upload/download; never download leads. "
                    "Scope with campaign / ad_group names (substring match on campaign if not exact); for Meta ad_group = ad set.",
     "inputSchema": {"type": "object", "properties": {
         "kind": {"type": "string", "enum": KINDS}, "account": ACCT,
         "campaign": {"type": "string"}, "ad_group": {"type": "string"},
         "status": {"type": "string", "enum": ["active", "enabled", "all"], "description": "active = not removed (default)"},
         "days": {"type": "integer", "enum": [7, 14, 30, 90], "description": "stats period in days (default 7)"},
         "start": {"type": "string", "description": "custom stats period start YYYY-MM-DD (with end, instead of days)"},
         "end": {"type": "string", "description": "custom stats period end YYYY-MM-DD"},
         "search": {"type": "string", "description": "case-insensitive text filter over all columns"},
         "limit": {"type": "integer", "description": "default 200"}}, "required": ["kind"]}},
    {"name": "get_draft", "description": "The shared draft (changes not yet sent to Editor), with problems and warnings per item.",
     "inputSchema": {"type": "object", "properties": {"account": ACCT}}},
    {"name": "add_to_draft",
     "description": "Add changes to the shared draft. They appear live in the user's Ads Studio for review. Nothing is written "
                    "to Editor, Google or Meta. Items with problems must be fixed (remove + re-add) before the draft can be sent.\n\n"
                    + core.CHANGE_REFERENCE + "\n\n" + meta.CHANGE_REFERENCE,
     "inputSchema": {"type": "object", "properties": {
         "account": ACCT, "changes": {"type": "array", "items": {"type": "object"}},
         "note": {"type": "string", "description": "why - shown to the user next to each change"}}, "required": ["changes"]}},
    {"name": "remove_from_draft", "description": "Remove draft items by id.",
     "inputSchema": {"type": "object", "properties": {"account": ACCT, "ids": {"type": "array", "items": {"type": "string"}}},
                     "required": ["ids"]}},
    {"name": "check_draft", "description": "Google: dry run of every draft item on a throwaway copy of Editor's database. Meta: re-run Studio's checks (copy rules, dead links, prices vs the linked page) on fresh data.",
     "inputSchema": {"type": "object", "properties": {"account": ACCT}}},
    {"name": "send_draft_to_editor",
     "description": "Write the whole draft into Google Ads Editor as pending changes (closes and reopens Editor, ~15s). Only call "
                    "this when the user has explicitly said to send it in this conversation. Nothing goes live until they Post in Editor.",
     "inputSchema": {"type": "object", "properties": {"account": ACCT, "title": {"type": "string"},
                                                      "user_confirmed": {"type": "boolean", "description": "must be true"}},
                     "required": ["user_confirmed"]}},
    {"name": "preview_live_push",
     "description": "Ask Google or Meta to check the draft as a live push (validate only - nothing changes): shows live before -> after "
                    "values, spend-raising changes and permanent removals. Pushing live itself is only done by the user's "
                    "Push live button in Ads Studio.",
     "inputSchema": {"type": "object", "properties": {"account": ACCT, "enable_new": {"type": "boolean"}}}},
    {"name": "live_push_history", "description": "Recent live pushes from Ads Studio (what went live, when, undone or not).",
     "inputSchema": {"type": "object", "properties": {"account": ACCT}}},
    {"name": "find_location", "description": "Look up Google Ads location IDs by name (from Editor's location data).",
     "inputSchema": {"type": "object", "properties": {"account": ACCT, "name": {"type": "string"}}, "required": ["name"]}},
]

LIMIT = {"type": "integer", "description": "default 200"}
DAYS = {"type": "integer", "enum": [7, 14, 28, 30, 90], "description": "period in days (default 28; Search Console data ends 2 days ago)"}
AREA_TOOLS = [
    # Email (the connected email platform). Drafts stay in Studio until the user presses Save to platform; sending is the user's button.
    {"name": "email_list_campaigns", "description": "Email campaigns from the connected email platform (demo data if none is connected): name, subject, audience, status, stats, and whether a Studio draft exists.",
     "inputSchema": {"type": "object", "properties": {"status": {"type": "string", "description": "draft, scheduled, sending, sent, cancelled, failed or all"},
                                                      "search": {"type": "string"}, "limit": LIMIT}}},
    {"name": "email_get_campaign", "description": "One email campaign: the platform's original, the Studio draft (if any) and the merged view.",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
    {"name": "email_list_drafts", "description": "Studio email drafts (edits not yet saved to the email platform).",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "email_add_draft",
     "description": "Draft edits to one email campaign in Studio. Never touches the email platform and never sends. Fields: subject, preview_text, html "
                    "(raw HTML), from_name, list_id, segment_id, name, text. From and reply-to are fixed (dive@). The user reviews, saves and sends.",
     "inputSchema": {"type": "object", "properties": {"campaign_id": {"type": "string"}, "fields": {"type": "object"},
                                                      "note": {"type": "string", "description": "why - shown to the user"}},
                     "required": ["campaign_id", "fields"]}},
    # SEO (WordPress posts + Search Console). Publishing to WordPress is the user's button.
    {"name": "seo_list_posts", "description": "WordPress blog posts with Search Console clicks, impressions, CTR, position, change vs the previous period, GA4 sessions and Select Cabin, index state and any drafted edits.",
     "inputSchema": {"type": "object", "properties": {"days": DAYS, "search": {"type": "string", "description": "filter on title or URL"}, "limit": LIMIT}}},
    {"name": "seo_get_post", "description": "One WordPress post with its fields (title, SEO title, meta description, content ...), top queries and cannibalisation.",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "integer"}, "days": DAYS}, "required": ["id"]}},
    {"name": "seo_opportunities", "description": "SEO opportunities from Search Console: losing posts, low CTR, striking distance queries, not indexed.",
     "inputSchema": {"type": "object", "properties": {"days": DAYS}}},
    {"name": "seo_list_drafts", "description": "The SEO draft (edits and to-dos not yet published to WordPress), with quick checks per change.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "seo_add_draft",
     "description": "Add one change to the SEO draft (never to WordPress). Edit: {post_id, field, new, note}. Suggestion without text: "
                    "{post_id, field, note, kind: 'todo'}. Same post + field replaces the earlier edit. The user checks and publishes.",
     "inputSchema": {"type": "object", "properties": {"post_id": {"type": "integer"},
                                                      "field": {"type": "string", "enum": ["title", "slug", "seo_title", "meta_desc", "focus_kw", "excerpt", "categories", "content"]},
                                                      "new": {},
                                                      "note": {"type": "string"}, "kind": {"type": "string", "enum": ["edit", "todo"]}},
                     "required": ["post_id", "field"]}},
    # SoMe (organic Facebook, Instagram, LinkedIn). Posting and scheduling are the user's buttons.
    {"name": "social_calendar", "description": "The SoMe calendar: Studio posts (draft, scheduled, published) and posts already live on Facebook and Instagram, between start and end.",
     "inputSchema": {"type": "object", "properties": {"start": {"type": "string", "description": "YYYY-MM-DD (default 1st of this month)"},
                                                      "end": {"type": "string", "description": "YYYY-MM-DD (default start + 42 days)"}}}},
    {"name": "social_list_drafts", "description": "Every Studio SoMe post with status (draft, scheduled, published) and its check problems.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "social_add_draft",
     "description": "Create a draft SoMe post (no id) or change a draft post (with id). Never posts or schedules; scheduled or published posts "
                    "can't be changed. Fields as in the Studio editor: title, channels (facebook, instagram, linkedin), caption, captions "
                    "(per channel), when (planned time, local), link, media, note.",
     "inputSchema": {"type": "object", "properties": {"change": {"type": "object"}}, "required": ["change"]}},
    # Activity report (read only): every change in a period across Google Ads, Meta, email, blog, SoMe and Studio.
    {"name": "activity_summary", "description": "Counts of activity per channel in a period (Google Ads changes, Meta changes, emails sent, blog posts published/edited, "
                                                 "SoMe posts, Studio pushes), compared with the previous period, plus per-day counts and how far back the archive goes.",
     "inputSchema": {"type": "object", "properties": {"period": {"type": "string", "description": "this_week, last_week, this_month, last_month, last_7, last_30, last_90, this_quarter or custom"},
                                                      "start": {"type": "string", "description": "YYYY-MM-DD, for custom"}, "end": {"type": "string", "description": "YYYY-MM-DD, for custom"}}}},
    {"name": "activity_rows", "description": "The combined activity timeline for a period, filtered. Who made each change is left out (staff personal data).",
     "inputSchema": {"type": "object", "properties": {"period": {"type": "string"}, "start": {"type": "string"}, "end": {"type": "string"},
                                                      "filters": {"type": "object", "description": "e.g. {channel: 'google', type: '...', search: '...'}"},
                                                      "limit": {"type": "integer", "description": "default 200"}}}},
]
DATA_TOOLS = [
    # Studio keeps what it fetched until someone refreshes (datapolicy, "manual" mode): every read tool above uses the cache.
    {"name": "data_status", "description": "When Studio last fetched each source (Google Ads per account, Meta per ad account, Email, "
                                           "SEO: Search Console / GA4 / WordPress, SoMe, Activity archive, exchange rates) and any Activity "
                                           "warning (Google keeps change history about 30 days). Read tools use this cached data.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "refresh_data", "description": "Fetch one source again, read-only (no writes anywhere): google (account ml or mluk, none = both), "
                                            "meta (account e.g. meta, none = every Meta account), email, seo (account gsc, ga4 or wp, none = all), "
                                            "social, activity (syncs the archive), fx, or all. Use when the user asks for fresh data.",
     "inputSchema": {"type": "object", "properties": {
         "source": {"type": "string", "enum": ["all", "google", "meta", "email", "seo", "social", "activity", "fx"]},
         "account": {"type": "string", "description": "optional: one account or SEO part"},
         "days": {"type": "integer", "description": "stats window to fetch with Google (default 7)"}}, "required": ["source"]}},
]
SETUP_TOOLS = [
    {"name": "setup_status", "description": "Which keys Marketing Studio has (Meta, Google Ads, Google sign-in, Email, WordPress, LinkedIn): set or not set per key. Never returns a value.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "setup_import_env_file",
     "description": "Load Marketing Studio's keys from a .env file on this Mac (KEY=value lines, e.g. META_ACCESS_TOKEN, META_AD_ACCOUNT_ID, "
                    "GADS_DEVELOPER_TOKEN, GADS_LOGIN_CUSTOMER_ID, GADS_OPERATING_CUSTOMER_ID, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, "
                    "GOOGLE_REFRESH_TOKEN, GOOGLE_SERVICE_ACCOUNT_FILE=<path to a service-account json>, EMAIL_*, WP_STUDIO_*, LINKEDIN_*). "
                    "Secrets go into the macOS Keychain. Returns only which keys were imported and which lines were ignored, never values. "
                    "Only run this when the user asks to set Studio up from a file.",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string", "description": "absolute path to the .env file"}}, "required": ["path"]}},
]
TOOLS += AREA_TOOLS + DATA_TOOLS + SETUP_TOOLS


def _scope(acct, campaign, ad_group, tree=None):
    if not campaign:
        return None, None
    tree = tree if tree is not None else core.tree(acct, "all")
    hits = [c for c in tree if c["name"].lower() == campaign.lower()] or [c for c in tree if campaign.lower() in c["name"].lower()]
    live = [c for c in hits if c["status"] != "Removed"] or hits
    if len(live) != 1:
        raise ValueError(f'campaign "{campaign}": ' + ("not found" if not live else "matches " + "; ".join(c["name"] for c in live[:10])))
    c = live[0]
    if not ad_group:
        return c["id"], None
    gs = [g for g in c["ad_groups"] if g["name"].lower() == ad_group.lower()] or [g for g in c["ad_groups"] if ad_group.lower() in g["name"].lower()]
    if len(gs) != 1:
        raise ValueError(f'ad group "{ad_group}": ' + ("not found" if not gs else "matches " + "; ".join(g["name"] for g in gs[:10])))
    return c["id"], gs[0]["id"]


def _date_q(a):
    """MCP days -> the SEO area's date range query (range=7|14|28|30|90)."""
    return {"range": str(a["days"])} if a.get("days") else {}


def _limit(rows, a):
    n = a.get("limit") or 200
    return {"total": len(rows), "returned": min(len(rows), n), "rows": rows[:n]}


def call_area(name, a):
    em, seo, so, ac = (areas.AREAS.get(k) for k in ("email", "seo", "social", "activity"))
    area = {"email": em, "seo": seo, "social": so, "activity": ac}[name.split("_")[0]]
    if area is None:
        raise ValueError(f"the {name.split('_')[0]} area didn't load: see the Studio server log")
    if name == "email_list_campaigns":
        r = em.g_campaigns({"status": a.get("status"), "search": a.get("search")})
        return {"demo": r["demo"], "counts": r["counts"], **_limit(r["campaigns"], a)}
    if name == "email_get_campaign":
        return em.g_campaign({"id": str(a["id"])})
    if name == "email_list_drafts":
        return em.list_drafts()
    if name == "email_add_draft":
        return em.add_draft({"campaign_id": a.get("campaign_id"), "fields": a.get("fields"), "note": a.get("note")}, by="claude")
    if name == "seo_list_posts":
        r = seo.get_posts(_date_q(a))
        rows, needle = r["rows"], (a.get("search") or "").lower().strip()
        if needle:
            rows = [x for x in rows if needle in f"{x.get('title', '')} {x.get('url', '')}".lower()]
        return {"range": r["range"], "errors": r["errors"], "fetched": r["fetched"], **_limit(rows, a)}
    if name == "seo_get_post":
        return seo.get_post({"id": a["id"], **_date_q(a)})
    if name == "seo_opportunities":
        return seo.get_opportunities(_date_q(a))
    if name == "seo_list_drafts":
        return seo.list_drafts()
    if name == "seo_add_draft":
        return seo.add_draft({k: a[k] for k in ("post_id", "field", "new", "note", "kind") if k in a}, by="claude")
    if name == "social_calendar":
        return so.get_calendar({k: a[k] for k in ("start", "end") if a.get(k)})
    if name == "social_list_drafts":
        return so.list_drafts()
    if name == "social_add_draft":
        return so.add_draft(a.get("change") or {}, by="claude")
    if name == "activity_summary":
        return ac.activity_summary(a.get("period") or "last_30", a.get("start"), a.get("end"))
    if name == "activity_rows":  # never include_who for Claude
        return ac.activity_rows(a.get("period") or "last_30", a.get("filters"), a.get("start"), a.get("end"), limit=int(a.get("limit") or 200))
    raise ValueError(f"unknown tool {name}")


AREA_NAMES = {t["name"] for t in AREA_TOOLS}


def call(name, a):
    if name in AREA_NAMES:
        return call_area(name, a)
    if name == "setup_status":
        st = appconfig.status()
        return {"keys": {k["name"]: k["set"] for k in st["keys"]}, "google_signed_in": st["google_signed_in"],
                "google_service_account": st["google_service_account"], "ready": st["ready"], "data_folder": str(appconfig.DATA)}
    if name == "setup_import_env_file":
        return appconfig.import_env_file(a["path"])
    if name == "data_status":
        return datasources.status()
    if name == "refresh_data":
        return datasources.refresh(a.get("source"), a.get("account") or None, **({"days": a["days"]} if a.get("days") else {}))
    if name == "list_accounts":
        return core.accounts() + meta.accounts()
    if meta.is_meta(a.get("account")):
        return call_meta(name, a)
    acct = core.resolve(a.get("account"))
    if name == "list_entities":
        if a["kind"] not in GOOGLE_KINDS:
            raise ValueError(f"Google kinds: {', '.join(GOOGLE_KINDS)}")
        cid, gid = _scope(acct, a.get("campaign"), a.get("ad_group"), gapi.tree_any(acct, "all"))
        rows = gapi.grid_any(acct, a["kind"], cid, gid, a.get("status", "active"), a.get("search"), None,
                             a.get("days"), a.get("start"), a.get("end"))
        limit = a.get("limit") or 200
        return {"total": len(rows), "returned": min(len(rows), limit), "rows": rows[:limit]}
    if name == "get_draft":
        return core.draft(acct)
    if name == "add_to_draft":
        return core.add(acct, a["changes"], by="claude", note=a.get("note"))
    if name == "remove_from_draft":
        return {"removed": core.remove(acct, a["ids"])}
    if name == "check_draft":
        return core.validate(acct)
    if name == "send_draft_to_editor":
        if a.get("user_confirmed") is not True:
            raise ValueError("Ask the user first; call again with user_confirmed=true once they have said to send it.")
        return core.push(acct, a.get("title"))
    if name == "preview_live_push":
        import live
        return live.preview(acct, bool(a.get("enable_new")))
    if name == "live_push_history":
        import live
        return live.history(acct)
    if name == "find_location":
        return core.geo_search(acct, a["name"])
    raise ValueError(f"unknown tool {name}")


def call_meta(name, a):
    meta.use(a.get("account"))
    if name == "list_entities":
        if a["kind"] not in meta.GRIDS:
            raise ValueError(f"Meta kinds: {', '.join(meta.GRIDS)}")
        cid, sid = _scope(None, a.get("campaign"), a.get("ad_group"), meta.tree("all"))
        win = {"since": a.get("start"), "until": a.get("end")} if a.get("start") or a.get("end") else {"window": a.get("days") or 7}
        rows = meta.grid(a["kind"], cid, sid, a.get("status", "active"), a.get("search"), **win)
        for r in rows:
            r.pop("thumbnail", None)
        limit = a.get("limit") or 200
        return {"total": len(rows), "returned": min(len(rows), limit), "rows": rows[:limit]}
    if name == "get_draft":
        return core.draft(meta.acct())
    if name == "add_to_draft":
        return meta.add(a["changes"], by="claude", note=a.get("note"))
    if name == "remove_from_draft":
        return {"removed": core.remove(meta.acct(), a["ids"])}
    if name == "check_draft":
        return meta.validate()
    if name == "send_draft_to_editor":
        raise ValueError("Meta has no Editor. The user pushes Meta changes live with the Push live button in Ads Studio.")
    if name == "preview_live_push":
        return meta.preview()
    if name == "live_push_history":
        return meta.history()
    raise ValueError(f"{name} is not available for Meta")


def handle(msg):
    m, mid = msg.get("method"), msg.get("id")
    if m == "initialize":
        return {"protocolVersion": msg.get("params", {}).get("protocolVersion", PROTOCOL), "capabilities": {"tools": {}},
                "serverInfo": {"name": "marketing-studio", "version": "1.2"},
                "instructions": "Marketing Studio (localhost:4830), four areas: Ads (Google Ads Editor data, the Meta ad account and a "
                                "shared draft per account), Email (campaigns on the connected email platform), SEO (WordPress posts + Search Console) and SoMe "
                                "(organic Facebook, Instagram, LinkedIn posts). Drafts only: you read and add to drafts the user reviews "
                                "in Studio. Never send to Editor without the user's explicit go. Live actions (push live, send, schedule, "
                                "publish, post, delete, undo) are only ever the user's own buttons in Studio; there are no tools for them."}
    if m == "tools/list":
        return {"tools": TOOLS}
    if m == "tools/call":
        p = msg["params"]
        try:
            out = call(p["name"], p.get("arguments") or {})
            return {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False, default=str)}]}
        except Exception as ex:  # noqa: BLE001 - tool errors go back to the model, not the transport
            return {"content": [{"type": "text", "text": f"Error: {ex}"}], "isError": True}
    if m == "ping":
        return {}
    if mid is not None:
        raise LookupError(m)
    return None


def main():
    for line in sys.stdin:
        if not line.strip():
            continue
        msg = json.loads(line)
        if "id" not in msg:  # notification
            continue
        try:
            res = {"jsonrpc": "2.0", "id": msg["id"], "result": handle(msg)}
        except LookupError as ex:
            res = {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": f"method not found: {ex}"}}
        except Exception as ex:  # noqa: BLE001
            traceback.print_exc(file=sys.stderr)
            res = {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32603, "message": str(ex)}}
        sys.stdout.write(json.dumps(res, ensure_ascii=False, default=str) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
