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
        `seo:new_posts`, `seo:map_nodes`, `seo:map_site`, `seo:map_proposals`, `social`, `library`, `calendar`. id: the item
        id inside it.
        data: the item's JSON (a dict). version: server integer, per item, starts at 1.
        changeset: optional `{id, name}` the author grouped it under.
event   `{seq, at, actor, kind, area, coll, id, summary, data}`  kinds: `edit_session`, `push`, `publish`, `undo`, `comment`,
        `mention`, `claim`, `join`, `custom`, `settings`
comment `{id, coll, item_id, parent, author, body, mentions:[member_id], resolved, at, edited_at}`
presence `{member, area, coll, id, editing, at}`  (expires 120 s after the last heartbeat)
claim   `{coll, id, member, note, at}`

## Items (sync)

- `GET /api/v1/items?since=<cursor>[&limit=1000]` -> `{items:[item...], cursor, more, reset}`. cursor is an opaque int
  (server change seq). since=0 = all. `more: true` = page full, pull again from `cursor`. `reset: true` = `since` is ahead
  of the server (for example after a restore): drop local sync state and pull again from 0.
- coll must match `^[a-z_]+(:[A-Za-z0-9_.-]+)?$` (max 200); item id `^[A-Za-z0-9_.:-]{1,200}$` (integers become strings).
  The same shapes apply wherever coll / id appear (comments, claims, events, presence).
- `POST /api/v1/items {ops:[{op:"put"|"delete", coll, id, base_version, data?, changeset?}]}` -> `{results:[...], cursor}`
  - base_version 0 = new item. If base_version != current version -> result `{ok:false, conflict:true, server:item}`.
    The client merges field by field (top-level keys of data) and retries with the new base_version.
  - ok -> `{ok:true, version}`. Every result also carries `coll` and `id`.
  - A bad op (bad coll / id shape, data not an object or over 512 KB, base_version not an integer, unknown op) fails
    alone with `{ok:false, error}`; the other ops still apply. Delete of a missing item -> `{ok:false, error:"not found"}`.
  - Whole-request errors stay: 403 for viewers, 413 for a body over 4 MB, 400 for a body that is not JSON or has no ops.
- The server groups each member's item writes into one `edit_session` event: a session ends after 10 minutes without
  writes; the event says e.g. "24 changes in ads:4387091070" and lists the item ids. Item writes never notify anyone.

## Events (activity feed)

- `GET /api/v1/events?since=<seq>&limit=200` -> `{events, seq, reset}` (newest last; `reset` as for items).
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

## Team settings

- `GET /api/v1/settings` -> `{settings: {key: setting}}` (every key, also the unset ones); `GET /api/v1/settings/<key>` -> setting.
  setting `{key, value, version, updated_by, updated_at}`; an unset key has `value: null, version: 0`. Everyone reads.
- `PUT /api/v1/settings/<key> {value, base_version?}` (owner/admin, else 403) -> setting. value: a JSON object of at most
  64 KB, or null to clear it (everyone falls back to their own). base_version, when given, must be the current version,
  else 409 `{error, setting}`. Each write: version + 1, an event `{kind: "settings", area: "team", data: {key, version}}`
  and the SSE event `settings` `{key, version}` to everyone. `DELETE /api/v1/settings/<key>` = PUT with value null.
- Keys: `copy_rules`, `seo_sites`, `email_test_addresses`, `social`, `calendar_limits` (unknown key: 404). The server
  checks only the key, the object and its size; the app checks what is inside (engine/team_settings.py describes each
  value) and ignores a value it can't use.

## Library and calendar marks

Two ordinary item collections, synced like drafts (`/api/v1/items`, versions, merge, conflicts):

- `library`: the shared library. id `lib_` + 10 hex; data `{id, kind, text, label, note, by, created}`, kind one of
  `headline`, `cta`, `image` (an https URL), `utm` (`utm_source=..&utm_medium=..&utm_campaign=..`), `email_block` (HTML).
  Every non-viewer may add or remove items (the server's item rules: viewers can't write).
- `calendar`: who scheduled what, for the shared calendar. id `email_<campaign id>`; data
  `{id, kind: "email", campaign_id, when, audience, audience_name, subject, by}`. Written when a campaign is scheduled
  from Studio, removed when its schedule is cancelled. SoMe posts need no mark: their results sync with the post.

## Members (admin)

- `GET /api/v1/me`, `GET /api/v1/members` (admin/owner also get each member's `devices` and pending `invites`
  `[{id, email, name, role, created_by, member_id, expires_at}]`). Admin/owner: `POST /api/v1/invites {email, role, name?}`
  -> `{id, web, app, email, role, device, expires_at}`, `PATCH /api/v1/members/<id> {role?, approver?, active?}`,
  `DELETE /api/v1/devices/<id>` (only an owner revokes an owner's device; anyone revokes their own).
- `DELETE /api/v1/invites/<id>` withdraws a pending invite: its creator or an admin/owner; owner invites only by an owner.
  409 if already used.
- An invite for an email that already belongs to a member is a device invite (`device: true`): only that member may
  make one (any role), it keeps their role, and nobody else (owners included) can make one over the API.
- An invite only works while its creator could still grant it (active; owner for owner invites, admin/owner otherwise).
  Demoting or deactivating a member voids the pending invites they can no longer grant; deactivation also revokes
  all their device tokens.

## Live updates

- `GET /api/v1/stream` (text/event-stream, auth header or `?token=`): `event: items` `{cursor}`, `event: event` `{seq}`,
  `event: presence`, `event: comment` `{coll, id}`, `event: mention` `{comment_id}` (only to the mentioned member),
  `event: settings` `{key, version}`.
  A `: ping` comment every 20 s.

## Health

- `GET /api/v1/health` -> `{ok, protocol: 1, version, team}` (no auth).

## Errors and limits

JSON bodies: no NaN or Infinity, integers of at most 20 digits, valid unicode (else 400). A member may have at most 10
active devices (409 on join or device invite beyond that).

`{error: "<message>"}` with 400 (bad input), 401 (no/bad token), 403 (role), 404, 409 (conflict on non-item writes), 429.
