import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

from coordinator_core import model
from coordinator_core.model import DecisionInput, DomainError, FeatureInput
from coordinator_core.repo import resolve_repo
from coordinator_core.service import Coordinator
from coordinator_core.store import open_store
from tests.support import ext4_mountinfo


def digest(data):
    return hashlib.sha256(data).hexdigest()


class AssignmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.main = self.root / "main"
        self.main.mkdir()
        subprocess.run(["git", "init", "-q", str(self.main)], check=True)
        subprocess.run(["git", "-C", str(self.main), "-c", "user.name=Test", "-c",
                        "user.email=test@example.invalid", "commit", "--allow-empty", "-qm", "initial"], check=True)
        self.source = self.main / "brief.md"
        self.contract = self.main / "contract.md"
        self.source.write_text("brief v1")
        self.contract.write_text("contract v1")
        self.repo = resolve_repo(self.main)
        with patch("coordinator_core.store.read_mountinfo", return_value=ext4_mountinfo(self.root)):
            self.store = open_store(self.repo, create=True)
        self.coordinator = Coordinator(self.store)
        self.owner = self.mutate(self.coordinator.claim, "session", self.root / "owner.token")
        self.feature_id = self.mutate(self.coordinator.create_feature, self.owner, FeatureInput(
            "ship", [], [], ["checks"], ["edit"], "main", "HEAD", "abc", str(self.main), [], ["*.md"]))

    def mutate(self, action, *args):
        with patch("coordinator_core.store.read_mountinfo", return_value=ext4_mountinfo(self.root)):
            return action(*args)

    def spec(self, **changes):
        source = [{"path": "brief.md", "digest": digest(self.source.read_bytes())}]
        contracts = [{"path": "contract.md", "digest": digest(self.contract.read_bytes())}]
        data = dict(feature_id=self.feature_id, idempotency_key="key-1", brief_ref="brief.md",
                    mandate_revision=1, source_digests=source, contract_files=contracts,
                    contract_digest=digest(json.dumps(contracts, sort_keys=True, separators=(",", ":")).encode()),
                    writable_paths=["result.md"], permission_mode="workspace-write", author_id="worker-1",
                    depends_on=[], blocking_requests=[])
        data.update(changes)
        return model.AssignmentInput(**data)

    def create(self, **changes):
        return self.mutate(self.coordinator.create_assignment, self.owner, self.spec(**changes))

    def code(self, expected, action, *args):
        with self.assertRaises(DomainError) as caught:
            self.mutate(action, *args)
        self.assertEqual(expected, caught.exception.code)

    def test_status_includes_bounded_assignment_task_state(self):
        with closing(sqlite3.connect(self.store.path)) as conn, conn:
            conn.execute("INSERT INTO assignments(id, feature_id, owner_generation, idempotency_key, request_fingerprint, "
                         "brief_ref, mandate_revision, source_digests_json, contract_files_json, contract_digest, "
                         "paths_json, permission_mode, author_id, depends_on_json, blocking_requests_json, state) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         ("a-1", self.feature_id, self.owner.generation, "key", "fp", "brief.md", 1,
                          "[]", "[]", "fp", "[]", "workspace-write", "worker", "[]", "[]", "prepared"))
        task = self.coordinator.status()["tasks"][0]
        self.assertEqual({"id": "a-1", "feature_id": self.feature_id, "state": "prepared",
                          "detail": {"kind": "assignment", "id": "a-1"}}, task)

    def test_duplicate_idempotency_key_returns_same_assignment(self):
        first = self.create()
        revision = self.store.read_snapshot()["revision"]
        self.assertEqual(first, self.mutate(self.coordinator.create_assignment, self.owner, self.spec()))
        self.assertEqual(revision, self.store.read_snapshot()["revision"])
        self.assertEqual("prepared", self.coordinator.detail("assignment", first)["state"])

    def test_changed_payload_same_key_conflicts_across_processes(self):
        first = self.create()
        script = """import json, pathlib, sys
from unittest.mock import patch
from coordinator_core.model import AssignmentInput, DomainError, OwnerRef
from coordinator_core.repo import resolve_repo
from coordinator_core.service import Coordinator
from coordinator_core.store import open_store
from tests.support import ext4_mountinfo
root, main, token = map(pathlib.Path, sys.argv[1:])
credentials = json.loads(token.read_text())
try:
 with patch('coordinator_core.store.read_mountinfo', return_value=ext4_mountinfo(root)):
  Coordinator(open_store(resolve_repo(main))).create_assignment(
   OwnerRef(**credentials), AssignmentInput(**json.load(sys.stdin)))
except DomainError as error:
 print(error.code)
"""
        process = subprocess.run([sys.executable, "-c", script, str(self.root), str(self.main),
                                  str(self.root / "owner.token")], input=json.dumps(asdict(self.spec(author_id="different"))),
                                 text=True, capture_output=True, cwd=Path(__file__).parents[1], check=True)
        self.assertEqual("IDEMPOTENCY_CONFLICT", process.stdout.strip())
        self.assertEqual(first, self.coordinator.detail("assignment", first)["id"])

    def test_prepared_can_cancel_and_release(self):
        assignment = self.create()
        self.mutate(self.coordinator.cancel_prepared, self.owner, assignment)
        self.assertEqual("cancelled-before-launch", self.coordinator.detail("assignment", assignment)["state"])
        self.mutate(self.coordinator.release, self.owner)
        self.assertEqual("released", self.coordinator.recover_status().state)

    def test_reserved_cannot_cancel_without_adapter_proof(self):
        assignment = self.create()
        self.mutate(self.coordinator.reserve_launch, self.owner, assignment)
        row = self.coordinator.detail("assignment", assignment)
        self.assertEqual(("launch-pending", 0, None), (row["state"], row["external_start_seen"], row["launch_ref"]))
        self.code("BLOCKED_EVIDENCE", self.coordinator.cancel_prepared, self.owner, assignment)
        self.code("RECOVERY_BLOCKED", self.coordinator.release, self.owner)

    def test_an_assignment_and_its_reservation_accept_an_empty_contract_list(self):
        """Implementation drive D22: a story seals no contract files; the digest of the empty canonical list must
        still match, and the source digests still need the brief."""
        empty_digest = digest(json.dumps([], sort_keys=True, separators=(",", ":")).encode())
        assignment = self.create(contract_files=[], contract_digest=empty_digest)
        row = next(item for item in self.store.read_snapshot()["assignments"] if item["id"] == assignment)
        self.assertEqual(("[]", empty_digest), (row["contract_files_json"], row["contract_digest"]))
        self.mutate(self.coordinator.reserve_launch, self.owner, assignment)
        self.code("INVALID_INPUT", lambda: self.create(idempotency_key="key-2", contract_files=[],
                                                       contract_digest=digest(b"x")))
        with self.assertRaises(DomainError) as caught:
            self.create(idempotency_key="key-3", contract_files=[], contract_digest=empty_digest,
                        source_digests=[{"path": "contract.md", "digest": digest(self.contract.read_bytes())}])
        self.assertEqual("INVALID_INPUT", caught.exception.code)
        self.assertEqual("Brief must be included in source digests", caught.exception.message)

    def test_stale_prepared_inputs_block_reservation(self):
        assignment = self.create()
        self.contract.write_text("contract v2")
        revision = self.store.read_snapshot()["revision"]
        self.code("STALE_INPUT", self.coordinator.reserve_launch, self.owner, assignment)
        self.assertEqual("prepared", self.coordinator.detail("assignment", assignment)["state"])
        self.assertEqual(revision, self.store.read_snapshot()["revision"])

    def test_crash_after_intent_is_unknown(self):
        assignment = self.create()
        self.mutate(self.coordinator.reserve_launch, self.owner, assignment)
        revision = self.store.read_snapshot()["revision"]
        recovery = self.coordinator.recover_status()
        self.assertEqual("launch-pending", self.coordinator.detail("assignment", assignment)["state"])
        self.assertEqual("launch-unknown", recovery.assignment_states[assignment])
        self.assertEqual(revision, self.store.read_snapshot()["revision"])

    def test_old_owner_precheck_then_takeover_cannot_record_launch(self):
        assignment = self.create()
        self.mutate(self.coordinator.reserve_launch, self.owner, assignment)
        # Simulate a takeover between a caller's read and its fenced write.
        with closing(sqlite3.connect(self.store.path)) as conn, conn:
            conn.execute("UPDATE owner SET generation=generation+1, token_hash=?",
                         (digest(b"new-owner"),))
        event = model.LaunchEvidence("test", "event:1", "2026-10-04T00:00:00Z", "worker-1", digest(b"event"))
        self.code("OWNERSHIP_CONFLICT", self.coordinator.observe_launch, self.owner, assignment, event)
        self.assertEqual("launch-pending", self.coordinator.detail("assignment", assignment)["state"])

    def test_unknown_pending_blocks_clean_handoff(self):
        assignment = self.create()
        self.mutate(self.coordinator.reserve_launch, self.owner, assignment)
        self.assertEqual("launch-unknown", self.coordinator.recover_status().assignment_states[assignment])
        self.code("RECOVERY_BLOCKED", self.coordinator.release, self.owner)

    def test_completed_claim_still_blocks_release(self):
        assignment = self.create()
        self.mutate(self.coordinator.reserve_launch, self.owner, assignment)
        self.mutate(self.coordinator.record_return, self.owner, assignment, model.ReturnEvidence(
            "test", "return:1", [], [], [], "completed", None, [], [], digest(b"return")))
        self.assertEqual("completed-claim", self.coordinator.detail("assignment", assignment)["state"])
        self.code("RECOVERY_BLOCKED", self.coordinator.release, self.owner)

    def test_need_decision_blocks_only_dependency(self):
        class ObservedSource:
            def classify_return(self, event):
                return "observed"

        # Attempt fence: unobserved terminal returns would hold the shared worktree, so the
        # attempts here end with observed returns.
        observed = Coordinator(self.store, ObservedSource())
        first = self.create()
        second = self.create(idempotency_key="key-2")
        self.mutate(self.coordinator.reserve_launch, self.owner, first)
        self.mutate(observed.record_return, self.owner, first, model.ReturnEvidence(
            "test", "return:2", [], [], [], "needs-decision", "Which path?", ["A", "B"],
            [first], digest(b"question")))
        requests = self.store.read_snapshot()["decision_requests"]
        self.assertEqual(1, len(requests))
        request_id = requests[0]["id"]
        blocked = self.create(idempotency_key="key-3", blocking_requests=[request_id])
        self.code("BLOCKED_EVIDENCE", self.coordinator.reserve_launch, self.owner, blocked)
        self.mutate(self.coordinator.reserve_launch, self.owner, second)
        decision = self.mutate(self.coordinator.record_decision, self.owner, DecisionInput(
            self.feature_id, 1, "operator-reported", "conversation:1", "resolve", {"choice": "A"}, request_id))
        self.assertEqual(decision, self.coordinator.detail("decision-request", request_id)["resolution_decision_id"])
        self.mutate(observed.record_return, self.owner, second, model.ReturnEvidence(
            "test", "return:second", [], [], [], "completed", None, [], [], digest(b"second")))
        self.mutate(self.coordinator.reserve_launch, self.owner, blocked)
        self.assertEqual("launch-pending", self.coordinator.detail("assignment", blocked)["state"])

    def test_claimed_completion_creates_no_acceptance(self):
        assignment = self.create()
        self.mutate(self.coordinator.reserve_launch, self.owner, assignment)
        self.mutate(self.coordinator.record_return, self.owner, assignment, model.ReturnEvidence(
            "test", "return:3", [{"path": "result.md", "digest": digest(b"result")}],
            ["tests pass"], [], "completed", None, [], [], digest(b"result")))
        self.assertEqual([], self.store.read_snapshot()["acceptances"])
        self.assertEqual("completed-claim", self.coordinator.detail("assignment", assignment)["state"])

    def test_reported_completion_does_not_satisfy_prerequisite(self):
        first = self.create()
        dependent = self.create(idempotency_key="key-2", depends_on=[first])
        self.mutate(self.coordinator.reserve_launch, self.owner, first)
        self.mutate(self.coordinator.record_return, self.owner, first, model.ReturnEvidence(
            "test", "return:reported", [], [], [], "completed", None, [], [], digest(b"reported")))
        self.code("BLOCKED_EVIDENCE", self.coordinator.reserve_launch, self.owner, dependent)

    def test_observed_completion_satisfies_prerequisite_but_not_acceptance(self):
        class ObservedSource:
            def classify_return(self, event):
                return "observed"

        first = self.create()
        dependent = self.create(idempotency_key="key-2", depends_on=[first])
        self.mutate(self.coordinator.reserve_launch, self.owner, first)
        observed = Coordinator(self.store, ObservedSource())
        self.mutate(observed.record_return, self.owner, first, model.ReturnEvidence(
            "test", "return:observed", [], [], [], "completed", None, [], [], digest(b"observed")))
        self.mutate(self.coordinator.reserve_launch, self.owner, dependent)
        self.assertEqual("launch-pending", self.coordinator.detail("assignment", dependent)["state"])
        self.assertEqual([], self.store.read_snapshot()["acceptances"])

    def test_client_stopped_is_an_observed_result_only_and_goes_with_a_client_stop(self):
        """Design D25, ruling H7: a reported return never claims `client-stopped`; a stop carries its `client_stop` and
        no other result does."""
        class ObservedSource:
            def classify_return(self, event):
                return "observed"

        stop = {"kind": "max-turns", "num_turns": 61}
        assignment = self.create()
        self.mutate(self.coordinator.reserve_launch, self.owner, assignment)
        stopped = model.ReturnEvidence("test", "return:stop", [], [], [], "client-stopped", None, [], [],
                                       digest(b"stop"), stop)
        with self.assertRaises(DomainError) as caught:
            self.mutate(self.coordinator.record_return, self.owner, assignment, stopped)
        self.assertEqual(("INVALID_INPUT", "client-stopped is the coordinator's own observed result"),
                         (caught.exception.code, caught.exception.message))
        observed = Coordinator(self.store, ObservedSource())
        for bad in (replace(stopped, client_stop=None), replace(stopped, result="completed"),
                    replace(stopped, client_stop=[stop]), replace(stopped, client_stop="max-turns"),
                    replace(stopped, result="completed", client_stop=[stop]),
                    replace(stopped, result="completed", client_stop={})):
            with self.subTest(bad=bad):
                self.code("INVALID_INPUT", observed.record_return, self.owner, assignment, bad)
        self.assertEqual("launch-unknown", self.coordinator.recover_status().assignment_states[assignment])
        self.mutate(observed.record_return, self.owner, assignment, stopped)
        detail = self.coordinator.detail("assignment", assignment)
        self.assertEqual(("client-stopped", stop, "observed"),
                         (detail["state"], detail["return"]["client_stop"], detail["return"]["provenance_class"]))

    def test_retry_reads_stored_state_without_mutation(self):
        assignment = self.create()
        fingerprint = self.coordinator.detail("assignment", assignment)["request_fingerprint"]
        self.mutate(self.coordinator.reserve_launch, self.owner, assignment)
        revision = self.store.read_snapshot()["revision"]
        retry = self.coordinator.retry_assignment(assignment, fingerprint)
        self.assertEqual((assignment, "launch-pending"), (retry.assignment_id, retry.state))
        self.assertTrue(retry.evidence_needs)
        self.assertEqual(revision, self.store.read_snapshot()["revision"])

    def test_return_cannot_claim_changes_outside_assignment_scope(self):
        assignment = self.create()
        self.mutate(self.coordinator.reserve_launch, self.owner, assignment)
        revision = self.store.read_snapshot()["revision"]
        self.code("UNSAFE_PATH", self.coordinator.record_return, self.owner, assignment,
                  model.ReturnEvidence("test", "return:scope", [{"path": "other.md", "digest": digest(b"x")}],
                                       [], [], "completed", None, [], [], digest(b"scope")))
        self.assertEqual("launch-pending", self.coordinator.detail("assignment", assignment)["state"])
        self.assertEqual(revision, self.store.read_snapshot()["revision"])

    def test_assignment_rejects_out_of_scope_and_cross_feature_dependency(self):
        self.code("UNSAFE_PATH", self.coordinator.create_assignment, self.owner,
                  self.spec(writable_paths=["outside.txt"]))
        other_feature = self.mutate(self.coordinator.create_feature, self.owner, FeatureInput(
            "other", [], [], [], [], "main", "HEAD", "abc", str(self.main), [], ["*.md"]))
        other = self.create(feature_id=other_feature, idempotency_key="other-key")
        self.code("INVALID_INPUT", self.coordinator.create_assignment, self.owner,
                  self.spec(idempotency_key="dependent-key", depends_on=[other]))

    def test_recursive_feature_scope_allows_nested_assignment_path(self):
        nested_feature = self.mutate(self.coordinator.create_feature, self.owner, FeatureInput(
            "nested", [], [], [], [], "main", "HEAD", "abc", str(self.main), [], ["outputs/**"]))
        assignment = self.create(feature_id=nested_feature, idempotency_key="nested-key",
                                 writable_paths=["outputs/reports/final.md"])
        self.assertEqual("prepared", self.coordinator.detail("assignment", assignment)["state"])

    def test_readonly_assignment_empty_paths_creates(self):
        assignment = self.create(permission_mode="read-only", writable_paths=[])
        self.assertEqual([], self.coordinator.detail("assignment", assignment)["paths"])

    def test_readonly_nonempty_paths_and_workspace_write_empty_refused(self):
        self.code("UNSAFE_PATH", self.coordinator.create_assignment, self.owner,
                  self.spec(permission_mode="read-only", writable_paths=["result.md"]))
        self.code("UNSAFE_PATH", self.coordinator.create_assignment, self.owner,
                  self.spec(writable_paths=[]))

    def test_missing_launch_trace_is_recorded_as_unobserved(self):
        assignment = self.create()
        self.mutate(self.coordinator.reserve_launch, self.owner, assignment)
        self.mutate(self.coordinator.observe_launch, self.owner, assignment,
                    model.LaunchEvidence("reported-source", None, "2026-10-04T00:00:00Z", None, digest(b"unknown")))
        row = self.coordinator.detail("assignment", assignment)
        self.assertEqual(("launch-unknown", 0, None),
                         (row["state"], row["external_start_seen"], row["launch_ref"]))
        event = self.store.read_snapshot()["events"][-1]
        self.assertEqual(("reported-source", None), (event["source_id"], event["evidence_ref"]))

    def test_missing_return_trace_stays_unobserved(self):
        assignment = self.create()
        self.mutate(self.coordinator.reserve_launch, self.owner, assignment)
        self.mutate(self.coordinator.record_return, self.owner, assignment,
                    model.ReturnEvidence("reported-source", None, [], [], [], "completed", None, [], [], digest(b"note")))
        payload = self.coordinator.detail("assignment", assignment)["return"]
        self.assertEqual("unobserved", payload["trace"])
        self.assertEqual(0, self.coordinator.detail("assignment", assignment)["external_start_seen"])
        self.assertEqual("reported-source", self.store.read_snapshot()["events"][-1]["source_id"])


if __name__ == "__main__":
    unittest.main()
