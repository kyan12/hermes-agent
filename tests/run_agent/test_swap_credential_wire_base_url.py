"""Credential rotation must preserve the API-wire base URL path.

Pool entries store the *credential-level* base URL (e.g. Kimi Code's
``https://api.kimi.com/coding`` — correct for the Anthropic SDK, which appends
``/v1/messages`` itself).  Every client-construction path runs that URL
through ``agent.auxiliary_client._to_openai_base_url`` before handing it to
the OpenAI SDK (``/coding`` → ``/coding/v1``, ``/anthropic`` → ``/v1``).
``AIAgent._swap_credential`` must apply the same normalization — otherwise a
chat_completions agent on ``/coding/v1`` that leases or rotates to a pool
entry gets rebound to ``/coding`` and every request 404s on
``/coding/chat/completions`` (observed: delegate_task subagents dying on
their first model call, kanban t_f736ebb5).
"""

from types import MethodType, SimpleNamespace
from unittest.mock import MagicMock, patch

from run_agent import AIAgent


def _make_chat_agent(base_url: str) -> SimpleNamespace:
    agent = SimpleNamespace(
        api_mode="chat_completions",
        provider="kimi-coding",
        model="kimi-k3",
        api_key="old-key",
        base_url=base_url,
        _client_kwargs={"api_key": "old-key", "base_url": base_url},
        _apply_client_headers_for_base_url=MagicMock(),
        _replace_primary_openai_client=MagicMock(),
    )
    # Exercise the real route-config reapplication (TLS/header recompute),
    # with only the header writer mocked out.
    agent._reapply_route_client_config = MethodType(
        AIAgent._reapply_route_client_config, agent
    )
    return agent


def _make_entry(base_url: str) -> SimpleNamespace:
    return SimpleNamespace(
        id="entry-1",
        runtime_api_key="new-key",
        access_token="",
        runtime_base_url=base_url,
        base_url=base_url,
    )


def test_swap_credential_preserves_kimi_openai_wire_v1_suffix():
    """Pool entry /coding must become /coding/v1 on the OpenAI wire."""
    agent = _make_chat_agent("https://api.kimi.com/coding/v1")
    entry = _make_entry("https://api.kimi.com/coding")

    AIAgent._swap_credential(agent, entry)

    assert agent.base_url == "https://api.kimi.com/coding/v1"
    assert agent._client_kwargs["base_url"] == "https://api.kimi.com/coding/v1"
    assert agent.api_key == "new-key"
    agent._replace_primary_openai_client.assert_called_once_with(
        reason="credential_rotation"
    )


def test_swap_credential_rewrites_anthropic_suffix_for_openai_wire():
    """MiniMax-style /anthropic credential URL maps to /v1 for chat_completions."""
    agent = _make_chat_agent("https://api.minimax.io/v1")
    entry = _make_entry("https://api.minimax.io/anthropic")

    AIAgent._swap_credential(agent, entry)

    assert agent.base_url == "https://api.minimax.io/v1"
    assert agent._client_kwargs["base_url"] == "https://api.minimax.io/v1"


def test_swap_credential_keeps_plain_url_unchanged():
    """URLs without a known wire rewrite pass through untouched."""
    agent = _make_chat_agent("https://a.example/v1")
    entry = _make_entry("https://b.example/v1")

    AIAgent._swap_credential(agent, entry)

    assert agent.base_url == "https://b.example/v1"
    assert agent._client_kwargs["base_url"] == "https://b.example/v1"


def test_swap_credential_still_overrides_when_host_genuinely_differs():
    """Leftover-OpenRouter guard: a real host change must still win.

    The delegation inheritance docstring guards the case where the agent
    surface carries a stale OpenRouter URL while the pool entry points at
    the real endpoint.  Rotation must still rebind — with the wire path
    normalized, not dropped.
    """
    agent = _make_chat_agent("https://openrouter.ai/api/v1")
    agent.provider = "openrouter"
    entry = _make_entry("https://api.kimi.com/coding")

    AIAgent._swap_credential(agent, entry)

    assert agent.base_url == "https://api.kimi.com/coding/v1"
    assert agent._client_kwargs["base_url"] == "https://api.kimi.com/coding/v1"


def test_swap_credential_anthropic_mode_keeps_credential_url():
    """Anthropic Messages wire: the SDK appends /v1/messages itself, so the
    credential URL (no /v1) is correct as-is and must not gain a suffix."""
    agent = SimpleNamespace(
        api_mode="anthropic_messages",
        provider="kimi-coding",
        model="kimi-k3",
        api_key="old-key",
        base_url="https://api.kimi.com/coding",
        _anthropic_client=MagicMock(),
        _client_kwargs={},
    )
    entry = _make_entry("https://api.kimi.com/coding")

    with patch(
        "agent.anthropic_adapter.build_anthropic_client", return_value=MagicMock()
    ):
        AIAgent._swap_credential(agent, entry)

    assert agent.base_url == "https://api.kimi.com/coding"
    assert agent._anthropic_base_url == "https://api.kimi.com/coding"
