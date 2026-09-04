"""Dispatcher-stuck telemetry must name the real reason a card did not spawn.

The pre-control-loop probe answered one boolean — "is there a ready+assigned
card whose assignee is a real profile?" — and the gateway turned a false
positive into a *credential* warning ("check venv, PATH, credentials").
Every nonspawnable-but-legitimate ready state (global capacity, per-profile
capacity, an active PR, a recent success, an invalid workspace, a
control-plane lane) produced that same wrong warning, which never cleared.

These tests pin the distinct reason codes and pin that only a genuinely
eligible, below-cap card past the grace window raises the alert.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_health as kh


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    conn = kb.connect()
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def real_profiles(monkeypatch):
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda name: name != "orion-cc")


def _reasons(report):
    return {e.task_id: e.reason_code for e in report.entries}


def test_eligible_ready_card_is_spawnable(board, real_profiles):
    tid = kb.create_task(board, title="do the thing", assignee="alice")
    report = kh.ready_queue_report(board)
    assert _reasons(report)[tid] == kh.READY_SPAWNABLE
    assert report.spawnable_ids == [tid]


def test_unassigned_ready_card_reports_unassigned_not_credentials(board, real_profiles):
    tid = kb.create_task(board, title="nobody owns this", assignee=None)
    report = kh.ready_queue_report(board)
    assert _reasons(report)[tid] == kh.READY_UNASSIGNED
    assert report.spawnable_ids == []


def test_registered_control_plane_lane_reports_its_own_reason(board, real_profiles, monkeypatch):
    monkeypatch.setattr(kh, "control_plane_assignees", lambda: frozenset({"orion-cc"}))
    tid = kb.create_task(board, title="human lane work", assignee="orion-cc")
    report = kh.ready_queue_report(board)
    assert _reasons(report)[tid] == kh.READY_CONTROL_PLANE_LANE
    assert report.spawnable_ids == []


def test_unknown_profile_is_invalid_not_a_healthy_human_lane(board, real_profiles):
    tid = kb.create_task(board, title="typo", assignee="orion-cc")
    report = kh.ready_queue_report(board)
    assert _reasons(report)[tid] == kh.READY_INVALID_EXECUTOR
    assert kh.board_health(board)["healthy"] is False


def test_default_assignee_census_matches_dispatch_eligibility(board, real_profiles):
    tid = kb.create_task(board, title="fallback", assignee=None)
    report = kh.ready_queue_report(board, default_assignee="alice")
    dispatched = kb.dispatch_once(
        board, dry_run=True, spawn_fn=lambda *_a, **_k: 123,
        default_assignee="alice",
    )
    assert _reasons(report)[tid] == kh.READY_SPAWNABLE
    assert report.spawnable_ids == [tid]
    assert [item[0] for item in dispatched.spawned] == [tid]


def test_default_assignee_does_not_make_unassigned_review_spawnable(
    board, real_profiles, monkeypatch
):
    monkeypatch.setattr(kb, "_memory_pressure_level", lambda: "unknown")
    tid = kb.create_task(board, title="unowned review", assignee=None)
    assert kb.request_review(board, tid, summary="review")
    monkeypatch.setattr(kb, "review_dispatch_enabled", lambda: True)

    report = kh.ready_queue_report(board, default_assignee="alice")
    dispatched = kb.dispatch_once(board, dry_run=True, default_assignee="alice")
    assert _reasons(report)[tid] == kh.READY_UNASSIGNED
    assert report.spawnable_ids == []
    assert dispatched.spawned == []
    assert dispatched.skipped_unassigned == [tid]


def test_ready_report_matches_separate_lane_order_and_review_reservation(
    board, real_profiles, monkeypatch
):
    monkeypatch.setattr(kb, "_memory_pressure_level", lambda: "unknown")
    ready_high = kb.create_task(board, title="ready high", assignee="alice", priority=30)
    ready_low = kb.create_task(board, title="ready low", assignee="bob", priority=20)
    review = kb.create_task(board, title="review low", assignee="carol", priority=1)
    assert kb.request_review(board, review, summary="review")
    monkeypatch.setattr(kb, "review_dispatch_enabled", lambda: True)

    report = kh.ready_queue_report(board, max_spawn=2, memory_pressure="unknown")
    dispatched = kb.dispatch_once(board, dry_run=True, max_spawn=2)
    expected = [item[0] for item in dispatched.spawned]
    assert expected == [ready_high, review]
    assert report.spawnable_ids == expected
    assert _reasons(report)[ready_low] == kh.READY_CAPACITY_MAX_SPAWN


def test_global_capacity_wait_is_a_capacity_reason(board, real_profiles):
    running = kb.create_task(board, title="already running", assignee="alice")
    kb.claim_task(board, running)
    waiting = kb.create_task(board, title="waiting for a slot", assignee="bob")

    report = kh.ready_queue_report(board, max_in_progress=1)
    assert _reasons(report)[waiting] == kh.READY_CAPACITY_GLOBAL
    assert report.spawnable_ids == []
    assert report.at_global_cap is True


def test_per_profile_capacity_wait_is_distinct_from_global(board, real_profiles):
    running = kb.create_task(board, title="alice busy", assignee="alice")
    kb.claim_task(board, running)
    waiting = kb.create_task(board, title="alice queued", assignee="alice")
    other = kb.create_task(board, title="bob free", assignee="bob")

    report = kh.ready_queue_report(board, max_in_progress_per_profile=1)
    reasons = _reasons(report)
    assert reasons[waiting] == kh.READY_CAPACITY_PER_PROFILE
    assert reasons[other] == kh.READY_SPAWNABLE
    assert report.spawnable_ids == [other]


def test_active_pr_guard_is_its_own_reason(board, real_profiles):
    tid = kb.create_task(board, title="pr already open", assignee="alice")
    kb.add_comment(board, tid, "worker", "opened https://github.com/o/r/pull/12")
    report = kh.ready_queue_report(board)
    assert _reasons(report)[tid] == kh.READY_GUARD_ACTIVE_PR
    assert report.spawnable_ids == []


def test_invalid_workspace_is_its_own_reason(board, real_profiles):
    tid = kb.create_task(
        board,
        title="broken workspace",
        assignee="alice",
        workspace_kind="dir",
        workspace_path="relative/not/absolute",
    )
    report = kh.ready_queue_report(board)
    assert _reasons(report)[tid] == kh.READY_INVALID_WORKSPACE


def test_claimed_card_is_not_counted_as_waiting_work(board, real_profiles):
    tid = kb.create_task(board, title="in flight", assignee="alice")
    kb.claim_task(board, tid)
    report = kh.ready_queue_report(board)
    assert tid not in _reasons(report)
    assert report.spawnable_ids == []


def test_review_queue_uses_the_same_spawnability_census(
    board, real_profiles, monkeypatch
):
    tid = kb.create_task(board, title="review me", assignee="alice")
    assert kb.request_review(board, tid, summary="ready for review")

    monkeypatch.setattr(kb, "review_dispatch_enabled", lambda: False)
    disabled = kh.ready_queue_report(board)
    assert _reasons(disabled)[tid] == kh.READY_REVIEW_DISABLED
    assert disabled.spawnable_ids == []

    monkeypatch.setattr(kb, "review_dispatch_enabled", lambda: True)
    enabled = kh.ready_queue_report(board)
    assert _reasons(enabled)[tid] == kh.READY_SPAWNABLE
    assert enabled.spawnable_ids == [tid]


def test_forward_path_reuses_spawnability_and_triage_is_not_magically_healthy(
    board, real_profiles
):
    broken = kb.create_task(
        board,
        title="bad workspace",
        assignee="alice",
        workspace_kind="dir",
        workspace_path="relative/path",
    )
    triage = kb.create_task(board, title="machine recovery", assignee="alice")
    assert kb.block_task(board, triage, kind="needs_input", reason="claim")

    health = kh.board_health(board)
    missing = {
        row["task_id"]: row["reason_code"] for row in health["no_forward_path"]
    }
    assert missing[broken] == kh.READY_INVALID_WORKSPACE
    assert missing[triage] == kh.REASON_AWAITING_TRIAGE


def test_max_spawn_cap_is_its_own_wait_reason(board, real_profiles):
    """``kanban.max_spawn`` is a live concurrency cap the dispatcher enforces
    before it looks at a single ready row. Omitting it from the census made
    ``dispatch_once`` correctly spawn nothing while telemetry called the same
    card spawnable — and the gateway paged about a dispatcher that was
    behaving exactly as configured."""
    running = kb.create_task(board, title="already running", assignee="alice")
    kb.claim_task(board, running)
    waiting = kb.create_task(board, title="waiting on the cap", assignee="bob")

    report = kh.ready_queue_report(board, max_spawn=1)
    assert _reasons(report)[waiting] == kh.READY_CAPACITY_MAX_SPAWN
    assert report.spawnable_ids == []
    assert report.at_spawn_cap is True

    assert kh.dispatcher_stuck_alert(
        [report], consecutive_idle_ticks=99, grace_ticks=3
    ) is None


def test_max_spawn_headroom_still_spawns(board, real_profiles):
    running = kb.create_task(board, title="already running", assignee="alice")
    kb.claim_task(board, running)
    waiting = kb.create_task(board, title="has headroom", assignee="bob")

    report = kh.ready_queue_report(board, max_spawn=3)
    assert _reasons(report)[waiting] == kh.READY_SPAWNABLE


def test_critical_memory_pressure_defers_every_card(board, real_profiles, monkeypatch):
    monkeypatch.setattr(kb, "_memory_pressure_level", lambda: "critical")
    tid = kb.create_task(board, title="would spawn", assignee="alice")

    report = kh.ready_queue_report(board)
    assert _reasons(report)[tid] == kh.READY_MEMORY_PRESSURE_CRITICAL
    assert report.spawnable_ids == []
    assert report.memory_pressure == "critical"


def test_elevated_memory_pressure_allows_exactly_one_worker(
    board, real_profiles, monkeypatch
):
    monkeypatch.setattr(kb, "_memory_pressure_level", lambda: "elevated")
    first = kb.create_task(board, title="first", assignee="alice", priority=5)
    second = kb.create_task(board, title="second", assignee="bob")

    report = kh.ready_queue_report(board)
    reasons = _reasons(report)
    assert reasons[first] == kh.READY_SPAWNABLE
    assert reasons[second] == kh.READY_MEMORY_PRESSURE_ELEVATED
    assert report.spawnable_ids == [first]


def test_gateway_passes_max_spawn_into_the_ready_census(monkeypatch, board):
    """The reason codes only help if the production caller supplies the
    production constraints."""
    from gateway import kanban_watchers

    seen = {}

    def _fake_report(conn, **kwargs):
        seen.update(kwargs)
        return kh.ReadyQueueReport(board=kwargs.get("board"))

    monkeypatch.setattr(kh, "ready_queue_report", _fake_report)
    monkeypatch.setattr(
        kanban_watchers, "_load_config",
        lambda: {"kanban": {"max_spawn": 2, "max_in_progress": 4,
                            "max_in_progress_per_profile": 1}},
        raising=False,
    )

    reports = kanban_watchers._ready_queue_reports_for_telemetry()
    assert reports
    assert seen["max_spawn"] == 2
    assert seen["max_in_progress_per_profile"] == 1
    assert seen["include_other_boards"] is True


# ---------------------------------------------------------------------------
# The alert decision itself
# ---------------------------------------------------------------------------


def test_capacity_and_guarded_queues_never_raise_dispatcher_stuck(board, real_profiles):
    running = kb.create_task(board, title="busy", assignee="alice")
    kb.claim_task(board, running)
    kb.create_task(board, title="capacity wait", assignee="bob")
    guarded = kb.create_task(board, title="guarded", assignee="bob")
    kb.add_comment(board, guarded, "worker", "https://github.com/o/r/pull/9")
    kb.create_task(board, title="lane work", assignee="orion-cc")

    report = kh.ready_queue_report(board, max_in_progress=1)
    alert = kh.dispatcher_stuck_alert(
        [report], consecutive_idle_ticks=99, grace_ticks=3
    )
    assert alert is None


def test_truly_eligible_below_cap_work_raises_dispatcher_stuck(board, real_profiles):
    tid = kb.create_task(board, title="should have spawned", assignee="alice")
    report = kh.ready_queue_report(board, max_in_progress=8)

    assert kh.dispatcher_stuck_alert([report], consecutive_idle_ticks=1, grace_ticks=3) is None

    alert = kh.dispatcher_stuck_alert(
        [report], consecutive_idle_ticks=3, grace_ticks=3
    )
    assert alert is not None
    assert tid in alert.task_ids
    assert alert.reason_code == kh.READY_SPAWNABLE


def test_stuck_alert_message_does_not_blame_credentials_for_capacity(board, real_profiles):
    running = kb.create_task(board, title="busy", assignee="alice")
    kb.claim_task(board, running)
    kb.create_task(board, title="waiting", assignee="alice")
    report = kh.ready_queue_report(board, max_in_progress=1)
    summary = kh.ready_queue_summary([report])
    assert summary["nonspawnable"][kh.READY_CAPACITY_GLOBAL] == 1
    assert summary["spawnable"] == 0


def test_one_queue_reports_several_distinct_reasons_at_once(
    board, real_profiles, monkeypatch
):
    """A real board mixes reasons. Each card must keep its own, rather than
    all of them collapsing into whatever the first check happened to be."""
    monkeypatch.setattr(
        kh, "control_plane_assignees", lambda: frozenset({"orion-cc"})
    )
    busy = kb.create_task(board, title="alice busy", assignee="alice")
    kb.claim_task(board, busy)

    capped = kb.create_task(board, title="alice queued", assignee="alice")
    guarded = kb.create_task(board, title="pr open", assignee="bob")
    kb.add_comment(board, guarded, "worker", "https://github.com/o/r/pull/3")
    lane = kb.create_task(board, title="human lane", assignee="orion-cc")
    unassigned = kb.create_task(board, title="unrouted", assignee=None)
    broken = kb.create_task(
        board, title="bad workspace", assignee="bob",
        workspace_kind="dir", workspace_path="not/absolute",
    )
    fine = kb.create_task(board, title="genuinely ready", assignee="carol")

    report = kh.ready_queue_report(board, max_in_progress_per_profile=1)
    reasons = _reasons(report)

    assert reasons[capped] == kh.READY_CAPACITY_PER_PROFILE
    assert reasons[guarded] == kh.READY_GUARD_ACTIVE_PR
    assert reasons[lane] == kh.READY_CONTROL_PLANE_LANE
    assert reasons[unassigned] == kh.READY_UNASSIGNED
    assert reasons[broken] == kh.READY_INVALID_WORKSPACE
    assert reasons[fine] == kh.READY_SPAWNABLE
    # Six cards, six different verdicts — not one bucket.
    assert len(set(reasons.values())) == 6
    assert report.spawnable_ids == [fine]


def test_no_alert_at_all_when_every_reason_is_a_legitimate_wait(
    board, real_profiles, monkeypatch
):
    """The regression that mattered most: these queues produced a
    'check venv, PATH, credentials' warning that never cleared."""
    monkeypatch.setattr(
        kh, "control_plane_assignees", lambda: frozenset({"orion-cc"})
    )
    busy = kb.create_task(board, title="busy", assignee="alice")
    kb.claim_task(board, busy)
    kb.create_task(board, title="capped", assignee="alice")
    guarded = kb.create_task(board, title="pr open", assignee="bob")
    kb.add_comment(board, guarded, "worker", "https://github.com/o/r/pull/3")
    kb.create_task(board, title="lane", assignee="orion-cc")
    kb.create_task(
        board, title="bad ws", assignee="bob",
        workspace_kind="dir", workspace_path="not/absolute",
    )

    report = kh.ready_queue_report(board, max_in_progress_per_profile=1)
    assert report.spawnable_ids == []
    for ticks in (3, 10, 500):
        assert kh.dispatcher_stuck_alert(
            [report], consecutive_idle_ticks=ticks, grace_ticks=3
        ) is None


def test_stuck_alert_names_credentials_only_for_the_genuinely_stuck_card(
    board, real_profiles, monkeypatch
):
    """When a card really is eligible and below cap, a broken profile IS the
    likely cause — so the credential hint belongs there, and only there. The
    other cards are reported by reason code as correctly waiting."""
    monkeypatch.setattr(
        kh, "control_plane_assignees", lambda: frozenset({"orion-cc"})
    )
    stuck = kb.create_task(board, title="should have spawned", assignee="carol")
    guarded = kb.create_task(board, title="pr open", assignee="bob")
    kb.add_comment(board, guarded, "worker", "https://github.com/o/r/pull/3")
    kb.create_task(board, title="lane", assignee="orion-cc")

    report = kh.ready_queue_report(board)
    alert = kh.dispatcher_stuck_alert(
        [report], consecutive_idle_ticks=6, grace_ticks=6
    )
    assert alert is not None
    assert alert.task_ids == [stuck]
    assert "credentials" in alert.message
    # The waiting cards are accounted for, not silently folded into the stall.
    assert kh.READY_GUARD_ACTIVE_PR in alert.message
    assert kh.READY_CONTROL_PLANE_LANE in alert.message
    assert guarded not in alert.task_ids


def test_alert_aggregates_across_boards(board, real_profiles, tmp_path):
    kb.create_board("second", name="Second")
    kb.create_task(board, title="board one", assignee="alice")
    conn2 = kb.connect(board="second")
    try:
        other = kb.create_task(conn2, title="board two", assignee="bob")
        reports = [
            kh.ready_queue_report(board, board="default"),
            kh.ready_queue_report(conn2, board="second"),
        ]
    finally:
        conn2.close()
    alert = kh.dispatcher_stuck_alert(
        reports, consecutive_idle_ticks=6, grace_ticks=6
    )
    assert alert is not None
    assert other in alert.task_ids
    assert len(alert.task_ids) == 2
