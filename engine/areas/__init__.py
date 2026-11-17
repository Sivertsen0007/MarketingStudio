"""Marketing Studio areas (Email, SEO, SoMe). Each area is one module in this folder with:

  LABEL = "Email"                       # tab name
  GET  = {"campaigns": fn(q) -> json}   # served at GET  /api/area/<name>/<key>?...
  POST = {"save": fn(body) -> json}     # served at POST /api/area/<name>/<key>
  def health(today) -> [health._row(...)]   # optional, added to the Connections check

and one UI file static/<name>.js that registers window.AREAS.<name> = {label, render(el)}.
Live actions (send, schedule, publish, post) only run from the user's own button in the UI; Claude's MCP has no tool for them.
"""
import importlib
import pkgutil
import traceback

ORDER = ("pacing", "alerts", "email", "seo", "social", "activity")


def load():
    mods = {}
    for m in pkgutil.iter_modules(__path__):
        if m.name.startswith("_"):
            continue
        try:
            mod = importlib.import_module(f"{__name__}.{m.name}")
            if getattr(mod, "LABEL", None):  # helper modules (seo_wp.py, social_meta.py ...) have no LABEL and are not areas
                mods[m.name] = mod
        except Exception:  # noqa: BLE001 - one broken area must not take the studio down
            traceback.print_exc()
    return dict(sorted(mods.items(), key=lambda kv: ORDER.index(kv[0]) if kv[0] in ORDER else 99))


AREAS = load()


def health_checks(today):
    rows = []
    for name, mod in AREAS.items():
        fn = getattr(mod, "health", None)
        if fn:
            try:
                rows += fn(today)
            except Exception as ex:  # noqa: BLE001
                rows.append({"channel": getattr(mod, "LABEL", name), "account": "", "check": "Health check", "ok": False,
                             "detail": f"Check crashed: {ex}"[:400], "fix": ""})
    return rows
