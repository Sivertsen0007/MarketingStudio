# Team server protocol (v1)

The contract between the Marketing Studio app (engine/team.py) and the team server (teamserver/).
One server = one team. JSON over HTTPS. Python stdlib only on both sides.

## Auth

- Invite: an admin runs `python3 -m teamserver invite --email a@b.com --role editor [--name "Alex"]`.
  It prints `https://<host>/join#<invite_token>` and `marketingstudio://join?server=<url>&invite=<token>`.
  Invite tokens are single-use and expire after 7 days. The first owner invite: `python3 -m teamserver init --email ... --name ...`.
- `POST /api/v1/join {invite, device_name}` -> `{device_token, member, team}`. No auth header.
- Every other call: `Authorization: Bearer <device_token>`. Tokens are stored hashed (sha256) and can be revoked.
- Roles: `owner`, `admin`, `editor`, `viewer` (+ `approver: bool` flag: may decide reviews). Viewers cannot write items.

## Objects

member  `{id, email, name, role, approver, colour, active}`  (colour: a stable hex from a fixed palette, for tints)
item    `{coll, id, version, data, deleted, author, updated_by, updated_at, changeset}`
        coll: a draftstore collection name, e.g. `ads:4387091070`, `ads:meta_644981006414114`, `email`, `seo:changes`,
        `seo:new_posts`, `seo:map_nodes`, `seo:map_site`, `seo:map_proposals`, `social`, `library`, `calendar`. id: the item
        id inside it.
        data: the item's JSON (a dict). version: server integer, per item, starts at 1.
        changeset: optional `{id, name}` the author grouped it under.
event   `{seq, at, actor, kind, area, coll, id, summary, data}`  kinds: `edit_session`, `push`, `publish`, `undo`, `comment`,
        `mention`, `claim`, `join`, `custom`, `settings`, `send_to_editor`, `review_requested`, `review_decided`
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
- `POST /api/v1/events {kind, area, coll?, id?, summary, data?}` -> event. Clients report push / publish / undo /
  send_to_editor here.

## Comments and mentions

- `GET /api/v1/comments?coll=&id=` -> thread (all comments for that item). `GET /api/v1/comments/counts?coll=` -> `{item_id: {open, total, latest}}`.
- `POST /api/v1/comments {coll, id, body, parent?}` -> comment. Mentions are parsed from `@name` / `@email-local-part` in body
  (case-insensitive, unique match on member name without spaces or email local part) plus an optional explicit `mentions` list.
- `PATCH /api/v1/comments/<id> {body?|resolved?}` (author edits body; anyone non-viewer resolves).
- `GET /api/v1/mentions?unread=1` -> `{mentions:[{comment, read}]}`; `POST /api/v1/mentions/read {ids}`.

## Presence and claims

- `POST /api/v1/presence {area, coll?, id?, editing}` heartbeat (clients send every 30 s while active). `GET /api/v1/presence` -> live list.
- `POST /api/v1/claims {coll, id, note?}`, `DELETE /api/v1/claims {coll, id}`, `GET /api/v1/claims`.

## Reviews (approvals), policy and owners

review `{id, changeset {id,name}|null, title, area, items:[{coll, id, version, hash, content_hash, current_version, stale}],
        requested_by, reviewers:[member id], status, note, decision_note, decided_by, decided_at, at, updated_at, comments,
        stale, decider_ok}`
        status: `open`, `approved`, `changes_requested`, `withdrawn`. `comments` = how many comments the review's own
        thread has (comments with coll `review`, id = the review id; per-item comments use the item's own coll/id).

- `POST /api/v1/reviews {items:[{coll, id, version, content_hash?}], reviewers:[member id], note?, title?, area?, changeset?}`
  (editor+). Each version must be the item's current server version (0 = no such item, e.g. an email campaign with no
  draft); else 409 with `{coll, id, current_version}`: sync and ask again. An item that does not exist (or is deleted) and
  has no `content_hash` is refused (400, nothing to review).
  `content_hash` (64 lower-case hex, only for coll `email`, else 400): the client's hash of what would really go out,
  for items whose live content is not only the draft item (an email: the platform campaign with any Studio draft on top). The server cannot
  see that content, so such items are never `stale` on the server; the client checks the hash again at send time against
  the platform copy (so edits on the platform, or a draft saved there outside Send, end the approval). Instead of items, `{changeset:{id,name}, from_changeset:true}`
  takes every live item whose last write was the requester's, tagged with that changeset. Reviewers must be active
  members who may approve (`approver: true`, or admin / owner), not the requester, and who never wrote any item in the
  review (author, last writer, or any edit session that touched it, at any time; 400 otherwise).
- `GET /api/v1/reviews?mine=1&to_me=1&status=open,approved&current=1&before=<id>` (filters combine; newest first, 200
  per page; `{reviews, more, next_before}`: pass `before=next_before` for the next page).
  `current=1` leaves out reviews whose every item is stale, and approvals whose decider may no longer approve. `GET /api/v1/reviews/<id>` -> review.
- `POST /api/v1/reviews/<id>/decide {decision: approve|request_changes, note?, confirm?:[{coll, id, content_hash}]}`: only
  a listed reviewer who may approve, never the requester (403), only while open (409). Approving needs that the approver
  never wrote any item in it (author, last writer, or any of their edit sessions touching it, before or after the
  request, so an edit put back later still counts; 403), that no item is stale, and for EVERY item an entry in `confirm`: the
  approver's own hash of exactly what their review screen showed, equal to the item's `content_hash` (or `hash` when
  it has none). Otherwise 409: the review has to be asked for again.
- An approval counts only while the member who gave it may still approve: `decider_ok` is false once they are
  deactivated, or are neither approver nor admin / owner any more, and `current=1` then leaves the review out.
- `POST /api/v1/reviews/<id>/withdraw`: the requester or an admin / owner (409 if already withdrawn). A review with
  changes requested holds that content: only an admin / owner may withdraw it (403 for the requester).
- An approval covers an item only while its content is what was asked about. The server keeps a content hash per item:
  sha256 of the item's data as compact JSON with sorted keys (`separators=(",", ":")`, `ensure_ascii=False`), leaving out
  the top-level bookkeeping keys `at, ts, by, updated, updated_at, edited_at, edited_by, problems, warnings, approval,
  approved_by, approved_at`; "" for a missing or deleted item. `stale: true` = the server's copy now hashes differently.
  Clients compute the same hash on their local copy, so a local edit not yet synced also counts as not covered.
- Events: `review_requested` (coll `review`, id = review id, data `{review, reviewers, count}`) and `review_decided`
  (data `{review, status, note}`). SSE `event: review` `{id, status}` goes to the reviewers and the requester on request,
  decision and withdrawal.
- `GET /api/v1/policy` -> `{require_approval: {ads, email, seo, social}}` (all false by default).
  `PUT /api/v1/policy {require_approval: {area: bool}}` (admin/owner; areas not named stay). SSE `event: policy`.
- `GET /api/v1/owners` -> `{owners: {scope: member id}}`. `PUT /api/v1/owners {owners: {scope: member id | null}}`
  (admin/owner; merged, null removes). A scope is a collection or anything inside it, 1-200 characters starting with
  a-z or _, no control characters, e.g. `ads:4387091070/campaign/Brand`, `email/123`, `seo/post/42`,
  `seo:map_nodes/n_ab12cd34`, `social`. Ownership is advisory: clients show "Owned by Alex", nothing is blocked.
  SSE `event: owners` `{}`.
- When `require_approval` is on for an area, Studio's Push live / Publish / Send / Schedule only includes items covered
  by a current approval, and every push in a team carries a one-line why note, reported as a `push` event
  `{kind: "push", area, summary, data: {why, counts, items, scope}}` (`send_to_editor` for Ads Send to Editor, gated the
  same way). If the rules were never fetched and the server can't be reached, live actions wait (fail closed). An item
  whose current content has a `changes_requested` decision in any review stays out of pushes, even if another review
  approved it, until it changes (or an admin / owner withdraws that review). Clients read every page of reviews. The review screen shows every field an item sends live, in
  full, and the approver's `confirm` hash is computed from exactly what it showed. That gate is the client's: the server stores the
  policy and the approvals, it never pushes anything itself.

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
  `event: review` `{id, status}` (reviewers and requester), `event: policy`, `event: owners`,
  `event: settings` `{key, version}`, `event: notify` `{id?, unread?, quiet}` (only to that member).
  A `: ping` comment every 20 s.

## Notifications

notification `{id, kind, actor, area, coll, item_id, text, data, at, read, link}`  link: `marketingstudio://open?coll=..&id=..`
(or `?area=..` when there is no item). For a review, coll is `review` and item_id the review id.

- Rules (what pings a member; the server decides, per member):
  always (on by default; each can be turned off in `rules`): `review_requested` (a review addressed to you),
  `mention` (a comment, or an edit of one, that @mentions you), `review_decided` (your own request approved or sent
  back), `pending_changed` (someone else changed an item you have pending: your last write to it was tagged with a
  changeset and you have not reported a push / publish listing it since), `claim` (a claim on something you own: an
  owner scope that is the collection, `coll/id` or a scope above it, or an item you created).
  follow (off by default, per area `ads`, `email`, `seo`, `social`): `push`, `publish`, `send_to_editor` events in that
  area. Item writes (edit sessions) never ping anyone, except the `pending_changed` case. Your own actions never ping you.
- `GET /api/v1/notify/prefs` -> `{prefs, server: {email}, rules, areas, timings}`; `PUT /api/v1/notify/prefs {partial prefs}`
  (merged; any member, for themselves). prefs:
  `{rules: {kind: bool}, follow: {area: bool}, bell: {timing}, email: {on, timing, last_error},
  webhook: {on, timing, set, host, last_error}, daily_at: "HH:MM", tz_offset: minutes east of UTC (-840..840),
  focus: {on, until, active}, away: {on, until, backup, backup_ok, active}}`. timing: `instant`, `hourly` (sent on the
  hour), `daily` (at daily_at in the member's tz_offset). Defaults: every rule on, follow nothing, bell instant, email and
  webhook off, daily_at 09:00. `until`: ISO 8601 or null (until turned off). `server.email`: the server has SMTP settings.
- Webhook: `PUT {webhook: {url}}` sets it (`""` or null removes it). The URL is write-only (GET shows only `set` and the
  `host`): it often holds a secret. It must be `https://`, without user info, and every address its host resolves to
  must be public (no loopback, private, link-local, shared or reserved ranges, and no IPv6 forms that carry an IPv4
  address: IPv4-compatible, 6to4, Teredo, NAT64); the sender connects to the checked
  address. A server started with `--allow-private-webhooks` (tests, private networks) also accepts private addresses,
  and plain `http://` only for those. Payload: `{"text": "..."}` (Slack-compatible; a Telegram bot URL
  `https://api.telegram.org/bot<token>/sendMessage?chat_id=<id>` works the same way).
- Delivery: every notification goes on the bell. Email (plain text, short, with the app link) goes to the member's
  address when `email.on` and the server has SMTP settings; the webhook when `webhook.on` and a URL is set. Several
  notifications due at once go as one digest. `review_requested` and `mention` are always instant on the bell, whatever
  `bell.timing`. Focus (`focus.on`, until a time or until turned off): the bell still counts (SSE `notify` with
  `quiet: true`), email and webhook wait and go as one digest when Focus ends. Away (`away.on`): the same, plus each new
  review addressed to you is also routed to `away.backup`: the server adds them as a reviewer when they may approve it
  (active, approver or admin / owner, not the requester, never wrote an item in it; `backup_ok` shows the first part),
  else the review stays with the reviewers asked. The review's response does not yet list the backup; GET it again.
- Sending: a background thread with retry and backoff (30 s doubling to 1 h, 6 tries, then the batch is marked failed;
  `last_error` shows the latest failure, never the URL), rate limited per member (email 12, webhook 30 messages an hour;
  the rest waits and goes as one digest). Nothing in a message comes from settings or tokens: only the notification
  text and the app link. Notifications older than 90 days are deleted.
- `GET /api/v1/notifications?unread=1&limit=50&before=<id>` -> `{notifications, unread, more, next_before}` (unread
  first, then read, each newest first; only what is on the bell; pass `before=next_before` for the next page: a positive
  id while still in the unread part, a negative one in the read part). `POST /api/v1/notifications/read {ids: [id]} | {all: true}` -> `{ok, updated, unread}`.
  Marking a `mention` notification read marks its mention read, and `POST /api/v1/mentions/read` marks the matching
  mention notifications read.
- `POST /api/v1/notify/test {channel: email|webhook}` -> `{ok, error?}`: a test message now (5 per 10 minutes).
- SSE `event: notify` `{id?, unread?, quiet}` to the member only, when something lands on their bell or they marked
  notifications read.

## Health

- `GET /api/v1/health` -> `{ok, protocol: 1, version, team}` (no auth).

## Errors and limits

JSON bodies: no NaN or Infinity, integers of at most 20 digits, valid unicode (else 400). A member may have at most 10
active devices (409 on join or device invite beyond that).

`{error: "<message>"}` with 400 (bad input), 401 (no/bad token), 403 (role), 404, 409 (conflict on non-item writes), 429.
