import importlib.util
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('guard_fixture_helpers', Path(__file__).with_name('test_kanban_pr_continuation.py'))
f = importlib.util.module_from_spec(spec)
spec.loader.exec_module(f)
board, checkout = f.board, f.checkout
kb, kbd, kpr = f.kb, f.kbd, f.kpr

def prepared(board, checkout, monkeypatch, unrelated=False):
    task, branch, head = f._mid_pr_crash(board, checkout)
    shim_dir = checkout.parent / ('shim-' + task)
    shim_dir.mkdir()
    shim = shim_dir / 'gh'
    payload = f._pr_payload(head_ref='unrelated' if unrelated else branch, head_sha=head)
    shim.write_text('#!' + sys.executable + '\nimport json, sys\nprint(json.dumps({\"id\": 123} if sys.argv[2] == \"user\" else ' + repr(payload) + '))\n')
    shim.chmod(0o755)
    monkeypatch.setenv('PATH', str(shim_dir) + os.pathsep + os.environ['PATH'])
    monkeypatch.setattr(kpr, '_gh_api', f.REAL_GH_API)
    return task


@pytest.mark.parametrize('held', ['active_pr', 'history_unresolved', 'fresh'])
def test_direct_claim_must_not_bypass_hold(board, checkout, monkeypatch, held):
    if held == 'fresh':
        task = kb.create_task(board, title='ordinary fresh claim', assignee='a')
        board.execute("UPDATE tasks SET status='ready' WHERE id=?", (task,))
    else:
        task = prepared(board, checkout, monkeypatch) if held == 'active_pr' else f._crashed_on_no_pr_of_its_own(board, f'I opened {f.PR}')
        assert kbd.check_respawn_guard(board, task) == held
    def no_network(*args, **kwargs):
        pytest.fail('claim must never fetch network evidence inside its transaction')
    monkeypatch.setattr(kpr, '_gh_api', no_network)
    before = board.execute('SELECT COUNT(*) FROM task_runs WHERE task_id=?', (task,)).fetchone()[0]
    claimed = kb.claim_task(board, task)
    assert (claimed is not None) == (held == 'fresh')
    assert board.execute('SELECT COUNT(*) FROM task_runs WHERE task_id=?', (task,)).fetchone()[0] == before + (held == 'fresh')


@pytest.mark.parametrize('authority', ['reconciled', 'attested'])
@pytest.mark.parametrize('error', [None, '403 forbidden quota exhausted', 'invalid API key'])
def test_receipt_claim_rechecks_new_quota_block(board, checkout, monkeypatch, authority, error):
    from hermes_cli import kanban_resume as resume
    task = prepared(board, checkout, monkeypatch)
    if authority == 'reconciled':
        receipt = kbd._reconcile_active_pr(board, task, lane='ready').resume_receipt_id
    else:
        preview = resume.preview(board, task)
        with kb.write_txn(board):
            receipt = resume.issue(board, task, expected=preview['expected'],
                                   issued_by='authenticated-owner', attested=True)
    assert receipt
    board.execute('UPDATE tasks SET last_failure_error=? WHERE id=?', (error, task))
    if error:
        assert kbd.check_respawn_guard(board, task) == 'blocker_auth'
    monkeypatch.setattr(kpr, '_gh_api', lambda *args, **kwargs: pytest.fail('claim attempted network'))
    claimed = kb.claim_task(board, task, resume_receipt_id=receipt)
    assert (claimed is not None) == (error is None)
    consumed = board.execute('SELECT consumed_at FROM task_resume_receipts WHERE id=?', (receipt,)).fetchone()[0]
    assert (consumed is not None) == (error is None)


@pytest.mark.parametrize('unrelated', [False, True])
@pytest.mark.parametrize('mutation', ['none', 'comment', 'assignee', 'occurrence', 'predecessor', 'claim_lock'])
def test_spawn_revalidates_complete_authority(board, checkout, monkeypatch, unrelated, mutation):
    task = prepared(board, checkout, monkeypatch, unrelated)
    original = kb.claim_task
    def claim(*args, **kwargs):
        claimed = original(*args, **kwargs)
        assert claimed is not None
        mutations = {
            'none': lambda: None,
            'comment': lambda: kb.add_comment(board, task, 'owner', 'Stop: captured authority is obsolete'),
            'assignee': lambda: board.execute("UPDATE tasks SET assignee='different-owner' WHERE id=?", (task,)),
            'occurrence': lambda: kb._append_event(board, task, 'reopened', {'status': 'ready'}),
            'predecessor': lambda: board.execute(
                "INSERT INTO task_runs(task_id, status, started_at, ended_at, outcome) "
                "VALUES (?, 'crashed', ?, ?, 'crashed')", (task, int(time.time()), int(time.time()))),
            'claim_lock': lambda: board.execute("UPDATE tasks SET claim_lock='different-claim' WHERE id=?", (task,)),
        }
        mutations[mutation]()
        return claimed
    monkeypatch.setattr(kb, 'claim_task', claim)
    result, spawned = f._dispatch(board)
    assert bool(spawned) == (mutation == 'none')
    if mutation == 'claim_lock':
        row = board.execute('SELECT claim_lock, consecutive_failures FROM tasks WHERE id=?', (task,)).fetchone()
        assert row['claim_lock'] == 'different-claim'
        assert row['consecutive_failures'] == 0


@pytest.mark.parametrize('boundary', ['admit', 'claim'])
@pytest.mark.parametrize('source', ['pr_acceptance', 'published_pr', 'completion_contract'])
def test_exact_source_payload_digest(board, checkout, monkeypatch, boundary, source):
    task = prepared(board, checkout, monkeypatch)
    run_id = board.execute('SELECT id FROM task_runs WHERE task_id=?', (task,)).fetchone()[0]
    if source == 'pr_acceptance':
        kb._append_event(board, task, 'pr_acceptance', {'pr_url': f.PR, 'head_sha': 'old', 'ok': False}, run_id=run_id)
    elif source == 'published_pr':
        board.execute('UPDATE task_runs SET metadata=? WHERE id=?', (json.dumps({'published_pr': f.PR, 'head_sha': 'old'}), run_id))
    else:
        board.execute('UPDATE tasks SET completion_contract=? WHERE id=?', (f.PR, task))
    snap = kpr.capture(board, task, window_seconds=86400)
    snap, observation = kpr.collect(snap, deadline=time.time()+10)
    receipt = kpr.admit(board, snap, observation) if boundary == 'claim' else None
    if source == 'pr_acceptance':
        board.execute("UPDATE task_events SET payload=? WHERE task_id=? AND kind='pr_acceptance'", (json.dumps({'pr_url': f.PR, 'head_sha': 'changed', 'ok': True}), task))
    elif source == 'published_pr':
        board.execute('UPDATE task_runs SET metadata=? WHERE id=?', (json.dumps({'published_pr': f.PR, 'head_sha': 'changed'}), run_id))
    else:
        board.execute('UPDATE tasks SET completion_contract=? WHERE id=?', (f.PR + '\n', task))
    if boundary == 'admit':
        assert kpr.admit(board, snap, observation) is None
    else:
        assert receipt
        assert kb.claim_task(board, task, resume_receipt_id=receipt) is None
        assert board.execute('SELECT consumed_at FROM task_resume_receipts WHERE id=?', (receipt,)).fetchone()[0] is None


def test_transport_reaps_reader_when_descendant_holds_pipe(tmp_path, monkeypatch):
    shim = tmp_path / 'gh'
    done = tmp_path / 'done'
    child = f"import time; from pathlib import Path; time.sleep(2); Path({str(done)!r}).touch()"
    shim.write_text('#!' + sys.executable + '\nimport subprocess, sys\nsubprocess.Popen([sys.executable, "-c", '+repr(child)+'])\nprint("{}", flush=True)\n')
    shim.chmod(0o755)
    monkeypatch.setenv('PATH', str(tmp_path) + os.pathsep + os.environ['PATH'])
    processes = []
    original_popen = kpr.subprocess.Popen
    def owned_popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(kpr.subprocess, 'Popen', owned_popen)
    before = set(threading.enumerate())
    try:
        with pytest.raises(kpr.TransportError):
            f.REAL_GH_API('user', timeout=0.4)
        leaked = [t for t in threading.enumerate() if t not in before and t.is_alive()]
    finally:
        deadline = time.monotonic()+5
        while not done.exists() and time.monotonic()<deadline:
            time.sleep(.05)
        for t in set(threading.enumerate())-before:
            t.join(timeout=1)
    assert not leaked, f'live reader threads after transport returned: {leaked}'
    assert processes and all(process.stdout.closed for process in processes)
    assert done.exists(), 'the disposable descendant must exit cooperatively'
