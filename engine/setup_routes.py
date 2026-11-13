"""Setup: the keys each person gives Marketing Studio, Sign in with Google, and importing a .env file.

Routes (wired in server.py):
  GET  /api/setup                    status of every key (secrets: set / not set only)
  POST /api/setup/save               {"values": {NAME: value}}  blank secret = keep, null = remove
  POST /api/setup/import_env         {"text": .env file contents} -> which keys were saved (never the values)
  POST /api/setup/google/start       -> {"url": Google's consent page}; the UI opens it in the browser
  POST /api/setup/google/sign_out
  GET  /oauth/google/callback        Google sends the browser back here (state nonce checked)
"""
import html

import appconfig

ALLOWED = {k[0] for k in appconfig.KEYS}


def status():
    s = appconfig.status()
    s["mcp_command"] = mcp_command()
    return s


def save(body):
    vals = {k: v for k, v in (body.get("values") or {}).items() if k in ALLOWED}
    appconfig.set_many(vals)
    return status()


def import_env(body):
    r = appconfig.import_env(str(body.get("text") or ""))
    return {**r, "status": status()}


def mcp_command():
    """The one line that connects Claude Code to this Studio (same drafts as the app)."""
    from pathlib import Path
    eng = Path(__file__).resolve().parent / "mcp_server.py"
    return f'claude mcp add marketing-studio -s user -- python3 "{eng}"'


def google_start(port):
    return {"url": appconfig.google_auth_url(f"http://127.0.0.1:{port}/oauth/google/callback")}


def google_sign_out(_body):
    appconfig.google_sign_out()
    return status()


def google_callback(q):
    """-> (status code, html page) for the browser tab Google sent back."""
    if q.get("error"):
        return 400, _page("Not signed in", f"Google said: {html.escape(q['error'])}. Close this tab and try again from Marketing Studio.")
    try:
        email = appconfig.google_finish(q.get("code", ""), q.get("state", ""))
    except Exception as e:  # noqa: BLE001 - shown to the person, never contains a secret
        return 400, _page("Not signed in", html.escape(str(e)))
    who = f" as <b>{html.escape(email)}</b>" if email else ""
    return 200, _page("Signed in", f"Marketing Studio is signed in to Google{who}. You can close this tab and go back to the app.")


def _page(title, msg):
    return f"""<!doctype html><meta charset="utf-8"><title>{title} - Marketing Studio</title>
<body style="font:16px -apple-system,Roboto,Arial,sans-serif;display:grid;place-items:center;height:90vh;color:#202124">
<div style="max-width:460px;text-align:center"><h2 style="font-weight:500">{title}</h2><p>{msg}</p></div></body>"""
