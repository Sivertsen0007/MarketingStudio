"""Command line: serve, init, invite, members, revoke.

The admin commands work directly on the data directory, so they run on the server host (or in its container)
whether or not the server is running.
"""
import argparse
import os
import sys

from . import __version__
from .app import TeamServer, invite_links
from .store import ApiError, Store, iso


def data_dir(args):
    return os.path.abspath(args.data or os.environ.get("TEAMSERVER_DATA") or "teamdata")


def open_store(args):
    d = data_dir(args)
    os.makedirs(d, exist_ok=True)
    return Store(os.path.join(d, "team.db"))


def public_url(args, store, port=8790):
    """--public-url, then TEAMSERVER_URL, then the URL saved by an earlier command, then localhost. Saves it."""
    url = getattr(args, "public_url", None) or os.environ.get("TEAMSERVER_URL")
    if url:
        url = url.rstrip("/")
        store.set_meta("public_url", url)
        return url
    return store.get_meta("public_url") or "http://localhost:%d" % port


def print_invite(store, args, token, expires, email, role):
    links = invite_links(public_url(args, store), token)
    print("Invite for %s (%s), single use, expires %s" % (email, role, iso(expires)))
    print("  Web:  %s" % links["web"])
    print("  App:  %s" % links["app"])


def cmd_serve(args):
    srv = TeamServer(data_dir(args), host=args.host, port=args.port, verbose=args.verbose,
                     trust_proxy=args.trust_proxy)
    url = args.public_url or os.environ.get("TEAMSERVER_URL")
    if url:
        srv.store.set_meta("public_url", url.rstrip("/"))
    srv.public_url = srv.store.get_meta("public_url")  # None: links use the request's Host header
    print("Team server %s for '%s' on http://%s:%d (data: %s)" % (
        __version__, srv.store.team_name(), args.host, srv.port, data_dir(args)), flush=True)
    if not srv.store.has_owner():
        print("No owner has joined yet. If you have not made the first invite: python3 -m teamserver init --email ... --name ...",
              flush=True)
    srv.serve_forever()
    return 0


def cmd_init(args):
    store = open_store(args)
    if store.has_owner():
        print("This team already has an owner. Use `invite` to add people.", file=sys.stderr)
        return 1
    if args.team:
        store.set_meta("team_name", args.team.strip()[:100])
    token, expires = store.create_invite(args.email, "owner", args.name)
    print("Team '%s' ready." % store.team_name())
    print_invite(store, args, token, expires, args.email.lower(), "owner")
    return 0


def cmd_invite(args):
    store = open_store(args)
    token, expires = store.create_invite(args.email, args.role, args.name)
    print_invite(store, args, token, expires, args.email.lower(), args.role)
    return 0


def cmd_members(args):
    store = open_store(args)
    print("Team '%s'" % store.team_name())
    for m in store.list_members(with_devices=True):
        flags = ("" if m["active"] else " (inactive)") + (" approver" if m["approver"] else "")
        print("%4d  %-28s %-20s %-7s %s%s" % (m["id"], m["email"], m["name"], m["role"], m["colour"], flags))
        for d in m["devices"]:
            print("        device %-4d %-24s last seen %s%s" % (
                d["id"], d["name"], d["last_seen"] or "-", "  REVOKED" if d["revoked"] else ""))
    pending = store.pending_invites()
    if pending:
        print("Pending invites:")
        for i in pending:
            print("        %-28s %-7s expires %s" % (i["email"], i["role"], i["expires_at"]))
    return 0


def cmd_revoke(args):
    store = open_store(args)
    store.revoke_device(None, args.device)
    print("Device %s revoked." % args.device)
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="python3 -m teamserver", description="Marketing Studio team server")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd")

    def add(name, fn, help_text):
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("--data", help="data directory (default: $TEAMSERVER_DATA or ./teamdata)")
        sp.set_defaults(fn=fn)
        return sp

    sp = add("serve", cmd_serve, "run the server")
    sp.add_argument("--port", type=int, default=int(os.environ.get("TEAMSERVER_PORT", "8790")))
    sp.add_argument("--host", default=os.environ.get("TEAMSERVER_HOST", "127.0.0.1"),
                    help="bind address (default 127.0.0.1; use 0.0.0.0 in a container)")
    sp.add_argument("--public-url", help="public base URL for links (or $TEAMSERVER_URL)")
    sp.add_argument("--verbose", action="store_true", help="log requests to stderr")
    sp.add_argument("--trust-proxy", action="store_true",
                    default=os.environ.get("TEAMSERVER_TRUST_PROXY", "") in ("1", "true", "yes"),
                    help="use X-Forwarded-For for the client IP (only behind your own reverse proxy)")

    sp = add("init", cmd_init, "create the team and the first owner invite")
    sp.add_argument("--email", required=True)
    sp.add_argument("--name", required=True)
    sp.add_argument("--team", help="team name shown in the app")
    sp.add_argument("--public-url")

    sp = add("invite", cmd_invite, "invite a member")
    sp.add_argument("--email", required=True)
    sp.add_argument("--role", required=True, choices=["owner", "admin", "editor", "viewer"])
    sp.add_argument("--name")
    sp.add_argument("--public-url")

    add("members", cmd_members, "list members, devices and pending invites")

    sp = add("revoke", cmd_revoke, "revoke a device token")
    sp.add_argument("device", type=int, help="device id (see `members`)")

    args = p.parse_args(argv)
    if not getattr(args, "fn", None):
        p.print_help()
        return 2
    try:
        return args.fn(args)
    except ApiError as e:
        print("Error: %s" % e.message, file=sys.stderr)
        return 1
