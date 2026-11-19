# Proposal actions

The closed list of changes the AI may propose for v1.0. Save refuses anything not on it. Every Save and send depends on this list.

## Shape of a proposal

```json
{
  "plan_version": 41,
  "why": "Your knee felt sore after Saturday. Moving the hard session gives it two more days.",
  "sources": [{"url": "https://...", "type": "meta-analysis"}],
  "actions": [
    {"action": "move_session", "item": "p-118", "to": "2026-10-31"},
    {"action": "change_session", "item": "p-120", "parts": [{"part_kind": "interval", "distance_m": 6000}]}
  ]
}
```

A proposal holds one or more actions. Save applies all of them in one transaction, or none.

## The actions

| Action | What it changes | Save checks | Sends to apps |
|---|---|---|---|
| `add_session` | A new plan item | Date not in the past, known family | Yes, apps whose `sends` list the family |
| `move_session` | An item's date | Item exists and is not done | Yes, as an update |
| `change_session` | An item's targets or label | Item exists and is not done | Yes, as an update |
| `remove_session` | Marks an item removed | Item exists and is not done | Delete, or a `needs_you` to-do |
| `restore_session` | Restores a removed plan on an explicit date | Item is removed; date not past | Create a new copy in approved apps |
| `set_goal` | Adds or edits a goal: type, date, target | Date in the future, known goal type | No |
| `set_limit` | A constraint, such as an injury, a day you cannot train, or a maximum session length | Known limit kind | No |
| `log_meal` | A meal with foods, portions, how sure, and sources | Values in plausible ranges | No |
| `accept_document_values` | Values read from a lab PDF or record, each with its page | Document exists, units known | No |
| `correct_record` | A fix to a source record, such as its sport family | Record exists | No |
| `link_sessions`, `unlink_sessions` | Two records counted as one workout, or split apart | Records exist | No |
| `set_source_order` | Your source list for one data type | Data type known | No |
| `unlink_plan_match` | Rejects one workout match for a plan item | Exact current source-record IDs | No |
| `mark_done` | "I did this" for a plan item | Item exists | No |
| `set_forecast` | A coach estimate for a goal with no code forecast: a range, the method, sources and date | Goal exists and has no code forecast | No |

Building a whole plan is a proposal made of many `add_session` actions and a `set_goal`.

To take back a saved change, the AI proposes the opposite change, such as `remove_session` for an added session or `move_session` back to the old date. The person approves it like any change.

## Rules

1. **Unknown action or field:** Save refuses the whole proposal.
2. **Stale proposal:** if `plan_version` is older than the current one, or a limit it depends on changed, Save refuses and asks the AI for a fresh one.
3. **Every Save writes one journal row** holding the proposal, who approved it (your chat's permission prompt or a TFS screen) and the time.
4. **The missed-session rule** may only propose `move_session`, `change_session`, `remove_session` and `add_session`, within the current week.

Later candidates, added only when a need appears: `log_note` (a free note on a day) and `set_preference` (such as "I prefer morning runs").

## Stored journal and versions

The journal's increasing integer `version` is the shared saved-state version.
Before the first save it is zero. A change to goals or limits advances it too,
so a proposal cannot use an outdated constraint. Each save records its proposal,
before-state, approval route (`chat` or `screen`), UTC time and format version.
Journal rows cannot be updated or deleted.

A save has a unique `request_id`. Retrying the same request and content returns
its original receipt; reusing the ID for different content is an error. The
application performs this comparison and validates the proposal again inside
the write transaction. The database enforces request uniqueness. These fields
record the client's approval route; they do not independently prove consent.

Goals store a type, target date and target JSON; limits store a kind and values
JSON. Their action handlers validate the allowed fields and units. Storage alone
does not make a Save tool available.

## Local session proposal API

The local application API currently supports `set_goal`, `set_limit`,
`add_session`, `move_session`, `change_session`, `remove_session`, `restore_session`, `mark_done` and `unlink_plan_match`. Other actions are refused
until their handlers arrive. The MCP tools below expose this same API.

Every proposal supplies `plan_version`, `why`, `sources` and a non-empty
`actions` list. Each source has `url` and `type`. Every action has `action`
and a stable `item` ID. Unknown fields are errors.

| Action | Additional fields |
|---|---|
| add_session | date, family, label, parts; optional goal_ids naming existing goals, start_hint |
| move_session, restore_session | to; optional start_hint |
| change_session | at least one of label, parts, start_hint |
| remove_session, mark_done | none |
| unlink_plan_match | record_ids: complete sorted unique non-empty list from matched_record_ids_json |

`start_hint` is a local 24-hour HH:MM time, or null for no preferred time.
Omission on add means null; omission on move/change keeps the previous hint.
Explicit null clears it. Seconds and timezone offsets are not accepted.

Dates are local YYYY-MM-DD dates, today or later. The caller passes an aware
`now` in the person's local zone; journal time is stored in UTC. Parts are an
ordered list with `part_kind` and optional `label`, `duration_s`,
`distance_m`, `reps`, `weight_kg` and `avg_hr`. Numbers are finite and
non-negative; reps is a whole number. Array order becomes the stored sequence.

Preview calculates the batch in action order without writes, so adding then
changing an item in the same proposal works. Saving validates again inside the
write transaction and stores the final item state once per affected item,
together with its prior state in the journal. A stale proposal, invalid action
or database failure saves nothing. Repeated matching requests return the
original receipt. Ordinary edits of done, removed or replaced items are refused;
restore_session is the explicit route for a removed item.


## Goals, limits and combined history

`set_goal` requires `action`, `item`, `type`, `date` and `target`. The date is
strictly future. Types are `marathon`, `ironman`, `hyrox`, `powerlifting`,
`lean_physique`, `longevity` and `custom`. Use custom for another goal; the
engine does not need sport-specific code. A target has a non-empty
`description` and optionally a finite, non-negative `value` together with a
non-empty `unit`. Units are preserved as supplied, not converted or used for
forecasts here. These fields do not authorize diagnosis or clinician-target changes.

`set_limit` requires `action`, `item`, `kind` and `values`. Supported kinds:

| Kind | Exact values fields |
|---|---|
| injury | description: non-empty user-recorded text |
| unavailable_day | date: YYYY-MM-DD, today or later |
| max_session_length | minutes: finite, positive number |

Goal, limit and session IDs have separate namespaces. Reusing a goal/limit ID
edits it. A session can reference a goal established earlier in the same
proposal. Preview returns `items`, `goals` and `limits` without writes. Saving
any of them advances the global version, so a changed limit invalidates an old
proposal even when no session changed.

Journal rows use format 5. The before-state has separate `items`, `goals` and
`limits` maps, each keyed by ID. An absent prior row is null. It adds `jobs` and
`exclusions` when a Save changes them. All states and the new journal receipt
commit or roll back together.


## Chat MCP tools

- `propose(proposal)` validates and returns the predicted items, goals and
  limits without writing.
- `save(proposal, request_id)` submits the full reviewed proposal through the
  client permission prompt. The server records the route as chat and uses the
  computer's aware local time; there is no AI-supplied approval boolean.

The tools advertise explicit input schemas. Unknown arguments and invalid
proposals fail without a write. Reuse a request ID only for an identical retry.
A JSON-RPC notification has no reply ID and is ignored before tool dispatch,
so it cannot save. Tool annotations describe behavior to the client;
they do not enforce approval. Always-allow client settings can skip prompts.
These tools perform no provider delivery yet. Real installed-client approval
and refusal remain a separate proof from synthetic protocol tests.


## Queued delivery approval

Preview returns `delivery_targets`, a map from item ID to sorted, unique connector
IDs, and `delivery_apps`, their display labels. Targets come from enabled sources
whose manifest accepts the final item's family. Copy that exact target map into
the proposal shown for Save. Omission means no destinations; Save refuses if the
current targets differ. A newly connected app cannot silently receive a plan.

Save cancels superseded pending/not-sent jobs and queues the exact final item in
the same transaction as its plan and journal. Removed items target only existing
linked copies; unlinked removals and done items enqueue nothing. Jobs keep a stable operation ID and immutable approved payload; secrets
never enter them. Provider sends are not enabled yet.

The before-state `jobs` map holds each job ID with its prior status and
next-attempt time, or null for a newly created job.

A move or change after confirmed delivery retains the acknowledged job and
remote link, and creates a new approved job with a new operation ID. The item
returns to saved while the update waits. Preview and Save require previously
linked apps to be enabled and capable of receiving the plan; otherwise reconnect
first. Approval covers the desired item and destination apps. A delivery confirmed
between preview and Save can turn the next dispatch into an update of the known
remote object without changing that approved content or those destinations.
Sending, in-doubt, unknown, conflict and needs-you jobs still block overlapping
changes. Manual deletion tasks and the copies they name
remain history and do not block later work. Invented delivery tests do
not satisfy real-provider proof #29.


## Unlink a wrong plan match

`unlink_plan_match` is separate from `unlink_sessions`, which separates duplicate
imported workouts. It excludes the supplied source records from matching this
plan. Preview and Save require the complete active evidence currently shown by
`matched_record_ids_json`; changed or pending evidence requires a fresh read and
preview. The choice uses stable source IDs, never a rebuilt workout group ID.

Preview returns `match_exclusions` as item/record_ids entries. Unlink alone changes
no planned targets, delivery jobs or remote links. It preserves manual done.
Unlink followed by a plan edit in the same proposal follows action order; the
edit keeps the ordinary delivery rules. Another eligible workout may still
complete the plan.

The before-state `exclusions` map holds each item ID with its prior
source-ID list. Saving an unlink rebuilds matches. The global version advances;
unlink alone does not change the item's payload version. Request-ID retries
return the original receipt.


## Manual deletion and restoration

Removal queues an immutable removed payload for each linked destination.
Hevy returns needs_you, manual_action=delete and the remote ID, without any
HTTP request. The existing credential and capability gates still apply before
dispatch; a disconnected app may need reconnecting before TFS can produce this
instruction. TFS never claims it deleted the routine.

When an app cannot delete a removed session, TFS gives the person that manual
delete task and forgets the link straight away. The person deletes the copy in
the app. TFS does not check the app afterwards. The original needs_you outcome,
result, payload and earlier sent receipts remain. Manual tasks and the copies
they name do not block later work.

restore_session requires a removed item. The explicit to date must be today or
later; existing targets are retained and an optional start_hint follows
move_session rules. Preview's creates_new_copies names restored items with
outgoing destinations. Save queues new operation IDs and creates a new copy in
the approved apps. Dispatch has no retired remote ID.

coach_v1_deliveries exposes manual_action. Its remote fields prefer the job's
recorded result, so an old task stays identifiable after the link is forgotten
or the item is restored.

Manual mark_done retains confirmed history and cancels pending replacements.
It follows the rules for forward mark_done: current links and every acknowledged
receipt stay, only pending/not-sent work is cancelled, and nothing new is
queued. Sending or unresolved work still blocks it.


## Correcting a proven unsent mapping failure

A connector may return needs_you with safe_to_replace=true only when this fresh
operation failed its own mapping/validation before any write. Hevy uses this for
unknown or ambiguous exercise names, unsupported targets and missing sets.
Reconciliation never carries this flag; neither do unknown, conflict,
authentication, manual-deletion or unproven provider failures.

The coach delivery view exposes this evidence as safe_to_replace=1. It permits
an approved correction, not an automatic retry. Preview and Save may supersede
the flagged operation, retain its immutable payload/result, mark it cancelled
and queue a fresh approved operation. Acknowledged jobs and links stay intact.
Other unresolved outcomes still require reconciliation; no user-supplied flag bypasses this rule.
