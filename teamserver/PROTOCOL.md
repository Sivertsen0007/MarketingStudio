# Team server protocol (v1)

The contract between the Marketing Studio app (engine/team.py) and the team server (teamserver/).
One server = one team. JSON over HTTPS. Python stdlib only on both sides.

## Auth

- Invite: an admin runs `python3 -m teamserver invite --email a@b.com --role editor [--name "Alex"]`.
  It prints `https://<host>/join#<invite_token>` and `marketingstudio://join?server=<url>&invite=<token>`.
  Invite tokens are single-use and expire after 7 days. The first owner invite: `python3 -m teamserver init --email ... --name ...`.
- `POST /api/v1/join {invite, device_name}` -> `{device_token, member, team}`. No auth header.
- Every other call: `Authorization: Bearer <device_token>`. Tokens are stored hashed (sha256) and can be revoked.
- Roles: `owner`, `admin`, `editor`, `viewer` (+ `approver: bool` flag, used in a later phase). Viewers cannot write items.

## Objects

member  `{id, email, name, role, approver, colour, active}`  (colour: a stable hex from a fixed palette, for tints)
item    `{coll, id, version, data, deleted, author, updated_by, updated_at, changeset}`
        coll: a draftstore collection name, e.g. `ads:4387091070`, `ads:meta_644981006414114`, `email`, `seo:changes`,
        `seo:new_posts`, `seo:map_nodes`, `seo:map_site`, `seo:map_proposals`, `social`. id: the item id inside it.
        data: the item's JSON (a dict). version: server integer, per item, starts at 1.
        changeset: optional `{id, name}` the author grouped it under.
event   `{seq, at, actor, kind, area, coll, id, summary, data}`  kinds: `edit_session`, `push`, `publish`, `undo`, `comment`,
        `mention`, `claim`, `join`, `custom`
comment `{id, coll, item_id, parent, author, body, mentions:[member_id], resolved, at, edited_at}`
presence `{member, area, coll, id, editing, at}`  (expires 120 s after the last heartbeat)
claim   `{coll, id, member, note, at}`

## Items (sync)

- `GET /api/v1/items?since=<cursor>` -> `{items:[item...], cursor}`. cursor is an opaque int (server change seq). since=0 = all.
- `POST /api/v1/items {ops:[{op:"put"|"delete", coll, id, base_version, data?, changeset?}]}` -> `{results:[...], cursor}`
  - base_version 0 = new item. If base_version != current version -> result `{ok:false, conflict:true, server:item}`.
    The client merges field by field (top-level keys of data) and retries with the new base_version.
  - ok -> `{ok:true, version}`.
- The server groups each member's item writes into one `edit_session` event: a session ends after 10 minutes without
  writes; the event says e.g. "24 changes in ads:4387091070" and lists the item ids. Item writes never notify anyone.

## Events (activity feed)

- `GET /api/v1/events?since=<seq>&limit=200` -> `{events, seq}` (newest last).
- `POST /api/v1/events {kind, area, coll?, id?, summary, data?}` -> event. Clients report push / publish / undo here.

## Comments and mentions

- `GET /api/v1/comments?coll=&id=` -> thread (all comments for that item). `GET /api/v1/comments/counts?coll=` -> `{item_id: {open, total, latest}}`.
- `POST /api/v1/comments {coll, id, body, parent?}` -> comment. Mentions are parsed from `@name` / `@email-local-part` in body
  (case-insensitive, unique match on member name without spaces or email local part) plus an optional explicit `mentions` list.
- `PATCH /api/v1/comments/<id> {body?|resolved?}` (author edits body; anyone non-viewer resolves).
- `GET /api/v1/mentions?unread=1` -> `{mentions:[{comment, read}]}`; `POST /api/v1/mentions/read {ids}`.

## Presence and claims

- `POST /api/v1/presence {area, coll?, id?, editing}` heartbeat (clients send every 30 s while active). `GET /api/v1/presence` -> live list.
- `POST /api/v1/claims {coll, id, note?}`, `DELETE /api/v1/claims {coll, id}`, `GET /api/v1/claims`.

## Members (admin)

- `GET /api/v1/me`, `GET /api/v1/members`. Admin/owner: `POST /api/v1/invites {email, role, name?}` -> links,
  `PATCH /api/v1/members/<id> {role?, approver?, active?}`, `DELETE /api/v1/devices/<id>`.

## Live updates

- `GET /api/v1/stream` (text/event-stream, auth header or `?token=`): `event: items` `{cursor}`, `event: event` `{seq}`,
  `event: presence`, `event: comment` `{coll, id}`, `event: mention` `{comment_id}` (only to the mentioned member).
  A `: ping` comment every 20 s.

## Health

- `GET /api/v1/health` -> `{ok, protocol: 1, version, team}` (no auth).

## Errors

`{error: "<message>"}` with 400 (bad input), 401 (no/bad token), 403 (role), 404, 409 (conflict on non-item writes), 429.
