import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from coordinator_core import profiles, service, worker
from coordinator_core.exporter import export_checkpoint, import_checkpoint
from coordinator_core.model import (AssignmentInput, BindingEvidence, BundleInput, DecisionInput, DomainError,
                                    FeatureInput, LaunchEvidence, ReturnEvidence, ReviewEvidence)
from coordinator_core.profiles import ReturnProfile
from coordinator_core.repo import resolve_repo
from coordinator_core.run_receipt import (ReceiptEvidenceSource, canonical, read_binding, run_name, run_root,
                                          seal_binding)
from coordinator_core.service import Coordinator
from coordinator_core.store import Store, open_store
from tests.support import ext4_mountinfo


ROOT = Path(__file__).parents[1]
PLAN, REVIEW = "bmad-planning-v1", "bmad-review-v1"


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
cwd = pathlib.Path(args[args.index('-C') + 1])
last = pathlib.Path(args[args.index('--output-last-message') + 1])
envelope = json.loads(sys.stdin.read())
with pathlib.Path(os.environ['FAKE_COUNT']).open('a') as out:
    out.write('spawn\n')
mode = os.environ.get('FAKE_MODE', 'completed')
if mode == 'sleep':
    time.sleep(30)
    sys.exit(0)
if os.environ.get('FAKE_ENV_OUT'):
    pathlib.Path(os.environ['FAKE_ENV_OUT']).write_text(os.environ.get('BMAD_TEST', 'absent'))
claimed = []
for path, text in json.loads(os.environ.get('FAKE_WRITE', '[]')):
    target = cwd / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    claimed.append(path)
for path, link in json.loads(os.environ.get('FAKE_LINK', '[]')):
    (cwd / path).parent.mkdir(parents=True, exist_ok=True)
    (cwd / path).symlink_to(link)
    claimed.append(path)
for path in json.loads(os.environ.get('FAKE_CHMOD', '[]')):
    os.chmod(cwd / path, 0o755)
    claimed.append(path)
for path in json.loads(os.environ.get('FAKE_DELETE', '[]')):
    (cwd / path).unlink()
    claimed.append(path)
payload = dict(assignment_id=envelope['assignment_id'], request_fingerprint=envelope['request_fingerprint'],
               result='completed', changed_paths=claimed, checks=[], descendants=[],
               unresolved_question=None, options=[], affected_ids=[])
payload.update(json.loads(os.environ.get('FAKE_EXTRA', '{}')))
if mode == 'blocked':
    payload['result'] = 'blocked'
if mode == 'needs-decision':
    payload['result'] = 'needs-decision'
    payload['unresolved_question'] = 'Which option?'
last.write_text('not json' if mode == 'bad-json' else json.dumps(payload))
print(json.dumps({'type': 'thread.started', 'thread_id': 'thread-1'}), flush=True)
print(json.dumps({'type': 'turn.completed'}), flush=True)
'''


class Observed:
    def classify_launch(self, event):
        return "observed"

    def classify_return(self, event):
        return "observed"

    def classify_review(self, event):
        return "observed"


class Recorder:
    def __init__(self):
        self.calls = []
        self.reasons = {}

    def gate(self, stage, ctx):
        self.calls.append((stage, ctx["assignment"]["id"]))
        return list(self.reasons.get(stage, []))

    def profile(self, name=PLAN, env=None):
        def check(final, assignment, binding, run_dir, observed_changed):
            return {"tag": final["tag"], "changed": list(observed_changed)}
        return ReturnProfile(name, {"tag": {"type": "string"}}, env or {}, check, self.gate)


def writes(*pairs):
    return json.dumps([list(pair) for pair in pairs])


class CoreBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        mounts = patch("coordinator_core.store.read_mountinfo", return_value=ext4_mountinfo(self.root))
        mounts.start()
        self.addCleanup(mounts.stop)
        self.main = self.root / "main"
        self.main.mkdir()
        git(self.main, "init", "-q")
        git(self.main, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "commit", "--allow-empty", "-qm", "initial")
        (self.main / "AGENTS.md").write_text("Local fixture.\n")
        from install import apply, preview
        apply(self.main, preview(self.main).sha256)
        git(self.main, "add", "AGENTS.md", ".bmad-coordinator")
        git(self.main, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "installed")
        self.base = git(self.main, "rev-parse", "HEAD")
        self.fake_dir = self.root / "bin"
        self.fake_dir.mkdir()
        fake = self.fake_dir / "codex"
        fake.write_text(FAKE)
        fake.chmod(0o755)
        self.count = self.root / "count.log"
        self.env = {"PATH": str(self.fake_dir) + os.pathsep + os.environ["PATH"], "FAKE_COUNT": str(self.count)}
        self.token = self.root / "owner.token"
        self.t = self.tree("feature", claim=True)
        self.coordinator = Coordinator(self.store)
        self.base_dir = self.t.repo.common_dir / "coordinator"
        self.recorder = Recorder()

    def tree(self, name, claim=False, scopes=None):
        path = self.root / name
        git(self.main, "worktree", "add", "-qb", name, str(path))
        (path / "brief.md").write_text("Do the assigned work.\n")
        (path / "contract.md").write_text("Contract.\n")
        repo = resolve_repo(path)
        if claim:
            self.store = open_store(repo, create=True)
            self.owner = Coordinator(self.store).claim("test-owner", self.token)
        feature_id = self.add_feature(SimpleNamespace(path=path), name, scopes)
        return SimpleNamespace(path=path, repo=repo, feature_id=feature_id, name=name)

    def add_feature(self, tree, branch, scopes=None):
        return Coordinator(self.store).create_feature(self.owner, FeatureInput(
            "ship result", [], [], ["review"], ["worker only"], branch, self.base, self.base, str(tree.path), [],
            scopes or ["result.md", "brief.md", "contract.md", "input.md", "other.md", "plan/**"]))

    def entry(self, t, path):
        return {"path": path, "digest": digest((t.path / path).read_bytes())}

    def spec(self, t=None, brief="brief.md", contract="contract.md", **changes):
        t = t or self.t
        contracts = [self.entry(t, contract)]
        data = dict(feature_id=t.feature_id, idempotency_key="one", brief_ref=brief, mandate_revision=1,
                    source_digests=[self.entry(t, brief)], contract_files=contracts,
                    contract_digest=digest(json.dumps(contracts, sort_keys=True, separators=(",", ":")).encode()),
                    writable_paths=["result.md"], permission_mode="workspace-write", author_id="worker",
                    depends_on=[], blocking_requests=[])
        data.update(changes)
        return AssignmentInput(**data)

    def assign(self, t=None, required_profile=None, **changes):
        spec = self.spec(t, **changes)
        if required_profile is None:
            return self.coordinator.create_assignment(self.owner, spec)
        return self.coordinator.create_assignment(self.owner, spec, required_profile=required_profile)

    def use(self, profile=None):
        profile = profile or self.recorder.profile()
        patcher = patch.dict(profiles.REGISTRY, {profile.name: profile})
        patcher.start()
        self.addCleanup(patcher.stop)

    def bind(self, assignment, t=None, name=PLAN, **extra):
        t = t or self.t
        row = self.coordinator.detail("assignment", assignment)
        value = {"kind": "binding", "format": 1, "profile": name, "assignment_id": assignment,
                 "request_fingerprint": row["request_fingerprint"], "worktree": str(t.path),
                 "common_dir": str(t.repo.common_dir), **extra}
        evidence = seal_binding(self.base_dir, value)
        self.coordinator.bind_assignment(self.owner, assignment, evidence)
        return evidence

    def run_it(self, assignment, t=None, timeout=2, **env):
        t = t or self.t
        with patch.dict(os.environ, {**self.env, **env}):
            return worker.run_assignment(t.repo, self.store, self.owner, assignment, "fake-model", timeout)

    def state(self, assignment):
        return self.coordinator.detail("assignment", assignment)["state"]

    def code(self, expected, action, *args, **kwargs):
        with self.assertRaises(DomainError) as caught:
            action(*args, **kwargs)
        self.assertEqual(expected, caught.exception.code, caught.exception.message)
        return caught.exception

    def cli(self, *args, t=None, data=None, code=0):
        t = t or self.t
        result = subprocess.run([sys.executable, str(t.path / ".bmad-coordinator" / "run.py"),
                                 "--repo", str(t.path), *args],
                                input=json.dumps(data) if data is not None else None, capture_output=True,
                                text=True, env={**os.environ, **self.env})
        self.assertEqual(code, result.returncode, (result.stdout, result.stderr))
        return result

    def decide(self, request_id):
        return self.coordinator.record_decision(self.owner, DecisionInput(
            self.t.feature_id, 1, "coordinator", "clause:1", "within mandate", {"choice": "A"}, request_id))

    def bundle(self, artifacts, sources, t=None):
        t = t or self.t
        return self.coordinator.capture_bundle(self.owner, BundleInput(
            t.feature_id, artifacts, [], sources, [self.entry(t, "contract.md")]))

    def request_of(self, assignment):
        return next(row["id"] for row in self.store.read_snapshot()["decision_requests"]
                    if row["assignment_id"] == assignment)

    def events(self, kind, subject=None):
        return [row for row in self.store.read_snapshot()["events"]
                if row["kind"] == kind and (subject is None or row["subject_id"] == subject)]


class SeamTests(CoreBase):
    def test_second_event_in_one_transaction_is_refused_and_rolled_back(self):
        before = self.store.read_snapshot()

        def change(conn):
            service._event(conn, "first", "x")
            service._event(conn, "second", "x")

        self.code("CORRUPT_STATE", self.store.write, self.owner, change)
        after = self.store.read_snapshot()
        self.assertEqual(before["revision"], after["revision"])
        self.assertEqual(len(before["events"]), len(after["events"]))

    def test_required_profile_written_in_prepared_event_and_conflicts_on_retry(self):
        first = self.assign(required_profile=PLAN)
        self.assertEqual([PLAN], [row["source_id"] for row in self.events("assignment.prepared", first)])
        self.assertEqual(first, self.assign(required_profile=PLAN))
        self.code("IDEMPOTENCY_CONFLICT", self.assign, None, REVIEW)
        self.code("IDEMPOTENCY_CONFLICT", self.assign)
        self.code("INVALID_INPUT", self.assign, None, "nope", idempotency_key="other")
        plain = self.assign(idempotency_key="plain")
        self.assertIsNone(self.events("assignment.prepared", plain)[0]["source_id"])
        self.code("IDEMPOTENCY_CONFLICT", self.assign, None, PLAN, idempotency_key="plain")
        expected = digest(canonical(self.spec(idempotency_key="plain").__dict__))
        self.assertEqual(expected, self.coordinator.detail("assignment", plain)["request_fingerprint"])

    def test_reserve_refuses_required_profile_without_binding_via_plain_cli_and_worker_run(self):
        assignment = self.assign(required_profile=PLAN)
        self.cli("assignment", "reserve-launch", assignment, "--token-file", str(self.token), code=4)
        self.cli("worker", "run", assignment, "--token-file", str(self.token), "--model", "m",
                 "--timeout", "2", code=4)
        self.code("BLOCKED_EVIDENCE", self.coordinator.reserve_launch, self.owner, assignment)
        self.code("BLOCKED_EVIDENCE", self.run_it, assignment)
        self.assertEqual("prepared", self.state(assignment))
        self.assertFalse(self.count.exists())

    def test_crash_between_create_and_bind_cannot_run_generic(self):
        assignment = self.assign(required_profile=PLAN)
        row = self.coordinator.detail("assignment", assignment)
        seal_binding(self.base_dir, {"kind": "binding", "profile": PLAN, "assignment_id": assignment,
                                     "request_fingerprint": row["request_fingerprint"]})
        restarted = Coordinator(open_store(self.t.repo))
        self.code("BLOCKED_EVIDENCE", restarted.reserve_launch, self.owner, assignment)
        self.code("BLOCKED_EVIDENCE", self.run_it, assignment)
        self.assertEqual("prepared", self.state(assignment))
        self.assertFalse(self.count.exists())

    def test_bind_idempotent_prelaunch_only_single_and_profile_must_match(self):
        self.use()
        assignment = self.assign(required_profile=PLAN)
        first = self.bind(assignment)
        revision = self.store.read_snapshot()["revision"]
        self.coordinator.bind_assignment(self.owner, assignment, first)
        self.assertEqual(revision, self.store.read_snapshot()["revision"])
        other = seal_binding(self.base_dir, {"kind": "binding", "format": 1, "profile": PLAN,
                                             "assignment_id": assignment, "other": True,
                                             "request_fingerprint": self.coordinator.detail(
                                                 "assignment", assignment)["request_fingerprint"]})
        self.code("BLOCKED_EVIDENCE", self.coordinator.bind_assignment, self.owner, assignment, other)
        self.assertEqual(1, len(self.events("assignment.bound", assignment)))
        wrong = self.assign(required_profile=PLAN, idempotency_key="wrong")
        self.code("BLOCKED_EVIDENCE", self.bind, wrong, None, REVIEW)
        foreign = self.assign(idempotency_key="foreign")
        value = {"kind": "binding", "profile": PLAN, "assignment_id": wrong, "request_fingerprint": "x"}
        self.code("BLOCKED_EVIDENCE", self.coordinator.bind_assignment, self.owner, foreign,
                  seal_binding(self.base_dir, value))
        self.coordinator.reserve_launch(self.owner, assignment)
        self.code("BLOCKED_EVIDENCE", self.coordinator.bind_assignment, self.owner, assignment, other)
        self.assertEqual(1, len(self.events("assignment.bound", assignment)))

    def test_binding_file_conflict_orphan_retry_and_missing_bytes_unknown(self):
        self.use()
        assignment = self.assign(required_profile=PLAN)
        row = self.coordinator.detail("assignment", assignment)
        value = {"kind": "binding", "profile": PLAN, "assignment_id": assignment,
                 "request_fingerprint": row["request_fingerprint"], "worktree": str(self.t.path),
                 "common_dir": str(self.t.repo.common_dir)}
        orphan = seal_binding(self.base_dir, value)
        self.assertEqual(orphan, seal_binding(self.base_dir, value))
        self.assertEqual(value, read_binding(self.base_dir, orphan.ref, orphan.payload_digest))
        conflicting = {**value, "x": 1}
        clash = self.base_dir / "bindings" / (digest(canonical(conflicting)) + ".json")
        clash.write_bytes(b"different bytes")
        clash.chmod(0o600)
        self.code("BLOCKED_EVIDENCE", seal_binding, self.base_dir, conflicting)
        self.code("UNKNOWN_EVIDENCE", self.coordinator.bind_assignment, self.owner, assignment,
                  BindingEvidence(PLAN, "bindings/" + "a" * 64 + ".json", "a" * 64))
        self.assertEqual([], self.events("assignment.bound"))
        self.coordinator.bind_assignment(self.owner, assignment, orphan)
        (self.base_dir / orphan.ref).unlink()
        self.code("UNKNOWN_EVIDENCE", self.coordinator.reserve_launch, self.owner, assignment)
        source = ReceiptEvidenceSource(self.t.repo, assignment, row["request_fingerprint"], self.store)
        self.code("UNKNOWN_EVIDENCE", source.binding)

    def test_reserve_gate_runs_for_plain_reserve_launch_and_worker_run(self):
        self.use()
        assignment = self.assign(required_profile=PLAN)
        self.bind(assignment)
        self.recorder.reasons["reserve"] = ["nope"]
        error = self.code("BLOCKED_EVIDENCE", self.coordinator.reserve_launch, self.owner, assignment)
        self.assertIn("nope", error.message)
        self.code("BLOCKED_EVIDENCE", self.run_it, assignment)
        self.assertEqual("prepared", self.state(assignment))
        self.assertFalse(self.count.exists())
        self.recorder.reasons.clear()
        report = self.run_it(assignment, FAKE_EXTRA=json.dumps({"tag": "x"}))
        self.assertEqual("completed-claim", report.state)
        self.assertEqual([("reserve", assignment)] * 3, self.recorder.calls)

    def test_unbound_assignment_runs_generic_byte_for_byte(self):
        assignment = self.assign()
        report = self.run_it(assignment, FAKE_WRITE=writes(("result.md", "r")))
        directory = Path(report.run_dir)
        launch = json.loads((directory / "launch.json").read_text())
        terminal = json.loads((directory / "return.json").read_text())
        self.assertEqual({"kind", "assignment_id", "request_fingerprint", "worktree", "common_dir", "source_id",
                          "pid", "argv", "pgid", "requested_sandbox", "model", "cli_version", "envelope_sha256",
                          "observed_at", "client", "binary"}, set(launch))
        self.assertEqual({"kind", "assignment_id", "request_fingerprint", "worktree", "common_dir", "source_id",
                          "client", "result", "changed_paths", "launch_sha256", "checks", "descendants",
                          "unresolved_question", "options", "affected_ids", "thread_id", "exit_code", "before",
                          "after", "input_digests", "immutable_inputs", "claimed_paths", "events_sha256",
                          "last_sha256", "ended_at"}, set(terminal))
        fields = sorted(["assignment_id", "request_fingerprint", "result", "changed_paths", "checks",
                         "descendants", "unresolved_question", "options", "affected_ids"])
        schema = json.loads((directory / "return.schema.json").read_text())
        self.assertEqual(fields, schema["required"])
        self.assertEqual(fields, sorted(schema["properties"]))
        self.assertEqual("completed-claim", self.state(assignment))

    def test_profile_extra_field_accepted_only_under_bound_profile(self):
        self.use()
        generic = self.assign()
        self.assertEqual("unknown", self.run_it(generic, FAKE_EXTRA=json.dumps({"tag": "x"})).state)
        t2 = self.tree("second")
        bound = self.assign(t2, PLAN, idempotency_key="bound")
        self.bind(bound, t2)
        report = self.run_it(bound, t2, FAKE_EXTRA=json.dumps({"tag": "x"}))
        self.assertEqual("completed-claim", report.state)
        schema = json.loads((Path(report.run_dir) / "return.schema.json").read_text())
        self.assertIn("tag", schema["required"])
        t3 = self.tree("third")
        missing = self.assign(t3, PLAN, idempotency_key="missing")
        self.bind(missing, t3)
        self.assertEqual("unknown", self.run_it(missing, t3).state)

    def test_profile_payload_in_receipt_and_forged_payload_makes_inspect_unknown(self):
        self.use()
        assignment = self.assign(required_profile=PLAN)
        evidence = self.bind(assignment)
        report = self.run_it(assignment, FAKE_WRITE=writes(("result.md", "r")),
                             FAKE_EXTRA=json.dumps({"tag": "x"}))
        directory = Path(report.run_dir)
        launch = json.loads((directory / "launch.json").read_text())
        terminal = json.loads((directory / "return.json").read_text())
        self.assertEqual((PLAN, evidence.payload_digest), (launch["profile"], launch["binding_sha256"]))
        self.assertEqual({"tag": "x", "changed": ["result.md"]}, terminal["profile_payload"])
        self.assertEqual(PLAN, terminal["profile"])
        self.assertEqual("completed-claim", worker.inspect_run(self.t.repo, self.store, assignment).state)
        terminal["profile_payload"]["tag"] = "forged"
        (directory / "return.json").write_bytes(canonical(terminal))
        inspected = worker.inspect_run(self.t.repo, self.store, assignment)
        self.assertEqual("unknown", inspected.state)
        self.assertEqual("completed-claim", self.state(assignment))

    def test_profile_env_is_passed_and_recorded(self):
        self.use(self.recorder.profile(env={"BMAD_TEST": "yes"}))
        out = self.root / "env.out"
        assignment = self.assign(required_profile=PLAN)
        self.bind(assignment)
        report = self.run_it(assignment, FAKE_ENV_OUT=str(out), FAKE_EXTRA=json.dumps({"tag": "x"}))
        self.assertEqual("yes", out.read_text())
        self.assertEqual({"BMAD_TEST": "yes"}, json.loads((Path(report.run_dir) / "launch.json").read_text())["env"])
        t2 = self.tree("second")
        generic = self.assign(t2, idempotency_key="generic")
        report = self.run_it(generic, t2, FAKE_ENV_OUT=str(out))
        self.assertEqual("absent", out.read_text())
        self.assertNotIn("env", json.loads((Path(report.run_dir) / "launch.json").read_text()))


class ScopeTests(CoreBase):
    def test_directory_scope_grammar_and_stored_verbatim(self):
        for index, bad in enumerate(["/abs/", "../x/", "a//b/", "./", ".git/x/", ".bmad-coordinator/x/",
                                     "a*/", "a?/", "a[b]/", "/"]):
            with self.subTest(scope=bad):
                self.code("UNSAFE_PATH", self.assign, None, None, idempotency_key=f"bad{index}",
                          writable_paths=[bad])
        assignment = self.assign(writable_paths=["plan/sub/"])
        self.assertEqual(["plan/sub/"], self.coordinator.detail("assignment", assignment)["paths"])

    def test_file_and_directory_scope_have_different_fingerprints(self):
        self.assign(writable_paths=["plan/x"], idempotency_key="same")
        self.code("IDEMPOTENCY_CONFLICT", self.assign, None, None, idempotency_key="same",
                  writable_paths=["plan/x/"])

    def test_directory_scope_covers_nested_return_not_siblings_or_outside(self):
        self.assertTrue(service.scope_covers("plan/a/", "plan/a/b/c.md"))
        self.assertFalse(service.scope_covers("plan/a/", "plan/ab/c.md"))
        self.assertFalse(service.scope_covers("plan/a/", "plan/a"))
        self.assertTrue(service.scope_covers("result.md", "result.md"))
        self.assertFalse(service.scope_covers("result.md", "result.md/x"))
        for index, (path, expected) in enumerate([("plan/a/b/c.md", None), ("plan/ab/c.md", "UNSAFE_PATH"),
                                                  ("other.md", "UNSAFE_PATH")]):
            with self.subTest(path=path):
                t = self.tree(f"case{index}")
                assignment = self.assign(t, writable_paths=["plan/a/"], idempotency_key=path)
                self.coordinator.reserve_launch(self.owner, assignment)
                result = ReturnEvidence("direct", None, [{"path": path, "digest": digest(b"x")}], [], [],
                                        "completed", None, [], [], digest(path.encode()))
                if expected:
                    self.code(expected, self.coordinator.record_return, self.owner, assignment, result)
                else:
                    self.coordinator.record_return(self.owner, assignment, result)
                    self.assertEqual("completed-claim", self.state(assignment))

    def test_directory_scope_checkpoint_export_import_roundtrip(self):
        assignment = self.assign(required_profile=PLAN, writable_paths=["plan/sub/"])
        (self.root / "export").mkdir()
        receipt = export_checkpoint(self.store, self.owner, self.root / "export")
        clone = self.root / "fresh"
        subprocess.run(["git", "clone", "-q", str(self.main), str(clone)], check=True)
        repo = resolve_repo(clone)
        import_checkpoint(repo, receipt.directory)
        snapshot = open_store(repo).read_snapshot()
        row = next(item for item in snapshot["assignments"] if item["id"] == assignment)
        self.assertEqual('["plan/sub/"]', row["paths_json"])
        prepared = [item for item in snapshot["events"]
                    if item["kind"] == "assignment.prepared" and item["subject_id"] == assignment]
        self.assertEqual([PLAN], [item["source_id"] for item in prepared])

    def test_worker_directory_scope_accepts_new_nested_files_and_refuses_delete_symlink_and_outside(self):
        assignment = self.assign(writable_paths=["plan/out/"])
        report = self.run_it(assignment, FAKE_WRITE=writes(("plan/out/a/b.md", "new")))
        self.assertEqual("completed-claim", report.state)
        changed = self.coordinator.detail("assignment", assignment)["return"]["changed_paths"]
        self.assertEqual([{"path": "plan/out/a/b.md", "canonical_path": "plan/out/a/b.md",
                           "digest": digest(b"new")}], changed)
        cases = {"delete": {"FAKE_DELETE": json.dumps(["plan/out/old.md"])},
                 "symlink": {"FAKE_LINK": json.dumps([["plan/out/link.md", "../../brief.md"]])},
                 "chmod": {"FAKE_CHMOD": json.dumps(["plan/out/old.md"])},
                 "outside": {"FAKE_WRITE": writes(("other.md", "x"))}}
        for name, env in cases.items():
            with self.subTest(name=name):
                t = self.tree(name)
                (t.path / "plan" / "out").mkdir(parents=True)
                (t.path / "plan" / "out" / "old.md").write_text("old")
                refused = self.assign(t, writable_paths=["plan/out/"], idempotency_key=name)
                self.assertEqual("unknown", self.run_it(refused, t, **env).state)
                self.assertEqual("running", self.state(refused))

    def test_scope_covering_brief_contract_agents_or_package_refused_before_spawn(self):
        for name in ("brief", "contract"):
            with self.subTest(name=name):
                t = self.tree(name)
                (t.path / "plan").mkdir()
                (t.path / "plan" / "brief.md").write_text("brief")
                (t.path / "plan" / "contract.md").write_text("contract")
                changes = {"brief": dict(brief="plan/brief.md"),
                           "contract": dict(contract="plan/contract.md")}[name]
                assignment = self.assign(t, writable_paths=["plan/"], idempotency_key=name, **changes)
                self.code("UNSAFE_PATH", self.run_it, assignment, t)
                self.assertEqual("prepared", self.state(assignment))
        self.assertFalse(self.count.exists())

    def test_directory_scope_cannot_write_outside_feature_scopes_via_worker_or_plain_return(self):
        # fnmatch lets "?" match "/", so the feature glob "plan?" matches "plan/" but not "plan/x.md".
        t = self.tree("globworker", scopes=["plan?", "brief.md", "contract.md"])
        assignment = self.assign(t, writable_paths=["plan/"], idempotency_key="globworker")
        self.assertEqual("unknown", self.run_it(assignment, t, FAKE_WRITE=writes(("plan/x.md", "x"))).state)
        self.assertEqual("running", self.state(assignment))
        t = self.tree("globplain", scopes=["plan?", "brief.md", "contract.md"])
        plain = self.assign(t, writable_paths=["plan/"], idempotency_key="globplain")
        self.coordinator.reserve_launch(self.owner, plain)
        self.code("UNSAFE_PATH", self.coordinator.record_return, self.owner, plain, ReturnEvidence(
            "direct", None, [{"path": "plan/x.md", "digest": digest(b"x")}], [], [], "completed", None, [], [],
            digest(b"direct")))
        self.assertEqual("launch-pending", self.state(plain))

    def test_aliased_directory_scope_is_refused_at_create_and_before_spawn(self):
        t = self.tree("aliascreate")
        (t.path / "plan").symlink_to(".")
        self.code("UNSAFE_PATH", self.assign, t, writable_paths=["plan/"], idempotency_key="aliascreate")
        t = self.tree("aliasrun")
        (t.path / "plan").mkdir()
        assignment = self.assign(t, writable_paths=["plan/"], idempotency_key="aliasrun")
        (t.path / "plan").rmdir()
        (t.path / "plan").symlink_to(".")
        self.code("UNSAFE_PATH", self.run_it, assignment, t, FAKE_WRITE=writes(("AGENTS.md", "x")))
        self.assertEqual("prepared", self.state(assignment))
        self.assertFalse(self.count.exists())
        self.assertNotEqual("x", (t.path / "AGENTS.md").read_text())

    def test_source_inside_directory_scope_is_correctable_with_frozen_input_digest(self):
        (self.t.path / "plan").mkdir()
        (self.t.path / "plan" / "doc.md").write_text("old")
        assignment = self.assign(writable_paths=["plan/"], source_digests=[
            self.entry(self.t, "brief.md"), self.entry(self.t, "plan/doc.md")])
        report = self.run_it(assignment, FAKE_WRITE=writes(("plan/doc.md", "new")))
        self.assertEqual("completed-claim", report.state)
        terminal = json.loads((Path(report.run_dir) / "return.json").read_text())
        self.assertIn({"path": "plan/doc.md", "digest": digest(b"old")}, terminal["input_digests"])
        self.assertNotIn("plan/doc.md", [item["path"] for item in terminal["immutable_inputs"]])
        self.assertEqual("completed-claim", worker.inspect_run(self.t.repo, self.store, assignment).state)


class FenceTests(CoreBase):
    def second_feature(self, t):
        return self.add_feature(t, t.name)

    def test_attempt_fence_blocks_same_worktree_across_features_in_each_holding_state(self):
        def pending(a):
            self.coordinator.reserve_launch(self.owner, a)

        def unknown(a):
            pending(a)
            self.coordinator.observe_launch(self.owner, a, LaunchEvidence("s", None, "t", None, digest(b"x")))

        def running(a):
            pending(a)
            Coordinator(self.store, Observed()).observe_launch(
                self.owner, a, LaunchEvidence("s", "ref", "t", "w", digest(b"x")))

        def reported(a):
            pending(a)
            self.coordinator.record_return(self.owner, a, ReturnEvidence(
                "direct", None, [], [], [], "completed", None, [], [], digest(b"direct")))

        for name, drive, state in [("pending", pending, "launch-pending"), ("unknown", unknown, "launch-unknown"),
                                   ("running", running, "running"), ("reported", reported, "completed-claim")]:
            with self.subTest(name=name):
                t = self.tree(name)
                other = self.second_feature(t)
                holder = self.assign(t, idempotency_key=name + "-holder")
                drive(holder)
                self.assertEqual(state, self.state(holder))
                blocked = self.assign(t, feature_id=other, idempotency_key=name + "-blocked")
                error = self.code("BLOCKED_EVIDENCE", self.coordinator.reserve_launch, self.owner, blocked)
                self.assertIn(holder, error.message)
                self.assertEqual("prepared", self.state(blocked))

    def test_fence_released_by_observed_terminal_but_held_by_reported_unknown_timeout_or_invalid_output(self):
        cases = {"observed": ({}, True), "decision": ({"FAKE_MODE": "needs-decision"}, True),
                 "invalid": ({"FAKE_MODE": "bad-json"}, False),
                 "timeout": ({"FAKE_MODE": "sleep"}, False)}
        for name, (env, released) in cases.items():
            with self.subTest(name=name):
                t = self.tree(name)
                holder = self.assign(t, idempotency_key=name + "-holder")
                self.run_it(holder, t, timeout=1 if name == "timeout" else 2, **env)
                follower = self.assign(t, idempotency_key=name + "-follower")
                if released:
                    self.coordinator.reserve_launch(self.owner, follower)
                    self.assertEqual("launch-pending", self.state(follower))
                else:
                    self.code("BLOCKED_EVIDENCE", self.coordinator.reserve_launch, self.owner, follower)

    def test_reserve_precedes_inventory_and_recheck_failure_keeps_launch_pending(self):
        assignment = self.assign()
        seen = []
        original = worker.inventory

        def watching(worktree):
            seen.append(self.state(assignment))
            return original(worktree)

        with patch("coordinator_core.worker.inventory", side_effect=watching):
            self.assertEqual("completed-claim", self.run_it(assignment).state)
        self.assertEqual("launch-pending", seen[0])
        t = self.tree("recheck")
        stale = self.assign(t, idempotency_key="stale")
        before = len(self.count.read_text().splitlines())
        with patch("coordinator_core.worker._check_inputs", side_effect=DomainError("STALE_INPUT", "changed")):
            self.code("STALE_INPUT", self.run_it, stale, t)
        self.assertEqual("launch-pending", self.state(stale))
        self.assertEqual(before, len(self.count.read_text().splitlines()))
        sidecar = run_root(t.repo) / run_name(stale, self.coordinator.detail("assignment", stale)["request_fingerprint"])
        self.assertFalse(sidecar.exists())

    def test_reserve_race_after_snapshot_is_decided_inside_the_transaction(self):
        first = self.assign(idempotency_key="first")
        second = self.assign(idempotency_key="second")
        original = Store.read_snapshot
        raced = []

        def racing(store):
            snapshot = original(store)
            if not raced:
                raced.append(True)
                Coordinator(open_store(self.t.repo)).reserve_launch(self.owner, first)
            return snapshot

        with patch.object(Store, "read_snapshot", racing):
            error = self.code("BLOCKED_EVIDENCE", self.coordinator.reserve_launch, self.owner, second)
        self.assertEqual([True], raced)
        self.assertIn(first, error.message)
        self.assertEqual("launch-pending", self.state(first))
        self.assertEqual("prepared", self.state(second))

    def test_prepared_assignments_do_not_hold_fence_and_other_worktrees_do_not_conflict(self):
        first = self.assign(idempotency_key="first")
        second = self.assign(idempotency_key="second")
        self.coordinator.reserve_launch(self.owner, second)
        self.assertEqual("prepared", self.state(first))
        self.code("BLOCKED_EVIDENCE", self.coordinator.reserve_launch, self.owner, first)
        t = self.tree("elsewhere")
        third = self.assign(t, idempotency_key="third")
        self.coordinator.reserve_launch(self.owner, third)
        self.assertEqual("launch-pending", self.state(third))

    def test_concurrent_reserve_processes_exactly_one_wins(self):
        first = self.assign(idempotency_key="first")
        second = self.assign(idempotency_key="second")
        processes = [subprocess.Popen([sys.executable, str(self.t.path / ".bmad-coordinator" / "run.py"),
                                       "--repo", str(self.t.path), "assignment", "reserve-launch", item,
                                       "--token-file", str(self.token)], stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True, env={**os.environ, **self.env})
                     for item in (first, second)]
        codes = []
        for process in processes:
            process.communicate(timeout=30)
            codes.append(process.returncode)
        self.assertEqual([0, 4], sorted(codes))
        self.assertEqual(1, sum(self.state(item) == "launch-pending" for item in (first, second)))


class GateTests(CoreBase):
    def finished(self, t, key, required, path, **extra):
        assignment = self.assign(t, required, idempotency_key=key, writable_paths=[path], author_id=key)
        if required:
            self.bind(assignment, t)
        env = {"FAKE_WRITE": writes((path, key))}
        if required:
            env["FAKE_EXTRA"] = json.dumps({"tag": key})
        self.assertEqual("completed-claim", self.run_it(assignment, t, **env).state)
        return assignment

    def test_capture_bundle_accept_verify_gates_run_for_plain_commands_and_refuse_mixed_bound_unbound(self):
        self.use()
        bound = self.finished(self.t, "p1", PLAN, "result.md")
        plain = self.finished(self.t, "p2", None, "other.md")
        self.recorder.calls.clear()
        self.code("BLOCKED_EVIDENCE", self.bundle, ["result.md", "other.md"], [bound, plain])
        self.assertEqual([], self.recorder.calls)
        self.recorder.reasons["bundle"] = ["no-bundle"]
        error = self.code("BLOCKED_EVIDENCE", self.bundle, ["result.md"], [bound])
        self.assertIn("no-bundle", error.message)
        self.recorder.reasons.clear()
        bundle = self.bundle(["result.md"], [bound])
        self.assertIn(("bundle", bound), self.recorder.calls)
        observer = Coordinator(self.store, Observed())
        review = observer.record_review(self.owner, ReviewEvidence(bundle, "reviewer", "src", "ref", "ok", False))
        self.recorder.reasons["accept"] = ["no-accept"]
        self.code("BLOCKED_EVIDENCE", self.coordinator.accept, self.owner, bundle, review)
        self.assertEqual([], self.store.read_snapshot()["acceptances"])
        self.recorder.reasons.clear()
        self.coordinator.accept(self.owner, bundle, review)
        self.assertEqual("current", self.coordinator.verify().current)
        self.recorder.reasons["verify"] = ["no-verify"]
        report = self.coordinator.verify()
        self.assertEqual("unknown", report.checked[0]["current"])
        self.assertIn("no-verify", json.dumps(report.checked[0]))
        self.assertIn(("accept", bound), self.recorder.calls)
        self.assertIn(("verify", bound), self.recorder.calls)
        calls = len(self.recorder.calls)
        generic = self.bundle(["other.md"], [plain])
        generic_review = observer.record_review(self.owner, ReviewEvidence(generic, "reviewer", "src", "ref2", "ok", False))
        self.coordinator.accept(self.owner, generic, generic_review)
        self.assertEqual(calls, len(self.recorder.calls))

    def test_plain_inspect_stays_current_byte_while_historical_path_survives_later_attempt(self):
        first = self.assign(idempotency_key="first")
        self.run_it(first, FAKE_WRITE=writes(("result.md", "r")))
        second = self.assign(idempotency_key="second", writable_paths=["other.md"])
        self.run_it(second, FAKE_WRITE=writes(("other.md", "o")))
        inspected = worker.inspect_run(self.t.repo, self.store, first)
        self.assertEqual("unknown", inspected.state)
        self.assertTrue(inspected.evidence_needs)
        event = worker.historical_return(self.t.repo, self.store, first)
        self.assertEqual("completed", event.result)
        terminal = Path(run_root(self.t.repo)) / run_name(
            first, self.coordinator.detail("assignment", first)["request_fingerprint"]) / "return.json"
        value = json.loads(terminal.read_text())
        value["checks"] = ["edited"]
        terminal.write_bytes(canonical(value))
        self.code("UNKNOWN_EVIDENCE", worker.historical_return, self.t.repo, self.store, first)

    def test_needs_decision_reports_same_process_continuation_unsupported(self):
        assignment = self.assign()
        self.assertEqual("needs-decision", self.run_it(assignment, FAKE_MODE="needs-decision").state)
        fingerprint = self.coordinator.detail("assignment", assignment)["request_fingerprint"]
        for needs in (worker.inspect_run(self.t.repo, self.store, assignment).evidence_needs,
                      self.coordinator.retry_assignment(assignment, fingerprint).evidence_needs):
            self.assertEqual([service.CONTINUATION_UNSUPPORTED], needs)
        self.assertIn("same-process continuation unsupported", service.CONTINUATION_UNSUPPORTED)

    def test_partial_needs_decision_files_covered_only_after_linked_completed_continuation(self):
        partial = self.assign(idempotency_key="partial", author_id="partial")
        self.run_it(partial, FAKE_MODE="needs-decision", FAKE_WRITE=writes(("result.md", "partial")))
        self.code("BLOCKED_EVIDENCE", self.bundle, ["result.md"], [partial])
        request = self.request_of(partial)
        self.decide(request)
        self.code("BLOCKED_EVIDENCE", self.bundle, ["result.md"], [partial])
        update = self.assign(idempotency_key="update", author_id="update", writable_paths=["other.md"],
                             blocking_requests=[request])
        self.run_it(update, FAKE_WRITE=writes(("other.md", "done")))
        self.assertTrue(self.bundle(["result.md"], [partial]))
        self.assertTrue(self.bundle(["result.md", "other.md"], [partial, update]))

    def test_unresolved_unlinked_or_blocked_row_never_covers(self):
        t = self.tree("blocked")
        blocked = self.assign(t, idempotency_key="blocked")
        self.run_it(blocked, t, FAKE_MODE="blocked", FAKE_WRITE=writes(("result.md", "x")))
        self.code("BLOCKED_EVIDENCE", self.bundle, ["result.md"], [blocked], t)
        t = self.tree("unlinked")
        partial = self.assign(t, idempotency_key="unlinked-partial")
        self.run_it(partial, t, FAKE_MODE="needs-decision", FAKE_WRITE=writes(("result.md", "x")))
        self.coordinator.record_decision(self.owner, DecisionInput(
            t.feature_id, 1, "coordinator", "clause:1", "within mandate", {"choice": "A"}, self.request_of(partial)))
        other = self.assign(t, idempotency_key="other", writable_paths=["other.md"])
        self.run_it(other, t, FAKE_WRITE=writes(("other.md", "x")))
        self.code("BLOCKED_EVIDENCE", self.bundle, ["result.md"], [partial], t)
        t = self.tree("reported")
        partial = self.assign(t, idempotency_key="reported-partial")
        self.run_it(partial, t, FAKE_MODE="needs-decision", FAKE_WRITE=writes(("result.md", "x")))
        request = self.request_of(partial)
        self.coordinator.record_decision(self.owner, DecisionInput(
            t.feature_id, 1, "coordinator", "clause:1", "within mandate", {"choice": "A"}, request))
        linked = self.assign(t, idempotency_key="linked", writable_paths=["other.md"], blocking_requests=[request])
        self.coordinator.reserve_launch(self.owner, linked)
        self.coordinator.record_return(self.owner, linked, ReturnEvidence(
            "direct", None, [], [], [], "completed", None, [], [], digest(b"direct")))
        self.code("BLOCKED_EVIDENCE", self.bundle, ["result.md"], [partial], t)


if __name__ == "__main__":
    unittest.main()
