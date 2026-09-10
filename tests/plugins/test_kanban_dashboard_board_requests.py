"""How many board requests the dashboard may have in flight, and what it renders.

The board response is not cheap: on a real board it is megabytes and takes
roughly half a second. Every live event schedules a refresh 250ms later. So on
a board that is actually being worked -- the only kind anyone watches -- the
refreshes arrive faster than the responses do.

Under that load the page issued a fresh request each time and treated only the
newest one as valid, so each response was already superseded when it landed:
nothing was ever applied, ``loading`` was never cleared, and the tab sat on
"Loading Kanban board..." while the network showed a steady stream of
successful 200s. It came back only when the board went quiet. That is not slow,
it is starvation -- and it gets worse the busier the board is.

The contract these tests pin is therefore about scheduling, not about markup:

  * at most ONE request in flight per cohort -- the (board, tenant,
    include_archived) tuple a response is an answer to;
  * a refresh asked for while one is in flight is remembered and issued once,
    afterwards, instead of racing it;
  * a response that a cohort change has superseded, or that arrives out of
    order, is still discarded -- the protections in 72f79423 / 92dde13e are
    load-bearing and stay.

They drive the REAL bundle (``tests/plugins/kanban_bundle/driver.js``) on a
virtual clock, so "the response takes longer than the debounce" is exact.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BUNDLE = REPO_ROOT / "plugins" / "kanban" / "dashboard" / "dist" / "index.js"
DRIVER = Path(__file__).resolve().parent / "kanban_bundle" / "driver.js"

# A real board response measured against a real-size board (1,700 tasks, ~9MB)
# on the full-host harness; the refresh debounce in the bundle is 250ms.
BOARD_LATENCY_MS = 450

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required to run the dashboard bundle")


def run_scenario(tmp_path: Path, **cfg) -> dict:
    scenario = tmp_path / "scenario.json"
    cfg.setdefault("boardLatencyMs", BOARD_LATENCY_MS)
    scenario.write_text(json.dumps(cfg), encoding="utf-8")
    proc = subprocess.run(
        ["node", str(DRIVER), str(BUNDLE), str(scenario)],
        capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, f"driver failed: {proc.stdout}\n{proc.stderr}"
    report = json.loads(proc.stdout)
    assert not report["errors"], report["errors"]
    return report


def cohort(request: dict) -> tuple:
    """What a response is an answer to. Two requests differing here are not
    duplicates of each other -- the second one legitimately supersedes."""
    return (request["board"], request["tenant"], request["includeArchived"])


def concurrent_same_cohort(report: dict) -> list:
    """Pairs of same-cohort requests whose in-flight windows overlap."""
    overlaps = []
    for i, a in enumerate(report["boardRequests"]):
        for b in report["boardRequests"][i + 1:]:
            if cohort(a) != cohort(b):
                continue
            if b["t"] < a.get("respondedAt", float("inf")):
                overlaps.append((a["serial"], b["serial"], cohort(a)))
    return overlaps


def assert_never_applied_out_of_order(report: dict) -> None:
    """A response may only reach the screen if it is newer than what is there.

    The serial is the order requests were ISSUED in, so a serial going
    backwards means an older answer overwrote a newer one -- the exact stale
    write that 72f79423 exists to prevent.
    """
    serials = [a["serial"] for a in report["applied"]]
    assert serials == sorted(serials), f"stale response applied: {report['applied']}"


# ---------------------------------------------------------------------------
# Starvation: the regression these tests were written for
# ---------------------------------------------------------------------------


def test_a_board_under_a_live_event_stream_still_renders(tmp_path):
    """Events arriving faster than responses return must not stop the render.

    Every event schedules a refresh; each refresh used to start a request that
    invalidated the one already in flight. With responses slower than the
    debounce, no response was ever the newest, so the board never appeared --
    for as long as the board stayed busy.
    """
    report = run_scenario(tmp_path, runMs=12000, eventPeriodMs=300)

    assert report["finalState"] == "board", report["renderStates"]
    assert report["renderedAtMs"] is not None
    # Two round-trips is plenty: the mount request, then the one that names the
    # board the switcher resolved. Starvation showed up here as 12,700ms.
    assert report["renderedAtMs"] <= 2500, (
        f"board took {report['renderedAtMs']}ms to appear under a live event "
        f"stream; requests={len(report['boardRequests'])}")
    assert not concurrent_same_cohort(report)
    assert_never_applied_out_of_order(report)


def test_a_live_event_stream_never_stacks_up_board_requests(tmp_path):
    """A busy board must not turn into a pile of concurrent megabyte requests.

    Refreshes asked for while a request is in flight coalesce into one trailing
    refresh, so the request count follows the response time, not the event rate.
    """
    report = run_scenario(tmp_path, runMs=12000, eventPeriodMs=100)

    assert not concurrent_same_cohort(report)
    # Serial requests of BOARD_LATENCY_MS each, over runMs, plus a small margin
    # for the mount cohort and the trailing refresh after the last event.
    ceiling = 12000 // BOARD_LATENCY_MS + 6
    assert len(report["boardRequests"]) <= ceiling, (
        f"{len(report['boardRequests'])} board requests for a 12s window at "
        f"{BOARD_LATENCY_MS}ms each (events every 100ms)")
    assert report["finalState"] == "board"


def test_a_quiet_board_renders_from_a_bounded_number_of_requests(tmp_path):
    """Opening the tab must not fan out into overlapping board reads."""
    report = run_scenario(tmp_path, runMs=6000)

    assert report["finalState"] == "board"
    assert report["renderedAtMs"] <= 1500, report["renderStates"]
    assert not concurrent_same_cohort(report)
    # The mount request (before /boards has answered) and the one for the board
    # it resolved. Anything beyond that is churn.
    assert len(report["boardRequests"]) <= 3, report["boardRequests"]


# ---------------------------------------------------------------------------
# Freshness: the point of the event stream
# ---------------------------------------------------------------------------


def test_an_event_refreshes_the_board_without_a_reload(tmp_path):
    """One event after the board is up must produce one newer board on screen."""
    report = run_scenario(
        tmp_path, runMs=6000, eventPeriodMs=4000, eventStartMs=3900)

    assert report["finalState"] == "board"
    assert len(report["applied"]) >= 2, report["applied"]
    assert report["applied"][-1]["version"] > report["applied"][0]["version"], report["applied"]
    assert not concurrent_same_cohort(report)
    assert_never_applied_out_of_order(report)


def test_a_dropped_socket_reconnects_and_brings_the_board_up_to_date(tmp_path):
    """Recovery is event-backed: the reconnect's bootstrap frame reconciles."""
    report = run_scenario(
        tmp_path, runMs=9000,
        actions=[{"atMs": 2000, "kind": "closeSocket"},
                 # a write the page missed while the socket was down
                 {"atMs": 2100, "kind": "includeArchived", "value": False}])

    assert report["finalState"] == "board"
    # The socket for the selected board opened again after the drop.
    assert len(report["sockets"]) >= 3, report["sockets"]
    assert not concurrent_same_cohort(report)
    assert_never_applied_out_of_order(report)


# ---------------------------------------------------------------------------
# The protections that must survive: stale board, stale filter, stale callback
# ---------------------------------------------------------------------------


def test_a_delayed_response_for_the_previous_board_is_never_applied(tmp_path):
    """Switch away, then let the old board's slow answer land."""
    report = run_scenario(
        tmp_path, runMs=8000,
        actions=[{"atMs": 1000, "kind": "holdNextBoardResponse",
                  "board": "default", "latencyMs": 4000},
                 {"atMs": 1100, "kind": "switchBoard", "board": "beta"}])

    after_switch = [a for a in report["applied"] if a["t"] >= 1100]
    assert after_switch, report["applied"]
    assert all(a["board"] == "beta" for a in after_switch), report["applied"]
    assert_never_applied_out_of_order(report)


def test_an_out_of_order_response_for_the_same_board_is_never_applied(tmp_path):
    """The older answer for the SAME board must not restore what it superseded.

    A cohort change is what legitimately puts two requests in flight at once,
    so it is also the only way their answers can cross. Change the filter back
    and forth with the first answer held.
    """
    report = run_scenario(
        tmp_path, runMs=9000,
        actions=[{"atMs": 2000, "kind": "includeArchived", "value": True},
                 {"atMs": 2010, "kind": "holdNextBoardResponse",
                  "board": "default", "latencyMs": 4000},
                 {"atMs": 2100, "kind": "includeArchived", "value": False}])

    assert_never_applied_out_of_order(report)
    assert report["finalState"] == "board"


def test_a_stale_callback_does_not_load_the_board_the_user_left(tmp_path):
    """A mutation's ``.then(loadBoard)`` from a previous render (92dde13e)."""
    report = run_scenario(
        tmp_path, runMs=8000,
        actions=[{"atMs": 1500, "kind": "switchBoard", "board": "beta"},
                 {"atMs": 2500, "kind": "staleCallback"}])

    leaked = [r for r in report["boardRequests"]
              if r["t"] >= 2500 and r["board"] != "beta"]
    assert not leaked, leaked
    assert_never_applied_out_of_order(report)


def test_a_filter_change_supersedes_the_request_in_flight(tmp_path):
    """The answer to the old filter must not paint over the new one."""
    report = run_scenario(
        tmp_path, runMs=8000,
        actions=[{"atMs": 1500, "kind": "includeArchived", "value": True}])

    assert report["finalState"] == "board"
    archived_requests = [r for r in report["boardRequests"] if r["includeArchived"]]
    assert archived_requests, report["boardRequests"]
    assert not concurrent_same_cohort(report)
    assert_never_applied_out_of_order(report)
