import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from coordinator_core import bmad_contract as contract
from coordinator_core.model import DomainError
from tests.bmad_fixture import CLIENT, make_bmad, write

ROOT = Path(__file__).parents[1]
SUMMARY = ROOT.parent / ".superpowers/sdd/2026-10-04-bmad-integration/task2-actual-discovery.json"


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=T", "-c", "user.email=t@example.invalid", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


class ContractBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        git(self.root, "init", "-q")
        make_bmad(self.root)
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "bmad")
        which = patch("coordinator_core.bmad_contract.shutil.which", return_value="/usr/bin/uv")
        which.start()
        self.addCleanup(which.stop)

    def report(self):
        return contract.discover(self.root)

    def reasons(self):
        report = self.report()
        self.assertFalse(report.supported)
        self.assertIsNone(report.fingerprint_sha256)
        return "\n".join(report.reasons)

    def skill(self, name, rel):
        return self.root / CLIENT / name / rel


class DiscoveryTests(ContractBase):
    def test_catalog_shims_detected_and_manifest_paths_never_opened(self):
        report = self.report()
        self.assertTrue(report.supported, report.reasons)
        self.assertIn("_bmad/custom/bmad-editorial-review.toml", report.contract["absent"])
        self.assertFalse(any("never/opened" in item["path"] for item in report.contract_files))
        (self.root / "_bmad/_config/skill-manifest.csv").write_text(
            '"canonicalId","name","description","module","path"\n"bmad-prd","x","x","core","/etc/passwd"\n')
        self.assertIn("bmad-architecture", self.reasons())

    def test_capabilities_recognized_from_installed_files_without_hashes(self):
        report = self.report()
        self.assertTrue(all(report.capabilities.values()), report.capabilities)
        self.assertTrue(report.capabilities["memlog_append"])
        self.assertIn("semantic compatibility is not judged", " ".join(report.limitations))
        with self.skill("bmad-prd", "references/headless.md").open("a") as stream:
            stream.write("\nHarmless edit that keeps every marker.\n")
        edited = self.report()
        self.assertTrue(edited.supported)
        self.assertNotEqual(report.fingerprint_sha256, edited.fingerprint_sha256)
        write(self.root, f"{CLIENT}/bmad-architecture/references/headless.md", "no markers\n")
        self.assertIn("bmad-architecture", self.reasons())

    def test_unsupported_reasons_are_listed(self):
        cases = {}

        def case(name, mutate, expected):
            cases[name] = (mutate, expected)

        case("missing skill", lambda: __import__("shutil").rmtree(self.root / CLIENT / "bmad-review"), "bmad-review")
        case("name mismatch", lambda: write(self.root, f"{CLIENT}/bmad-prd/SKILL.md", "---\nname: other\n---\n"),
             "name")
        case("symlink", lambda: self.skill("bmad-prd", "assets/link.md").symlink_to("prd-template.md"), "symlink")
        case("custom subdir", lambda: (self.root / "_bmad/custom/sub").mkdir(), "_bmad/custom")
        case("resolver failure", lambda: write(self.root, "_bmad/scripts/resolve_customization.py",
                                               "import sys\nsys.exit(1)\n"), "resolver")
        case("central failure", lambda: write(self.root, "_bmad/scripts/resolve_config.py", "print('not json')\n"),
             "central")
        case("side effect", lambda: write(self.root, "_bmad/custom/bmad-prd.toml",
                                          '[workflow]\nexternal_handoffs = ["push to somewhere"]\n'), "external_handoffs")
        case("on_complete", lambda: write(self.root, "_bmad/custom/bmad-prd.toml",
                                          '[workflow]\non_complete = "do it"\n'), "on_complete")
        case("foreign skill", lambda: write(self.root, "_bmad/custom/bmad-prd.toml",
                                            '[workflow]\npersistent_facts = ["skill:acme:terms"]\n'), "foreign skill")
        case("outside file", lambda: write(self.root, "_bmad/custom/bmad-prd.toml",
                                           '[workflow]\npersistent_facts = ["file:/etc/hostname"]\n'), "outside")
        case("outside template", lambda: write(self.root, "_bmad/custom/bmad-prd.toml",
                                               '[workflow]\nprd_template = "../../../../outside.md"\n'), "outside")
        for name, (mutate, expected) in cases.items():
            with self.subTest(name):
                git(self.root, "reset", "-q", "--hard")
                git(self.root, "clean", "-qfd")
                mutate()
                self.assertIn(expected, self.reasons())
        git(self.root, "reset", "-q", "--hard")
        git(self.root, "clean", "-qfd")
        with patch("coordinator_core.bmad_contract.shutil.which", return_value=None):
            self.assertIn("uv", self.reasons())
        write(self.root, "_bmad/custom/bmad-prd.toml", '[workflow]\npersistent_facts = ["file:{project-root}/d/*.md"]\n')
        for number in range(201):
            write(self.root, f"d/{number}.md", "x")
        self.assertIn("200", self.reasons())

    def test_fingerprint_is_deterministic_sorted_timestamp_free(self):
        first, second = self.report(), self.report()
        self.assertEqual(first.fingerprint_sha256, second.fingerprint_sha256)
        paths = [item["path"] for item in first.contract_files]
        self.assertEqual(sorted(paths), paths)
        self.assertEqual(set(), {key for key in first.contract if "time" in key or "date" in key})
        self.assertEqual(contract.fingerprint(first.contract), first.fingerprint_sha256)

    def test_documented_scripts_without_a_bound_form_are_facts_outside_the_fingerprint(self):
        before = self.report()
        self.assertEqual([], before.facts["unbound_scripts"])  # the fixture documents memlog.py only
        self.assertIn("no subagent tool", " ".join(before.limitations))
        write(self.root, f"{CLIENT}/bmad-architecture/references/reviewer-gate.md",
              "Lint first: `uv run {skill-root}/scripts/lint_spine.py --workspace {doc_workspace}`.\n")
        write(self.root, f"{CLIENT}/bmad-review/references/editorial-common.md",
              "Counts: `uv run {skill-root}/scripts/word_metrics.py <path>`, then "
              "`uv run --no-project {project-root}/_bmad/scripts/other.py`, never `uv run elsewhere.py`.\n")
        after = self.report()
        self.assertTrue(after.supported, after.reasons)
        self.assertEqual([f"{CLIENT}/bmad-review/scripts/word_metrics.py", "_bmad/scripts/other.py"],
                         after.facts["unbound_scripts"])  # lint_spine.py has a bound form (issue #1)

    def test_facts_head_tools_parity_manifest_do_not_change_fingerprint(self):
        before = self.report()
        write(self.root, "unrelated.txt", "new commit\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "unrelated")
        write(self.root, ".claude/skills/bmad-prd/SKILL.md", "sibling differs\n")
        (self.root / "_bmad/_config/files-manifest.csv").write_text(
            (self.root / "_bmad/_config/files-manifest.csv").read_text() + '"md","x","core","core/x","00"\n')
        with patch("coordinator_core.bmad_contract._version", return_value="uv 9.9"):
            after = self.report()
        self.assertEqual(before.fingerprint_sha256, after.fingerprint_sha256)
        self.assertNotEqual(before.facts["head_commit"], after.facts["head_commit"])
        self.assertEqual("differs", after.facts["sibling_parity"]["bmad-prd"])
        self.assertEqual("uv 9.9", after.facts["tools"]["uv"])

    def test_contract_edit_or_locally_modified_file_changes_fingerprint_and_needs_compat_decision_not_rejection(self):
        before = self.report()
        self.assertEqual("matched", before.facts["manifest_check"])
        with self.skill("bmad-review", "references/lens-adversarial.md").open("a") as stream:
            stream.write("local tweak\n")
        after = self.report()
        self.assertTrue(after.supported)
        self.assertEqual("locally-modified", after.facts["manifest_check"])
        self.assertNotEqual(before.fingerprint_sha256, after.fingerprint_sha256)
        decision = {"id": "d", "feature_id": "f", "mandate_revision": 1, "source_kind": "coordinator",
                    "source_ref": "bmad-fingerprint:" + before.fingerprint_sha256,
                    "choice_json": json.dumps({"bmad_compat": before.fingerprint_sha256})}
        snapshot = {"decisions": [decision]}
        self.assertEqual("d", contract.compat_decision(snapshot, "f", 1, before.fingerprint_sha256))
        self.assertIsNone(contract.compat_decision(snapshot, "f", 2, before.fingerprint_sha256))
        self.assertIsNone(contract.compat_decision(snapshot, "f", 1, after.fingerprint_sha256))
        self.assertIsNone(contract.compat_decision(snapshot, "other", 1, before.fingerprint_sha256))

    def test_manifest_file_change_invalidates_frozen_closure_conservatively(self):
        before = self.report()
        (self.root / "_bmad/_config/manifest.yaml").write_text("version: changed\n")
        self.assertNotEqual(before.fingerprint_sha256, self.report().fingerprint_sha256)

    def test_explicit_absence_and_added_override_files_change_fingerprint(self):
        seen = {self.report().fingerprint_sha256}
        for rel in ("_bmad/custom/bmad-prd.toml", "_bmad/custom/bmad-review.user.toml", "_bmad/custom/config.toml",
                    "_bmad/config.user.yaml", "_bmad/custom/bmad-editorial-review.toml"):
            with self.subTest(rel):
                (self.root / rel).unlink(missing_ok=True)
                before = self.report()
                write(self.root, rel, "")
                added = self.report()
                (self.root / rel).unlink()
                seen.add(added.fingerprint_sha256)
                self.assertNotEqual(before.fingerprint_sha256, added.fingerprint_sha256)
        self.assertEqual(6, len(seen))

    def test_template_and_file_reference_closure_including_missing_and_glob_additions(self):
        write(self.root, "_bmad/custom/bmad-prd.toml",
              '[workflow]\nprd_template = "assets/other.md"\npersistent_facts = ["file:{project-root}/docs/*.md"]\n')
        self.assertIn("other.md", self.reasons())
        write(self.root, f"{CLIENT}/bmad-prd/assets/other.md", "# other\n")
        empty = self.report()
        self.assertTrue(empty.supported, empty.reasons)
        self.assertIn({"skill": "bmad-prd", "ref": "{project-root}/docs/*.md", "paths": []},
                      empty.contract["references"])
        write(self.root, "docs/a.md", "fact\n")
        added = self.report()
        self.assertNotEqual(empty.fingerprint_sha256, added.fingerprint_sha256)
        self.assertIn("docs/a.md", [item["path"] for item in added.contract_files])
        write(self.root, "_bmad/custom/bmad-prd.toml",
              '[workflow]\npersistent_facts = ["file:{project-root}/docs/missing.md"]\n')
        self.assertIn("missing.md", self.reasons())

    def test_relative_and_unknown_placeholders_resolve_consistently_or_fail_explicitly(self):
        write(self.root, "_bmad/custom/bmad-prd.toml", '[workflow]\nprd_template = "{planning_artifacts}/t.md"\n')
        self.assertIn("unresolved placeholder", self.reasons())
        write(self.root, "_bmad/custom/bmad-prd.toml", '[workflow]\npersistent_facts = ["file:_bmad/config.toml"]\n')
        self.assertIn("config.toml", self.reasons())  # relative file: refs resolve against the skill, never silently the root
        write(self.root, "_bmad/custom/bmad-prd.toml",
              '[workflow]\npersistent_facts = ["file:../../../_bmad/config.toml"]\n')
        self.assertTrue(self.report().supported)

    def test_resolver_runs_in_coordinator_and_failure_is_unsupported(self):
        report = self.report()
        resolved = report.contract["resolved"]
        self.assertEqual({"bmad-prd", "bmad-architecture", "bmad-review"}, set(resolved["skills"]))
        self.assertEqual(64, len(resolved["central"]))
        self.assertFalse((self.root / "_bmad/scripts/__pycache__").exists())
        write(self.root, "_bmad/custom/config.toml", "not = [valid\n")
        self.assertIn("central", self.reasons())

    def test_verify_fingerprint_names_changed_added_removed(self):
        report = self.report()
        self.assertEqual([], contract.verify_fingerprint(self.root, report.fingerprint_sha256, contract=report.contract))
        self.skill("bmad-prd", "assets/prd-template.md").write_text("changed\n")
        self.skill("bmad-prd", "assets/new.md").write_text("added\n")
        self.skill("bmad-review", "references/lens-adversarial.md").unlink()
        text = "\n".join(contract.verify_fingerprint(self.root, report.fingerprint_sha256, contract=report.contract))
        self.assertIn("changed: .agents/skills/bmad-prd/assets/prd-template.md", text)
        self.assertIn("added: .agents/skills/bmad-prd/assets/new.md", text)
        self.assertIn("removed: .agents/skills/bmad-review/references/lens-adversarial.md", text)
        self.assertEqual(["contract fingerprint differs"],
                         contract.verify_fingerprint(self.root, report.fingerprint_sha256))

    def test_central_paths_expand_or_fail_explicitly(self):
        self.assertEqual("_bmad-output/planning-artifacts", contract.central_path(self.root, "modules.bmm.planning_artifacts"))
        write(self.root, "_bmad/custom/config.toml", '[modules.bmm]\nplanning_artifacts = "{unknown}/x"\n')
        with self.assertRaises(DomainError) as caught:
            contract.central_path(self.root, "modules.bmm.planning_artifacts")
        self.assertEqual("UNSUPPORTED_CONTRACT", caught.exception.code)
        write(self.root, "_bmad/custom/config.toml", '[modules.bmm]\nplanning_artifacts = "/outside"\n')
        with self.assertRaises(DomainError):
            contract.central_path(self.root, "modules.bmm.planning_artifacts")
        # F23: BMAD 6.12.0 installs `output_folder = "_bmad-output"`, relative to the project root
        write(self.root, "_bmad/custom/config.toml", '[core]\noutput_folder = "_bmad-output"\n')
        self.assertEqual("_bmad-output", contract.central_path(self.root, "core.output_folder"))
        for outside in ("../elsewhere", ".", "a/../.."):
            write(self.root, "_bmad/custom/config.toml", f'[core]\noutput_folder = "{outside}"\n')
            with self.subTest(outside=outside), self.assertRaises(DomainError):
                contract.central_path(self.root, "core.output_folder")
        with self.assertRaises(DomainError):
            contract.central_path(self.root, "modules.bmm.absent_key")


class ActualInstallTests(unittest.TestCase):
    def test_actual_installed_discovery_is_read_only_and_summarized(self):
        repo = ROOT.parent
        if not (repo / "_bmad/_config/skill-manifest.csv").is_file():
            self.skipTest("no installed BMAD tree in this checkout")
        before = subprocess.run(["git", "-C", str(repo), "status", "--short"], capture_output=True, text=True).stdout
        report = contract.discover(repo)
        after = subprocess.run(["git", "-C", str(repo), "status", "--short"], capture_output=True, text=True).stdout
        self.assertEqual(before, after)
        self.assertEqual(report.supported, not report.reasons)
        summary = {"supported": report.supported, "reasons": report.reasons, "capabilities": report.capabilities,
                   "limitations": report.limitations, "fingerprint_sha256": report.fingerprint_sha256,
                   "contract_file_count": len(report.contract_files),
                   "reference_count": len(report.contract["references"]),
                   "absent_count": len(report.contract["absent"]),
                   "custom_listing_count": len(report.contract["custom_listing"]),
                   "resolved_skill_digests": len(report.contract["resolved"]["skills"]),
                   "central_digest_present": bool(report.contract["resolved"]["central"]),
                   "facts": report.facts}
        if SUMMARY.parent.is_dir():
            SUMMARY.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    unittest.main()
