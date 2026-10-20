import json
import hashlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from coordinator_core.exporter import export_checkpoint, import_checkpoint, validate_checkpoint
from coordinator_core.model import (AssignmentInput, BundleInput, DomainError, FeatureInput,
                                    ReturnEvidence, ReviewEvidence)
from coordinator_core.repo import resolve_repo
from coordinator_core.service import Coordinator
from coordinator_core.store import open_store
from tests.support import ext4_mountinfo


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.main = self.root / "main"
        self.main.mkdir()
        subprocess.run(["git", "init", "-q", str(self.main)], check=True)
        subprocess.run(["git", "-C", str(self.main), "-c", "user.name=Test", "-c",
                        "user.email=test@example.invalid", "commit", "--allow-empty", "-qm", "initial"], check=True)
        (self.main / "input.md").write_text("source")
        self.repo = resolve_repo(self.main)
        with self.ext4():
            self.store = open_store(self.repo, create=True)
            self.owner = Coordinator(self.store).claim("session", self.root / "owner.token")
            self.feature = Coordinator(self.store).create_feature(self.owner, FeatureInput(
                "ship", [], [], [], [], "main", "HEAD", "abc", str(self.main), ["input.md"], ["*.md"]))
        self.target = self.root / "exports"
        self.target.mkdir()

    def ext4(self):
        return patch("coordinator_core.store.read_mountinfo", return_value=ext4_mountinfo(self.root))

    def export(self):
        with self.ext4():
            return export_checkpoint(self.store, self.owner, self.target)

    def accepted(self, author="author", artifact="artifact.md"):
        class Observed:
            def classify_return(self, event):
                return "observed"
            def classify_review(self, event):
                return "observed"
        for name in ("contract.md", artifact, "dependency.md"):
            (self.main / name).write_text(name)
        digest = lambda name: hashlib.sha256((self.main / name).read_bytes()).hexdigest()
        contract = [{"path": "contract.md", "digest": digest("contract.md")}]
        contract_digest = hashlib.sha256(json.dumps(contract, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        coordinator = Coordinator(self.store, Observed())
        with self.ext4():
            assignment = coordinator.create_assignment(self.owner, AssignmentInput(
                self.feature, "key", "input.md", 1, [{"path": "input.md", "digest": digest("input.md")}],
                contract, contract_digest, [artifact], "workspace-write", author, [], []))
            coordinator.reserve_launch(self.owner, assignment)
            coordinator.record_return(self.owner, assignment, ReturnEvidence(
                "source", "return:1", [{"path": artifact, "digest": digest(artifact)}],
                [], [], "completed", None, [], [], hashlib.sha256(b"return").hexdigest()))
            bundle = coordinator.capture_bundle(self.owner, BundleInput(
                self.feature, [artifact], ["dependency.md"], [assignment], contract))
            review = coordinator.record_review(self.owner, ReviewEvidence(bundle, "reviewer", "source", "review:1", {}, False))
            coordinator.accept(self.owner, bundle, review)
        return self.store.read_snapshot()["bundles"][0]["path"]

    def code(self, expected, call):
        with self.assertRaises(DomainError) as caught:
            call()
        self.assertEqual(expected, caught.exception.code)

    def changed_checkpoint(self, receipt, change):
        data = json.loads((receipt.directory / "checkpoint.json").read_bytes())
        data.pop("digest")
        change(data)
        digest = hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":"),
                                         ensure_ascii=False).encode()).hexdigest()
        data["digest"] = digest
        directory = self.target / f"checkpoint-r{data['revision']}-{digest}"
        shutil.copytree(receipt.directory, directory)
        (directory / "checkpoint.json").write_text(json.dumps(data, sort_keys=True, separators=(",", ":"),
                                                             ensure_ascii=False))
        (directory / "checkpoint.md").write_text(
            f"# Coordinator checkpoint\n\nRevision: {data['revision']}\nDigest: {digest}\n\n"
            "This handoff is historical evidence. Resolve worktrees and verify files before continuing.\n")
        return directory

    def test_successful_export_clears_pending_without_revision_bump(self):
        n = self.store.read_snapshot()["revision"]
        receipt = self.export()
        state = self.store.read_snapshot()
        self.assertEqual(n, state["revision"])
        self.assertEqual(n, state["export_revision"])
        self.assertEqual(0, state["export_pending"])
        self.assertEqual(receipt.directory.name, state["export_dir_name"])
        checkpoint = validate_checkpoint(receipt.directory)
        self.assertEqual(n, checkpoint.revision)
        self.assertEqual(self.feature, checkpoint.features[0]["id"])
        self.assertNotIn(str(self.main), (receipt.directory / "checkpoint.json").read_text())
        self.assertNotIn(self.owner.token, (receipt.directory / "checkpoint.json").read_text())

    def test_crash_before_publish_keeps_pending(self):
        with patch("coordinator_core.exporter._publish_directory", side_effect=OSError("crash")):
            with self.assertRaises(OSError):
                self.export()
        self.assertEqual(1, self.store.read_snapshot()["export_pending"])
        self.assertFalse(list(self.target.glob("checkpoint-r*")))

    def test_crash_after_publish_before_pointer_keeps_pending(self):
        with patch.object(self.store, "commit_export", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self.export()
        self.assertEqual(1, self.store.read_snapshot()["export_pending"])
        self.assertEqual(1, len(list(self.target.glob("checkpoint-r*"))))

    def test_stale_exporter_cannot_replace_newer_pair_or_pointer(self):
        original = self.store.commit_export
        def race(*args):
            with self.ext4():
                Coordinator(self.store).create_feature(self.owner, FeatureInput(
                    "later", [], [], [], [], "main", "HEAD", "abc", str(self.main), [], ["*.md"]))
            return original(*args)
        with patch.object(self.store, "commit_export", side_effect=race):
            self.code("STALE_INPUT", self.export)
        self.assertEqual(1, len(list(self.target.glob("checkpoint-r*"))))
        self.assertIsNone(self.store.read_snapshot()["export_dir_name"])
        newer = self.export()
        self.assertEqual(newer.directory.name, self.store.read_snapshot()["export_dir_name"])
        self.assertEqual(2, len(list(self.target.glob("checkpoint-r*"))))

    def test_modified_published_directory_conflicts(self):
        receipt = self.export()
        (receipt.directory / "checkpoint.md").write_text("altered")
        self.code("CHECKPOINT_CONFLICT", self.export)

    def test_matching_directory_reused(self):
        first = self.export()
        second = self.export()
        self.assertEqual(first.directory, second.directory)
        self.assertEqual(1, len(list(self.target.glob("checkpoint-r*"))))

    def test_stale_export_rejected_for_same_repository(self):
        first = self.export()
        with self.ext4():
            Coordinator(self.store).create_feature(self.owner, FeatureInput(
                "later", [], [], [], [], "main", "HEAD", "abc", str(self.main), [], ["*.md"]))
        self.code("STALE_INPUT", lambda: import_checkpoint(self.repo, first.directory))

    def test_sibling_linked_worktree_remap(self):
        receipt = self.export()
        sibling = self.root / "sibling"
        subprocess.run(["git", "-C", str(self.main), "worktree", "add", "-q", "-b", "sibling", str(sibling)], check=True)
        (sibling / "input.md").write_text("source")
        clone = self.root / "clone"
        subprocess.run(["git", "clone", "-q", str(self.main), str(clone)], check=True)
        clone_repo = resolve_repo(clone)
        with self.ext4():
            self.code("INVALID_REPOSITORY", lambda: import_checkpoint(clone_repo, receipt.directory,
                                                                        {self.feature: sibling}))
        # A destination's own sibling worktree has the same canonical common dir.
        clone_sibling = self.root / "clone-sibling"
        subprocess.run(["git", "-C", str(clone), "worktree", "add", "-q", "-b", "mapped", str(clone_sibling)], check=True)
        (clone_sibling / "input.md").write_text("source")
        with self.ext4():
            report = import_checkpoint(clone_repo, receipt.directory, {self.feature: clone_sibling})
        self.assertEqual(str(clone_sibling), report.worktrees[self.feature])
        self.assertEqual(str(clone_sibling), open_store(clone_repo).read_snapshot()["features"][0]["worktree"])

    def test_mapped_changed_file_remains_unresolved(self):
        receipt = self.export()
        clone = self.root / "fresh"
        subprocess.run(["git", "clone", "-q", str(self.main), str(clone)], check=True)
        (clone / "input.md").write_text("changed")
        repo = resolve_repo(clone)
        with self.ext4():
            report = import_checkpoint(repo, receipt.directory, {self.feature: clone})
        self.assertIn(self.feature, report.unknown_features)
        self.assertEqual("unresolved", open_store(repo).read_snapshot()["features"][0]["state"])

    def test_fresh_clone_import_marks_unfinished_unknown_and_acceptance_noncurrent(self):
        self.accepted()
        receipt = self.export()
        clone = self.root / "fresh"
        subprocess.run(["git", "clone", "-q", str(self.main), str(clone)], check=True)
        repo = resolve_repo(clone)
        with self.ext4():
            report = import_checkpoint(repo, receipt.directory)
        state = open_store(repo).read_snapshot()
        self.assertNotEqual(self.store.repository_id, state["repository_id"])
        self.assertEqual("unresolved", state["features"][0]["state"])
        self.assertEqual("", state["features"][0]["worktree"])
        self.assertEqual("released", state["owner"]["state"])
        self.assertEqual("unknown", Coordinator(open_store(repo)).verify().current)
        self.assertTrue(Coordinator(open_store(repo)).verify().historical_accepted)
        self.assertEqual("unknown", state["assignments"][0]["state"])
        self.assertIsNone(state["assignments"][0]["launch_ref"])
        self.assertEqual(self.store.repository_id, report.source_repository_id)
        self.assertEqual({}, report.worktrees)

    def test_unicode_bundle_manifest_exports_validates_and_imports(self):
        self.accepted(author="Łukasz", artifact="zażółć.md")
        receipt = self.export()
        checkpoint = validate_checkpoint(receipt.directory)
        self.assertEqual("Łukasz", checkpoint.records["bundles"][0]["manifest"]["derived_author_ids"][0])
        clone = self.root / "fresh-unicode"
        subprocess.run(["git", "clone", "-q", str(self.main), str(clone)], check=True)
        with self.ext4():
            import_checkpoint(resolve_repo(clone), receipt.directory)
        self.assertTrue(Coordinator(open_store(resolve_repo(clone))).verify().historical_accepted)
        altered = self.changed_checkpoint(receipt, lambda data:
            data["records"]["bundles"][0]["manifest"]["derived_author_ids"].append("intruder"))
        self.code("INVALID_CHECKPOINT", lambda: validate_checkpoint(altered))

    def test_imported_acceptance_is_unknown_even_with_matching_mapped_files_and_old_bundle_cwd(self):
        old_bundle = Path(self.accepted())
        receipt = self.export()
        clone = self.root / "fresh"
        subprocess.run(["git", "clone", "-q", str(self.main), str(clone)], check=True)
        for name in ("input.md", "contract.md", "artifact.md", "dependency.md"):
            (clone / name).write_bytes((self.main / name).read_bytes())
        repo = resolve_repo(clone)
        with self.ext4():
            import_checkpoint(repo, receipt.directory, {self.feature: clone})
        current_directory = Path.cwd()
        try:
            import os
            os.chdir(old_bundle)
            report = Coordinator(open_store(repo)).verify()
        finally:
            os.chdir(current_directory)
        self.assertTrue(report.historical_accepted)
        self.assertEqual("unknown", report.current)

    def test_validation_rejects_tamper_escape_and_nonempty_import(self):
        receipt = self.export()
        clone = self.root / "fresh"
        subprocess.run(["git", "clone", "-q", str(self.main), str(clone)], check=True)
        repo = resolve_repo(clone)
        original = (receipt.directory / "checkpoint.json").read_bytes()
        for index, mutate in enumerate((lambda d: d.update(digest="0" * 64),
                                        lambda d: d["features"][0].update(dirty_paths_json='["../escape"]'),
                                        lambda d: d.update(format=999))):
            data = json.loads(original)
            mutate(data)
            if index:
                data.pop("digest")
                digest = hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":"),
                                                 ensure_ascii=False).encode()).hexdigest()
                data["digest"] = digest
                altered = self.target / f"checkpoint-r{data['revision']}-{digest}"
                shutil.copytree(receipt.directory, altered)
                (altered / "checkpoint.md").write_text(
                    f"# Coordinator checkpoint\n\nRevision: {data['revision']}\nDigest: {digest}\n\n"
                    "This handoff is historical evidence. Resolve worktrees and verify files before continuing.\n")
            else:
                altered = receipt.directory
            (altered / "checkpoint.json").write_text(json.dumps(data, sort_keys=True, separators=(",", ":"),
                                                                ensure_ascii=False))
            self.code("INVALID_CHECKPOINT", lambda: import_checkpoint(repo, altered))
            self.assertFalse((repo.common_dir / "coordinator" / "state.sqlite3").exists())
        (receipt.directory / "checkpoint.json").write_bytes(original)
        with self.ext4():
            import_checkpoint(repo, receipt.directory)
        self.code("NONEMPTY_STATE", lambda: import_checkpoint(repo, receipt.directory))

    def test_nonempty_coordinator_directory_blocks_import(self):
        receipt = self.export()
        clone = self.root / "fresh"
        subprocess.run(["git", "clone", "-q", str(self.main), str(clone)], check=True)
        repo = resolve_repo(clone)
        directory = repo.common_dir / "coordinator"
        directory.mkdir()
        (directory / "unclaimed.txt").write_text("existing state")
        self.code("NONEMPTY_STATE", lambda: import_checkpoint(repo, receipt.directory))
        self.assertFalse((directory / "state.sqlite3").exists())

    def test_concurrent_initializer_and_claim_win_without_import_writing_their_database(self):
        receipt = self.export()
        clone = self.root / "fresh"
        subprocess.run(["git", "clone", "-q", str(self.main), str(clone)], check=True)
        repo = resolve_repo(clone)
        real_open = open_store
        winner = {}
        def concurrent_open(*args, **kwargs):
            if kwargs.get("create"):
                winner["store"] = real_open(repo, create=True)
                winner["owner"] = Coordinator(winner["store"]).claim("winner", self.root / "winner.token")
            return real_open(*args, **kwargs)
        with self.ext4(), patch("coordinator_core.exporter.open_store", side_effect=concurrent_open):
            self.code("NONEMPTY_STATE", lambda: import_checkpoint(repo, receipt.directory))
        snapshot = winner["store"].read_snapshot()
        self.assertEqual("winner", snapshot["owner"]["session_id"])
        self.assertEqual([], snapshot["features"])

    def test_unmapped_import_can_be_reexported_without_cwd_path(self):
        source = self.export()
        clone = self.root / "fresh"
        subprocess.run(["git", "clone", "-q", str(self.main), str(clone)], check=True)
        repo = resolve_repo(clone)
        with self.ext4():
            import_checkpoint(repo, source.directory)
            imported = open_store(repo)
            owner = Coordinator(imported).claim("new", self.root / "new.token")
            target = self.root / "fresh-exports"
            target.mkdir()
            second = export_checkpoint(imported, owner, target)
        checkpoint = validate_checkpoint(second.directory)
        self.assertEqual(self.feature, checkpoint.features[0]["id"])
        self.assertEqual([{"path": "input.md", "digest": hashlib.sha256(b"source").hexdigest()}],
                         checkpoint.features[0]["file_digests"])
        self.assertEqual("unresolved", imported.read_snapshot()["features"][0]["state"])

    def test_semantic_cross_record_mismatch_rejected_before_db_creation(self):
        self.accepted()
        receipt = self.export()
        def contradict(data):
            data["records"]["bundles"].append({**data["records"]["bundles"][0], "id": "second-bundle"})
            data["records"]["acceptances"][0]["bundle_id"] = "second-bundle"
        altered = self.changed_checkpoint(receipt, contradict)
        clone = self.root / "fresh"
        subprocess.run(["git", "clone", "-q", str(self.main), str(clone)], check=True)
        repo = resolve_repo(clone)
        with self.ext4():
            self.code("INVALID_CHECKPOINT", lambda: import_checkpoint(repo, altered))
        self.assertFalse((repo.common_dir / "coordinator" / "state.sqlite3").exists())

    def test_missing_required_file_digest_cannot_activate_mapped_feature(self):
        receipt = self.export()
        altered = self.changed_checkpoint(receipt, lambda data: data["features"][0].update(file_digests=[]))
        clone = self.root / "fresh"
        subprocess.run(["git", "clone", "-q", str(self.main), str(clone)], check=True)
        (clone / "input.md").write_text("source")
        repo = resolve_repo(clone)
        with self.ext4():
            self.code("INVALID_CHECKPOINT", lambda: import_checkpoint(repo, altered, {self.feature: clone}))
        self.assertFalse((repo.common_dir / "coordinator" / "state.sqlite3").exists())

    def test_reexport_rehashes_active_mapped_feature_after_local_change(self):
        source = self.export()
        clone = self.root / "fresh"
        subprocess.run(["git", "clone", "-q", str(self.main), str(clone)], check=True)
        (clone / "input.md").write_text("source")
        repo = resolve_repo(clone)
        with self.ext4():
            import_checkpoint(repo, source.directory, {self.feature: clone})
            imported = open_store(repo)
            self.assertEqual("active", imported.read_snapshot()["features"][0]["state"])
            owner = Coordinator(imported).claim("new", self.root / "new.token")
            (clone / "input.md").write_text("changed after import")
            target = self.root / "fresh-exports"
            target.mkdir()
            second = export_checkpoint(imported, owner, target)
        checkpoint = validate_checkpoint(second.directory)
        self.assertEqual(hashlib.sha256(b"changed after import").hexdigest(),
                         checkpoint.features[0]["file_digests"][0]["digest"])

    def test_export_accepts_duplicate_dirty_paths_with_one_digest_per_path(self):
        with self.ext4():
            duplicate = Coordinator(self.store).create_feature(self.owner, FeatureInput(
                "repeat", [], [], [], [], "main", "HEAD", "abc", str(self.main),
                ["input.md", "input.md"], ["*.md"]))
        checkpoint = validate_checkpoint(self.export().directory)
        feature = next(row for row in checkpoint.features if row["id"] == duplicate)
        self.assertEqual([{"path": "input.md", "digest": hashlib.sha256(b"source").hexdigest()}],
                         feature["file_digests"])


if __name__ == "__main__":
    unittest.main()
