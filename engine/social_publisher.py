#!/usr/bin/env python3
"""SoMe publisher job (plan Option A). Publishes ONLY queue items the user scheduled in Marketing Studio (status
"scheduled", time reached), then marks them published or failed. It never creates, edits or deletes posts of its own.

NOT INSTALLED. Written and tested on the Mac; installing it on the VPS (and its cron line) needs the user's approval,
see the README.

  python3 social_publisher.py [--data DIR] [--dry-run | --live] [--fake] [--max-late 120]
      run once: dry-run (the default) only logs what it would publish
  python3 social_publisher.py --data DIR --add -        (stdin: one queue item JSON, sent by Studio at the Schedule press)
  python3 social_publisher.py --data DIR --cancel ID    (Studio's Cancel press)
  python3 social_publisher.py --data DIR --status       (queue as JSON, for Studio to show results)

Safety:
- a lock file: a second run while one is busy exits straight away
- an item is marked "publishing" before the call, so a crash mid-way never posts twice (it shows as needs-checking)
- items overdue by more than their grace (Studio's late-posting setting when the user pressed Schedule, at least
  2 minutes; --max-late overrides) are marked "missed" and NOT posted late
- Instagram: Meta's limit of 100 API posts per 24 hours is checked before each post
- logs to DIR/social_publisher.log (no tokens, no captions: ids, channels and outcomes only)
"""
import argparse
import fcntl
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "areas"))


def _modules(data):
    if data:
        os.environ["SOCIAL_DIR"] = str(data)
    import _social_store as store  # noqa: PLC0415 - plain modules, no Studio server needed
    import _social_publish as pub  # noqa: PLC0415
    return store, pub


def log(store, msg):
    line = f"{store.iso(store.now())} {msg}"
    with open(store.data_dir() / "social_publisher.log", "a") as f:
        f.write(line + "\n")
    print(line)


def real_publishers():
    import _social_meta as sm  # noqa: PLC0415
    import _social_linkedin as li  # noqa: PLC0415
    return {"facebook": sm.FacebookPublisher(), "instagram": sm.InstagramPublisher(), "linkedin": li.LinkedInPublisher()}


def run_once(store, pub, publishers, live=False, max_late=None, at=None, runner=None, default_late=2):
    """-> {"published": [...], "failed": [...], "missed": [...], "would": [...], "busy": bool}
    runner: only items for this runner ("local" = Studio's scheduler on the Mac, "vps" = this job); None = all.
    Studio's in-process scheduler calls this same function, so both share the lock file and the publishing rules."""
    out = {"published": [], "failed": [], "missed": [], "would": [], "busy": False}
    lock = open(store.data_dir() / "social_publisher.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        out["busy"] = True
        lock.close()
        log(store, "another run is busy: stopping")
        return out
    try:
        items = [x for x in store.queue() if runner is None or x.get("runner", "vps") == runner]
        ready, missed = store.due(items, at, max_late, default_late)
        for it in missed:
            if live:
                if not store.queue_claim(it["id"], it["due"], to="missed"):
                    continue  # cancelled or moved meanwhile
                late = max_late if max_late is not None else it.get("max_late", default_late)
                store.queue_set(it["id"], result={"error": f"more than {late} minutes late (the computer was off or asleep): "
                                                           "not posted. Use Post now if you still want it", "at": store.iso(store.now())})
            out["missed"].append(it["id"])
            log(store, f"{'missed' if live else 'would mark missed'} {it['id']} {it['channel']} due {it['due']}")
        for it in ready:
            if not live:
                out["would"].append(it["id"])
                log(store, f"dry-run: would publish {it['id']} {it['channel']} due {it['due']}")
                continue
            if not store.queue_claim(it["id"], it["due"]):  # cancelled or moved since the list was read: not posted
                out.setdefault("skipped", []).append(it["id"])
                log(store, f"skipped {it['id']} {it['channel']}: cancelled or moved meanwhile")
                continue
            res = pub.run("publish", {it["channel"]: it["payload"]}, publishers, lambda ch, step, msg: None)[it["channel"]]
            if res.get("error"):
                store.queue_set(it["id"], status="failed", result=res)
                out["failed"].append(it["id"])
                log(store, f"failed {it['id']} {it['channel']}: {res['error'][:200]}")
            else:
                store.queue_set(it["id"], status="published", result=res)
                out["published"].append(it["id"])
                log(store, f"published {it['id']} {it['channel']} -> {res.get('post_id')}")
        return out
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def add_item(store, item):
    """Store an item Studio sent at the user's Schedule press (kept as given: same id, status scheduled)."""
    need = {"id", "post_id", "channel", "due", "payload"}
    if not need <= set(item) or item["channel"] not in store.CHANNELS:
        raise ValueError("not a queue item")
    with store._locked("social_queue.json", {"items": []}) as q:
        q["items"] = [x for x in q["items"] if x["id"] != item["id"]]
        q["items"].append({**item, "status": "scheduled", "result": None})
    return item["id"]


def main(argv=None):
    a = argparse.ArgumentParser(description="SoMe publisher job: publishes only what the user scheduled in Studio")
    a.add_argument("--data", help="queue folder (default: Studio's data folder)")
    g = a.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", default=True)
    g.add_argument("--live", action="store_true")
    a.add_argument("--fake", action="store_true", help="FakePublisher: nothing leaves the machine")
    a.add_argument("--max-late", type=int, default=None,
                   help="override the grace in minutes; by default each item uses the late-posting setting it was scheduled with")
    a.add_argument("--runner", default="vps", help='only items for this runner (default "vps"; "all" for every item)')
    a.add_argument("--add", metavar="FILE", help="add one queue item (JSON file, or - for stdin)")
    a.add_argument("--cancel", metavar="ID")
    a.add_argument("--status", action="store_true")
    o = a.parse_args(argv)
    store, pub = _modules(o.data)
    o.runner = None if o.runner == "all" else o.runner
    if o.add:
        item = json.loads(sys.stdin.read() if o.add == "-" else Path(o.add).read_text())
        log(store, f"added {add_item(store, item)} {item['channel']} due {item['due']}")
        return 0
    if o.cancel:
        st = store.queue_cancel_id(o.cancel)
        log(store, f"cancel {o.cancel}: {st}")
        if st == "cancelled":
            return 0
        print(f"too late to cancel: the item is {st}" if st != "missing" else "no such item", file=sys.stderr)
        return 2
    if o.status:
        print(json.dumps({"items": [{k: v for k, v in x.items() if k != "payload"} for x in store.queue()]}, ensure_ascii=False))
        return 0
    pubs = {c: pub.FakePublisher(c) for c in store.CHANNELS} if o.fake else (real_publishers() if o.live else {})
    r = run_once(store, pub, pubs, live=o.live, max_late=o.max_late, runner=o.runner)
    return 1 if r["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
