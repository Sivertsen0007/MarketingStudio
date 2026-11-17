"""Pacing (part of Ads, both platforms): month-to-date spend, forecast to month end and over/under flags per ad account
and campaign against a monthly target, with budget recommendations added to the Ads draft on request. The logic is in
pacing.py (Studio's folder); this module only serves it to static/pacing.js.

  GET  /api/area/pacing/status?account=<id>          pacing (cached; read once when nothing is cached)
  POST /api/area/pacing/refresh {account?}           read Google / Meta again now (read-only)
  POST /api/area/pacing/targets {account, monthly, campaigns, tolerance_pct, meta_step_pct, share}
  POST /api/area/pacing/draft {account, ids?}         recommendations -> the account's Ads draft (never pushed)
"""
import pacing

LABEL = "Pacing"


def g_status(q):
    return pacing.status(q.get("account") or None)


def p_refresh(body):
    return pacing.refresh(body.get("account") or None)


def p_targets(body):
    return pacing.save_targets(body)


def p_draft(body):
    if not body.get("account"):
        raise ValueError("account is required")
    return pacing.add_to_draft(body["account"], body.get("ids") or None, by=body.get("by") or "you")


GET = {"status": g_status}
POST = {"refresh": p_refresh, "targets": p_targets, "draft": p_draft}
