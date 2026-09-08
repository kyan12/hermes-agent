"""Merge-boundary canaries: local authority gates on upstream tool/runtime APIs."""
import json
import os

import pytest

from agent.delegation_context import non_dispatcher_owned_context
from tools import kanban_tools as kt
from tools.registry import registry


def test_non_worker_context_does_not_inherit_continuation_authority(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "inherited-worker")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "worker-board")
    with non_dispatcher_owned_context():
        # No DB access is needed: the ambient worker is not this caller's principal.
        assert kt._inherited_parent_scope(None, None, []) == ({}, None)
        assert kt._board_authority_error("cron-board") is None
        assert kt._default_task_id(None) is None
    # A concurrent/parent worker must retain its identity; no environment scrubbing.
    assert os.environ["HERMES_KANBAN_TASK"] == "inherited-worker"
    assert os.environ["HERMES_KANBAN_BOARD"] == "worker-board"


def test_descendant_create_refused_before_board_or_payload_validation(monkeypatch):
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", "1")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    result = registry.dispatch("kanban_create", {"board": "../not-authority"})
    result = json.loads(result) if isinstance(result, str) else result
    assert "delegate_task child agents are not Kanban run owners" in result["error"]


@pytest.mark.parametrize("field", ["triage", "goal_mode"])
def test_create_keeps_upstream_structured_boolean_rejections(field):
    result = registry.dispatch("kanban_create", {
        "title": "invalid boolean", "assignee": "worker", field: "not-a-boolean",
    })
    result = json.loads(result) if isinstance(result, str) else result
    assert f"{field} must be a boolean" in result["error"]
    assert "unpack" not in result["error"]
