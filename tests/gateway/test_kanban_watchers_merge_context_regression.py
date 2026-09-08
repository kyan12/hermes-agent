"""The extracted notifier collector remains a process-owned writer."""
from contextvars import ContextVar
from types import ModuleType
from unittest.mock import AsyncMock

import pytest

from gateway import kanban_watchers as watchers


@pytest.mark.asyncio
async def test_notifier_collection_does_not_inherit_request_context(monkeypatch):
    import hermes_cli

    # Collection itself is a unit seam; no live board or transport is needed.
    monkeypatch.setattr(hermes_cli, "kanban_db", ModuleType("kanban_db"), raising=False)
    request_marker = ContextVar[str | None]("watcher_merge_request_marker", default=None)
    seen = []

    class Runner(watchers.GatewayKanbanWatchersMixin):
        _running = True

        def _active_profile_name(self):
            return "default"

        async def _sleep_between_ticks(self, interval):
            self._running = False

    def collect(*args, **kwargs):
        seen.append(request_marker.get())
        return []

    monkeypatch.setattr(watchers, "_notifier_collect", collect)
    monkeypatch.setattr(watchers, "_gc_retention_days", lambda: 30)
    monkeypatch.setattr(watchers.asyncio, "sleep", AsyncMock())
    token = request_marker.set("delegated-request")
    try:
        await Runner()._kanban_notifier_watcher()
        assert request_marker.get() == "delegated-request"
    finally:
        request_marker.reset(token)
    assert seen == [None]
