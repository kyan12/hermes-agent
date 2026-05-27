"""Tests for the bundled supervisor layer plugin.

The first supervisor slice intentionally mirrors the daily briefing queue shape:
- preserve origin routing in state,
- keep a single active human-attention item per origin,
- interpret Kevin replies as natural language instead of slash commands.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace


def _load_plugin():
    plugin_path = Path(__file__).resolve().parents[2] / "plugins" / "supervisor-layer" / "__init__.py"
    spec = importlib.util.spec_from_file_location("supervisor_layer_plugin", plugin_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _event(
    *,
    text: str = "Can you get Code Crab to inspect the failing deploy?",
    platform: str = "discord",
    chat_id: str = "channel-123",
    thread_id: str | None = "thread-456",
    message_id: str = "msg-789",
):
    source = SimpleNamespace(
        platform=SimpleNamespace(value=platform),
        chat_id=chat_id,
        chat_name="business-general",
        chat_type="thread" if thread_id else "channel",
        user_id="kevin-1",
        user_name="Kevin Yan",
        thread_id=thread_id,
        chat_topic="multi-agent platform rethink",
        user_id_alt=None,
        chat_id_alt=None,
        guild_id="guild-1",
        parent_chat_id="parent-999" if thread_id else None,
        message_id=message_id,
        is_bot=False,
    )
    return SimpleNamespace(
        text=text,
        source=source,
        message_id=message_id,
        raw_message={"id": message_id},
        reply_to_message_id=None,
        reply_to_text=None,
    )


class _Gateway:
    def __init__(self, authorized: bool, update_pending: bool = False):
        self.authorized = authorized
        self._update_prompt_pending = {"session-key": True} if update_pending else {}

    def _is_user_authorized(self, source):
        return self.authorized

    def _session_key_for_source(self, source):
        return "session-key"


def _read_store(home: Path) -> dict:
    return json.loads((home / "workspace" / "supervisor" / "state" / "supervisor-tasks.json").read_text())


def test_origin_envelope_preserves_platform_thread_and_message(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()

    origin = plugin.origin_envelope_from_event(_event())

    assert origin == {
        "platform": "discord",
        "chat_id": "channel-123",
        "thread_id": "thread-456",
        "message_id": "msg-789",
        "user_id": "kevin-1",
        "user_name": "Kevin Yan",
        "chat_name": "business-general",
        "chat_type": "thread",
        "guild_id": "guild-1",
        "parent_chat_id": "parent-999",
        "visibility": "team",
        "fallback_route": "origin",
    }


def test_standalone_continuation_reply_passes_through_without_new_task(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()

    for text in ("continue", "Okay!", "YES"):
        event = _event(text=text)
        result = plugin.pre_gateway_dispatch(event=event, gateway=_Gateway(True), session_store=None)
        assert result is None

    assert not (tmp_path / "workspace" / "supervisor" / "state" / "supervisor-tasks.json").exists()


def test_bare_punctuation_passes_through_without_new_task(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()

    for text in ("?", "??", "...", "!", "👍"):
        event = _event(text=text)
        result = plugin.pre_gateway_dispatch(event=event, gateway=_Gateway(True), session_store=None)
        assert result is None

    assert not (tmp_path / "workspace" / "supervisor" / "state" / "supervisor-tasks.json").exists()


def test_standalone_continuation_reply_to_active_attention_item_is_captured(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    first = _event(text="I need to approve the DIDA collection order")
    plugin.pre_gateway_dispatch(event=first, gateway=_Gateway(True), session_store=None)

    data = _read_store(tmp_path)
    task = data["tasks"][0]
    task["state"] = "needs_human"
    task["attention"] = {"active": True, "ask": "Keep going?", "reply_style": "natural_language"}
    plugin.save_task_store(data)

    for reply in ("continue", "?"):
        result = plugin.pre_gateway_dispatch(event=_event(text=reply), gateway=_Gateway(True), session_store=None)
        assert result and result["action"] == "rewrite"

    updated = _read_store(tmp_path)
    assert [reply["text"] for reply in updated["tasks"][0]["human_replies"][-2:]] == ["continue", "?"]


def test_new_message_creates_origin_routed_task_and_rewrites_to_supervisor_context(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    event = _event(text="Can you get Code Crab to inspect the failing deploy?")

    result = plugin.pre_gateway_dispatch(event=event, gateway=_Gateway(True), session_store=None)

    assert result["action"] == "rewrite"
    rewritten = result["text"]
    assert "[Supervisor layer context]" in rewritten
    assert "Origin envelope" in rewritten
    assert "Do not require slash commands" in rewritten
    assert "Can you get Code Crab to inspect the failing deploy?" in rewritten

    data = _read_store(tmp_path)
    assert data["schema_version"] == 1
    assert len(data["tasks"]) == 1
    task = data["tasks"][0]
    assert task["state"] == "inbox"
    assert task["origin"]["platform"] == "discord"
    assert task["origin"]["thread_id"] == "thread-456"
    assert task["origin"]["fallback_route"] == "origin"
    assert task["human_interaction"]["mode"] == "natural_language"
    assert task["human_interaction"]["commands_required"] is False


def test_natural_reply_to_active_attention_item_is_captured_without_command(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    first = _event(text="I need to approve the DIDA collection order")
    plugin.pre_gateway_dispatch(event=first, gateway=_Gateway(True), session_store=None)

    data = _read_store(tmp_path)
    task = data["tasks"][0]
    task["state"] = "needs_human"
    task["attention"] = {
        "active": True,
        "ask": "Approve the drafted DIDA collection order?",
        "reply_style": "natural_language",
    }
    plugin.save_task_store(data)

    reply = _event(text="yeah approve the safer default and keep going")
    result = plugin.pre_gateway_dispatch(event=reply, gateway=_Gateway(True), session_store=None)

    assert result["action"] == "rewrite"
    rewritten = result["text"]
    assert "Active supervisor task" in rewritten
    assert "Kevin's natural-language reply" in rewritten
    assert "yeah approve the safer default and keep going" in rewritten
    assert "/done" not in rewritten
    assert "/next" not in rewritten

    updated = _read_store(tmp_path)
    updated_task = updated["tasks"][0]
    assert updated_task["state"] == "in_discussion"
    assert updated_task["attention"]["active"] is True
    assert updated_task["human_replies"][-1]["text"] == "yeah approve the safer default and keep going"
    assert updated_task["human_replies"][-1]["mode"] == "natural_language"


def test_same_origin_duplicate_intake_merges_instead_of_spawning_parallel_asks(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    event = _event(text="Please check whether the WebAir bootstrap is blocked")

    plugin.pre_gateway_dispatch(event=event, gateway=_Gateway(True), session_store=None)
    plugin.pre_gateway_dispatch(event=event, gateway=_Gateway(True), session_store=None)

    data = _read_store(tmp_path)
    assert len(data["tasks"]) == 1
    task = data["tasks"][0]
    assert task["merge_count"] == 2
    assert len(task["occurrences"]) == 2
    assert task["state"] == "inbox"


def test_repeated_same_text_after_completion_gets_unique_task_id(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    data = {"schema_version": 1, "tasks": []}
    real_datetime = plugin.dt.datetime

    class _FixedDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime(2026, 1, 1, 12, 0, 0, tzinfo=tz)

    monkeypatch.setattr(plugin.dt, "datetime", _FixedDateTime)
    uuids = iter([
        SimpleNamespace(hex="a" * 32),
        SimpleNamespace(hex="b" * 32),
    ])
    monkeypatch.setattr(plugin.uuid, "uuid4", lambda: next(uuids))

    first = plugin.upsert_intake_task(data, _event(text="Approve invoice draft", message_id="msg-1"), "Approve invoice draft")
    plugin.complete_task(data, first["task_id"], result="done")
    second = plugin.upsert_intake_task(data, _event(text="Approve invoice draft", message_id="msg-2"), "Approve invoice draft")

    assert first["task_id"] != second["task_id"]
    assert len({task["task_id"] for task in data["tasks"]}) == 2


def test_distinct_unicode_messages_do_not_merge_via_empty_ascii_fingerprint(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()

    plugin.pre_gateway_dispatch(event=_event(text="审批发票"), gateway=_Gateway(True), session_store=None)
    plugin.pre_gateway_dispatch(event=_event(text="安排会议"), gateway=_Gateway(True), session_store=None)

    data = _read_store(tmp_path)
    assert [task["objective"] for task in data["tasks"]] == ["审批发票", "安排会议"]


def test_attention_requests_keep_one_active_ask_per_origin(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    data = {"schema_version": 1, "tasks": []}
    first = plugin.upsert_intake_task(data, _event(text="Approve invoice draft", message_id="msg-1"), "Approve invoice draft")
    second = plugin.upsert_intake_task(data, _event(text="Pick launch date", message_id="msg-2"), "Pick launch date")

    first_status = plugin.request_human_attention(
        data,
        first["task_id"],
        ask="Approve the invoice draft?",
        recommended_default="approve",
        why_now="unblocks sending",
        where="this thread",
    )
    second_status = plugin.request_human_attention(
        data,
        second["task_id"],
        ask="Pick the launch date",
        recommended_default="Friday",
    )

    assert first_status == "activated"
    assert second_status == "queued"
    assert first["state"] == "needs_human"
    assert first["attention"]["active"] is True
    assert first["attention"]["recommended_default"] == "approve"
    assert first["attention"]["why_now"] == "unblocks sending"
    assert first["attention"]["where"] == "this thread"
    assert second["state"] == "needs_human"
    assert second["attention"]["active"] is False
    assert second["attention"]["queued"] is True


def test_completing_active_attention_promotes_next_queued_same_origin(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    data = {"schema_version": 1, "tasks": []}
    first = plugin.upsert_intake_task(data, _event(text="Approve invoice draft", message_id="msg-1"), "Approve invoice draft")
    second = plugin.upsert_intake_task(data, _event(text="Pick launch date", message_id="msg-2"), "Pick launch date")
    plugin.request_human_attention(data, first["task_id"], ask="Approve the invoice draft?")
    plugin.request_human_attention(data, second["task_id"], ask="Pick the launch date")

    promoted = plugin.complete_task(data, first["task_id"], result="approved")

    assert first["state"] == "done"
    assert first["attention"]["active"] is False
    assert first["result"] == "approved"
    assert promoted is second
    assert second["state"] == "needs_human"
    assert second["attention"]["active"] is True
    assert second["attention"]["queued"] is False


def test_worker_callback_can_queue_human_attention_without_interrupting_active_ask(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    data = {"schema_version": 1, "tasks": []}
    active = plugin.upsert_intake_task(data, _event(text="Approve invoice draft", message_id="msg-1"), "Approve invoice draft")
    worker_task = plugin.upsert_intake_task(data, _event(text="Review contract", message_id="msg-2"), "Review contract")
    plugin.request_human_attention(data, active["task_id"], ask="Approve the invoice draft?")

    status = plugin.append_worker_callback(
        data,
        worker_task["task_id"],
        worker="legal_ops",
        status="blocked",
        summary="Need Kevin to choose fallback language.",
        needs_human=True,
        ask="Use safer fallback language?",
        recommended_default="use safer fallback",
    )

    assert status == "queued"
    assert worker_task["callbacks"][-1]["worker"] == "legal_ops"
    assert worker_task["callbacks"][-1]["status"] == "blocked"
    assert worker_task["attention"]["active"] is False
    assert worker_task["attention"]["queued"] is True
    assert worker_task["attention"]["recommended_default"] == "use safer fallback"


def test_render_attention_ask_is_atomic_and_natural_language(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    data = {"schema_version": 1, "tasks": []}
    task = plugin.upsert_intake_task(data, _event(text="Approve invoice draft"), "Approve invoice draft")
    plugin.request_human_attention(
        data,
        task["task_id"],
        ask="Approve the invoice draft?",
        recommended_default="approve",
        why_now="unblocks sending",
        where="this thread",
    )

    rendered = plugin.render_attention_ask(task)

    assert "Needs Kevin" in rendered
    assert "Do exactly this: Approve the invoice draft?" in rendered
    assert "Recommended default: approve" in rendered
    assert "Where: this thread" in rendered
    assert "Why now: unblocks sending" in rendered
    assert "Reply naturally" in rendered


def test_delivery_plan_preserves_discord_thread_route(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    data = {"schema_version": 1, "tasks": []}
    task = plugin.upsert_intake_task(data, _event(text="Report worker result"), "Report worker result")

    plan = plugin.delivery_plan_for_task(task)

    assert plan["task_id"] == task["task_id"]
    assert plan["primary_target"] == "discord:parent-999:thread-456"
    assert plan["fallback_target"] == "discord:parent-999:thread-456"
    assert plan["visibility"] == "team"


def test_delivery_target_falls_back_to_thread_id_without_parent(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    origin = plugin.origin_envelope_from_event(_event(thread_id="thread-456"))
    origin.pop("parent_chat_id")

    assert plugin.delivery_target_for_origin(origin) == "discord:thread-456"


def test_delivery_target_builds_non_discord_threaded_route(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()

    assert plugin.delivery_target_for_origin({
        "platform": "telegram",
        "chat_id": "chat-1",
        "thread_id": "topic-2",
    }) == "telegram:chat-1:topic-2"


def test_delivery_plan_returns_origin_copy(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    data = {"schema_version": 1, "tasks": []}
    task = plugin.upsert_intake_task(data, _event(text="Report worker result"), "Report worker result")

    plan = plugin.delivery_plan_for_task(task)
    plan["origin"]["chat_id"] = "mutated"

    assert task["origin"]["chat_id"] == "channel-123"


def test_record_delivery_attempt_rejects_fallback_without_target(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    data = {"schema_version": 1, "tasks": []}
    task = plugin.upsert_intake_task(data, _event(text="Send completed worker summary"), "Send completed worker summary")

    try:
        plugin.record_delivery_attempt(data, task["task_id"], target="discord", status="fallback_queued")
    except ValueError as exc:
        assert "fallback_target" in str(exc)
    else:
        raise AssertionError("fallback_queued without fallback_target should fail closed")


def test_record_delivery_attempt_tracks_success_and_fallback(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    data = {"schema_version": 1, "tasks": []}
    task = plugin.upsert_intake_task(data, _event(text="Send completed worker summary"), "Send completed worker summary")

    failure = plugin.record_delivery_attempt(
        data,
        task["task_id"],
        target="discord:parent-999:thread-456",
        status="failed",
        error="Unknown Channel",
        fallback_target="discord",
    )
    success = plugin.record_delivery_attempt(
        data,
        task["task_id"],
        target="discord",
        status="success",
        message_id="fallback-msg-1",
    )

    assert failure["status"] == "failed"
    assert failure["fallback_target"] == "discord"
    assert success["message_id"] == "fallback-msg-1"
    assert task["delivery_status"] == "success"
    assert "pending_fallback_target" not in task
    assert task["delivered_at"]
    assert len(task["deliveries"]) == 2


def test_deliver_task_result_sends_to_primary_and_records_success(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    data = {"schema_version": 1, "tasks": []}
    task = plugin.upsert_intake_task(data, _event(text="Send completed worker summary"), "Send completed worker summary")
    calls = []

    def sender(target, message):
        calls.append((target, message))
        return {"success": True, "message_id": "msg-primary"}

    result = plugin.deliver_task_result(data, task["task_id"], "Worker result summary", sender=sender)

    assert result["success"] is True
    assert result["target"] == "discord:parent-999:thread-456"
    assert calls == [("discord:parent-999:thread-456", "Worker result summary")]
    assert task["delivery_status"] == "success"
    assert task["deliveries"][-1]["message_id"] == "msg-primary"


def test_deliver_task_result_falls_back_and_records_both_attempts(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    data = {"schema_version": 1, "tasks": []}
    task = plugin.upsert_intake_task(data, _event(text="Send completed worker summary"), "Send completed worker summary")
    task["origin"]["fallback_route"] = "discord"
    calls = []

    def sender(target, message):
        calls.append((target, message))
        if target == "discord:parent-999:thread-456":
            return {"error": "Unknown Channel"}
        return {"success": True, "message_id": "msg-fallback"}

    result = plugin.deliver_task_result(data, task["task_id"], "Worker result summary", sender=sender)

    assert result["success"] is True
    assert result["target"] == "discord"
    assert calls == [
        ("discord:parent-999:thread-456", "Worker result summary"),
        ("discord", "Worker result summary"),
    ]
    assert [attempt["status"] for attempt in task["deliveries"]] == ["failed", "success"]
    assert task["deliveries"][0]["fallback_target"] == "discord"
    assert task["deliveries"][0]["error"] == "Unknown Channel"
    assert task["deliveries"][1]["message_id"] == "msg-fallback"


def test_deliver_task_result_falls_back_after_primary_sender_exception(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    data = {"schema_version": 1, "tasks": []}
    task = plugin.upsert_intake_task(data, _event(text="Send completed worker summary"), "Send completed worker summary")
    task["origin"]["fallback_route"] = "discord"
    calls = []

    def sender(target, message):
        calls.append(target)
        if target == "discord:parent-999:thread-456":
            raise RuntimeError("primary route exploded")
        return {"success": True, "message_id": "msg-fallback"}

    result = plugin.deliver_task_result(data, task["task_id"], "Worker result summary", sender=sender)

    assert result["success"] is True
    assert result["target"] == "discord"
    assert calls == ["discord:parent-999:thread-456", "discord"]
    assert task["deliveries"][0]["status"] == "failed"
    assert "primary route exploded" in task["deliveries"][0]["error"]
    assert task["deliveries"][1]["status"] == "success"


def test_deliver_stored_task_result_locks_loads_sends_and_saves(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    data = {"schema_version": 1, "tasks": []}
    task = plugin.upsert_intake_task(data, _event(text="Send completed worker summary"), "Send completed worker summary")
    plugin.save_task_store(data)

    result = plugin.deliver_stored_task_result(
        task["task_id"],
        "Worker result summary",
        sender=lambda target, message: {"success": True, "message_id": "msg-primary"},
    )

    updated = _read_store(tmp_path)
    assert result["success"] is True
    assert updated["tasks"][0]["delivery_status"] == "success"
    assert updated["tasks"][0]["deliveries"][-1]["message_id"] == "msg-primary"


def test_deliver_task_result_does_not_persist_by_default(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    data = {"schema_version": 1, "tasks": []}
    task = plugin.upsert_intake_task(data, _event(text="Send completed worker summary"), "Send completed worker summary")
    plugin.save_task_store(data)

    plugin.deliver_task_result(
        data,
        task["task_id"],
        "Worker result summary",
        sender=lambda target, message: {"success": True, "message_id": "not-persisted"},
    )

    stored = _read_store(tmp_path)
    assert "deliveries" not in stored["tasks"][0]


def test_deliver_task_result_persist_failure_after_send_returns_without_raising(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    data = {"schema_version": 1, "tasks": []}
    task = plugin.upsert_intake_task(data, _event(text="Send completed worker summary"), "Send completed worker summary")

    def fail_save(_data):
        raise OSError("disk full")

    monkeypatch.setattr(plugin, "save_task_store", fail_save)

    result = plugin.deliver_task_result(
        data,
        task["task_id"],
        "Worker result summary",
        sender=lambda target, message: {"success": True, "message_id": "msg-primary"},
        persist=True,
    )

    assert result["success"] is True
    assert result["audit_persisted"] is False
    assert "disk full" in result["audit_error"]
    assert task["delivery_status"] == "success"


def test_deliver_task_result_json_false_sender_response_is_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    data = {"schema_version": 1, "tasks": []}
    task = plugin.upsert_intake_task(data, _event(text="Send completed worker summary"), "Send completed worker summary")

    result = plugin.deliver_task_result(
        data,
        task["task_id"],
        "Worker result summary",
        sender=lambda target, message: "false",
    )

    assert result["success"] is False
    assert task["deliveries"][-1]["status"] == "failed"


def test_record_delivery_attempt_rejects_unknown_status(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    data = {"schema_version": 1, "tasks": []}
    task = plugin.upsert_intake_task(data, _event(text="Send completed worker summary"), "Send completed worker summary")

    try:
        plugin.record_delivery_attempt(data, task["task_id"], target="discord", status="weird")
    except ValueError as exc:
        assert "delivery status" in str(exc)
    else:
        raise AssertionError("unknown delivery status should fail closed")


def test_re_requesting_attention_clears_stale_optional_guidance(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    data = {"schema_version": 1, "tasks": []}
    task = plugin.upsert_intake_task(data, _event(text="Approve invoice draft"), "Approve invoice draft")
    plugin.request_human_attention(
        data,
        task["task_id"],
        ask="Approve the invoice draft?",
        recommended_default="approve",
        why_now="unblocks sending",
        where="this thread",
    )

    plugin.request_human_attention(data, task["task_id"], ask="Pick a launch date")

    attention = task["attention"]
    assert attention["ask"] == "Pick a launch date"
    assert "recommended_default" not in attention
    assert "why_now" not in attention
    assert "where" not in attention


def test_completion_promotes_only_explicitly_queued_attention(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    data = {"schema_version": 1, "tasks": []}
    first = plugin.upsert_intake_task(data, _event(text="Approve invoice draft", message_id="msg-1"), "Approve invoice draft")
    inactive = plugin.upsert_intake_task(data, _event(text="Inactive draft", message_id="msg-2"), "Inactive draft")
    queued = plugin.upsert_intake_task(data, _event(text="Pick launch date", message_id="msg-3"), "Pick launch date")
    plugin.request_human_attention(data, first["task_id"], ask="Approve the invoice draft?")
    inactive["state"] = "needs_human"
    inactive["attention"] = {"active": False, "queued": False, "ask": "Should not auto-promote", "reply_style": "natural_language"}
    plugin.request_human_attention(data, queued["task_id"], ask="Pick launch date")

    promoted = plugin.complete_task(data, first["task_id"], result="approved")

    assert promoted is queued
    assert inactive["attention"]["active"] is False
    assert queued["attention"]["active"] is True


def test_pre_auth_hook_skips_missing_or_unauthorized_users(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()

    missing_user = _event(text="unauth should not persist")
    missing_user.source.user_id = None
    assert plugin.pre_gateway_dispatch(event=missing_user, gateway=_Gateway(True), session_store=None) is None

    unauthorized = _event(text="unauth should not rewrite either")
    assert plugin.pre_gateway_dispatch(event=unauthorized, gateway=_Gateway(False), session_store=None) is None

    no_gateway = _event(text="pre-auth fallback should fail closed")
    assert plugin.pre_gateway_dispatch(event=no_gateway, gateway=None, session_store=None) is None

    assert not (tmp_path / "workspace" / "supervisor" / "state" / "supervisor-tasks.json").exists()


def test_gateway_control_state_takes_precedence_over_supervisor_rewrite(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()

    event = _event(text="approve")
    result = plugin.pre_gateway_dispatch(event=event, gateway=_Gateway(True, update_pending=True), session_store=None)

    assert result is None
    assert not (tmp_path / "workspace" / "supervisor" / "state" / "supervisor-tasks.json").exists()


def test_pending_clarify_text_reply_takes_precedence_over_supervisor_rewrite(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    from tools import clarify_gateway

    clarify_gateway.register("clarify-1", "session-key", "Which option?", choices=None)
    try:
        event = _event(text="use the safer default")
        result = plugin.pre_gateway_dispatch(event=event, gateway=_Gateway(True), session_store=None)
    finally:
        clarify_gateway.clear_session("session-key")

    assert result is None
    assert not (tmp_path / "workspace" / "supervisor" / "state" / "supervisor-tasks.json").exists()


def test_bluebubbles_briefing_guard_only_yields_for_configured_alert_recipient(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_TEXT_ALERT_TO", raising=False)
    plugin = _load_plugin()

    queue_path = tmp_path / "workspace" / "autonomy" / "state" / "briefing-attention-queue-2026-05-26.json"
    queue_path.parent.mkdir(parents=True)
    queue_path.write_text(json.dumps({"items": [{"status": "sent"}]}))

    event = _event(text="this BlueBubbles task is not a briefing reply", platform="bluebubbles", thread_id=None)
    event.source.user_id = "+15551234567"
    event.source.chat_id = "+15551234567"

    result = plugin.pre_gateway_dispatch(event=event, gateway=_Gateway(True), session_store=None)
    assert result and result["action"] == "rewrite"

    monkeypatch.setenv("HERMES_TEXT_ALERT_TO", "+15551234567")
    assert plugin._briefing_queue_should_handle(event) is True


def test_bluebubbles_briefing_guard_ignores_nonnumeric_alert_recipient(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_TEXT_ALERT_TO", "kevin")
    plugin = _load_plugin()

    queue_path = tmp_path / "workspace" / "autonomy" / "state" / "briefing-attention-queue-2026-05-26.json"
    queue_path.parent.mkdir(parents=True)
    queue_path.write_text(json.dumps({"items": [{"status": "sent"}]}))
    event = _event(text="normal BlueBubbles task", platform="bluebubbles", thread_id=None)

    assert plugin._briefing_queue_should_handle(event) is False


def test_load_task_store_backs_up_unreadable_or_malformed_store(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    store_path = tmp_path / "workspace" / "supervisor" / "state" / "supervisor-tasks.json"
    store_path.parent.mkdir(parents=True)
    store_path.write_text("not-json")

    data = plugin.load_task_store()

    assert data["tasks"] == []
    assert "state_recovery_note" in data
    backups = list(store_path.parent.glob("supervisor-tasks.json.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_text() == "not-json"

    store_path.write_text(json.dumps({"tasks": "not-a-list"}))
    data = plugin.load_task_store()

    assert data["tasks"] == []
    backups = list(store_path.parent.glob("supervisor-tasks.json.corrupt-*"))
    assert len(backups) == 2


def test_duplicate_merge_tolerates_malformed_existing_counters(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    event = _event(text="Please check whether the WebAir bootstrap is blocked")
    data = {"schema_version": 1, "tasks": []}
    existing = plugin.upsert_intake_task(data, event, event.text)
    existing["occurrences"] = "bad"
    existing["merge_count"] = "bad"

    plugin.upsert_intake_task(data, event, event.text)

    assert isinstance(existing["occurrences"], list)
    assert len(existing["occurrences"]) == 1
    assert existing["merge_count"] == 2


def test_save_task_store_uses_atomic_writer(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    calls = []

    def fake_atomic_json_write(path, data, indent=2):
        calls.append((path, dict(data), indent))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=indent) + "\n")

    monkeypatch.setattr(plugin, "_atomic_json_write", fake_atomic_json_write)

    plugin.save_task_store({"schema_version": 1, "tasks": []})

    assert calls
    assert calls[0][0] == tmp_path / "workspace" / "supervisor" / "state" / "supervisor-tasks.json"
    assert calls[0][2] == 2
    data = _read_store(tmp_path)
    assert data["schema_version"] == 1
    assert data["tasks"] == []
    assert "updated_at" in data
