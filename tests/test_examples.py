"""The example under examples/greetings (docs/getting-started.md, Quickstart): the fixture script builds a committed
repository whose backlog the coordinator's own readers parse and whose verify command passes, and the example
pipeline is a valid epic template. No model call."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from coordinator_core import drive_epic
from coordinator_core.repo import resolve_repo

ROOT = Path(__file__).parents[1]
EXAMPLE = ROOT / "examples" / "greetings"
ARTIFACTS = "_bmad-output/implementation-artifacts"


def _git(main: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(main), *args], capture_output=True, text=True, check=True).stdout


class ExampleFixtureTests(unittest.TestCase):
    def test_make_fixture_builds_a_committed_repository_the_epic_drive_can_read(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            target = Path(tmp) / "greetings"
            script = [sys.executable, str(EXAMPLE / "make_fixture.py")]
            result = subprocess.run([*script, str(target)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            main = target / "main"
            self.assertIn(str(main), result.stdout)
            self.assertEqual(_git(main, "status", "--porcelain"), "")
            self.assertEqual(_git(main, "log", "--format=%s").split("\n")[0], "greetings fixture")
            unit = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."],
                                  cwd=main, capture_output=True, text=True)
            self.assertEqual(unit.returncode, 0, unit.stderr)
            # The coordinator's own readers: the sprint entries and the stories of epic 1 at the committed base.
            commit = _git(main, "rev-parse", "HEAD").strip()
            stories = drive_epic.plan_stories(resolve_repo(main), commit, "_bmad-output/planning-artifacts/epics.md", 1)
            self.assertEqual([(story["key"], story["status"]) for story in stories],
                             [("1-1-greeting", "done"), ("1-2-farewell", "backlog"), ("1-3-shout", "backlog"),
                              ("1-4-whisper", "backlog")])
            self.assertIn("### DW-1:", (main / ARTIFACTS / "deferred-work.md").read_text())
            # The filled epic input and the copied pipeline beside the repository.
            epic_input = json.loads((target / "epic-input.json").read_text())
            self.assertEqual(epic_input["worktrees_dir"], str(target / "worktrees"))
            self.assertEqual(epic_input["epic"], 1)
            self.assertEqual((target / "pipeline.json").read_bytes(), (EXAMPLE / "pipeline.json").read_bytes())
            # A non-empty target is refused and left alone.
            again = subprocess.run([*script, str(target)], capture_output=True, text=True)
            self.assertEqual(again.returncode, 2)
            self.assertIn("not an empty directory", again.stderr)
            self.assertEqual(_git(main, "status", "--porcelain"), "")

    def test_the_example_pipeline_is_a_valid_epic_template(self):
        template = json.loads((EXAMPLE / "pipeline.json").read_text())
        canonical = drive_epic.check_template(template)
        self.assertEqual(canonical["feature_id"], "__FEATURE__")
        self.assertTrue(template["project"]["verify"])
        self.assertEqual({role["client"] for role in template["roles"].values()}, {"claude-code"})
        self.assertTrue(template["spec_auto_accept"])
