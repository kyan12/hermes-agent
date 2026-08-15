"""Tests for forced-skill/profile capability validation at kanban
create-time and dispatch-time.

Regression coverage for the 2026-08-14 incident on proteusx-engineering:
task t_6b7f8d42 was created with assignee=code-crab and a forced skill the
profile does not expose; the worker crashed at startup with
``Unknown skill(s): ...`` and only the automation_recovery reconciler
cleaned it up. ``create_task`` now rejects such cards up front, and
``dispatch_once`` re-validates before spawning (auto-blocking
deterministically instead of crash-looping) for cards that predate the
create-time gate or whose assignee later lost the skill.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB and a profiles root."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _make_profile(home: Path, name: str, *, config: str | None = None) -> Path:
    """Create a bare profile dir under the test profiles root."""
    profile_dir = home / "profiles" / name
    profile_dir.mkdir(parents=True)
    if config is not None:
        (profile_dir / "config.yaml").write_text(config, encoding="utf-8")
    return profile_dir


def _make_skill(
    profile_dir: Path,
    rel_dir: str,
    *,
    frontmatter_name: str | None = None,
) -> Path:
    """Drop a minimal SKILL.md into a profile's skills tree."""
    skill_dir = profile_dir / "skills" / rel_dir
    skill_dir.mkdir(parents=True)
    name_line = f"name: {frontmatter_name}\n" if frontmatter_name else ""
    (skill_dir / "SKILL.md").write_text(
        f"---\n{name_line}description: test skill\n---\n\nBody.\n",
        encoding="utf-8",
    )
    return skill_dir


# ---------------------------------------------------------------------------
# find_unavailable_forced_skills — unit surface
# ---------------------------------------------------------------------------


def test_helper_returns_empty_for_unknown_profile(kanban_home):
    assert kb.find_unavailable_forced_skills("ghost", ["anything"]) == []


def test_helper_returns_empty_for_empty_inputs(kanban_home):
    _make_profile(kanban_home, "worker")
    assert kb.find_unavailable_forced_skills("worker", []) == []
    assert kb.find_unavailable_forced_skills(None, ["x"]) == []
    assert kb.find_unavailable_forced_skills("", ["x"]) == []


def test_helper_flags_missing_and_accepts_present(kanban_home):
    profile_dir = _make_profile(kanban_home, "worker")
    _make_skill(profile_dir, "blogwatcher")
    assert kb.find_unavailable_forced_skills("worker", ["blogwatcher"]) == []
    assert kb.find_unavailable_forced_skills(
        "worker", ["apple-app-release-ops"]
    ) == ["apple-app-release-ops"]


def test_helper_matches_frontmatter_name_and_categorized_path(kanban_home):
    profile_dir = _make_profile(kanban_home, "worker")
    # Directory name differs from the frontmatter name; both must resolve,
    # plus the categorized relative path form.
    _make_skill(profile_dir, "devops/kanban-worker-dir", frontmatter_name="kanban-worker")
    assert kb.find_unavailable_forced_skills("worker", ["kanban-worker"]) == []
    assert kb.find_unavailable_forced_skills("worker", ["kanban-worker-dir"]) == []
    assert (
        kb.find_unavailable_forced_skills("worker", ["devops/kanban-worker-dir"])
        == []
    )


def test_helper_reads_external_dirs_from_profile_config(kanban_home, tmp_path):
    ext = tmp_path / "ext-skills"
    (ext / "special").mkdir(parents=True)
    (ext / "special" / "SKILL.md").write_text(
        "---\nname: special\ndescription: x\n---\n", encoding="utf-8"
    )
    _make_profile(
        kanban_home, "worker", config=f"skills:\n  external_dirs:\n    - {ext}\n"
    )
    assert kb.find_unavailable_forced_skills("worker", ["special"]) == []


def test_helper_treats_disabled_skill_as_unavailable(kanban_home):
    profile_dir = _make_profile(
        kanban_home, "worker", config="skills:\n  disabled:\n    - blogwatcher\n"
    )
    _make_skill(profile_dir, "blogwatcher")
    assert kb.find_unavailable_forced_skills("worker", ["blogwatcher"]) == [
        "blogwatcher"
    ]


def test_helper_skips_plugin_qualified_names(kanban_home):
    _make_profile(kanban_home, "worker")
    # Plugin skills resolve through the runtime plugin registry, which a
    # static cross-profile check cannot reproduce — never flag them.
    assert kb.find_unavailable_forced_skills("worker", ["superpowers:x"]) == []


def test_helper_flags_ambiguous_skill_names(kanban_home, tmp_path):
    """skill_view refuses a name matching >1 candidate; the worker would
    crash on it at startup exactly like a missing skill, so the gate must
    flag it too (independent-review finding)."""
    profile_dir = _make_profile(kanban_home, "worker")
    _make_skill(profile_dir, "a/dup")
    _make_skill(profile_dir, "b/dup")
    assert kb.find_unavailable_forced_skills("worker", ["dup"]) == ["dup"]
    # The unambiguous categorized forms remain loadable.
    assert kb.find_unavailable_forced_skills("worker", ["a/dup", "b/dup"]) == []


# ---------------------------------------------------------------------------
# create_task — create-time gate
# ---------------------------------------------------------------------------


def test_create_task_rejects_skill_missing_from_assignee_profile(kanban_home):
    _make_profile(kanban_home, "code-crab")
    _make_skill(kanban_home / "profiles" / "code-crab", "blogwatcher")
    with kb.connect() as conn:
        with pytest.raises(ValueError, match="apple-app-release-ops"):
            kb.create_task(
                conn,
                title="misconfigured",
                assignee="code-crab",
                skills=["apple-app-release-ops"],
            )
        # The partial set must also be rejected, naming only the missing one.
        with pytest.raises(ValueError) as excinfo:
            kb.create_task(
                conn,
                title="partially misconfigured",
                assignee="code-crab",
                skills=["blogwatcher", "apple-app-release-ops"],
            )
        assert "apple-app-release-ops" in str(excinfo.value)
        # Error message must name the assignee so the creator can reroute.
        assert "code-crab" in str(excinfo.value)


def test_create_task_accepts_available_forced_skills(kanban_home):
    profile_dir = _make_profile(kanban_home, "code-crab")
    _make_skill(profile_dir, "blogwatcher")
    _make_skill(profile_dir, "github-code-review")
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="well configured",
            assignee="code-crab",
            skills=["blogwatcher", "github-code-review"],
        )
        task = kb.get_task(conn, tid)
        assert task.skills == ["blogwatcher", "github-code-review"]


def test_create_task_skips_gate_for_unknown_profile(kanban_home):
    """Unknown assignees stay the dispatcher's `skipped_nonspawnable` class —
    create-time must not start rejecting them on skills grounds."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="lane task",
            assignee="orion-cc",
            skills=["whatever"],
        )
        assert kb.get_task(conn, tid).skills == ["whatever"]


def test_create_task_skips_gate_without_assignee(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="unassigned", skills=["whatever"])
        assert kb.get_task(conn, tid).skills == ["whatever"]


# ---------------------------------------------------------------------------
# dispatch_once — pre-spawn re-validation
# ---------------------------------------------------------------------------


def _create_then_strip_skill(conn, home: Path, profile: str, skill: str) -> str:
    """Create a valid forced-skill task, then uninstall the skill.

    Simulates cards that predate the create-time gate or whose assignee
    profile lost the skill after creation.
    """
    profile_dir = home / "profiles" / profile
    skill_dir = _make_skill(profile_dir, skill)
    tid = kb.create_task(conn, title="stale card", assignee=profile, skills=[skill])
    (skill_dir / "SKILL.md").unlink()
    skill_dir.rmdir()
    return tid


def test_dispatch_auto_blocks_unavailable_forced_skill_without_spawning(kanban_home):
    _make_profile(kanban_home, "code-crab")
    spawned: list[str] = []

    def _spy_spawn(task, workspace, **kwargs):
        spawned.append(task.id)
        return 4321

    with kb.connect() as conn:
        tid = _create_then_strip_skill(conn, kanban_home, "code-crab", "gone-skill")
        res = kb.dispatch_once(conn, spawn_fn=_spy_spawn)

        assert spawned == [], "worker must never be spawned for a deterministic miss"
        assert tid in res.auto_blocked
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        row = conn.execute(
            "SELECT last_failure_error FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
        assert "gone-skill" in row["last_failure_error"]
        assert "code-crab" in row["last_failure_error"]
        # The gave_up event carries the machine-readable trigger so the
        # reconciler can distinguish this from transient spawn failures.
        gave_up = [e for e in kb.list_events(conn, tid) if e.kind == "gave_up"]
        assert gave_up, "expected a gave_up event"
        assert gave_up[-1].payload.get("trigger") == "forced_skill_unavailable"
        assert gave_up[-1].payload.get("missing_skills") == ["gone-skill"]


def test_dispatch_spawns_when_forced_skill_available(kanban_home):
    profile_dir = _make_profile(kanban_home, "code-crab")
    _make_skill(profile_dir, "blogwatcher")
    spawned: list[str] = []

    def _spy_spawn(task, workspace, **kwargs):
        spawned.append(task.id)
        return 4321

    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="good", assignee="code-crab", skills=["blogwatcher"]
        )
        res = kb.dispatch_once(conn, spawn_fn=_spy_spawn)
        assert spawned == [tid]
        assert [s[0] for s in res.spawned] == [tid]
        assert kb.get_task(conn, tid).status == "running"


def test_dispatch_leaves_unskilled_tasks_untouched(kanban_home):
    """The gate only applies to tasks with forced skills."""
    _make_profile(kanban_home, "code-crab")

    def _spy_spawn(task, workspace, **kwargs):
        return 4321

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="plain", assignee="code-crab")
        res = kb.dispatch_once(conn, spawn_fn=_spy_spawn)
        assert [s[0] for s in res.spawned] == [tid]
        assert res.auto_blocked == []
