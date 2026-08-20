"""Subagent base_url inheritance must preserve the parent's live wire path.

Characterization coverage for the inheritance contract referenced by kanban
t_f736ebb5: a kimi-coding parent's credential surface URL is
``https://api.kimi.com/coding`` while its live OpenAI client calls
``https://api.kimi.com/coding/v1``.  The spawned child must inherit the
full path (``/v1`` included) or its first chat_completions request 404s on
``/coding/chat/completions``.

Note: these tests intentionally exercise ``_inherit_parent_base_url`` /
``_build_child_agent``, which were never broken — the t_f736ebb5 404 came
from the later pool-lease ``_swap_credential`` clobber, covered by
``tests/run_agent/test_swap_credential_wire_base_url.py``.  This file locks
the inheritance contract so a future change to either side is caught.
"""

import threading
import unittest
from unittest.mock import MagicMock, patch

from tools.delegate_tool import _build_child_agent, _inherit_parent_base_url


def _make_kimi_parent():
    """Parent whose surface URL lacks /v1 but whose live client has it."""
    parent = MagicMock()
    parent.base_url = "https://api.kimi.com/coding"  # credential surface
    parent.api_key = "sk-kimi-test"
    parent.provider = "kimi-coding"
    parent.api_mode = "chat_completions"
    parent.model = "kimi-k3"
    parent.platform = "cli"
    parent.providers_allowed = None
    parent.providers_ignored = None
    parent.providers_order = None
    parent.provider_sort = None
    parent._session_db = None
    parent._delegate_depth = 0
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    parent._print_fn = None
    parent.tool_progress_callback = None
    parent.thinking_callback = None
    # The live client kwargs carry the wire URL with the /v1 suffix.
    parent._client_kwargs = {
        "api_key": "sk-kimi-test",
        "base_url": "https://api.kimi.com/coding/v1/",
    }
    return parent


class TestInheritParentBaseUrl(unittest.TestCase):
    def test_live_client_path_suffix_wins_over_surface_url(self):
        parent = _make_kimi_parent()
        inherited = _inherit_parent_base_url(parent, parent.base_url)
        self.assertEqual(inherited, "https://api.kimi.com/coding/v1")

    def test_surface_url_used_when_live_matches(self):
        parent = _make_kimi_parent()
        parent._client_kwargs = {
            "api_key": "sk-kimi-test",
            "base_url": "https://api.kimi.com/coding",
        }
        parent.client = None
        inherited = _inherit_parent_base_url(parent, parent.base_url)
        self.assertEqual(inherited, "https://api.kimi.com/coding")


class TestBuildChildAgentBaseUrl(unittest.TestCase):
    def test_child_inherits_full_base_url_path(self):
        """_build_child_agent must pass the /v1-preserving URL to AIAgent."""
        parent = _make_kimi_parent()

        with patch("run_agent.AIAgent") as MockAgent:
            MockAgent.return_value = MagicMock()
            _build_child_agent(
                task_index=0,
                goal="Probe the endpoint",
                context=None,
                toolsets=None,
                model=None,
                max_iterations=10,
                parent_agent=parent,
                task_count=1,
                role="leaf",
            )

        _, kwargs = MockAgent.call_args
        self.assertEqual(kwargs["base_url"], "https://api.kimi.com/coding/v1")
        self.assertEqual(kwargs["provider"], "kimi-coding")
        self.assertEqual(kwargs["api_mode"], "chat_completions")


if __name__ == "__main__":
    unittest.main()
