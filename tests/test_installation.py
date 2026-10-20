import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from coordinator_core.repo import resolve_repo


ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "commit", "--allow-empty", "-qm", "initial")
        self.agents = self.repo / "AGENTS.md"
        self.agents.write_bytes(b"Local instructions\r\nKeep this line.\r\n")

    def installed(self):
        from install import apply, preview
        return apply(self.repo, preview(self.repo).sha256)

    def newer_source(self):
        source = Path(self.tmp.name) / "newer"
        shutil.copytree(ROOT / "package", source / "package")
        shutil.copytree(ROOT / "coordinator_core", source / "coordinator_core",
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copyfile(ROOT / "LICENSE", source / "LICENSE")   # in the allowlist since 2026-10-20
        with (source / "coordinator_core" / "repo.py").open("ab") as stream:
            stream.write(b"# Upgraded module.\n")
        (source / "coordinator_core" / "extra_module.py").write_bytes(b'"""Module added by an upgrade."""\n')
        (source / "coordinator_core" / "__init__.py").unlink()
        manifest_path = source / "package" / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["files"] = [entry for entry in manifest["files"]
                             if entry["destination"] != "coordinator_core/__init__.py"]
        manifest["files"].append({"source": "coordinator_core/extra_module.py",
                                  "destination": "coordinator_core/extra_module.py"})
        manifest_path.write_text(json.dumps(manifest))
        block = source / "package" / "AGENTS.block.md"
        block.write_bytes(block.read_bytes().replace(
            b"<!-- bmad-coordinator:end v1 -->", b"Upgraded instruction.\n<!-- bmad-coordinator:end v1 -->"))
        return source

    def snapshot(self):
        return {path: path.read_bytes() for path in self.repo.rglob("*")
                if path.is_file() and path.relative_to(self.repo).parts[0] != ".git"}

    def test_preview_is_read_only_and_existing_agents_preserved(self):
        from install import preview
        before = self.agents.read_bytes()
        result = preview(self.repo)
        self.assertEqual(before, self.agents.read_bytes())
        self.assertFalse((self.repo / ".bmad-coordinator").exists())
        self.assertIn("bmad-coordinator:begin v1", result.agents_diff)
        self.assertTrue(result.sha256)
        self.assertGreater(len(result.files), 3)

    def test_apply_requires_fresh_preview_and_is_noop_when_exact(self):
        from install import apply, preview
        before = self.agents.read_bytes()
        token = preview(self.repo).sha256
        self.agents.write_bytes(before + b"new local line\n")
        with self.assertRaises(ValueError):
            apply(self.repo, token)
        self.assertFalse((self.repo / ".bmad-coordinator").exists())
        result = self.installed()
        self.assertTrue(result.applied)
        self.assertTrue(self.agents.read_bytes().startswith(before + b"new local line\n"))
        self.assertFalse(self.installed().applied)
        self.assertEqual(1, self.agents.read_bytes().count(b"bmad-coordinator:begin v1"))

    def test_source_change_after_preview_refuses_without_activation(self):
        import install
        source = Path(self.tmp.name) / "source"
        shutil.copytree(ROOT / "package", source / "package")
        shutil.copytree(ROOT / "coordinator_core", source / "coordinator_core",
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copyfile(ROOT / "LICENSE", source / "LICENSE")   # in the allowlist since 2026-10-20
        with patch.object(install, "SOURCE", source):
            token = install.preview(self.repo).sha256
            (source / "package" / "run.py").write_bytes(b"changed")
            with self.assertRaises(ValueError):
                install.apply(self.repo, token)
        self.assertFalse((self.repo / ".bmad-coordinator").exists())
        self.assertEqual(b"Local instructions\r\nKeep this line.\r\n", self.agents.read_bytes())

    def test_absent_agents_becoming_empty_stales_preview(self):
        from install import apply, preview
        self.agents.unlink()
        token = preview(self.repo).sha256
        self.agents.touch()
        with self.assertRaises(ValueError):
            apply(self.repo, token)
        self.assertEqual(b"", self.agents.read_bytes())
        self.assertFalse((self.repo / ".bmad-coordinator").exists())

    def test_staged_file_changed_during_apply_blocks_activation(self):
        import install
        plan = install.preview(self.repo)
        first = self.repo / plan.files[0]["destination"]
        second = self.repo / plan.files[1]["destination"]
        real_open = os.open

        def interrupted_open(path, flags, *args, **kwargs):
            if Path(path) == second and flags & os.O_EXCL:
                first.write_bytes(b"changed mid-apply")
            return real_open(path, flags, *args, **kwargs)

        with patch.object(install.os, "open", side_effect=interrupted_open):
            with self.assertRaises(ValueError):
                install.apply(self.repo, plan.sha256)
        self.assertEqual(b"Local instructions\r\nKeep this line.\r\n", self.agents.read_bytes())
        self.assertEqual(b"changed mid-apply", first.read_bytes())

    def test_ignore_rule_added_during_staging_blocks_activation(self):
        import install
        plan = install.preview(self.repo)
        second = self.repo / plan.files[1]["destination"]
        real_open = os.open

        def interrupted_open(path, flags, *args, **kwargs):
            if Path(path) == second and flags & os.O_EXCL:
                (self.repo / ".gitignore").write_text(".bmad-coordinator/\n")
            return real_open(path, flags, *args, **kwargs)

        with patch.object(install.os, "open", side_effect=interrupted_open):
            with self.assertRaises(ValueError):
                install.apply(self.repo, plan.sha256)
        self.assertEqual(b"Local instructions\r\nKeep this line.\r\n", self.agents.read_bytes())
        self.assertTrue((self.repo / ".bmad-coordinator" / "run.py").exists())

    def test_preview_reads_managed_block_source_from_allowlist(self):
        import install
        source = Path(self.tmp.name) / "source"
        shutil.copytree(ROOT / "package", source / "package")
        shutil.copytree(ROOT / "coordinator_core", source / "coordinator_core",
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copyfile(ROOT / "LICENSE", source / "LICENSE")   # in the allowlist since 2026-10-20
        block = (source / "package" / "AGENTS.block.md").read_bytes()
        (source / "package" / "other.block.md").write_bytes(block.replace(
            b"<!-- bmad-coordinator:end v1 -->", b"Distinct instruction.\n<!-- bmad-coordinator:end v1 -->"))
        manifest_path = source / "package" / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["block_source"] = "package/other.block.md"
        manifest_path.write_text(json.dumps(manifest))
        with patch.object(install, "SOURCE", source):
            self.assertIn("Distinct instruction.", install.preview(self.repo).agents_diff)

    def test_partial_pre_activation_retry_only_fills_exact_missing_files(self):
        from install import apply, preview
        token = preview(self.repo).sha256
        first = preview(self.repo).files[0]
        path = self.repo / first["destination"]
        path.parent.mkdir()
        source = ROOT / first["source"]
        path.write_bytes(source.read_bytes())
        with self.assertRaises(ValueError):
            apply(self.repo, token)
        self.assertEqual(b"Local instructions\r\nKeep this line.\r\n", self.agents.read_bytes())
        self.installed()
        self.assertEqual(source.read_bytes(), path.read_bytes())
        self.assertEqual(1, self.agents.read_bytes().count(b"bmad-coordinator:begin v1"))

    def test_changed_markers_symlinks_ignored_or_colliding_paths_refuse(self):
        from install import apply, preview
        original = self.agents.read_bytes()
        self.agents.write_bytes(original + b"<!-- bmad-coordinator:begin v1 -->\nwrong\n")
        with self.assertRaises(ValueError):
            preview(self.repo)
        self.agents.write_bytes(original)
        target = self.repo / ".bmad-coordinator"
        target.symlink_to(self.repo, target_is_directory=True)
        with self.assertRaises(ValueError):
            preview(self.repo)
        target.unlink()
        target.mkdir()
        first = preview(self.repo).files[0]
        collision = self.repo / first["destination"]
        collision.parent.mkdir(exist_ok=True)
        collision.write_bytes(b"sentinel")
        with self.assertRaises(ValueError):
            preview(self.repo)
        self.assertEqual(b"sentinel", collision.read_bytes())
        collision.unlink()
        (self.repo / ".gitignore").write_text(".bmad-coordinator/\n")
        with self.assertRaises(ValueError):
            apply(self.repo, preview(self.repo).sha256)
        self.assertEqual(original, self.agents.read_bytes())
        self.assertFalse((self.repo / ".git" / "coordinator").exists())

    def test_manifest_contains_only_runtime_allowlist(self):
        self.installed()
        manifest = json.loads((self.repo / ".bmad-coordinator" / "manifest.json").read_text())
        destinations = set(manifest["files"])
        self.assertEqual(1, manifest["format"])
        self.assertIn("coordinator_core/installation.py", destinations)
        self.assertIn("coordinator_core/__main__.py", destinations)
        self.assertIn("run.py", destinations)
        self.assertIn("coordinator_core/worker.py", destinations)
        self.assertIn("coordinator_core/run_receipt.py", destinations)
        self.assertFalse(any("test" in x or "experiment" in x for x in destinations))
        self.assertEqual(destinations | {"manifest.json"},
                         {str(path.relative_to(self.repo / ".bmad-coordinator"))
                          for path in (self.repo / ".bmad-coordinator").rglob("*") if path.is_file()})

    def test_unlisted_package_file_refuses_install_and_doctor(self):
        from install import preview
        from coordinator_core.installation import doctor
        self.installed()
        (self.repo / ".bmad-coordinator" / "json.py").write_bytes(b"sentinel")
        with self.assertRaises(ValueError):
            preview(self.repo)
        self.assertFalse(doctor(resolve_repo(self.repo), "HEAD").ready)

    def test_doctor_committed_linked_worktree_and_dirty_base_refusal(self):
        from coordinator_core.installation import doctor
        self.installed()
        dirty = doctor(resolve_repo(self.repo), "HEAD")
        self.assertFalse(dirty.ready)
        self.assertFalse((self.repo / ".git" / "coordinator").exists())
        git(self.repo, "add", "AGENTS.md", ".bmad-coordinator")
        git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "installed")
        base = git(self.repo, "rev-parse", "HEAD")
        linked = Path(self.tmp.name) / "linked"
        git(self.repo, "worktree", "add", "-qb", "feature", str(linked))
        report = doctor(resolve_repo(linked), base)
        self.assertTrue(report.ready, report.reasons)
        self.assertEqual(base, report.base_commit)
        self.assertFalse((self.repo / ".git" / "coordinator").exists())
        (linked / ".bmad-coordinator" / "run.py").write_bytes(b"changed")
        self.assertFalse(doctor(resolve_repo(linked), base).ready)

    def test_doctor_rejects_unrelated_installed_commit_as_base(self):
        from coordinator_core.installation import doctor
        self.installed()
        git(self.repo, "add", "AGENTS.md", ".bmad-coordinator")
        git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "installed")
        installed = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "checkout", "-q", "--orphan", "unrelated")
        git(self.repo, "rm", "-qr", "--cached", ".")
        git(self.repo, "add", "AGENTS.md", ".bmad-coordinator")
        git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "copied")
        report = doctor(resolve_repo(self.repo), installed)
        self.assertFalse(report.ready)
        self.assertTrue(any("ancestor" in reason.lower() for reason in report.reasons), report.reasons)

    def test_doctor_rejects_extra_file_committed_in_base(self):
        from coordinator_core.installation import doctor
        self.installed()
        extra = self.repo / ".bmad-coordinator" / "extra.py"
        extra.write_bytes(b"not allowed")
        git(self.repo, "add", "AGENTS.md", ".bmad-coordinator")
        git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "installed plus extra")
        base = git(self.repo, "rev-parse", "HEAD")
        extra.unlink()
        report = doctor(resolve_repo(self.repo), base)
        self.assertFalse(report.ready)
        self.assertTrue(any("base package" in reason.lower() for reason in report.reasons), report.reasons)

    def test_doctor_rejects_package_symlink_and_cli_creates_no_state(self):
        from coordinator_core.installation import doctor
        self.installed()
        git(self.repo, "add", "AGENTS.md", ".bmad-coordinator")
        git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "installed")
        base = git(self.repo, "rev-parse", "HEAD")
        result = subprocess.run([sys.executable, str(self.repo / ".bmad-coordinator" / "run.py"),
                                 "--repo", str(self.repo), "doctor", "--base", base],
                                capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(json.loads(result.stdout)["ready"])
        self.assertFalse((self.repo / ".git" / "coordinator").exists())
        package = self.repo / ".bmad-coordinator"
        package.rename(self.repo / "real-package")
        package.symlink_to(self.repo / "real-package", target_is_directory=True)
        self.assertFalse(doctor(resolve_repo(self.repo), base).ready)

    def test_doctor_rejects_manifest_path_escape(self):
        from coordinator_core.installation import doctor
        self.installed()
        manifest_path = self.repo / ".bmad-coordinator" / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["files"]["../AGENTS.md"] = hashlib.sha256(self.agents.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(manifest))
        report = doctor(resolve_repo(self.repo), "HEAD")
        self.assertFalse(report.ready)
        self.assertTrue(any("invalid" in reason.lower() for reason in report.reasons))

    def test_doctor_rejects_manifest_missing_runtime_entry(self):
        from coordinator_core.installation import doctor
        self.installed()
        manifest_path = self.repo / ".bmad-coordinator" / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        del manifest["files"]["run.py"]
        manifest_path.write_text(json.dumps(manifest))
        report = doctor(resolve_repo(self.repo), "HEAD")
        self.assertFalse(report.ready)
        self.assertTrue(any("invalid" in reason.lower() for reason in report.reasons))

    def test_wrapper_runs_status_without_bytecode_cache(self):
        self.installed()
        run = self.repo / ".bmad-coordinator" / "run.py"
        result = subprocess.run([sys.executable, str(run), "--repo", str(self.repo), "status"],
                                capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("uninitialized", json.loads(result.stdout)["state"])
        self.assertFalse(list((self.repo / ".bmad-coordinator").rglob("__pycache__")))
        self.assertFalse(list((self.repo / ".bmad-coordinator").rglob("*.pyc")))
        self.assertFalse((self.repo / ".git" / "coordinator").exists())

    def test_upgrade_preview_classifies_files_and_shows_the_block_diff(self):
        import install
        self.installed()
        before = self.snapshot()
        newer = self.newer_source()
        with patch.object(install, "SOURCE", newer):
            plan = install.preview(self.repo, upgrade=True)
            with self.assertRaisesRegex(ValueError, "Changed or misplaced managed AGENTS block"):
                install.preview(self.repo)
        self.assertEqual(before, self.snapshot())
        statuses = {}
        for item in plan.files:
            statuses.setdefault(item["status"], set()).add(item["destination"])
        names = {f".bmad-coordinator/{entry['destination']}"
                 for entry in json.loads((ROOT / "package" / "manifest.json").read_text())["files"]}
        changed = ".bmad-coordinator/coordinator_core/repo.py"
        removed = ".bmad-coordinator/coordinator_core/__init__.py"
        self.assertEqual({"upgrade": {changed, ".bmad-coordinator/manifest.json"},
                          "absent": {".bmad-coordinator/coordinator_core/extra_module.py"},
                          "remove": {removed},
                          "exact": names - {changed, removed}}, statuses)
        self.assertIn({"source": "installed", "destination": removed, "status": "remove",
                       "sha256": hashlib.sha256(before[self.repo / removed]).hexdigest()}, plan.files)
        self.assertIn("+Upgraded instruction.", plan.agents_diff)

    def test_upgrade_apply_replaces_adds_removes_and_rewrites_the_block_then_doctor_passes(self):
        import install
        self.installed()
        newer = self.newer_source()
        package = self.repo / ".bmad-coordinator"
        with patch.object(install, "SOURCE", newer):
            result = install.apply(self.repo, install.preview(self.repo, upgrade=True).sha256, upgrade=True)
            self.assertTrue(result.applied)
            again = install.apply(self.repo, install.preview(self.repo, upgrade=True).sha256, upgrade=True)
            self.assertFalse(again.applied)
        self.assertEqual((newer / "coordinator_core" / "repo.py").read_bytes(),
                         (package / "coordinator_core" / "repo.py").read_bytes())
        self.assertTrue((package / "coordinator_core" / "extra_module.py").is_file())
        self.assertFalse((package / "coordinator_core" / "__init__.py").exists())
        manifest = json.loads((package / "manifest.json").read_text())
        source_manifest = json.loads((newer / "package" / "manifest.json").read_text())
        self.assertEqual({entry["destination"]: hashlib.sha256((newer / entry["source"]).read_bytes()).hexdigest()
                          for entry in source_manifest["files"]}, manifest["files"])
        block = (newer / "package" / "AGENTS.block.md").read_bytes()
        self.assertEqual(hashlib.sha256(block).hexdigest(), manifest["block_sha256"])
        self.assertEqual(result.package_revision, manifest["package_revision"])
        self.assertEqual(b"Local instructions\r\nKeep this line.\r\n" + block, self.agents.read_bytes())
        git(self.repo, "add", "AGENTS.md", ".bmad-coordinator")
        git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "upgraded")
        doctor = subprocess.run([sys.executable, "-E", str(package / "run.py"),
                                 "--repo", str(self.repo), "doctor", "--base", "HEAD"],
                                capture_output=True, text=True)
        self.assertEqual(0, doctor.returncode, doctor.stderr)
        self.assertTrue(json.loads(doctor.stdout)["ready"], doctor.stdout)

    def test_upgrade_refuses_a_locally_modified_file_a_missing_file_and_no_installed_package(self):
        from install import apply, preview

        def refused(message):
            before = self.snapshot()
            with self.assertRaisesRegex(ValueError, message):
                preview(self.repo, upgrade=True)
            with self.assertRaisesRegex(ValueError, message):
                apply(self.repo, "0" * 64, upgrade=True)
            self.assertEqual(before, self.snapshot())

        refused("No installed package to upgrade")
        self.assertFalse((self.repo / ".bmad-coordinator").exists())
        self.installed()
        module = self.repo / ".bmad-coordinator" / "coordinator_core" / "repo.py"
        original = module.read_bytes()
        module.write_bytes(original + b"# local change\n")
        refused("Package collision: .bmad-coordinator/coordinator_core/repo.py")
        module.unlink()
        refused("Activated package has missing files; reviewed repair required")
        module.write_bytes(original)
        manifest_path = self.repo / ".bmad-coordinator" / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["files"]["coordinator_core/./store.py"] = manifest["files"]["coordinator_core/store.py"]
        manifest_path.write_text(json.dumps(manifest))
        refused("Installed manifest is malformed")
        manifest_path.write_text("{}")
        refused("Installed manifest is malformed")

    def test_upgrade_token_is_mode_specific(self):
        from install import apply, preview
        self.installed()
        with self.assertRaisesRegex(ValueError, "Preview is stale"):
            apply(self.repo, preview(self.repo, upgrade=True).sha256)
        with self.assertRaisesRegex(ValueError, "Preview is stale"):
            apply(self.repo, preview(self.repo).sha256, upgrade=True)

    def test_upgrade_with_a_changed_block_outside_the_markers_is_refused(self):
        from install import preview
        self.installed()
        installed = self.agents.read_bytes()
        self.agents.write_bytes(installed.replace(b"Portable BMAD coordinator opt-in", b"Edited by hand"))
        with self.assertRaisesRegex(ValueError, "Changed or misplaced managed AGENTS block"):
            preview(self.repo, upgrade=True)
        self.agents.write_bytes(installed + b"local tail\n")
        with self.assertRaisesRegex(ValueError, "Changed or misplaced managed AGENTS block"):
            preview(self.repo, upgrade=True)

    def test_cli_upgrade_flag(self):
        self.installed()
        result = subprocess.run([sys.executable, "-B", str(ROOT / "install.py"),
                                 "--repo", str(self.repo), "--upgrade", "--preview"],
                                capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        files = json.loads(result.stdout)["files"]
        self.assertGreater(len(files), 3)
        self.assertEqual({"exact"}, {item["status"] for item in files})
