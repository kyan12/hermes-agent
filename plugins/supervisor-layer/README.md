# Supervisor Layer Plugin

First slice of the multi-agent supervisor upgrade.

## What it does

- Runs on `pre_gateway_dispatch` for incoming gateway messages.
- Preserves a portable origin envelope (`platform`, `chat_id`, `thread_id`, `message_id`, user/chat metadata, visibility, fallback route).
- Creates durable task envelopes in `~/.hermes/workspace/supervisor/state/supervisor-tasks.json`.
- Merges duplicate intake from the same origin instead of spawning parallel asks.
- If a task is marked as the active human-attention item, captures Kevin's reply as natural language and rewrites the turn with explicit supervisor context.
- Yields to the existing BlueBubbles daily briefing queue when that queue has an active item, so the generic supervisor does not steal briefing blocker replies.

## Product rule

The supervisor is not a command bot. It should interpret replies like:

- “yeah approve the safer default and keep going”
- “defer this until tomorrow”
- “that’s wrong, use the other client”
- “drop it”

Slash commands can still exist elsewhere in Hermes, but this layer should not require them for normal work.

## Current activation

Enable the plugin in `~/.hermes/config.yaml`:

```yaml
plugins:
  enabled:
    - supervisor-layer
```

Restart the gateway after config/code changes.

## State shape

The store is intentionally JSON for the first slice, matching the daily briefing queue’s lightweight state pattern. Later phases can migrate the same envelopes to SQLite/Postgres without changing the conversational model.

Each task includes:

- `task_id`
- `state`
- `objective`
- `origin`
- `owner`
- `priority`
- `human_interaction`
- `attention`
- `occurrences`
- `human_replies`
- `context_refs`
- `acceptance_criteria`

## Next slices

1. Promote/triage one active ask per Kevin-attention queue across origins, not just per origin.
2. Add explicit task tools (`create_task`, `merge_task`, `resolve_task`, `route_result`).
3. Add worker registry + structured callbacks.
4. Add route-result delivery verification/fallback behavior.
