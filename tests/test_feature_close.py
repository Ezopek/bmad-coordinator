"""Closing a feature (feature lifecycle design F1 to F9, issue #7)."""

import hashlib
import json
import sqlite3
import subprocess
import unittest
from contextlib import closing
from pathlib import Path

from coordinator_core import profiles
from coordinator_core.model import (AssignmentInput, BindingEvidence, BundleInput, DecisionInput, DomainError, FeatureInput,
                                    LaunchEvidence, ReturnEvidence, ReviewEvidence)
from coordinator_core.exporter import import_checkpoint
from coordinator_core.repo import resolve_repo
from coordinator_core.service import Coordinator
from coordinator_core.store import open_store
from tests import test_assignments, test_export, test_review   # fixtures borrowed by attribute, never collected here

AssignmentTests = test_assignments.AssignmentTests.__dict__
ReviewTests = test_review.ReviewTests.__dict__
ExportTests = test_export.ExportTests.__dict__
ObservedReview = test_review.ObservedReview


def digest(data):
    return hashlib.sha256(data).hexdigest()


def returned(result="completed", question=None):
    return ReturnEvidence("test-source", "return:1", [], ["checks"], [], result, question,
                          ["a", "b"] if question else [], [], digest(b"return:1"))


class FeatureCloseTests(unittest.TestCase):
    setUp = AssignmentTests['setUp']
    mutate = AssignmentTests['mutate']
    spec = AssignmentTests['spec']
    create = AssignmentTests['create']
    code = AssignmentTests['code']

    def decision(self, feature=None, revision=1):
        return self.mutate(self.coordinator.record_decision, self.owner, DecisionInput(
            feature or self.feature_id, revision, "operator-reported", "operator:merged", "merged", {"pr": 1}))

    def close(self, decision, resolution="completed", revision=1, feature=None):
        return self.mutate(self.coordinator.close_feature, self.owner, feature or self.feature_id, revision,
                           decision, resolution)

    def sql(self, statement, *args):
        with closing(sqlite3.connect(self.store.path)) as conn, conn:
            conn.execute(statement, args)

    def feature_row(self, feature=None):
        return next(row for row in self.store.read_snapshot()["features"] if row["id"] == (feature or self.feature_id))

    def revision(self):
        return self.store.read_snapshot()["revision"]

    def observed(self):
        return Coordinator(self.store, ObservedReview())

    def test_close_records_state_event_and_keeps_the_revision(self):
        decision = self.decision()
        self.assertEqual("completed", self.close(decision))
        row = self.feature_row()
        self.assertEqual(("completed", 1), (row["state"], row["mandate_revision"]))
        self.assertIsNotNone(row["portable_file_digests_json"])
        event = self.store.read_snapshot()["events"][-1]
        self.assertEqual(("feature.completed", self.feature_id, decision),
                         (event["kind"], event["subject_id"], event["source_id"]))

    def test_close_refusals_change_nothing(self):
        decision = self.decision()
        other = self.mutate(self.coordinator.create_feature, self.owner, FeatureInput(
            "other", [], [], [], [], "main", "HEAD", "abc", str(self.main), [], ["*.md"]))
        foreign = self.decision(other)

        def refused(code, *args, **kwargs):
            before = self.revision()
            with self.assertRaises(DomainError) as caught:
                self.close(*args, **kwargs)
            self.assertEqual(code, caught.exception.code, caught.exception.message)
            self.assertEqual(before, self.revision())
            self.assertEqual("active", self.feature_row()["state"])
            return caught.exception.message

        refused("REVISION_CONFLICT", decision, revision=2)
        refused("INVALID_INPUT", foreign)
        refused("INVALID_INPUT", decision, resolution="done")
        assignment = self.create()
        self.assertIn("cancel-prepared", refused("BLOCKED_EVIDENCE", decision))
        self.mutate(self.coordinator.reserve_launch, self.owner, assignment)
        refused("BLOCKED_EVIDENCE", decision)
        self.mutate(self.coordinator.record_return, self.owner, assignment, returned())
        self.assertEqual("completed-claim", self.store.read_snapshot()["assignments"][0]["state"])
        refused("BLOCKED_EVIDENCE", decision)
        self.sql("UPDATE assignments SET state='resolved-unknown' WHERE id=?", assignment)
        second = self.create(idempotency_key="key-2")
        self.mutate(self.coordinator.reserve_launch, self.owner, second)
        self.mutate(self.observed().record_return, self.owner, second, returned("needs-decision", "Which?"))
        self.assertIn("decision request", refused("BLOCKED_EVIDENCE", decision))
        revised = self.decision()
        self.mutate(self.coordinator.revise_mandate, self.owner, self.feature_id, 1, {"outcome": "ship more"}, revised)
        refused("INVALID_INPUT", decision, revision=2)

    def test_imported_unknown_assignment_does_not_block(self):
        assignment = self.create()
        self.sql("UPDATE assignments SET state='unknown' WHERE id=?", assignment)
        self.assertEqual("abandoned", self.close(self.decision(), "abandoned"))

    def test_repeat_close_is_a_no_op_and_a_different_one_is_refused(self):
        decision = self.decision()
        self.close(decision)
        before = self.revision()
        self.assertEqual("completed", self.close(decision))
        self.assertEqual(before, self.revision())
        self.code("FEATURE_CLOSED", self.coordinator.close_feature, self.owner, self.feature_id, 1, decision,
                  "abandoned")
        self.code("FEATURE_CLOSED", self.coordinator.close_feature, self.owner, self.feature_id, 1,
                  "another-decision", "completed")

    def test_every_write_refuses_a_closed_feature(self):
        assignment = self.create()
        self.mutate(self.coordinator.reserve_launch, self.owner, assignment)
        self.mutate(self.observed().record_return, self.owner, assignment, returned())
        decision = self.decision()
        self.close(decision)
        closed = "FEATURE_CLOSED"
        self.code(closed, self.coordinator.record_decision, self.owner, DecisionInput(
            self.feature_id, 1, "operator-reported", "operator:later", "later", {}))
        self.code(closed, self.coordinator.revise_mandate, self.owner, self.feature_id, 1, {"outcome": "x"}, decision)
        self.code(closed, self.coordinator.create_assignment, self.owner, self.spec(idempotency_key="key-new"))
        anchor = ("pipelines/x.json", "0" * 64)
        self.code(closed, self.coordinator.anchor_event, self.owner, "pipeline.bound", self.feature_id,
                  ("source", *anchor))
        self.code(closed, self.coordinator.anchor_event, self.owner, "bmad.materialized", self.feature_id,
                  ("source", *anchor))
        self.code(closed, self.coordinator.anchor_event, self.owner, "drive.leased", "lease-1",
                  (self.feature_id, *anchor))
        for state, call in (
                ("prepared", lambda: self.coordinator.cancel_prepared(self.owner, assignment)),
                ("prepared", lambda: self.coordinator.reserve_launch(self.owner, assignment)),
                ("prepared", lambda: self.coordinator.bind_assignment(self.owner, assignment, BindingEvidence(
                    next(iter(profiles.MODULES)), f"bindings/{'1' * 64}.json", "1" * 64))),
                ("launch-pending", lambda: self.coordinator.observe_launch(self.owner, assignment, LaunchEvidence(
                    "source", None, "now", None, digest(b"launch")))),
                ("running", lambda: self.coordinator.anchor_return(self.owner, assignment, ReturnEvidence(
                    "source", "returns/x.json", [], [], [], "completed", None, [], [], "2" * 64))),
                ("running", lambda: self.coordinator.record_return(self.owner, assignment, returned()))):
            self.sql("UPDATE assignments SET state=? WHERE id=?", state, assignment)
            with self.assertRaises(DomainError) as caught:
                self.mutate(call)
            self.assertEqual(closed, caught.exception.code, (state, caught.exception.message))

    def test_status_hides_closed_features_unless_all(self):
        other = self.mutate(self.coordinator.create_feature, self.owner, FeatureInput(
            "other", [], [], [], [], "main", "HEAD", "abc", str(self.main), [], ["*.md"]))
        self.close(self.decision())
        status = self.coordinator.status()
        self.assertEqual([other], status["feature_ids"])
        self.assertEqual([other], [row["id"] for row in status["features"]])
        self.assertEqual([self.feature_id], status["closed_feature_ids"])
        self.assertEqual(2, status["counts"]["features"])
        self.assertEqual({self.feature_id: "completed", other: "active"},
                         {row["id"]: row["state"] for row in self.coordinator.status(include_closed=True)["features"]})
        self.assertEqual("completed", self.coordinator.detail("feature", self.feature_id)["state"])

    def test_exact_assignment_replay_returns_its_id(self):
        assignment = self.create()
        self.mutate(self.coordinator.cancel_prepared, self.owner, assignment)
        self.close(self.decision())
        self.assertEqual(assignment, self.create())

    def test_attempt_resolve_is_not_blocked_by_the_state(self):
        assignment = self.create()
        self.mutate(self.coordinator.cancel_prepared, self.owner, assignment)
        self.close(self.decision())
        self.sql("UPDATE assignments SET state='running' WHERE id=?", assignment)
        data = b'{"evidence": "operator"}'
        directory = self.store.path.parent / "resolutions"
        directory.mkdir(exist_ok=True)
        (directory / f"{assignment}-1.json").write_bytes(data)
        self.mutate(self.coordinator.resolve_unknown_attempt, self.owner, assignment,
                    f"resolutions/{assignment}-1.json", digest(data), "process gone", "operator:checked")
        self.assertEqual("resolved-unknown", self.store.read_snapshot()["assignments"][0]["state"])


class EvidenceAfterCloseTests(unittest.TestCase):
    setUp = ReviewTests['setUp']
    file_digest = ReviewTests['file_digest']
    contracts = ReviewTests['contracts']
    mutate = ReviewTests['mutate']
    capture = ReviewTests['capture']
    review = ReviewTests['review']
    accepted = ReviewTests['accepted']
    code = ReviewTests['code']

    def close(self):
        decision = self.mutate(self.coordinator.record_decision, self.owner, DecisionInput(
            self.feature, 1, "operator-reported", "operator:merged", "merged", {}))
        self.mutate(self.coordinator.close_feature, self.owner, self.feature, 1, decision, "completed")

    def test_bundle_capture_and_review_refuse_without_writing(self):
        bundle = self.capture()
        self.close()
        bundles = sorted((self.store.path.parent / "bundles").iterdir())
        self.code("FEATURE_CLOSED", self.coordinator.capture_bundle, self.owner, BundleInput(
            self.feature, ["artifact.md"], ["dependency.md"], [self.assignment], self.contracts()))
        self.assertEqual(bundles, sorted((self.store.path.parent / "bundles").iterdir()))
        self.code("FEATURE_CLOSED", self.observed.record_review, self.owner,
                  ReviewEvidence(bundle, "reviewer-1", "test-source", "review:1", {"result": "ok"}, False))

    def test_accept_refuses_a_closed_feature(self):
        bundle = self.capture()
        review = self.review(bundle)
        self.close()
        self.code("FEATURE_CLOSED", self.coordinator.accept, self.owner, bundle, review)



class VerifyAfterCloseTests(unittest.TestCase):
    setUp = ReviewTests['setUp']
    file_digest = ReviewTests['file_digest']
    contracts = ReviewTests['contracts']
    mutate = ReviewTests['mutate']
    capture = ReviewTests['capture']
    review = ReviewTests['review']
    accepted = ReviewTests['accepted']
    close = EvidenceAfterCloseTests.__dict__['close']

    def test_closed_acceptance_is_closed_without_its_worktree(self):
        self.accepted()
        self.close()
        for name in ("brief.md", "contract.md", "artifact.md", "dependency.md"):
            (self.main / name).unlink()
        report = self.coordinator.verify()
        self.assertEqual(("closed", False), (report.checked[0]["current"], report.checked[0]["files_compared"]))
        self.assertEqual("closed", report.current)

    def test_closed_acceptance_keeps_failing_checks(self):
        bundle = self.accepted()
        self.review(bundle, reviewer="reviewer-2", critical=True)
        self.close()
        report = self.coordinator.verify()
        self.assertEqual(("unknown", "open-critical-finding"), (report.current, report.checked[0]["reason"]))

    def test_closed_acceptance_with_a_damaged_bundle_copy_is_unknown(self):
        self.accepted()
        self.close()
        path = Path(self.store.read_snapshot()["bundles"][0]["path"])
        copy = next(item for item in path.rglob("*") if item.is_file() and item.name == "artifact.md")
        copy.chmod(0o644)
        copy.write_text("damaged")
        report = self.coordinator.verify()
        self.assertEqual(("unknown", "bundle-copy"), (report.checked[0]["current"], report.checked[0]["reason"]))

    def test_aggregate_order_with_closed_entries(self):
        self.accepted()
        self.close()
        other = self.mutate(self.coordinator.create_feature, self.owner, FeatureInput(
            "other", [], [], ["checks"], ["edit"], "main", "HEAD", "abc", str(self.main), [], ["*.md"]))
        (self.main / "other.md").write_text("other v1")
        source = [{"path": "brief.md", "digest": self.file_digest("brief.md")}]
        contracts = self.contracts()
        assignment = self.mutate(self.coordinator.create_assignment, self.owner, AssignmentInput(
            other, "key-2", "brief.md", 1, source, contracts,
            digest(json.dumps(contracts, sort_keys=True, separators=(",", ":")).encode()),
            ["other.md"], "workspace-write", "author-2", [], []))
        self.mutate(self.coordinator.reserve_launch, self.owner, assignment)
        self.mutate(self.observed.record_return, self.owner, assignment, ReturnEvidence(
            "test-source", "return:2", [{"path": "other.md", "digest": self.file_digest("other.md")}],
            ["checks"], [], "completed", None, [], [], digest(b"return:2")))
        bundle = self.mutate(self.coordinator.capture_bundle, self.owner, BundleInput(
            other, ["other.md"], [], [assignment], contracts))
        self.mutate(self.coordinator.accept, self.owner, bundle, self.review(bundle))
        report = self.coordinator.verify()
        self.assertEqual(["closed", "current"], sorted(item["current"] for item in report.checked))
        self.assertEqual("current", report.current)


class ExportCloseTests(unittest.TestCase):
    setUp = ExportTests['setUp']
    ext4 = ExportTests['ext4']
    export = ExportTests['export']
    accepted = ExportTests['accepted']

    def close(self, resolution="completed", store=None, owner=None):
        store, owner = store or self.store, owner or self.owner
        with self.ext4():
            coordinator = Coordinator(store)
            feature = store.read_snapshot()["features"][0]["id"]
            decision = coordinator.record_decision(owner, DecisionInput(
                feature, 1, "operator-reported", "operator:merged", "merged", {}))
            return coordinator.close_feature(owner, feature, 1, decision, resolution)

    def clone(self, receipt, mapped=False):
        clone = self.root / "fresh"
        subprocess.run(["git", "clone", "-q", str(self.main), str(clone)], check=True)
        repo = resolve_repo(clone)
        with self.ext4():
            import_checkpoint(repo, receipt.directory, {self.feature: clone} if mapped else None)
        return repo, open_store(repo).read_snapshot()

    def test_closed_feature_exports_without_its_worktree_and_imports_closed(self):
        self.accepted()
        self.close()
        stored = json.loads(self.store.read_snapshot()["features"][0]["portable_file_digests_json"])
        for name in ("input.md", "artifact.md", "contract.md", "dependency.md"):
            (self.main / name).unlink()
        receipt = self.export()
        checkpoint = json.loads((receipt.directory / "checkpoint.json").read_text())
        self.assertEqual(stored, checkpoint["features"][0]["file_digests"])
        self.assertIsNotNone(stored[0]["digest"])
        _, state = self.clone(receipt)
        self.assertEqual(("completed", ""), (state["features"][0]["state"], state["features"][0]["worktree"]))
        self.assertEqual({"unknown"}, {row["state"] for row in state["assignments"]})

    def test_mapped_closed_feature_stays_closed(self):
        self.accepted()
        self.close("abandoned")
        repo, state = self.clone(self.export(), mapped=True)
        self.assertEqual(("abandoned", str(repo.worktree)),
                         (state["features"][0]["state"], state["features"][0]["worktree"]))

    def test_imported_unresolved_feature_with_history_closes(self):
        self.accepted()
        repo, state = self.clone(self.export())
        self.assertEqual("unresolved", state["features"][0]["state"])
        with self.ext4():
            store = open_store(repo)
            owner = Coordinator(store).claim("session", self.root / "fresh.token")
        self.assertEqual("abandoned", self.close("abandoned", store, owner))

if __name__ == "__main__":
    unittest.main()
