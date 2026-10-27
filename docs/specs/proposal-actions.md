# Proposal actions

The closed list of changes the AI may propose for v1.0. Save refuses anything not on it. Every Save, send and undo depends on this list.

## Shape of a proposal

```json
{
  "plan_version": 41,
  "why": "Your knee felt sore after Saturday. Moving the hard session gives it two more days.",
  "sources": [{"url": "https://...", "type": "meta-analysis"}],
  "actions": [
    {"action": "move_session", "item": "p-118", "to": "2026-10-31"},
    {"action": "change_session", "item": "p-120", "parts": [{"distance_km": 6, "zone": 2}]}
  ]
}
```

A proposal holds one or more actions. Save applies all of them in one transaction, or none.

## The actions

| Action | What it changes | Save checks | Sends to apps | Undo |
|---|---|---|---|---|
| `add_session` | A new plan item | Date not in the past, known family | Yes, apps whose `sends` list the family | Remove it |
| `move_session` | An item's date | Item exists and is not done | Yes, as an update | Move it back |
| `change_session` | An item's targets or label | Item exists and is not done | Yes, as an update | Restore the old version |
| `remove_session` | Marks an item removed | Item exists and is not done | Delete, or a `needs_you` to-do | Restore it |
| `set_goal` | Adds or edits a goal: type, date, target | Date in the future, known goal type | No | Restore the old goal |
| `set_limit` | A constraint, such as an injury, a day you cannot train, or a maximum session length | Known limit kind | No | Remove it |
| `log_meal` | A meal with foods, portions, how sure, and sources | Values in plausible ranges | No | Delete it |
| `accept_document_values` | Values read from a lab PDF or record, each with its page | Document exists, units known | No | Delete them |
| `correct_record` | A fix to a source record, such as its sport family | Record exists | No | Remove the correction |
| `link_sessions`, `unlink_sessions` | Two records counted as one workout, or split apart | Records exist | No | The opposite action |
| `set_source_order` | Your source list for one data type | Data type known | No | Restore the old order |
| `mark_done` | "I did this" for a plan item | Item exists | No | Clear it |
| `set_forecast` | A coach estimate for a goal with no code forecast: a range, the method, sources and date | Goal exists and has no code forecast | No | Restore the previous estimate |

Building a whole plan is a proposal made of many `add_session` actions and a `set_goal`.

## Rules

1. **Unknown action or field:** Save refuses the whole proposal.
2. **Stale proposal:** if `plan_version` is older than the current one, or a limit it depends on changed, Save refuses and asks the AI for a fresh one.
3. **Every Save writes one journal row** holding the proposal, who approved it (your chat's permission prompt or a TFS screen) and the time.
4. **Undo** is a new journal row with the opposite actions. If an app already received the change and cannot take it back, undo creates a `needs_you` to-do.
5. **The missed-session rule** may only propose `move_session`, `change_session`, `remove_session` and `add_session`, within the current week.

Later candidates, added only when a need appears: `log_note` (a free note on a day) and `set_preference` (such as "I prefer morning runs").
