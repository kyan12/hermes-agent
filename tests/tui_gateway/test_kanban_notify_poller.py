"""Tests for the TUI-side kanban notification poller (issue #59890).

``kanban_create`` auto-subscribes TUI/desktop sessions with
``platform="tui"`` / ``chat_id=HERMES_SESSION_KEY``, but no component ever
read those rows back: the gateway notifier skips them (no "tui" messaging
adapter) and the TUI notification poller only watched process completions.
``last_event_id`` stayed 0 forever and no notification was ever delivered.

These tests cover the delivery half that now lives in tui_gateway/server.py:
``_collect_kanban_notifications`` (cursor claim + formatting + archive-only
unsubscribe) and ``_format_kanban_event_text``.
"""

from types import SimpleNamespace
from unittest.mock import patch

from hermes_cli import kanban_db as kb
from tui_gateway.server import (
    _collect_kanban_notifications,
    _format_kanban_event_text,
)

SESSION_KEY = "tui-session-key-1"


def _session(key: str = SESSION_KEY) -> dict:
    return {"session_key": key}


def _create_subscribed_task(*, chat_id: str = SESSION_KEY, platform: str = "tui"):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="notify tui", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform=platform, chat_id=chat_id)
        return tid
    finally:
        conn.close()


def _complete(tid: str, summary: str = "all done") -> None:
    conn = kb.connect()
    try:
        kb.complete_task(conn, tid, summary=summary)
    finally:
        conn.close()


def _sub_rows(tid: str) -> list:
    conn = kb.connect()
    try:
        return kb.list_notify_subs(conn, task_id=tid)
    finally:
        conn.close()


class TestCollectKanbanNotifications:
    def test_zero_sub_board_is_never_opened_writable(self):
        conn = kb.connect()
        conn.close()
        kb.create_board("second-board")

        with patch.object(kb, "connect", wraps=kb.connect) as spy_connect:
            texts = _collect_kanban_notifications(_session())

        assert texts == []
        spy_connect.assert_not_called()

    def test_done_reopen_notifies_once_per_event_until_archive(self):
        tid = _create_subscribed_task()
        _complete(tid, summary="shipped the fix")

        first = _collect_kanban_notifications(_session())

        assert len(first) == 1
        assert tid in first[0]
        assert "done" in first[0]
        assert "shipped the fix" in first[0]
        rows = _sub_rows(tid)
        assert len(rows) == 1, "done must retain the originating session"
        first_cursor = rows[0]["last_event_id"]

        # The retained subscription must not replay the completed event.
        assert _collect_kanban_notifications(_session()) == []

        conn = kb.connect()
        try:
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,)
                )
                kb._append_event(conn, tid, "status", {"status": "ready"})
            assert kb.complete_task(conn, tid, summary="review corrections")
        finally:
            conn.close()

        reopened = _collect_kanban_notifications(_session())

        assert len(reopened) == 2
        assert "ready" in reopened[0]
        assert "review corrections" in reopened[1]
        rows = _sub_rows(tid)
        assert len(rows) == 1
        assert rows[0]["chat_id"] == SESSION_KEY
        assert rows[0]["last_event_id"] > first_cursor
        assert _collect_kanban_notifications(_session()) == []

        conn = kb.connect()
        try:
            assert kb.archive_task(conn, tid)
        finally:
            conn.close()

        # Archive is notification-terminal and removes the retained route.
        assert _collect_kanban_notifications(_session()) == []
        assert _sub_rows(tid) == []

    def test_matching_tui_sub_delivers_and_advances_cursor(self):
        tid = _create_subscribed_task()
        pre_cursor = _sub_rows(tid)[0]["last_event_id"]
        conn = kb.connect()
        try:
            kb.block_task(conn, tid, reason="waiting on review")
        finally:
            conn.close()

        with patch.object(kb, "connect", wraps=kb.connect) as spy_connect:
            first = _collect_kanban_notifications(_session())
            second = _collect_kanban_notifications(_session())

        assert len(first) == 1
        assert "blocked" in first[0]
        assert "waiting on review" in first[0]
        assert second == []
        assert spy_connect.called
        # Blocked is not a final status -> subscription stays alive so a
        # respawned task's next terminal event still reaches the user.
        rows = _sub_rows(tid)
        assert len(rows) == 1
        assert rows[0]["last_event_id"] > pre_cursor

    def test_non_tui_subscription_does_not_open_board_writable(self):
        tid = _create_subscribed_task(platform="telegram", chat_id="chat-1")
        # New subs start caught up at creation time (issue #29905); record the
        # pre-completion cursors so we can assert they were never claimed.
        pre_cursor = _sub_rows(tid)[0]["last_event_id"]
        _complete(tid)

        with patch.object(kb, "connect", wraps=kb.connect) as spy_connect:
            texts = _collect_kanban_notifications(_session())

        assert texts == []
        spy_connect.assert_not_called()
        rows = _sub_rows(tid)
        assert len(rows) == 1
        assert rows[0]["last_event_id"] == pre_cursor

    def test_other_tui_session_does_not_open_board_writable(self):
        tid = _create_subscribed_task(chat_id="some-other-session")
        pre_cursor = _sub_rows(tid)[0]["last_event_id"]
        _complete(tid)

        with patch.object(kb, "connect", wraps=kb.connect) as spy_connect:
            texts = _collect_kanban_notifications(_session())

        assert texts == []
        spy_connect.assert_not_called()
        rows = _sub_rows(tid)
        assert len(rows) == 1
        assert rows[0]["last_event_id"] == pre_cursor

    def test_probe_error_falls_back_to_writable_delivery(self, monkeypatch):
        tid = _create_subscribed_task()
        _complete(tid, summary="fallback delivery")

        def fail_probe(*args, **kwargs):
            raise OSError("probe unavailable")

        monkeypatch.setattr(kb, "count_notify_subs", fail_probe)
        with patch.object(kb, "connect", wraps=kb.connect) as spy_connect:
            texts = _collect_kanban_notifications(_session())

        assert len(texts) == 1
        assert tid in texts[0]
        spy_connect.assert_called_once()

    def test_no_session_key_is_a_noop(self):
        tid = _create_subscribed_task()
        _complete(tid)

        assert _collect_kanban_notifications({"session_key": ""}) == []
        assert _collect_kanban_notifications({"session_key": None}) == []
        assert len(_sub_rows(tid)) == 1

    def test_profile_scoped_session_reads_the_shared_board(self, tmp_path):
        """The kanban board is shared across profiles BY DESIGN (see the
        hermes_cli/kanban_db.py module docstring): ``kanban_home()`` anchors on
        ``get_default_hermes_root()``, which resolves the process env and
        ignores context-local profile overrides. A Desktop session bound to a
        non-launch profile (``session["profile_home"]``) must therefore still
        have its subscription claimed from the one shared board — the poller
        needs no per-profile home binding.
        """
        from hermes_constants import (
            reset_hermes_home_override,
            set_hermes_home_override,
        )

        tid = _create_subscribed_task()
        _complete(tid, summary="cross-profile delivery")

        other_profile_home = tmp_path / "profiles" / "reviewer"
        other_profile_home.mkdir(parents=True)
        session = {
            "session_key": SESSION_KEY,
            "profile_home": str(other_profile_home),
        }
        # Simulate the strictest case: a context-local profile override is
        # active while the poller collects (as a profile-bound RPC would set).
        token = set_hermes_home_override(str(other_profile_home))
        try:
            texts = _collect_kanban_notifications(session)
        finally:
            reset_hermes_home_override(token)

        assert len(texts) == 1
        assert tid in texts[0]
        assert "cross-profile delivery" in texts[0]
        # Completion is reversible, so the shared-board subscription remains
        # owned by this exact Desktop session until the task is archived.
        rows = _sub_rows(tid)
        assert len(rows) == 1
        assert rows[0]["chat_id"] == SESSION_KEY


class TestFormatKanbanEventText:
    SUB = {"task_id": "t_abc123"}
    TASK = SimpleNamespace(title="build the thing", assignee="worker", result=None)

    def test_silent_kinds_return_none(self):
        for kind in ("archived", "unblocked"):
            ev = SimpleNamespace(kind=kind, payload={})
            assert _format_kanban_event_text(self.SUB, self.TASK, ev, "main") is None

    def test_blocked_includes_reason(self):
        ev = SimpleNamespace(kind="blocked", payload={"reason": "needs creds"})
        text = _format_kanban_event_text(self.SUB, self.TASK, ev, "main")
        assert "t_abc123" in text
        assert "blocked" in text
        assert "needs creds" in text
        assert "[main]" in text
        assert "@worker" in text

    def test_completed_prefers_payload_summary(self):
        ev = SimpleNamespace(kind="completed", payload={"summary": "first line\nsecond"})
        text = _format_kanban_event_text(self.SUB, self.TASK, ev, "")
        assert "done" in text
        assert "first line" in text
        assert "second" not in text

    def test_timed_out_with_bad_payload_does_not_raise(self):
        ev = SimpleNamespace(kind="timed_out", payload={"limit_seconds": "not-a-number"})
        text = _format_kanban_event_text(self.SUB, self.TASK, ev, "")
        assert "timed out" in text


class TestNotificationPollerLoopKanbanWiring:
    """Drive a real TUI subscription through ``_notification_poller_loop``.

    Covers the wiring above ``_collect_kanban_notifications``: status.update
    emission, agent-turn dispatch when the session is idle, and the
    busy-session pending buffer that flushes once the session goes idle.
    """

    def _start_poller(self, session: dict, monkeypatch):
        import threading
        import tui_gateway.server as server

        emits: list = []
        submits: list = []
        monkeypatch.setattr(server, "_KANBAN_POLL_SECONDS", 0.01)
        monkeypatch.setattr(
            server, "_emit", lambda event, sid, payload=None: emits.append((event, payload))
        )
        monkeypatch.setattr(
            server,
            "_run_prompt_submit",
            lambda rid, sid, sess, text: submits.append(text),
        )
        stop = threading.Event()
        thread = threading.Thread(
            target=server._notification_poller_loop,
            args=(stop, "sid-poller-test", session),
            daemon=True,
        )
        thread.start()
        return stop, thread, emits, submits

    @staticmethod
    def _wait_for(predicate, timeout: float = 5.0) -> bool:
        import time as _time

        deadline = _time.monotonic() + timeout
        while _time.monotonic() < deadline:
            if predicate():
                return True
            _time.sleep(0.02)
        return False

    def _poller_session(self, *, running: bool = False) -> dict:
        import threading

        return {
            "session_key": SESSION_KEY,
            "history_lock": threading.Lock(),
            "running": running,
        }

    def test_idle_session_gets_status_update_and_agent_turn(self, monkeypatch):
        tid = _create_subscribed_task()
        _complete(tid, summary="poller e2e done")
        session = self._poller_session(running=False)

        stop, thread, emits, submits = self._start_poller(session, monkeypatch)
        try:
            assert self._wait_for(lambda: submits), "agent turn was never dispatched"
        finally:
            stop.set()
            thread.join(timeout=5)

        status_texts = [p["text"] for e, p in emits if e == "status.update" and p]
        assert any(tid in t for t in status_texts), status_texts
        assert any(e == "message.start" for e, _ in emits)
        assert any(tid in text for text in submits), submits
        assert session["running"] is True  # poller claimed the turn
        assert not session.get("_kanban_pending")

    def test_busy_session_buffers_then_flushes_when_idle(self, monkeypatch):
        tid = _create_subscribed_task()
        _complete(tid, summary="buffered while busy")
        session = self._poller_session(running=True)

        stop, thread, emits, submits = self._start_poller(session, monkeypatch)
        try:
            # Busy: the status line appears and the event is buffered, but no
            # agent turn is dispatched while another turn is running.
            assert self._wait_for(
                lambda: any(e == "status.update" for e, _ in emits)
                and session.get("_kanban_pending")
            )
            assert not submits

            with session["history_lock"]:
                session["running"] = False

            assert self._wait_for(lambda: submits), "pending batch never flushed"
        finally:
            stop.set()
            thread.join(timeout=5)

        assert any(tid in text for text in submits), submits
        assert session["_kanban_pending"] == []
        assert session["running"] is True


# ---------------------------------------------------------------------------
# A machine stop is visible, and it is visibly the MACHINE's problem
# ---------------------------------------------------------------------------
#
# ``block_task`` routes an untyped / transient / unaffirmed machine stop to the
# internal recovery lane and emits ``automation_recovery_requested`` — no
# ``blocked`` event at all. Neither notifier claimed that kind, so the single
# most important thing a subscriber can be told ("your task stopped") produced
# exactly zero notifications, on Telegram and in the TUI alike.
#
# Making it visible must not overcorrect the other way: an automation failure
# is not an approval request. The message names the machine as the owner and
# never asks a person to classify or route the work.


def _machine_stop(tid: str, reason: str = "provider timed out", kind=None):
    conn = kb.connect()
    try:
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        assert kb.block_task(conn, tid, reason=reason, kind=kind)
    finally:
        conn.close()


def _events(tid: str, kind: str):
    conn = kb.connect()
    try:
        return [e for e in kb.list_events(conn, tid) if e.kind == kind]
    finally:
        conn.close()


def _task(tid: str):
    conn = kb.connect()
    try:
        return kb.get_task(conn, tid)
    finally:
        conn.close()


class TestMachineStopNotifications:
    def test_a_machine_stop_notifies_at_all(self):
        tid = _create_subscribed_task()
        pre_cursor = _sub_rows(tid)[0]["last_event_id"]
        _machine_stop(tid, reason="provider quota exhausted")

        texts = _collect_kanban_notifications(_session())

        assert texts, "a machine stop produced no notification whatsoever"
        assert any("provider quota exhausted" in t for t in texts)
        assert _sub_rows(tid)[0]["last_event_id"] > pre_cursor

    def test_a_machine_stop_reads_as_blocked_and_machine_owned(self):
        tid = _create_subscribed_task()
        _machine_stop(tid, reason="provider quota exhausted")

        text = "\n".join(_collect_kanban_notifications(_session()))

        assert "blocked" in text.lower()
        # Never an invented approval request, and never the internal lane name.
        assert "triage" not in text.lower()
        assert "human decision" not in text.lower()
        assert "your input" not in text.lower()
        assert "automatic recovery" in text.lower() or "recovery" in text.lower()

    def test_the_same_wording_is_used_in_the_tui_and_the_gateway(self):
        """One vocabulary. A card must not read differently per surface."""
        from gateway.kanban_watchers import format_kanban_notification

        tid = _create_subscribed_task()
        _machine_stop(tid, reason="provider quota exhausted")
        ev = _events(tid, "automation_recovery_requested")[-1]
        sub = _sub_rows(tid)[0]

        tui = _format_kanban_event_text(sub, _task(tid), ev, "default")
        gateway = format_kanban_notification(sub, _task(tid), ev, "default")
        assert tui == gateway

    def test_a_repeated_block_loop_is_not_an_invented_kevin_gate(self):
        """``block_loop_detected`` used to say "routed to TRIAGE — needs a
        human decision". Repeated machine failure is still machine failure."""
        from gateway.kanban_watchers import format_kanban_notification

        tid = _create_subscribed_task()
        for _ in range(6):
            conn = kb.connect()
            try:
                if kb.get_task(conn, tid).status != "ready":
                    conn.execute(
                        "UPDATE tasks SET status='ready' WHERE id=?", (tid,)
                    )
                    conn.commit()
            finally:
                conn.close()
            _machine_stop(tid, reason="same cause again", kind="transient")
        loops = _events(tid, "block_loop_detected")
        assert loops, "the recurrence limit never tripped"

        text = format_kanban_notification(
            _sub_rows(tid)[0], _task(tid), loops[-1], "default",
        )
        assert text
        assert "triage" not in text.lower()
        assert "needs a human decision" not in text.lower()
        assert "blocked" in text.lower()

    def test_an_affirmed_human_gate_still_asks_for_kevin(self):
        """Removing the invented gate must not silence the genuine one."""
        from gateway.kanban_watchers import format_kanban_notification
        from hermes_cli import kanban_health as kh
        import time as _time

        tid = _create_subscribed_task()
        _machine_stop(tid, reason="which envelope ships?", kind="needs_input")
        conn = kb.connect()
        try:
            assert kh.affirm_human_gate(conn, tid, evidence={
                "type": "human_decision",
                "action": "Confirm the approved subject line",
                "affirmed_by": "Kevin Yan",
                "affirmed_at": int(_time.time()),
            }, reason="which envelope ships?")
        finally:
            conn.close()
        gate = [
            e for e in _events(tid, "blocked") if (e.payload or {}).get("affirmed")
        ]
        assert gate

        text = format_kanban_notification(
            _sub_rows(tid)[0], _task(tid), gate[-1], "default",
        )
        assert text and "blocked" in text.lower()


# ---------------------------------------------------------------------------
# A gate the operator has since taken supersedes the machine's own play-by-play
# ---------------------------------------------------------------------------
#
# A repeated block emits `automation_recovery_requested` and, past the
# recurrence limit, `block_loop_detected`. If an operator affirms a human gate
# on that same occurrence before the notifier's next poll, all three are still
# sitting unclaimed — and the user receives, in order, two messages promising
# that automatic recovery will handle it, then one saying it is blocked on
# them. The first two are false by the time they are sent: nothing is going to
# pick this card up, because the operator took it.
#
# Every machine-recovery intermediary superseded by an affirmed gate for the
# same occurrence is suppressed. The gate itself is still delivered.


def _repeat_block_to_loop(tid: str, reason: str = "needs credentials"):
    conn = kb.connect()
    try:
        for _ in range(kb.BLOCK_RECURRENCE_LIMIT + 1):
            task = kb.get_task(conn, tid)
            if task.status != "ready":
                conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
                conn.commit()
            assert kb.claim_task(conn, tid, claimer="worker") is not None
            assert kb.block_task(conn, tid, reason=reason, kind="needs_input")
    finally:
        conn.close()


def _affirm_now(tid: str, action: str = "Confirm the approved subject line"):
    from hermes_cli import kanban_health as kh
    import time as _time

    conn = kb.connect()
    try:
        assert kh.affirm_human_gate(conn, tid, evidence={
            "type": "human_decision",
            "action": action,
            "affirmed_by": "Kevin Yan",
            "affirmed_at": int(_time.time()),
        }, reason="needs a decision")
    finally:
        conn.close()


class TestGateSupersedesMachineChatter:
    def test_no_superseded_machine_message_reaches_the_user(self):
        tid = _create_subscribed_task()
        _repeat_block_to_loop(tid)
        assert _events(tid, "block_loop_detected"), "the loop never tripped"
        _affirm_now(tid)

        texts = _collect_kanban_notifications(_session())

        assert texts, "the affirmed gate itself must still be delivered"
        joined = "\n".join(texts).lower()
        assert "automatic recovery" not in joined, (
            "the user was promised automatic recovery on a card an operator "
            f"has already taken: {texts}"
        )
        assert "no action needed from you" not in joined

    def test_the_affirmed_gate_is_the_message_that_survives(self):
        tid = _create_subscribed_task()
        _repeat_block_to_loop(tid)
        _affirm_now(tid, action="Confirm the approved subject line")

        texts = _collect_kanban_notifications(_session())

        assert len(texts) == 1, f"expected only the gate, got {texts}"
        assert "blocked" in texts[0].lower()
        # The gate's own reason, not the machine's last excuse.
        assert "needs a decision" in texts[0]

    def test_the_cursor_still_advances_past_suppressed_events(self):
        """Suppressed is not unclaimed: an unclaimed row wedges later events."""
        tid = _create_subscribed_task()
        pre_cursor = _sub_rows(tid)[0]["last_event_id"]
        _repeat_block_to_loop(tid)
        _affirm_now(tid)

        _collect_kanban_notifications(_session())

        assert _sub_rows(tid)[0]["last_event_id"] > pre_cursor
        assert _collect_kanban_notifications(_session()) == []

    def test_an_unsuperseded_machine_stop_is_still_announced(self):
        """Suppression is scoped to a gate the operator actually holds."""
        tid = _create_subscribed_task()
        _repeat_block_to_loop(tid, reason="provider quota exhausted")

        texts = _collect_kanban_notifications(_session())

        assert texts, "a machine stop with no gate must still be announced"
        assert any("provider quota exhausted" in t for t in texts)

    def test_a_released_gate_does_not_suppress_a_later_machine_stop(self):
        """Once the operator hands the card back, the machine speaks again."""
        tid = _create_subscribed_task()
        _repeat_block_to_loop(tid)
        _affirm_now(tid)
        _collect_kanban_notifications(_session())
        conn = kb.connect()
        try:
            assert kb.unblock_task(conn, tid)
        finally:
            conn.close()
        _repeat_block_to_loop(tid, reason="provider quota exhausted")

        texts = _collect_kanban_notifications(_session())

        assert any("provider quota exhausted" in t for t in texts)


# ---------------------------------------------------------------------------
# `gave_up` is a machine stop like any other, and it says what actually failed
# ---------------------------------------------------------------------------
#
# `gave_up` is the circuit breaker tripping. It is emitted for EVERY trigger
# outcome — timed_out, crashed, protocol_violation, rate_limited, spawn_failed
# — but was announced verbatim as "gave up after repeated spawn failures". For
# a task that timed out six times, that message names the wrong failure, and an
# operator who goes looking for spawn errors finds none.
#
# It is also emitted in the SAME transaction that mints the recovery owner, so
# a message that stops at "gave up" omits the one fact that matters: automation
# already owns the next attempt. And like every other machine-stop kind it must
# fall silent when an operator has affirmed a gate on the same occurrence.


def _trip_breaker(tid: str, *, outcome: str = "timed_out", error: str = "elapsed 600s"):
    conn = kb.connect()
    try:
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        assert kb._record_task_failure(
            conn, tid, error=error, outcome=outcome,
            force_trip=True, release_claim=True, end_run=True,
        ) is True
    finally:
        conn.close()


class TestGaveUpIsAnHonestMachineStop:
    def test_the_message_does_not_invent_spawn_failures(self):
        tid = _create_subscribed_task()
        _trip_breaker(tid, outcome="timed_out", error="elapsed 600s > limit 300s")

        texts = _collect_kanban_notifications(_session())

        assert texts, "a circuit-breaker trip produced no notification"
        joined = "\n".join(texts)
        assert "spawn" not in joined.lower(), (
            f"a timeout was reported as repeated spawn failures: {texts}"
        )
        assert "timed_out" in joined or "timed out" in joined.lower()

    def test_a_protocol_violation_is_named_as_itself(self):
        tid = _create_subscribed_task()
        _trip_breaker(tid, outcome="protocol_violation", error="no terminal tool")

        joined = "\n".join(_collect_kanban_notifications(_session()))

        assert "protocol_violation" in joined or "protocol violation" in joined
        assert "spawn" not in joined.lower()

    def test_a_real_spawn_failure_still_says_spawn(self):
        tid = _create_subscribed_task()
        _trip_breaker(tid, outcome="spawn_failed", error="no such profile")

        joined = "\n".join(_collect_kanban_notifications(_session()))

        assert "spawn_failed" in joined or "spawn" in joined.lower()

    def test_the_recovery_owner_minted_in_the_same_txn_is_reported(self):
        tid = _create_subscribed_task()
        _trip_breaker(tid)
        conn = kb.connect()
        try:
            owner_id = kb.active_recovery_owner(conn, tid)
        finally:
            conn.close()

        joined = "\n".join(_collect_kanban_notifications(_session()))

        assert "recovery" in joined.lower(), (
            "the breaker tripped and automation already owns the retry, but "
            f"the message says nothing about it: {joined}"
        )
        if owner_id:
            assert owner_id in joined or "no action needed" in joined.lower()

    def test_a_gate_affirmed_before_the_poll_suppresses_gave_up(self):
        tid = _create_subscribed_task()
        _trip_breaker(tid)
        conn = kb.connect()
        try:
            # Release the machine stop, then take the card as a real gate.
            assert kb.unblock_task(conn, tid)
            assert kb.claim_task(conn, tid, claimer="worker") is not None
            assert kb.block_task(conn, tid, reason="which envelope ships?",
                                 kind="needs_input")
        finally:
            conn.close()
        _affirm_now(tid)

        joined = "\n".join(_collect_kanban_notifications(_session())).lower()

        assert "gave up" not in joined, joined
        assert "automatic recovery" not in joined, joined

    def test_the_gateway_and_the_tui_still_say_the_same_thing(self):
        from gateway.kanban_watchers import format_kanban_notification

        tid = _create_subscribed_task()
        _trip_breaker(tid)
        ev = _events(tid, "gave_up")[-1]
        sub = _sub_rows(tid)[0]

        assert (
            _format_kanban_event_text(sub, _task(tid), ev, "default")
            == format_kanban_notification(sub, _task(tid), ev, "default")
        )
