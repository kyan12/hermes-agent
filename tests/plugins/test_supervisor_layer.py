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
    chat_name: str = "business-general",
    thread_id: str | None = "thread-456",
    message_id: str = "msg-789",
):
    source = SimpleNamespace(
        platform=SimpleNamespace(value=platform),
        chat_id=chat_id,
        chat_name=chat_name,
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
    assert data["schema_version"] == plugin.TASK_STORE_SCHEMA_VERSION
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


def test_attention_requests_keep_one_active_ask_globally_across_origins(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    data = {"schema_version": 1, "tasks": []}
    first = plugin.upsert_intake_task(data, _event(text="Approve invoice draft", chat_id="origin-a", message_id="msg-1"), "Approve invoice draft")
    second = plugin.upsert_intake_task(data, _event(text="Pick launch date", chat_id="origin-b", message_id="msg-2"), "Pick launch date")

    first_status = plugin.request_human_attention(data, first["task_id"], ask="Approve invoice?")
    second_status = plugin.request_human_attention(data, second["task_id"], ask="Pick launch date?")

    assert first_status == "activated"
    assert second_status == "queued"
    assert first["attention"]["active"] is True
    assert second["attention"]["active"] is False
    assert second["attention"]["queued"] is True
    assert len(plugin._find_global_active_attention_tasks(data)) == 1
    assert plugin.supervisor_dashboard_snapshot(data)["multiple_active_attention"] is False


def test_attention_request_queues_when_store_already_has_multiple_active_asks(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    data = {"schema_version": 1, "tasks": []}
    first = plugin.upsert_intake_task(data, _event(text="Approve invoice", chat_id="origin-a"), "Approve invoice")
    second = plugin.upsert_intake_task(data, _event(text="Pick launch", chat_id="origin-b"), "Pick launch")
    third = plugin.upsert_intake_task(data, _event(text="Choose copy", chat_id="origin-c"), "Choose copy")
    plugin.request_human_attention(data, first["task_id"], ask="Approve invoice?")
    plugin.request_human_attention(data, second["task_id"], ask="Pick launch?")
    second["attention"]["active"] = True
    second["attention"]["queued"] = False

    status = plugin.request_human_attention(data, third["task_id"], ask="Choose copy?")

    assert status == "queued"
    assert third["attention"]["active"] is False
    assert third["attention"]["queued"] is True
    assert len(plugin._find_global_active_attention_tasks(data)) == 2


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


def test_completing_active_attention_promotes_next_queued_globally(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    data = {"schema_version": 1, "tasks": []}
    first = plugin.upsert_intake_task(data, _event(text="Approve invoice draft", chat_id="origin-a", message_id="msg-1"), "Approve invoice draft")
    second = plugin.upsert_intake_task(data, _event(text="Pick launch date", chat_id="origin-b", message_id="msg-2"), "Pick launch date")
    plugin.request_human_attention(data, first["task_id"], ask="Approve invoice?")
    plugin.request_human_attention(data, second["task_id"], ask="Pick launch date?")

    promoted = plugin.complete_task(data, first["task_id"], result="approved")

    assert promoted is second
    assert first["state"] == "done"
    assert first["attention"]["active"] is False
    assert second["attention"]["active"] is True
    assert second["attention"]["queued"] is False
    assert len(plugin._find_global_active_attention_tasks(data)) == 1


def test_promotes_oldest_queued_attention_by_queued_at_not_task_order(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    data = {"schema_version": 1, "tasks": []}
    active = plugin.upsert_intake_task(data, _event(text="Approve invoice", chat_id="origin-a"), "Approve invoice")
    newer = plugin.upsert_intake_task(data, _event(text="Newer queued", chat_id="origin-b"), "Newer queued")
    older = plugin.upsert_intake_task(data, _event(text="Older queued", chat_id="origin-c"), "Older queued")
    plugin.request_human_attention(data, active["task_id"], ask="Approve invoice?")
    plugin.request_human_attention(data, newer["task_id"], ask="Newer queued?")
    plugin.request_human_attention(data, older["task_id"], ask="Older queued?")
    newer["attention"]["queued_at"] = "2026-05-28T12:00:00+00:00"
    older["attention"]["queued_at"] = "2026-05-28T11:00:00+00:00"

    promoted = plugin.complete_task(data, active["task_id"], result="approved")

    assert promoted is older
    assert older["attention"]["active"] is True
    assert newer["attention"]["queued"] is True


def test_promotes_oldest_queued_attention_by_absolute_time_across_offsets(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    data = {"schema_version": 1, "tasks": []}
    active = plugin.upsert_intake_task(data, _event(text="Approve invoice", chat_id="origin-a"), "Approve invoice")
    older = plugin.upsert_intake_task(data, _event(text="Older absolute time", chat_id="origin-b"), "Older absolute time")
    newer = plugin.upsert_intake_task(data, _event(text="Newer absolute time", chat_id="origin-c"), "Newer absolute time")
    plugin.request_human_attention(data, active["task_id"], ask="Approve invoice?")
    plugin.request_human_attention(data, older["task_id"], ask="Older absolute?")
    plugin.request_human_attention(data, newer["task_id"], ask="Newer absolute?")
    older["attention"]["queued_at"] = "2026-11-01T01:30:00-04:00"  # 05:30Z
    newer["attention"]["queued_at"] = "2026-11-01T01:15:00-05:00"  # 06:15Z

    promoted = plugin.complete_task(data, active["task_id"], result="approved")

    assert promoted is older
    assert older["attention"]["active"] is True
    assert newer["attention"]["queued"] is True


def test_apply_attention_control_approve_completes_and_promotes(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    data = {"schema_version": 1, "tasks": []}
    first = plugin.upsert_intake_task(data, _event(text="Approve invoice draft", chat_id="origin-a", message_id="msg-1"), "Approve invoice draft")
    second = plugin.upsert_intake_task(data, _event(text="Pick launch date", chat_id="origin-b", message_id="msg-2"), "Pick launch date")
    plugin.request_human_attention(data, first["task_id"], ask="Approve invoice?")
    plugin.request_human_attention(data, second["task_id"], ask="Pick launch date?")

    result = plugin.apply_attention_control(data, "approve and keep going")

    assert result["status"] == "applied"
    assert result["decision"] == "approved"
    assert result["task_id"] == first["task_id"]
    assert result["promoted_task_id"] == second["task_id"]
    assert first["state"] == "done"
    assert first["result"] == "Kevin approved: approve and keep going"
    assert second["attention"]["active"] is True


def test_attention_control_classifier_prefers_proceed_over_no_problem(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()

    assert plugin.classify_attention_control("no problem, proceed") == "approved"


def test_attention_control_classifier_treats_bare_no_as_natural_language(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()

    assert plugin.classify_attention_control("no") is None
    assert plugin.classify_attention_control("nope") is None


def test_attention_control_classifier_rejects_mixed_or_negated_replies(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()

    for text in ("don't drop it, proceed", "please don't cancel, approve it", "don't stop", "ok, but change the date first", "wait, proceed"):
        assert plugin.classify_attention_control(text) is None


def test_controller_rejects_unknown_actions_without_creating_store(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()

    result = plugin.supervisor_control(
        "discord:1508918955744559246:1509636803970207794",
        payload={"task_id": "sup_unsafe", "text": "approve"},
        actor={"chat_id": "channel-123", "thread_id": "thread-456", "user_id": "kevin-1"},
    )

    serialized = json.dumps(result, ensure_ascii=False)
    assert result["status"] == "unknown_action"
    assert result["action"] == "unknown"
    assert "1508918955744559246" not in serialized
    assert "channel-123" not in serialized
    assert not (tmp_path / "workspace" / "supervisor" / "state" / "supervisor-tasks.json").exists()


def test_load_task_store_migrates_legacy_store_to_current_controller_schema(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    store_path = tmp_path / "workspace" / "supervisor" / "state" / "supervisor-tasks.json"
    store_path.parent.mkdir(parents=True)
    store_path.write_text(json.dumps({"schema_version": 1, "tasks": [{"task_id": "sup_legacy", "state": "inbox"}]}), encoding="utf-8")

    data = plugin.load_task_store()

    assert data["schema_version"] == plugin.TASK_STORE_SCHEMA_VERSION
    assert data["control_plane"]["schema_version"] == 1
    assert data["control_plane"]["last_event_seq"] == 0
    assert data["tasks"][0]["revision"] == 0
    assert data["tasks"][0]["controller_events"] == []


def test_migration_recovers_controller_event_sequence_from_existing_events(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    store_path = tmp_path / "workspace" / "supervisor" / "state" / "supervisor-tasks.json"
    store_path.parent.mkdir(parents=True)
    store_path.write_text(json.dumps({"schema_version": 1, "controller_events": [{"seq": 7}], "tasks": []}), encoding="utf-8")

    data = plugin.load_task_store()

    assert data["control_plane"]["last_event_seq"] == 7


def test_controller_dashboard_and_not_found_paths_do_not_create_store(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    store_path = tmp_path / "workspace" / "supervisor" / "state" / "supervisor-tasks.json"

    dashboard = plugin.supervisor_control("dashboard")
    not_found = plugin.supervisor_control("complete_task", payload={"task_id": "sup_missing"})

    assert dashboard["status"] == "ok"
    assert not_found["status"] == "not_found"
    assert not store_path.exists()


def test_controller_apply_attention_control_persists_promotes_and_audits_safely(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    data = {"schema_version": plugin.TASK_STORE_SCHEMA_VERSION, "tasks": []}
    first = plugin.upsert_intake_task(data, _event(text="Approve invoice", chat_id="origin-a"), "Approve invoice")
    second = plugin.upsert_intake_task(data, _event(text="Pick launch", chat_id="origin-b"), "Pick launch")
    plugin.request_human_attention(data, first["task_id"], ask="Approve invoice?")
    plugin.request_human_attention(data, second["task_id"], ask="Pick launch?")
    plugin.save_task_store(data)

    result = plugin.supervisor_control(
        "apply_attention_control",
        payload={"text": "approve and keep going"},
        actor={"platform": "discord", "chat_id": "channel-123", "thread_id": "thread-456", "user_id": "kevin-1"},
    )

    serialized = json.dumps(result, ensure_ascii=False)
    assert result["status"] == "applied"
    assert result["event_seq"] == 1
    assert "dashboard" in result
    assert "channel-123" not in serialized
    assert "thread-456" not in serialized
    stored = _read_store(tmp_path)
    assert stored["control_plane"]["last_event_seq"] == 1
    assert stored["controller_events"][-1]["action"] == "apply_attention_control"
    assert stored["controller_events"][-1]["actor"] == "[redacted route ref]"
    assert stored["tasks"][0]["state"] == "done"
    assert stored["tasks"][0]["revision"] == 1
    assert stored["tasks"][1]["attention"]["active"] is True


def test_apply_attention_control_defer_and_drop_fail_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    data = {"schema_version": 1, "tasks": []}
    first = plugin.upsert_intake_task(data, _event(text="Approve invoice draft"), "Approve invoice draft")
    second = plugin.upsert_intake_task(data, _event(text="Pick launch date", chat_id="origin-b"), "Pick launch date")
    plugin.request_human_attention(data, first["task_id"], ask="Approve invoice?")
    plugin.request_human_attention(data, second["task_id"], ask="Pick launch date?")

    defer_result = plugin.apply_attention_control(data, "defer")

    assert defer_result["status"] == "applied"
    assert defer_result["decision"] == "deferred"
    assert defer_result["task_id"] == first["task_id"]
    assert first["state"] == "deferred"
    assert second["attention"]["active"] is True

    drop_result = plugin.apply_attention_control(data, "drop it")

    assert drop_result["status"] == "applied"
    assert drop_result["decision"] == "dropped"
    assert drop_result["task_id"] == second["task_id"]
    assert second["state"] == "cancelled"
    assert plugin.apply_attention_control(data, "approve")["status"] == "no_active"


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


def test_render_supervisor_dashboard_summarizes_queue_without_route_ids(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    data = {"schema_version": 1, "tasks": []}
    active = plugin.upsert_intake_task(data, _event(text="Approve invoice draft", message_id="msg-1"), "Approve invoice draft")
    triage = plugin.upsert_intake_task(data, _event(text="Rename product", message_id="msg-2"), "Rename product")
    waiting = plugin.upsert_intake_task(data, _event(text="Check deploy", message_id="msg-3"), "Check deploy")
    waiting["state"] = "waiting_agent"
    plugin.request_human_attention(
        data,
        active["task_id"],
        ask="Approve the invoice draft?",
        recommended_default="approve",
        why_now="unblocks sending",
        where="original thread",
    )

    rendered = plugin.render_supervisor_dashboard(data)

    assert "🧠 Supervisor" in rendered
    assert "Active Kevin ask" in rendered
    assert "Approve the invoice draft?" in rendered
    assert "Needs triage: 1" in rendered
    assert "Waiting on workers: 1" in rendered
    assert "Recommended next: resolve the active Kevin ask" in rendered
    assert triage["task_id"][:18] in rendered
    assert "channel-123" not in rendered
    assert "thread-456" not in rendered
    assert "kevin-1" not in rendered

    snapshot = plugin.supervisor_dashboard_snapshot(data)
    serialized = json.dumps(snapshot, ensure_ascii=False)
    assert "\"origin\"" not in serialized
    assert "channel-123" not in serialized
    assert "thread-456" not in serialized
    assert "kevin-1" not in serialized


def test_dashboard_redacts_route_like_attention_metadata(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    data = {"schema_version": 1, "tasks": []}
    task = plugin.upsert_intake_task(data, _event(text="Approve invoice draft"), "Approve invoice draft")
    plugin.request_human_attention(
        data,
        task["task_id"],
        ask="Approve the invoice draft?",
        recommended_default="message:msg-123",
        where="channel-123",
        why_now="thread-456 blocks follow-up",
    )

    rendered = plugin.render_supervisor_dashboard(data)
    serialized = json.dumps(plugin.supervisor_dashboard_snapshot(data), ensure_ascii=False)

    assert "channel-123" not in rendered
    assert "thread-456" not in rendered
    assert "message:msg-123" not in rendered
    assert "channel-123" not in serialized
    assert "thread-456" not in serialized
    assert "message:msg-123" not in serialized


def test_dashboard_redacts_numeric_and_platform_route_targets(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    data = {"schema_version": 1, "tasks": []}
    task = plugin.upsert_intake_task(data, _event(text="Approve invoice draft"), "Approve invoice draft")
    plugin.request_human_attention(
        data,
        task["task_id"],
        ask="Approve the invoice draft?",
        recommended_default="1508918955744559246",
        where="discord:1508918955744559246:1509636803970207794",
        why_now="route target 1509636803970207794 is waiting",
    )

    rendered = plugin.render_supervisor_dashboard(data)
    serialized = json.dumps(plugin.supervisor_dashboard_snapshot(data), ensure_ascii=False)

    for leaked in ("1508918955744559246", "1509636803970207794", "discord:1508918955744559246:1509636803970207794"):
        assert leaked not in rendered
        assert leaked not in serialized


def test_configured_supervisor_channel_ids_disable_name_fallback(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_SUPERVISOR_CHANNELS", "configured-supervisor")
    plugin = _load_plugin()

    result = plugin.pre_gateway_dispatch(
        event=_event(
            text="status",
            chat_id="wrong-supervisor-channel",
            chat_name="🧠-supervisor",
            thread_id=None,
            message_id="msg-status",
        ),
        gateway=_Gateway(True),
        session_store=None,
    )

    assert result["action"] == "rewrite"
    assert "Current supervisor dashboard" not in result["text"]
    data = _read_store(tmp_path)
    assert [task["objective"] for task in data["tasks"]] == ["status"]


def test_supervisor_channel_smart_apostrophe_status_does_not_create_task(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()

    result = plugin.pre_gateway_dispatch(
        event=_event(
            text="what’s next?",
            chat_id="supervisor-channel",
            chat_name="🧠-supervisor",
            thread_id=None,
            message_id="msg-status",
        ),
        gateway=_Gateway(True),
        session_store=None,
    )

    assert result["action"] == "rewrite"
    assert "Current supervisor dashboard" in result["text"]
    assert not (tmp_path / "workspace" / "supervisor" / "state" / "supervisor-tasks.json").exists()


def test_supervisor_channel_bare_control_reply_without_active_ask_does_not_create_task(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()

    for text in ("approve", "approve and keep going", "defer", "drop it"):
        result = plugin.pre_gateway_dispatch(
            event=_event(
                text=text,
                chat_id="supervisor-channel",
                chat_name="🧠-supervisor",
                thread_id=None,
                message_id=f"msg-{text}",
            ),
            gateway=_Gateway(True),
            session_store=None,
        )
        assert result["action"] == "rewrite"
        assert "No active Kevin ask" in result["text"]

    assert not (tmp_path / "workspace" / "supervisor" / "state" / "supervisor-tasks.json").exists()


def test_supervisor_channel_non_bare_control_words_without_active_ask_can_be_new_task(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()

    result = plugin.pre_gateway_dispatch(
        event=_event(
            text="ok, but change the date first",
            chat_id="supervisor-channel",
            chat_name="🧠-supervisor",
            thread_id=None,
            message_id="msg-ok-but",
        ),
        gateway=_Gateway(True),
        session_store=None,
    )

    assert result["action"] == "rewrite"
    assert "No active Kevin ask" not in result["text"]
    data = _read_store(tmp_path)
    assert [task["objective"] for task in data["tasks"]] == ["ok, but change the date first"]


def test_supervisor_channel_reply_fails_closed_with_multiple_global_active_asks(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    data = {"schema_version": 1, "tasks": []}
    first = plugin.upsert_intake_task(data, _event(text="Approve invoice", chat_id="origin-a"), "Approve invoice")
    second = plugin.upsert_intake_task(data, _event(text="Pick launch", chat_id="origin-b"), "Pick launch")
    plugin.request_human_attention(data, first["task_id"], ask="Approve invoice?")
    plugin.request_human_attention(data, second["task_id"], ask="Pick launch date?")
    second["attention"]["active"] = True
    second["attention"]["queued"] = False
    plugin.save_task_store(data)

    result = plugin.pre_gateway_dispatch(
        event=_event(
            text="approve",
            chat_id="supervisor-channel",
            chat_name="🧠-supervisor",
            thread_id=None,
            message_id="msg-approve",
        ),
        gateway=_Gateway(True),
        session_store=None,
    )

    assert result["action"] == "rewrite"
    assert "Multiple active Kevin asks" in result["text"]
    updated = _read_store(tmp_path)
    assert all(not task.get("human_replies") for task in updated["tasks"])


def test_supervisor_channel_status_message_rewrites_to_dashboard_without_new_task(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    plugin.pre_gateway_dispatch(
        event=_event(text="Rename product", message_id="msg-1"),
        gateway=_Gateway(True),
        session_store=None,
    )

    result = plugin.pre_gateway_dispatch(
        event=_event(
            text="status",
            chat_id="supervisor-channel",
            chat_name="🧠-supervisor",
            thread_id=None,
            message_id="msg-status",
        ),
        gateway=_Gateway(True),
        session_store=None,
    )

    assert result["action"] == "rewrite"
    assert "Current supervisor dashboard" in result["text"]
    assert "🧠 Supervisor" in result["text"]
    data = _read_store(tmp_path)
    assert [task["objective"] for task in data["tasks"]] == ["Rename product"]


def test_supervisor_channel_reply_captures_global_active_attention(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    plugin.pre_gateway_dispatch(
        event=_event(text="Approve invoice draft", message_id="msg-1"),
        gateway=_Gateway(True),
        session_store=None,
    )
    data = _read_store(tmp_path)
    task = data["tasks"][0]
    plugin.request_human_attention(data, task["task_id"], ask="Approve the invoice draft?")
    plugin.save_task_store(data)

    result = plugin.pre_gateway_dispatch(
        event=_event(
            text="approve and keep going",
            chat_id="supervisor-channel",
            chat_name="🧠-supervisor",
            thread_id=None,
            message_id="msg-approve",
        ),
        gateway=_Gateway(True),
        session_store=None,
    )

    assert result["action"] == "rewrite"
    assert "control reply was applied deterministically" in result["text"]
    updated = _read_store(tmp_path)
    assert len(updated["tasks"]) == 1
    assert updated["tasks"][0]["state"] == "done"
    assert updated["tasks"][0]["result"] == "Kevin approved: approve and keep going"
    assert updated["tasks"][0]["attention"]["active"] is False


def test_supervisor_channel_non_control_reply_to_cross_origin_active_ask_is_route_sanitized(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    data = {"schema_version": 1, "tasks": []}
    task = plugin.upsert_intake_task(
        data,
        _event(text="Approve invoice draft", chat_id="channel-123", thread_id="thread-456", message_id="msg-1"),
        "Approve invoice draft",
    )
    plugin.request_human_attention(data, task["task_id"], ask="Approve the invoice draft?")
    task["attention"]["where"] = "discord:1508918955744559246:1509636803970207794"
    task["attention"]["recommended_default"] = "message:msg-123"
    task["attention"]["why_now"] = "reply in channel-123"
    plugin.save_task_store(data)

    result = plugin.pre_gateway_dispatch(
        event=_event(
            text="I need the safer copy first",
            chat_id="supervisor-channel",
            chat_name="🧠-supervisor",
            thread_id=None,
            message_id="msg-natural",
        ),
        gateway=_Gateway(True),
        session_store=None,
    )

    assert result["action"] == "rewrite"
    assert "Active supervisor task" in result["text"]
    assert "channel-123" not in result["text"]
    assert "thread-456" not in result["text"]
    assert "kevin-1" not in result["text"]
    assert "1508918955744559246" not in result["text"]
    assert "1509636803970207794" not in result["text"]
    assert "message:msg-123" not in result["text"]
    assert "[redacted route ref]" in result["text"]
    updated = _read_store(tmp_path)
    assert updated["tasks"][0]["state"] == "in_discussion"
    assert updated["tasks"][0]["human_replies"][-1]["text"] == "I need the safer copy first"


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


def test_default_worker_registry_is_portable_json_contract(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()

    registry = plugin.default_worker_registry()

    json.dumps(registry)
    assert registry["schema_version"] == 1
    assert registry["portable"] is True
    assert registry["selection_policy"]["optimize_for"] == "kevin_attention"
    assert registry["selection_policy"]["default_max_risk"] == "medium"
    assert {worker["worker_id"] for worker in registry["workers"]} >= {"code-crab", "research-worker", "ops-worker"}
    for worker in registry["workers"]:
        assert set(worker) >= {"worker_id", "display_name", "capabilities", "risk_level", "cadence", "handoff_contract"}
        assert isinstance(worker["capabilities"], list) and worker["capabilities"]
        assert worker["risk_level"] in plugin.WORKER_RISK_LEVELS
        assert worker["cadence"] in plugin.WORKER_CADENCES
        assert worker["handoff_contract"]["input"] == "supervisor_task_envelope"
        assert worker["handoff_contract"]["output"] == "worker_callback_envelope"
        assert not any("secret" in key or "token" in key or "password" in key for key in worker)


def test_worker_registry_normalizes_copyable_external_entries(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()

    registry = plugin.normalize_worker_registry({
        "workers": [
            {
                "worker_id": "  Custom Analyst  ",
                "capabilities": ["Research", "research", "Market"],
                "risk_level": "HIGH",
                "cadence": "Scheduled",
                "handoff_contract": {
                    "input": "raw_origin_payload",
                    "output": "direct_dm",
                    "transport": "worker_direct_route",
                },
            }
        ]
    })

    worker = registry["workers"][0]
    assert worker["worker_id"] == "custom-analyst"
    assert worker["display_name"] == "Custom Analyst"
    assert worker["capabilities"] == ["research", "market"]
    assert worker["risk_level"] == "high"
    assert worker["cadence"] == "scheduled"
    assert worker["handoff_contract"]["input"] == "supervisor_task_envelope"
    assert worker["handoff_contract"]["output"] == "worker_callback_envelope"
    assert worker["handoff_contract"]["transport"] == "agent_system_native"


def test_plan_worker_dispatch_selects_lowest_risk_matching_specialist(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    registry = plugin.normalize_worker_registry({
        "workers": [
            {"worker_id": "generalist", "capabilities": ["code", "research"], "risk_level": "medium", "cadence": "on_demand"},
            {"worker_id": "code-reviewer", "capabilities": ["code", "testing"], "risk_level": "low", "cadence": "on_demand"},
        ]
    })
    data = {"schema_version": 1, "tasks": []}
    task = plugin.upsert_intake_task(data, _event(text="Review the failing tests"), "Review the failing tests")

    plan = plugin.plan_worker_dispatch(task, registry=registry, required_capabilities=["code"])

    assert plan["status"] == "ready"
    assert plan["worker_id"] == "code-reviewer"
    assert plan["needs_human"] is False
    assert plan["handoff"]["task_id"].startswith("wtsk_")
    assert plan["handoff"]["task_id"] != task["task_id"]
    assert plan["handoff"]["required_capabilities"] == ["code"]


def test_worker_handoff_keeps_route_metadata_supervisor_only(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    data = {"schema_version": 1, "tasks": []}
    task = plugin.upsert_intake_task(data, _event(text="Review the failing tests"), "Review the failing tests")
    task["title"] = "Review thread-456 deploy failure"
    task["objective"] = "Use message msg-789 from channel-123"
    task["acceptance_criteria"] = ["Do not mention kevin-1 or guild-1"]
    task["origin"]["chatId"] = "ExternalCaseABC"
    task["origin"]["numericRoomId"] = 987654321
    task["context_refs"] = {
        "files": [
            "/tmp/notes.md",
            "/tmp/channel-123-notes.md",
            "/tmp/external-channel-999.md",
            "/tmp/channel-general.md",
            1508918955744559246,
            "987654321",
            "/tmp/Channel=123.md",
            "/tmp/chat 123.md",
            "/tmp/thread.456.md",
            "/tmp/msg#789.md",
            "/tmp/channel123.md",
            "/tmp/chat123.md",
            "/tmp/сhannel-123.md",
            "/tmp/ѕerver-123.md",
            "/tmp/сһат-123.md",
            "/tmp/externalcaseabc.md",
        ],
        "urls": ["https://example.test/docs", "https://discord.test/channel/external-channel-999", "https://example.test/workspace-acme"],
        "repos": ["git@example.test:safe/repo.git", "git@example.test:thread-456/repo.git", "git@example.test:user-kevin/repo.git"],
        "chat_id": "channel-123",
        "nested": {
            "thread_id": "thread-456",
            "chatId": "external-chat-999",
            "messageId": "external-message-999",
            "source_chat_id": "source-channel-999",
            "channelId": "external-channel-999",
            "conversationId": "external-conversation-999",
            "room_id": "external-room-999",
            "originRef": "origin-ref-999",
            "сhat_id": "confusable-key-value",
            "safe_note": "look at msg-789 before replying",
        },
        "origin": task["origin"],
    }

    plan = plugin.plan_worker_dispatch(task, required_capabilities=["code"])
    handoff_text = json.dumps(plan["handoff"])

    assert "origin" not in plan["handoff"]
    assert set(plan["handoff"]) == {"schema_version", "task_id", "origin_ref", "route_policy", "context_refs", "required_capabilities", "worker_id", "callback_contract"}
    assert "origin_ref" in plan["handoff"]
    assert plan["handoff"]["context_refs"]["files"] == ["/tmp/notes.md"]
    assert plan["handoff"]["context_refs"]["urls"] == ["https://example.test/docs"]
    assert plan["handoff"]["context_refs"]["repos"] == ["git@example.test:safe/repo.git"]
    for leaked_key in (
        "chat_id", "thread_id", "message_id", "user_id", "guild_id", "fallback_route", "visibility",
        "chatId", "messageId", "source_chat_id", "channelId", "conversationId", "room_id", "originRef",
    ):
        assert leaked_key not in handoff_text
    for leaked_value in (
        "channel-123", "thread-456", "msg-789", "kevin-1", "guild-1",
        "external-chat-999", "external-message-999", "source-channel-999",
        "external-channel-999", "external-conversation-999", "external-room-999", "origin-ref-999",
        "channel-general", "workspace-acme", "user-kevin", "1508918955744559246", task["task_id"],
        "987654321", "Channel=123", "chat 123", "thread.456", "msg#789", "externalcaseabc",
        "channel123", "chat123", "сhannel-123", "ѕerver-123", "сһат-123", "confusable-key-value",
    ):
        assert leaked_value not in handoff_text


def test_explicit_empty_worker_registry_fails_closed_instead_of_using_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    data = {"schema_version": 1, "tasks": []}
    task = plugin.upsert_intake_task(data, _event(text="Review failing tests"), "Review failing tests")

    plan = plugin.plan_worker_dispatch(task, registry={}, required_capabilities=["code"])

    assert plan["status"] == "no_match"
    assert plan["worker_id"] is None


def test_worker_registry_rejects_duplicate_normalized_worker_ids(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()

    try:
        plugin.normalize_worker_registry({
            "workers": [
                {"worker_id": "Code Crab", "capabilities": ["code"]},
                {"worker_id": "code-crab", "capabilities": ["testing"]},
            ]
        })
    except ValueError as exc:
        assert "duplicate worker_id" in str(exc)
    else:
        raise AssertionError("duplicate normalized worker IDs should fail closed")


def test_plan_worker_dispatch_duplicate_registry_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    data = {"schema_version": 1, "tasks": []}
    task = plugin.upsert_intake_task(data, _event(text="Review failing tests"), "Review failing tests")

    plan = plugin.plan_worker_dispatch(
        task,
        registry={
            "workers": [
                {"worker_id": "Code Crab", "capabilities": ["code"]},
                {"worker_id": "code-crab", "capabilities": ["testing"]},
            ]
        },
        required_capabilities=["code"],
    )

    assert plan["status"] == "no_match"
    assert plan["worker_id"] is None
    assert "duplicate worker_id" in plan["reason"]


def test_assign_worker_to_task_records_portable_dispatch_plan(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    data = {"schema_version": 1, "tasks": []}
    task = plugin.upsert_intake_task(data, _event(text="Code Crab should inspect this bug"), "Code Crab should inspect this bug")

    plan = plugin.assign_worker_to_task(data, task["task_id"], required_capabilities=["code", "testing"])

    assert plan["status"] == "ready"
    assert task["owner"] == plan["worker_id"]
    assert task["state"] == "triaged"
    assert task["worker_assignment"]["worker_id"] == plan["worker_id"]
    assert task["worker_assignment"]["handoff"]["origin_ref"]
    assert "origin" not in task["worker_assignment"]["handoff"]
    assert plan["handoff"]["callback"]["token"]
    assert "token" not in task["worker_assignment"]["handoff"]["callback"]
    assert task["worker_assignment"]["callback_auth"]["token_hash"]


def test_ready_worker_assignment_clears_stale_attention_state(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    data = {"schema_version": 1, "tasks": []}
    task = plugin.upsert_intake_task(data, _event(text="Code Crab should inspect this bug"), "Code Crab should inspect this bug")
    plugin.request_human_attention(data, task["task_id"], ask="Old ask")

    plugin.assign_worker_to_task(data, task["task_id"], required_capabilities=["code"])

    assert task["state"] == "triaged"
    assert task["attention"]["active"] is False
    assert task["attention"]["queued"] is False
    assert task["attention"]["ask"] is None
    assert "Old ask" not in json.dumps(task["attention"])


def test_worker_callback_control_requires_token_and_uses_opaque_task_ref(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    data = {"schema_version": 1, "tasks": []}
    task = plugin.upsert_intake_task(data, _event(text="Code Crab should inspect this bug"), "Code Crab should inspect this bug")
    plan = plugin.assign_worker_to_task(data, task["task_id"], required_capabilities=["code"])
    callback = plan["handoff"]["callback"]
    token = callback["token"]
    opaque_task_id = callback["task_id"]
    plugin.save_task_store(data)

    result = plugin.supervisor_control(
        "append_worker_callback",
        payload={
            "task_id": opaque_task_id,
            "callback_token": token,
            "callback_id": "cb-1",
            "worker": plan["worker_id"],
            "worker_status": "done",
            "summary": "Tests are green.",
        },
        actor={"worker": plan["worker_id"], "callback_token": token},
    )

    assert result["status"] == "applied"
    assert result["task_id"] == task["task_id"]
    assert result["callback_id"] == "cb-1"
    stored = _read_store(tmp_path)
    stored_task = stored["tasks"][0]
    serialized_store = json.dumps(stored)
    assert stored_task["state"] == "review"
    assert stored_task["callbacks"][-1]["callback_id"] == "cb-1"
    assert stored_task["callbacks"][-1]["summary"] == "Tests are green."
    assert stored_task["worker_assignment"]["callback_auth"]["token_hash"]
    assert token not in serialized_store
    assert stored["control_plane"]["last_event_seq"] == 1


def test_worker_callback_control_rejects_bad_token_without_mutating(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    data = {"schema_version": 1, "tasks": []}
    task = plugin.upsert_intake_task(data, _event(text="Code Crab should inspect this bug"), "Code Crab should inspect this bug")
    plan = plugin.assign_worker_to_task(data, task["task_id"], required_capabilities=["code"])
    opaque_task_id = plan["handoff"]["callback"]["task_id"]
    plugin.save_task_store(data)

    result = plugin.supervisor_control(
        "append_worker_callback",
        payload={
            "task_id": opaque_task_id,
            "callback_token": "wrong-token",
            "callback_id": "cb-evil",
            "worker": plan["worker_id"],
            "worker_status": "done",
            "summary": "Pretend this is complete.",
        },
        actor={"worker": "attacker", "channel_id": "channel-123"},
    )

    assert result["status"] == "unauthorized"
    assert "dashboard" not in result
    assert "rendered_dashboard" not in result
    stored = _read_store(tmp_path)
    stored_task = stored["tasks"][0]
    assert stored_task["state"] == "triaged"
    assert "callbacks" not in stored_task
    assert stored["control_plane"]["last_event_seq"] == 0
    assert "wrong-token" not in json.dumps(result)


def test_worker_callback_control_dedupes_callback_id_without_second_mutation(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    data = {"schema_version": 1, "tasks": []}
    task = plugin.upsert_intake_task(data, _event(text="Code Crab should inspect this bug"), "Code Crab should inspect this bug")
    plan = plugin.assign_worker_to_task(data, task["task_id"], required_capabilities=["code"])
    callback = plan["handoff"]["callback"]
    token = callback["token"]
    plugin.save_task_store(data)

    first = plugin.supervisor_control(
        "append_worker_callback",
        payload={
            "task_id": callback["task_id"],
            "callback_token": token,
            "callback_id": "cb-repeat",
            "worker": plan["worker_id"],
            "worker_status": "blocked",
            "summary": "Need a decision.",
        },
    )
    second = plugin.supervisor_control(
        "append_worker_callback",
        payload={
            "task_id": callback["task_id"],
            "callback_token": token,
            "callback_id": "cb-repeat",
            "worker": plan["worker_id"],
            "worker_status": "done",
            "summary": "Different duplicate payload should not overwrite.",
            "needs_human": True,
            "ask": "Should I proceed?",
        },
    )

    assert first["status"] == "applied"
    assert second["status"] == "duplicate"
    stored_task = _read_store(tmp_path)["tasks"][0]
    assert len(stored_task["callbacks"]) == 1
    assert stored_task["callbacks"][0]["summary"] == "Need a decision."
    assert stored_task["state"] == "blocked"
    assert stored_task["attention"]["active"] is False


def test_assign_stored_worker_to_task_locks_loads_assigns_and_saves(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    data = {"schema_version": 1, "tasks": []}
    task = plugin.upsert_intake_task(data, _event(text="Code Crab should inspect this bug"), "Code Crab should inspect this bug")
    plugin.save_task_store(data)

    plan = plugin.assign_stored_worker_to_task(task["task_id"], required_capabilities=["code"])

    updated = _read_store(tmp_path)
    assert plan["status"] == "ready"
    assert updated["tasks"][0]["worker_assignment"]["worker_id"] == plan["worker_id"]
    assert updated["tasks"][0]["state"] == "triaged"


def test_dispatch_worker_task_sends_sanitized_handoff_and_records_waiting(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    data = {"schema_version": 1, "tasks": []}
    task = plugin.upsert_intake_task(data, _event(text="Code Crab should inspect this bug"), "Code Crab should inspect this bug")
    task["context_refs"] = {
        "files": ["/tmp/safe-notes.md", "/tmp/channel-123-notes.md"],
        "urls": ["https://example.test/safe", "https://discord.test/channel-123"],
        "repos": ["git@example.test:safe/repo.git", "git@example.test:thread-456/repo.git"],
    }
    calls = []

    def dispatcher(payload):
        calls.append(payload)
        return {"success": True, "dispatch_id": "dispatch-1"}

    result = plugin.dispatch_worker_task(data, task["task_id"], dispatcher=dispatcher, required_capabilities=["code"])

    assert result["status"] == "dispatched"
    assert result["dispatch_id"] == "dispatch-1"
    assert len(calls) == 1
    payload = calls[0]
    payload_text = json.dumps(payload)
    assert set(payload) == {"schema_version", "worker_id", "handoff"}
    assert payload["worker_id"] == "code-crab"
    assert payload["handoff"]["callback"]["token"]
    assert payload["handoff"]["callback"]["action"] == "append_worker_callback"
    assert payload["handoff"]["context_refs"]["files"] == ["/tmp/safe-notes.md"]
    assert payload["handoff"]["context_refs"]["urls"] == ["https://example.test/safe"]
    assert payload["handoff"]["context_refs"]["repos"] == ["git@example.test:safe/repo.git"]
    assert task["state"] == "waiting_agent"
    assert task["worker_dispatches"][-1]["status"] == "dispatched"
    assert task["worker_dispatches"][-1]["dispatch_id"] == "dispatch-1"
    assert '"origin"' not in payload_text
    assert task["task_id"] not in payload_text
    assert "channel-123" not in payload_text
    assert "thread-456" not in payload_text
    assert payload["handoff"]["callback"]["token"] not in json.dumps(task)


def test_dispatch_worker_assignment_without_callback_token_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    data = {"schema_version": 1, "tasks": []}
    task = plugin.upsert_intake_task(data, _event(text="Code Crab should inspect this bug"), "Code Crab should inspect this bug")
    plugin.assign_worker_to_task(data, task["task_id"], required_capabilities=["code"])
    calls = []

    result = plugin.dispatch_worker_assignment(data, task["task_id"], dispatcher=lambda payload: calls.append(payload))

    assert result["status"] == "missing_callback_token"
    assert calls == []
    assert task["state"] == "triaged"
    assert "worker_dispatches" not in task


def test_dispatch_worker_assignment_rejects_cross_task_assignment(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    data = {"schema_version": 1, "tasks": []}
    first = plugin.upsert_intake_task(data, _event(text="First bug", message_id="msg-1"), "First bug")
    second = plugin.upsert_intake_task(data, _event(text="Second bug", message_id="msg-2"), "Second bug")
    first_assignment = plugin.assign_worker_to_task(data, first["task_id"], required_capabilities=["code"])
    plugin.assign_worker_to_task(data, second["task_id"], required_capabilities=["code"])
    calls = []

    result = plugin.dispatch_worker_assignment(data, second["task_id"], dispatcher=lambda payload: calls.append(payload), assignment=first_assignment)

    assert result["status"] == "assignment_mismatch"
    assert calls == []
    assert second["state"] == "triaged"
    assert "worker_dispatches" not in second


def test_dispatch_worker_assignment_rejects_mutated_handoff_fields(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    data = {"schema_version": 1, "tasks": []}
    task = plugin.upsert_intake_task(data, _event(text="Code Crab should inspect this bug"), "Code Crab should inspect this bug")
    assignment = plugin.assign_worker_to_task(data, task["task_id"], required_capabilities=["code"])
    assignment["handoff"]["origin"] = task["origin"]
    calls = []

    result = plugin.dispatch_worker_assignment(data, task["task_id"], dispatcher=lambda payload: calls.append(payload), assignment=assignment)

    assert result["status"] == "assignment_mismatch"
    assert calls == []
    assert task["state"] == "triaged"
    assert "worker_dispatches" not in task


def test_dispatch_worker_task_does_not_apply_inline_worker_result(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    data = {"schema_version": 1, "tasks": []}
    task = plugin.upsert_intake_task(data, _event(text="Code Crab should inspect this bug"), "Code Crab should inspect this bug")

    def dispatcher(_payload):
        return {"success": True, "dispatch_id": "dispatch-2", "worker_status": "done", "summary": "Inline result should be ignored."}

    result = plugin.dispatch_worker_task(data, task["task_id"], dispatcher=dispatcher, required_capabilities=["code"])

    assert result["status"] == "dispatched"
    assert task["state"] == "waiting_agent"
    assert "callbacks" not in task
    assert "Inline result should be ignored." not in json.dumps(task)


def test_dispatch_worker_failure_redacts_callback_token_from_audit(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    data = {"schema_version": 1, "tasks": []}
    task = plugin.upsert_intake_task(data, _event(text="Code Crab should inspect this bug"), "Code Crab should inspect this bug")
    seen = {}

    def dispatcher(payload):
        token = payload["handoff"]["callback"]["token"]
        seen["token"] = token
        return {"success": False, "dispatch_id": f"dispatch-{token}", "error": f"transport failed token={token}"}

    result = plugin.dispatch_worker_task(data, task["task_id"], dispatcher=dispatcher, required_capabilities=["code"])

    assert result["status"] == "failed"
    serialized_task = json.dumps(task)
    assert seen["token"] not in serialized_task
    assert seen["token"] not in json.dumps(result)
    assert "[redacted secret]" in serialized_task
    assert task["worker_dispatches"][-1]["status"] == "failed"


def test_worker_dispatch_audit_is_bounded(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    data = {"schema_version": 1, "tasks": []}
    task = plugin.upsert_intake_task(data, _event(text="Code Crab should inspect this bug"), "Code Crab should inspect this bug")

    for index in range(plugin.WORKER_DISPATCH_EVENT_LIMIT + 5):
        plugin._record_worker_dispatch_attempt(
            data,
            task["task_id"],
            worker_id="code-crab",
            status="failed",
            dispatch_id=f"dispatch-{index}",
            error="boom",
        )

    assert len(task["worker_dispatches"]) == plugin.WORKER_DISPATCH_EVENT_LIMIT
    assert task["worker_dispatches"][0]["dispatch_id"] == "dispatch-5"


def test_dispatch_stored_worker_task_persists_callback_auth_before_external_dispatch(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    data = {"schema_version": 1, "tasks": []}
    task = plugin.upsert_intake_task(data, _event(text="Code Crab should inspect this bug"), "Code Crab should inspect this bug")
    plugin.save_task_store(data)
    snapshots = []
    real_save = plugin.save_task_store

    def flaky_save(next_data):
        snapshots.append(json.loads(json.dumps(next_data)))
        if len(snapshots) == 2:
            raise RuntimeError("second save failed")
        return real_save(next_data)

    monkeypatch.setattr(plugin, "save_task_store", flaky_save)
    calls = []

    def dispatcher(payload):
        calls.append(payload)
        return {"success": True, "dispatch_id": "dispatch-after-auth"}

    result = plugin.dispatch_stored_worker_task(task["task_id"], dispatcher=dispatcher, required_capabilities=["code"])

    assert calls and calls[0]["handoff"]["callback"]["token"]
    assert result["status"] == "dispatched"
    assert result["audit_persisted"] is False
    assert snapshots[0]["tasks"][0]["worker_assignment"]["callback_auth"]["token_hash"]
    assert calls[0]["handoff"]["callback"]["token"] not in json.dumps(snapshots[0])


def test_dispatch_stored_worker_task_locks_assigns_dispatches_and_saves(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    data = {"schema_version": 1, "tasks": []}
    task = plugin.upsert_intake_task(data, _event(text="Code Crab should inspect this bug"), "Code Crab should inspect this bug")
    plugin.save_task_store(data)
    calls = []

    def dispatcher(payload):
        calls.append(payload)
        return {"success": True, "id": "dispatch-stored"}

    result = plugin.dispatch_stored_worker_task(task["task_id"], dispatcher=dispatcher, required_capabilities=["code"])

    updated = _read_store(tmp_path)
    assert result["status"] == "dispatched"
    assert result["audit_persisted"] is True
    assert calls[0]["handoff"]["callback"]["token"]
    assert updated["tasks"][0]["state"] == "waiting_agent"
    assert updated["tasks"][0]["worker_dispatches"][-1]["dispatch_id"] == "dispatch-stored"
    assert calls[0]["handoff"]["callback"]["token"] not in json.dumps(updated)


def test_origin_ref_is_task_opaque_not_same_for_same_origin(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    data = {"schema_version": 1, "tasks": []}
    first = plugin.upsert_intake_task(data, _event(text="First task", message_id="msg-1"), "First task")
    second = plugin.upsert_intake_task(data, _event(text="Second task", message_id="msg-2"), "Second task")

    first_ref = plugin.plan_worker_dispatch(first, required_capabilities=["code"])["handoff"]["origin_ref"]
    second_ref = plugin.plan_worker_dispatch(second, required_capabilities=["code"])["handoff"]["origin_ref"]

    assert first_ref != second_ref
    assert "channel-123" not in first_ref
    assert "thread-456" not in first_ref


def test_risky_worker_dispatch_plan_requires_human_instead_of_auto_assigning(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    registry = plugin.normalize_worker_registry({
        "workers": [
            {"worker_id": "publisher", "capabilities": ["deploy", "publish"], "risk_level": "high", "cadence": "on_demand"}
        ]
    })
    data = {"schema_version": 1, "tasks": []}
    task = plugin.upsert_intake_task(data, _event(text="Publish the production deploy"), "Publish the production deploy")

    plan = plugin.assign_worker_to_task(
        data,
        task["task_id"],
        registry=registry,
        required_capabilities=["deploy"],
        max_risk="medium",
    )

    assert plan["status"] == "needs_human_approval"
    assert plan["needs_human"] is True
    assert task["owner"] == "supervisor"
    assert task["state"] == "needs_human"
    assert task["attention"]["active"] is True
    assert "publisher" not in task["attention"]["ask"]
    assert task["attention"]["recommended_default"] == "reply approve to hand off"
    assert "otherwise" not in task["attention"]["recommended_default"].lower()
    assert "risk" not in task["attention"]["why_now"].lower()


def test_no_matching_worker_asks_for_registry_choice_not_approval(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    registry = plugin.normalize_worker_registry({
        "workers": [
            {"worker_id": "researcher", "capabilities": ["research"], "risk_level": "low", "cadence": "on_demand"}
        ]
    })
    data = {"schema_version": 1, "tasks": []}
    task = plugin.upsert_intake_task(data, _event(text="Review failing tests"), "Review failing tests")

    plan = plugin.assign_worker_to_task(data, task["task_id"], registry=registry, required_capabilities=["code"])

    assert plan["status"] == "no_match"
    assert task["state"] == "needs_human"
    assert task["attention"]["ask"] == "Pick the right specialist for this task."
    assert " or " not in task["attention"]["ask"].lower()
    assert "Approve dispatching" not in task["attention"]["ask"]
    assert "Code Crab" not in task["attention"]["recommended_default"]


def test_worker_handoff_drops_route_like_required_capabilities(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    registry = plugin.normalize_worker_registry({
        "workers": [
            {"worker_id": "coder", "capabilities": ["code", "thread-456", "thread456", "сhannel123", "12345"], "risk_level": "low", "cadence": "on_demand"}
        ]
    })
    data = {"schema_version": 1, "tasks": []}
    task = plugin.upsert_intake_task(data, _event(text="Review failing tests"), "Review failing tests")

    plan = plugin.plan_worker_dispatch(task, registry=registry, required_capabilities=["code", "thread-456", "thread456", "сhannel123", "12345"])

    assert plan["status"] == "ready"
    assert plan["required_capabilities"] == ["code"]
    assert plan["handoff"]["required_capabilities"] == ["code"]
    assert "thread-456" not in json.dumps(plan["handoff"])
    assert "thread456" not in json.dumps(plan["handoff"])
    assert "сhannel123" not in json.dumps(plan["handoff"])
    assert "12345" not in json.dumps(plan["handoff"])


def test_unsafe_only_required_capabilities_fail_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    data = {"schema_version": 1, "tasks": []}
    task = plugin.upsert_intake_task(data, _event(text="Review failing tests"), "Review failing tests")

    plan = plugin.plan_worker_dispatch(task, required_capabilities=["thread-456", "thread456", "сhannel123", "12345"])

    assert plan["status"] == "no_match"
    assert plan["worker_id"] is None
    assert plan["required_capabilities"] == []


def test_malformed_or_route_like_only_registry_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin()
    data = {"schema_version": 1, "tasks": []}
    task = plugin.upsert_intake_task(data, _event(text="Review failing tests"), "Review failing tests")

    malformed = plugin.plan_worker_dispatch(task, registry=[], required_capabilities=["code"])
    route_only_registry = plugin.normalize_worker_registry({
        "workers": [
            {"worker_id": "unsafe", "capabilities": ["thread456", "channel123", "сhannel123"], "risk_level": "low", "cadence": "on_demand"}
        ]
    })
    route_only = plugin.plan_worker_dispatch(task, registry=route_only_registry, required_capabilities=[])

    assert malformed["status"] == "no_match"
    assert route_only_registry["workers"] == []
    assert route_only["status"] == "no_match"


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
    assert data["schema_version"] == plugin.TASK_STORE_SCHEMA_VERSION
    assert data["tasks"] == []
    assert "updated_at" in data
