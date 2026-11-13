"""Google Ads request headers + query/mutate helpers for Marketing Studio (slimmed from AI-Hub's shared/pm/gads.py)."""
import json
import urllib.error
import urllib.request

import google_ads_api as g


def customer():
    return g._env("GADS_OPERATING_CUSTOMER_ID")


def headers():
    cid = customer()
    return {"Authorization": f"Bearer {g._token()}", "developer-token": g._env("GADS_DEVELOPER_TOKEN"),
            "login-customer-id": g._env("GADS_LOGIN_CUSTOMER_ID", cid), "Content-Type": "application/json"}


def query(gaql):
    return g._search_stream(gaql, g._token())


def mutate(ops, apply=False):
    """Returns (ok, response_or_error). validateOnly unless apply=True."""
    url = f"https://googleads.googleapis.com/{g.API_VERSION}/customers/{customer()}/googleAds:mutate"
    body = {"mutateOperations": ops, "validateOnly": not apply, "partialFailure": False}
    req = urllib.request.Request(url, json.dumps(body).encode(), headers())
    try:
        return True, json.loads(urllib.request.urlopen(req, timeout=120).read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            errs = json.loads(raw)["error"]["details"][0]["errors"]
            return False, [{"code": x.get("errorCode"), "message": x.get("message"), "where": x.get("location")} for x in errs]
        except Exception:  # noqa: BLE001
            return False, raw[:2000]
