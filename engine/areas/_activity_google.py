"""Activity area: Google Ads change history (the change_event resource), read-only.

Google rules (developers.google.com/google-ads/api/docs/change-event, read 10 November 2026):
- the WHERE must bound change_event.change_date_time, inside the last 30 days
- LIMIT is required, at most 10,000 rows per query. For more, query again with the window ending at the oldest
  row returned (rows are de-duplicated by resource_name)
- a change can take up to 3 minutes to show up; not every change has an event; Editor is in the client type list
  but Google says Editor changes are not reported as such (they tend to show as OTHER)
change_date_time is in the account's time zone (customer.time_zone).
"""
import datetime as dt
import json
import re
from zoneinfo import ZoneInfo

from . import _activity_periods as per

MAX_ROWS = 10000
KEEP_DAYS = 30
FIELDS = ("change_event.resource_name, change_event.change_date_time, change_event.change_resource_name, change_event.user_email, "
          "change_event.client_type, change_event.change_resource_type, change_event.resource_change_operation, "
          "change_event.changed_fields, change_event.old_resource, change_event.new_resource, campaign.name, ad_group.name")

CLIENT = {"GOOGLE_ADS_WEB_CLIENT": "Google Ads web", "GOOGLE_ADS_API": "API", "GOOGLE_ADS_EDITOR": "Google Ads Editor",
          "GOOGLE_ADS_SCRIPTS": "Google Ads scripts", "GOOGLE_ADS_AUTOMATED_RULE": "Automated rule",
          "GOOGLE_ADS_BULK_UPLOAD": "Bulk upload", "GOOGLE_ADS_MOBILE_APP": "Google Ads app",
          "GOOGLE_ADS_RECOMMENDATIONS": "Recommendations", "GOOGLE_ADS_RECOMMENDATIONS_SUBSCRIPTION": "Auto-applied recommendations",
          "SEARCH_ADS_360_SYNC": "Search Ads 360", "SEARCH_ADS_360_POST": "Search Ads 360", "INTERNAL_TOOL": "Google internal tool",
          "OTHER": "Other (often Google Ads Editor)", "UNKNOWN": "Unknown", "UNSPECIFIED": "Unknown"}
RESOURCE = {"CAMPAIGN": "Campaign", "AD_GROUP": "Ad group", "AD_GROUP_AD": "Ad", "AD": "Ad", "CAMPAIGN_BUDGET": "Budget",
            "AD_GROUP_CRITERION": "Ad group targeting", "CAMPAIGN_CRITERION": "Campaign targeting", "ASSET": "Asset",
            "CAMPAIGN_ASSET": "Campaign asset", "AD_GROUP_ASSET": "Ad group asset", "CUSTOMER_ASSET": "Account asset",
            "ASSET_SET": "Asset set", "ASSET_SET_ASSET": "Asset set asset", "CAMPAIGN_ASSET_SET": "Campaign asset set",
            "ASSET_GROUP": "Asset group", "ASSET_GROUP_ASSET": "Asset group asset", "ASSET_GROUP_SIGNAL": "Asset group signal",
            "ASSET_GROUP_LISTING_GROUP_FILTER": "Listing group", "AD_GROUP_BID_MODIFIER": "Bid adjustment",
            "CAMPAIGN_SHARED_SET": "Shared list link", "SHARED_SET": "Shared list", "SHARED_CRITERION": "Shared list keyword",
            "CUSTOMER": "Account", "AD_GROUP_FEED": "Ad group feed", "CAMPAIGN_FEED": "Campaign feed", "FEED": "Feed",
            "FEED_ITEM": "Feed item", "CUSTOMER_FEED": "Account feed", "CAMPAIGN_CONVERSION_GOAL": "Conversion goal"}
ROOT = {"CAMPAIGN": "campaign", "AD_GROUP": "adGroup", "AD_GROUP_AD": "adGroupAd", "AD": "ad", "CAMPAIGN_BUDGET": "campaignBudget",
        "AD_GROUP_CRITERION": "adGroupCriterion", "CAMPAIGN_CRITERION": "campaignCriterion", "ASSET": "asset",
        "CAMPAIGN_ASSET": "campaignAsset", "AD_GROUP_ASSET": "adGroupAsset", "CUSTOMER_ASSET": "customerAsset",
        "ASSET_SET": "assetSet", "ASSET_SET_ASSET": "assetSetAsset", "CAMPAIGN_ASSET_SET": "campaignAssetSet", "ASSET_GROUP": "assetGroup",
        "ASSET_GROUP_ASSET": "assetGroupAsset", "ASSET_GROUP_SIGNAL": "assetGroupSignal",
        "ASSET_GROUP_LISTING_GROUP_FILTER": "assetGroupListingGroupFilter", "AD_GROUP_BID_MODIFIER": "adGroupBidModifier",
        "CAMPAIGN_SHARED_SET": "campaignSharedSet", "SHARED_SET": "sharedSet", "SHARED_CRITERION": "sharedCriterion", "CUSTOMER": "customer"}
FIELD = {"amountMicros": "budget", "cpcBidMicros": "max CPC", "cpmBidMicros": "max CPM", "targetCpaMicros": "target CPA",
         "status": "status", "name": "name", "finalUrls": "final URL", "keyword.text": "keyword", "keyword.matchType": "match type",
         "negative": "negative", "bidModifier": "bid adjustment", "startDate": "start date", "endDate": "end date",
         "startDateTime": "start", "endDateTime": "end", "trackingUrlTemplate": "tracking template", "finalUrlSuffix": "final URL suffix",
         "targetRoas": "target ROAS", "maximizeConversions.targetCpaMicros": "target CPA", "targetCpa.targetCpaMicros": "target CPA",
         "maximizeConversionValue.targetRoas": "target ROAS", "biddingStrategyType": "bid strategy", "fieldType": "asset type"}
SKIP_FIELDS = {"resourceName", "id", "criterionId", "campaign", "adGroup", "asset", "customer", "ad.resourceName", "ad.id"}


def plain_field(f):
    if f in FIELD:
        return FIELD[f]
    last = f.split(".")[-1]
    if last in FIELD:
        return FIELD[last]
    return re.sub(r"(?<!^)(?=[A-Z])", " ", last.replace("Micros", "")).lower()


def _get(obj, path):
    for k in path.split("."):
        if isinstance(obj, list):
            obj = [x.get(k) if isinstance(x, dict) else None for x in obj]
            obj = [x for x in obj if x is not None]
            continue
        if not isinstance(obj, dict) or k not in obj:
            return None
        obj = obj[k]
    return obj


def fmt_value(field, v):
    if v is None or v == {} or v == []:
        return ""
    if field.endswith("Micros") or field.split(".")[-1].endswith("Micros"):
        try:
            return f"{int(v) / 1e6:,.2f}"
        except (TypeError, ValueError):
            return str(v)
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, str) and re.fullmatch(r"[A-Z][A-Z0-9_]+", v):
        return v.replace("_", " ").capitalize()
    if isinstance(v, list):
        if all(isinstance(x, str) for x in v):
            return ", ".join(v)[:200]
        texts = [x.get("text") for x in v if isinstance(x, dict) and x.get("text")]
        return ("; ".join(texts)[:200]) if texts else (f"{len(v)} item" + ("" if len(v) == 1 else "s"))
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False)[:160]
    return str(v)[:200]


def _criterion_kind(res):
    for k, label in (("keyword", "keyword"), ("location", "location"), ("language", "language"), ("device", "device"),
                     ("adSchedule", "ad schedule"), ("userList", "audience"), ("userInterest", "audience"), ("ageRange", "age"),
                     ("gender", "gender"), ("placement", "placement"), ("webpage", "webpage"), ("proximity", "radius")):
        if k in res:
            return k, label
    return None, None


def describe(ce, campaign_name="", ad_group_name=""):
    """change_event -> (object_type, object name, type, [changes]) in plain words."""
    rtype = ce.get("changeResourceType", "")
    op = ce.get("resourceChangeOperation", "")
    root = ROOT.get(rtype, "")
    old = (ce.get("oldResource") or {}).get(root) or {}
    new = (ce.get("newResource") or {}).get(root) or {}
    res = new or old
    label = RESOURCE.get(rtype, rtype.replace("_", " ").capitalize() or "Change")
    name = ""
    if rtype in ("AD_GROUP_CRITERION", "CAMPAIGN_CRITERION", "SHARED_CRITERION"):
        k, kl = _criterion_kind(res)
        neg = bool(res.get("negative"))
        if k == "keyword":
            kw = res["keyword"]
            label = ("Negative keyword" if neg or rtype == "SHARED_CRITERION" else "Keyword") + (" (campaign)" if rtype == "CAMPAIGN_CRITERION" else "")
            name = f"{kw.get('text', '')} [{(kw.get('matchType') or '').lower()}]"
        else:
            cid = res.get("criterionId") or (ce.get("changeResourceName", "").split("~")[-1])
            if not kl and str(cid).isdigit():  # Google's constant ids: 1000-1100 languages, 2000-2999 countries
                kl = "language" if 1000 <= int(cid) < 1100 else ("location" if 2000 <= int(cid) < 3000 else None)
            label = (label + f": {kl}") if kl else label
            name = f"{'excluded ' if neg else ''}criterion {cid}"
    elif rtype in ("AD_GROUP_AD", "AD"):
        ad = res.get("ad", res) if rtype == "AD_GROUP_AD" else res
        name = ad.get("name") or ""
        if not name:
            h = _get(ad, "responsiveSearchAd.headlines")
            name = (h[0].get("text") if h and isinstance(h[0], dict) else "") or f"ad {ce.get('changeResourceName', '').split('~')[-1].split('/')[-1]}"
    elif rtype in ("CAMPAIGN_ASSET", "AD_GROUP_ASSET", "CUSTOMER_ASSET", "ASSET_GROUP_ASSET"):
        ft = res.get("fieldType") or ce.get("changeResourceName", "").split("~")[-1]
        label = f"{label}: {fmt_value('fieldType', ft).lower()}" if ft else label
        name = "asset " + (str(res.get("asset", "")).split("/")[-1] or ce.get("changeResourceName", "").split("/")[-1].split("~")[0])
    elif rtype == "ASSET":
        name = res.get("name") or _get(res, "sitelinkAsset.linkText") or _get(res, "calloutAsset.calloutText") or f"asset {res.get('id') or ce.get('changeResourceName', '').split('/')[-1]}"
    elif rtype == "CAMPAIGN":
        name = res.get("name") or campaign_name
    elif rtype == "AD_GROUP":
        name = res.get("name") or ad_group_name
    elif rtype == "CAMPAIGN_BUDGET":
        name = res.get("name") or (f"budget of {campaign_name}" if campaign_name else "budget")
    else:
        name = res.get("name") or ce.get("changeResourceName", "").split("/")[-1]
    fields = [f for f in (ce.get("changedFields") or "").split(",") if f and f not in SKIP_FIELDS]
    changes = []
    if op == "UPDATE":
        for f in fields:
            o, n = fmt_value(f, _get(old, f)), fmt_value(f, _get(new, f))
            if o != n:
                changes.append({"field": plain_field(f), "old": o, "new": n})
        st_new = _get(new, "status") or _get(new, "ad.status")
        if "status" in fields and st_new == "REMOVED":
            verb = "removed"
        elif fields == ["status"] and st_new in ("PAUSED", "ENABLED"):
            verb = "paused" if st_new == "PAUSED" else "enabled"
        else:
            verb = "changed"
    elif op == "CREATE":
        verb = "added"
        for f in ("status", "cpcBidMicros", "amountMicros", "finalUrls"):
            v = _get(new, f) if f != "finalUrls" else (_get(new, "ad.finalUrls") or _get(new, f))
            if v not in (None, "", []):
                changes.append({"field": plain_field(f), "old": "", "new": fmt_value(f, v)})
    elif op == "REMOVE":
        verb = "removed"
    else:
        verb = (op or "changed").lower()
    return label, name, f"{label} {verb}", changes


def map_row(r, cid, account_name, tz):
    """One searchStream result -> a plain activity row. tz: the account's time zone (tzinfo)."""
    ce = r.get("changeEvent") or {}
    camp = (r.get("campaign") or {}).get("name", "")
    ag = (r.get("adGroup") or {}).get("name", "")
    otype, name, typ, changes = describe(ce, camp, ag)
    ts = per.parse(ce.get("changeDateTime", ""), assume=tz)
    where = " > ".join(x for x in (camp, ag) if x and x != name)
    client = ce.get("clientType", "UNKNOWN")
    return {"id": "g:" + ce.get("resourceName", ""), "ts": per.iso(ts), "channel": "google", "account": account_name, "account_id": cid,
            "type": typ, "object_type": otype, "object": name, "where": where,
            "old": "; ".join(f"{c['field']} {c['old']}" if len(changes) > 1 else c["old"] for c in changes if c["old"]),
            "new": "; ".join(f"{c['field']} {c['new']}" if len(changes) > 1 else c["new"] for c in changes if c["new"]),
            "fields": ", ".join(c["field"] for c in changes), "by": CLIENT.get(client, client.replace("_", " ").capitalize()),
            "by_kind": client, "who": ce.get("userEmail", ""), "link": "", "source": "Google Ads change history"}


def gaql_time(t, tz):
    return t.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S")


def clamp(start, end, now=None):
    """Keep a window inside what Google allows (the last 30 days, a few minutes of margin). None when nothing is left."""
    now = now or dt.datetime.now(dt.timezone.utc)
    earliest = now - dt.timedelta(days=KEEP_DAYS) + dt.timedelta(minutes=10)
    s, e = max(start, earliest), min(end, now)
    return (s, e) if s < e else None


def fetch(cid, start, end, tz, query):
    """Every change_event in [start, end] (aware datetimes), paging past the 10,000 row cap. query(cid, gaql) -> rows."""
    out, seen = [], set()
    hi = end
    for _ in range(50):  # 500,000 rows at most: a guard, not a real limit
        q = (f"SELECT {FIELDS} FROM change_event WHERE change_event.change_date_time >= '{gaql_time(start, tz)}' "
             f"AND change_event.change_date_time <= '{gaql_time(hi, tz)}' ORDER BY change_event.change_date_time DESC LIMIT {MAX_ROWS}")
        rows = query(cid, q)
        fresh = [r for r in rows if r.get("changeEvent", {}).get("resourceName") not in seen]
        for r in fresh:
            seen.add(r["changeEvent"]["resourceName"])
        out += fresh
        if len(rows) < MAX_ROWS or not fresh:
            break
        oldest = per.parse(rows[-1]["changeEvent"]["changeDateTime"], assume=tz)
        if oldest >= hi:  # 10,000 rows in one second: stop rather than loop
            break
        hi = oldest
    return out


_TZ = {}


def account_tz(cid, query):
    if cid not in _TZ:
        try:
            r = query(cid, "SELECT customer.time_zone FROM customer")
            _TZ[cid] = ZoneInfo(r[0]["customer"]["timeZone"])
        except Exception:  # noqa: BLE001 - fall back to UTC rather than fail
            return dt.timezone.utc
    return _TZ[cid]
