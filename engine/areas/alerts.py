"""Alerts (part of Ads, both platforms): rules on spend, CPC, CTR, CPA, ROAS, conversions, delivery, disapproved ads
and Meta learning, checked on fresh reads, with a history of fired alerts. The logic is in alerts.py (Studio's
folder); this module only serves it to static/alerts.js. Nothing here changes anything on Google or Meta.

  GET  /api/area/alerts/view                     rules, fired alerts, last checks, settings (no API calls)
  GET  /api/area/alerts/badge                    {unacked, latest, running} for the header badge
  POST /api/area/alerts/check {account?}         Check now: read and evaluate every switched-on rule (read-only)
  POST /api/area/alerts/rule {id?, ...rule}      add a rule, or change one (id)
  POST /api/area/alerts/delete {id}              delete a rule
  POST /api/area/alerts/ack {ids | all: true}    acknowledge fired alerts
  POST /api/area/alerts/snooze {id, hours}       snooze one fired alert (0 = wake it up)
  POST /api/area/alerts/settings {mac_notify, team_notify}
"""
import alerts

LABEL = "Alerts"


def g_view(q):
    return alerts.view()


def g_badge(q):
    return alerts.badge()


def p_check(body):
    acct = body.get("account")
    return alerts.check(None, [acct] if acct else None, reason="check now")


def p_rule(body):
    if body.get("id"):
        return alerts.update_rule(body["id"], {k: v for k, v in body.items() if k != "id"})
    return alerts.add_rule(body, by=body.get("by") or "you")


def p_delete(body):
    return alerts.delete_rule(body.get("id"))


def p_ack(body):
    return alerts.ack(body.get("ids"), body.get("all") is True)


def p_snooze(body):
    return alerts.snooze(body.get("id"), body.get("hours", 24))


def p_settings(body):
    return alerts.save_settings(body)


GET = {"view": g_view, "badge": g_badge}
POST = {"check": p_check, "rule": p_rule, "delete": p_delete, "ack": p_ack, "snooze": p_snooze, "settings": p_settings}
