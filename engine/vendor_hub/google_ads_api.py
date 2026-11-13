"""Google Ads API connection for Marketing Studio (slimmed from AI-Hub's dashboard connector).

Keys come from appconfig (Keychain / config.json): GADS_DEVELOPER_TOKEN, GADS_LOGIN_CUSTOMER_ID (the MCC),
GADS_OPERATING_CUSTOMER_ID. The access token is the signed-in person's (appconfig.google_token()).
"""
import json
import urllib.error
import urllib.request

import appconfig

API_VERSION = "v23"  # v21/v20 are deprecated + blocked by Google (bumped 2026-08-17)
_MISSING = object()


def _env(key, default=_MISSING):
    v = appconfig.get(key)
    if v:
        return v
    if default is not _MISSING:
        return default
    raise RuntimeError(f"{key} is not set. Open Setup and fill in Google Ads.")


def _token():
    return appconfig.google_token()


def _search_stream(query, token):
    """googleAds:searchStream returns a JSON array of batches; flatten to results."""
    operating = _env("GADS_OPERATING_CUSTOMER_ID")
    login = _env("GADS_LOGIN_CUSTOMER_ID", operating)
    url = f"https://googleads.googleapis.com/{API_VERSION}/customers/{operating}/googleAds:searchStream"
    headers = {"Authorization": f"Bearer {token}", "developer-token": _env("GADS_DEVELOPER_TOKEN"),
               "login-customer-id": login, "Content-Type": "application/json"}
    req = urllib.request.Request(url, data=json.dumps({"query": query}).encode(), headers=headers)
    try:
        raw = urllib.request.urlopen(req, timeout=120).read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Google Ads API {e.code}: {e.read().decode('utf-8', 'replace')[:400]}")
    results = []
    for b in json.loads(raw):
        results.extend(b.get("results", []))
    return results
