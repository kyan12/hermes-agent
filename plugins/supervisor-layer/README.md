# Supervisor Layer Plugin

First slice of the multi-agent supervisor upgrade.

## What it does

- Runs on `pre_gateway_dispatch` for incoming gateway messages.
- Preserves a portable origin envelope (`platform`, `chat_id`, `thread_id`, `message_id`, user/chat metadata, visibility, fallback route).
- Creates durable task envelopes in `~/.hermes/workspace/supervisor/state/supervisor-tasks.json`.
- Exposes a small task lifecycle API (`find_task`, `request_human_attention`, `complete_task`, `append_worker_callback`, `render_attention_ask`).
- Keeps only one active human ask per origin; additional asks for the same origin are queued and promoted after the active task completes.
- Merges duplicate intake from the same origin instead of spawning parallel asks.
- Records worker callbacks and can turn worker blockers into queued Kevin-attention items without letting workers DM Kevin directly.
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

1. Add route-result delivery verification/fallback behavior.
2. Add a worker registry with capability/risk/cadence metadata.
3. Promote/triage one active ask across Kevin globally, not just per origin.
4. Add a supervisor dashboard/status rendering surface.
