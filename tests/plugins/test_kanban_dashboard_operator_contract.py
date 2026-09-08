"""The SHIPPED dashboard bundle must agree with the operator projection.

``plugins/kanban/dashboard/dist/index.js`` has no build step — it is a
hand-maintained plain IIFE loaded straight from ``manifest.json`` (``"entry":
"dist/index.js"``). There is no source that compiles into it, so the artifact
IS the source and asserting on its text is asserting on shipped behaviour.

The backend now projects every card into an operator lane with an owner and a
next action (``kanban_health.project_operator_state``, served as
``task.operator`` and ``dependencies`` by the dashboard API). A bundle that
still renders raw ``triage``/``review`` columns, bare dependency ids, and
Specify/Decompose buttons gated on ``status === "triage"`` contradicts that
projection on the one surface Kevin actually looks at — which is the whole
complaint the projection exists to answer.

These are text-level assertions because that is the only kind this artifact
supports; ``node --check`` (the convention documented for the sibling
achievements plugin) guards syntax.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
DASHBOARD = REPO_ROOT / "plugins" / "kanban" / "dashboard"
BUNDLE = DASHBOARD / "dist" / "index.js"


@pytest.fixture(scope="module")
def source() -> str:
    return BUNDLE.read_text(encoding="utf-8")


def test_the_bundle_is_the_entry_point_the_manifest_ships(source):
    """Guards the premise: this file is what the host actually loads."""
    manifest = json.loads((DASHBOARD / "manifest.json").read_text())
    assert manifest["entry"] == "dist/index.js"
    assert source.strip()


def test_the_bundle_still_parses(source):
    """No build step means no compiler caught a typo on the way in."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available on this host")
    result = subprocess.run(
        [node, "--check", str(BUNDLE)], capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# Columns
# ---------------------------------------------------------------------------


def test_triage_and_review_are_not_shipped_as_columns(source):
    order = re.search(r"const COLUMN_ORDER = (\[[^\]]*\]);", source)
    assert order, "COLUMN_ORDER not found"
    columns = json.loads(order.group(1))
    assert "triage" not in columns
    assert "review" not in columns
    # Matches kanban_health.OPERATOR_COLUMNS, which the API also uses to build
    # the column list — a bundle ordering lanes the API never sends renders
    # empty phantom columns.
    from hermes_cli import kanban_health as kh
    assert columns == list(kh.OPERATOR_COLUMNS)


def test_no_user_facing_triage_or_review_column_copy(source):
    for block in ("FALLBACK_COLUMN_LABEL", "FALLBACK_COLUMN_HELP", "COLUMN_DOT"):
        body = re.search(block + r" = \{(.*?)\n  \};", source, re.S)
        assert body, f"{block} not found"
        assert not re.search(r"^\s*triage:", body.group(1), re.M), block
        assert not re.search(r"^\s*review:", body.group(1), re.M), block


# ---------------------------------------------------------------------------
# The operator projection is what the card renders
# ---------------------------------------------------------------------------


def test_the_card_reads_the_operator_projection(source):
    """Lane, owner and next action come from the backend, not from `status`."""
    assert "operatorOf(" in source, (
        "the bundle has no accessor for task.operator"
    )
    assert "function operatorOf(" in source
    assert "lifecycle_status" in source, (
        "the durable status must still be reachable as secondary metadata"
    )


def test_the_lane_is_the_projection_not_the_raw_status(source):
    """Column bucketing must not fall back to the raw lifecycle status."""
    assert "operatorLane(" in source
    assert "function operatorLane(" in source


def test_the_machine_owner_and_next_action_are_rendered(source):
    """Blocked must distinguish machine recovery from Needs Kevin."""
    assert "next_owner" in source
    assert re.search(r"\.owner\b", source), "operator.owner is never read"
    assert "OWNER_LABEL" in source


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def test_dependencies_render_titles_and_state_not_bare_ids(source):
    editor = re.search(
        r"function DependencyEditor\(props\) \{(.*?)\n  \}\n", source, re.S,
    )
    assert editor, "DependencyEditor not found"
    body = editor.group(1)
    assert "dependencies" in body
    # The titled/stateful entries the API now serves.
    assert "depEntries" in body or "entry.title" in body, (
        "the dependency editor still renders bare ids"
    )
    assert "entry.label" in body or ".label" in body


def test_the_detail_view_requests_the_dependency_payload(source):
    """The backend serves `dependencies`; an unread payload is not a fix."""
    assert re.search(r"\bdependencies\b", source)
    assert "deps.parents" in source or "dependencies.parents" in source


# ---------------------------------------------------------------------------
# Status controls
# ---------------------------------------------------------------------------


def test_no_triage_or_review_status_control_is_offered(source):
    actions = re.search(
        r"function StatusActions\(props\) \{(.*?)\n  \}\n", source, re.S,
    )
    assert actions, "StatusActions not found"
    # Comments explaining WHY a control is absent legitimately name it; only
    # live code can actually render a button.
    body = "\n".join(
        line for line in actions.group(1).splitlines()
        if not line.strip().startswith("//")
    )
    assert 'status: "triage"' not in body, (
        "the operator can still push a card into the internal triage lane"
    )
    assert 'status: "review"' not in body
    assert "→ triage" not in body


def test_specify_and_decompose_are_not_operator_actions(source):
    """Triage recovery is classification the machine owns, not a button.

    Running the LLM specifier over a stalled card is exactly the
    "blindly rewrite a long card through the specifier's truncated input"
    failure the recovery lane forbids, and offering it in the drawer makes
    Triage an operator concept again.
    """
    actions = re.search(
        r"function StatusActions\(props\) \{(.*?)\n  \}\n", source, re.S,
    )
    assert actions, "StatusActions not found"
    body = "\n".join(
        line for line in actions.group(1).splitlines()
        if not line.strip().startswith("//")
    )
    assert "specifyButton" not in body
    assert "decomposeButton" not in body
    assert "onSpecify" not in body
    assert "onDecompose" not in body


def test_the_bundle_never_reads_the_session_token(source):
    """Pre-existing contract, restated here so an edit cannot regress it."""
    for line in source.splitlines():
        if "__HERMES_SESSION_TOKEN__" in line:
            assert line.strip().startswith("//"), line
