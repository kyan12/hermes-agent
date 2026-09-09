"""The dashboard's per-request connection helper must not re-run DB init.

``plugin_api._conn`` is on the hot path of every ``/api/plugins/kanban/*``
request. Re-running :func:`kanban_db.init_db` there evicts the board from
``kanban_db_connect._INITIALIZED_PATHS``, which forces every request back
through the full init path: the cross-process init flock, the byte-level
header probe, a whole-database ``PRAGMA integrity_check``, the schema script
and the additive-migration pass.

These are behaviour contracts on the REAL router + REAL SQLite, not counts of
a current implementation: the integrity probe is a *first-connect* guard, and
it must stay a fail-closed guard (last test) rather than becoming a per-request
tax.
"""

from __future__ import annotations

import contextlib
import importlib.util
import sys
import threading
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


def _load_plugin_module():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_kanban_reinit_test", plugin_file,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def client(kanban_home):
    app = FastAPI()
    app.include_router(_load_plugin_module().router, prefix="/api/plugins/kanban")
    return TestClient(app)


@pytest.fixture
def probe_calls(monkeypatch):
    """Count whole-database integrity probes performed by ``connect()``."""
    calls: list[str] = []
    real = kbc._probe_integrity

    def counting(path):
        calls.append(str(path))
        return real(path)

    monkeypatch.setattr(kbc, "_probe_integrity", counting)
    return calls


def test_steady_state_requests_do_not_reprobe_integrity(client, probe_calls):
    """After the board is initialized, further requests must not re-probe.

    The probe is a first-connect guard. Re-running it per request is the
    difference between one whole-database scan and one per API call.
    """
    for _ in range(6):
        assert client.get("/api/plugins/kanban/board").status_code == 200

    assert len(probe_calls) <= 1, (
        f"integrity probe ran {len(probe_calls)}x across 6 requests: "
        "the dashboard is re-initializing the board on every request"
    )


def test_steady_state_requests_do_not_take_the_cross_process_init_lock(
    client, kanban_home, monkeypatch,
):
    """Steady-state reads must use ``connect()``'s fast path.

    The bounded init flock is a first-connect guard. Taking it per request is
    what let a stalled holder starve every other reader: past the bounded
    acquire, callers proceed WITHOUT the lock the schema pass needs.
    """
    assert client.get("/api/plugins/kanban/board").status_code == 200

    taken: list[str] = []
    real = kbc._cross_process_init_lock

    @contextlib.contextmanager
    def counting(path):
        taken.append(str(path))
        with real(path):
            yield

    monkeypatch.setattr(kbc, "_cross_process_init_lock", counting)

    for _ in range(4):
        assert client.get("/api/plugins/kanban/board").status_code == 200

    assert taken == [], (
        f"steady-state requests took the cross-process init lock {len(taken)}x"
    )


def test_concurrent_requests_do_not_serialize_on_the_init_lock(client, probe_calls):
    """Concurrent reads must not each take the cross-process init lock.

    Real threads against the real router: the dashboard serves sync endpoints
    from a thread pool, and the per-request re-init made those threads contend
    on the board's init flock.
    """
    errors: list[BaseException] = []

    def hit():
        try:
            assert client.get("/api/plugins/kanban/board").status_code == 200
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=hit) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)

    assert not errors, f"concurrent dashboard reads failed: {errors[:3]}"
    assert len(probe_calls) <= 1, (
        f"integrity probe ran {len(probe_calls)}x across 8 concurrent requests"
    )


def test_first_connect_still_probes_and_fails_closed_on_a_corrupt_board(
    client, kanban_home, monkeypatch,
):
    """The fail-closed corruption guard must survive the fix.

    Removing the per-request re-init must not remove the probe: a board that
    is corrupt when this process first opens it still refuses to open, and is
    never silently recreated.
    """
    db_path = kb.kanban_db_path()
    assert client.get("/api/plugins/kanban/board").status_code == 200

    # Force the next connect back onto the first-connect path, then hand it a
    # file whose pages are destroyed but whose SQLite header is intact.
    kbc._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    for sidecar in ("-wal", "-shm"):
        Path(str(db_path) + sidecar).unlink(missing_ok=True)
    raw = bytearray(db_path.read_bytes())
    assert len(raw) > 8192, "expected a multi-page kanban DB"
    raw[4096:] = b"\x00" * (len(raw) - 4096)
    db_path.write_bytes(bytes(raw))

    with pytest.raises(kbc.KanbanDbCorruptError):
        kbc.connect(db_path=db_path)
