"""Creatives (part of Ads, both platforms): asset and creative performance per account for a date range, grouped by
asset across ads, with Add to draft for the changes the draft already knows (pause an ad, replace or remove a text).
The logic is in creatives.py (Studio's folder); this module only serves it to static/creatives.js. Read-only towards
Google and Meta.

  GET  /api/area/creatives/view?account=&view=&days=|start=&end=&grouped=&min_impressions=
                                                   the view (cached; read once when nothing is cached)
  POST /api/area/creatives/refresh {account, days | start, end}    read again now (read-only)
  POST /api/area/creatives/rest {account, days | start, end}       Meta: read only the breakdowns a read stopped before
  POST /api/area/creatives/draft {account, view, key, action, new_text?, ad_ids?, text_field?, days | start, end, grouped}
                                                   a row -> the account's Ads draft (never pushed)
"""
import creatives

LABEL = "Creatives"


def _flag(v, default=True):
    return default if v in (None, "") else str(v).lower() not in ("0", "false", "no")


def g_view(q):
    mi = q.get("min_impressions")
    return creatives.status(q.get("account") or None, q.get("view") or None, q.get("days") or None, q.get("start") or None,
                            q.get("end") or None, grouped=_flag(q.get("grouped")),
                            min_impressions=float(mi) if mi not in (None, "") else creatives.LOW_IMPRESSIONS)


def p_refresh(body):
    return creatives.refresh(body.get("account") or None, body.get("days"), body.get("start"), body.get("end"))


def p_rest(body):
    if not body.get("account"):
        raise ValueError("account is required")
    return creatives.fetch_rest(body["account"], body.get("days"), body.get("start"), body.get("end"))


def p_draft(body):
    for k in ("account", "key", "action"):
        if not body.get(k):
            raise ValueError(f"{k} is required")
    return creatives.add_to_draft(body["account"], body.get("view"), body["key"], body["action"], body.get("new_text"),
                                  body.get("ad_ids") or None, body.get("days"), body.get("start"), body.get("end"),
                                  _flag(body.get("grouped")), body.get("text_field"), by=body.get("by") or "you")


GET = {"view": g_view}
POST = {"refresh": p_refresh, "rest": p_rest, "draft": p_draft}
