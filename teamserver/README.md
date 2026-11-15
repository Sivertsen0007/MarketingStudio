# Marketing Studio team server

A small server that lets a team share Marketing Studio drafts, comments, mentions, presence and an activity feed.
One server = one team. Python 3.9+ standard library only, data in one SQLite file. The wire contract is
[PROTOCOL.md](PROTOCOL.md).

## Run it

From the repository root:

```sh
# 1. Create the team and the first owner invite
python3 -m teamserver init --data ./teamdata --email you@example.com --name "Your Name" --team "My Team" \
    --public-url https://team.example.com

# 2. Start the server
python3 -m teamserver serve --data ./teamdata --port 8790
```

`init` prints two links: a web link (`https://<host>/join#<token>`) and an app link
(`marketingstudio://join?...`). Open either on the Mac with Marketing Studio to join. Invites are single use and
expire after 7 days.

The server binds to `127.0.0.1` by default. In production it should always sit behind an HTTPS reverse proxy
(for example Caddy or nginx): the proxy terminates TLS and absorbs slow or abusive connections. Start the server
with `--trust-proxy` there, so rate limits and the per-address connection cap use the forwarded client address.
Pass `--host 0.0.0.0` only when it runs inside a container or on a private network.

Built-in limits (for direct exposure and as a second line behind the proxy): 15 s per socket read, 20 s to send a
whole request, 200 connections in total, 20 per client address, 5 live streams per device, 4 MB per request.

## Manage the team

These commands work on the data directory directly, so run them on the server host (the server can be running):

```sh
python3 -m teamserver invite  --data ./teamdata --email alex@example.com --role editor --name "Alex"
python3 -m teamserver members --data ./teamdata          # members, device ids, pending invite ids
python3 -m teamserver revoke  --data ./teamdata 3        # revoke device 3
```

Roles: `owner`, `admin`, `editor`, `viewer`. Viewers can read and comment but not change items. Admins and owners
can also invite, change roles and revoke devices from the app.

## Settings

| Flag | Environment | Default |
|---|---|---|
| `--data` | `TEAMSERVER_DATA` | `./teamdata` |
| `--port` | `TEAMSERVER_PORT` | `8790` |
| `--host` | `TEAMSERVER_HOST` | `127.0.0.1` |
| `--trust-proxy` | `TEAMSERVER_TRUST_PROXY=1` | off: use X-Forwarded-For for rate limits only behind your own proxy |
| `--public-url` | `TEAMSERVER_URL` | saved from the last time it was given, else the request's Host |

## Back up

Everything lives in `<data>/team.db` (SQLite in WAL mode). Back it up with `sqlite3 team.db ".backup copy.db"`.
Device and invite tokens are stored only as sha256 hashes.

## Tests

```sh
python3 -m unittest discover -s teamserver/tests -v
```
