"""The copy rules Studio had built in before copy rules became a setting (team_settings.py, key "copy_rules").

They are not the default: a new install starts with no copy rules. They are copied once into the local settings of
an install that existed before this change (it has a config.json or a data folder with files), so the checks on that
Mac do not change. After that they live in <data>/studio_settings.json, where Studio > Calendar > Team settings edits
them (or the team's copy replaces them). Nothing else reads this module.

level "block" = a problem that stops the action (Ads, SoMe) or an error (Email, SEO); "warn" = shown, never blocks.
areas: where the rule applies ("ads" = Google Ads and Meta ads, "email", "seo", "social"); empty = everywhere.
"""
EM_DASH = "\\u2014"
EMOJI = "[\\U0001F000-\\U0001FAFF\\u2600-\\u27BF\\u2B00-\\u2BFF\\uFE0F\\u200D]"
PRICE = r"(?:£|\$|€|usd|gbp|eur|price|prices|priced|from|per|rate|rates|cost|costs)"

RULES = [
    # everywhere
    {"id": "em_dash", "level": "block", "pattern": EM_DASH, "message": "em dash: use a normal dash (-) or rewrite", "areas": []},
    {"id": "place_ban", "level": "block", "pattern": r"\bbahamas\b", "message": 'never mention "Bahamas"',
     "areas": ["ads", "seo", "social"]},
    # Ads (Google and Meta)
    {"id": "ads_cabins", "level": "warn", "pattern": r"\bcabins?\b", "message": 'say "spaces", not "cabins"', "areas": ["ads"]},
    {"id": "ads_guarantee", "level": "warn", "pattern": r"\bguarantee", "message": "never guarantee marine life or conditions",
     "areas": ["ads"]},
    {"id": "ads_yacht", "level": "warn", "pattern": r"\byachts?\b", "message": 'say "vessel", not "yacht"', "areas": ["ads"]},
    {"id": "ads_expeditions", "level": "warn", "pattern": r"\bexpeditions?\b", "message": 'no "Expeditions" in ad copy',
     "areas": ["ads"]},
    {"id": "ads_emoji", "level": "warn", "pattern": "[\\U0001F300-\\U0001FAFF\\u2600-\\u27BF]", "message": "no emojis in marketing copy",
     "areas": ["ads"]},
    {"id": "ownership", "level": "warn", "pattern": r"\bour own (vessels?|boats?|fleet)\b|\bwe own\b",
     "message": "no ownership claims about vessels", "areas": ["ads", "social"]},
    # SoMe
    {"id": "social_emoji", "level": "block", "pattern": EMOJI, "message": "no emojis in posts", "areas": ["social"]},
    {"id": "social_promise", "level": "block",
     "pattern": r"\bguarantee(d|s)?\b|\byou will see\b|\byou'll see\b|\bwill definitely see\b|\b100% (chance|sure)\b",
     "message": "never promise sightings, weather or conditions", "areas": ["social"]},
    {"id": "social_yacht", "level": "warn", "pattern": r"\byachts?\b", "message": 'say "vessel", not "yacht"', "areas": ["social"]},
    {"id": "social_expeditions", "level": "warn", "pattern": r"\bexpeditions?\b", "message": 'no "Expeditions" in copy',
     "areas": ["social"]},
    {"id": "social_cabin_price", "level": "warn",
     "pattern": r"(?:[$£€]\s?\d[\d,.]*|\d[\d,.]*\s?(?:USD|GBP|EUR)).{0,60}\bcabins?\b|\bcabins?\b.{0,60}(?:[$£€]\s?\d|\d\s?(?:USD|GBP|EUR))",
     "message": 'prices are per space: say "spaces", not "cabins"', "areas": ["social"]},
    # SEO
    {"id": "seo_emoji", "level": "block", "pattern": EMOJI, "message": "Has an emoji. Use none.", "areas": ["seo"]},
    {"id": "seo_yacht", "level": "warn", "pattern": r"\byachts?\b", "message": 'Say "vessel", not "yacht".', "areas": ["seo"]},
    {"id": "seo_promise", "level": "warn",
     "pattern": r"\b(guarantee[sd]?|guaranteed sightings?|you will (?:see|spot|meet)|you'll (?:see|spot|meet)|always see|certain to see|sure to see)\b",
     "message": "Do not promise sightings, weather or conditions.", "areas": ["seo"]},
    {"id": "seo_cabin_price", "level": "warn", "pattern": rf"(\bcabins?\b[^.]{{0,40}}{PRICE}\b|{PRICE}\b[^.]{{0,40}}\bcabins?\b)",
     "message": 'For pricing, say "spaces", not "cabins".', "areas": ["seo"]},
    # Email (your own extra rules stay in <data>/email_copy_rules.json, read as before)
    {"id": "email_emoji", "level": "block", "pattern": EMOJI, "message": "No emojis.", "areas": ["email"]},
    {"id": "email_promise", "level": "block", "pattern": r"\bguarantee(d|s)?\b", "message": 'No promises ("guaranteed").',
     "areas": ["email"]},
]
