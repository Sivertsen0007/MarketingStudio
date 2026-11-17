"""Activity area: Meta ad account activity log (act_<id>/activities), read-only.

Fields (AdActivity, Graph v27, checked 17 November 2026): actor_id, actor_name, application_id, application_name,
date_time_in_timezone, event_time, event_type, extra_data (JSON text), object_id, object_name, object_type,
translated_event_type. The edge returns one week by default; Studio asks for since/until (epoch seconds), which the
API accepts although the reference page lists no parameters.

Meta's own object names: CAMPAIGN_GROUP = campaign, CAMPAIGN = ad set, ADGROUP = ad.
"""
import json

from . import _activity_periods as per

FIELDS = ("actor_id,actor_name,application_id,application_name,date_time_in_timezone,event_time,event_type,extra_data,"
          "object_id,object_name,object_type,translated_event_type")
OBJECT = {"CAMPAIGN_GROUP": "Campaign", "CAMPAIGN": "Ad set", "ADGROUP": "Ad", "AD": "Ad", "ACCOUNT": "Ad account",
          "AD_ACCOUNT": "Ad account", "AUDIENCE": "Audience", "CUSTOM_AUDIENCE": "Audience", "IMAGE": "Image", "CREATIVE": "Creative"}
EVENT = {
    "create_campaign_group": "Campaign created", "create_campaign_legacy": "Campaign created", "update_campaign_name": "Campaign renamed",
    "update_campaign_run_status": "Campaign status changed", "update_campaign_budget": "Campaign budget changed",
    "update_campaign_group_spend_cap": "Campaign spend cap changed", "campaign_ended": "Campaign ended",
    "update_campaign_schedule": "Campaign schedule changed", "update_campaign_duration": "Campaign dates changed",
    "update_campaign_budget_optimization_toggling_status": "Advantage campaign budget switched",
    "update_campaign_conversion_goal": "Campaign conversion goal changed", "merge_campaigns": "Campaigns merged",
    "create_ad_set": "Ad set created", "update_ad_set_name": "Ad set renamed", "update_ad_set_run_status": "Ad set status changed",
    "update_ad_set_budget": "Ad set budget changed", "update_ad_set_bidding": "Ad set bid changed",
    "update_ad_set_bid_strategy": "Ad set bid strategy changed", "update_ad_set_duration": "Ad set dates changed",
    "update_ad_set_target_spec": "Ad set targeting changed", "update_ad_set_optimization_goal": "Ad set optimisation goal changed",
    "update_ad_set_spend_cap": "Ad set spend cap changed", "update_ad_set_min_spend_target": "Ad set minimum spend changed",
    "update_ad_set_learning_stage_status": "Ad set learning phase changed", "di_ad_set_learning_stage_exit": "Ad set left learning phase",
    "update_ad_set_bid_adjustments": "Ad set bid adjustments changed", "update_ad_set_value_rules": "Ad set value rules changed",
    "create_ad": "Ad created", "update_ad_friendly_name": "Ad renamed", "update_ad_run_status": "Ad status changed",
    "update_ad_run_status_to_be_set_after_review": "Ad status set (after review)", "update_ad_creative": "Ad creative changed",
    "edit_and_update_ad_creative": "Ad creative edited", "update_ad_targets_spec": "Ad targeting changed",
    "update_ad_bid_info": "Ad bid changed", "update_ad_bid_type": "Ad bid type changed", "update_ad_labels": "Ad labels changed",
    "update_adgroup_stop_delivery": "Ad delivery stopped", "ad_review_approved": "Ad approved", "ad_review_declined": "Ad rejected",
    "first_delivery_event": "First delivery", "create_audience": "Audience created", "update_audience": "Audience changed",
    "delete_audience": "Audience deleted", "share_audience": "Audience shared", "unshare_audience": "Audience unshared",
    "receive_audience": "Audience received", "remove_shared_audience": "Shared audience removed",
    "add_images": "Images added", "edit_images": "Images edited", "delete_images": "Images deleted",
    "ad_account_update_spend_limit": "Account spending limit changed", "ad_account_reset_spend_limit": "Account spending limit reset",
    "ad_account_remove_spend_limit": "Account spending limit removed", "ad_account_update_status": "Account status changed",
    "ad_account_add_user_to_role": "User added to account", "ad_account_remove_user_from_role": "User removed from account",
    "ad_account_set_business_information": "Business information changed", "ad_account_billing_charge": "Billing charge",
    "ad_account_billing_charge_failed": "Billing charge failed", "ad_account_billing_refund": "Billing refund",
    "billing_event": "Billing event", "add_funding_source": "Payment method added", "remove_funding_source": "Payment method removed",
    "account_spending_limit_reached": "Account spending limit reached", "campaign_spending_limit_reached": "Campaign spend cap reached",
    "lifetime_budget_spent": "Lifetime budget spent", "conversion_event_updated": "Conversion event changed",
    "funding_event_initiated": "Funding started", "funding_event_successful": "Funding done", "unknown": "Other change",
}
# Things Meta does on its own: shown in the table, not counted as changes made by people or tools.
SYSTEM = {"ad_review_approved", "ad_review_declined", "first_delivery_event", "campaign_ended", "account_spending_limit_reached",
          "campaign_spending_limit_reached", "lifetime_budget_spent", "ad_account_billing_charge", "ad_account_billing_charge_failed",
          "ad_account_billing_chargeback", "ad_account_billing_chargeback_reversal", "ad_account_billing_decline",
          "ad_account_billing_refund", "billing_event", "funding_event_initiated", "funding_event_successful",
          "update_ad_set_learning_stage_status", "di_ad_set_learning_stage_exit"}
WEB_APPS = ("Power Editor", "Ads Manager", "Business Manager", "Facebook", "Meta Business Suite", "Meta Ads Manager")
APP = {"Power Editor": "Ads Manager", "Business Manager": "Business Manager", "": "Meta"}  # Meta still calls Ads Manager "Power Editor"
MONEY_EVENTS = ("budget", "spend_cap", "spend_limit", "bid", "billing", "min_spend")


def plain_event(event_type, translated=""):
    if event_type in EVENT:
        return EVENT[event_type]
    if translated:
        return translated[:1].upper() + translated[1:]
    return (event_type or "change").replace("_", " ").capitalize()


def _val(v, money=False):
    if v is None or v == "":
        return ""
    if isinstance(v, dict) and "content" in v:  # targeting summaries: {"content": "Location:", "children": [...]}
        return f"{v['content']} {', '.join(str(c) for c in v.get('children') or [])}".strip()[:200]
    if isinstance(v, dict):
        for k in ("new_value", "value", "name", "text"):
            if k in v:
                return _val(v[k], money)
        return json.dumps(v, ensure_ascii=False)[:160]
    if isinstance(v, list):
        return "; ".join(_val(x) for x in v)[:300]
    if money and isinstance(v, (int, float)) or (money and isinstance(v, str) and v.isdigit()):
        return f"{int(v) / 100:,.2f}"  # Meta money is in the account's minor unit (cents)
    return str(v)[:200]


def old_new(event_type, extra):
    """extra_data (JSON text or dict) -> (old, new) as plain text. Shapes vary; read defensively."""
    try:
        d = json.loads(extra) if isinstance(extra, str) and extra.strip().startswith(("{", "[")) else extra
    except ValueError:
        return "", ""
    if not isinstance(d, dict):
        return "", ""
    money = d.get("type") in ("payment_amount", "budget") or ("type" not in d and any(k in (event_type or "") for k in MONEY_EVENTS))
    old = d.get("old_value", d.get("old"))
    new = d.get("new_value", d.get("new"))
    if isinstance(old, dict) and "old_value" in old:
        old = old["old_value"]
    cur = d.get("currency", "")
    o, n = _val(old, money), _val(new, money)
    if money and cur:
        o, n = (f"{o} {cur}" if o else ""), (f"{n} {cur}" if n else "")
    return o, n


def link(act, object_type, object_id):
    a = str(act).replace("act_", "")
    level = {"CAMPAIGN_GROUP": "campaigns", "CAMPAIGN": "adsets", "ADGROUP": "ads", "AD": "ads"}.get(object_type)
    if not level or not object_id:
        return ""
    sel = {"campaigns": "selected_campaign_ids", "adsets": "selected_adset_ids", "ads": "selected_ad_ids"}[level]
    return f"https://adsmanager.facebook.com/adsmanager/manage/{level}?act={a}&{sel}={object_id}"


def map_row(x, act, account_name):
    et = x.get("event_type", "")
    o, n = old_new(et, x.get("extra_data"))
    ts = per.parse(x.get("event_time", ""))
    oid = x.get("object_id", "")
    key = f"m:{act}:{oid}:{et}:{x.get('event_time', '')}:{x.get('actor_id', '')}"
    system = et in SYSTEM or (str(x.get("actor_id", "")) == "0" and not x.get("application_name"))  # actor 0 = Meta itself (e.g. review)
    app = x.get("application_name") or ""
    return {"id": key, "ts": per.iso(ts), "channel": "meta", "account": account_name, "account_id": act,
            "type": plain_event(et, x.get("translated_event_type", "")), "event_type": et,
            "object_type": OBJECT.get(x.get("object_type", ""), (x.get("object_type") or "").replace("_", " ").capitalize()),
            "object": x.get("object_name") or oid, "where": "", "old": o, "new": n, "fields": "",
            "by": "Meta (automatic)" if system else APP.get(app, app or "Meta"), "by_kind": "system" if system else ("api" if app and app not in WEB_APPS else "web"),
            "who": "" if system else (x.get("actor_name") or ""), "system": system, "link": link(act, x.get("object_type"), oid),
            "source": "Meta activity log"}


def fetch(act, start, end, get_all):
    """All activities between two aware datetimes. get_all(path, **params) -> rows (meta_ads.get_all)."""
    return get_all(f"{act}/activities", fields=FIELDS, since=int(start.timestamp()), until=int(end.timestamp()), limit=500)
