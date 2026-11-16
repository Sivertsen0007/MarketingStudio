# Run a team server

A Marketing Studio team server lets a team share drafts, an activity feed, comments and @mentions, approvals, a
shared calendar and notifications. One server = one team. It is a small Python program (standard library only) with
all its data in one SQLite file. The wire contract is [PROTOCOL.md](PROTOCOL.md).

This guide runs it with Docker Compose behind Caddy, which gets and renews the HTTPS certificate for you.
[Without Docker](#without-docker) is at the end.

## Contents

1. [What you need](#1-what-you-need)
2. [Start the server](#2-start-the-server)
3. [Create the team and the first owner](#3-create-the-team-and-the-first-owner)
4. [Join from the app](#4-join-from-the-app)
5. [Invite and manage members](#5-invite-and-manage-members)
6. [Roles](#6-roles)
7. [Approvals](#7-approvals)
8. [Notifications and email](#8-notifications-and-email)
9. [Server publishing and the master key](#9-server-publishing-and-the-master-key) (optional, off by default)
10. [Backups and restore](#10-backups-and-restore)
11. [Upgrades](#11-upgrades)
12. [Security notes](#12-security-notes)

Also: [Settings reference](#settings-reference) · [Without Docker](#without-docker) · [Tests](#tests)

## 1. What you need

- A small Linux server or VM you control (1 CPU and 512 MB of memory are plenty for a team), with
  [Docker Engine and the Compose plugin](https://docs.docker.com/engine/install/).
- A domain name for the server, for example `team.example.com`, with a DNS `A` record (and `AAAA` if you use IPv6)
  pointing at the server.
- Ports 80 and 443 open to the internet (Caddy needs both to get the certificate). Nothing else has to be open apart
  from your SSH port.
- Every member: Marketing Studio on their Mac.

## 2. Start the server

```sh
git clone https://github.com/Sivertsen0007/MarketingStudio.git
cd MarketingStudio/teamserver
cp .env.example .env
chmod 600 .env
```

Edit `.env` and set at least:

```sh
TEAMSERVER_DOMAIN=team.example.com
TEAMSERVER_URL=https://team.example.com
```

Make the backups folder writable by the container's user (uid 10001), then start:

```sh
sudo chown 10001:10001 backups
docker compose up -d --build
docker compose ps                                  # teamserver should say "healthy"
curl https://team.example.com/api/v1/health
```

The health answer looks like `{"ok": true, "protocol": 1, "version": "0.1.0", "team": "Team"}`. The first start can
take a minute while Caddy gets the certificate; `docker compose logs caddy` shows how it is going.

What runs: `teamserver` (port 8790, reachable only from Caddy on the internal network, data in the `teamdata`
volume) and `caddy` (ports 80 and 443, certificates in the `caddy_data` volume). Both start again on their own after a
reboot.

## 3. Create the team and the first owner

```sh
docker compose exec teamserver python3 -m teamserver init \
    --email you@example.com --name "Your Name" --team "My team"
```

It prints two invite links for the first owner:

```
Team 'My team' ready.
Invite 1 for you@example.com (owner), single use, expires 2026-11-23T10:00:00Z
  Web:  https://team.example.com/join#<code>
  App:  marketingstudio://join?server=https%3A%2F%2Fteam.example.com&invite=<code>
```

Invites are single use and expire after 7 days. Treat a link like a password until it has been used: whoever uses it
first joins as that person. `init` works once; after that, use `invite` (step 5).

## 4. Join from the app

On the Mac with Marketing Studio:

1. Open the **App** link (Studio opens **Setup > Team** with the link filled in), or open Studio, go to
   **Setup > Team** and paste either link.
2. Press **Join team**.

Studio checks that the server speaks its protocol version, joins, and keeps the device token in the macOS Keychain.
From then on drafts sync with the team, the **Team** tab shows who is here, the activity feed, the members and your
notifications, and **Reviews** in the top bar holds approvals. If the server and the app do not match, Studio says
so and does not sync: *The team server needs an update* (upgrade the server, step 11) or *Marketing Studio needs an
update* (update the app).

## 5. Invite and manage members

Members are managed on the server host:

```sh
docker compose exec teamserver python3 -m teamserver invite --email alex@example.com --role editor --name "Alex"
docker compose exec teamserver python3 -m teamserver members                   # ids, roles, devices, pending invites
docker compose exec teamserver python3 -m teamserver member 4 --approver yes   # may approve other people's work
docker compose exec teamserver python3 -m teamserver member 4 --role admin     # change the role
docker compose exec teamserver python3 -m teamserver member 4 --active no      # someone leaves: signs out all devices
docker compose exec teamserver python3 -m teamserver revoke 7                  # sign out one device (a lost Mac)
```

Send each person their own link by a private channel. Someone with a new Mac is invited again with the same email:
the new device joins as the same member with the same role. A member who was deactivated needs a new invite after
`member <id> --active yes`. The team always keeps at least one active owner.

## 6. Roles

| Role | Can |
|---|---|
| `viewer` | read everything, comment and @mention; never changes drafts |
| `editor` | everything a viewer can, plus edit and send drafts, schedule, ask for approval |
| `admin` | everything an editor can, plus invite, manage members, approval rules, shared team settings |
| `owner` | everything an admin can, plus owners and the owner-only settings (server publishing and its keys) |

On top of the role, a member can be an **approver** (`member <id> --approver yes`). Approvers, admins and owners may
approve other people's work.

## 7. Approvals

Off by default. An owner or admin turns it on per area (Ads, Email, SEO, SoMe) under **Reviews > Approval rules and
owners > Approval needed before going live**. With it on:

- Push live, Publish, Send and Schedule in that area only take changes with a current approval.
- An editor asks for approval and picks reviewers; the reviewer sees exactly what will go live, field by field, and
  approves or requests changes.
- An approval covers the content as it was approved. Any later change to the item needs a new approval.
- Nobody approves their own work, and an approval stops counting when the approver loses the right to approve.

The server stores the rules and the approvals; Studio applies the gate on each Mac.

## 8. Notifications and email

Every member gets notifications on the bell in the app: mentions, review requests and decisions, and whatever they
choose to follow. Each member sets what pings them, email on or off, a webhook (Slack-compatible, or a Telegram bot
URL), instant or hourly / daily digests, Focus and Away under **Team > Notifications**.

To also send email, give the server an SMTP account in `.env`:

```sh
TEAMSERVER_SMTP_HOST=smtp.example.com
TEAMSERVER_SMTP_PORT=587
TEAMSERVER_SMTP_USER=team@example.com
TEAMSERVER_SMTP_PASSWORD=...
TEAMSERVER_SMTP_FROM=team@example.com
```

then `docker compose up -d` (it recreates the server with the new settings). Members can press **Send a test** in
Team > Notifications. Messages are plain text with a link that opens Studio; they never contain tokens or webhook
URLs. Without SMTP the bell still works and the app says the server has no email settings.

## 9. Server publishing and the master key

Optional and **off by default**. Normally a scheduled SoMe post is published by a Mac running Studio. With server
publishing, the team server publishes scheduled posts itself, so they go out while every Mac is closed. It needs all
three of these:

1. A master key on the server. Make one and put it in `.env`:

   ```sh
   docker compose run --rm --no-deps teamserver python3 -m teamserver vault new-key
   # in .env:  TEAMSERVER_MASTER_KEY=<the printed key>
   docker compose up -d
   ```

2. The runner: uncomment the `runner` service in `docker-compose.yml` and run `docker compose up -d` (or add
   `--runner --live` to the server's command instead; use one of the two, not both). Try it first with `--fake`
   instead of `--live`: then nothing is posted.
3. An owner stores the channel keys from Studio (they are encrypted with the master key, and nobody can read them back
   over the API) and switches server publishing on for SoMe.

**Back up the master key** in a password manager, apart from the database backups. It is never written to the
database, a log or a backup. Without it the stored channel keys cannot be opened: you would set a new key, clear the
vault and upload the keys again from Studio.

```sh
docker compose exec teamserver python3 -m teamserver vault check        # does the key open every secret?
docker compose exec teamserver python3 -m teamserver vault clear --yes  # only when the key is lost
```

**Rotate** the key (for example when someone who knew it leaves):

```sh
docker compose stop runner                                           # if you run the runner service
docker compose exec teamserver python3 -m teamserver vault rotate   # asks for the new key twice
# put the new key in .env as TEAMSERVER_MASTER_KEY and in the password manager, then:
docker compose up -d
```

The image includes `openssl`, which the vault encrypts with.

## 10. Backups and restore

Everything the team shares lives in one database file in the `teamdata` volume. Make a backup at any time, while the
server runs:

```sh
docker compose exec -T teamserver python3 -m teamserver backup --out /backups/team-$(date +%F).db
```

It uses SQLite's online backup, so the copy is consistent even while people sync, and it is checked before it is
kept. Backups land in `teamserver/backups/` on the host, readable only by their owner. They hold device tokens only as
hashes, and the publishing keys only encrypted.

**Nightly**, with cron on the host (`crontab -e`; adjust the path; `%` has to be escaped in a crontab):

```cron
15 3 * * * cd /srv/MarketingStudio/teamserver && docker compose exec -T teamserver python3 -m teamserver backup --out /backups/team-$(date +\%F).db >> backups/backup.log 2>&1 && find backups -name 'team-*.db' -mtime +30 -delete
```

That keeps 30 days. Copy the backups off the server too (another machine or object storage): a backup on the same
disk does not survive losing that disk.

**Restore** (the server has to be stopped; the restore refuses otherwise):

```sh
docker compose stop teamserver runner      # leave out runner if you do not run it
docker compose run --rm --no-deps teamserver python3 -m teamserver restore /backups/team-2026-11-16.db --yes
docker compose up -d
```

The database it replaces is kept next to it in the volume as `team.db.before-restore-<time>`. Members do not have to
join again. Anything changed on the server after the backup was made is gone from the server; Macs that still hold
newer edits send them again at their next sync.

## 11. Upgrades

```sh
cd MarketingStudio/teamserver
docker compose exec -T teamserver python3 -m teamserver backup --out /backups/before-upgrade.db --force
git pull                          # or: git checkout v<version> for a given release
docker compose up -d --build
curl https://team.example.com/api/v1/health      # shows the new version
```

The database is upgraded in place when the new server starts. The app and the server agree on a protocol version
(`protocol` in the health answer). When a release changes it, upgrade the server and update the apps around the same
time: until both match, Studio says *The team server needs an update* or *Marketing Studio needs an update* and does
not sync. Local drafts are kept and sync once both match.

With a published image instead of a local build, set `image:` in `docker-compose.yml` to the version you want, then
`docker compose pull && docker compose up -d`.

## 12. Security notes

- **HTTPS only.** Members connect over the internet with a device token, so never offer the server over plain HTTP.
  Caddy redirects HTTP to HTTPS and renews the certificate on its own.
- **Always behind the proxy.** The compose file does not publish port 8790 on the host. Keep it that way: the server
  runs with `--trust-proxy`, so it believes the client address the proxy forwards. Exposed directly, anyone could
  claim any address and get around the per-address limits.
- **Limits.** Built in (a second line behind the proxy): 15 s per socket read, 20 s to send a whole request, 200
  connections in total, 20 per client address, 5 live streams per device, 4 MB per request, 10 join attempts per
  address per minute. Caddy drops slow clients first and refuses request bodies over 5 MB.
- **Tokens.** Device and invite tokens are stored only as sha256 hashes. Revoke a lost Mac at once (`revoke`), and
  deactivate people who leave (`member <id> --active no`).
- **Secrets.** The SMTP password and the master key live in `teamserver/.env` on the host (`chmod 600`, never commit
  it). The image has no secrets in it. The containers run as a non-root user, with a read-only filesystem apart from
  the data volume and the backups folder, and no extra privileges.
- **Webhooks.** Leave `TEAMSERVER_ALLOW_PRIVATE_WEBHOOKS` off on a server people reach over the internet: on, it lets
  members point webhooks at your private network.
- Keep the host patched, allow only SSH, 80 and 443 through the firewall, and keep backups and the master key apart.

## Settings reference

Each `serve` flag can also come from an environment variable; with Docker, set them in `.env`.

| Flag | Environment | Default |
|---|---|---|
| `--data` | `TEAMSERVER_DATA` | `./teamdata` (`/data` in the image) |
| `--port` | `TEAMSERVER_PORT` | `8790` |
| `--host` | `TEAMSERVER_HOST` | `127.0.0.1` (`0.0.0.0` in the image) |
| `--trust-proxy` | `TEAMSERVER_TRUST_PROXY=1` | off (on in the image): use X-Forwarded-For, only behind your own proxy |
| `--public-url` | `TEAMSERVER_URL` | saved from the last time it was given, else the request's Host |
| `--smtp-host` | `TEAMSERVER_SMTP_HOST` | none: email is not offered |
| `--smtp-port` | `TEAMSERVER_SMTP_PORT` | `587` |
| `--smtp-user` | `TEAMSERVER_SMTP_USER` | none (no login) |
| (password) | `TEAMSERVER_SMTP_PASSWORD` | only from the environment, never a flag |
| `--smtp-from` | `TEAMSERVER_SMTP_FROM` | required with a host |
| `--smtp-starttls` / `--no-smtp-starttls` | `TEAMSERVER_SMTP_STARTTLS=1/0` | STARTTLS on |
| `--allow-private-webhooks` | `TEAMSERVER_ALLOW_PRIVATE_WEBHOOKS=1` | off: webhooks must be public `https://` URLs |
| (vault key) | `TEAMSERVER_MASTER_KEY` | none: server publishing stays off |

Commands: `serve`, `init`, `invite`, `members`, `member`, `revoke`, `backup`, `restore`, and with server publishing
`runner` and `vault`. `python3 -m teamserver <command> --help` lists the options of each.

## Without Docker

Python 3.9 or newer and nothing to install (`openssl` only for server publishing). From the repository root:

```sh
python3 -m teamserver init  --data ./teamdata --email you@example.com --name "Your Name" --team "My team" \
    --public-url https://team.example.com
python3 -m teamserver serve --data ./teamdata --port 8790 --trust-proxy
```

The server binds to `127.0.0.1`. Put your own HTTPS reverse proxy in front of it (the [Caddyfile](Caddyfile) here
works with `reverse_proxy 127.0.0.1:8790`), with buffering off for `/api/v1/stream` (server-sent events) and a read
timeout well above a minute, and run the server under a service manager (systemd) as an unprivileged user. Back up
with `python3 -m teamserver backup --data ./teamdata --out backups/team-$(date +%F).db`.

## Tests

```sh
python3 -m unittest discover -s teamserver/tests -v
```
