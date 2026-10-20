import hashlib
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import closing
import sqlite3
import stat
from pathlib import Path
from unittest.mock import patch

from coordinator_core.model import AssignmentInput, DecisionInput, DomainError, FeatureInput, OwnerRef
from coordinator_core.repo import resolve_repo
from coordinator_core.service import Coordinator
from coordinator_core.store import open_store
from tests.support import ext4_mountinfo


ROOT = Path(__file__).parents[1]


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def digest(data):
    return hashlib.sha256(data).hexdigest()


FAKE = r'''#!/usr/bin/env python3
import json, os, pathlib, sys, time
args = sys.argv[1:]
if args == ['--version']:
 print('codex fake 1')
 sys.exit(0)
assert args[0:2] == ['exec', '-']
cwd = pathlib.Path(args[args.index('-C') + 1])
schema = pathlib.Path(args[args.index('--output-schema') + 1])
last = pathlib.Path(args[args.index('--output-last-message') + 1])
assert schema.is_file()
envelope = json.loads(sys.stdin.read())
assert envelope['assignment_id'] and envelope['request_fingerprint']
counter = pathlib.Path(os.environ['FAKE_COUNT'])
with counter.open('a') as out: out.write('spawn\n')
mode = os.environ.get('FAKE_MODE', 'completed')
if mode == 'sleep':
 time.sleep(30)
 sys.exit(0)
target = os.environ.get('FAKE_TARGET')
if target: (cwd / target).write_text(os.environ.get('FAKE_BYTES', 'result'))
if os.environ.get('FAKE_DELETE'): (cwd / os.environ['FAKE_DELETE']).unlink()
payload = dict(assignment_id=envelope['assignment_id'], request_fingerprint=envelope['request_fingerprint'],
 result='completed', changed_paths=[os.environ.get('FAKE_CLAIM', target)] if target else [], checks=['claimed'], descendants=[],
 unresolved_question=None, options=[], affected_ids=[])
if os.environ.get('FAKE_CLAIM_EMPTY'): payload['changed_paths'] = []
if mode == 'blocked': payload['result'] = 'blocked'
if mode == 'needs-decision':
 payload['result'] = 'needs-decision'
 payload['unresolved_question'] = 'Which option?'
 payload['options'] = ['A', 'B']
if mode == 'wrong-id': payload['assignment_id'] = 'wrong'
if mode == 'wrong-fingerprint': payload['request_fingerprint'] = 'wrong'
if mode == 'bad-schema': payload['unexpected'] = True
last.write_text(json.dumps(payload))
print(json.dumps({'type':'thread.started','thread_id':'thread-1'}), flush=True)
if mode == 'wrong-thread': print(json.dumps({'type':'thread.started','thread_id':'thread-2'}), flush=True)
if mode != 'no-terminal': print(json.dumps({'type':'turn.completed'}), flush=True)
if mode == 'failed': print(json.dumps({'type':'turn.failed'}), flush=True)
sys.exit(1 if mode == 'exit-1' else 0)
'''


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.main = self.root / 'main'
        self.main.mkdir()
        git(self.main, 'init', '-q')
        git(self.main, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
            'commit', '--allow-empty', '-qm', 'initial')
        (self.main / 'AGENTS.md').write_text('Local fixture.\n')
        from install import apply, preview
        apply(self.main, preview(self.main).sha256)
        git(self.main, 'add', 'AGENTS.md', '.bmad-coordinator')
        git(self.main, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
            'commit', '-qm', 'installed')
        self.base = git(self.main, 'rev-parse', 'HEAD')
        self.feature = self.root / 'feature'
        git(self.main, 'worktree', 'add', '-qb', 'feature', str(self.feature))
        (self.feature / 'brief.md').write_text('Do the assigned work.\n')
        (self.feature / 'contract.md').write_text('Contract.\n')
        self.repo = resolve_repo(self.feature)
        self.fake_dir = self.root / 'bin'
        self.fake_dir.mkdir()
        fake = self.fake_dir / 'codex'
        fake.write_text(FAKE)
        fake.chmod(0o755)
        self.count = self.root / 'count.log'
        self.env = {'PATH': str(self.fake_dir) + os.pathsep + os.environ['PATH'],
                    'FAKE_COUNT': str(self.count)}
        with patch('coordinator_core.store.read_mountinfo', return_value=ext4_mountinfo(self.root)):
            self.store = open_store(self.repo, create=True)
            self.coordinator = Coordinator(self.store)
            self.owner = self.coordinator.claim('test-owner', self.root / 'owner.token')
            self.feature_id = self.coordinator.create_feature(self.owner, FeatureInput(
                'ship result', [], [], ['review'], ['worker only'], 'feature', self.base,
                self.base, str(self.feature), [], ['result.md', 'brief.md', 'contract.md', 'input.md']))

    def spec(self, **changes):
        sources = [{'path': 'brief.md', 'digest': digest((self.feature / 'brief.md').read_bytes())}]
        contracts = [{'path': 'contract.md', 'digest': digest((self.feature / 'contract.md').read_bytes())}]
        data = dict(feature_id=self.feature_id, idempotency_key='one', brief_ref='brief.md',
                    mandate_revision=1, source_digests=sources, contract_files=contracts,
                    contract_digest=digest(json.dumps(contracts, sort_keys=True, separators=(',', ':')).encode()),
                    writable_paths=['result.md'], permission_mode='workspace-write', author_id='worker',
                    depends_on=[], blocking_requests=[])
        data.update(changes)
        return AssignmentInput(**data)

    def fresh(self, name):
        # Attempt fence: an unknown or timed-out run keeps its worktree blocked, so each case that
        # leaves one behind gets its own worktree and feature.
        path = self.root / name
        git(self.main, 'worktree', 'add', '-qb', name, str(path))
        (path / 'brief.md').write_text('Do the assigned work.\n')
        (path / 'contract.md').write_text('Contract.\n')
        self.feature, self.repo = path, resolve_repo(path)
        with patch('coordinator_core.store.read_mountinfo', return_value=ext4_mountinfo(self.root)):
            self.feature_id = self.coordinator.create_feature(self.owner, FeatureInput(
                'ship result', [], [], ['review'], ['worker only'], name, self.base,
                self.base, str(path), [], ['result.md', 'brief.md', 'contract.md', 'input.md']))

    def assign(self, **changes):
        with patch('coordinator_core.store.read_mountinfo', return_value=ext4_mountinfo(self.root)):
            return self.coordinator.create_assignment(self.owner, self.spec(**changes))

    def run_it(self, assignment, timeout=2, **env):
        from coordinator_core.worker import run_assignment
        with patch.dict(os.environ, {**self.env, **env}), \
             patch('coordinator_core.store.read_mountinfo', return_value=ext4_mountinfo(self.root)):
            return run_assignment(self.repo, self.store, self.owner, assignment, 'fake-model', timeout)

    def test_readonly_assignment_empty_paths_creates_launches_and_zero_delta_returns(self):
        assignment = self.assign(permission_mode='read-only', writable_paths=[])
        report = self.run_it(assignment)
        self.assertEqual('completed-claim', report.state)
        row = self.coordinator.detail('assignment', assignment)
        self.assertEqual('observed', row['return']['provenance_class'])
        self.assertEqual([], row['return']['changed_paths'])
        self.assertEqual('spawn\n', self.count.read_text())

    def test_valid_completed_return_measures_exact_file_bytes(self):
        assignment = self.assign()
        report = self.run_it(assignment, FAKE_TARGET='result.md')
        self.assertEqual('completed-claim', report.state)
        changed = self.coordinator.detail('assignment', assignment)['return']['changed_paths']
        self.assertEqual([{'path': 'result.md', 'canonical_path': 'result.md',
                           'digest': digest(b'result')}], changed)

    def test_worker_cli_inspect_is_read_only(self):
        assignment = self.assign()
        state = self.store.read_snapshot()['revision']
        result = subprocess.run([os.sys.executable, str(self.feature / '.bmad-coordinator' / 'run.py'),
                                 '--repo', str(self.feature), 'worker', 'inspect', assignment],
                                capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual('prepared', json.loads(result.stdout)['state'])
        self.assertEqual(state, self.store.read_snapshot()['revision'])

    def test_readonly_nonempty_paths_and_mutation_refused(self):
        assignment = self.assign(permission_mode='read-only', writable_paths=[])
        report = self.run_it(assignment, FAKE_TARGET='result.md')
        self.assertEqual('unknown', report.state)
        self.assertEqual('running', self.coordinator.detail('assignment', assignment)['state'])
        self.assertEqual(b'result', (self.feature / 'result.md').read_bytes())

    def test_workspace_write_still_requires_nonempty_paths(self):
        with self.assertRaises(DomainError) as caught:
            self.assign(writable_paths=[])
        self.assertEqual('UNSAFE_PATH', caught.exception.code)

    def test_runner_rejects_recorded_branch_or_common_dir_mismatch(self):
        assignment = self.assign()
        git(self.feature, 'branch', '-m', 'renamed')
        with self.assertRaises(DomainError):
            self.run_it(assignment)
        git(self.feature, 'branch', '-m', 'feature')
        with closing(sqlite3.connect(self.store.path)) as conn, conn:
            conn.execute('UPDATE features SET worktree=? WHERE id=?', (str(self.main), self.feature_id))
        with self.assertRaises(DomainError):
            self.run_it(assignment)
        self.assertFalse(self.count.exists())

    def test_two_runs_one_spawn(self):
        assignment = self.assign()
        outcome = []

        def first():
            outcome.append(self.run_it(assignment, timeout=1, FAKE_MODE='sleep'))

        thread = threading.Thread(target=first)
        thread.start()
        deadline = time.monotonic() + 3
        while not self.count.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(self.count.exists())
        with self.assertRaises(DomainError):
            self.run_it(assignment)
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual('spawn\n', self.count.read_text())
        self.assertEqual('unknown', outcome[0].state)

    def test_crash_after_sidecar_or_reserve_never_relaunches(self):
        assignment = self.assign()
        original = Coordinator.reserve_launch

        def reserved_then_crash(coordinator, owner, assignment_id, client=None):
            original(coordinator, owner, assignment_id, client)
            raise DomainError('STOP', 'crash after durable reservation')

        with patch('coordinator_core.worker.Coordinator.reserve_launch', new=reserved_then_crash):
            with self.assertRaises(DomainError):
                self.run_it(assignment)
        self.assertEqual('launch-pending', self.coordinator.detail('assignment', assignment)['state'])
        with self.assertRaises(DomainError):
            self.run_it(assignment)
        self.assertFalse(self.count.exists())

    def test_unmet_dependency_does_not_poison_prepared_assignment(self):
        first = self.assign(idempotency_key='first')
        dependent = self.assign(idempotency_key='dependent', depends_on=[first],
                                permission_mode='read-only', writable_paths=[])
        from coordinator_core.run_receipt import run_name, run_root
        fingerprint = self.coordinator.detail('assignment', dependent)['request_fingerprint']
        sidecar = run_root(self.repo) / run_name(dependent, fingerprint)
        with self.assertRaises(DomainError) as caught:
            self.run_it(dependent)
        self.assertEqual('BLOCKED_EVIDENCE', caught.exception.code)
        self.assertFalse(sidecar.exists())
        self.assertEqual('prepared', self.coordinator.detail('assignment', dependent)['state'])
        self.assertEqual('completed-claim', self.run_it(first).state)
        self.assertEqual('completed-claim', self.run_it(dependent).state)
        self.assertEqual('spawn\nspawn\n', self.count.read_text())

    def test_unresolved_decision_does_not_poison_prepared_assignment(self):
        first = self.assign(idempotency_key='first')
        self.assertEqual('needs-decision', self.run_it(first, FAKE_MODE='needs-decision').state)
        request_id = self.store.read_snapshot()['decision_requests'][0]['id']
        dependent = self.assign(idempotency_key='dependent', blocking_requests=[request_id],
                                permission_mode='read-only', writable_paths=[])
        from coordinator_core.run_receipt import run_name, run_root
        fingerprint = self.coordinator.detail('assignment', dependent)['request_fingerprint']
        sidecar = run_root(self.repo) / run_name(dependent, fingerprint)
        with self.assertRaises(DomainError) as caught:
            self.run_it(dependent)
        self.assertEqual('BLOCKED_EVIDENCE', caught.exception.code)
        self.assertFalse(sidecar.exists())
        with patch('coordinator_core.store.read_mountinfo', return_value=ext4_mountinfo(self.root)):
            self.coordinator.record_decision(self.owner, DecisionInput(
                self.feature_id, 1, 'operator-reported', 'test:choice', 'choose', {'choice': 'A'}, request_id))
        self.assertEqual('completed-claim', self.run_it(dependent).state)
        self.assertEqual('spawn\nspawn\n', self.count.read_text())

    def test_blocked_and_needs_decision_are_observed_transport_not_completion(self):
        for mode, expected in [('blocked', 'blocked'), ('needs-decision', 'needs-decision')]:
            with self.subTest(mode=mode):
                assignment = self.assign(idempotency_key=mode)
                report = self.run_it(assignment, FAKE_MODE=mode)
                self.assertEqual(expected, report.state)
                row = self.coordinator.detail('assignment', assignment)
                self.assertEqual('observed', row['return']['provenance_class'])
                self.assertNotEqual('completed-claim', row['state'])
        self.assertEqual([], self.store.read_snapshot()['acceptances'])

    def test_missing_failed_or_wrong_thread_terminal_is_unknown(self):
        for mode in ('no-terminal', 'failed', 'wrong-thread', 'exit-1'):
            with self.subTest(mode=mode):
                self.fresh(mode)
                assignment = self.assign(idempotency_key=mode)
                report = self.run_it(assignment, FAKE_MODE=mode)
                self.assertEqual('unknown', report.state)
                self.assertEqual('running', self.coordinator.detail('assignment', assignment)['state'])
                if mode == 'exit-1':   # design D25: a run that is no client stop keeps the exit code as its cause
                    self.assertEqual(['Worker process exited unsuccessfully'], report.evidence_needs)

    def test_wrong_id_schema_or_fingerprint_is_unknown(self):
        for mode in ('wrong-id', 'wrong-fingerprint', 'bad-schema'):
            with self.subTest(mode=mode):
                self.fresh(mode)
                assignment = self.assign(idempotency_key=mode)
                report = self.run_it(assignment, FAKE_MODE=mode)
                self.assertEqual('unknown', report.state)

    def test_out_of_scope_alias_delete_or_immutable_source_drift_refuses_completion(self):
        cases = [('extra', {'FAKE_TARGET': 'extra.md'}),
                 ('alias', {'FAKE_TARGET': 'result.md', 'FAKE_CLAIM': './result.md'}),
                 ('delete', {'FAKE_DELETE': 'contract.md'}),
                 ('contract', {'FAKE_TARGET': 'contract.md'})]
        for name, env in cases:
            with self.subTest(name=name):
                self.fresh(name)
                assignment = self.assign(idempotency_key=name)
                report = self.run_it(assignment, **env)
                self.assertEqual('unknown', report.state)
                if name == 'extra':
                    self.assertEqual('result', (self.feature / 'extra.md').read_text())

    def test_authorized_source_target_correction_succeeds(self):
        source = self.feature / 'input.md'
        source.write_text('old')
        spec = self.spec(idempotency_key='correction', writable_paths=['input.md'])
        spec = AssignmentInput(**{**spec.__dict__, 'source_digests': spec.source_digests +
                                  [{'path': 'input.md', 'digest': digest(b'old')}]})
        with patch('coordinator_core.store.read_mountinfo', return_value=ext4_mountinfo(self.root)):
            assignment = self.coordinator.create_assignment(self.owner, spec)
        report = self.run_it(assignment, FAKE_TARGET='input.md', FAKE_BYTES='new')
        self.assertEqual('completed-claim', report.state)
        self.assertEqual('new', source.read_text())

    def test_timeout_records_signals_and_requires_reconciliation(self):
        assignment = self.assign()
        report = self.run_it(assignment, timeout=1, FAKE_MODE='sleep')
        self.assertEqual('unknown', report.state)
        timeout = json.loads((Path(report.run_dir) / 'timeout.json').read_text())
        self.assertIn('SIGTERM', timeout['signals'])
        from coordinator_core.worker import reconcile_run
        with patch('coordinator_core.store.read_mountinfo', return_value=ext4_mountinfo(self.root)):
            again = reconcile_run(self.repo, self.store, self.owner, assignment)
        self.assertEqual('unknown', again.state)

    def test_reconcile_is_idempotent_and_cannot_take_over(self):
        assignment = self.assign()
        with patch('coordinator_core.worker.Coordinator.record_return', side_effect=DomainError('STOP', 'crash')):
            report = self.run_it(assignment, FAKE_TARGET='result.md')
        self.assertEqual('unknown', report.state)
        from coordinator_core.worker import reconcile_run
        with patch('coordinator_core.store.read_mountinfo', return_value=ext4_mountinfo(self.root)):
            first = reconcile_run(self.repo, self.store, self.owner, assignment)
            revision = self.store.read_snapshot()['revision']
            second = reconcile_run(self.repo, self.store, self.owner, assignment)
        self.assertEqual('completed-claim', first.state)
        self.assertEqual('completed-claim', second.state)
        self.assertEqual(revision, self.store.read_snapshot()['revision'])
        with self.assertRaises(DomainError):
            reconcile_run(self.repo, self.store, OwnerRef('wrong', self.owner.generation), assignment)

    def test_worker_observed_assignment_exports_checkpoint_with_relative_refs(self):
        assignment = self.assign()
        self.run_it(assignment, FAKE_TARGET='result.md')
        from coordinator_core.exporter import export_checkpoint
        (self.root / 'export').mkdir()
        with patch('coordinator_core.store.read_mountinfo', return_value=ext4_mountinfo(self.root)):
            receipt = export_checkpoint(self.store, self.owner, self.root / 'export')
        data = json.loads((receipt.directory / 'checkpoint.json').read_text())
        references = [row['evidence_ref'] for row in data['records']['events'] if row.get('evidence_ref')]
        self.assertTrue(references)
        self.assertTrue(all(ref.startswith('runs/') and not Path(ref).is_absolute() for ref in references))
        anchors = [row for row in data['records']['events'] if row['kind'] == 'assignment.return-sealed']
        self.assertEqual(1, len(anchors))
        self.assertEqual(assignment, anchors[0]['subject_id'])
        self.assertEqual(digest((self.repo.common_dir / 'coordinator' / anchors[0]['evidence_ref']).read_bytes()),
                         anchors[0]['payload_digest'])

    def test_direct_reported_return_and_review_defaults_unchanged(self):
        assignment = self.assign()
        from coordinator_core.model import ReturnEvidence
        with patch('coordinator_core.store.read_mountinfo', return_value=ext4_mountinfo(self.root)):
            self.coordinator.reserve_launch(self.owner, assignment)
            self.coordinator.record_return(self.owner, assignment, ReturnEvidence(
                'direct', None, [], [], [], 'completed', None, [], [], digest(b'direct')))
        self.assertEqual('reported', self.coordinator.detail('assignment', assignment)['return']['provenance_class'])

    def test_run_sidecar_files_are_private(self):
        assignment = self.assign()
        report = self.run_it(assignment)
        directory = Path(report.run_dir)
        self.assertEqual(0o700, stat.S_IMODE(directory.stat().st_mode))
        for name in ('envelope.json', 'return.schema.json', 'events.jsonl', 'stderr.log',
                     'last.json', 'launch.json', 'return.json'):
            self.assertEqual(0o600, stat.S_IMODE((directory / name).stat().st_mode), name)

    def test_edited_or_aliased_envelope_makes_inspect_unknown(self):
        assignment = self.assign()
        report = self.run_it(assignment)
        from coordinator_core.worker import inspect_run
        envelope = Path(report.run_dir) / 'envelope.json'
        original = envelope.read_bytes()
        envelope.write_bytes(b'{"role":"different"}')
        self.assertEqual('unknown', inspect_run(self.repo, self.store, assignment).state)
        self.assertEqual('completed-claim', self.coordinator.detail('assignment', assignment)['state'])
        envelope.unlink()
        target = Path(report.run_dir) / 'alias-target'
        target.write_bytes(original)
        envelope.symlink_to(target)
        self.assertEqual('unknown', inspect_run(self.repo, self.store, assignment).state)

    def test_fifo_envelope_inspect_returns_unknown_without_blocking(self):
        assignment = self.assign()
        report = self.run_it(assignment)
        from coordinator_core.worker import inspect_run
        envelope = Path(report.run_dir) / 'envelope.json'
        envelope.unlink()
        os.mkfifo(envelope, 0o600)
        result = []
        thread = threading.Thread(target=lambda: result.append(inspect_run(self.repo, self.store, assignment)),
                                  daemon=True)
        thread.start()
        thread.join(timeout=.5)
        self.assertFalse(thread.is_alive(), 'inspection blocked on FIFO')
        self.assertEqual('unknown', result[0].state)

    def test_missing_envelope_makes_reconcile_unknown(self):
        assignment = self.assign()
        with patch('coordinator_core.worker.Coordinator.record_return', side_effect=DomainError('STOP', 'crash')):
            report = self.run_it(assignment)
        (Path(report.run_dir) / 'envelope.json').unlink()
        from coordinator_core.worker import reconcile_run
        with patch('coordinator_core.store.read_mountinfo', return_value=ext4_mountinfo(self.root)):
            result = reconcile_run(self.repo, self.store, self.owner, assignment)
        self.assertEqual('unknown', result.state)
        self.assertEqual('running', self.coordinator.detail('assignment', assignment)['state'])

    def test_envelope_drift_before_initial_return_remains_unknown(self):
        assignment = self.assign()
        from coordinator_core import worker
        original = worker._return_evidence

        def altered(*args):
            event = original(*args)
            directory = args[2]
            (directory / 'envelope.json').write_bytes(b'changed')
            return event

        with patch('coordinator_core.worker._return_evidence', side_effect=altered):
            report = self.run_it(assignment)
        self.assertEqual('unknown', report.state)
        self.assertEqual('running', self.coordinator.detail('assignment', assignment)['state'])

    def test_ignored_immutable_source_drift_cannot_complete(self):
        (self.feature / '.gitignore').write_text('ignored.md\n')
        hidden = self.feature / 'ignored.md'
        hidden.write_text('old')
        spec = self.spec(idempotency_key='ignored')
        spec = AssignmentInput(**{**spec.__dict__, 'source_digests': spec.source_digests +
                                  [{'path': 'ignored.md', 'digest': digest(b'old')}]})
        with patch('coordinator_core.store.read_mountinfo', return_value=ext4_mountinfo(self.root)):
            assignment = self.coordinator.create_assignment(self.owner, spec)
        report = self.run_it(assignment, FAKE_TARGET='ignored.md', FAKE_CLAIM_EMPTY='1')
        self.assertEqual('unknown', report.state)

    def test_reconcile_refuses_post_receipt_unrelated_drift(self):
        assignment = self.assign()
        with patch('coordinator_core.worker.Coordinator.record_return', side_effect=DomainError('STOP', 'crash')):
            self.run_it(assignment, FAKE_TARGET='result.md')
        (self.feature / 'contract.md').write_text('changed after process')
        from coordinator_core.worker import reconcile_run
        with patch('coordinator_core.store.read_mountinfo', return_value=ext4_mountinfo(self.root)):
            report = reconcile_run(self.repo, self.store, self.owner, assignment)
        self.assertEqual('unknown', report.state)
        self.assertEqual('running', self.coordinator.detail('assignment', assignment)['state'])

    def test_runner_refuses_brief_or_contract_as_writable_target_before_spawn(self):
        for target in ('brief.md', 'contract.md'):
            with self.subTest(target=target):
                assignment = self.assign(idempotency_key=target, writable_paths=[target])
                with self.assertRaises(DomainError):
                    self.run_it(assignment)
                self.assertEqual('prepared', self.coordinator.detail('assignment', assignment)['state'])
        self.assertFalse(self.count.exists())

    def test_reconcile_requires_bound_launch_and_matching_thread(self):
        assignment = self.assign()
        with patch('coordinator_core.worker.Coordinator.record_return', side_effect=DomainError('STOP', 'crash')):
            report = self.run_it(assignment)
        directory = Path(report.run_dir)
        launch_bytes = (directory / 'launch.json').read_bytes()
        (directory / 'launch.json').unlink()
        from coordinator_core.worker import reconcile_run
        with patch('coordinator_core.store.read_mountinfo', return_value=ext4_mountinfo(self.root)):
            result = reconcile_run(self.repo, self.store, self.owner, assignment)
        self.assertEqual('unknown', result.state)

        # Restore a valid launch but make terminal receipt claim another thread.
        from coordinator_core.run_receipt import canonical
        (directory / 'launch.json').write_bytes(launch_bytes)
        (directory / 'launch.json').chmod(0o600)
        terminal = json.loads((directory / 'return.json').read_text())
        terminal['thread_id'] = 'thread-2'
        (directory / 'return.json').write_bytes(canonical(terminal))
        with patch('coordinator_core.store.read_mountinfo', return_value=ext4_mountinfo(self.root)):
            result = reconcile_run(self.repo, self.store, self.owner, assignment)
        self.assertEqual('unknown', result.state)

    def test_post_spawn_receipt_failure_stops_process_and_records_unknown(self):
        assignment = self.assign()
        spawned = []
        real_popen = subprocess.Popen

        def capture(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            if args[0][1:3] != ['exec', '-']:
                return process
            spawned.append(process)
            def cleanup():
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, 9)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=2)
            self.addCleanup(cleanup)
            return process

        with patch('coordinator_core.worker.subprocess.Popen', side_effect=capture), \
             patch('coordinator_core.worker.seal', side_effect=OSError('receipt failure')):
            report = self.run_it(assignment, FAKE_MODE='sleep')
        self.assertEqual('unknown', report.state)
        self.assertEqual(1, len(spawned))
        self.assertIsNotNone(spawned[0].poll())
        self.assertEqual('launch-pending', self.coordinator.detail('assignment', assignment)['state'])

    def test_run_receipts_include_process_version_group_and_exit_metadata(self):
        assignment = self.assign()
        report = self.run_it(assignment)
        directory = Path(report.run_dir)
        launch = json.loads((directory / 'launch.json').read_text())
        terminal = json.loads((directory / 'return.json').read_text())
        self.assertEqual('codex fake 1', launch['cli_version'])
        self.assertIsInstance(launch['pgid'], int)
        self.assertEqual(0, terminal['exit_code'])
        self.assertTrue(terminal['ended_at'])

    def test_crash_before_terminal_anchor_cannot_reconcile(self):
        assignment = self.assign()
        with patch('coordinator_core.worker.Coordinator.anchor_return', side_effect=DomainError('STOP', 'crash')):
            report = self.run_it(assignment)
        self.assertEqual('unknown', report.state)
        self.assertTrue((Path(report.run_dir) / 'return.json').exists())
        from coordinator_core.worker import reconcile_run
        with patch('coordinator_core.store.read_mountinfo', return_value=ext4_mountinfo(self.root)):
            retry = reconcile_run(self.repo, self.store, self.owner, assignment)
        self.assertEqual('unknown', retry.state)
        self.assertEqual('running', self.coordinator.detail('assignment', assignment)['state'])

    def test_edited_terminal_receipt_cannot_reconcile_against_durable_anchor(self):
        assignment = self.assign()
        with patch('coordinator_core.worker.Coordinator.record_return', side_effect=DomainError('STOP', 'crash')):
            report = self.run_it(assignment)
        from coordinator_core.run_receipt import canonical
        terminal = Path(report.run_dir) / 'return.json'
        value = json.loads(terminal.read_text())
        value['checks'] = ['edited']
        terminal.write_bytes(canonical(value))
        from coordinator_core.worker import reconcile_run
        with patch('coordinator_core.store.read_mountinfo', return_value=ext4_mountinfo(self.root)):
            retry = reconcile_run(self.repo, self.store, self.owner, assignment)
        self.assertEqual('unknown', retry.state)

    def test_claim_fields_must_match_last_message_even_with_matching_anchor(self):
        assignment = self.assign()
        with patch('coordinator_core.worker.Coordinator.anchor_return', return_value=None):
            report = self.run_it(assignment)
        self.assertEqual('unknown', report.state)
        from coordinator_core.run_receipt import canonical
        from coordinator_core.model import ReturnEvidence
        directory = Path(report.run_dir)
        terminal = json.loads((directory / 'return.json').read_text())
        terminal['checks'] = ['different']
        (directory / 'return.json').write_bytes(canonical(terminal))
        event = ReturnEvidence(terminal['source_id'], f'runs/{directory.name}/return.json',
                               terminal['changed_paths'], terminal['checks'], terminal['descendants'],
                               terminal['result'], terminal['unresolved_question'], terminal['options'],
                               terminal['affected_ids'], digest((directory / 'return.json').read_bytes()))
        with patch('coordinator_core.store.read_mountinfo', return_value=ext4_mountinfo(self.root)):
            self.coordinator.anchor_return(self.owner, assignment, event)
            from coordinator_core.worker import reconcile_run
            retry = reconcile_run(self.repo, self.store, self.owner, assignment)
        self.assertEqual('unknown', retry.state)

    def test_edited_launch_receipt_cannot_reconcile_against_observed_event(self):
        assignment = self.assign()
        with patch('coordinator_core.worker.Coordinator.record_return', side_effect=DomainError('STOP', 'crash')):
            report = self.run_it(assignment)
        from coordinator_core.run_receipt import canonical
        directory = Path(report.run_dir)
        launch = json.loads((directory / 'launch.json').read_text())
        launch['argv'].append('--ignore-rules')
        (directory / 'launch.json').write_bytes(canonical(launch))
        terminal = json.loads((directory / 'return.json').read_text())
        terminal['launch_sha256'] = digest((directory / 'launch.json').read_bytes())
        (directory / 'return.json').write_bytes(canonical(terminal))
        from coordinator_core.worker import reconcile_run
        with patch('coordinator_core.store.read_mountinfo', return_value=ext4_mountinfo(self.root)):
            retry = reconcile_run(self.repo, self.store, self.owner, assignment)
        self.assertEqual('unknown', retry.state)

    def test_inspect_completed_state_rechecks_current_bytes_and_reported_origin(self):
        from coordinator_core.worker import inspect_run, reconcile_run
        assignment = self.assign()
        self.run_it(assignment, FAKE_TARGET='result.md')
        (self.feature / 'result.md').write_text('drift')
        report = inspect_run(self.repo, self.store, assignment)
        self.assertEqual('unknown', report.state)
        self.assertTrue(report.evidence_needs)
        with patch('coordinator_core.store.read_mountinfo', return_value=ext4_mountinfo(self.root)):
            again = reconcile_run(self.repo, self.store, self.owner, assignment)
        self.assertEqual('unknown', again.state)
        self.assertEqual('completed-claim', self.coordinator.detail('assignment', assignment)['state'])

        direct = self.assign(idempotency_key='direct')
        from coordinator_core.model import ReturnEvidence
        with patch('coordinator_core.store.read_mountinfo', return_value=ext4_mountinfo(self.root)):
            self.coordinator.reserve_launch(self.owner, direct)
            self.coordinator.record_return(self.owner, direct, ReturnEvidence(
                'direct', None, [], [], [], 'completed', None, [], [], digest(b'direct')))
        self.assertEqual('unknown', inspect_run(self.repo, self.store, direct).state)

    def test_keyboard_interrupt_after_spawn_stops_group_and_preserves_unknown(self):
        assignment = self.assign()
        spawned = []
        real_popen = subprocess.Popen

        def capture(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            if args[0][1:3] == ['exec', '-']:
                spawned.append(process)
                def cleanup():
                    if process.poll() is None:
                        os.killpg(process.pid, 9)
                        process.wait(timeout=2)
                self.addCleanup(cleanup)
            return process

        with patch('coordinator_core.worker.subprocess.Popen', side_effect=capture), \
             patch('coordinator_core.worker.seal', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_it(assignment, FAKE_MODE='sleep')
        self.assertEqual(1, len(spawned))
        self.assertIsNotNone(spawned[0].poll())
        self.assertEqual('launch-pending', self.coordinator.detail('assignment', assignment)['state'])

    def test_nonzero_or_malformed_terminal_has_private_process_end_record(self):
        for mode, code in [('exit-1', 1), ('no-terminal', 0)]:
            with self.subTest(mode=mode):
                self.fresh(mode)
                assignment = self.assign(idempotency_key=mode)
                report = self.run_it(assignment, FAKE_MODE=mode)
                end = Path(report.run_dir) / 'end.json'
                self.assertTrue(end.exists())
                self.assertEqual(0o600, stat.S_IMODE(end.stat().st_mode))
                self.assertEqual(code, json.loads(end.read_text())['exit_code'])


if __name__ == '__main__':
    unittest.main()
