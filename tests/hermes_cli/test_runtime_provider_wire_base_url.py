"""Regression tests: resolve_runtime_provider must rebase credential-level
base URLs onto the OpenAI wire when the resolved api_mode is an OpenAI-wire
mode (chat_completions / codex_responses).

Provider credentials store the URL the credential was issued against — e.g.
an sk-kimi- Kimi Code credential carries ``https://api.kimi.com/coding``,
correct for the Anthropic SDK wire but a 404 for
``/coding/chat/completions``.  A user who explicitly pins
``model.api_mode: chat_completions`` for kimi-coding opts out of the
URL-based auto-detection that would route to anthropic_messages, so the
resolved runtime must pass the final base_url through the same
``_to_openai_base_url`` normalization client construction and
``AIAgent._swap_credential`` already apply (kanban t_f7914e5f; sibling of
the PR #13 delegate-path fix).

Covers both return points: ``_resolve_runtime_from_pool_entry`` (credential
pool) and the non-pool api_key branch of ``resolve_runtime_provider``.
"""

from types import SimpleNamespace

import pytest

from hermes_cli import runtime_provider as rp

KIMI_CODE_BARE = "https://api.kimi.com/coding"
KIMI_CODE_OPENAI = "https://api.kimi.com/coding/v1"


def _kimi_pool_entry():
    return SimpleNamespace(
        access_token="sk-kimi-pool-test",
        runtime_api_key=None,
        base_url=KIMI_CODE_BARE,
        runtime_base_url=None,
        source="manual",
    )


class _EmptyPool:
    def has_credentials(self):
        return False


# ---------------------------------------------------------------------------
# Pool path (_resolve_runtime_from_pool_entry)
# ---------------------------------------------------------------------------


def test_pool_entry_kimi_pinned_chat_completions_rebases_to_openai_wire(monkeypatch):
    """Acceptance 1 (pool path): pinned chat_completions + bare /coding
    credential URL must resolve to /coding/v1."""
    monkeypatch.setattr(rp, "_get_model_config", lambda: {
        "provider": "kimi-coding",
        "api_mode": "chat_completions",
        "default": "kimi-k3",
    })

    resolved = rp._resolve_runtime_from_pool_entry(
        provider="kimi-coding",
        entry=_kimi_pool_entry(),
        requested_provider="kimi-coding",
    )

    assert resolved["api_mode"] == "chat_completions"
    assert resolved["base_url"] == KIMI_CODE_OPENAI


def test_pool_entry_kimi_default_resolution_keeps_anthropic_wire(monkeypatch):
    """Acceptance 2 (pool path): no pinned api_mode — URL auto-detection
    routes /coding to anthropic_messages and the bare URL stays untouched."""
    monkeypatch.setattr(rp, "_get_model_config", lambda: {
        "provider": "kimi-coding",
        "default": "kimi-k3",
    })

    resolved = rp._resolve_runtime_from_pool_entry(
        provider="kimi-coding",
        entry=_kimi_pool_entry(),
        requested_provider="kimi-coding",
    )

    assert resolved["api_mode"] == "anthropic_messages"
    assert resolved["base_url"] == KIMI_CODE_BARE


def test_pool_entry_minimax_pinned_chat_completions_rebases_anthropic_suffix(monkeypatch):
    """MiniMax's /anthropic credential URL has the same credential-vs-wire
    mismatch; pinned chat_completions must land on /v1."""
    monkeypatch.setattr(rp, "_get_model_config", lambda: {
        "provider": "minimax",
        "api_mode": "chat_completions",
        "default": "MiniMax-M2.5",
    })
    entry = SimpleNamespace(
        access_token="minimax-pool-test-key",
        runtime_api_key=None,
        base_url="https://api.minimax.io/anthropic",
        runtime_base_url=None,
        source="manual",
    )

    resolved = rp._resolve_runtime_from_pool_entry(
        provider="minimax",
        entry=entry,
        requested_provider="minimax",
    )

    assert resolved["api_mode"] == "chat_completions"
    assert resolved["base_url"] == "https://api.minimax.io/v1"


@pytest.mark.parametrize(
    "entry_base_url",
    ["https://api.deepseek.com/v1", "https://api.deepseek.com"],
    ids=["already-normalized", "bare-url-byte-identical"],
)
def test_pool_entry_unknown_wire_provider_unchanged(monkeypatch, entry_base_url):
    """Acceptance 3 (pool path): providers without a known wire rewrite are
    byte-identical before and after the normalization — both for URLs that
    already carry a wire path and for bare URLs with no path suffix."""
    monkeypatch.setattr(rp, "_get_model_config", lambda: {
        "provider": "deepseek",
        "api_mode": "chat_completions",
        "default": "deepseek-v4-pro",
    })
    entry = SimpleNamespace(
        access_token="deepseek-pool-test-key",
        runtime_api_key=None,
        base_url=entry_base_url,
        runtime_base_url=None,
        source="manual",
    )

    resolved = rp._resolve_runtime_from_pool_entry(
        provider="deepseek",
        entry=entry,
        requested_provider="deepseek",
    )

    assert resolved["api_mode"] == "chat_completions"
    assert resolved["base_url"] == entry_base_url


# ---------------------------------------------------------------------------
# Non-pool api_key branch (resolve_runtime_provider)
# ---------------------------------------------------------------------------


def _patch_kimi_non_pool(monkeypatch, model_cfg, creds_base_url=KIMI_CODE_BARE):
    monkeypatch.setattr(rp, "resolve_provider", lambda *a, **k: "kimi-coding")
    monkeypatch.setattr(rp, "_get_model_config", lambda: model_cfg)
    monkeypatch.setattr(rp, "load_pool", lambda provider: _EmptyPool())
    monkeypatch.setattr(
        rp,
        "resolve_api_key_provider_credentials",
        lambda provider: {
            "provider": "kimi-coding",
            "api_key": "sk-kimi-nonpool-test",
            "base_url": creds_base_url,
            "source": "env",
        },
    )
    monkeypatch.delenv("KIMI_BASE_URL", raising=False)


def test_non_pool_kimi_pinned_chat_completions_rebases_to_openai_wire(monkeypatch):
    """Acceptance 1 (non-pool path): sk-kimi- credential resolves bare
    /coding; pinned chat_completions must rebase to /coding/v1."""
    _patch_kimi_non_pool(monkeypatch, {
        "provider": "kimi-coding",
        "api_mode": "chat_completions",
        "default": "kimi-k3",
    })

    resolved = rp.resolve_runtime_provider(requested="kimi-coding")

    assert resolved["provider"] == "kimi-coding"
    assert resolved["api_mode"] == "chat_completions"
    assert resolved["base_url"] == KIMI_CODE_OPENAI


def test_non_pool_kimi_default_resolution_keeps_anthropic_wire(monkeypatch):
    """Acceptance 2 (non-pool path): default kimi-coding resolution still
    picks anthropic_messages with the bare /coding URL."""
    _patch_kimi_non_pool(monkeypatch, {
        "provider": "kimi-coding",
        "default": "kimi-k3",
    })

    resolved = rp.resolve_runtime_provider(requested="kimi-coding")

    assert resolved["provider"] == "kimi-coding"
    assert resolved["api_mode"] == "anthropic_messages"
    assert resolved["base_url"] == KIMI_CODE_BARE


def test_non_pool_kimi_user_base_url_with_wire_path_not_double_appended(monkeypatch):
    """A user-explicit model.base_url that already carries the OpenAI-wire
    path must pass through unchanged (helper is idempotent on .../v1 —
    no /coding/v1/v1)."""
    _patch_kimi_non_pool(monkeypatch, {
        "provider": "kimi-coding",
        "api_mode": "chat_completions",
        "base_url": KIMI_CODE_OPENAI,
        "default": "kimi-k3",
    })

    resolved = rp.resolve_runtime_provider(requested="kimi-coding")

    assert resolved["api_mode"] == "chat_completions"
    assert resolved["base_url"] == KIMI_CODE_OPENAI


def test_non_pool_minimax_pinned_chat_completions_rebases_anthropic_suffix(monkeypatch):
    monkeypatch.setattr(rp, "resolve_provider", lambda *a, **k: "minimax")
    monkeypatch.setattr(rp, "_get_model_config", lambda: {
        "provider": "minimax",
        "api_mode": "chat_completions",
        "default": "MiniMax-M2.5",
    })
    monkeypatch.setattr(rp, "load_pool", lambda provider: _EmptyPool())
    monkeypatch.setattr(
        rp,
        "resolve_api_key_provider_credentials",
        lambda provider: {
            "provider": "minimax",
            "api_key": "minimax-nonpool-test-key",
            "base_url": "https://api.minimax.io/anthropic",
            "source": "env",
        },
    )
    monkeypatch.delenv("MINIMAX_BASE_URL", raising=False)

    resolved = rp.resolve_runtime_provider(requested="minimax")

    assert resolved["api_mode"] == "chat_completions"
    assert resolved["base_url"] == "https://api.minimax.io/v1"


def test_non_pool_unknown_wire_provider_unchanged(monkeypatch):
    """Acceptance 3 (non-pool path): no behavior change for providers
    without a known wire rewrite."""
    monkeypatch.setattr(rp, "resolve_provider", lambda *a, **k: "deepseek")
    monkeypatch.setattr(rp, "_get_model_config", lambda: {
        "provider": "deepseek",
        "api_mode": "chat_completions",
        "default": "deepseek-v4-pro",
    })
    monkeypatch.setattr(rp, "load_pool", lambda provider: _EmptyPool())
    monkeypatch.setattr(
        rp,
        "resolve_api_key_provider_credentials",
        lambda provider: {
            "provider": "deepseek",
            "api_key": "deepseek-nonpool-test-key",
            "base_url": "https://api.deepseek.com/v1",
            "source": "env",
        },
    )

    resolved = rp.resolve_runtime_provider(requested="deepseek")

    assert resolved["api_mode"] == "chat_completions"
    assert resolved["base_url"] == "https://api.deepseek.com/v1"


# ---------------------------------------------------------------------------
# Helper contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "api_mode, base_url, expected",
    [
        # OpenAI-wire modes trigger the known rewrites.
        ("chat_completions", KIMI_CODE_BARE, KIMI_CODE_OPENAI),
        ("codex_responses", KIMI_CODE_BARE, KIMI_CODE_OPENAI),
        ("chat_completions", "https://api.minimax.io/anthropic", "https://api.minimax.io/v1"),
        # Anthropic wire is never rewritten — bare /coding and /anthropic are
        # the correct URLs there.
        ("anthropic_messages", KIMI_CODE_BARE, KIMI_CODE_BARE),
        ("anthropic_messages", "https://api.minimax.io/anthropic", "https://api.minimax.io/anthropic"),
        # Identity: already-normalized and unknown-rewrite URLs pass through.
        ("chat_completions", KIMI_CODE_OPENAI, KIMI_CODE_OPENAI),
        ("chat_completions", "https://api.deepseek.com/v1", "https://api.deepseek.com/v1"),
        # Bare URL for a provider with no known rewrite stays byte-identical.
        ("chat_completions", "https://api.deepseek.com", "https://api.deepseek.com"),
        ("chat_completions", "", ""),
    ],
)
def test_normalize_openai_wire_base_url_contract(api_mode, base_url, expected):
    assert rp._normalize_openai_wire_base_url(api_mode, base_url) == expected
