"""The copy rules Studio had built in before copy rules became a setting (team_settings.py, key "copy_rules").

They are not the default: a new install starts with no copy rules. They are copied once into the local settings of an
install that has a config.json (it was set up before this version), on its first start (team_settings.first_run), so
the checks on that Mac stay as they were: the same matches, severities and order (messages are worded neutrally). After that
they live in <data>/studio_settings.json, where Calendar > Team settings edits them (or the team's copy replaces them).
Nothing else reads this module.

level "block" = a problem that stops the action (Ads, SoMe) or an error (Email, SEO); "warn" = shown, never blocks.
areas: google_ads, meta_ads, email, seo, social ("ads" = both ads); empty = everywhere. Rules run in list order.
Flags (see team_settings.clean_rule): early (Google Ads: before the length limits), raw (SEO: the whole field, markup
included, not only the visible text), quote false (SEO: no 'Found: "..."' after the message).
"""
EM_DASH = "\\u2014"
EMOJI = "[\\U0001F000-\\U0001FAFF\\u2600-\\u27BF\\u2B00-\\u2BFF\\uFE0F\\u200D]"  # SoMe and Email
SEO_EMOJI = "[\\U0001F000-\\U0001FAFF\\u2600-\\u27BF\\u2B50\\u2B55\\u2934\\u2935\\u3030\\u303D\\uFE0F]"
PRICE = r"(?:£|\$|€|usd|gbp|eur|price|prices|priced|from|per|rate|rates|cost|costs)"

RULES = [
    # Google Ads (gae.lint: every column, including names and URLs)
    {"id": "g_em_dash", "level": "block", "pattern": EM_DASH, "message": "contains an em dash", "areas": ["google_ads"], "early": True},
    {"id": "g_place_ban", "level": "block", "pattern": "bahamas", "message": "mentions Bahamas (not allowed)", "areas": ["google_ads"]},
    # Meta ads, SoMe: blocks
    {"id": "m_em_dash", "level": "block", "pattern": EM_DASH, "message": "em dash - use a normal dash", "areas": ["meta_ads"]},
    {"id": "s_em_dash", "level": "block", "pattern": EM_DASH, "message": "em dash: use a normal dash (-) or rewrite", "areas": ["social"]},
    {"id": "place_ban", "level": "block", "pattern": r"\bbahamas\b", "message": "never mention the Bahamas", "areas": ["meta_ads", "social"]},
    {"id": "s_emoji", "level": "block", "pattern": EMOJI, "message": "no emojis in posts", "areas": ["social"]},
    {"id": "s_promise", "level": "block",
     "pattern": r"\bguarantee(d|s)?\b|\byou will see\b|\byou'll see\b|\bwill definitely see\b|\b100% (chance|sure)\b",
     "message": "never promise sightings, weather or conditions", "areas": ["social"]},
    # Ads (Google and Meta) and SoMe: warnings
    {"id": "ads_cabins", "level": "warn", "pattern": r"\bcabins?\b", "message": 'say "spaces", not "cabins"', "areas": ["ads"]},
    {"id": "ads_guarantee", "level": "warn", "pattern": r"\bguarantee", "message": "never guarantee marine life or conditions",
     "areas": ["ads"]},
    {"id": "yacht", "level": "warn", "pattern": r"\byachts?\b", "message": 'say "vessel", not "yacht"', "areas": ["ads", "social"]},
    {"id": "ads_expeditions", "level": "warn", "pattern": r"\bexpeditions?\b", "message": 'no "Expeditions" in ad copy',
     "areas": ["ads"]},
    {"id": "ads_emoji", "level": "warn", "pattern": "[\\U0001F300-\\U0001FAFF\\u2600-\\u27BF]", "message": "no emojis in marketing copy",
     "areas": ["ads"]},
    {"id": "ownership", "level": "warn", "pattern": r"\bour own (vessels?|boats?|fleet)\b|\bwe own\b",
     "message": "no ownership claims about vessels", "areas": ["meta_ads", "social"]},
    {"id": "s_expeditions", "level": "warn", "pattern": r"\bexpeditions?\b", "message": 'no "Expeditions" in copy',
     "areas": ["social"]},
    {"id": "s_cabin_price", "level": "warn",
     "pattern": r"(?:[$£€]\s?\d[\d,.]*|\d[\d,.]*\s?(?:USD|GBP|EUR)).{0,60}\bcabins?\b|\bcabins?\b.{0,60}(?:[$£€]\s?\d|\d\s?(?:USD|GBP|EUR))",
     "message": 'prices are per space: say "spaces", not "cabins"', "areas": ["social"]},
    # SEO
    {"id": "seo_em_dash", "level": "block", "pattern": EM_DASH, "message": "Has an em dash. Use a regular dash or rewrite.",
     "areas": ["seo"], "raw": True, "quote": False},
    {"id": "seo_emoji", "level": "block", "pattern": SEO_EMOJI, "message": "Has an emoji. Use none.", "areas": ["seo"],
     "quote": False},
    {"id": "seo_place_ban", "level": "block", "pattern": r"\bbahamas\b", "message": 'Never use "Bahamas".', "areas": ["seo"]},
    {"id": "seo_yacht", "level": "warn", "pattern": r"\byachts?\b", "message": 'Say "vessel", not "yacht".', "areas": ["seo"]},
    {"id": "seo_promise", "level": "warn",
     "pattern": r"\b(guarantee[sd]?|guaranteed sightings?|you will (?:see|spot|meet)|you'll (?:see|spot|meet)|always see|certain to see|sure to see)\b",
     "message": "Do not promise sightings, weather or conditions.", "areas": ["seo"]},
    {"id": "seo_cabin_price", "level": "warn", "pattern": rf"(\bcabins?\b[^.]{{0,40}}{PRICE}\b|{PRICE}\b[^.]{{0,40}}\bcabins?\b)",
     "message": 'For pricing, say "spaces", not "cabins".', "areas": ["seo"]},
    # Email (your own extra rules stay in <data>/email_copy_rules.json, read after these, as before)
    {"id": "e_emoji", "level": "block", "pattern": EMOJI, "message": "No emojis.", "areas": ["email"]},
    {"id": "e_em_dash", "level": "block", "pattern": EM_DASH, "message": "No em dash; use a dash (-) or rewrite.", "areas": ["email"]},
    {"id": "e_promise", "level": "block", "pattern": r"\bguarantee(d|s)?\b", "message": 'No promises ("guaranteed").', "areas": ["email"]},
]
