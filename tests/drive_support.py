"""Driver test support: scenario-driven fake clients behind the production registry entries, a child-driver harness
with kill and pause hooks, a CLI runner and the launch log. Fakes prove coordinator logic only."""

import json
import os
import signal
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from coordinator_core import bmad_contract as contract
from coordinator_core import bmad_implementation as implementation
from coordinator_core import bmad_review, clients, drive_epic, drive_pipeline, inbox, judgment
from coordinator_core.bmad_implementation import IMPLEMENTATION
from coordinator_core.bmad_planning import PLAN, REVIEW
from coordinator_core.model import DomainError, OwnerRef
from coordinator_core.repo import resolve_repo
from coordinator_core.run_receipt import read_binding, receipt_ref, run_name
from coordinator_core.service import (ANCHOR_TOPS, DRIVE_EVENTS, TERMINAL_STATES, Coordinator, _anchored_bytes,
                                      binding_anchor)
from coordinator_core.store import open_store
from coordinator_core.worker import historical_return
from tests import fake_clients
from tests.test_bmad_review import ReviewCase

COORDINATOR = Path(__file__).parents[1]

# The production `codex` and `claude` names resolve to this wrapper. With FAKE_EXTRA in the environment it passes
# straight through (the in-process helpers of the planning tests); otherwise the per-test scenario file decides the
# return of each launch by its brief key. The emission itself is tests/fake_clients.FAKE, used unchanged. A
# coordinator-judgment envelope (an isolated run, never an assignment) is answered here from the scenario's
# `judgments` entry (by key, else `default`), shaped by the envelope's `schema` (v1, v2 or v3 defaults), emitted with
# the Codex last-message protocol or the fake Claude stream (tests/fake_clients.FAKE_CLAUDE, read only).
# `version_fail` names executables whose --version probe fails. A bound review brief is answered from the brief itself
# (sealed files hashed, carried items disposed `low`) plus the step entry's `findings` (severity and optional fields)
# or `mode`. An implementation brief (`# BMAD story implement`) writes the entry's `files`, deletes its `delete` paths
# and returns its `status`, `questions`, `conflicts`, `options`, `tasks`, `verification`, `notes` and `claim`; a spec
# author brief (`# BMAD story spec`, design D24) the same with `open_questions` and `assumptions` instead of `tasks` and
# `verification`. A spec review brief lists its sealed files without a kind. A `stories` map (story key → step entries)
# is consulted first for a story brief, so an epic's stories share step ids.
WRAPPER = r'''#!/usr/bin/env python3
import json, os, pathlib, re, subprocess, sys, time, uuid
INNER = __INNER__
__CLAUDE__
args = sys.argv[1:]
scenario_path = os.environ.get('DRIVE_SCENARIO')
scenario = {}
if scenario_path and os.path.exists(scenario_path):
    scenario = json.loads(pathlib.Path(scenario_path).read_text())
if args == ['--version'] and os.path.basename(sys.argv[0]) in scenario.get('version_fail', []):
    sys.exit(1)
if args == ['--version'] or 'FAKE_EXTRA' in os.environ:
    sys.exit(subprocess.run([INNER, *args]).returncode)
raw = sys.stdin.buffer.read()
envelope = json.loads(raw)
if envelope.get('kind') == 'coordinator-judgment-request':
    jkey, cwd = envelope['judgment_key'], pathlib.Path(os.getcwd())
    entries = scenario.get('judgments', {})
    entry = entries.get(jkey, entries.get('default', {}))
    files = sorted(path.relative_to(cwd).as_posix() for path in cwd.rglob('*')
                   if '.git' not in path.relative_to(cwd).parts and path.is_file())
    with open(os.environ['DRIVE_LAUNCH_LOG'], 'a') as out:
        out.write(json.dumps({'key': jkey, 'judgment': True, 'pid': os.getpid(), 'cwd': str(cwd), 'files': files,
                              'executable': os.path.basename(sys.argv[0])}) + '\n')
    if entry.get('block'):
        deadline = time.time() + 30
        while not os.path.exists(entry['block']) and time.time() < deadline:
            time.sleep(.02)
    schema = envelope.get('schema', 'coordinator-judgment-v1')
    payload = {'judgment_key': jkey, 'request_id': envelope['request_id'], 'verdict': 'refuse', 'answers': [],
               'rationale': '', 'refusal': 'no delegated clause covers this question'}
    if schema in ('coordinator-judgment-v2', 'coordinator-judgment-v3'):
        payload['schema'] = schema
        if schema == 'coordinator-judgment-v3':
            payload['deferred'] = []
    else:
        payload['clause'] = {'authority_id': '', 'field': '', 'pointer': '', 'quote': ''}
    payload.update(entry.get('payload', {}))
    mode = entry.get('mode')
    if mode == 'wrong-key':
        payload['judgment_key'] = jkey + '-other'
    if mode == 'wrong-request':
        payload['request_id'] = 'other-request'
    if mode == 'schema-extra':
        payload['extra'] = True
    if mode == 'schema-type':
        payload['answers'] = 'not a list'
    if mode == 'write':
        (cwd / 'note.md').write_text('written\n')
    def emit(value):
        print(json.dumps(value), flush=True)
    if args and args[0] == '-p':
        session, permission, requested = claude_argv(args)
        claude_emit(emit, session, permission, requested, 'completed', payload, cwd)
    else:
        assert args[:2] == ['exec', '-'] and args[args.index('--sandbox') + 1] == 'read-only'
        assert '--skip-git-repo-check' in args
        pathlib.Path(args[args.index('--output-last-message') + 1]).write_text(json.dumps(payload))
        emit({'type': 'thread.started', 'thread_id': uuid.uuid4().hex})
        emit({'type': 'turn.completed', 'usage': {'input_tokens': 1000, 'cached_input_tokens': 400,
                                                  'cache_write_input_tokens': 0, 'output_tokens': 50,
                                                  'reasoning_output_tokens': 5}})   # bill design B2, as the fake
    sys.exit(0)
key = pathlib.PurePosixPath(envelope['brief_ref']).stem
match = re.fullmatch(r'drv-[0-9a-f]{12}-(.+)-([0-9]+)', key)
entry = scenario.get('keys', {}).get(key)
story_match = re.match(r'# BMAD story (?:spec|implement) (\S+) ', envelope['brief'])
if entry is None and match and story_match:   # an epic run: the same step ids repeat per story (epic drive tests)
    entry = scenario.get('stories', {}).get(story_match.group(1), {}).get(match.group(1) + '-' + match.group(2))
if entry is None and match:
    steps = scenario.get('steps', {})
    entry = steps.get(match.group(1) + '-' + match.group(2), steps.get(match.group(1)))
if entry is None:
    entry = scenario.get('default', {'status': 'complete'})
with open(os.environ['DRIVE_LAUNCH_LOG'], 'a') as out:
    out.write(json.dumps({'key': key, 'assignment_id': envelope['assignment_id'], 'pid': os.getpid()}) + '\n')
if entry.get('block'):
    deadline = time.time() + 30
    while not os.path.exists(entry['block']) and time.time() < deadline:
        time.sleep(.02)
brief = envelope['brief']
if brief.startswith('# BMAD review'):
    # A bound reviewer: attest the sealed files it can read, run the bound lenses, dispose of every carried item
    # (`low`) and report the scenario's findings for this step (default none).
    import hashlib
    profile = dict(re.findall(r'^- (bundle_id|manifest_digest): `([^`]*)`$', brief, re.M))
    lenses = re.search(r'^- lenses: (.+)$', brief, re.M).group(1).split(', ')
    read = [{'path': path, 'sha256': hashlib.sha256(pathlib.Path(sealed).read_bytes()).hexdigest()}
            for path, sealed in re.findall(r'^- (?:\[(?:artifact|dependency)\] )?`([^`]+)` at `([^`]+)`$', brief, re.M)]
    carried = re.findall(r'^- contributor `([^`]+)` kind `([^`]+)` index ([0-9]+):', brief, re.M)
    findings = [{'lens': lenses[0], 'location': 'prd.md:1', 'trigger_condition': 't', 'guard_snippet': 'g',
                 'potential_consequence': 'c', **item} for item in entry.get('findings', [])]
    review = {'result': 'completed', 'bundle_id': profile['bundle_id'], 'manifest_digest': profile['manifest_digest'],
              'lenses_run': lenses, 'bundle_read': read, 'findings': findings,
              'dispositions': [{'contributor_id': cid, 'kind': kind, 'index': int(index), 'severity': 'low'}
                               for cid, kind, index in carried]}
    env = dict(os.environ, FAKE_WRITE='[]', FAKE_EXTRA=json.dumps(review), FAKE_MODE=entry.get('mode', 'completed'))
    sys.exit(subprocess.run([INNER, *args], input=raw, env=env).returncode)
if brief.startswith(('# BMAD story implement', '# BMAD story spec')):
    # An implementation worker: write the scenario's files, delete the named ones, declare tasks and verification.
    # A spec author declares its open questions and assumptions instead.
    status = entry.get('status', 'complete')
    questions, conflicts = entry.get('questions', []), entry.get('conflicts', [])
    files = [] if status == 'blocked' else [list(item) for item in entry.get('files', [])]
    deletes = [] if status == 'blocked' else list(entry.get('delete', []))
    result = 'blocked' if status == 'blocked' else 'needs-decision' if questions or conflicts else 'completed'
    question = json.dumps({'open_questions': questions, 'conflicts': conflicts}, sort_keys=True,
                          separators=(',', ':')) if result == 'needs-decision' else None
    claim = entry.get('claim', [path for path, _ in files])
    extra = {'result': result, 'unresolved_question': question, 'options': entry.get('options', []),
             'changed_paths': claim, 'tasks': entry.get('tasks', [{'task': 'Implement `greet` in `pkg/greet.py`', 'done': True}]),
             'verification': entry.get('verification', [{'command': 'unit', 'exit_code': 0, 'summary': 'declared'}]),
             'notes': entry.get('notes', 'fake worker notes')}
    if brief.startswith('# BMAD story spec'):
        del extra['tasks'], extra['verification']
        extra.update(open_questions=entry.get('open_questions', []), assumptions=entry.get('assumptions', []))
    env = dict(os.environ, FAKE_WRITE=json.dumps(files), FAKE_DELETE=json.dumps(deletes), FAKE_EXTRA=json.dumps(extra),
               FAKE_MODE=entry.get('mode', 'completed'))
    sys.exit(subprocess.run([INNER, *args], input=raw, env=env).returncode)
head = brief.splitlines()[0].split()
skill, intent = head[2], head[3]
workspace = envelope['writable_paths'][0].rstrip('/')
artifact = workspace + ('/prd.md' if skill == 'bmad-prd' else '/ARCHITECTURE-SPINE.md')
memlog = workspace + '/.memlog.md'
status = entry.get('status', 'complete')
questions, conflicts = entry.get('questions', []), entry.get('conflicts', [])
files = []
if status != 'blocked':
    old = pathlib.Path(memlog).read_text() if pathlib.Path(memlog).is_file() else ''
    files = [[artifact, '# %s %s %s\n' % (skill, intent, key)], [memlog, old + 'log ' + key + '\n']]
workflow = {'status': status, 'intent': intent, ('prd' if skill == 'bmad-prd' else 'spine'): artifact,
            'memlog': memlog, 'open_questions': questions, 'assumptions': [],
            'conflicts_with_prior_decisions': conflicts}
result = 'blocked' if status == 'blocked' else 'needs-decision' if questions or conflicts else 'completed'
question = json.dumps({'conflicts': conflicts, 'open_questions': questions}, sort_keys=True,
                      separators=(',', ':')) if result == 'needs-decision' else None
extra = {'result': result, 'workflow_json': json.dumps(workflow), 'unresolved_question': question, 'options': [],
         'customization': [{'skill': skill, 'sha256': scenario['customization'][skill]}]}
env = dict(os.environ, FAKE_WRITE=json.dumps(files), FAKE_EXTRA=json.dumps(extra),
           FAKE_MODE=entry.get('mode', 'completed'))
sys.exit(subprocess.run([INNER, *args], input=raw, env=env).returncode)
'''

# Child driver: the real CLI in a child interpreter with named hook points. kill_at SIGKILLs the child at the n-th
# occurrence of a point (`point` or `point:n`, optionally filtered by one hook field: `point@field=value` or
# `point:n@field=value`, e.g. `after-action@action=bundle`); pause_at waits there for a release file. `after-anchor`
# wraps Coordinator.record_return, `before-claim-commit` the claim's event insert, `before-bind-assignment` the
# prepare's bind, `before-export-commit` Store.commit_export (the checkpoint is published, its pointer not yet
# committed) and `before-isolated-run` clients.run_isolated (a judgment's launch record is sealed, nothing spawned),
# so those kills fall inside the named window.
HARNESS = r'''
import json, os, signal, sys, time
sys.path.insert(0, os.environ['DRIVE_SOURCE'])
from coordinator_core import __main__ as cli, clients, drive, service, store
kill_at, pause_at = os.environ.get('DRIVE_KILL_AT', ''), os.environ.get('DRIVE_PAUSE_AT', '')
log = os.environ.get('DRIVE_HOOK_LOG')
seen, matched = {}, {}
def hit(spec, point, info):
    head, _, where = spec.partition('@')
    name, _, nth = head.partition(':')
    if name != point:
        return False
    if not where:
        return seen[point] == int(nth or 1)
    field, _, value = where.partition('=')
    if str(info.get(field)) != value:
        return False
    matched[spec] = matched.get(spec, 0) + 1
    return matched[spec] == int(nth or 1)
def hook(point, **info):
    seen[point] = seen.get(point, 0) + 1
    if log:
        with open(log, 'a') as out:
            out.write(json.dumps({'point': point, 'n': seen[point], 'pid': os.getpid(), **info}) + '\n')
    if kill_at and hit(kill_at, point, info):
        os.kill(os.getpid(), signal.SIGKILL)
    if pause_at and hit(pause_at, point, info):
        deadline = time.time() + 30
        while not os.path.exists(os.environ['DRIVE_PAUSE_RELEASE']) and time.time() < deadline:
            time.sleep(.02)
drive.HOOK = hook
def wrap(owner, attribute, point, when=lambda *a, **k: True):
    original = getattr(owner, attribute)
    def wrapped(*a, **k):
        if when(*a, **k):
            hook(point)
        return original(*a, **k)
    setattr(owner, attribute, wrapped)
wrapped_point = kill_at.partition('@')[0].partition(':')[0]
if wrapped_point == 'after-anchor':
    wrap(service.Coordinator, 'record_return', 'after-anchor')
if wrapped_point == 'before-claim-commit':
    wrap(service, '_event', 'before-claim-commit', lambda conn, kind, *a, **k: kind == 'owner.claimed')
if wrapped_point == 'before-bind-assignment':
    wrap(service.Coordinator, 'bind_assignment', 'before-bind-assignment')
if wrapped_point == 'before-export-commit':
    wrap(store.Store, 'commit_export', 'before-export-commit')
if wrapped_point == 'before-isolated-run':
    wrap(clients, 'run_isolated', 'before-isolated-run')
sys.argv = ['coordinator', *sys.argv[1:]]
sys.exit(cli.main())
'''


def pid_alive(pid: int) -> bool:
    """A process that exists and is not a zombie."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return text[text.rindex(")") + 2] != "Z"


def kill_group(pid: int) -> None:
    """Stop an orphaned fake worker group (not our child, so it cannot be waited) and wait until it is gone."""
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        pass
    deadline = time.monotonic() + 10
    while pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(.02)


class DriveCase(ReviewCase):
    """ReviewCase repository with the BMAD fixture; the fake `codex`/`claude` on PATH are the scenario wrapper; the
    attended owner has released (handoff), unless a test claims again."""

    def setUp(self):
        super().setUp()
        inner = self.root / "inner"
        inner.mkdir()
        for name, executable in (("codex-cli", "codex"), ("claude-code", "claude")):
            target = fake_clients.install(inner, name)
            wrapper = self.fake_dir / executable
            wrapper.write_text(WRAPPER.replace("__INNER__", repr(str(target)))
                               .replace("__CLAUDE__", fake_clients.FAKE_CLAUDE))
            wrapper.chmod(0o755)
        self.scenario_file = self.root / "scenario.json"
        self.launches = self.root / "launches.log"
        self.hooks = self.root / "hooks.log"
        self.pause_release = self.root / "pause.release"
        self.harness = self.root / "harness.py"
        self.harness.write_text(HARNESS)
        environ = patch.dict(os.environ, {"DRIVE_SCENARIO": str(self.scenario_file),
                                          "DRIVE_LAUNCH_LOG": str(self.launches),
                                          "PYTHONDONTWRITEBYTECODE": "1"})
        environ.start()
        self.addCleanup(environ.stop)
        skills = contract.discover(self.t.path).contract["resolved"]["skills"]
        self.customization = {skill: skills[skill] for skill in ("bmad-prd", "bmad-architecture")}
        self.scenario()
        self.drive_token = self.root / "drive.token"
        self.drive_repo = self.t.path
        self.coordinator.release(self.owner)  # orderly handoff (D2): no assignments yet
        self.children = []
        self.addCleanup(self._reap)

    def _reap(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.wait()
            for stream in (child.stdout, child.stderr):
                if stream is not None:
                    stream.close()

    # Scenario and pipeline -------------------------------------------------------------------------------------
    def scenario(self, steps=None, keys=None, default=None, judgments=None, version_fail=None, customization=None,
                 stories=None):
        value = {"customization": customization or self.customization, "steps": steps or {}, "keys": keys or {},
                 "judgments": judgments or {}, "version_fail": version_fail or [], "stories": stories or {}}
        if default is not None:
            value["default"] = default
        self.scenario_file.write_text(json.dumps(value))

    def pipeline(self, steps=None, **over):
        value = {"kind": "drive-pipeline", "format": 1, "feature_id": self.fid, "mandate_revision": 1,
                 "roles": {"worker": {"client": "codex-cli", "model": "fake-model", "timeout": 30}},
                 "budgets": {"max_updates": 2, "max_judgments": 0},
                 "steps": steps if steps is not None else [
                     {"id": "compat", "kind": "compat"},
                     {"id": "prd", "kind": "author", "skill": "bmad-prd", "slug": "gitops", "author_id": "prd-author",
                      "inputs": {"docs/input.md": "driving-brief"}}]}
        value.update(over)
        return value

    def pipeline_file(self, value=None, name="pipeline.json"):
        path = self.root / name
        path.write_text(json.dumps(value if value is not None else self.pipeline()))
        return path

    # Processes -------------------------------------------------------------------------------------------------
    def child_env(self, **extra):
        return {**os.environ, "PYTHONPATH": str(COORDINATOR), "PYTHONDONTWRITEBYTECODE": "1",
                "DRIVE_SOURCE": str(COORDINATOR), **extra}

    def run_cli(self, *args, data=None, code=None, timeout=120, wrapper=False):
        """One CLI process; returns (exit code, stdout JSON or None, stderr JSON or None)."""
        command = [sys.executable, str(self.t.path / ".bmad-coordinator" / "run.py")] if wrapper else \
            [sys.executable, "-m", "coordinator_core"]
        result = subprocess.run([*command, "--repo", str(self.drive_repo), *args],
                                input=json.dumps(data) if data is not None else None, capture_output=True, text=True,
                                cwd=COORDINATOR, env=self.child_env(), timeout=timeout)
        if code is not None:
            self.assertEqual(code, result.returncode, (result.stdout, result.stderr))
        for text in ("Warning", "Exception ignored", "Traceback"):
            self.assertNotIn(text, result.stderr)
        parse = lambda text: json.loads(text) if text.strip() else None  # noqa: E731
        return result.returncode, parse(result.stdout), parse(result.stderr)

    def child_driver(self, args, kill_at=None, pause_at=None, stdin=None):
        env = self.child_env(DRIVE_HOOK_LOG=str(self.hooks), DRIVE_PAUSE_RELEASE=str(self.pause_release))
        if kill_at:
            env["DRIVE_KILL_AT"] = kill_at
        if pause_at:
            env["DRIVE_PAUSE_AT"] = pause_at
        child = subprocess.Popen([sys.executable, str(self.harness), "--repo", str(self.drive_repo), *args],
                                 stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=COORDINATOR, env=env)
        self.children.append(child)
        if stdin is not None:
            child.stdin.write(stdin)
            child.stdin.close()
            child.stdin = None   # Python 3.11's communicate() flushes a still-set stdin and fails on a closed one
        return child

    def finish(self, child, timeout=120):
        """Wait for a child driver; returns (exit code, stdout JSON or None, stderr text)."""
        out, err = child.communicate(timeout=timeout)
        for text in ("Warning", "Exception ignored", "Traceback"):
            self.assertNotIn(text, err)
        return child.returncode, (json.loads(out) if out.strip() else None), err

    def start_args(self, pipeline=None, token=None, *extra):
        return ["drive", "start", "--feature", self.fid, "--pipeline", str(pipeline or self.pipeline_file()),
                "--token-file", str(token or self.drive_token), *extra]

    def resume_args(self, token=None, *extra):
        return ["drive", "resume", "--token-file", str(token or self.drive_token), *extra]

    def drive_start(self, *extra, code=None, pipeline=None, token=None):
        return self.run_cli(*self.start_args(pipeline, token, *extra), code=code)

    def drive_resume(self, *extra, code=None, token=None):
        return self.run_cli(*self.resume_args(token, *extra), code=code)

    def wait_for(self, predicate, timeout=30, message="condition"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(.02)
        self.fail(f"timed out waiting for {message}")

    def launch_log(self) -> list[dict]:
        return [json.loads(line) for line in self.launches.read_text().splitlines()] if self.launches.exists() else []

    def hook_log(self) -> list[dict]:
        return [json.loads(line) for line in self.hooks.read_text().splitlines()] if self.hooks.exists() else []

    def wait_launches(self, count, timeout=30):
        return self.wait_for(lambda: len(self.launch_log()) >= count and self.launch_log(), timeout, "launch")

    def block_file(self, name="block"):
        return str(self.root / f"{name}.release")

    def unblock(self, name="block"):
        Path(self.block_file(name)).write_text("go\n")

    # State -----------------------------------------------------------------------------------------------------
    def driver_owner(self, token=None) -> OwnerRef:
        return OwnerRef(**json.loads((token or self.drive_token).read_text()))

    def snapshot(self):
        return self.store.read_snapshot()

    def rows(self, table, **match):
        return [row for row in self.snapshot()[table] if all(row[key] == value for key, value in match.items())]

    def prd_rows(self):
        return [row for row in self.snapshot()["assignments"] if row["feature_id"] == self.fid]

    def open_request(self):
        return next(row["id"] for row in self.snapshot()["decision_requests"] if row["state"] == "open")

    def decision_message(self, request_id, **over):
        value = {"feature_id": self.fid, "request_id": request_id, "choice": {"answer": "EU region"},
                 "reason": "operator answer", "source_ref": "operator:chat-1"}
        value.update(over)
        return value

    def inbox_add(self, kind, data, code=0):
        return self.run_cli("inbox", "add", kind, data=data, code=code)[1]

    def lock_entries(self):
        """/proc/locks lines naming coordinator/drive.lock (read only, never acquiring)."""
        info = (self.base_dir / "drive.lock").stat()
        device = f"{os.major(info.st_dev):02x}:{os.minor(info.st_dev):02x}:{info.st_ino}"
        return [line.split() for line in Path("/proc/locks").read_text().splitlines()
                if "->" not in line and line.split()[5] == device]

    def summary_file(self, summary) -> Path:
        return self.base_dir / "drives" / summary["lease_id"] / summary["run_id"] / "summary.json"


# Unattended evidence -------------------------------------------------------------------------------------------------

CRITERIA = ("driver-claimed-generation", "worker-observed-complete", "judgment-backed-decision", "update-observed",
            "review-observed-bound", "acceptance-current", "zero-interventions-inbox-empty", "drive-events-anchored",
            "one-launch-per-key", "no-duplicate-after-restart", "correction-observed",
            "attempt-committed", "verify-observed", "status-synced", "deferred-recorded", "integration-current")


def _observed(row) -> bool:
    return bool(row["return_json"]) and json.loads(row["return_json"]).get("provenance_class") == "observed"


def unattended_checks(repo, lease_ids, epic_id=None) -> dict[str, str]:
    """Score the unattended evidence of the given drive leases, read only from the store, the anchored sealed files
    and the receipts (it writes nothing): each criterion is `pass`, `fail` (the evidence contradicts it, including
    anchored evidence that no longer reads back), `blocked` (the evidence is missing) or, for the restart, correction
    and deferred-work criteria only, `trivial` (see below). The later live smoke imports
    this function unchanged; it proves only what the records show, never what a model read or understood.
    `no-duplicate-after-restart` is `trivial` when no lease ran more than once (nothing was restarted, so the absence
    of duplicates proves nothing about restarts) and `pass` only after a restart. `correction-observed` is `trivial`
    when no correction attempt exists. The story criteria (implementation drive) read the anchored story records of
    a single story feature and are `blocked` without an implementation attempt (a planning run): `attempt-committed`
    (every terminal implementation attempt has a commit record, a sha or `empty`), `verify-observed` (the commit of
    every completed attempt has a verify record of its sha; a `needs-decision` attempt is committed, never verified),
    `status-synced` (the `done` status record's sha is on the branch; a spec acceptance named by a `spec` status
    record whose commit is on the branch counts as current for `acceptance-current`, design §7 F10) and
    `deferred-recorded` (`trivial` without judgment-backed deferred items or accepted open highs, else the `done` status
    record carries one entry for each). For `driver-claimed-generation`, a lease of a story inside an epic names the
    epic's session `epic:<id>` and carries `epic_id` (design epic-drive E1), every record of a lease follows exactly one
    claim of its generation, and each run's generation is the generation of the lease current when it started (after
    an E12 reseal the earlier runs stay valid).
    `integration-current` (design epic-drive §5, with `epic_id`): every `done` story's recorded head is an ancestor of
    the integration branch's head, and from the newest done story's head the first-parent chain reaches that head only
    through commits by the template's `project.commit_identity` whose subjects start with `epic(<N>):`; `blocked`
    without an epic or without a done story.
    A story record that no longer reads back is dropped by `story_facts` (never an older one instead), so it scores
    `blocked`, not `fail`; an implementation binding that no longer reads back scores `fail`."""
    lease_ids = list(lease_ids)
    try:
        store = open_store(repo)
        snapshot = store.read_snapshot()
    except DomainError:
        return dict.fromkeys(CRITERIA, "blocked")
    base, events = store.path.parent, snapshot["events"]
    leased = {row["subject_id"]: row for row in events
              if row["kind"] == "drive.leased" and row["subject_id"] in lease_ids}
    features = {row["source_id"] for row in leased.values()}
    runs = [row for row in events if row["kind"] == "drive.started" and row["subject_id"] in lease_ids]
    ends = [row for row in events if row["kind"] in ("drive.ended", "drive.stop-acknowledged") and
            row["subject_id"] in lease_ids]
    rows = [row for row in snapshot["assignments"] if row["feature_id"] in features]
    bundles = {row["id"]: row for row in snapshot["bundles"] if row["feature_id"] in features}
    reviews = [row for row in snapshot["reviews"] if row["bundle_id"] in bundles]
    sealed = lambda row: drive_pipeline.read_sealed_json(base, row["evidence_ref"], row["payload_digest"])  # noqa: E731

    def bound(profile):
        found = {}
        for row in rows:
            if row["state"] == "cancelled-before-launch":
                continue
            try:
                name, anchor = binding_anchor(events, row["id"])
            except DomainError:
                continue
            if name == profile and anchor is not None:
                found[row["id"]] = (row, read_binding(base, anchor["evidence_ref"], anchor["payload_digest"]))
        return found

    def claimed():
        if not lease_ids or set(lease_ids) - set(leased):
            return "blocked"
        for lease_id in leased:
            history = sorted((row for row in events if row["kind"] == "drive.leased" and row["subject_id"] == lease_id),
                             key=lambda row: row["revision"])
            for row in history:   # every record of the lease, an E12 reseal included
                lease = sealed(row)
                claims = [item for item in events if item["kind"] == "owner.claimed" and
                          item["subject_id"] == str(lease["generation"]) and item["revision"] < row["revision"]]
                session = f"epic:{lease['epic_id']}" if lease.get("epic_id") else f"drive:{lease_id}"   # E1
                if lease.get("lease_id") != lease_id or lease.get("session_id") != session or len(claims) != 1:
                    return "fail"
            started = [item for item in runs if item["subject_id"] == lease_id]
            if not started:
                return "blocked"
            for item in started:   # the lease current when the run started decides its generation (E12)
                current = [row for row in history if row["revision"] < item["revision"]]
                if not current or sealed(current[-1])["generation"] != sealed(item)["generation"]:
                    return "fail"
        return "pass"

    def worker_complete():
        done = [row for row, _ in (bound(PLAN) | bound(IMPLEMENTATION)).values()
                if row["state"] == "completed-claim" and _observed(row)]
        for row in done:
            historical_return(repo, store, row["id"])
        return "pass" if done else "blocked"

    def judged():
        found = [item for feature in sorted(features) for item in judgment.decisions(snapshot, feature)]
        if not found:
            return "blocked"
        requests = {row["id"]: row for row in snapshot["decision_requests"]}
        revisions = {row["id"]: row["mandate_revision"] for row in snapshot["features"]}
        decisions = {row["id"]: row for row in snapshot["decisions"]}
        for item in found:
            request, found_verdict = requests.get(item["request_id"]), judgment.verdict(store, item["request_id"])
            if request is None or request["state"] != "resolved" or request["resolution_decision_id"] != item["id"]:
                return "fail"
            # `valid` is checked against the current mandate; a decision recorded at an earlier revision (a spec
            # question judged before the carry, live hook 21 F20) was valid when `record` refused anything else
            decision = decisions[item["id"]]
            current = decision["mandate_revision"] == revisions.get(decision["feature_id"])
            if not found_verdict or found_verdict["key"] != item["judgment_key"] or \
               found_verdict["state"] != "observed" or found_verdict["verdict"] not in ("answer", "defer") or \
               (current and not found_verdict["valid"]) or \
               found_verdict.get("mandate_revision") != decision["mandate_revision"] or \
               (found_verdict["receipt_ref"], found_verdict["receipt_sha256"]) != \
               (item["receipt_ref"], item["receipt_sha256"]):
                return "fail"
            returned = drive_pipeline.read_sealed_json(base, item["receipt_ref"], item["receipt_sha256"])
            launch = json.loads(_anchored_bytes(base, f"judgments/{item['judgment_key']}/receipts/launch.json",
                                                returned["receipts"]["launch.json"], ("judgments",)))
            if launch.get("kind") != "isolated-launch" or launch.get("client") not in clients.REGISTRY or \
               returned.get("client") != launch.get("client"):
                return "fail"
        return "pass"

    def updated():
        updates = [(row, binding) for row, binding in (bound(PLAN) | bound(IMPLEMENTATION)).values()
                   if binding.get("intent") == "update" and (binding.get("continues") or {}).get("request")]
        done = [row for row, _ in updates if row["state"] == "completed-claim" and _observed(row)]
        for row in done:
            historical_return(repo, store, row["id"])
        return "pass" if done else "blocked"

    def reviewed():
        observed = [row for row in reviews if row["provenance_class"] == "observed"]
        if not observed:
            return "blocked"
        bindings = bound(REVIEW)
        for row in observed:
            reviewer = next((item for item in rows if row["provenance_ref"] ==
                             receipt_ref(run_name(item["id"], item["request_fingerprint"]), "return")), None)
            if reviewer is None or reviewer["id"] not in bindings or \
               bindings[reviewer["id"]][1]["bundle_id"] != row["bundle_id"]:
                return "fail"
            bmad_review._prove(repo, store, reviewer["id"], bmad_review._evidence_from_row(row), live=False)
        return "pass"

    def on_branch(feature_id, sha) -> bool:
        """The feature worktree's HEAD is `sha` or descends from it."""
        feature = next(row for row in snapshot["features"] if row["id"] == feature_id)
        try:
            subprocess.run(["git", "-C", feature["worktree"], "merge-base", "--is-ancestor", sha, "HEAD"], check=True,
                           capture_output=True)
        except (OSError, subprocess.CalledProcessError):
            return False
        return True

    def accepted():
        mine = {row["id"]: bundles[row["bundle_id"]]["feature_id"] for row in snapshot["acceptances"]
                if row["bundle_id"] in bundles}
        if not mine:
            return "blocked"
        states = {item["acceptance_id"]: item["current"] for item in Coordinator(store).verify().checked}

        def carried(acceptance_id, feature_id) -> bool:
            """Design §7 F10: the spec acceptance a `spec` status record on the branch names (stale after the carry)."""
            spec = implementation.story_facts(base, snapshot, feature_id)["status"].get("spec") or {}
            return (spec.get("result") or {}).get("acceptance_id") == acceptance_id and \
                on_branch(feature_id, spec.get("sha"))

        return "pass" if all(states.get(item) == "current" or
                             (states.get(item) == "stale" and carried(item, feature_id))
                             for item, feature_id in mine.items()) else "fail"

    def quiet():
        if not ends:
            return "blocked"
        if any(sealed(row)["interventions"] for row in ends):
            return "fail"
        decisions = [row for row in snapshot["decisions"] if row["feature_id"] in features]
        if any(row["source_ref"].startswith("inbox/") for row in decisions) or inbox.listing(repo, store):
            return "fail"
        mine = {row["id"] for row in rows}
        if any(row["kind"] == "assignment.resolved-unknown" and row["subject_id"] in mine for row in events):
            return "fail"
        first = min(row["revision"] for row in leased.values())
        generations = {str(sealed(row)["generation"]) for row in leased.values()}
        if any(row["kind"] == "owner.claimed" and row["revision"] > first and row["subject_id"] not in generations
               for row in events):
            return "fail"
        return "pass"

    def anchored():
        if not any(row["kind"] in DRIVE_EVENTS and row["subject_id"] in lease_ids for row in events):
            return "blocked"
        for row in events:
            if row["kind"] in DRIVE_EVENTS:
                _anchored_bytes(base, row["evidence_ref"], row["payload_digest"], ANCHOR_TOPS)
        return "pass"

    def launched_once():
        launches = Counter(row["subject_id"] for row in events if row["kind"] == "assignment.launch-reserved")
        ran = [row for row in rows if row["state"] not in ("prepared", "cancelled-before-launch")]
        if not ran:
            return "blocked"
        if any(launches[row["id"]] != 1 for row in ran) or any(launches[row["id"]] for row in rows if row not in ran):
            return "fail"
        keys = {key for key, row in judgment.reservations(snapshot).items()
                if any(item["id"] == row["source_id"] and item["feature_id"] in features
                       for item in snapshot["decision_requests"])}
        ended = Counter(row["subject_id"] for row in events if row["kind"] in judgment.TERMINAL)
        return "fail" if any(ended[key] > 1 for key in keys) else "pass"

    def unduplicated():
        if not runs:
            return "blocked"
        restarted = any(count > 1 for count in Counter(row["subject_id"] for row in runs).values())
        plans, checks, attempts = bound(PLAN), bound(REVIEW), bound(IMPLEMENTATION)
        mine = {row["id"] for row in rows}
        counts = [
            Counter(row["request_id"] for row in snapshot["decisions"] if row["feature_id"] in features and
                    row["request_id"]),
            Counter((binding["skill"], binding["workspace"]) for _, binding in plans.values()
                    if binding.get("intent") == "create"),
            Counter((binding.get("continues") or {}).get("update_of") for _, binding in (plans | attempts).values()
                    if binding.get("intent") == "update"),
            # one commit record per attempt, one verify record per (non-empty) commit, one status record per phase
            Counter(row["subject_id"] for row in events if row["kind"] == "story.committed" and
                    row["subject_id"] in mine),
            Counter(row["subject_id"] for row in events if row["kind"] == "story.verified" and
                    row["subject_id"] in mine),
            Counter((row["subject_id"], row["source_id"]) for row in events if row["kind"] == "story.status" and
                    row["subject_id"] in features),
            Counter(binding["bundle_id"] for _, binding in checks.values()),
            Counter(row["bundle_id"] for row in reviews if row["provenance_class"] == "observed"),
            Counter(row["provenance_ref"] for row in reviews),
            Counter(row["bundle_id"] for row in snapshot["acceptances"] if row["bundle_id"] in bundles),
            Counter(row["source_assignments_json"] for row in bundles.values()),
            Counter(row["source_id"] for row in judgment.reservations(snapshot).values()
                    if row["source_id"] in {item["id"] for item in snapshot["decision_requests"]
                                            if item["feature_id"] in features}),
        ]
        if any(value > 1 for count in counts for value in count.values()):
            return "fail"
        return "pass" if restarted else "trivial"

    def corrected():
        """Design D7 (ruling R7): `trivial` without a correction attempt; `pass` when every correction's binding names
        an observed review of a bundle sourced from its `update_of` (a story correction of a red verify instead names
        the anchored failing verify record of its `update_of`), its chain ends in an observed completed attempt and a
        later bundle names that end. The end is the correction itself when it completed directly; otherwise (it asked)
        the newest observed completed attempt whose `continues.update_of` chain, walked through the bound planning
        and implementation bindings, reaches the correction. `fail` when a binding contradicts that; `blocked` while
        no such end exists or no bundle names it, and without a drive run (missing evidence proves no absence of
        corrections)."""
        if not runs:
            return "blocked"
        plans = bound(PLAN) | bound(IMPLEMENTATION)
        corrections = [(row, binding) for row, binding in plans.values()
                       if any((binding.get("continues") or {}).get(key)
                              for key in ("corrects_review", "corrects_verify"))]
        if not corrections:
            return "trivial"
        observed = {row["id"]: row for row in reviews if row["provenance_class"] == "observed"}
        prepared = {row["subject_id"]: row["revision"] for row in events if row["kind"] == "assignment.prepared"}

        def reaches(assignment_id, target):
            while assignment_id in plans and assignment_id != target:
                assignment_id = (plans[assignment_id][1].get("continues") or {}).get("update_of")
            return assignment_id == target

        for row, binding in corrections:
            continues = binding["continues"]
            if continues.get("corrects_verify"):
                verify = implementation.story_facts(base, snapshot, row["feature_id"])["verifies"].get(
                    continues["update_of"])
                if verify is None or verify["ref"] != continues["corrects_verify"] or verify.get("outcome") != "fail":
                    return "fail"
            else:
                review = observed.get(continues["corrects_review"])
                if review is None or "/" in continues.get("finding_ids", []) or continues["update_of"] not in \
                   json.loads(bundles[review["bundle_id"]]["source_assignments_json"]):
                    return "fail"
            ends = sorted((prepared[item["id"]], item["id"]) for item, _ in plans.values()
                          if item["state"] == "completed-claim" and _observed(item) and reaches(item["id"], row["id"]))
            if not ends:
                return "blocked"
            end = row["id"] if row["state"] == "completed-claim" and _observed(row) else ends[-1][1]
            historical_return(repo, store, end)
            if not any(end in json.loads(item["source_assignments_json"]) for item in bundles.values()):
                return "blocked"
        return "pass"

    memo = {}

    def story_evidence():
        """The implementation attempts and the story facts, read once inside the checks, so a binding that no longer
        reads back fails each story criterion instead of the whole scoring. Without an implementation attempt (a
        planning run) there is no story evidence: the story criteria are `blocked`."""
        if not memo:
            attempts = bound(IMPLEMENTATION)
            memo["value"] = attempts, implementation.story_facts(base, snapshot, next(iter(features))) \
                if len(features) == 1 and attempts else None
        return memo["value"]

    def committed():
        attempts, story = story_evidence()
        terminal = [row for row, _ in attempts.values() if row["state"] in TERMINAL_STATES]
        if story is None or not terminal:
            return "blocked"
        for row in terminal:
            record = story["commits"].get(row["id"])
            if record is None:
                return "blocked"
            if not record.get("empty") and not record.get("sha"):
                return "fail"
        return "pass"

    def verified():
        attempts, story = story_evidence()
        if story is None:
            return "blocked"
        completed = {row["id"] for row, _ in attempts.values() if row["state"] == "completed-claim"}
        commits = [record for record in story["commits"].values()
                   if record.get("sha") and record.get("assignment_id") in completed]
        if not commits:
            return "blocked"
        for record in commits:
            verify = story["verifies"].get(record["assignment_id"])
            if verify is None:
                return "blocked"
            if verify.get("commit_sha") != record["sha"]:
                return "fail"
        return "pass"

    def synced():
        _, story = story_evidence()
        if story is None or "done" not in story["status"]:
            return "blocked"
        return "pass" if on_branch(next(iter(features)), story["status"]["done"].get("sha")) else "fail"

    def listed(row) -> int:
        """The deferred items of a decision as the driver writes them to the ledger: only a judgment-backed choice
        (an object with a `judgment` object) carrying a `deferred` list; any other choice carries none."""
        choice = json.loads(row["choice_json"])
        if not isinstance(choice, dict) or not isinstance(choice.get("judgment"), dict):
            return 0
        return len(choice["deferred"]) if isinstance(choice.get("deferred"), list) else 0

    def deferred():
        _, story = story_evidence()
        if story is None:
            return "blocked"
        items = sum(listed(row) for row in snapshot["decisions"]
                    if row["feature_id"] in features and row["source_kind"] == "coordinator")
        highs = 0
        for acceptance in snapshot["acceptances"]:
            if acceptance["bundle_id"] in bundles:
                review = next(row for row in reviews if row["id"] == acceptance["review_id"])
                highs += len(drive_pipeline.open_high_findings(review["finding_json"]))
        if not items and not highs:
            return "trivial"
        done = story["status"].get("done")
        if done is None:
            return "blocked"
        return "pass" if len(done.get("entries") or []) == items + highs else "fail"

    def integration_current():
        if epic_id is None:
            return "blocked"
        states = [row for row in events if row["kind"] in ("epic.started", "epic.advanced") and
                  row["subject_id"] == epic_id]
        if not states:
            return "blocked"
        git = ["git", "-C", str(repo.worktree)]
        try:
            state = sealed(states[-1])   # the newest anchored state; one that no longer reads back is `fail`
            done = [item for item in state.get("stories", []) if item["state"] == "done" and item.get("head_sha")]
            if not done:
                return "blocked"
            template = drive_pipeline.read_sealed_json(base, state["template_ref"], state["template_sha256"])
            head = subprocess.run([*git, "rev-parse", "--verify", f"refs/heads/{state['branch']}^{{commit}}"],
                                  check=True, capture_output=True, text=True).stdout.strip()
            for item in done:
                subprocess.run([*git, "merge-base", "--is-ancestor", item["head_sha"], head], check=True,
                               capture_output=True)
            chain = subprocess.run([*git, "log", "--first-parent", "--format=%an%x00%ae%x00%s",
                                    f"{done[-1]['head_sha']}..{head}"], check=True, capture_output=True,
                                   text=True).stdout
        except (OSError, subprocess.CalledProcessError, DomainError):
            return "fail"
        identity = template["project"]["commit_identity"]
        for line in chain.splitlines():
            name, email, subject = (line.split("\0") + ["", ""])[:3]
            if (name, email) != (identity["name"], identity["email"]) or \
               not subject.startswith(f"epic({state['epic']}):"):
                return "fail"
        return "pass"

    result = {}
    for name, check in zip(CRITERIA, (claimed, worker_complete, judged, updated, reviewed, accepted, quiet, anchored,
                                      launched_once, unduplicated, corrected, committed, verified, synced, deferred,
                                      integration_current)):
        try:
            result[name] = check()
        except (DomainError, OSError, KeyError, TypeError, ValueError):  # anchored evidence that does not read back
            result[name] = "fail"
    return result


MERGE_ORDER = ("fail", "pass", "trivial", "blocked")


def epic_checks(repo, epic_id: str) -> dict[str, str]:
    """Design epic-drive §7 F17: the unattended evidence of an epic, scored story by story. Every story with a feature
    in the epic's newest anchored state is scored by `unattended_checks` from that story's own worktree with the
    leases of its feature (the anchored `drive.leased` records), because its attempts, receipts and returns ran
    there; the criteria merge per name in MERGE_ORDER (`fail` wins, then `pass`, `trivial`, `blocked`), so one
    story's contradiction fails the epic while a criterion no story has evidence for stays `blocked`.
    `integration-current` reads the shared refs from any worktree. A story whose worktree no longer resolves scores
    `blocked` throughout (missing evidence); `blocked` throughout without a leased story."""
    try:
        store = open_store(repo)
        snapshot = store.read_snapshot()
        state = drive_epic.newest_state(store, epic_id)
    except DomainError:
        return dict.fromkeys(CRITERIA, "blocked")
    features = {row["id"]: row for row in snapshot["features"]}
    leases = {}
    for item in state.get("stories", []):
        if item.get("feature_id") in features:
            leases[item["feature_id"]] = sorted({row["subject_id"] for row in snapshot["events"]
                                                 if row["kind"] == "drive.leased" and
                                                 row["source_id"] == item["feature_id"]})
    scored = []
    for feature_id, lease_ids in sorted(leases.items()):
        if not lease_ids:
            continue
        try:
            story_repo = resolve_repo(Path(features[feature_id]["worktree"]))
        except DomainError:
            scored.append(dict.fromkeys(CRITERIA, "blocked"))
            continue
        scored.append(unattended_checks(story_repo, lease_ids, epic_id))
    if not scored:
        return dict.fromkeys(CRITERIA, "blocked")
    return {name: next(value for value in MERGE_ORDER if any(found[name] == value for found in scored))
            for name in CRITERIA}
