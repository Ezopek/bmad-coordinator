"""F24 (launch-boundary design L0): the coordinator's own Git calls in a worktree never run a command the
repository's config or hooks name, whoever wrote them."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from coordinator_core import bmad_implementation as implementation
from coordinator_core import clients
from coordinator_core.repo import GIT_SAFE, resolve_repo
from coordinator_core.run_receipt import inventory
from tests.test_bmad_core import CoreBase, writes

ROOT = Path(__file__).parents[1]


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=T", "-c", "user.email=t@example.invalid", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


class GitSafeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.repo, self.marker = self.root / "repo", self.root / "marker"
        git(self.root, "init", "-q", str(self.repo))
        (self.repo / "a.txt").write_text("a\n")
        git(self.repo, "add", "a.txt")
        git(self.repo, "commit", "-qm", "a")
        self.base = git(self.repo, "rev-parse", "HEAD")
        script = self.root / "planted.sh"
        script.write_text(f"#!/bin/sh\necho \"$0 $*\" >> {self.marker}\nexit 1\n")
        script.chmod(0o755)
        hooks = self.repo / ".git" / "hooks"
        for name in ("post-checkout", "post-merge", "pre-commit", "reference-transaction"):
            (hooks / name).write_text(f"#!/bin/sh\necho {name} >> {self.marker}\n")
            (hooks / name).chmod(0o755)
        # What a worker that can write .git/config would plant (written after the setup commits, which ran no hook).
        git(self.repo, "config", "core.fsmonitor", str(script))
        (self.repo / "b.txt").write_text("untracked\n")
        self.assertFalse(self.marker.exists())

    def ran(self) -> list[str]:
        return self.marker.read_text().splitlines() if self.marker.exists() else []

    def test_the_prefix_turns_off_fsmonitor_and_hooks(self):
        self.assertEqual(("-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null"), GIT_SAFE)
        subprocess.run(["git", "-C", str(self.repo), "ls-files", "--others"], capture_output=True)
        self.assertTrue(self.ran(), "the planted fsmonitor must run without the prefix, or this test proves nothing")

    def test_the_coordinators_reads_run_nothing_the_repository_names(self):
        stamps = inventory(self.repo)
        self.assertEqual({"a.txt", "b.txt"}, set(stamps))
        found = implementation.git_delta(self.repo, self.base, ["**"])
        self.assertIn("b.txt", found["changed"])
        self.assertTrue(clients._git_lines(self.repo, "worktree", "list", "--porcelain"))
        self.assertEqual(self.repo, resolve_repo(self.repo).worktree)
        self.assertEqual([], self.ran())

    def test_a_worktree_add_and_a_commit_run_no_hook(self):
        target = self.root / "story"
        implementation._git(self.repo, "worktree", "add", "-q", "-b", "story", str(target), self.base)
        self.assertTrue((target / "a.txt").is_file())
        (target / "c.txt").write_text("c\n")
        implementation._git_run(target, ["add", "c.txt"])
        implementation._git_run(target, ["-c", "user.name=T", "-c", "user.email=t@example.invalid", "commit",
                                         "--no-verify", "-q", "-m", "c"])
        self.assertEqual([], self.ran())


class GitMetadataRunTests(CoreBase):
    """F24: a run that changes the worktree's Git metadata ends `unknown` before the coordinator runs Git again;
    the fence stays held, as for every unknown run."""

    def planted(self, path_of, text, expect=None):
        path = path_of(self.t.repo.common_dir, Path(git(self.t.path, "rev-parse", "--absolute-git-dir")), self.t.path)
        report = self.run_it(self.assign(), FAKE_WRITE=writes(("result.md", "r"), (str(path), text)))
        self.assertEqual("unknown", report.state, report.evidence_needs)
        self.assertIn("Git metadata", " ".join(report.evidence_needs))
        record = json.loads((Path(report.run_dir) / "integrity.json").read_text())
        self.assertIn(str(expect or path), record["git_metadata_changed"])

    def test_a_clean_run_is_unaffected_and_its_integrity_record_lists_no_change(self):
        report = self.run_it(self.assign(), FAKE_WRITE=writes(("result.md", "r")))
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        record = json.loads((Path(report.run_dir) / "integrity.json").read_text())
        self.assertEqual([], record["git_metadata_changed"])

    def test_a_planted_hook(self):
        self.planted(lambda common, gitdir, tree: common / "hooks" / "post-checkout", "#!/bin/sh\n")

    def test_an_exclude_entry_that_would_hide_a_file_from_the_inventory(self):
        self.planted(lambda common, gitdir, tree: common / "info" / "exclude", "secret.md\n")

    def test_a_filter_driver_in_the_shared_config(self):
        self.planted(lambda common, gitdir, tree: common / "config",
                     (self.t.repo.common_dir / "config").read_text() + "[filter \"x\"]\n\tclean = touch\n")

    def test_a_config_worktree_in_the_worktrees_own_git_directory(self):
        self.planted(lambda common, gitdir, tree: gitdir / "config.worktree", "[core]\n\tfsmonitor = /bin/true\n")

    def test_a_redirected_worktree_pointer(self):
        self.planted(lambda common, gitdir, tree: tree / ".git", "gitdir: /elsewhere\n")

    def test_the_operators_global_git_config(self):
        home = self.root / "home"
        home.mkdir()
        with patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(home / "gitconfig"), "XDG_CONFIG_HOME": str(home)}):
            self.planted(lambda common, gitdir, tree: home / "gitconfig", "[filter \"x\"]\n\tclean = touch\n")

    def test_a_symlinked_hooks_directory_is_read_through(self):
        common = self.t.repo.common_dir
        elsewhere = self.root / "elsewhere-hooks"
        (common / "hooks").rename(elsewhere)
        (common / "hooks").symlink_to(elsewhere)
        self.planted(lambda common, gitdir, tree: elsewhere / "post-merge", "#!/bin/sh\n")

    def test_a_file_the_config_includes(self):
        included = self.root / "included.cfg"
        included.write_text("[core]\n\tautocrlf = false\n")
        git(self.t.path, "config", "include.path", str(included))
        self.planted(lambda common, gitdir, tree: included, "[filter \"x\"]\n\tclean = touch\n")

    def test_a_write_through_a_symlinked_global_config(self):
        home = self.root / "home"
        (home / "dotfiles").mkdir(parents=True)
        (home / "dotfiles" / "gitconfig").write_text("[core]\n\tautocrlf = false\n")
        (home / "gitconfig").symlink_to(home / "dotfiles" / "gitconfig")
        with patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(home / "gitconfig"), "XDG_CONFIG_HOME": str(home)}):
            self.planted(lambda common, gitdir, tree: home / "dotfiles" / "gitconfig",
                         "[filter \"x\"]\n\tclean = touch\n", expect=home / "gitconfig")

    def test_a_config_value_with_a_newline_cannot_fake_a_source(self):
        git(self.t.path, "config", "x.y", "multi\nfile:/fake-source\tcore.attributesfile=/fake-attrs")
        sources = [str(path) for path in clients._config_sources(self.t.repo)]
        self.assertNotIn("/fake-source", sources)
        self.assertNotIn("/fake-attrs", sources)
        self.assertIn(str(self.t.repo.common_dir / "config"), [os.path.normpath(item) for item in sources])

    def test_an_unreadable_entry_never_compares_equal(self):
        record = clients.integrity_record(self.t.repo, {}, {"group_terminated": True}, {"/x": "unreadable"})
        self.assertIn("/x", record["git_metadata_changed"])
        self.assertFalse(clients.integrity_clean(record))


if __name__ == "__main__":
    unittest.main()
