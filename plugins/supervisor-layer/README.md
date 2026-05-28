# Supervisor Layer Plugin

First slice of the multi-agent supervisor upgrade.

## What it does

- Runs on `pre_gateway_dispatch` for incoming gateway messages.
- Preserves a portable origin envelope (`platform`, `chat_id`, `thread_id`, `message_id`, user/chat metadata, visibility, fallback route).
- Creates durable task envelopes in `~/.hermes/workspace/supervisor/state/supervisor-tasks.json`.
- Exposes a small task lifecycle API (`find_task`, `request_human_attention`, `complete_task`, `append_worker_callback`, `render_attention_ask`).
- Keeps only one active human ask per origin; additional asks for the same origin are queued and promoted after the active task completes.
- Merges duplicate intake from the same origin instead of spawning parallel asks.
- Defines a portable JSON worker registry contract with capability/risk/cadence metadata (`default_worker_registry`, `normalize_worker_registry`, `plan_worker_dispatch`, `assign_worker_to_task`, `assign_stored_worker_to_task`). The registry avoids Hermes-only classes, secrets, or callables so the same envelope can be copied into other agent systems and implemented with their native dispatch transport.
- Records worker callbacks and can turn worker blockers into queued Kevin-attention items without letting workers DM Kevin directly.
- Builds route-preserving delivery plans for worker results, sends completed results through the shared messaging tool path, and records delivery/fallback attempts for auditability. Durable workers should call `deliver_stored_task_result()` so load/mutate/save happens under the supervisor task-store lock; `deliver_task_result()` is the in-memory/testable core.
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
- `worker_assignment` (optional portable dispatch plan)
- `priority`
- `human_interaction`
- `attention`
- `occurrences`
- `human_replies`
- `context_refs`
- `acceptance_criteria`

## Portable worker registry

The worker registry is intentionally plain JSON:

```json
{
  "schema_version": 1,
  "portable": true,
  "selection_policy": {
    "optimize_for": "kevin_attention",
    "default_max_risk": "medium"
  },
  "workers": [
    {
      "worker_id": "code-crab",
      "display_name": "Code Crab",
      "capabilities": ["code", "testing", "github"],
      "risk_level": "medium",
      "cadence": "on_demand",
      "handoff_contract": {
        "input": "supervisor_task_envelope",
        "output": "worker_callback_envelope",
        "transport": "agent_system_native"
      }
    }
  ]
}
```

Copying the supervisor pattern to another agent system should only require implementing that system's native transport for the same fixed `supervisor_task_envelope → worker_callback_envelope` contract; registry input cannot override the envelope names or transport label. Worker handoffs use per-task opaque `task_id` and `origin_ref` values derived from separate SHA-256 namespaces over the supervisor's internal task ID (`worker-task|...` and `worker-origin|...`) plus `route_policy: supervisor_managed`; full chat/thread/user route metadata and the supervisor's internal task ID stay supervisor-only so workers cannot DM Kevin or leak routing context. Worker-facing handoffs intentionally omit free-text task title/objective/criteria; workers receive only the opaque task reference, required capabilities, sanitized allowlisted refs (`gbrain`, `files`, `urls`, `repos`), and callback contract. Sanitization drops route-like keys, non-string/non-ASCII scalar refs, exact origin values after Unicode/case/confusable normalization, compact route-like strings (`channel123`, `thread456`), and route-like values embedded inside otherwise-allowed refs instead of redacting them into worker-visible hints. Passing `registry=None` uses the default registry; passing an explicit empty or malformed registry fails closed as `no_match`. Unsafe-only required capabilities also fail closed rather than becoming match-all dispatch. The supervisor produces dispatch plans but does not auto-run high-risk workers; those become normal one-atomic-action Kevin asks.

## Next slices

1. Promote/triage one active ask across Kevin globally, not just per origin.
2. Add a supervisor dashboard/status rendering surface.
3. Migrate the JSON task store to SQLite/Postgres once the lifecycle contract stabilizes.
