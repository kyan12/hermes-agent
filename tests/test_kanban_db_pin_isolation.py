"""The test suite must never resolve the operator's real kanban database.

``HERMES_KANBAN_DB`` pins the database file path directly and takes precedence
over ``HERMES_HOME`` in ``kanban_db.kanban_db_path()``. Pytest is routinely
launched from inside a dispatcher-spawned worker or a developer shell that
exports it, so the variable arrives pointing at the operator's live board.

``tests/conftest.py`` sandboxes ``HERMES_HOME`` at module scope, before any
test module is imported — but sandboxing ``HERMES_HOME`` does nothing for a
path pin that outranks it. Measured on this install: after importing
``tests.conftest``, ``kanban_db_path()`` still resolved
``~/.hermes/kanban/boards/proteusx-engineering/kanban.db`` — the production
engineering board. Any module-scope or collection-time resolution (a lazily
imported watcher, a subprocess child that rebuilds its env from ``os.environ``)
therefore targeted the live board.

The per-test ``_hermetic_environment`` fixture does delete these vars, but
fixtures run *after* collection has imported every test module, which is
exactly the window this file guards. The ``_kanban_write_guard`` fixture is the
belt to this suspenders: it refuses writes under the real root, but only for
calls that go through ``kanban_db.connect`` while a test is running, and only
once the module has been imported.
"""

import os
from pathlib import Path

import pytest


KANBAN_PATH_PIN_VARS = (
    "HERMES_KANBAN_DB",
    "HERMES_KANBAN_HOME",
    "HERMES_KANBAN_WORKSPACES_ROOT",
    "HERMES_KANBAN_LOGS_ROOT",
)


def _real_hermes_home() -> Path:
    """The operator's actual Hermes root, ignoring any test sandboxing."""
    return (Path.home() / ".hermes").resolve()


def _under_real_root(value: str) -> bool:
    if not value:
        return False
    try:
        resolved = Path(value).expanduser().resolve()
    except Exception:
        return False
    return resolved == _real_hermes_home() or _real_hermes_home() in resolved.parents


class TestKanbanPinIsolation:
    def test_no_kanban_path_pin_survives_conftest_import(self):
        # Deliberately NOT os.environ: by test time the per-test fixture has
        # already deleted these, so reading the live environment would pass
        # even with the conftest sandbox deleted. Assert the snapshot taken at
        # conftest import, which is the moment that actually matters.
        from tests.conftest import KANBAN_PINS_AT_CONFTEST_IMPORT as pins

        offenders = {
            name: value for name, value in pins.items()
            if _under_real_root(value)
        }
        assert offenders == {}, (
            "kanban path pins still pointed inside the operator's real "
            f"~/.hermes when conftest loaded: {offenders}. Collection-time "
            "resolution of kanban_db_path() writes to the live board."
        )

    def test_the_resolved_db_path_is_outside_the_real_root(self):
        kb = pytest.importorskip("hermes_cli.kanban_db")

        resolved = kb.kanban_db_path().expanduser().resolve()
        assert not _under_real_root(str(resolved)), (
            f"kanban_db_path() resolved to {resolved}, inside the operator's "
            "real ~/.hermes"
        )

    def test_the_pre_sandbox_values_are_still_captured_for_the_write_guard(self):
        """Scrubbing must not blind the deny-list it protects.

        ``_kanban_write_guard`` denies writes under the REAL kanban root, which
        it derives from the pre-sandbox environment. If the scrub ran before
        that capture, the deny-list would point at a tempdir and silently stop
        guarding anything.
        """
        from tests import conftest as c

        assert isinstance(c._REAL_KANBAN_ROOT, Path)
        assert not str(c._REAL_KANBAN_ROOT).startswith(
            os.environ.get("HERMES_HOME", "\0")
        )

    def test_a_child_process_does_not_inherit_a_production_pin(self):
        """Subprocess tests rebuild env from os.environ; the pin must be gone."""
        for name in KANBAN_PATH_PIN_VARS:
            assert not _under_real_root(os.environ.get(name, "")), (
                f"{name} still points into the operator's real ~/.hermes; any "
                "test that spawns a child hands it the live board"
            )
