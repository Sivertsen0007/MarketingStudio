#!/usr/bin/env python3
"""Marketing Studio MCP server (stdio). Gives Claude the same view and the same drafts as the Marketing Studio UI
(Ads, Email, SEO, SoMe, Activity, Alerts). Ads: read Google Ads (from the Google Ads API by default, or Google Ads
Editor's copy when the user picked Editor for an account) and Meta, add/remove draft changes (they show up live in the
UI), check the draft, preview a live push (validate only) and - only after the user says so - send the draft into Editor.
Email, SEO, SoMe: read tools plus draft tools only. Team tools comment, ask for a review and sync drafts with the team.
There are no tools to push live, send, schedule, publish, post, delete or undo anything: live actions are only ever
the user's own buttons in the Studio page.

Read tools use what Studio has cached; refresh_data (or refresh=true on the few tools that have it) fetches again,
read-only. Google Ads Editor is optional: without it the server keeps running and only the Editor tools say so.

No dependencies: plain JSON-RPC 2.0 over stdin/stdout, one message per line (the MCP stdio transport).
"""
import json, sys, traceback

import appconfig  # noqa: F401  (keys + vendor modules; same Keychain and data folder as the app)

import core
import datasources
import demandgen
import gae
import gapi
import meta_ads as meta
import meta_tests
import audiences  # [audiences]
import meta_audiences  # [audiences]
import areas

PROTOCOL = "2025-06-18"
INSTRUCTIONS = (
    "Marketing Studio (the Mac app on 127.0.0.1:4830). Areas: Ads (Google Ads through the Google Ads API, Google Ads Editor "
    "optional; Meta ad accounts; a shared draft per account), Email (campaigns on the connected email platform), SEO "
    "(WordPress posts, Search Console, GA4, topical map), SoMe (organic Facebook, Instagram, LinkedIn posts), Activity and "
    "Alerts. Drafts and team comments only: you read, add to drafts the user reviews in Studio, and - when the user asks - "
    "comment for the team, ask for a review or sync drafts with the team. Nothing goes live from here: push live, send, "
    "schedule, publish, post, delete and undo are only ever the user's own buttons in Studio; there are no tools for them. "
    "Account keys come from list_accounts. Read tools use Studio's cache and say when nothing is cached; refresh_data "
    "fetches again (read-only) when the user wants fresh data. Never send to Editor without the user's explicit go.")
ACCT = {"type": "string", "description": "account key from list_accounts (its key or id): a Google Ads account, or a Meta ad account "
                                         "(meta / meta_<id>). Default: the first Google Ads account"}
GOOGLE_KINDS = tuple(gapi.KINDS) + tuple(gapi.EXTRA_KINDS)
KINDS = sorted(set(GOOGLE_KINDS) | set(meta.GRIDS))
CACHE_NOTE = "Reads Studio's cache only: when nothing is cached it says so and fetches nothing."

TOOLS = [
    {"name": "list_accounts", "description": "Every ad account Studio knows: Google Ads accounts (read through the Google Ads API; "
                                             "last_download is Editor's download time when Google Ads Editor also holds the account) and "
                                             "Meta ad accounts (cached_only = no Meta token on this Mac, cached data still readable). Use "
                                             "these keys for account everywhere. Reads local files only.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "list_entities",
     "description": "Rows of one entity type, with any draft changes overlaid (_draft = pending draft value, _new = row added "
                    "in the draft). Google: from Studio's Google Ads API cache (or Editor's copy if the user set the account to Editor "
                    "in Studio Settings), with stats for the period: cost, impressions, clicks, ctr, cpc, conversions, conv_value, "
                    "cost_per_conv. Google kinds: " + ", ".join(GOOGLE_KINDS) + " (asset_group_assets, user_lists and recommendations are "
                    "API only). Meta kinds: " + ", ".join(meta.GRIDS) + "; stats for the period on spend, impressions, reach, frequency, "
                    "clicks, ctr, cpc, cpm, results, cost_per_result. audiences and leadforms are read only and personal data: UK GDPR "
                    "check needed before any upload/download; never download leads. The account's structure comes from the cache only "
                    "(nothing cached: call refresh_data first); stats for a period nobody has viewed yet are fetched once, read-only, "
                    "then cached. Scope with campaign / ad_group names (substring match on campaign if not exact); for Meta ad_group = ad set.",
     "inputSchema": {"type": "object", "properties": {
         "kind": {"type": "string", "enum": KINDS}, "account": ACCT,
         "campaign": {"type": "string"}, "ad_group": {"type": "string"},
         "status": {"type": "string", "enum": ["active", "enabled", "all"], "description": "active = not removed (default)"},
         "days": {"type": "integer", "enum": [7, 14, 30, 90], "description": "stats period in days (default 7)"},
         "start": {"type": "string", "description": "custom stats period start YYYY-MM-DD (with end, instead of days)"},
         "end": {"type": "string", "description": "custom stats period end YYYY-MM-DD"},
         "search": {"type": "string", "description": "case-insensitive text filter over all columns"},
         "limit": {"type": "integer", "description": "default 200"}}, "required": ["kind"]}},
    {"name": "get_draft", "description": "The account's shared draft: changes the user has not pushed live (or sent to Editor) yet, "
                                         "with problems and warnings per item.",
     "inputSchema": {"type": "object", "properties": {"account": ACCT}}},
    {"name": "add_to_draft",
     "description": "Add changes to the shared draft. They appear live in the user's Ads Studio for review; the user pushes them "
                    "live through the Google Ads / Meta API with their own Push live button. Nothing is written to Google, Meta or "
                    "Editor from here. A change that can't be read (unknown type, missing required field, not an object) is refused "
                    "and nothing from that call is added; a change that reads fine but breaks a rule (length, banned word ...) is "
                    "added with its problems listed: fix it (remove + re-add) before the user can push it.\n\n"
                    + core.CHANGE_REFERENCE + "\n\n" + meta.CHANGE_REFERENCE + "\n\n" + meta_tests.CHANGE_REFERENCE,
     "inputSchema": {"type": "object", "properties": {
         "account": ACCT, "changes": {"type": "array", "items": {"type": "object"}},
         "note": {"type": "string", "description": "why - shown to the user next to each change"}}, "required": ["changes"]}},
    {"name": "remove_from_draft", "description": "Remove draft items by id.",
     "inputSchema": {"type": "object", "properties": {"account": ACCT, "ids": {"type": "array", "items": {"type": "string"}}},
                     "required": ["ids"]}},
    {"name": "check_draft", "description": "Google: dry run of every draft item on a throwaway copy of Google Ads Editor's database "
                                           "(needs Editor on this Mac; without it use preview_live_push, which asks Google). Meta: re-run "
                                           "Studio's checks (copy rules, dead links, prices vs the linked page) on the cached Meta data.",
     "inputSchema": {"type": "object", "properties": {"account": ACCT}}},
    {"name": "send_draft_to_editor",
     "description": "Optional Editor route (Google only, needs Google Ads Editor on this Mac): write the whole draft into Editor as "
                    "pending changes (closes and reopens Editor, ~15s). Only call this when the user has explicitly said to send it "
                    "to Editor in this conversation. Nothing goes live until they Post in Editor. In a team: needs the user's one-line "
                    "why, and when the team requires approval for Ads only approved changes are sent.",
     "inputSchema": {"type": "object", "properties": {"account": ACCT, "title": {"type": "string"},
                                                      "why": {"type": "string", "description": "the user's one-line why (team)"},
                                                      "user_confirmed": {"type": "boolean", "description": "must be true"}},
                     "required": ["user_confirmed"]}},
    {"name": "preview_live_push",
     "description": "Ask Google or Meta to check the draft as a live push (validate only - nothing changes): shows live before -> after "
                    "values, spend-raising changes and permanent removals. Pushing live itself is only done by the user's "
                    "Push live button in Ads Studio.",
     "inputSchema": {"type": "object", "properties": {"account": ACCT, "enable_new": {"type": "boolean"}}}},
    {"name": "live_push_history", "description": "Recent live pushes from Ads Studio (what went live, when, undone or not).",
     "inputSchema": {"type": "object", "properties": {"account": ACCT}}},
    {"name": "find_location", "description": "Look up Google Ads location IDs by name (Editor's location data when the account reads "
                                             "from Editor, else a read-only Google Ads API lookup).",
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
    {"name": "seo_list_drafts", "description": "The SEO draft (edits, to-dos and new pages not yet published to WordPress), with quick checks per change.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "seo_add_draft",
     "description": "Add one change to the SEO draft (never to WordPress). Edit: {post_id, field, new, note}. Suggestion without text: "
                    "{post_id, field, note, kind: 'todo'}. Same post + field replaces the earlier edit. The user checks and publishes.",
     "inputSchema": {"type": "object", "properties": {"post_id": {"type": "integer"},
                                                      "field": {"type": "string", "enum": ["title", "slug", "seo_title", "meta_desc", "focus_kw", "excerpt", "categories", "content"]},
                                                      "new": {},
                                                      "note": {"type": "string"}, "kind": {"type": "string", "enum": ["edit", "todo"]}},
                     "required": ["post_id", "field"]}},
    {"name": "seo_write_page",
     "description": "Write a NEW page for a topical-map node that has no page yet (state gap). It goes into the SEO draft only, never to "
                    "WordPress: the user reads and edits it in Studio (SEO > Topical map > the node, or Draft) and publishes it with their "
                    "own Publish button, which creates the post and points the node at its URL. One page per node: calling again for the "
                    "same node (or with id) updates it. Fields: title, slug (made from the title if empty), seo_title (30-60 chars), "
                    "meta_desc (70-160), focus_kw, excerpt, categories [ids], content (WordPress HTML: h2/h3, p, ul, a; no h1). "
                    "Follow the node's entity, attribute, macro context and target queries, link to the parent and the nodes in its "
                    "links list, and use only facts you can source. To change an EXISTING post use seo_add_draft (field content etc.).",
     "inputSchema": {"type": "object", "properties": {
         "node_id": {"type": "string"}, "id": {"type": "string", "description": "the new page's draft id, to update it"},
         "title": {"type": "string"}, "slug": {"type": "string"}, "seo_title": {"type": "string"}, "meta_desc": {"type": "string"},
         "focus_kw": {"type": "string"}, "excerpt": {"type": "string"}, "categories": {"type": "array", "items": {"type": "integer"}},
         "content": {"type": "string"}, "note": {"type": "string", "description": "what you did and what to check - shown to the user"}},
         "required": ["node_id"]}},
    # SoMe (organic Facebook, Instagram, LinkedIn). Posting and scheduling are the user's buttons.
    {"name": "seo_map_get",
     "description": "The SEO topical map (Koray method) joined with Search Console + GA4: site (central entity, source context, "
                    "central search intent), every node (id, parent, title, section core|outer, type root|hub|attribute|commercial, "
                    "entity, attribute, macro context, target queries with their own numbers, url, links, clicks, impressions, CTR, "
                    "position, change vs the previous period, sessions, key events, issues, state ok|update|gap), coverage per "
                    "section and pending proposals. Use state='gap' or 'update' to get only the nodes that need work.",
     "inputSchema": {"type": "object", "properties": {
         "days": {"type": "integer", "enum": [7, 28, 90], "description": "period (default 28)"},
         "state": {"type": "string", "enum": ["all", "gap", "update", "ok"]},
         "section": {"type": "string", "enum": ["core", "outer"]},
         "limit": {"type": "integer", "description": "max nodes (default 300)"}}}},
    {"name": "seo_map_unmapped",
     "description": "Search Console demand the topical map does not cover yet: pages with impressions that no node points at, "
                    "with their top queries. Use it to find missing nodes (gaps) when building or extending the map.",
     "inputSchema": {"type": "object", "properties": {"days": {"type": "integer", "enum": [7, 28, 90]}, "limit": {"type": "integer"}}}},
    {"name": "seo_map_propose",
     "description": "Propose changes to the topical map. Nothing changes until the user accepts the proposal in Studio (SEO > "
                    "Topical map). ops is a list of: {op:'site', site:{central_entity, source_context, central_search_intent, "
                    "exclude:[{path, why}] (pages left out on purpose, e.g. translations; '/de/' covers everything under it)}} | "
                    "{op:'add', ref:'<temp name, optional>', node:{title, parent (node id or a ref from this batch), section "
                    "core|outer, type root|hub|attribute|commercial, entity, attribute, macro_context, queries [..], intent, "
                    "priority 1-4, url, links [{to, anchor}], note}} | {op:'update', id, node:{fields to change}} | "
                    "{op:'remove', id}. Build it the Koray way: one central entity for the site, core section = what the source "
                    "context sells, outer section = the attributes that build authority, one entity-attribute pair per node, "
                    "target queries from real Search Console demand where it exists.",
     "inputSchema": {"type": "object", "properties": {"ops": {"type": "array", "items": {"type": "object"}},
                                                      "note": {"type": "string", "description": "why, shown to the user"}},
                     "required": ["ops"]}},
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
                                           "warning (Google keeps change history about 30 days). Read tools use this cached data. Reads local files only.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "refresh_data", "description": "Fetch one source again, read-only towards every service (it only updates Studio's own cache): "
                                            "google (account: a Google key from list_accounts, none = every Google account), meta (account: "
                                            "a Meta key from list_accounts, none = every Meta account), email, seo (account gsc, ga4 or wp, "
                                            "none = all), social, activity (syncs the archive), fx, or all. Each result says what failed "
                                            "(error / errors); a failed source keeps its old data. Use when the user asks for fresh data "
                                            "or a read tool says nothing is cached.",
     "inputSchema": {"type": "object", "properties": {
         "source": {"type": "string", "enum": ["all", "google", "meta", "email", "seo", "social", "activity", "fx"]},
         "account": {"type": "string", "description": "optional: one account key (see list_accounts) or SEO part"},
         "days": {"type": "integer", "description": "stats window to fetch with Google (default 7)"},
         "start": {"type": "string", "description": "custom Google stats window start YYYY-MM-DD (with end, instead of days)"},
         "end": {"type": "string", "description": "custom Google stats window end YYYY-MM-DD"}}, "required": ["source"]}},
]
SETUP_TOOLS = [
    {"name": "setup_status", "description": "Which keys Marketing Studio has (Meta, Google Ads, Google sign-in, Email, WordPress, LinkedIn): set or not set per key. Never returns a value.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "setup_import_env_file",
     "description": "Writes keys: load Marketing Studio's keys from a .env file on this Mac (KEY=value lines, e.g. META_ACCESS_TOKEN, META_AD_ACCOUNT_ID, "
                    "GADS_DEVELOPER_TOKEN, GADS_LOGIN_CUSTOMER_ID, GADS_OPERATING_CUSTOMER_ID, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, "
                    "GOOGLE_REFRESH_TOKEN, GOOGLE_SERVICE_ACCOUNT_FILE=<path to a service-account json>, EMAIL_*, WP_STUDIO_*, LINKEDIN_*). "
                    "Secrets go into the macOS Keychain. Returns only which keys were imported and which lines were ignored, never values. "
                    "Only run this when the user asks to set Studio up from a file.",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string", "description": "absolute path to the .env file"}}, "required": ["path"]}},
]
TEAM_TOOLS = [
    {"name": "team_status", "description": "Team sync status: whether this Studio is in a team, the team and your role, last sync, local "
                                           "changes waiting to be sent, remote changes waiting, open conflicts, the active changeset.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "team_sync", "description": "Sync the drafts with the team server: sends this Mac's draft changes to the team and receives "
                                         "teammates' draft changes (field-level merge; clashing edits become conflicts the user resolves in "
                                         "Studio). Draft level only: nothing goes live anywhere. Only when the user asks.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "reviews_list", "description": "Team reviews (approvals): ones waiting for the user (to_me), the user's own requests "
                                            "(mine) or all, with status (open, approved, changes requested, withdrawn) and which "
                                            "items went stale (changed after approval). Read-only.",
     "inputSchema": {"type": "object", "properties": {"filter": {"type": "string", "enum": ["to_me", "mine", "all"]}}}},
    {"name": "review_request", "description": "Send the user's own changeset (the active one, or changeset_id) for approval to "
                                              "named reviewers (member ids or names; approvers, admins or owners); they are notified. Syncs first. "
                                              "Only when the user asks. You can't approve or decide reviews: reviewers do that "
                                              "in Studio.",
     "inputSchema": {"type": "object", "properties": {
         "reviewers": {"type": "array", "items": {"type": ["integer", "string"]}, "description": "member ids or names"},
         "note": {"type": "string"}, "changeset_id": {"type": "string"}}, "required": ["reviewers"]}},
]
SHARED_TOOLS = [
    {"name": "team_settings_get", "description": "The settings that apply in Studio now: copy rules (per area, block or warn), SEO "
                                                 "sites, test email addresses, SoMe slots / UTM / late posting and the calendar "
                                                 "limits, each with whether it is set by the team or by this Mac. Read only.",
     "inputSchema": {"type": "object", "properties": {"key": {"type": "string", "description": "one key (copy_rules, seo_sites, "
                                                              "email_test_addresses, social, calendar_limits); none = all"}}}},
    {"name": "calendar_list", "description": "The shared calendar: SoMe posts, email campaigns and SEO publishes with a time between "
                                             "start and end, each with channel, status and who owns it. Read only.",
     "inputSchema": {"type": "object", "properties": {"start": {"type": "string", "description": "ISO date, default this Monday"},
                                                      "end": {"type": "string", "description": "ISO date, default start + 28 days"}}}},
    {"name": "library_list", "description": "The team library: approved headlines, CTAs, image URLs, UTM presets and email blocks.",
     "inputSchema": {"type": "object", "properties": {"kind": {"type": "string", "enum": ["headline", "cta", "image", "utm", "email_block"]},
                                                      "q": {"type": "string", "description": "text to look for"}}}},
]


class NotCached(ValueError):
    """A read tool found nothing in Studio's cache. Read tools never fetch on their own: the message says how to."""


def _key_of(cid):
    """The list_accounts key of a Google customer id (for messages)."""
    return next((k for k, v in gae.ACCOUNTS.items() if v == cid), cid)


def _need_google_cache(cid):
    if gapi._read(cid, "base") is None:
        k = _key_of(cid)
        raise NotCached(f"Nothing cached for Google Ads account {k} yet, so nothing was read. Call refresh_data with source "
                        f"google and account {k} (read-only), or ask the user to press Refresh in Studio.")


def _need_meta_cache(key=None):
    """After meta.use(): the account's structure must be cached (meta.load() would otherwise pull it from Meta)."""
    if meta._cached_copy(None)[0] is None:
        try:
            k = key or meta.key_for(meta.act())
        except Exception:  # noqa: BLE001
            k = key or "meta"
        raise NotCached(f"Nothing cached for Meta account {k} yet, so nothing was read. Call refresh_data with source meta "
                        f"and account {k} (read-only), or ask the user to press Refresh in Studio.")


# [U28] add_to_draft: a change Studio can't read is refused (never stored); one that reads but breaks a rule is stored
# with its problems, as before.
GOOGLE_TYPES = tuple(sorted(set(gae.SUPPORTED) | {"pmax_asset", audiences.TYPE} | set(demandgen.TYPES)))


def _changes_list(changes):
    if isinstance(changes, dict):
        changes = [changes]
    if isinstance(changes, str):
        try:
            changes = json.loads(changes)
        except ValueError:
            raise ValueError("changes must be a list of change objects (JSON), not text") from None
        changes = [changes] if isinstance(changes, dict) else changes
    if not isinstance(changes, list) or not changes:
        raise ValueError("changes must be a non-empty list of change objects")
    return changes


def _unreadable(changes, types, read):
    """-> one plain line per change that can't be read (not an object, no / unknown type, a required field missing)."""
    out = []
    for i, c in enumerate(changes, 1):
        n = f"change {i}"
        if not isinstance(c, dict):
            out.append(f"{n} is not an object")
            continue
        t = c.get("type")
        if not t:
            out.append(f"{n} has no type")
            continue
        if t not in types:
            out.append(f"{n}: unknown type {t!r}")
            continue
        try:
            read(c)
        except KeyError as ex:
            out.append(f"{n} ({t}): required field {ex} is missing")
        except Exception as ex:  # noqa: BLE001 - anything else that stops Studio reading it
            out.append(f"{n} ({t}): can't be read ({ex})")
    return out


def _refuse_unreadable(changes, types, read):
    bad = _unreadable(changes, types, read)
    if bad:
        typ = any("type" in x and "(" not in x for x in bad)  # a missing / unknown type: list the ones Studio knows
        raise ValueError("Nothing added to the draft: " + "; ".join(bad) + (". Types: " + ", ".join(types) if typ else "")
                         + ". Fix these and send the changes again.")


def _google_read(c):
    if not core.live_only(c):
        gae.plan_rows(c)


def call_shared(name, a):
    import shared_calendar  # noqa: PLC0415
    import team_settings  # noqa: PLC0415
    if name == "team_settings_get":
        info = team_settings.info()
        keys = {k: {x: v[x] for x in ("label", "value", "source")} for k, v in info["keys"].items()
                if not a.get("key") or k == a["key"]}
        if a.get("key") and not keys:
            raise ValueError(f"unknown key {a['key']}")
        return {"joined": info["joined"], "keys": keys}
    if name == "calendar_list":
        return shared_calendar.calendar_list(a.get("start"), a.get("end"))
    return team_settings.library_list(a.get("kind"), a.get("q"))


# [team live] activity, comments and mentions (team_live.py). Comments Claude adds are marked "Claude: " for the team.
ITEM_REF = {"coll": {"type": "string", "description": "the item's collection, e.g. ads:<account>, email, seo:posts, seo:new_posts, "
                                                       "seo:map_nodes, social"},
            "id": {"type": "string", "description": "the item id inside it (Ads: campaign:<id>, ad_group:<id>, ad:<id> ...)"}}
TEAM_LIVE_TOOLS = [
    {"name": "team_activity", "description": "The team's activity feed, newest first: edit sessions (who changed how many drafts "
                                             "where), pushes, publishes, comments, claims, joins. Optional filters: person (member id), area.",
     "inputSchema": {"type": "object", "properties": {"person": {"type": "string"}, "area": {"type": "string"},
                                                      "limit": {"type": "integer"}}}},
    {"name": "comments_list", "description": "The comment thread on one item (comments, replies, resolved or not, @mentions).",
     "inputSchema": {"type": "object", "properties": ITEM_REF, "required": ["coll", "id"]}},
    {"name": "comment_add", "description": "Post a comment (or a reply with parent) on an item to the team server, on the user's behalf; "
                                           "teammates see it and anyone mentioned is notified. "
                                           "Mention people with @name (their name without spaces) or @ the part of their email before the @. "
                                           "The comment is marked as written by Claude.",
     "inputSchema": {"type": "object", "properties": {**ITEM_REF, "body": {"type": "string"}, "parent": {"type": "integer"}},
                     "required": ["coll", "id", "body"]}},
    {"name": "mentions_list", "description": "Comments that mention the user (unread first).",
     "inputSchema": {"type": "object", "properties": {"unread_only": {"type": "boolean"}}}},
]
# [team notify] the user's notification bell (team_notify.py), read-only: nothing here marks, sends or changes settings
NOTIFY_TOOLS = [
    {"name": "notifications_list", "description": "The user's team notifications (their bell in Studio), unread first: "
                                                  "reviews addressed to them, @mentions, their own requests approved or "
                                                  "sent back, items they have pending changed by a teammate, claims on "
                                                  "what they own and pushes in areas they follow. Read-only.",
     "inputSchema": {"type": "object", "properties": {"unread_only": {"type": "boolean"}, "limit": {"type": "integer"}}}},
]
ALERT_TOOLS = [
    # Ads alerts (alerts.py). Read, and local rule config only: no tool checks, sends or changes anything on Google / Meta.
    {"name": "alerts_list",
     "description": "The user's Ads alert rules (Google Ads and Meta: metric, comparison, window, minimum volume, quiet period, "
                    "on or off, hourly or daily check) and the alerts that fired recently (what triggered them, value vs the "
                    "rule, acknowledged or snoozed), plus when each account was last checked. Read-only.",
     "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "description": "fired alerts to return (default 50)"},
                                                      "unacknowledged_only": {"type": "boolean"}}}},
    {"name": "alert_rule_add",
     "description": "Add an alert rule to the user's local Studio config (nothing is sent or changed on Google or Meta). "
                    "metric: spend, clicks, impressions, conversions, cpc, ctr (%), cpa, roas, spend_vs_budget (% of the daily "
                    "budget), no_delivery (active but no impressions for 24 hours), disapproved_ads (count), learning_limited "
                    "(Meta ad sets). op: above, below, up_pct, down_pct (change vs the previous comparable period). window: "
                    "today, yesterday, last_7_days. level: account, campaign, adset (Meta). Rules are checked hourly "
                    "(conversions, CPA, ROAS daily) when Refresh data is automatic, else when the user presses Check now or "
                    "refreshes data.",
     "inputSchema": {"type": "object", "properties": {
         "metric": {"type": "string"}, "op": {"type": "string"}, "value": {"type": "number"},
         "window": {"type": "string"}, "level": {"type": "string", "description": "account (default), campaign or adset"},
         "account": {"type": "string", "description": "account id or key (see list_accounts); none = every account"},
         "platform": {"type": "string", "description": "any (default), google or meta: for rules on every account"},
         "object_id": {"type": "string", "description": "one campaign or ad set id; none = each one at that level"},
         "min": {"type": "object", "description": "minimum volume in the window, e.g. {\"conversions\": 10}"},
         "quiet_hours": {"type": "number", "description": "fire at most once per this many hours per object (default 24)"},
         "name": {"type": "string"}, "enabled": {"type": "boolean", "description": "default true"}},
         "required": ["metric"]}},
]
ALERT_NAMES = {t["name"] for t in ALERT_TOOLS}


def call_alerts(name, a):
    import alerts  # noqa: PLC0415
    if name == "alerts_list":
        v = alerts.view()
        fired = [x for x in v["alerts"] if not (a.get("unacknowledged_only") and x.get("acked_at"))][:max(1, min(int(a.get("limit") or 50), 200))]
        return {"rules": [{k: r.get(k) for k in ("id", "name", "enabled", "platform", "account", "level", "object_id", "metric", "op",
                                                 "value", "window", "min", "quiet_hours", "cadence", "description", "last_fired")}
                          for r in v["rules"]],
                "alerts": [{k: x.get(k) for k in ("id", "at", "text", "rule_id", "account", "account_name", "platform", "object",
                                                  "metric", "value", "previous", "threshold", "op", "window", "acked_at", "snoozed_until")}
                           for x in fired],
                "unacknowledged": v["badge"]["unacked"], "last_check": v["last_check"], "meta_backoff": v["meta_backoff"],
                "data_policy": v["policy"], "method": v["method"]}
    body = {k: a[k] for k in ("metric", "op", "value", "window", "level", "account", "platform", "object_id", "min",
                              "quiet_hours", "name", "enabled") if k in a}
    return alerts.add_rule(body, by="claude")


META_ACCT = {"type": "string", "description": "Meta ad account key: meta or meta_<id> (default meta)"}
META_TEST_TOOLS = [  # [meta tests] meta_tests.py: two reads and one draft tool; tests reach Meta only by the user's Push live
    {"name": "meta_tests_list",
     "description": "Meta A/B tests and creative tests on the business that owns the ad account (Meta calls them ad studies): "
                    "id, name, label (A/B test or Creative test), status (Scheduled, Running, Ended, Cancelled), start, end, "
                    "cells (name, share, level, ids, names). " + CACHE_NOTE + " refresh=true fetches the list from Meta again (read-only).",
     "inputSchema": {"type": "object", "properties": {"account": META_ACCT,
                                                      "refresh": {"type": "boolean", "description": "fetch from Meta now (read-only); default false = cache only"}}}},
    {"name": "meta_test_results",
     "description": "Results of one Meta test: per cell spend, impressions, clicks, CTR, results and cost per result over the test "
                    "dates; for creative tests Meta's high performers with confidence; a plain verdict and notes (thin data, what "
                    "is compared). A/B tests have no Meta winner in the API: the verdict compares cost per result. " + CACHE_NOTE +
                    " refresh=true fetches this test's results from Meta (read-only).",
     "inputSchema": {"type": "object", "properties": {"account": META_ACCT, "study_id": {"type": "string"},
                                                      "refresh": {"type": "boolean", "description": "fetch from Meta now (read-only); default false = cache only"}},
                     "required": ["study_id"]}},
    {"name": "meta_test_add_to_draft",
     "description": "Draft a Meta test (nothing reaches Meta until the user presses Push live; team approval applies). mode:\n"
                    "  split: test = {name, level: adset|campaign, end_date, start_date?, cells: [{name, ids, percent?}]} "
                    "or {ids: [..]} for one object per cell; 2-5 cells, shares >= 10 adding up to 100 (equal if left out)\n"
                    "  creative: test = {name, ads: [2-7 ad ids], daily_budget (test budget per day) or budget_percent, end_date, start_date?}\n"
                    "  end: test = {study_id, pause?: also pause the ad sets / campaigns / ads in its cells}. Ending is final.\n"
                    "Tests only use objects that already exist on Meta. A test can't be removed from Meta once pushed. "
                    "Duplicating an ad set with one change (variant) is not available yet.\n\n"
                    + meta_tests.CHANGE_REFERENCE,
     "inputSchema": {"type": "object", "properties": {
         "account": META_ACCT, "mode": {"type": "string", "enum": ["split", "creative", "end"]},
         "test": {"type": "object"}, "note": {"type": "string", "description": "why - shown to the user next to each change"}},
         "required": ["mode", "test"]}},
]
META_TEST_NAMES = {t["name"] for t in META_TEST_TOOLS}
DG_TOOLS = [
    # Demand Gen / YouTube video ads (demandgen.py): one read, one draft tool. Uploading a video file to YouTube has no tool:
    # it is only ever the user's own Upload button in Studio.
    {"name": "video_assets_list",
     "description": "YouTube videos of a Google Ads account: video assets in its asset library (asset id, YouTube id, title, "
                    "duration where Google Ads knows it), YouTube uploads made through Google Ads (state, YouTube id), and the "
                    "Demand Gen campaigns, ad groups (with channel controls) and ads (type, status, texts, videos), plus square "
                    "logo images usable in a Demand Gen video ad. " + CACHE_NOTE + " refresh=true reads it from the Google Ads API "
                    "again (read-only). With video (an id or YouTube URL): asks Google Ads now (read-only) what it knows about that one video.",
     "inputSchema": {"type": "object", "properties": {
         "account": ACCT, "refresh": {"type": "boolean", "description": "read again from Google Ads now (read-only); default false = cache only"},
         "video": {"type": "string", "description": "one YouTube id or URL to check"},
         "part": {"type": "string", "enum": ["videos", "uploads", "campaigns", "ad_groups", "ads", "logos", "all"],
                  "description": "only this part (default all)"}}}},
    {"name": "demandgen_add_to_draft",
     "description": "Add Demand Gen / YouTube video ad changes to the Google Ads draft. Draft only: nothing reaches Google until the "
                    "user previews (Google validates) and presses Push live; team approval applies; new campaigns are always Paused, "
                    "new ad groups and ads Paused unless the user ticks otherwise. Experiment trial campaigns are refused. "
                    "Uploading a video file is not possible from here.\n\n" + demandgen.CHANGE_REFERENCE,
     "inputSchema": {"type": "object", "properties": {
         "account": ACCT, "changes": {"type": "array", "items": {"type": "object"}},
         "note": {"type": "string", "description": "why - shown to the user next to each change"}}, "required": ["changes"]}},
]


AUDIENCE_TOOLS = [  # [audiences] one read and one draft tool; audiences reach Google / Meta only by the user's Push live
    {"name": "audiences_list",
     "description": "Audiences of one ad account. Google: every audience list (user list) with type, size (Google's rounded estimate "
                    "and range), membership days, the rule in words, where it is used (campaigns, ad groups, combined lists) and "
                    "whether Studio can edit it. Meta: every custom audience with type, size (Meta shows small audiences only as a "
                    "floor value), retention, lookalike seed and size, where it is used (ad sets, lookalike seeds), status. "
                    "Customer lists are shown read-only: Studio never uploads, reads or changes customer data. " + CACHE_NOTE +
                    " Fresh data: refresh_data for the account.",
     "inputSchema": {"type": "object", "properties": {"account": ACCT}}},
    {"name": "audience_add_to_draft",
     "description": "Draft audience changes (nothing reaches Google or Meta until the user presses Push live; team approval applies). "
                    "Only audience changes are accepted here: Google user_list (website / combined lists) and audience (attach / "
                    "detach lists on a campaign); Meta meta_audience (website, engagement, lookalike; edit; remove) and "
                    "meta_targeting with audiences_add / audiences_remove / excluded_add / excluded_remove. Removing an audience "
                    "that is in use is refused and the users are named. No customer lists, no customer data.\n\n"
                    + "Google lists are named by their exact name, Meta audiences by id (see audiences_list).\nGoogle:\n" + audiences.__doc__.split("Draft change (gae plan format, one JSON object):")[1]
                    + "\nGoogle attach: {\"type\":\"audience\",\"campaign\":\"..\",\"lists\":[\"..\"]} (\"remove\":true to detach)\n"
                    + "\nMeta:\n" + meta_audiences.__doc__.split("Draft change (one JSON object):")[1]
                    + "Engagement events per source: " + "; ".join(f"{k}: {', '.join(v[1])}" for k, v in meta_audiences.ENGAGEMENT.items()),
     "inputSchema": {"type": "object", "properties": {
         "account": ACCT, "changes": {"type": "array", "items": {"type": "object"}},
         "note": {"type": "string", "description": "why - shown to the user next to each change"}}, "required": ["changes"]}},
]
DG_NAMES = {t["name"] for t in DG_TOOLS}


def call_demandgen(name, a):
    if meta.is_meta(a.get("account")):
        raise ValueError("Demand Gen is Google Ads: give a Google Ads account")
    acct = core.resolve(a.get("account"))
    if name == "video_assets_list":
        if a.get("video"):
            return demandgen.video_info(acct, a["video"])
        if not a.get("refresh") and not demandgen._cache(acct).exists():
            raise NotCached("The video library of this account has not been read yet. Call video_assets_list with "
                            "refresh=true (read-only) or ask the user to open the Videos tab.")
        v = demandgen.library(acct, refresh=bool(a.get("refresh")))
        part = a.get("part") or "all"
        return v if part == "all" else {"account": v["account"], "fetched_at": v["fetched_at"], "errors": v["errors"], part: v[part]}
    changes = a.get("changes") or []
    if isinstance(changes, dict):
        changes = [changes]
    bad = sorted({str(c.get("type")) for c in changes if not isinstance(c, dict) or c.get("type") not in demandgen.PUSH_TYPES})
    if bad:
        raise ValueError("demandgen_add_to_draft takes " + ", ".join(demandgen.PUSH_TYPES) + " (not " + ", ".join(bad) + ")")
    return core.add(acct, changes, by="claude", note=a.get("note"))


AUDIENCE_NAMES = {t["name"] for t in AUDIENCE_TOOLS}
AUDIENCE_GOOGLE_TYPES = (audiences.TYPE, "audience")


def call_audiences(name, a):
    changes = a.get("changes") or []
    if isinstance(changes, dict):
        changes = [changes]
    if meta.is_meta(a.get("account")):
        meta.use(a.get("account"))
        if name == "audiences_list":
            _need_meta_cache(a.get("account"))
            return meta_audiences.rows(meta.load())
        bad = [c for c in changes if not (c.get("type") == meta_audiences.TYPE or (
            c.get("type") == "meta_targeting" and any(c.get(k) for k in meta_audiences.AUD_KEYS)
            and "countries" not in c and not c.get("countries_clear") and not any(c.get(k) for k in ("age_min", "age_max"))))]
        if bad:
            raise ValueError("audience_add_to_draft takes Meta audience changes only (meta_audience, or meta_targeting with "
                             "audiences_add / audiences_remove / excluded_add / excluded_remove); use add_to_draft for the rest")
        return meta.add(changes, by="claude", note=a.get("note"))
    acct = core.resolve(a.get("account"))
    if name == "audiences_list":
        _need_google_cache(acct)
        return audiences.rows(acct)
    bad = [c for c in changes if c.get("type") not in AUDIENCE_GOOGLE_TYPES or (c.get("type") == "audience" and c.get("mode"))]
    if bad:
        raise ValueError("audience_add_to_draft takes Google audience changes only (user_list, or audience to attach / detach lists); "
                         "use add_to_draft for the rest")
    return core.add(acct, changes, by="claude", note=a.get("note"))


TOOLS += AREA_TOOLS + DATA_TOOLS + SETUP_TOOLS + TEAM_TOOLS + SHARED_TOOLS + TEAM_LIVE_TOOLS + NOTIFY_TOOLS + ALERT_TOOLS + META_TEST_TOOLS + DG_TOOLS + AUDIENCE_TOOLS
TEAM_LIVE_NAMES = {t["name"] for t in TEAM_LIVE_TOOLS}


def call_team_live(name, a):
    import team_live  # noqa: PLC0415
    if name == "team_activity":
        return team_live.feed(a.get("person"), a.get("area"), min(int(a.get("limit") or 50), 200))
    if name == "comments_list":
        return team_live.comments(a["coll"], a["id"])
    if name == "comment_add":
        return team_live.comment_add(a["coll"], a["id"], a["body"], a.get("parent"), by_claude=True)
    return team_live.mentions(bool(a.get("unread_only")))


def call_team(name):
    import team  # noqa: PLC0415
    import team_live  # noqa: F401,PLC0415 - records what teammates changed during this sync (hovers in Studio)
    if name == "team_status":
        st = team.status()
        st.pop("members", None)
        return st
    r = team.sync()
    r.get("status", {}).pop("members", None)
    return r


def call_reviews(name, a):
    """Reviews for Claude: list, and ask for a review of the user's own changeset. No tool decides a review."""
    import team_reviews  # noqa: PLC0415
    if name == "reviews_list":
        return team_reviews.list_reviews({"filter": a.get("filter") or "to_me"})
    people = team_reviews.reviewers()["reviewers"]
    ids = []
    for r in a.get("reviewers") or []:
        hit = [p for p in people if p["id"] == r or (isinstance(r, str) and str(p["name"]).lower() == r.strip().lower())]
        if not hit:
            raise ValueError(f"{r} is not someone who can approve. Can approve: " + ", ".join(p["name"] for p in people))
        ids.append(hit[0]["id"])
    return team_reviews.request({"changeset": a.get("changeset_id") or True, "reviewers": ids, "note": a.get("note") or ""})


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
    if name == "seo_write_page":
        keys = ("node_id", "id", "title", "slug", "seo_title", "meta_desc", "focus_kw", "excerpt", "categories", "content", "note")
        return seo.save_new_post({k: a[k] for k in keys if k in a}, by="claude")
    if name == "seo_add_draft":
        return seo.add_draft({k: a[k] for k in ("post_id", "field", "new", "note", "kind") if k in a}, by="claude")
    if name in ("seo_map_get", "seo_map_unmapped"):
        q = {"range": str(a.get("days") or 28)}
        r = seo.get_map(q)
        if name == "seo_map_unmapped":
            return {"range": r["range"], "unmapped": r["unmapped"][: a.get("limit") or 50], "errors": r["errors"]}
        rows = r["nodes"]
        if a.get("state") and a["state"] != "all":
            rows = [x for x in rows if x["state"] == a["state"]]
        if a.get("section"):
            rows = [x for x in rows if x["section"] == a["section"]]
        return {"site": r["site"], "coverage": r["coverage"], "proposals": r["proposals"], "range": r["range"], "errors": r["errors"],
                **_limit(rows, {"limit": a.get("limit") or 300})}
    if name == "seo_map_propose":
        from areas import _seo_map as tmap  # noqa: PLC0415
        return tmap.propose(a.get("ops") or [], a.get("note") or "", by="claude")
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


def _meta_tests_cache(name):
    """What meta_tests has cached under name for the current Meta account (memory, then disk), else None. No API calls."""
    hit = meta_tests._mem.get((meta.acct(), name))
    if hit:
        return hit[1]
    try:
        return json.loads(meta_tests._cache_path(name).read_text())["data"]
    except (OSError, ValueError, KeyError):
        return None


def call_meta_tests(name, a):
    meta.use(a.get("account") or "meta")
    refresh = bool(a.get("refresh"))
    if name == "meta_tests_list":
        if refresh:
            return meta_tests.studies(refresh=True)
        raw = _meta_tests_cache("list")
        if raw is None:
            raise NotCached("Meta tests have not been read yet. Call meta_tests_list with refresh=true (read-only) "
                            "or ask the user to open Meta tests in Studio.")
        local = meta_tests._local()
        return sorted((meta_tests._row(x, local) for x in raw), key=lambda r: r.get("start_time") or "", reverse=True)
    if name == "meta_test_results":
        if refresh:
            return meta_tests.results(a["study_id"], refresh=True)
        sid = str(a.get("study_id") or "")
        if not sid.isdigit():
            raise ValueError("study_id is a Meta test id (digits)")
        data = _meta_tests_cache(f"r{sid}")
        if data is None:
            raise NotCached(f"Results of test {sid} have not been read yet. Call again with refresh=true (read-only).")
        return {**data, **meta_tests.verdict(data)}
    return meta_tests.add(a["mode"], a.get("test") or {}, by="claude", note=a.get("note"))


def call(name, a):
    if name in AUDIENCE_NAMES:
        return call_audiences(name, a)
    if name in META_TEST_NAMES:
        return call_meta_tests(name, a)
    if name in AREA_NAMES:
        return call_area(name, a)
    if name in ALERT_NAMES:
        return call_alerts(name, a)
    if name in DG_NAMES:
        return call_demandgen(name, a)
    if name in ("team_status", "team_sync"):
        return call_team(name)
    if name in ("reviews_list", "review_request"):
        return call_reviews(name, a)
    if name in TEAM_LIVE_NAMES:
        return call_team_live(name, a)
    if name == "notifications_list":
        import team_notify  # noqa: PLC0415
        return team_notify.notifications(bool(a.get("unread_only")), min(int(a.get("limit") or 50), 200))
    if name in ("team_settings_get", "calendar_list", "library_list"):
        return call_shared(name, a)
    if name == "setup_status":
        st = appconfig.status()
        return {"keys": {k["name"]: k["set"] for k in st["keys"]}, "google_signed_in": st["google_signed_in"],
                "google_service_account": st["google_service_account"], "ready": st["ready"], "data_folder": str(appconfig.DATA),
                "keychain_error": st.get("keychain_error")}
    if name == "setup_import_env_file":
        return appconfig.import_env_file(a["path"])
    if name == "data_status":
        return datasources.status()
    if name == "refresh_data":
        return datasources.refresh(a.get("source"), a.get("account") or None, **{k: a[k] for k in ("days", "start", "end") if a.get(k)})
    if name == "list_accounts":
        return list_accounts()
    if meta.is_meta(a.get("account")):
        return call_meta(name, a)
    acct = core.resolve(a.get("account"))
    if name == "list_entities":
        if a["kind"] not in GOOGLE_KINDS:
            raise ValueError(f"Google kinds: {', '.join(GOOGLE_KINDS)}")
        if a["kind"] in gapi.EXTRA_KINDS or gapi.use_api(acct):
            _need_google_cache(acct)
        cid, gid = _scope(acct, a.get("campaign"), a.get("ad_group"), gapi.tree_any(acct, "all"))
        rows = gapi.grid_any(acct, a["kind"], cid, gid, a.get("status", "active"), a.get("search"), None,
                             a.get("days"), a.get("start"), a.get("end"))
        limit = a.get("limit") or 200
        return {"total": len(rows), "returned": min(len(rows), limit), "rows": rows[:limit]}
    if name == "get_draft":
        return core.draft(acct)
    if name == "add_to_draft":
        changes = _changes_list(a.get("changes"))
        _refuse_unreadable(changes, GOOGLE_TYPES, _google_read)
        return core.add(acct, changes, by="claude", note=a.get("note"))
    if name == "remove_from_draft":
        return {"removed": core.remove(acct, a["ids"])}
    if name == "check_draft":
        return core.validate(acct)
    if name == "send_draft_to_editor":
        if a.get("user_confirmed") is not True:
            raise ValueError("Ask the user first; call again with user_confirmed=true once they have said to send it.")
        import team_reviews  # noqa: PLC0415 - gated like the Send to Editor button when the team requires approval
        return team_reviews.gated_push("ads", f"ads:{acct}", {"why": a.get("why")}, lambda only: core.push(
            acct, a.get("title"), a.get("why"), only), "Sent to Google Ads Editor", kind="send_to_editor")
    if name == "preview_live_push":
        import live
        return live.preview(acct, bool(a.get("enable_new")))
    if name == "live_push_history":
        import live
        return live.history(acct)
    if name == "find_location":
        return gapi.geo_any(acct, a["name"])
    raise ValueError(f"unknown tool {name}")


def call_meta(name, a):
    meta.use(a.get("account"))
    if name == "list_entities":
        if a["kind"] not in meta.GRIDS:
            raise ValueError(f"Meta kinds: {', '.join(meta.GRIDS)}")
        _need_meta_cache(a.get("account"))
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
        changes = _changes_list(a.get("changes"))
        _refuse_unreadable(changes, tuple(meta.TYPES), meta._normalise)
        return meta.add(changes, by="claude", note=a.get("note"))
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


def list_accounts():
    """Google accounts (API list, Editor's download time when Editor has them) + Meta accounts. Without a Meta token the
    Meta accounts whose data is cached are still listed (cached_only), since the read tools can use that cache."""
    out = gapi.accounts_any()
    ms = meta.accounts()
    if not ms:
        try:
            for x in meta.settings().get("meta_accounts") or []:
                aid = f"meta_{x['act'].replace('act_', '')}"
                c = core.STUDIO_DIR / f"meta_cache_{aid}.json"
                if c.exists():
                    ms.append({"id": aid, "key": meta.key_for(x["act"]), "act": x["act"], "name": x.get("name") or x["act"],
                               "channel": "meta", "last_download": (json.loads(c.read_text()) or {}).get("fetched_at"),
                               "cached_only": True})
        except Exception:  # noqa: BLE001 - no Meta settings at all
            pass
    return out + ms


EDITOR_HINT = {"check_draft": "Use preview_live_push instead: Google checks the draft there without changing anything.",
               "send_draft_to_editor": "The user can push the draft live with their own Push live button in Studio instead."}


def _tool_error(name, ex):
    if isinstance(ex, gae.NoEditor):
        return (f"Error: {name} needs Google Ads Editor on this Mac, and Studio can't find it (not installed, or no account "
                f"downloaded in it). " + EDITOR_HINT.get(name, "Studio reads Google from the Google Ads API without Editor."))
    msg = ex.code if isinstance(ex, SystemExit) and not isinstance(ex.code, int) else ex
    return f"Error: {msg}"


def handle(msg):
    m, mid = msg.get("method"), msg.get("id")
    if m == "initialize":
        return {"protocolVersion": msg.get("params", {}).get("protocolVersion", PROTOCOL), "capabilities": {"tools": {}},
                "serverInfo": {"name": "marketing-studio", "version": "1.3"},
                "instructions": INSTRUCTIONS}
    if m == "tools/list":
        return {"tools": TOOLS}
    if m == "tools/call":
        p = msg["params"]
        try:
            out = call(p["name"], p.get("arguments") or {})
            return {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False, default=str)}]}
        except (Exception, SystemExit) as ex:  # noqa: BLE001 - tool errors (gae's sys.exit too) go back to the model, never end the server
            return {"content": [{"type": "text", "text": _tool_error(p.get("name"), ex)}], "isError": True}
    if m == "ping":
        return {}
    if mid is not None:
        raise LookupError(m)
    return None


def main():
    try:  # once per install (see team_settings.first_run)
        import team_settings  # noqa: PLC0415
        team_settings.first_run()
    except Exception:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
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
        except (Exception, SystemExit) as ex:  # noqa: BLE001 - one bad request never ends the server
            traceback.print_exc(file=sys.stderr)
            res = {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32603, "message": str(ex)}}
        sys.stdout.write(json.dumps(res, ensure_ascii=False, default=str) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
