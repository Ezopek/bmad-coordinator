import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from coordinator_core.model import AssignmentInput, BundleInput, DecisionInput, DomainError, FeatureInput, ReturnEvidence, ReviewEvidence
from coordinator_core.repo import resolve_repo
from coordinator_core.service import Coordinator
from coordinator_core.store import open_store
from tests.support import ext4_mountinfo


def digest(data):
    return hashlib.sha256(data).hexdigest()


class ObservedReview:
    def classify_return(self, event):
        return "observed"

    def classify_review(self, event):
        return "observed"


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.main = self.root / "main"
        self.main.mkdir()
        subprocess.run(["git", "init", "-q", str(self.main)], check=True)
        subprocess.run(["git", "-C", str(self.main), "-c", "user.name=Test", "-c",
                        "user.email=test@example.invalid", "commit", "--allow-empty", "-qm", "initial"], check=True)
        for name in ("brief.md", "contract.md", "artifact.md", "dependency.md"):
            (self.main / name).write_text(name + " v1")
        self.repo = resolve_repo(self.main)
        with patch("coordinator_core.store.read_mountinfo", return_value=ext4_mountinfo(self.root)):
            self.store = open_store(self.repo, create=True)
        self.coordinator = Coordinator(self.store)
        self.observed = Coordinator(self.store, ObservedReview())
        self.owner = self.mutate(self.coordinator.claim, "session", self.root / "owner.token")
        self.feature = self.mutate(self.coordinator.create_feature, self.owner, FeatureInput(
            "ship", [], [], ["checks"], ["edit"], "main", "HEAD", "abc", str(self.main), [], ["*.md"]))
        source = [{"path": "brief.md", "digest": self.file_digest("brief.md")}]
        contracts = self.contracts()
        self.assignment = self.mutate(self.coordinator.create_assignment, self.owner, AssignmentInput(
            self.feature, "key-1", "brief.md", 1, source, contracts,
            digest(json.dumps(contracts, sort_keys=True, separators=(",", ":")).encode()),
            ["artifact.md"], "workspace-write", "author-1", [], []))
        self.mutate(self.coordinator.reserve_launch, self.owner, self.assignment)
        self.mutate(self.observed.record_return, self.owner, self.assignment, ReturnEvidence(
            "test-source", "return:1", [{"path": "artifact.md", "digest": self.file_digest("artifact.md")}],
            ["checks"], [], "completed", None, [], [], digest(b"return:1")))

    def file_digest(self, name):
        return digest((self.main / name).read_bytes())

    def contracts(self):
        return [{"path": "contract.md", "digest": self.file_digest("contract.md")}]

    def mutate(self, method, *args):
        with patch("coordinator_core.store.read_mountinfo", return_value=ext4_mountinfo(self.root)):
            return method(*args)

    def capture(self, **changes):
        values = dict(feature_id=self.feature, artifact_paths=["artifact.md"],
                      dependency_paths=["dependency.md"], source_assignment_ids=[self.assignment],
                      contract_files=self.contracts())
        values.update(changes)
        return self.mutate(self.coordinator.capture_bundle, self.owner, BundleInput(**values))

    def review(self, bundle, reviewer="reviewer-1", observed=True, critical=False):
        coordinator = self.observed if observed else self.coordinator
        return self.mutate(coordinator.record_review, self.owner,
                           ReviewEvidence(bundle, reviewer, "test-source", "review:1", {"result": "ok"}, critical))

    def accepted(self):
        bundle = self.capture()
        review = self.review(bundle)
        self.mutate(self.coordinator.accept, self.owner, bundle, review)
        return bundle

    def code(self, code, method, *args):
        with self.assertRaises(DomainError) as caught:
            self.mutate(method, *args)
        self.assertEqual(code, caught.exception.code)

    def test_mutation_during_capture_is_unknown(self):
        from coordinator_core import bundles
        original = bundles.copy_file
        def mutate_after_copy(source, target):
            original(source, target)
            (self.main / "dependency.md").write_text("dependency.md v2")
        with patch("coordinator_core.bundles.copy_file", side_effect=mutate_after_copy):
            self.code("UNKNOWN_EVIDENCE", self.coordinator.capture_bundle, self.owner,
                      BundleInput(self.feature, ["artifact.md"], ["dependency.md"],
                                  [self.assignment], self.contracts()))
        self.assertEqual([], self.store.read_snapshot()["bundles"])
        (self.main / "dependency.md").write_text("dependency.md v1")
        self.assertTrue(self.capture())

    def test_reviewer_author_rejected(self):
        bundle = self.capture()
        self.assertIn("author-1", self.coordinator.detail("bundle", bundle)["derived_author_ids"])
        self.code("INVALID_INPUT", self.observed.record_review, self.owner,
                  ReviewEvidence(bundle, "author-1", "test-source", "review:1", {}, False))

    def test_caller_cannot_override_author_provenance(self):
        with self.assertRaises(TypeError):
            BundleInput(self.feature, ["artifact.md"], ["dependency.md"], [self.assignment],
                        self.contracts(), derived_author_ids=["reviewer-1"])
        bundle = self.capture()
        self.assertEqual(["author-1"], self.coordinator.detail("bundle", bundle)["derived_author_ids"])

    def test_unverified_review_cannot_accept(self):
        bundle = self.capture()
        review = self.review(bundle, observed=False)
        self.code("BLOCKED_EVIDENCE", self.coordinator.accept, self.owner, bundle, review)
        self.assertEqual([], self.store.read_snapshot()["acceptances"])

    def test_changed_artifact_dependency_mandate_and_contract_stale_acceptance(self):
        for changed in ("artifact.md", "dependency.md", "contract.md", "mandate"):
            with self.subTest(changed=changed):
                bundle = self.accepted()
                self.assertEqual("current", self.coordinator.verify().current)
                if changed == "mandate":
                    decision = self.mutate(self.coordinator.record_decision, self.owner,
                                           DecisionInput(self.feature, 1, "operator-reported", "conversation:1", "revise", {}))
                    self.assertEqual(2, self.mutate(self.coordinator.revise_mandate, self.owner,
                                                    self.feature, 1, {"exclusions": ["new"]}, decision))
                else:
                    (self.main / changed).write_bytes((self.main / changed).read_bytes() + b"x")
                self.assertTrue(self.coordinator.verify().historical_accepted)
                report = self.coordinator.verify()
                self.assertEqual("stale", report.current)
                self.assertEqual("stale", report.checked[-1]["current"])
                self.assertEqual(64, len(report.checked[-1]["manifest_digest"]))
                self.assertEqual(1, report.checked[-1]["mandate_revision"])
                self.assertEqual(3, len(report.checked[-1]["files"]))
                self.code("STALE_INPUT", self.coordinator.accept, self.owner, bundle,
                          self.store.read_snapshot()["reviews"][-1]["id"])
                if changed == "mandate":
                    break
                (self.main / changed).write_text(changed + " v1")

    def test_stale_handoff_metadata_only_does_not_force_rereview(self):
        self.accepted()
        revision = self.store.read_snapshot()["revision"]
        self.mutate(self.coordinator.record_decision, self.owner,
                    DecisionInput(self.feature, 1, "operator-reported", "conversation:2", "note", {}))
        self.assertGreater(self.store.read_snapshot()["revision"], revision)
        self.assertEqual("current", self.coordinator.verify().current)

    def test_missing_dependency_is_unknown(self):
        self.accepted()
        (self.main / "dependency.md").unlink()
        self.assertEqual("unknown", self.coordinator.verify().current)

    def test_malformed_bundle_paths_fail_as_domain_input(self):
        self.code("INVALID_INPUT", self.coordinator.capture_bundle, self.owner,
                  BundleInput(self.feature, None, ["dependency.md"], [self.assignment], self.contracts()))

    def test_prepared_assignment_is_not_artifact_provenance(self):
        prepared = self.new_assignment("prepared", "author-2")
        self.code("BLOCKED_EVIDENCE", self.coordinator.capture_bundle, self.owner,
                  BundleInput(self.feature, ["artifact.md"], ["dependency.md"],
                              [prepared], self.contracts()))

    def test_omitted_editor_cannot_self_review(self):
        editor = self.new_assignment("editor", "editor-2")
        (self.main / "artifact.md").write_text("artifact.md v2")
        self.mutate(self.coordinator.reserve_launch, self.owner, editor)
        self.mutate(self.observed.record_return, self.owner, editor, ReturnEvidence(
            "test-source", "return:editor", [{"path": "artifact.md", "digest": self.file_digest("artifact.md")}],
            ["checks"], [], "completed", None, [], [], digest(b"return:editor")))
        self.code("BLOCKED_EVIDENCE", self.coordinator.capture_bundle, self.owner,
                  BundleInput(self.feature, ["artifact.md"], ["dependency.md"],
                              [self.assignment], self.contracts()))
        bundle = self.capture(source_assignment_ids=[self.assignment, editor])
        self.assertEqual(["author-1", "editor-2"],
                         self.coordinator.detail("bundle", bundle)["derived_author_ids"])
        self.code("INVALID_INPUT", self.observed.record_review, self.owner,
                  ReviewEvidence(bundle, "editor-2", "test-source", "review:editor", {}, False))

    def new_assignment(self, key, author, mandate_revision=1, paths=None):
        contracts = self.contracts()
        return self.mutate(self.coordinator.create_assignment, self.owner, AssignmentInput(
            self.feature, key, "brief.md", mandate_revision,
            [{"path": "brief.md", "digest": self.file_digest("brief.md")}], contracts,
            digest(json.dumps(contracts, sort_keys=True, separators=(",", ":")).encode()),
            paths or ["artifact.md"], "workspace-write", author, [], []))

    def test_artifact_alias_keeps_every_author_out_of_review(self):
        (self.main / "alias.md").symlink_to("artifact.md")
        editors = []
        for alias in ("./artifact.md", "././/artifact.md", "alias.md"):
            with self.subTest(alias=alias):
                editor = self.new_assignment("alias-" + alias, "author-2", paths=[alias])
                editors.append(editor)
                self.mutate(self.coordinator.reserve_launch, self.owner, editor)
                self.mutate(self.observed.record_return, self.owner, editor, ReturnEvidence(
                    "test-source", "return:" + alias, [{"path": alias, "digest": self.file_digest(alias)}],
                    ["checks"], [], "completed", None, [], [], digest(alias.encode())))
                self.code("BLOCKED_EVIDENCE", self.coordinator.capture_bundle, self.owner,
                          BundleInput(self.feature, [alias], ["dependency.md"], [editor], self.contracts()))
        bundle = self.capture(artifact_paths=["alias.md"], source_assignment_ids=[self.assignment, *editors])
        self.assertEqual(["author-1", "author-2"],
                         self.coordinator.detail("bundle", bundle)["derived_author_ids"])
        self.code("INVALID_INPUT", self.observed.record_review, self.owner,
                  ReviewEvidence(bundle, "author-1", "test-source", "review:alias", {}, False))

    def test_later_critical_review_makes_that_acceptance_unknown(self):
        bundle = self.accepted()
        self.review(bundle, reviewer="reviewer-2", critical=True)
        report = self.coordinator.verify()
        self.assertTrue(report.historical_accepted)
        self.assertEqual("unknown", report.current)
        self.assertEqual("unknown", report.checked[0]["current"])
        self.assertEqual("open-critical-finding", report.checked[0]["reason"])

    def test_symlink_retarget_does_not_erase_recorded_author(self):
        alias = self.main / "alias.md"
        alias.symlink_to("artifact.md")
        editor = self.new_assignment("alias-retarget", "author-2", paths=["alias.md"])
        self.mutate(self.coordinator.reserve_launch, self.owner, editor)
        self.mutate(self.observed.record_return, self.owner, editor, ReturnEvidence(
            "test-source", "return:alias-retarget",
            [{"path": "alias.md", "digest": self.file_digest("alias.md")}],
            ["checks"], [], "completed", None, [], [], digest(b"return:alias-retarget")))
        (self.main / "other.md").write_bytes((self.main / "artifact.md").read_bytes())
        alias.unlink()
        alias.symlink_to("other.md")
        self.code("BLOCKED_EVIDENCE", self.coordinator.capture_bundle, self.owner,
                  BundleInput(self.feature, ["artifact.md"], ["dependency.md"], [self.assignment], self.contracts()))
        bundle = self.capture(source_assignment_ids=[self.assignment, editor])
        self.code("INVALID_INPUT", self.observed.record_review, self.owner,
                  ReviewEvidence(bundle, "author-2", "test-source", "review:retarget", {}, False))

    def test_critical_on_one_bundle_keeps_other_acceptance_current(self):
        first = self.accepted()
        (self.main / "second-dependency.md").write_text("second dependency")
        second = self.capture(dependency_paths=["second-dependency.md"])
        review = self.review(second, reviewer="reviewer-2")
        self.mutate(self.coordinator.accept, self.owner, second, review)
        self.review(first, reviewer="reviewer-3", critical=True)
        report = self.coordinator.verify()
        self.assertEqual("current", report.current)
        self.assertEqual({first: "unknown", second: "current"},
                         {item["bundle_id"]: item["current"] for item in report.checked})

    def test_concurrent_publish_reuses_winner_without_deleting_it(self):
        from coordinator_core import bundles
        original_mkdir = Path.mkdir
        triggered = False
        def publish_winner(path, *args, **kwargs):
            nonlocal triggered
            if not triggered and path.parent.name == "bundles" and len(path.name) == 64:
                triggered = True
                bundles.capture(self.main, self.store.path.parent / "bundles", self.feature, 1,
                                ["artifact.md"], ["dependency.md"], self.contracts(),
                                [self.assignment], ["author-1"])
            return original_mkdir(path, *args, **kwargs)
        with patch.object(Path, "mkdir", publish_winner):
            bundle = self.capture()
        self.assertTrue(triggered)
        path = self.store.path.parent / "bundles" / bundle
        self.assertTrue(path.is_dir())
        self.assertTrue(bundles.compare_copy(path, bundles.read_manifest(path, bundle)))

    def test_historical_author_survives_mandate_revision_rebundle(self):
        old_bundle = self.accepted()
        decision = self.mutate(self.coordinator.record_decision, self.owner,
                               DecisionInput(self.feature, 1, "operator-reported", "conversation:mandate", "revise", {}))
        self.assertEqual(2, self.mutate(self.coordinator.revise_mandate, self.owner,
                                        self.feature, 1, {"exclusions": ["new"]}, decision))
        editor = self.new_assignment("mandate-editor", "editor-2", mandate_revision=2)
        (self.main / "artifact.md").write_text("artifact.md v2")
        self.mutate(self.coordinator.reserve_launch, self.owner, editor)
        self.mutate(self.observed.record_return, self.owner, editor, ReturnEvidence(
            "test-source", "return:mandate-editor", [{"path": "artifact.md", "digest": self.file_digest("artifact.md")}],
            ["checks"], [], "completed", None, [], [], digest(b"return:mandate-editor")))
        bundle = self.capture(source_assignment_ids=[self.assignment, editor])
        self.assertNotEqual(old_bundle, bundle)
        self.assertEqual(["author-1", "editor-2"], self.coordinator.detail("bundle", bundle)["derived_author_ids"])
        self.code("INVALID_INPUT", self.observed.record_review, self.owner,
                  ReviewEvidence(bundle, "author-1", "test-source", "review:old-author", {}, False))
        review = self.review(bundle)
        self.mutate(self.coordinator.accept, self.owner, bundle, review)
        self.assertEqual("current", self.coordinator.verify().checked[-1]["current"])

    def test_historical_author_survives_contract_revision_rebundle(self):
        old_bundle = self.accepted()
        (self.main / "contract.md").write_text("contract.md v2")
        editor = self.new_assignment("contract-editor", "editor-2")
        (self.main / "artifact.md").write_text("artifact.md v2")
        self.mutate(self.coordinator.reserve_launch, self.owner, editor)
        self.mutate(self.observed.record_return, self.owner, editor, ReturnEvidence(
            "test-source", "return:contract-editor", [{"path": "artifact.md", "digest": self.file_digest("artifact.md")}],
            ["checks"], [], "completed", None, [], [], digest(b"return:contract-editor")))
        bundle = self.capture(source_assignment_ids=[self.assignment, editor])
        self.assertNotEqual(old_bundle, bundle)
        self.assertEqual(["author-1", "editor-2"], self.coordinator.detail("bundle", bundle)["derived_author_ids"])
        self.code("INVALID_INPUT", self.observed.record_review, self.owner,
                  ReviewEvidence(bundle, "author-1", "test-source", "review:old-author", {}, False))
        review = self.review(bundle)
        self.mutate(self.coordinator.accept, self.owner, bundle, review)
        self.assertEqual("current", self.coordinator.verify().checked[-1]["current"])
