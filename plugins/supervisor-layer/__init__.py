"""Supervisor layer gateway plugin.

This is the first productizable slice of the multi-agent supervisor model.  It
uses the same principle as Kevin's daily briefing queue: keep durable task state,
preserve the origin route, and interpret replies as a collaborative natural-
language loop rather than a command protocol.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import tempfile
import threading
import unicodedata
import uuid
from pathlib import Path
from typing import Any

try:
    import fcntl
except Exception:  # pragma: no cover - non-POSIX fallback
    fcntl = None  # type: ignore[assignment]

try:
    from utils import atomic_json_write as _atomic_json_write
except Exception:  # pragma: no cover - standalone/plugin test fallback
    _atomic_json_write = None  # type: ignore[assignment]

try:
    from hermes_constants import get_hermes_home as _core_hermes_home
except Exception:  # pragma: no cover - standalone/plugin test fallback
    _core_hermes_home = None  # type: ignore[assignment]

OPEN_STATES = {"inbox", "triaged", "doing", "waiting_agent", "needs_human", "in_discussion", "blocked", "review"}
ACTIVE_ATTENTION_STATES = {"needs_human", "in_discussion"}
COMMAND_RE = re.compile(r"^\s*/\w+")
PHONE_RE = re.compile(r"\D+")
BRIEFING_ACTIVE_STATUSES = {"sent", "in_discussion", "answered_pending_followup"}
STANDALONE_CONTINUATION_RE = re.compile(
    r"^\s*(continue|keep going|go on|proceed|yes|yeah|yep|no|nope|done|ok|okay|wait|stop)\s*[.!?]*\s*$",
    re.IGNORECASE,
)
_IN_PROCESS_STORE_LOCK = threading.RLock()


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def _hermes_home() -> Path:
    env_home = os.environ.get("HERMES_HOME")
    if env_home:
        return Path(env_home)
    if _core_hermes_home is not None:
        return Path(_core_hermes_home())
    return Path.home() / ".hermes"


def _state_dir() -> Path:
    return _hermes_home() / "workspace" / "supervisor" / "state"


def task_store_path() -> Path:
    return _state_dir() / "supervisor-tasks.json"


def _fresh_task_store(recovery_note: str | None = None) -> dict[str, Any]:
    data: dict[str, Any] = {
        "schema_version": 1,
        "description": "Supervisor-layer task envelopes with origin-routed natural-language attention state.",
        "tasks": [],
        "created_at": _now_iso(),
    }
    if recovery_note:
        data["state_recovery_note"] = recovery_note
    return data


def _backup_unreadable_task_store(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H%M%S%f")
    for index in range(100):
        suffix = f".corrupt-{stamp}" if index == 0 else f".corrupt-{stamp}-{index}"
        backup_path = path.with_name(f"{path.name}{suffix}")
        try:
            path.replace(backup_path)
            return backup_path
        except FileExistsError:
            continue
        except Exception:
            return None
    return None


def load_task_store() -> dict[str, Any]:
    path = task_store_path()
    if not path.exists():
        return _fresh_task_store()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("task store root must be an object")
        data.setdefault("schema_version", 1)
        data.setdefault("tasks", [])
        if not isinstance(data.get("tasks"), list):
            raise ValueError("task store tasks must be a list")
        return data
    except Exception as exc:
        # Do not let corrupt state break message delivery. Preserve the broken
        # file for manual recovery and start a fresh in-memory store.
        backup_path = _backup_unreadable_task_store(path)
        note = f"Ignored unreadable store at {path}: {exc}"
        if backup_path is not None:
            note += f"; backup saved to {backup_path}"
        return _fresh_task_store(note)


@contextlib.contextmanager
def _task_store_lock():
    """Serialize supervisor task-store mutations across threads/processes."""
    path = task_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with _IN_PROCESS_STORE_LOCK:
        with open(lock_path, "a+", encoding="utf-8") as lock_file:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    if _atomic_json_write is not None:
        _atomic_json_write(path, data, indent=2)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.stem}_",
        suffix=".tmp",
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def save_task_store(data: dict[str, Any]) -> None:
    path = task_store_path()
    data["updated_at"] = _now_iso()
    _atomic_write_json(path, data)


def _platform_value(event: Any) -> str:
    platform = getattr(getattr(event, "source", None), "platform", "")
    return str(getattr(platform, "value", platform) or "").lower()


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _fingerprint_text(text: str) -> str:
    normalized_text = unicodedata.normalize("NFKC", _clean_text(text)).casefold()
    normalized = "".join(
        char if (char.isalnum() or char.isspace()) else " "
        for char in normalized_text
    )
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not normalized:
        normalized = normalized_text.strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _is_standalone_continuation(text: str) -> bool:
    """Return true for context-control replies that are not standalone tasks."""
    return bool(STANDALONE_CONTINUATION_RE.match(_clean_text(text)))


def _is_low_signal_standalone(text: str) -> bool:
    """Return true for bare punctuation/emoji that should not become tasks."""
    cleaned = _clean_text(text)
    return bool(cleaned) and not any(char.isalnum() for char in cleaned)


def _task_id(origin: dict[str, Any], text: str) -> str:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H%M%S")
    seed = "|".join([
        str(origin.get("platform") or ""),
        str(origin.get("chat_id") or ""),
        str(origin.get("thread_id") or ""),
        text,
        stamp,
        uuid.uuid4().hex,
    ])
    return f"sup_{stamp}_{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:8]}"


def _origin_key(origin: dict[str, Any]) -> str:
    return "|".join([
        str(origin.get("platform") or ""),
        str(origin.get("chat_id") or ""),
        str(origin.get("thread_id") or ""),
        str(origin.get("user_id") or ""),
    ])


def _visibility_for_chat_type(chat_type: str) -> str:
    if chat_type == "dm":
        return "private"
    if chat_type in {"group", "channel", "thread"}:
        return "team"
    return "team"


def origin_envelope_from_event(event: Any) -> dict[str, Any]:
    """Build the portable origin envelope carried by supervisor tasks.

    This mirrors the cron `deliver=origin` shape, but keeps enough extra
    provenance for task merging and eventual product UI/audit views.
    """
    source = getattr(event, "source", None)
    chat_type = str(getattr(source, "chat_type", "") or "dm")
    message_id = (
        getattr(event, "message_id", None)
        or getattr(source, "message_id", None)
        or ""
    )
    origin: dict[str, Any] = {
        "platform": _platform_value(event),
        "chat_id": str(getattr(source, "chat_id", "") or ""),
        "thread_id": getattr(source, "thread_id", None),
        "message_id": str(message_id) if message_id else None,
        "user_id": getattr(source, "user_id", None),
        "user_name": getattr(source, "user_name", None),
        "chat_name": getattr(source, "chat_name", None),
        "chat_type": chat_type,
        "visibility": _visibility_for_chat_type(chat_type),
        "fallback_route": "origin",
    }
    for key in ("guild_id", "parent_chat_id"):
        value = getattr(source, key, None)
        if value:
            origin[key] = value
    return origin


def _same_origin(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return _origin_key(a) == _origin_key(b)


def _find_active_attention_task(data: dict[str, Any], origin: dict[str, Any]) -> dict[str, Any] | None:
    tasks = data.get("tasks", [])
    if not isinstance(tasks, list):
        return None
    for task in reversed(tasks):
        if not isinstance(task, dict):
            continue
        attention = task.get("attention") if isinstance(task.get("attention"), dict) else {}
        if (
            task.get("state") in ACTIVE_ATTENTION_STATES
            and attention.get("active") is True
            and _same_origin(task.get("origin") or {}, origin)
        ):
            return task
    return None


def find_task(data: dict[str, Any], task_id: str) -> dict[str, Any] | None:
    tasks = data.get("tasks", [])
    if not isinstance(tasks, list):
        return None
    for task in tasks:
        if isinstance(task, dict) and task.get("task_id") == task_id:
            return task
    return None


def _attention_for(task: dict[str, Any]) -> dict[str, Any]:
    attention = task.get("attention")
    if not isinstance(attention, dict):
        attention = {}
        task["attention"] = attention
    attention.setdefault("reply_style", "natural_language")
    return attention


def _set_attention_payload(
    task: dict[str, Any],
    *,
    ask: str,
    recommended_default: str | None = None,
    why_now: str | None = None,
    where: str | None = None,
) -> dict[str, Any]:
    attention = _attention_for(task)
    attention["ask"] = ask
    attention["reply_style"] = "natural_language"
    optional_payload = {
        "recommended_default": recommended_default,
        "why_now": why_now,
        "where": where,
    }
    for key, value in optional_payload.items():
        if value is None:
            attention.pop(key, None)
        else:
            attention[key] = value
    return attention


def _promote_next_attention_for_origin(data: dict[str, Any], origin: dict[str, Any]) -> dict[str, Any] | None:
    tasks = data.get("tasks", [])
    if not isinstance(tasks, list):
        return None
    if _find_active_attention_task(data, origin) is not None:
        return None
    for task in tasks:
        if not isinstance(task, dict):
            continue
        if task.get("state") != "needs_human":
            continue
        if not _same_origin(task.get("origin") or {}, origin):
            continue
        attention = _attention_for(task)
        if attention.get("ask") and attention.get("queued") is True:
            attention["active"] = True
            attention["queued"] = False
            attention["activated_at"] = _now_iso()
            task["updated_at"] = attention["activated_at"]
            return task
    return None


def request_human_attention(
    data: dict[str, Any],
    task_id: str,
    *,
    ask: str,
    recommended_default: str | None = None,
    why_now: str | None = None,
    where: str | None = None,
) -> str:
    """Activate or queue one natural-language human ask for a task's origin."""
    task = find_task(data, task_id)
    if task is None:
        raise KeyError(f"unknown supervisor task: {task_id}")
    now = _now_iso()
    origin = task.get("origin") or {}
    active = _find_active_attention_task(data, origin)
    attention = _set_attention_payload(
        task,
        ask=ask,
        recommended_default=recommended_default,
        why_now=why_now,
        where=where,
    )
    task["state"] = "needs_human"
    task["updated_at"] = now
    if active is None or active is task:
        attention["active"] = True
        attention["queued"] = False
        attention["activated_at"] = now
        return "activated"
    attention["active"] = False
    attention["queued"] = True
    attention["queued_at"] = now
    return "queued"


def complete_task(data: dict[str, Any], task_id: str, *, result: str | None = None) -> dict[str, Any] | None:
    """Mark a task done and promote the next queued ask for the same origin."""
    task = find_task(data, task_id)
    if task is None:
        raise KeyError(f"unknown supervisor task: {task_id}")
    now = _now_iso()
    origin = task.get("origin") or {}
    task["state"] = "done"
    task["updated_at"] = now
    task["completed_at"] = now
    if result is not None:
        task["result"] = result
    attention = _attention_for(task)
    attention["active"] = False
    attention["queued"] = False
    return _promote_next_attention_for_origin(data, origin)


def append_worker_callback(
    data: dict[str, Any],
    task_id: str,
    *,
    worker: str,
    status: str,
    summary: str,
    needs_human: bool = False,
    ask: str | None = None,
    recommended_default: str | None = None,
    why_now: str | None = None,
    where: str | None = None,
) -> str | None:
    """Record a worker callback and optionally request Kevin attention."""
    task = find_task(data, task_id)
    if task is None:
        raise KeyError(f"unknown supervisor task: {task_id}")
    now = _now_iso()
    callbacks = task.get("callbacks")
    if not isinstance(callbacks, list):
        callbacks = []
        task["callbacks"] = callbacks
    callbacks.append({
        "at": now,
        "worker": worker,
        "status": status,
        "summary": summary,
    })
    task["updated_at"] = now
    if needs_human:
        return request_human_attention(
            data,
            task_id,
            ask=ask or summary,
            recommended_default=recommended_default,
            why_now=why_now,
            where=where,
        )
    if status in {"done", "completed"}:
        task["state"] = "review"
    elif status in {"blocked", "needs_human"}:
        task["state"] = "blocked"
    return None


def render_attention_ask(task: dict[str, Any]) -> str:
    """Render the one atomic Kevin-facing ask for an active/queued task."""
    attention = _attention_for(task)
    ask = attention.get("ask") or task.get("objective") or task.get("title") or "Review this supervisor item."
    where = attention.get("where") or "origin thread"
    default = attention.get("recommended_default") or "use your judgment"
    why_now = attention.get("why_now") or "unblocks the next supervisor step"
    return (
        "Needs Kevin — 2 min\n\n"
        f"Do exactly this: {ask}\n"
        f"Where: {where}\n"
        f"Recommended default: {default}\n"
        f"Why now: {why_now}\n"
        "Reply naturally — e.g. approve, defer, pick another option, or tell me what is wrong."
    )


def delivery_target_for_origin(origin: dict[str, Any]) -> str | None:
    """Return the explicit gateway target string for a stored origin envelope.

    Background workers do not have the live gateway session's implicit
    ``deliver=origin`` context. This helper turns the portable origin envelope
    into the same explicit target shape accepted by the messaging layer, while
    preserving Discord thread routing when parent/thread IDs are available.
    """
    platform = str(origin.get("platform") or "").strip().lower()
    chat_id = str(origin.get("chat_id") or "").strip()
    thread_id = str(origin.get("thread_id") or "").strip()
    parent_chat_id = str(origin.get("parent_chat_id") or "").strip()
    chat_type = str(origin.get("chat_type") or "").strip().lower()
    if not platform or not chat_id:
        return None
    if platform == "discord" and chat_type == "thread" and thread_id:
        if parent_chat_id:
            return f"{platform}:{parent_chat_id}:{thread_id}"
        return f"{platform}:{thread_id}"
    if thread_id and thread_id != chat_id:
        return f"{platform}:{chat_id}:{thread_id}"
    return f"{platform}:{chat_id}"


def delivery_plan_for_task(task: dict[str, Any]) -> dict[str, Any]:
    """Build a route-preserving delivery plan for a supervisor result.

    The plan is data-only on purpose. Callers still perform the actual send and
    then record the outcome with ``record_delivery_attempt``. Keeping this
    separate makes it safe to use in tests, cron jobs, and future worker
    callbacks without accidentally emitting public messages.
    """
    raw_origin = task.get("origin")
    origin: dict[str, Any] = dict(raw_origin) if isinstance(raw_origin, dict) else {}
    primary = delivery_target_for_origin(origin)
    platform = str(origin.get("platform") or "").strip().lower()
    fallback = str(origin.get("fallback_route") or "origin").strip() or "origin"
    if fallback == "origin":
        fallback_target = primary or (platform if platform else None)
    else:
        fallback_target = fallback
    return {
        "task_id": task.get("task_id"),
        "primary_target": primary,
        "fallback_target": fallback_target,
        "origin": origin,
        "visibility": origin.get("visibility") or "team",
    }


def record_delivery_attempt(
    data: dict[str, Any],
    task_id: str,
    *,
    target: str | None,
    status: str,
    message_id: str | None = None,
    error: str | None = None,
    fallback_target: str | None = None,
) -> dict[str, Any]:
    """Append an auditable delivery attempt to a supervisor task."""
    task = find_task(data, task_id)
    if task is None:
        raise KeyError(f"unknown supervisor task: {task_id}")
    normalized_status = str(status or "").strip().lower()
    if normalized_status not in {"success", "failed", "fallback_queued"}:
        raise ValueError("delivery status must be success, failed, or fallback_queued")
    if normalized_status == "fallback_queued" and not fallback_target:
        raise ValueError("fallback_queued delivery attempts require fallback_target")
    now = _now_iso()
    deliveries = task.get("deliveries")
    if not isinstance(deliveries, list):
        deliveries = []
        task["deliveries"] = deliveries
    attempt: dict[str, Any] = {
        "at": now,
        "target": target,
        "status": normalized_status,
    }
    if message_id:
        attempt["message_id"] = message_id
    if error:
        attempt["error"] = error[:500]
    if fallback_target:
        attempt["fallback_target"] = fallback_target
    deliveries.append(attempt)
    task["delivery_status"] = normalized_status
    task["updated_at"] = now
    if normalized_status == "success":
        task["delivered_at"] = now
        task.pop("pending_fallback_target", None)
    elif fallback_target:
        task["pending_fallback_target"] = fallback_target
    return attempt


def _normalize_send_result(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        return result
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
            if isinstance(parsed, dict):
                return parsed
            return {"success": False, "error": f"non-object send result: {type(parsed).__name__}"}
        except Exception:
            return {"error": result[:500]}
    return {"success": bool(result)}


def _send_with_message_tool(target: str, message: str) -> dict[str, Any]:
    from tools.send_message_tool import send_message_tool

    return _normalize_send_result(send_message_tool({"action": "send", "target": target, "message": message}))


def _send_result_ok(result: dict[str, Any]) -> bool:
    return bool(result.get("success")) and not result.get("error")


def _send_result_message_id(result: dict[str, Any]) -> str | None:
    value = result.get("message_id") or result.get("id")
    if value is None and isinstance(result.get("raw_response"), dict):
        raw = result["raw_response"]
        value = raw.get("message_id") or raw.get("id")
    return str(value) if value is not None else None


def _send_result_error(result: dict[str, Any]) -> str | None:
    error = result.get("error") or result.get("message")
    return str(error) if error is not None else None


def _call_sender(sender: Any, target: str, message: str) -> dict[str, Any]:
    try:
        return _normalize_send_result(sender(target, message))
    except Exception as exc:
        return {"error": str(exc) or exc.__class__.__name__}


def _persist_delivery_audit(data: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    try:
        save_task_store(data)
        result["audit_persisted"] = True
    except Exception as exc:
        result["audit_persisted"] = False
        result["audit_error"] = str(exc) or exc.__class__.__name__
    return result


def deliver_task_result(
    data: dict[str, Any],
    task_id: str,
    message: str,
    *,
    sender: Any | None = None,
    persist: bool = False,
) -> dict[str, Any]:
    """Send a supervisor task result to its preserved origin and audit attempts.

    ``sender`` is injectable for tests and worker runtimes.  When omitted this
    uses the same ``send_message`` tool path as agents, so target parsing and
    platform-specific behavior stay centralized.
    """
    task = find_task(data, task_id)
    if task is None:
        raise KeyError(f"unknown supervisor task: {task_id}")
    clean_message = str(message or "").strip()
    if not clean_message:
        raise ValueError("delivery message must be non-empty")

    plan = delivery_plan_for_task(task)
    primary = plan.get("primary_target")
    fallback = plan.get("fallback_target")
    send = sender or _send_with_message_tool
    if not primary:
        record_delivery_attempt(data, task_id, target=None, status="failed", error="No primary delivery target resolved")
        failure = {"success": False, "error": "No primary delivery target resolved", "plan": plan}
        if persist:
            return _persist_delivery_audit(data, failure)
        return failure
    primary_target = str(primary)

    result = _call_sender(send, primary_target, clean_message)
    if _send_result_ok(result):
        record_delivery_attempt(
            data,
            task_id,
            target=primary_target,
            status="success",
            message_id=_send_result_message_id(result),
        )
        success = {"success": True, "target": primary_target, "result": result, "plan": plan}
        if persist:
            return _persist_delivery_audit(data, success)
        return success

    primary_error = _send_result_error(result) or "send failed"
    fallback_target = str(fallback) if fallback else None
    should_try_fallback = bool(fallback_target and fallback_target != primary_target)
    record_delivery_attempt(
        data,
        task_id,
        target=primary_target,
        status="failed",
        error=primary_error,
        fallback_target=fallback_target if should_try_fallback else None,
    )
    if not should_try_fallback:
        failure = {"success": False, "target": primary_target, "error": primary_error, "result": result, "plan": plan}
        if persist:
            return _persist_delivery_audit(data, failure)
        return failure

    assert fallback_target is not None
    fallback_result = _call_sender(send, fallback_target, clean_message)
    if _send_result_ok(fallback_result):
        record_delivery_attempt(
            data,
            task_id,
            target=fallback_target,
            status="success",
            message_id=_send_result_message_id(fallback_result),
        )
        success = {"success": True, "target": fallback_target, "result": fallback_result, "plan": plan, "fallback_from": primary_target}
        if persist:
            return _persist_delivery_audit(data, success)
        return success

    fallback_error = _send_result_error(fallback_result) or "fallback send failed"
    record_delivery_attempt(data, task_id, target=fallback_target, status="failed", error=fallback_error)
    failure = {
        "success": False,
        "target": primary_target,
        "error": primary_error,
        "fallback_target": fallback_target,
        "fallback_error": fallback_error,
        "result": result,
        "fallback_result": fallback_result,
        "plan": plan,
    }
    if persist:
        return _persist_delivery_audit(data, failure)
    return failure


def deliver_stored_task_result(task_id: str, message: str, *, sender: Any | None = None) -> dict[str, Any]:
    """Locked durable-store wrapper for delivering a supervisor task result."""
    with _task_store_lock():
        data = load_task_store()
        result = deliver_task_result(data, task_id, message, sender=sender, persist=False)
        try:
            save_task_store(data)
        except Exception as exc:
            result["audit_persisted"] = False
            result["audit_error"] = str(exc) or exc.__class__.__name__
            return result
        result["audit_persisted"] = True
        return result


def _find_merge_candidate(data: dict[str, Any], origin: dict[str, Any], text_fingerprint: str) -> dict[str, Any] | None:
    tasks = data.get("tasks", [])
    if not isinstance(tasks, list):
        return None
    for task in reversed(tasks):
        if not isinstance(task, dict):
            continue
        if task.get("state") not in OPEN_STATES:
            continue
        if task.get("fingerprint") != text_fingerprint:
            continue
        if _same_origin(task.get("origin") or {}, origin):
            return task
    return None


def _new_occurrence(event: Any, text: str) -> dict[str, Any]:
    return {
        "at": _now_iso(),
        "message_id": getattr(event, "message_id", None),
        "text": text,
    }


def upsert_intake_task(data: dict[str, Any], event: Any, text: str) -> dict[str, Any]:
    origin = origin_envelope_from_event(event)
    clean = _clean_text(text)
    fingerprint = _fingerprint_text(clean)
    occurrence = _new_occurrence(event, clean)
    existing = _find_merge_candidate(data, origin, fingerprint)
    if existing is not None:
        occurrences = existing.get("occurrences")
        if not isinstance(occurrences, list):
            occurrences = []
            existing["occurrences"] = occurrences
        try:
            previous_merge_count = int(existing.get("merge_count") or 1)
        except (TypeError, ValueError):
            previous_merge_count = max(1, len(occurrences))
        occurrences.append(occurrence)
        existing["merge_count"] = previous_merge_count + 1
        existing["last_seen_at"] = occurrence["at"]
        existing["updated_at"] = occurrence["at"]
        return existing

    title = clean[:96] or "Untitled supervisor task"
    now = _now_iso()
    task = {
        "task_id": _task_id(origin, clean),
        "created_at": now,
        "updated_at": now,
        "state": "inbox",
        "title": title,
        "objective": clean,
        "fingerprint": fingerprint,
        "origin": origin,
        "owner": "supervisor",
        "priority": {
            "lane": "unknown",
            "score": None,
            "rationale": "Awaiting supervisor triage.",
        },
        "human_interaction": {
            "mode": "natural_language",
            "commands_required": False,
            "one_active_ask": True,
        },
        "attention": {
            "active": False,
            "ask": None,
            "reply_style": "natural_language",
        },
        "occurrences": [occurrence],
        "merge_count": 1,
        "human_replies": [],
        "context_refs": {
            "gbrain": [],
            "files": [],
            "urls": [],
            "repos": [],
        },
        "acceptance_criteria": [],
    }
    tasks = data.get("tasks")
    if not isinstance(tasks, list):
        tasks = []
        data["tasks"] = tasks
    tasks.append(task)
    return task


def _task_summary(task: dict[str, Any]) -> str:
    return json.dumps(
        {
            "task_id": task.get("task_id"),
            "state": task.get("state"),
            "title": task.get("title"),
            "objective": task.get("objective"),
            "attention": task.get("attention"),
            "origin": task.get("origin"),
        },
        ensure_ascii=False,
        indent=2,
    )


def _new_task_rewrite(task: dict[str, Any], user_text: str) -> str:
    return (
        "[Supervisor layer context]\n"
        "You are operating as the thin supervisor for a multi-agent platform. "
        "Optimize Kevin's attention, not agent count. Keep at most one active human ask per origin, "
        "dedupe/merge related work, delegate specialist work when useful, and preserve routing.\n\n"
        "Origin envelope:\n"
        f"{json.dumps(task.get('origin') or {}, ensure_ascii=False, indent=2)}\n\n"
        "Supervisor task envelope:\n"
        f"{_task_summary(task)}\n\n"
        "Interaction rule: Do not require slash commands or rigid control words from Kevin. "
        "Interpret natural language replies like approvals, deferrals, corrections, and 'keep going' in context. "
        "If Kevin needs to do something, ask for exactly one atomic action with the recommended default, where to do it, why it matters, and how to reply naturally.\n\n"
        f"Kevin's message: {user_text}"
    )


def _active_task_rewrite(task: dict[str, Any], reply_text: str) -> str:
    return (
        "[Supervisor layer context]\n"
        "Kevin is replying to an active supervisor attention item. Treat this as a collaborative natural-language resolution loop, not as a command protocol.\n\n"
        "Active supervisor task:\n"
        f"{_task_summary(task)}\n\n"
        "Rules: infer whether Kevin approved, deferred, corrected, rejected, supplied missing data, or asked a follow-up. "
        "Keep the current task active unless the issue is genuinely resolved, delegated, deferred, merged, or dropped. "
        "If it is resolved, move to the next highest-leverage ask in normal prose; otherwise ask the narrowest possible follow-up.\n\n"
        f"Kevin's natural-language reply: {reply_text}"
    )


def capture_natural_reply(task: dict[str, Any], event: Any, text: str) -> None:
    now = _now_iso()
    task["state"] = "in_discussion"
    attention = task.setdefault("attention", {})
    attention["active"] = True
    attention["reply_style"] = "natural_language"
    human_replies = task.get("human_replies")
    if not isinstance(human_replies, list):
        human_replies = []
        task["human_replies"] = human_replies
    human_replies.append({
        "at": now,
        "message_id": getattr(event, "message_id", None),
        "text": text,
        "mode": "natural_language",
    })
    task["last_human_reply_at"] = now
    task["updated_at"] = now


def _digits(value: str) -> str:
    return PHONE_RE.sub("", value or "")


def _source_blob(event: Any) -> str:
    source = getattr(event, "source", None)
    parts = [
        getattr(source, "chat_id", ""),
        getattr(source, "chat_id_alt", ""),
        getattr(source, "user_id", ""),
        getattr(source, "user_name", ""),
        getattr(source, "chat_name", ""),
    ]
    return " ".join(str(p or "") for p in parts)


def _latest_briefing_queue_file() -> Path | None:
    state = _hermes_home() / "workspace" / "autonomy" / "state"
    files = sorted(state.glob("briefing-attention-queue-*.json"), key=lambda p: p.name)
    return files[-1] if files else None


def _briefing_queue_should_handle(event: Any) -> bool:
    """Yield to the existing BlueBubbles briefing queue loop when active.

    The briefing plugin is more specific and owns Kevin's daily text blockers.
    This guard keeps the generic supervisor from stealing those natural replies
    if plugin ordering changes.  Without an explicit alert recipient, fail open
    for supervisor handling instead of suppressing unrelated BlueBubbles traffic.
    """
    if _platform_value(event) != "bluebubbles":
        return False
    alert_to = os.environ.get("HERMES_TEXT_ALERT_TO", "")
    if not alert_to:
        return False
    alert_digits = _digits(alert_to)
    if not alert_digits:
        return False
    if alert_digits not in _digits(_source_blob(event)):
        return False
    qpath = _latest_briefing_queue_file()
    if not qpath or not qpath.exists():
        return False
    try:
        data = json.loads(qpath.read_text(encoding="utf-8"))
    except Exception:
        return False
    for item in data.get("items", []) or []:
        if isinstance(item, dict) and item.get("status") in BRIEFING_ACTIVE_STATUSES:
            return True
    return False


def _event_is_authorized_for_supervisor(event: Any, gateway: Any) -> bool:
    source = getattr(event, "source", None)
    if source is None:
        return False
    if getattr(source, "user_id", None) is None:
        return False
    if gateway is None:
        return False
    checker = getattr(gateway, "_is_user_authorized", None)
    if checker is None:
        return False
    try:
        return bool(checker(source))
    except Exception:
        return False


def _session_key_for_event(event: Any, gateway: Any) -> str | None:
    if gateway is None:
        return None
    source = getattr(event, "source", None)
    key_fn = getattr(gateway, "_session_key_for_source", None)
    if key_fn is None or source is None:
        return None
    try:
        return str(key_fn(source))
    except Exception:
        return None


def _gateway_control_state_pending(event: Any, gateway: Any) -> bool:
    """Let built-in gateway prompt/approval handlers consume replies first."""
    if gateway is None:
        return False
    session_key = _session_key_for_event(event, gateway)
    update_prompts = getattr(gateway, "_update_prompt_pending", {}) or {}
    if session_key and update_prompts.get(session_key):
        return True

    if session_key:
        try:
            from tools import slash_confirm as _slash_confirm_mod
            if _slash_confirm_mod.get_pending(session_key):
                return True
        except Exception:
            pass
        try:
            from tools.approval import has_blocking_approval
            if has_blocking_approval(session_key):
                return True
        except Exception:
            pass
        try:
            from tools import clarify_gateway as _clarify_mod
            if _clarify_mod.get_pending_for_session(session_key) is not None:
                return True
        except Exception:
            pass
    return False


def pre_gateway_dispatch(event: Any = None, gateway: Any = None, session_store: Any = None, **kwargs: Any) -> dict[str, Any] | None:
    if event is None:
        return None
    text = _clean_text(getattr(event, "text", "") or "")
    if not text:
        return None
    # This hook fires before core gateway auth. Do not persist/rewrite messages
    # that the gateway would drop or pair; otherwise arbitrary unauthenticated
    # senders could poison the supervisor store before normal auth runs.
    if not _event_is_authorized_for_supervisor(event, gateway):
        return None
    # Built-in gateway control replies (update prompts, slash confirms, tool
    # approvals) need the exact user text. Never turn those into supervisor tasks.
    if _gateway_control_state_pending(event, gateway):
        return None
    # Preserve existing explicit Hermes slash-command behavior. The supervisor
    # layer is for natural language, not for stealing core gateway commands.
    if COMMAND_RE.match(text):
        return None
    if _briefing_queue_should_handle(event):
        return None

    with _task_store_lock():
        data = load_task_store()
        origin = origin_envelope_from_event(event)
        active = _find_active_attention_task(data, origin)
        if active is not None:
            capture_natural_reply(active, event, text)
            save_task_store(data)
            return {"action": "rewrite", "text": _active_task_rewrite(active, text)}

        if _is_standalone_continuation(text) or _is_low_signal_standalone(text):
            return None

        task = upsert_intake_task(data, event, text)
        save_task_store(data)
        return {"action": "rewrite", "text": _new_task_rewrite(task, text)}


def register(ctx: Any) -> None:
    ctx.register_hook("pre_gateway_dispatch", pre_gateway_dispatch)
