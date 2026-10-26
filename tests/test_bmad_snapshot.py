"""BMAD snapshot design (issue #4): a gitignored install materialized into a feature worktree from a sealed,
content-addressed snapshot of the main worktree's install."""
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from coordinator_core import bmad_contract as contract
from coordinator_core import bmad_snapshot as snapshot
from coordinator_core import worker
from coordinator_core.model import DomainError
from coordinator_core.run_receipt import canonical
from tests.bmad_fixture import make_bmad, write
from tests.test_bmad_core import CoreBase, git
from tests.test_bmad_planning import PlanningCase, flow
from tests.test_bmad_review import ReviewCase
from tests.test_boundary import REASON

IGNORE = "_bmad/\n.agents/skills/\n.claude/skills/\n"
COMMIT = ("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm")


class SnapshotCase(CoreBase):
    """The main worktree holds the install, untracked; the feature worktree ignores both roots and has none."""

    def setUp(self):
        super().setUp()
        uv = self.fake_dir / "uv"
        uv.write_text("#!/bin/sh\necho uv-fake 1\n")
        uv.chmod(0o755)
        environ = patch.dict(os.environ, self.env)
        environ.start()
        self.addCleanup(environ.stop)
        make_bmad(self.main)
        for root in (self.main, self.t.path):
            (root / ".gitignore").write_text(IGNORE)
        git(self.t.path, "add", ".gitignore")
        git(self.t.path, *COMMIT, "ignore the BMAD install")
        self.root_ = ".agents/skills"

    def code(self, expected, action, *args, **kwargs):
        with self.assertRaises(DomainError) as caught:
            action(*args, **kwargs)
        self.assertEqual(expected, caught.exception.code, caught.exception.message)
        return caught.exception

    def listing(self, tree):
        return snapshot.verify_store(self.t.repo.common_dir, tree)

    def store_dir(self, tree) -> Path:
        return self.t.repo.common_dir / "coordinator" / "bmad" / tree

    def installed(self, root=None) -> list[str]:
        root = root or self.t.path
        return sorted(path.relative_to(root).as_posix() for top in ("_bmad", self.root_)
                      for path in (root / top).rglob("*") if path.is_file()) if (root / "_bmad").exists() else []


class SnapshotTests(SnapshotCase):
    def test_roots_are_a_closed_set(self):
        for root in (".", ".git", "_bmad", "x/skills", ".agents/skills/", ""):
            with self.subTest(root=root):
                self.code("INVALID_INPUT", snapshot.check_root, root)
        for root in snapshot.ROOTS:
            snapshot.check_root(root)
        self.assertEqual((".agents/skills", ".claude/skills"), snapshot.ROOTS)

    def test_a_snapshot_copies_the_whole_install_and_is_content_addressed(self):
        (self.main / "_bmad/scripts/__pycache__").mkdir()
        (self.main / "_bmad/scripts/__pycache__/x.pyc").write_bytes(b"cache")
        tree = snapshot.take_snapshot(self.t.repo, self.root_)
        self.assertEqual(tree, snapshot.take_snapshot(self.t.repo, self.root_))
        listing = self.listing(tree)
        self.assertEqual(tree, hashlib.sha256(canonical(listing)).hexdigest())
        self.assertEqual([path for path in self.installed(self.main) if "__pycache__" not in path],
                         [item["path"] for item in listing["files"]])
        self.assertEqual((1, self.root_, contract.discover(self.main, self.root_).fingerprint_sha256),
                         (listing["format"], listing["client_root"], listing["fingerprint_sha256"]))
        script = next(item for item in listing["files"] if item["path"] == "_bmad/scripts/memlog.py")
        self.assertEqual(os.access(self.main / "_bmad/scripts/memlog.py", os.X_OK), script["executable"])

    def test_the_store_check_refuses_drift(self):
        tree = snapshot.take_snapshot(self.t.repo, self.root_)
        store = self.store_dir(tree)
        target = store / "_bmad/config.toml"
        original = target.read_bytes()
        for label, damage, repair in (
                ("byte", lambda: target.write_bytes(original + b"x"), lambda: target.write_bytes(original)),
                ("removed", lambda: target.unlink(), lambda: target.write_bytes(original)),
                ("extra", lambda: (store / "_bmad/extra.md").write_text("x"),
                 lambda: (store / "_bmad/extra.md").unlink()),
                ("mode", lambda: target.chmod(0o755), lambda: target.chmod(0o644))):
            with self.subTest(label=label):
                damage()
                self.code("UNKNOWN_EVIDENCE", snapshot.verify_store, self.t.repo.common_dir, tree)
                repair()
        self.listing(tree)
        for bad in ("x" * 64, tree[:-1], tree.upper(), "../" + tree[3:]):
            self.code("UNKNOWN_EVIDENCE", snapshot.verify_store, self.t.repo.common_dir, bad)

    def test_a_listing_naming_a_path_outside_its_roots_is_refused(self):
        tree = snapshot.take_snapshot(self.t.repo, self.root_)
        listing = self.listing(tree)
        for path in ("_bmad/../escape.md", "../escape.md", "docs/x.md", "/etc/x"):
            with self.subTest(path=path):
                crafted = {**listing, "files": [*listing["files"], {"path": path, "sha256": "0" * 64,
                                                                   "executable": False}]}
                data = canonical(crafted)
                forged = hashlib.sha256(data).hexdigest()
                shutil.copytree(self.store_dir(tree), self.store_dir(forged))
                (self.store_dir(forged) / "snapshot.json").write_bytes(data)
                self.code("UNKNOWN_EVIDENCE", snapshot.materialize, self.t.repo, self.root_, forged)
        self.assertFalse((self.root / "escape.md").exists() or (self.t.path / "_bmad").exists())

    def test_a_store_file_swapped_for_a_fifo_during_the_copy_refuses_without_hanging(self):
        tree = snapshot.take_snapshot(self.t.repo, self.root_)
        listing = self.listing(tree)
        target = self.store_dir(tree) / "_bmad/config.toml"
        target.unlink()
        os.mkfifo(target)
        with patch.object(snapshot, "verify_store", return_value=listing):   # the swap lands after the check
            self.code("UNKNOWN_EVIDENCE", snapshot.materialize, self.t.repo, self.root_, tree)
        self.assertFalse((self.t.path / "_bmad").exists())

    def test_doctor_reports_unsafe_entries_in_the_roots(self):
        (self.t.path / "_bmad").mkdir()
        (self.t.path / "_bmad/config.toml").symlink_to(self.main / "_bmad/config.toml")
        report = snapshot.doctor(self.t.repo, self.root_)
        self.assertIn("not a regular single-link file: _bmad/config.toml", " ".join(report["reasons"]))

    def test_a_symlink_special_or_hardlinked_source_file_refuses_the_snapshot(self):
        source = self.main / ".agents/skills/bmad-prd/assets/prd-template.md"
        for label, plant in (("symlink", lambda path: path.symlink_to(source)),
                             ("hardlink", lambda path: os.link(source, path)),
                             ("fifo", lambda path: os.mkfifo(path))):
            with self.subTest(label=label):
                planted = self.main / "_bmad/planted"
                plant(planted)
                self.code("UNSAFE_PATH", snapshot.take_snapshot, self.t.repo, self.root_)
                planted.unlink()
        self.assertFalse((self.t.repo.common_dir / "coordinator" / "bmad").exists() and
                         any((self.t.repo.common_dir / "coordinator" / "bmad").iterdir()))

    def test_materialize_copies_proves_and_is_idempotent(self):
        tree = snapshot.take_snapshot(self.t.repo, self.root_)
        self.assertEqual("absent", snapshot.classify(self.t.path, self.root_))
        status = git(self.t.path, "status", "--porcelain")
        self.assertTrue(snapshot.materialize(self.t.repo, self.root_, tree))
        listing = self.listing(tree)
        self.assertEqual([], snapshot.compare(self.t.path, listing))
        self.assertEqual(listing["fingerprint_sha256"],
                         contract.discover(self.t.path, self.root_).fingerprint_sha256)
        self.assertEqual(status, git(self.t.path, "status", "--porcelain"))   # every materialized file is ignored
        stamp = (self.t.path / "_bmad/config.toml").stat().st_mtime_ns
        self.assertFalse(snapshot.materialize(self.t.repo, self.root_, tree))
        self.assertEqual(stamp, (self.t.path / "_bmad/config.toml").stat().st_mtime_ns)

    def test_only_the_bound_root_is_materialized(self):
        make_bmad(self.main, client=".claude/skills")
        snapshot.materialize(self.t.repo, self.root_, snapshot.take_snapshot(self.t.repo, self.root_))
        self.assertTrue((self.t.path / ".agents/skills/bmad-prd/SKILL.md").is_file())
        self.assertFalse((self.t.path / ".claude").exists())

    def test_a_failed_materialization_removes_only_what_it_created(self):
        tree = snapshot.take_snapshot(self.t.repo, self.root_)
        (self.t.path / ".agents").mkdir()
        (self.t.path / ".agents/keep.md").write_text("mine\n")
        real, count = snapshot._write_file, []

        def failing(*args):
            count.append(1)
            if len(count) == 5:
                raise OSError("disk full")
            return real(*args)
        with patch.object(snapshot, "_write_file", failing):
            with self.assertRaises(OSError):
                snapshot.materialize(self.t.repo, self.root_, tree)
        self.assertFalse((self.t.path / "_bmad").exists())
        self.assertFalse((self.t.path / ".agents/skills").exists())
        self.assertEqual("mine\n", (self.t.path / ".agents/keep.md").read_text())

    def test_unsafe_destinations_are_refused_before_any_write(self):
        tree = snapshot.take_snapshot(self.t.repo, self.root_)
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        (self.t.path / ".agents").symlink_to(elsewhere)
        self.code("UNSAFE_PATH", snapshot.materialize, self.t.repo, self.root_, tree)
        self.assertEqual([], os.listdir(elsewhere))
        self.assertFalse((self.t.path / "_bmad").exists())
        (self.t.path / ".agents").unlink()
        snapshot.materialize(self.t.repo, self.root_, tree)
        (self.t.path / "_bmad/extra.md").write_text("extra\n")
        self.code("BLOCKED_EVIDENCE", snapshot.materialize, self.t.repo, self.root_, tree)
        (self.t.path / "_bmad/extra.md").unlink()
        target = self.t.path / "_bmad/config.toml"
        twin = self.root / "twin"
        os.link(target, twin)
        self.code("BLOCKED_EVIDENCE", snapshot.materialize, self.t.repo, self.root_, tree)

    def test_classification_reads_the_commit_and_the_index(self):
        self.assertEqual("absent", snapshot.classify(self.t.path, self.root_))
        write(self.t.path, "_bmad/config.toml", "x\n")
        git(self.t.path, "add", "-f", "_bmad/config.toml")   # staged only
        self.assertEqual("mixed", snapshot.classify(self.t.path, self.root_))
        git(self.t.path, *COMMIT, "one root")
        self.assertEqual("mixed", snapshot.classify(self.t.path, self.root_))
        write(self.t.path, ".agents/skills/x/SKILL.md", "x\n")
        git(self.t.path, "add", "-f", ".agents/skills/x/SKILL.md")
        git(self.t.path, *COMMIT, "both roots")
        self.assertEqual("committed", snapshot.classify(self.t.path, self.root_))
        tree = snapshot.take_snapshot(self.t.repo, self.root_)
        self.code("BLOCKED_EVIDENCE", snapshot.materialize, self.t.repo, self.root_, tree)

    def test_an_ignore_negation_is_named_and_refused(self):
        write(self.main, "_bmad/keep.md", "keep\n")
        (self.t.path / ".gitignore").write_text("_bmad/*\n!_bmad/keep.md\n.agents/skills/\n")   # a real leak
        tree = snapshot.take_snapshot(self.t.repo, self.root_)
        refused = self.code("UNSAFE_PATH", snapshot.materialize, self.t.repo, self.root_, tree)
        self.assertIn("_bmad/keep.md", refused.message)
        self.assertFalse((self.t.path / "_bmad").exists())
        self.assertEqual(["_bmad/keep.md"], snapshot.uncovered(self.t.path, ["_bmad/keep.md", "_bmad/config.toml"]))

    def test_a_regenerated_main_install_does_not_change_a_materialized_worktree(self):
        first = snapshot.take_snapshot(self.t.repo, self.root_)
        snapshot.materialize(self.t.repo, self.root_, first)
        (self.main / ".agents/skills/bmad-prd/assets/prd-template.md").write_text("# PRD template v2\n")
        second = snapshot.take_snapshot(self.t.repo, self.root_)
        self.assertNotEqual(first, second)
        self.assertEqual([], snapshot.compare(self.t.path, self.listing(first)))
        self.code("BLOCKED_EVIDENCE", snapshot.materialize, self.t.repo, self.root_, second)
        self.assertEqual([], snapshot.compare(self.t.path, self.listing(first)))

    def test_a_reference_outside_the_roots_must_be_committed(self):
        write(self.main, "_bmad/custom/bmad-prd.toml",
              '[workflow]\nprd_template = "{project-root}/docs/template.md"\n')
        write(self.main, "docs/template.md", "# Template\n")   # present in the main worktree only
        tree = snapshot.take_snapshot(self.t.repo, self.root_)
        refused = self.code("UNSUPPORTED_CONTRACT", snapshot.materialize, self.t.repo, self.root_, tree)
        self.assertIn("referenced file is missing", refused.message)
        self.assertFalse((self.t.path / "_bmad").exists())

    def test_compare_never_runs_installed_code(self):
        tree = snapshot.take_snapshot(self.t.repo, self.root_)
        snapshot.materialize(self.t.repo, self.root_, tree)
        marker = self.root / "ran"
        script = self.t.path / "_bmad/scripts/resolve_config.py"
        script.write_text(f"open({str(marker)!r}, 'w').write('ran')\n")
        self.assertEqual(["changed: _bmad/scripts/resolve_config.py"], snapshot.compare(self.t.path, self.listing(tree)))
        script.chmod(0o755)
        self.assertIn("mode: _bmad/scripts/resolve_config.py", snapshot.compare(self.t.path, self.listing(tree)))
        self.assertFalse(marker.exists())


class CliTests(SnapshotCase):
    """S3, S5, S10 through the installed CLI: `bmad materialize`, its event, `doctor --bmad`, the discover hint."""

    def cli(self, *args, ok=True):
        done = subprocess.run([sys.executable, str(self.t.path / ".bmad-coordinator" / "run.py"), "--repo",
                               str(self.t.path), *args], capture_output=True, text=True, env=os.environ)
        if ok:
            self.assertEqual(0, done.returncode, done.stderr)
            return json.loads(done.stdout)
        self.assertNotEqual(0, done.returncode)
        return json.loads(done.stdout or done.stderr)

    def materialize(self, *extra, ok=True):
        return self.cli("bmad", "materialize", "--token-file", str(self.token), *extra, ok=ok)

    def events(self):
        return [item for item in self.store.read_snapshot()["events"] if item["kind"] == snapshot.EVENT]

    def test_materialize_answers_records_and_discover_agrees(self):
        answer = self.materialize()
        tree = answer["tree"]
        self.assertEqual({"tree": tree, "fingerprint_sha256": self.listing(tree)["fingerprint_sha256"],
                          "client_root": self.root_, "written": True}, answer)
        [event] = self.events()
        self.assertEqual((self.t.feature_id, str(self.t.path), f"bmad/{tree}/snapshot.json", tree),
                         (event["subject_id"], event["source_id"], event["evidence_ref"], event["payload_digest"]))
        self.assertEqual({"tree": tree, "feature_id": self.t.feature_id},
                         snapshot.materialized(self.store.read_snapshot()["events"], self.t.path))
        found = self.cli("bmad", "discover")
        self.assertEqual((True, answer["fingerprint_sha256"]), (found["supported"], found["fingerprint_sha256"]))

    def test_materialize_is_idempotent_and_records_the_event_again(self):
        first = self.materialize()
        again = self.materialize()
        self.assertEqual((first["tree"], False), (again["tree"], again["written"]))
        self.assertEqual(2, len(self.events()))

    def test_materialize_refuses_a_closed_feature(self):
        from coordinator_core.model import DecisionInput, OwnerRef
        from coordinator_core.service import Coordinator
        owner = OwnerRef(**json.loads(self.token.read_text()))
        coordinator = Coordinator(self.store)
        decision = coordinator.record_decision(owner, DecisionInput(
            self.t.feature_id, 1, "operator-reported", "chat:drop", "dropped", {}))
        coordinator.close_feature(owner, self.t.feature_id, 1, decision, "abandoned")
        self.assertEqual("FEATURE_CLOSED", self.materialize(ok=False)["error"])
        self.assertFalse((self.t.path / "_bmad").exists())

    def test_materialize_refuses_a_committed_install(self):
        snapshot.materialize(self.t.repo, self.root_, snapshot.take_snapshot(self.t.repo, self.root_))
        git(self.t.path, "add", "-f", "_bmad", ".agents/skills")
        git(self.t.path, *COMMIT, "commit the install")
        refused = self.materialize(ok=False)
        self.assertIn("committed", json.dumps(refused))
        self.assertEqual([], self.events())

    def test_materialize_with_a_tree_reuses_an_earlier_snapshot(self):
        first = snapshot.take_snapshot(self.t.repo, self.root_)
        (self.main / ".agents/skills/bmad-prd/assets/prd-template.md").write_text("# PRD template v2\n")
        self.assertEqual(first, self.materialize("--tree", first)["tree"])
        self.assertEqual([], snapshot.compare(self.t.path, self.listing(first)))
        self.materialize("--tree", "f" * 63, ok=False)

    def test_doctor_bmad_reports_missing_ignore_rules_as_not_ready(self):
        (self.t.path / ".gitignore").write_text("")
        result = self.cli("doctor", "--base", self.base, "--bmad")
        self.assertEqual((False, "not-ignored"), (result["ready"], result["bmad"]["state"]))
        reasons = " ".join(result["bmad"]["reasons"])
        for line in ("_bmad/", ".agents/skills/"):
            self.assertIn(line, reasons)
        (self.t.path / ".gitignore").write_text(IGNORE)
        result = self.cli("doctor", "--base", self.base, "--bmad")
        self.assertEqual(("ignored", []), (result["bmad"]["state"], result["bmad"]["reasons"]))

    def test_doctor_without_bmad_is_unchanged(self):
        (self.t.path / ".gitignore").write_text("")
        result = self.cli("doctor", "--base", self.base)
        self.assertNotIn("bmad", result)
        self.assertFalse(any("BMAD" in reason or "_bmad" in reason for reason in result["reasons"]))

    def test_discover_on_a_drifted_materialized_install_never_runs_it(self):
        self.materialize()
        marker = self.root / "ran"
        (self.t.path / "_bmad/scripts/resolve_config.py").write_text(f"open({str(marker)!r}, 'w').write('x')\n")
        refused = self.cli("bmad", "discover", ok=False)
        self.assertEqual("STALE_INPUT", refused["error"])
        self.assertFalse(marker.exists())

    def test_discover_on_a_worktree_without_an_install_names_the_command(self):
        found = self.cli("bmad", "discover")
        self.assertFalse(found["supported"])
        self.assertIn("no BMAD install in this worktree: run bmad materialize", found["reasons"])


class PlanningOnSnapshotTests(ReviewCase):
    """S6-S8: the planning path on a materialized install, guarded before any installed code runs."""

    def setUp(self):
        super().setUp()   # PlanningCase built the install in the feature worktree; it belongs to the main one here
        for top in ("_bmad", ".agents"):
            shutil.rmtree(self.t.path / top)
        make_bmad(self.main)
        (self.t.path / ".gitignore").write_text(IGNORE)
        git(self.t.path, "add", ".gitignore")
        git(self.t.path, *COMMIT, "ignore the BMAD install")
        self.tree_id = snapshot.materialize_command(self.t.repo, self.store, self.owner, ".agents/skills",
                                                    None)["tree"]
        self.compat = self.judge()
        self.source = {"kind": "snapshot", "tree": self.tree_id, "client_root": ".agents/skills"}

    def test_a_planning_author_review_and_acceptance_run_on_a_gitignored_install(self):
        bundle, acceptance = self.accepted_prd()
        self.assertTrue(acceptance)
        rows = self.store.read_snapshot()["assignments"]
        author = next(row["id"] for row in rows if row["idempotency_key"] == "p1")
        reviewer = next(row["id"] for row in rows if row["idempotency_key"] == "r1")
        self.assertEqual(self.source, self.binding_of(author)["bmad_source"])
        self.assertEqual(self.source, self.binding_of_review(reviewer)["bmad_source"])
        self.assertEqual("observed", self.record(reviewer)["provenance_class"])

    def test_a_changed_non_contract_skill_is_stale_before_the_launch(self):
        assignment = self.prep("p1")["assignment_id"]
        write(self.t.path, ".agents/skills/other/SKILL.md", "---\nname: other\n---\nplanted\n")
        refused = self.code("STALE_INPUT", self.execute, assignment, flow(), self.first_files())
        self.assertIn("added: .agents/skills/other/SKILL.md", refused.message)
        self.assertEqual("prepared", self.state(assignment))

    def test_a_changed_resolver_is_refused_before_it_runs(self):
        marker = self.root / "ran"
        (self.t.path / "_bmad/scripts/resolve_config.py").write_text(f"open({str(marker)!r}, 'w').write('x')\n")
        refused = self.code("STALE_INPUT", self.prep, "p1")
        self.assertIn("changed: _bmad/scripts/resolve_config.py", refused.message)
        self.assertFalse(marker.exists())

    def test_a_worker_change_to_the_roots_ends_unknown(self):
        assignment = self.prep("p1")["assignment_id"]
        report = self.execute(assignment, flow(), self.first_files(
            extra=[(".agents/skills/other/SKILL.md", "planted\n")]))
        self.assertEqual("unknown", report.state)
        self.assertIn("added: .agents/skills/other/SKILL.md", " ".join(report.evidence_needs))

    def test_replay_refuses_a_changed_snapshot_store(self):
        assignment = self.authored()
        self.assertEqual("completed-claim", worker.inspect_run(self.t.repo, self.store, assignment).state)
        stored = self.t.repo.common_dir / "coordinator" / "bmad" / self.tree_id / "_bmad/config.toml"
        stored.write_bytes(stored.read_bytes() + b"# tampered\n")
        self.assertEqual("unknown", worker.inspect_run(self.t.repo, self.store, assignment).state)

    @unittest.skipIf(REASON and os.environ.get("COORDINATOR_REQUIRE_BWRAP") != "1", f"bwrap unusable: {REASON}")
    def test_materialized_roots_are_read_only_under_the_boundary(self):
        if REASON:
            self.fail(f"COORDINATOR_REQUIRE_BWRAP=1 but bwrap is unusable: {REASON}")
        codex_home = self.root / "codex-home"   # never the operator's own ~/.codex
        codex_home.mkdir()
        assignment = self.prep("p1")["assignment_id"]
        final = {"result": "completed", "workflow_json": json.dumps(flow()), "unresolved_question": None,
                 "customization": self.custom()}
        files = [*self.first_files(), ("_bmad/config.toml", "planted\n")]
        env = {"FAKE_WRITE": json.dumps([list(item) for item in files]), "FAKE_EXTRA": json.dumps(final),
               "CODEX_HOME": str(codex_home), "COORDINATOR_BOUNDARY_PASS": "FAKE_",
               "FAKE_COUNT": f"/tmp/fake-count-{os.getpid()}"}
        with patch.dict(os.environ, env):
            report = worker.run_assignment(self.t.repo, self.store, self.owner, assignment, "fake-model", 30,
                                           boundary_kind="bwrap")
        self.assertEqual("unknown", report.state)
        self.assertEqual([], snapshot.compare(self.t.path, self.store_listing()))   # the write never landed
        modes = {item["path"]: item["mode"] for item in
                 json.loads((Path(report.run_dir) / "launch.json").read_text())["boundary"]["mounts"]}
        self.assertEqual(("ro", "ro"), (modes[str(self.t.path / "_bmad")], modes[str(self.t.path / ".agents/skills")]))

    def store_listing(self):
        return snapshot.verify_store(self.t.repo.common_dir, self.tree_id)


class CommittedInstallTests(PlanningCase):
    def test_a_committed_install_binding_has_no_bmad_source(self):
        assignment = self.prep("p1")["assignment_id"]
        self.assertNotIn("bmad_source", self.binding_of(assignment))


if __name__ == "__main__":
    unittest.main()
