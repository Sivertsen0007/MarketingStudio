"""Google Ads experiments: keep Studio's hands off trial campaigns.

An experiment (Campaigns > Experiments in Google Ads) runs a trial campaign next to its base campaign. The trial
belongs to the experiment: Google changes it when the experiment is changed, ended or applied, so Studio shows it
read-only and refuses draft changes, Send to Editor and Push live for it and everything under it.
A base campaign with a running experiment can be changed, with a warning: Google copies some changes from the base
into the trial (budget, for example), but not all of them.

Where the facts come from:
- Google Ads Editor's database: Campaign.trialParentId is not 0 on a trial campaign. It is the base campaign's parent
  key, (2 << 32) | base localId, and trialParentName holds the base campaign's name.
- the Google Ads API: campaign.experiment_type is EXPERIMENT (or DRAFT) on a trial campaign and BASE on the rest;
  experiment_arm.control marks the arm holding the base campaign, experiment.status says whether it is running.

Info is {"trials": {name: base name or ""}, "bases": {name: [trial names]}}; names are matched without case or
surrounding spaces. "bases" lists only base campaigns whose experiment is running (Editor can't tell a halted
experiment from a running one, so from Editor it is every base with a trial campaign that is not removed).
"""
import sqlite3

MSG = "This is an experiment's trial campaign: change it from the experiment"
RUNNING = ("ENABLED", "INITIATED")  # experiment.status values that mean the trial is (about to be) serving
NOT_TRIAL = ("BASE", "UNSPECIFIED", "UNKNOWN", "", None)
CAMPAIGNS_GAQL = "SELECT campaign.id, campaign.name, campaign.experiment_type FROM campaign WHERE campaign.status != 'REMOVED'"
ARMS_GAQL = ("SELECT experiment_arm.experiment, experiment_arm.control, experiment_arm.campaigns, experiment.status FROM experiment_arm "
             f"WHERE experiment.status IN ({', '.join(RUNNING)})")


def _n(s):
    return (s or "").strip().lower()


def empty():
    return {"trials": {}, "bases": {}}


def merge(*infos):
    out = empty()
    for i in infos:
        for k, v in (i or {}).get("trials", {}).items():
            out["trials"][_n(k)] = out["trials"].get(_n(k)) or v
        for k, v in (i or {}).get("bases", {}).items():
            out["bases"].setdefault(_n(k), [])
            out["bases"][_n(k)] += [t for t in v if t not in out["bases"][_n(k)]]
    return out


def from_editor(db):
    """Info from Editor's database (an open sqlite3 connection). Older Editors without trialParentId: no trials."""
    try:
        rows = db.execute("select localId, name, status, trialParentId, trialParentName from Campaign").fetchall()
    except sqlite3.OperationalError:
        return empty()
    by_local = {r[0]: r[1] for r in rows}
    out = empty()
    for local_id, name, status, parent, parent_name in rows:
        if not parent:
            continue
        base = by_local.get(parent & 0xFFFFFFFF) or parent_name or ""
        out["trials"][_n(name)] = base
        if status != 2 and base:
            out["bases"].setdefault(_n(base), []).append(name)
    return out


def from_api(campaign_rows, arm_rows=()):
    """Info from GAQL rows: CAMPAIGNS_GAQL (campaign.id, name, experiment_type) and ARMS_GAQL."""
    names = {str(r["campaign"]["id"]): r["campaign"].get("name", "") for r in campaign_rows}
    out = empty()
    for r in campaign_rows:
        c = r["campaign"]
        if c.get("experimentType") not in NOT_TRIAL:
            out["trials"][_n(c.get("name"))] = ""
    running = {}  # experiment -> (base names, trial names)
    for r in arm_rows:
        arm = r.get("experimentArm", {})
        if r.get("experiment", {}).get("status") not in RUNNING:
            continue
        exp = arm.get("experiment") or arm.get("resourceName", "").split("~")[0]
        side = 0 if arm.get("control") else 1
        for res in arm.get("campaigns", []):
            nm = names.get(res.rsplit("/", 1)[-1])
            if nm:
                running.setdefault(exp, ([], []))[side].append(nm)
    for bases, trials in running.values():
        for b in bases:
            out["bases"].setdefault(_n(b), []).extend(trials)
            for t in trials:
                out["trials"][_n(t)] = out["trials"].get(_n(t)) or b
    return out


def fetch(acct, query):
    """Info straight from the Google Ads API (query(acct, gaql) -> rows). The arms query only adds warnings, so a
    failure there is ignored; a failure of the campaign query raises."""
    camps = query(acct, CAMPAIGNS_GAQL)
    try:
        arms = query(acct, ARMS_GAQL)
    except Exception:  # noqa: BLE001 - warnings only
        arms = []
    return from_api(camps, arms)


_KNOWN = {}  # acct -> (stamp of the files it was read from, info)


def _stamp(acct):
    """Modification times of Editor's database and the API cache: known() reads them again only when they change."""
    out = []
    try:
        import gae  # noqa: PLC0415
        out.append((gae.editor_version_dir() / f"ape_{acct}.db").stat().st_mtime_ns)
    except (SystemExit, Exception):  # noqa: BLE001 - no Editor
        out.append(None)
    try:
        import gapi  # noqa: PLC0415
        out.append(gapi._file(acct, "base").stat().st_mtime_ns)
    except Exception:  # noqa: BLE001
        out.append(None)
    return tuple(out)


def known(acct):
    """What this Mac already knows, without asking Google: Editor's database (when the account is downloaded) and the
    API cache (gapi base). Used when a change is added to the draft and when the draft is sent to Editor.
    Kept per account until either file changes."""
    st = _stamp(acct)
    hit = _KNOWN.get(acct)
    if hit and hit[0] == st:
        return hit[1]
    info = _known(acct)
    _KNOWN[acct] = (st, info)
    return info


def _known(acct):
    infos = []
    try:
        import core  # noqa: PLC0415
        db = core._db(acct)
        try:
            infos.append(from_editor(db))
        finally:
            db.close()
    except (ValueError, SystemExit, sqlite3.Error, OSError):
        pass
    try:
        import gapi  # noqa: PLC0415
        d = gapi._read(acct, "base")
        if d and d.get("experiments"):
            infos.append(d["experiments"])
    except Exception:  # noqa: BLE001 - no cache, no facts
        pass
    return merge(*infos)


def touches(change):
    """The existing campaign a draft change touches, or None (new campaign, account-level asset)."""
    if change.get("level") == "account":
        return None
    if change.get("type") in ("campaign", "dg_campaign") and change.get("new"):
        return None
    if change.get("type") in ("video_asset", "video_upload"):
        return None
    return change.get("campaign") or None


def check(info, change):
    """-> (problems, warnings) for one draft change."""
    name = touches(change)
    if not name:
        return [], []
    if _n(name) in info["trials"]:
        return [f'{MSG} ("{name}")'], []
    if _n(name) in info["bases"]:
        return [], [f'"{name}" has a running experiment: Google copies only some changes into its trial campaign, '
                    "so check the trial after this change"]
    return [], []


def tag(rows, info, key="campaign"):
    """Mark grid / tree rows: experiment = "trial" (read-only) or "base" (has a running experiment)."""
    for r in rows:
        nm = _n(r.get(key))
        if nm in info["trials"]:
            r["experiment"] = "trial"
            if info["trials"][nm]:
                r["experiment_base"] = info["trials"][nm]
        elif nm in info["bases"]:
            r["experiment"] = "base"
    return rows
