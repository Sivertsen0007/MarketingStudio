"""Activity area: period maths. Pure functions, no Studio imports.

A period is [start, end) in one time zone (Europe/London by default). Weeks start on Monday.
"this_*" periods run from the start of the unit to now; their previous period is the same stretch of the unit before
(1 to 10 November is compared with 1 to 10 October). Full periods compare with the full unit before. Rolling and custom
periods compare with the same number of days straight before.
"""
import datetime as dt
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")
PERIODS = {"this_week": "This week", "last_week": "Last week", "this_month": "This month", "last_month": "Last month",
           "last_7": "Last 7 days", "last_30": "Last 30 days", "last_90": "Last 90 days", "this_quarter": "This quarter",
           "custom": "Custom"}


def _tz(tz):
    return tz if isinstance(tz, dt.tzinfo) else ZoneInfo(tz or "Europe/London")


def _midnight(d, tz):
    return dt.datetime(d.year, d.month, d.day, tzinfo=tz)


def _month_start(d, back=0):
    m = d.year * 12 + d.month - 1 - back
    return dt.date(m // 12, m % 12 + 1, 1)


def _quarter_start(d, back=0):
    q = (d.year * 4 + (d.month - 1) // 3) - back
    return dt.date(q // 4, (q % 4) * 3 + 1, 1)


def _shift_same_stretch(start, now, prev_start, tz):
    """The same elapsed stretch of the previous unit, in wall-clock terms (so DST does not move it), capped at its end."""
    elapsed_days = (now.date() - start.date()).days
    t = now.timetz().replace(tzinfo=None)
    p_end_date = prev_start.date() + dt.timedelta(days=elapsed_days)
    p_end = dt.datetime.combine(p_end_date, t).replace(tzinfo=tz)
    return min(p_end, start)


def period(name="last_30", now=None, tz="Europe/London", start=None, end=None):
    """-> {"name", "label", "tz", "start", "end", "prev_start", "prev_end", "notes"} (aware datetimes in tz).
    custom: start and end are dates (YYYY-MM-DD, both days included). Dates the wrong way round are swapped, a to date
    after today ends the period today, and a from date after today is refused; "notes" says what was changed."""
    tz = _tz(tz)
    now = (now or dt.datetime.now(dt.timezone.utc)).astimezone(tz)
    today = now.date()
    name = name if name in PERIODS else "last_30"
    notes = []
    if name == "custom":
        if not start or not end:
            raise ValueError("Custom period needs a from and a to date.")
        try:
            d0 = dt.date.fromisoformat(str(start)[:10])
            d1 = dt.date.fromisoformat(str(end)[:10])
        except ValueError:
            raise ValueError("Custom period: use dates like 2026-11-01.") from None
        if d1 < d0:
            d0, d1 = d1, d0
            notes.append(f"The from and to dates were the wrong way round, so they were swapped ({fmt_day(d0)} to {fmt_day(d1)}).")
        if d0 > today:
            raise ValueError(f"The from date ({fmt_day(d0)}) is after today: pick a date up to today ({fmt_day(today)}).")
        if d1 > today:
            notes.append(f"The to date ({fmt_day(d1)}) is after today, so the period ends today ({fmt_day(today)}).")
            d1 = today
        s, e = _midnight(d0, tz), _midnight(d1 + dt.timedelta(days=1), tz)
        n = (d1 - d0).days + 1
        ps, pe = _midnight(d0 - dt.timedelta(days=n), tz), s
        label = f"{fmt_day(d0)} to {fmt_day(d1)}"
    elif name.startswith("last_") and name[5:].isdigit():
        n = int(name[5:])
        s, e = _midnight(today - dt.timedelta(days=n - 1), tz), now
        ps = _midnight(today - dt.timedelta(days=2 * n - 1), tz)
        pe = _shift_same_stretch(s, now, ps, tz)
        label = PERIODS[name]
    elif name in ("this_week", "last_week"):
        mon = today - dt.timedelta(days=today.weekday())
        if name == "this_week":
            s, e = _midnight(mon, tz), now
            ps = _midnight(mon - dt.timedelta(days=7), tz)
            pe = _shift_same_stretch(s, now, ps, tz)
        else:
            s, e = _midnight(mon - dt.timedelta(days=7), tz), _midnight(mon, tz)
            ps, pe = _midnight(mon - dt.timedelta(days=14), tz), s
        label = PERIODS[name]
    elif name in ("this_month", "last_month"):
        if name == "this_month":
            s, e = _midnight(_month_start(today), tz), now
            ps = _midnight(_month_start(today, 1), tz)
            pe = _shift_same_stretch(s, now, ps, tz)
        else:
            s, e = _midnight(_month_start(today, 1), tz), _midnight(_month_start(today), tz)
            ps, pe = _midnight(_month_start(today, 2), tz), s
        label = PERIODS[name]
    else:  # this_quarter
        s, e = _midnight(_quarter_start(today), tz), now
        ps = _midnight(_quarter_start(today, 1), tz)
        pe = _shift_same_stretch(s, now, ps, tz)
        label = PERIODS[name]
    return {"name": name, "label": label, "tz": str(tz), "start": s, "end": e, "prev_start": ps, "prev_end": pe, "notes": notes}


def days(start, end, tz="Europe/London"):
    """Every calendar day (YYYY-MM-DD in tz) that [start, end) touches."""
    tz = _tz(tz)
    d, last = start.astimezone(tz).date(), (end - dt.timedelta(microseconds=1)).astimezone(tz).date()
    out = []
    while d <= last:
        out.append(d.isoformat())
        d += dt.timedelta(days=1)
    return out


def day_of(ts, tz="Europe/London"):
    return parse(ts).astimezone(_tz(tz)).date().isoformat()


def parse(v, assume=dt.timezone.utc):
    """ISO text, epoch number or datetime -> aware datetime. Naive text gets `assume` (a tzinfo)."""
    if isinstance(v, dt.datetime):
        return v if v.tzinfo else v.replace(tzinfo=assume)
    if isinstance(v, (int, float)):
        return dt.datetime.fromtimestamp(v, dt.timezone.utc)
    s = str(v).strip().replace("Z", "+00:00").replace(" ", "T", 1)
    if len(s) >= 5 and s[-5] in "+-" and s[-4:].isdigit() and ":" not in s[-5:]:
        s = s[:-2] + ":" + s[-2:]  # Meta's +0000
    if "." in s:  # Python 3.9 wants 3 or 6 fraction digits
        head, _, rest = s.partition(".")
        frac = "".join(ch for ch in rest if ch.isdigit())
        tail = rest[len(frac):]
        s = f"{head}.{(frac + '000000')[:6]}{tail}"
    t = dt.datetime.fromisoformat(s)
    return t if t.tzinfo else t.replace(tzinfo=assume)


def iso(t):
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_day(d):
    return f"{d.day} {d:%B %Y}"
