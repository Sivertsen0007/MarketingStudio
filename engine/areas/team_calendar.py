"""Calendar area: the shared calendar (week and month, every channel), the team library and the team settings.
UI: static/team_calendar.js. Logic: shared_calendar.py (calendar, clash warnings) and team_settings.py (settings,
library). Nothing here sends, schedules or publishes anything.
"""
import shared_calendar as cal
import team_settings as ts

LABEL = "Calendar"


def g_calendar(q):
    return cal.calendar_list(q.get("start"), q.get("end"))


def g_clashes(q):
    """Clash warnings before scheduling. kind=social&id=<post>[&channels=a,b] or kind=email&id=<campaign>&when=<London time>[&tz=] (or now=1 for Send now)."""
    kind = q.get("kind")
    if kind == "social":
        from areas import _social_store as store  # noqa: PLC0415
        chs = [c for c in (q.get("channels") or "").split(",") if c] or None
        return {"clashes": cal.social_clashes(store.get(q["id"]), chs)}
    if kind == "email":
        from areas import email as em  # noqa: PLC0415
        when = em._now() if q.get("now") == "1" else em.to_utc(q.get("when"), q.get("tz") or "Europe/London") if q.get("when") else None
        merged = em._merged(em.adapter(), q["id"])[0]
        return {"clashes": cal.email_clashes({**merged, "id": q["id"]}, when) if when else []}
    raise ValueError("kind must be social or email")


def g_settings(q):
    return ts.info(refresh_now=q.get("refresh") == "1")


def p_settings(body):
    return ts.save(body.get("key"), body.get("value"), body.get("scope") or "local")


def g_library(q):
    return ts.library_list(q.get("kind") or None, q.get("q") or None)


def p_library_add(body):
    return ts.library_add(body.get("item") or {}, by="you")


def p_library_delete(body):
    return ts.library_delete(body.get("id"))


GET = {"calendar": g_calendar, "clashes": g_clashes, "settings": g_settings, "library": g_library}
POST = {"settings": p_settings, "library_add": p_library_add, "library_delete": p_library_delete}
