"""Epic drive (docs/epic-drive-design.md, E1–E15): every remaining story of an epic through the story drive on a
coordinator-owned integration branch, on fake clients."""

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

from coordinator_core import bmad_implementation as impl
from coordinator_core import bmad_review, drive, drive_epic, drive_pipeline, judgment
from coordinator_core.model import DecisionInput, DomainError
from coordinator_core.service import Coordinator
from tests.drive_support import CRITERIA, DriveCase, epic_checks, kill_group, unattended_checks
from tests.test_bmad_core import git
from tests.test_bmad_implementation import (BASE_TEST, FAREWELL, FAREWELL_TEST, GREET, SPEC_PATH, SPEC_READY,
                                            SPEC_WITH_QUESTION)
from tests.test_drive_story import Q1

ARTIFACTS = "_bmad-output/implementation-artifacts"
EPICS_PATH = "_bmad-output/planning-artifacts/epics.md"
HOUSE = "House rules: return strings, never print.\n"
PROJECT_CLAUSES = ["The coordinator commits; never ask whether to commit.",
                   "Choose the simplest implementation that satisfies the story."]
EPIC_FILE = """# Fixture - Epic Breakdown

## Epic 1: Greetings

### Story 1.1: Add a greeting

As a user,
I want a greeting,
So that the package says hello.

**Acceptance Criteria:**

**Given** a name
**When** greet runs
**Then** it returns Hello, <name>!

### Story 1.2: Add a farewell

As a user,
I want a farewell,
So that the package says goodbye.

**Acceptance Criteria:**

**Given** a name
**When** farewell runs
**Then** it returns Goodbye, <name>!

### Story 1.3: Add a shout

As a user,
I want a shout,
So that the package can be loud.

**Preconditions:** Do not start until each entry carries a settling decision or reads done.
- DW-1 — the decision on the exclamation mark

**Acceptance Criteria:**

**Given** a name
**When** shout runs
**Then** it returns HEY, <name>!

### Story 1.4: Add a whisper

As a user,
I want a whisper,
So that the package can be quiet.

**Acceptance Criteria:**

**Given** a name
**When** whisper runs
**Then** it returns psst, <name>

## Epic 2: Other

### Story 2.1: Something else

As a user,
I want something,
So that it exists.

**Acceptance Criteria:**

**Given** x
**When** y
**Then** z
"""
LEDGER_FILE = """# Deferred work

### DW-1: Exclamation mark
origin: spec-deferred 1
location: epics.md
source_spec: `spec-1-1-greeting.md`
severity: low
reason: how loud
status: open
decision: 2026-10-01 one — one exclamation mark
"""
LEDGER_OPEN = LEDGER_FILE.replace("decision: 2026-10-01 one — one exclamation mark",
                                  "decision: 2026-10-01 keep-open — later")


def sprint_file(**status) -> str:
    """The fixture's sprint status; 1-1 is done, the others backlog unless overridden by key with `_` for `-`."""
    rows = {"1-2-farewell": "backlog", "1-3-shout": "backlog", "1-4-whisper": "backlog"}
    rows.update({key.replace("_", "-"): value for key, value in status.items()})
    lines = ["# Story Status", "", "development_status:", "  epic-1: in-progress", "  1-1-greeting: done"]
    lines += [f"  {key}: {value}" for key, value in rows.items()]
    return "\n".join(lines + ["  epic-2: backlog", "  2-1-other: backlog", ""])


def story_spec(number: str, title: str, func: str, word: str) -> str:
    return (SPEC_READY.replace("Story 1.2: Add a farewell", f"Story {number}: {title}").replace("say goodbye", func)
            .replace("farewell", func).replace("Goodbye", word))


def spec_path(key: str) -> str:
    return f"{ARTIFACTS}/spec-{key}.md"


SHOUT_SPEC = story_spec("1.3", "Add a shout", "shout", "HEY")
WHISPER_SPEC = story_spec("1.4", "Add a whisper", "whisper", "psst")
SHOUT = "def shout(n):\n    return 'HEY, %s!' % (n or 'stranger')\n"
SHOUT_TEST = FAREWELL_TEST.replace("farewell", "shout").replace("Goodbye", "HEY")
WHISPER = "def whisper(n):\n    return 'psst, %s!' % (n or 'stranger')\n"
WHISPER_TEST = FAREWELL_TEST.replace("farewell", "whisper").replace("Goodbye", "psst")


class SeamTests(DriveCase):
    """Task 2: the driver seams an epic run uses; a story drive alone is unchanged."""

    def test_operator_action_names_the_epic_command_for_resume_only(self):
        text = drive._operator_action("needs-human", "product-decision", {"request_id": "r1"}, command="epic")
        self.assertEqual("inbox add decision for request r1, then epic resume "
                         "(or keep a driver waiting with --human-wait)", text)
        self.assertIsNone(drive._operator_action("done", None, {}, command="epic"))
        self.assertEqual("epic resume", drive._operator_action("continue", "max-turns", {}, command="epic"))
        self.assertEqual("drive resume", drive._operator_action("continue", "max-turns", {}))
        self.assertIn("then drive start", drive._operator_action("needs-human", "mandate-revised", {}, command="epic"))

    def test_drive_resume_on_an_epic_session_is_the_epic_session_refusal(self):
        owner = self.coordinator.claim("epic:abc", self.drive_token)
        with self.assertRaises(drive.Refusal) as caught:
            drive.resume(self.t.repo, owner)
        self.assertEqual({"reason": "epic-session", "action": "epic resume --token-file <driver token>"},
                         caught.exception.extra)
        code, _, error = self.run_cli("drive", "resume", "--token-file", str(self.drive_token), code=2)
        self.assertEqual(("INVALID_INPUT", "epic-session"), (error["error"], error["reason"]))

    def test_a_run_with_release_on_done_false_keeps_the_owner_and_records_the_epic_session(self):
        pipeline = drive_pipeline.validate(self.pipeline())
        bounds = drive.RunBounds(20, 3600)
        fd = drive.acquire_lock(self.t.repo, exclusive=True)
        try:
            owner = self.coordinator.claim("epic:test", self.drive_token)
            lease = drive.bind_and_lease(self.t.repo, self.store, owner, pipeline, bounds, None, "epic:test",
                                         epic_id="test")
            summary, code = drive.Run(self.t.repo, self.store, owner, lease, pipeline, bounds, time.monotonic, "start",
                                      release_on_done=False).execute()
        finally:
            os.close(fd)
        self.assertEqual((0, "done", "retained", "epic run"),
                         (code, summary["outcome"], summary["ownership"], summary["ownership_reason"]), summary)
        self.assertEqual(("active", "epic:test"),
                         (self.snapshot()["owner"]["state"], self.snapshot()["owner"]["session_id"]))
        self.assertEqual(("epic:test", "test", pipeline["feature_id"]),
                         (lease["session_id"], lease["epic_id"], lease["feature_id"]))
        sealed = drive._sealed(self.base_dir,
                               next(row for row in self.snapshot()["events"] if row["kind"] == "drive.leased"))
        self.assertEqual(lease, sealed)
        self.assertEqual("pass", unattended_checks(self.t.repo, [lease["lease_id"]])["driver-claimed-generation"])

    def test_driver_claimed_generation_follows_the_lease_history_after_a_reseal(self):
        pipeline = drive_pipeline.validate(self.pipeline())
        bounds = drive.RunBounds(20, 3600)
        fd = drive.acquire_lock(self.t.repo, exclusive=True)
        try:
            owner = self.coordinator.claim("epic:test", self.drive_token)
            lease = drive.bind_and_lease(self.t.repo, self.store, owner, pipeline, bounds, None, "epic:test",
                                         epic_id="test")
            summary, code = drive.Run(self.t.repo, self.store, owner, lease, pipeline, bounds, time.monotonic, "start",
                                      release_on_done=False).execute()
        finally:
            os.close(fd)
        self.assertEqual((0, "done"), (code, summary["outcome"]), summary)
        check = lambda: unattended_checks(self.t.repo, [lease["lease_id"]])["driver-claimed-generation"]  # noqa: E731
        self.assertEqual("pass", check())
        self.coordinator.release(owner)
        again = self.coordinator.claim("epic:test", self.root / "second.token")
        self.assertNotEqual(owner.generation, again.generation)
        drive.reseal_lease(self.store, again, lease, f"lease-g{again.generation}")
        self.assertEqual("pass", check())   # the one run matches the lease record current when it started
        drive.reseal_lease(self.store, again, {**lease, "session_id": "epic:other"}, "lease-other")
        self.assertEqual("fail", check())   # a session naming another epic than the lease's epic_id

    def test_drive_start_still_releases_at_done_and_its_lease_has_no_epic_id(self):
        summary, code = drive.start(self.t.repo, self.fid, self.pipeline(), self.drive_token, drive.RunBounds(20, 3600))
        self.assertEqual((0, "done", "released"), (code, summary["outcome"], summary["ownership"]), summary)
        lease = drive._sealed(self.base_dir,
                              next(row for row in self.snapshot()["events"] if row["kind"] == "drive.leased"))
        self.assertNotIn("epic_id", lease)
        self.assertEqual(f"drive:{lease['lease_id']}", lease["session_id"])

    def test_reseal_lease_anchors_the_lease_under_a_new_generation_and_find_lease_returns_it(self):
        drive.start(self.t.repo, self.fid, self.pipeline(), self.drive_token, drive.RunBounds(20, 3600))
        row = next(row for row in self.snapshot()["events"] if row["kind"] == "drive.leased")
        lease = drive._sealed(self.base_dir, row)
        owner = self.coordinator.claim("drive:" + lease["lease_id"], self.root / "second.token")
        resealed = drive.reseal_lease(self.store, owner, lease, f"lease-g{owner.generation}")
        self.assertEqual({**lease, "generation": owner.generation}, resealed)
        self.assertEqual(resealed, drive.find_lease(self.store, owner))
        self.assertTrue((self.base_dir / "drives" / lease["lease_id"] / f"lease-g{owner.generation}.json").is_file())
        self.assertEqual(resealed, drive.reseal_lease(self.store, owner, lease, f"lease-g{owner.generation}"))
        rows = [item for item in self.snapshot()["events"]
                if item["kind"] == "drive.leased" and item["subject_id"] == lease["lease_id"]]
        self.assertEqual(3, len(rows))

    def test_the_epic_events_and_anchor_top_are_closed_kinds(self):
        from coordinator_core.service import ANCHOR_TOPS, DRIVE_EVENTS
        self.assertTrue({"epic.started", "epic.advanced", "epic.ended"} <= DRIVE_EVENTS)
        self.assertIn("epics", ANCHOR_TOPS)
        owner = self.coordinator.claim("epic:x", self.drive_token)
        base = self.base_dir
        ref, digest = drive._seal(base, base / "epics" / "x" / "state-1.json", {"kind": "epic-state", "format": 1})
        self.coordinator.anchor_event(owner, "epic.started", "x", ("epic", ref, digest))
        self.assertEqual(1, len([row for row in self.snapshot()["events"] if row["kind"] == "epic.started"]))
        with self.assertRaises(DomainError):
            self.coordinator.anchor_event(owner, "epic.advanced", "x", ("epic", "elsewhere/state.json", digest))


class EpicDriveCase(DriveCase):
    """DriveCase whose main worktree holds an epics file with four stories (1.1 done), a sprint status, a deferred-work
    ledger, a package and tests, and no BMAD tree (design D22); the epic runs from `self.main` with its own worktrees
    under `self.worktrees`."""

    def setUp(self):
        super().setUp()
        self.worktrees = self.root / "wt"
        self.jroot = self.root / "judgments"
        self.jroot.mkdir()
        for name in ("_bmad", ".agents"):
            if (self.main / name).exists():
                shutil.rmtree(self.main / name)
        self.commit_files({f"{ARTIFACTS}/sprint-status.yaml": sprint_file(),
                           f"{ARTIFACTS}/deferred-work.md": LEDGER_FILE, EPICS_PATH: EPIC_FILE,
                           "docs/house-rules.md": HOUSE, "pkg/__init__.py": "", "pkg/greet.py": GREET,
                           "tests/__init__.py": "", "tests/test_base.py": BASE_TEST}, "epic base")
        self.base = git(self.main, "rev-parse", "HEAD")
        self.base_branch = git(self.main, "symbolic-ref", "--short", "HEAD")
        self.drive_repo = self.main
        self.repo = impl.resolve_repo(self.main)
        self.scenario(stories=self.story_scenarios())
        self.template_file = self.write_template()

    def commit_files(self, files: dict, message: str, repo=None) -> str:
        root = repo or self.main
        for rel, text in files.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text)
        git(root, "add", "-A")
        git(root, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", message)
        return git(root, "rev-parse", "HEAD")

    def epic_worktree(self) -> Path:
        return self.worktrees / "epic-1"

    def template(self, **over):
        story = {"spec_path": f"{ARTIFACTS}/spec-__KEY__.md", "story_key": "__KEY__"}
        steps = [{"id": "spec", "kind": "spec", "author_id": "story-worker", "story": story},
                 {"id": "spec-bundle", "kind": "bundle", "of": "spec"},
                 {"id": "spec-review", "kind": "review", "of": "spec-bundle", "reviewer_id": "code-reviewer"},
                 {"id": "spec-accept", "kind": "accept", "of": "spec-review"},
                 {"id": "spec-ready", "kind": "status", "phase": "spec", "of": "spec-accept"},
                 {"id": "setup", "kind": "setup"}, {"id": "start", "kind": "status", "phase": "start"},
                 {"id": "impl", "kind": "implement", "author_id": "story-worker", "story": story},
                 {"id": "impl-commit", "kind": "commit", "of": "impl"},
                 {"id": "impl-verify", "kind": "verify", "of": "impl-commit"},
                 {"id": "impl-bundle", "kind": "bundle", "of": "impl-verify"},
                 {"id": "impl-review", "kind": "review", "of": "impl-bundle", "reviewer_id": "code-reviewer"},
                 {"id": "impl-accept", "kind": "accept", "of": "impl-review"},
                 {"id": "finish", "kind": "status", "phase": "done", "of": "impl-accept"}]
        roles = {"worker": {"client": "codex-cli", "model": "fake-model", "timeout": 60},
                 "reviewer": {"client": "codex-cli", "model": "fake-review", "timeout": 60},
                 "judgment": {"client": "codex-cli", "model": "fake-judge", "timeout": 60}}
        project = {"setup": [], "verify": [{"name": "unit", "timeout_seconds": 120,
                                            "argv": [sys.executable, "-m", "unittest", "discover", "-s", "tests",
                                                     "-t", "."]}],
                   "env": ["PATH"],
                   "commit_identity": {"name": "BMAD Coordinator", "email": "coordinator@example.invalid"},
                   "output_limit_bytes": 8192, "rules": ["docs/house-rules.md"]}
        value = {"kind": "drive-pipeline", "format": 1, "feature_id": "__FEATURE__", "mandate_revision": 1,
                 "roles": roles, "budgets": {"max_updates": 3, "max_judgments": 8, "max_corrections": 3},
                 "spec_auto_accept": True, "project": project, "steps": steps}
        value.update(over)
        return value

    def write_template(self, **over) -> Path:
        path = self.root / "template.json"
        path.write_text(json.dumps(self.template(**over)))
        return path

    def story_scenarios(self, **over) -> dict:
        value = {"1-2-farewell": {"spec-1": {"files": [[SPEC_PATH, SPEC_READY]], "open_questions": []},
                                  "impl-1": {"files": [["pkg/farewell.py", FAREWELL],
                                                       ["tests/test_farewell.py", FAREWELL_TEST]]}},
                 "1-3-shout": {"spec-1": {"files": [[spec_path("1-3-shout"), SHOUT_SPEC]], "open_questions": []},
                               "impl-1": {"files": [["pkg/shout.py", SHOUT], ["tests/test_shout.py", SHOUT_TEST]]}},
                 "1-4-whisper": {"spec-1": {"files": [[spec_path("1-4-whisper"), WHISPER_SPEC]], "open_questions": []},
                                 "impl-1": {"files": [["pkg/whisper.py", WHISPER],
                                                      ["tests/test_whisper.py", WHISPER_TEST]]}}}
        for key, steps in over.items():
            value.setdefault(key, {}).update(steps)
        return value

    def epic_input(self, **over) -> dict:
        value = {"epics_path": EPICS_PATH, "epic": 1, "worktrees_dir": str(self.worktrees),
                 "story_defaults": {"writable_scopes": ["pkg/", "tests/"], "project_clauses": PROJECT_CLAUSES}}
        value.update(over)
        return value

    def epic_args(self, *extra, template=None, token=None):
        return ["epic", "start", "--token-file", str(token or self.drive_token),
                "--pipeline", str(template or self.template_file), "--judgment-root", str(self.jroot),
                "--max-turns", "60", "--deadline", "3600", *extra]

    def epic_start(self, *extra, data=None, code=None, timeout=900, template=None):
        return self.run_cli(*self.epic_args(*extra, template=template),
                            data=data if data is not None else self.epic_input(), code=code, timeout=timeout)

    def epic_resume(self, *extra, code=None, token=None, timeout=900):
        return self.run_cli("epic", "resume", "--token-file", str(token or self.drive_token), "--max-turns", "60",
                            *extra, code=code, timeout=timeout)

    def states(self, summary) -> list[tuple]:
        return [(item["key"], item["state"]) for item in summary["epic"]["stories"]]

    def integration_log(self, *extra) -> list[str]:
        return git(self.main, "log", "--first-parent", "--format=%s|%an", *extra,
                   f"{self.base}..coordinator/epic-1").split("\n")

    def brief_of(self, key: str, heading: str) -> str:
        folder = self.worktrees / f"story-{key}" / "_bmad-output" / "coordinator" / "briefs"
        return next(path.read_text() for path in sorted(folder.glob("*.md")) if path.read_text().startswith(heading))


class EpicUnitTests(EpicDriveCase):
    """Task 3: the pieces of the epic drive, no story run."""

    def input(self, **over):
        value = {"epics_path": EPICS_PATH, "epic": 1, "worktrees_dir": str(self.worktrees),
                 "story_defaults": {"writable_scopes": ["pkg/", "tests/"], "project_clauses": PROJECT_CLAUSES}}
        value.update(over)
        return drive_epic.EpicInput(**value)

    def test_check_input_fills_the_default_branch_and_refuses_bad_values(self):
        value = drive_epic.check_input(self.repo, self.input())
        self.assertEqual(("coordinator/epic-1", None, str(self.worktrees), {}),
                         (value["branch"], value["base"], value["worktrees_dir"], value["stories"]))
        value = drive_epic.check_input(self.repo, self.input(branch="work/epic", base="HEAD",
                                                             stories={"1-3-shout": {"writable_scopes": ["pkg/"]}}))
        self.assertEqual(("work/epic", "HEAD", {"1-3-shout": {"writable_scopes": ["pkg/"]}}),
                         (value["branch"], value["base"], value["stories"]))
        git(self.main, "checkout", "-q", "-b", "side")   # `@{-1}` now expands to `side`
        git(self.main, "checkout", "-q", self.base_branch)
        for over, code in (({"epic": 0}, "INVALID_INPUT"), ({"epics_path": "/abs/epics.md"}, "INVALID_INPUT"),
                           ({"epics_path": "../epics.md"}, "INVALID_INPUT"), ({"story_defaults": {}}, "INVALID_INPUT"),
                           ({"story_defaults": {"writable_scopes": []}}, "INVALID_INPUT"),
                           ({"story_defaults": {"writable_scopes": ["pkg/"], "other": 1}}, "INVALID_INPUT"),
                           ({"stories": {"Bad Key": {}}}, "INVALID_INPUT"),
                           ({"stories": {"1-3-shout": {"writable_scopes": "pkg/"}}}, "INVALID_INPUT"),
                           ({"branch": "refs/heads/x"}, "INVALID_INPUT"), ({"branch": "a b"}, "INVALID_INPUT"),
                           ({"branch": "a..b"}, "INVALID_INPUT"), ({"branch": "@{-1}"}, "INVALID_INPUT"),
                           ({"branch": ""}, "INVALID_INPUT"), ({"worktrees_dir": ""}, "INVALID_INPUT")):
            with self.assertRaises(DomainError, msg=over) as caught:
                drive_epic.check_input(self.repo, self.input(**over))
            self.assertEqual(code, caught.exception.code, over)

    def test_epic_input_refuses_a_worktrees_dir_inside_the_repository(self):
        for inside in (self.main, self.main / "wt"):
            with self.assertRaises(DomainError) as caught:
                drive_epic.check_input(self.repo, self.input(worktrees_dir=str(inside)))
            self.assertEqual("UNSAFE_PATH", caught.exception.code)

    def test_check_template_and_fill_template(self):
        template = drive_epic.check_template(self.template())
        filled = drive_epic.fill_template(template, "f" * 32, "1-3-shout")
        self.assertEqual(("f" * 32, {"spec_path": f"{ARTIFACTS}/spec-1-3-shout.md", "story_key": "1-3-shout"}),
                         (filled["feature_id"], filled["steps"][0]["story"]))
        self.assertEqual(filled["steps"][0]["story"], filled["steps"][7]["story"])
        self.assertNotIn("__", json.dumps(filled))
        keyless = [{**step, "story": {"spec_path": "x.md", "story_key": "__KEY__"}} if "story" in step else step
                   for step in self.template()["steps"]]
        for over, text in (({"feature_id": "abc"}, "__FEATURE__"), ({"mandate_revision": 2}, "revision 1"),
                           ({"steps": self.template()["steps"][5:]}, "spec step"), ({"steps": keyless}, "__KEY__")):
            with self.assertRaises(DomainError, msg=over) as caught:
                drive_epic.check_template(self.template(**over))
            self.assertEqual("INVALID_INPUT", caught.exception.code)
            self.assertIn(text, caught.exception.message)
        with self.assertRaises(DomainError):
            drive_epic.check_template(self.template(roles={}))   # validate's own refusals pass through
        with self.assertRaises(DomainError):
            drive_epic.check_template([])

    def test_plan_stories_lists_the_epic_in_order_with_one_sprint_key_each(self):
        plan = drive_epic.plan_stories(self.repo, self.base, EPICS_PATH, 1)
        self.assertEqual([("1.1", "1-1-greeting", "done"), ("1.2", "1-2-farewell", "backlog"),
                          ("1.3", "1-3-shout", "backlog"), ("1.4", "1-4-whisper", "backlog")],
                         [(item["number"], item["key"], item["status"]) for item in plan])
        self.assertEqual("Add a shout", plan[2]["title"])
        commit = self.commit_files({f"{ARTIFACTS}/sprint-status.yaml": sprint_file() + "  1-3-shout-again: backlog\n"},
                                   "twice")
        plan = drive_epic.plan_stories(self.repo, commit, EPICS_PATH, 1)
        self.assertEqual((None, ["1-3-shout", "1-3-shout-again"]), (plan[2]["key"], plan[2]["keys"]))
        plan = drive_epic.plan_stories(self.repo, commit, EPICS_PATH, 2)
        self.assertEqual([("2.1", "2-1-other", "backlog")],
                         [(item["number"], item["key"], item["status"]) for item in plan])
        with self.assertRaises(DomainError) as caught:
            drive_epic.plan_stories(self.repo, commit, EPICS_PATH, 3)
        self.assertEqual("STALE_INPUT", caught.exception.code)
        commit = self.commit_files({f"{ARTIFACTS}/sprint-status.yaml": "# no map\n"}, "broken")
        with self.assertRaises(DomainError) as caught:
            drive_epic.plan_stories(self.repo, commit, EPICS_PATH, 1)
        self.assertEqual(("STALE_INPUT", True),
                         (caught.exception.code, caught.exception.message.startswith("story-status")))
        self.assertEqual({"epic-1": "in-progress", "1-1-greeting": "done"},
                         {key: value for key, value in drive_epic.sprint_entries(sprint_file()).items()
                          if key in ("epic-1", "1-1-greeting")})

    def test_create_integration_refuses_an_invalid_or_existing_branch_and_an_existing_worktree_path(self):
        created = drive_epic.create_integration(self.repo, "coordinator/epic-1", None, self.epic_worktree())
        self.assertEqual({"base_commit": self.base, "base_branch": self.base_branch}, created)
        self.assertEqual(self.base, git(self.main, "rev-parse", "coordinator/epic-1"))
        self.assertEqual("refs/heads/coordinator/epic-1", git(self.epic_worktree(), "symbolic-ref", "HEAD"))
        with self.assertRaises(drive.Refusal) as caught:
            drive_epic.create_integration(self.repo, "coordinator/epic-1", None, self.worktrees / "other")
        self.assertEqual("branch-exists", caught.exception.extra["reason"])
        with self.assertRaises(drive.Refusal) as caught:
            drive_epic.create_integration(self.repo, "coordinator/epic-2", None, self.epic_worktree())
        self.assertEqual("worktree-exists", caught.exception.extra["reason"])
        with self.assertRaises(DomainError) as caught:
            drive_epic.create_integration(self.repo, "coordinator/epic-2", "no-such-ref", self.worktrees / "e2")
        self.assertEqual("INVALID_INPUT", caught.exception.code)
        self.assertFalse((self.worktrees / "e2").exists())
        created = drive_epic.create_integration(self.repo, "coordinator/epic-3", self.base, self.worktrees / "e3")
        self.assertEqual({"base_commit": self.base, "base_branch": None}, created)   # a sha: no tracking
        created = drive_epic.create_integration(self.repo, "coordinator/epic-4", self.base_branch,
                                                self.worktrees / "e4")
        self.assertEqual({"base_commit": self.base, "base_branch": self.base_branch}, created)
        created = drive_epic.create_integration(self.repo, "coordinator/epic-5", "HEAD", self.worktrees / "e5")
        self.assertEqual({"base_commit": self.base, "base_branch": self.base_branch}, created)   # HEAD symbolic (E3)
        created = drive_epic.create_integration(self.repo, "coordinator/epic-6", f"{self.base_branch}~0",
                                                self.worktrees / "e6")
        self.assertEqual({"base_commit": self.base, "base_branch": None}, created)   # a revision expression
        git(self.main, "tag", "v1", self.base)
        created = drive_epic.create_integration(self.repo, "coordinator/epic-7", "v1", self.worktrees / "e7")
        self.assertEqual({"base_commit": self.base, "base_branch": None}, created)   # a tag
        git(self.main, "checkout", "-q", "--detach")
        created = drive_epic.create_integration(self.repo, "coordinator/epic-8", None, self.worktrees / "e8")
        self.assertEqual({"base_commit": self.base, "base_branch": None}, created)   # a detached HEAD

    def test_a_failed_worktree_add_leaves_no_branch_and_a_file_worktrees_dir_is_refused(self):
        blocker = self.root / "blocker"
        blocker.write_text("a file\n")
        with self.assertRaises(DomainError) as caught:
            drive_epic.check_input(self.repo, self.input(worktrees_dir=str(blocker)))
        self.assertEqual("INVALID_INPUT", caught.exception.code)
        with self.assertRaises(DomainError):
            drive_epic.create_integration(self.repo, "coordinator/epic-1", None, blocker / "epic-1")
        self.assertIsNone(drive_epic._rev(self.repo.worktree, "refs/heads/coordinator/epic-1"))
        self.assertFalse(os.path.lexists(blocker / "epic-1"))
        self.assertNotIn(str(blocker / "epic-1"), git(self.main, "worktree", "list", "--porcelain"))

    def test_check_worktree_wants_the_branch_and_a_clean_tree(self):
        drive_epic.create_integration(self.repo, "coordinator/epic-1", None, self.epic_worktree())
        drive_epic.check_worktree(self.epic_worktree(), "coordinator/epic-1")
        (self.epic_worktree() / "untracked.txt").write_text("x\n")
        drive_epic.check_worktree(self.epic_worktree(), "coordinator/epic-1")   # untracked files do not drift
        (self.epic_worktree() / "docs" / "house-rules.md").write_text("changed\n")
        with self.assertRaises(DomainError) as caught:
            drive_epic.check_worktree(self.epic_worktree(), "coordinator/epic-1")
        self.assertEqual(("STALE_INPUT", "worktree-drift: the epic worktree has uncommitted changes"),
                         (caught.exception.code, caught.exception.message))
        git(self.epic_worktree(), "checkout", "--", ".")
        git(self.epic_worktree(), "checkout", "-q", "--detach")
        with self.assertRaises(DomainError) as caught:
            drive_epic.check_worktree(self.epic_worktree(), "coordinator/epic-1")
        self.assertTrue(caught.exception.message.startswith("worktree-drift: HEAD is not on branch coordinator/epic-1"))

    def test_merge_into_fast_forwards_merges_with_the_identity_or_aborts_a_conflict(self):
        project = self.template()["project"]
        drive_epic.create_integration(self.repo, "coordinator/epic-1", None, self.epic_worktree())
        git(self.main, "branch", "story/a", self.base)
        story_a = self.root / "story-a"
        git(self.main, "worktree", "add", "-q", str(story_a), "story/a")
        sha_a = self.commit_files({"pkg/a.py": "A = 1\n"}, "a", repo=story_a)
        result = drive_epic.merge_into(self.epic_worktree(), "refs/heads/story/a", "epic(1): merge story/a", project)
        self.assertEqual({"outcome": "fast-forward", "head": sha_a}, result)
        self.assertEqual({"outcome": "already", "head": sha_a},
                         drive_epic.merge_into(self.epic_worktree(), "refs/heads/story/a", "x", project))
        # the integration branch moved: a merge commit by the identity, no hook, the given message
        moved = self.commit_files({"docs/note.md": "note\n"}, "operator note", repo=self.epic_worktree())
        git(self.main, "branch", "story/b", sha_a)
        story_b = self.root / "story-b"
        git(self.main, "worktree", "add", "-q", str(story_b), "story/b")
        self.commit_files({"pkg/b.py": "B = 1\n"}, "b", repo=story_b)
        git(self.main, "config", "merge.log", "true")   # a user's config cannot rewrite the recorded message
        result = drive_epic.merge_into(self.epic_worktree(), "refs/heads/story/b", "epic(1): merge story/b", project)
        self.assertEqual("merge", result["outcome"])
        self.assertEqual("epic(1): merge story/b",
                         git(self.epic_worktree(), "log", "-1", "--format=%B", result["head"]))
        shown = git(self.epic_worktree(), "log", "-1", "--format=%an%n%ae%n%s%n%P", result["head"]).split("\n")
        self.assertEqual(["BMAD Coordinator", "coordinator@example.invalid", "epic(1): merge story/b"], shown[:3])
        self.assertEqual({moved, git(self.main, "rev-parse", "story/b")}, set(shown[3].split()))
        # a conflict: aborted, the worktree clean, Git's words in the detail
        git(self.main, "branch", "story/c", sha_a)
        story_c = self.root / "story-c"
        git(self.main, "worktree", "add", "-q", str(story_c), "story/c")
        self.commit_files({"docs/note.md": "other note\n"}, "c", repo=story_c)
        result = drive_epic.merge_into(self.epic_worktree(), "refs/heads/story/c", "epic(1): merge story/c", project)
        self.assertEqual(("conflict", git(self.epic_worktree(), "rev-parse", "HEAD")),
                         (result["outcome"], result["head"]))
        self.assertIn("conflict", result["detail"].lower())
        self.assertTrue(result["detail"].startswith("conflicting paths: docs/note.md; "), result["detail"])
        self.assertEqual("", git(self.epic_worktree(), "status", "--porcelain"))
        result = drive_epic.merge_into(self.epic_worktree(), "refs/heads/story/none", "x", project)
        self.assertEqual(("conflict", "refs/heads/story/none does not resolve to a commit"),
                         (result["outcome"], result["detail"]))
        real = impl._git_run

        def no_abort(worktree, args, *rest, **options):
            if list(args[:2]) == ["merge", "--abort"]:
                return subprocess.CompletedProcess(args, 1, b"", b"")
            return real(worktree, args, *rest, **options)
        with patch.object(impl, "_git_run", no_abort), self.assertRaises(DomainError) as caught:
            drive_epic.merge_into(self.epic_worktree(), "refs/heads/story/c", "epic(1): merge story/c", project)
        self.assertEqual("INVALID_REPOSITORY", caught.exception.code)
        self.assertTrue(caught.exception.message.startswith(f"merge abort left {self.epic_worktree()} dirty: "))
        git(self.epic_worktree(), "merge", "--abort")

    def test_a_story_level_drift_keeps_the_story_action_and_an_epic_level_drift_names_the_epic_worktree(self):
        owner = self.coordinator.claim("epic:e1", self.drive_token)
        story = {"number": "1.2", "title": "Add a farewell", "key": "1-2-farewell", "state": "current",
                 "feature_id": "f" * 32, "lease_id": "l" * 32}
        state = {"kind": "epic-state", "format": 1, "revision": 1, "epic_id": "e1", "generation": owner.generation,
                 "epic": 1, "branch": "coordinator/epic-1", "worktree": str(self.epic_worktree()),
                 "head_sha": self.base, "base_branch": None, "base_synced_sha": self.base, "stories": [story]}
        epic = drive_epic.Epic(self.repo, self.store, owner, state, drive_epic.check_template(self.template()),
                               drive.RunBounds(60, 3600), time.monotonic)
        subjects = {"step_id": "impl-commit", "detail": "worktree-drift: HEAD is not on branch story/1-2-farewell",
                    "expected": "a" * 40, "key": "1-2-farewell", "feature_id": "f" * 32}
        epic.story_outcome = True   # the story drive's own `worktree-drift` (its story branch), passed through
        summary, code = epic._end(drive.Outcome("needs-human", "worktree-drift", subjects))
        self.assertEqual((11, drive._operator_action("needs-human", "worktree-drift", subjects, command="epic")),
                         (code, summary["operator_action"]))
        self.assertTrue(summary["operator_action"].startswith("restore the story branch"))
        self.assertIn("epic resume --skip 1-2-farewell", summary["epic_action"])
        epic.story_outcome = False   # the epic worktree's drift (E4)
        drift = {"worktree": str(self.epic_worktree()), "branch": "coordinator/epic-1",
                 "detail": "worktree-drift: the epic worktree has uncommitted changes"}
        summary, code = epic._end(drive.Outcome("needs-human", "worktree-drift", drift))
        self.assertTrue(summary["operator_action"].startswith(f"restore the epic worktree {self.epic_worktree()}"))
        self.assertIsNone(summary["epic_action"])
        self.assertEqual(2, len([row for row in self.snapshot()["events"] if row["kind"] == "epic.ended"]))

    def test_a_failure_the_loop_raises_names_epic_resume_never_drive_start(self):
        """Design epic-drive E10: the loop's own `stale-input`, `corrupt-state` and `internal-error` name the epic's
        repair and its re-claim; a signal stop keeps the story drive's text read with `epic resume`."""
        owner = self.coordinator.claim("epic:e1", self.drive_token)
        state = {"kind": "epic-state", "format": 1, "revision": 1, "epic_id": "e1", "generation": owner.generation,
                 "epic": 1, "branch": "coordinator/epic-1", "worktree": str(self.epic_worktree()),
                 "head_sha": self.base, "base_branch": None, "base_synced_sha": self.base, "stories": []}
        epic = drive_epic.Epic(self.repo, self.store, owner, state, drive_epic.check_template(self.template()),
                               drive.RunBounds(60, 3600), time.monotonic)
        repair = ("repair, then epic resume --token-file <driver token>; or release --token-file <driver token>, then "
                  "epic resume --epic e1 --token-file <new file>")
        failures = [epic._failure(DomainError(code, "x"))
                    for code in ("INVALID_REPOSITORY", "UNKNOWN_EVIDENCE", "UNSAFE_PATH", "CORRUPT_STATE", "BAD")]
        self.assertEqual(["stale-input"] * 3 + ["corrupt-state", "internal-error"], [item.reason for item in failures])
        for outcome in failures + [drive.Outcome("failed", "internal-error", {"detail": "KeyError: 'x'"})]:
            summary, _ = epic._end(outcome)
            self.assertEqual((repair, None), (summary["operator_action"], summary.get("end_error")), outcome)
        summary, _ = epic._end(drive.Outcome("needs-human", "signal-stop", {}))
        self.assertEqual("epic resume to continue, or release --token-file <driver token> to hand back",
                         summary["operator_action"])

    def test_a_story_run_reseals_a_lease_of_an_older_generation_first(self):
        """Design epic-drive E12: an E12 re-claim killed between its state and the lease's re-seal leaves the current
        story's newest lease record at the old generation; the next story run re-seals it before `Run`."""
        first = self.coordinator.claim("epic:e1", self.drive_token)
        lease = {"kind": "drive-lease", "format": 1, "lease_id": "l" * 32, "feature_id": "f" * 32,
                 "pipeline_sha256": "p" * 64, "generation": first.generation, "session_id": "epic:e1", "epic_id": "e1"}
        ref, digest = drive._seal(self.base_dir, self.base_dir / "drives" / ("l" * 32) / "lease.json", lease)
        self.coordinator.anchor_event(first, "drive.leased", "l" * 32, ("f" * 32, ref, digest))
        self.coordinator.release(first)
        owner = self.coordinator.claim("epic:e1", self.root / "second.token")
        story = {"number": "1.2", "title": "Add a farewell", "key": "1-2-farewell", "state": "current",
                 "feature_id": "f" * 32, "lease_id": "l" * 32}
        state = {"kind": "epic-state", "format": 1, "revision": 1, "epic_id": "e1", "generation": owner.generation,
                 "epic": 1, "branch": "coordinator/epic-1", "worktree": str(self.epic_worktree()),
                 "head_sha": self.base, "base_branch": None, "base_synced_sha": self.base, "stories": [story]}
        epic = drive_epic.Epic(self.repo, self.store, owner, state, drive_epic.check_template(self.template()),
                               drive.RunBounds(60, 3600), time.monotonic)
        ran = []

        class FakeRun:
            def __init__(self, repo, store, owner, lease, *rest, **options):
                ran.append(lease)

            def execute(self):
                return {"outcome": "needs-human", "reason": "spec-decisions", "run_id": "run-x", "subjects": {}}, 11
        with patch.object(drive, "Run", FakeRun), patch.object(drive_pipeline, "load", return_value=epic.sample), \
                patch.object(epic, "_feature", return_value={"worktree": str(self.main)}):
            outcome = epic._run(story)
        self.assertEqual(("needs-human", "spec-decisions"), (outcome.outcome, outcome.reason))
        self.assertEqual([{**lease, "generation": owner.generation}], ran)
        rows = [row for row in self.snapshot()["events"] if row["kind"] == "drive.leased"]
        self.assertEqual(f"drives/{'l' * 32}/lease-g{owner.generation}.json", rows[-1]["evidence_ref"])
        self.assertEqual(ran[0], epic._lease_record("l" * 32))
        self.assertIs(ran[0], epic._owned(ran[0]))   # the owner's generation: reused as it is
        self.assertEqual(2, len(rows))

    def test_states_are_sealed_anchored_and_found_by_the_owner_session(self):
        owner = self.coordinator.claim("epic:e1", self.drive_token)
        state = {"kind": "epic-state", "format": 1, "revision": 1, "epic_id": "e1", "generation": owner.generation,
                 "stories": []}
        ref, digest = drive_epic.anchor_state(self.store, owner, state, "epic.started", "epic")
        self.assertEqual("epics/e1/state-1.json", ref)
        second = {**state, "revision": 2, "stories": [{"number": "1.2", "state": "current"}]}
        drive_epic.anchor_state(self.store, owner, second, "epic.advanced", "1-2-farewell")
        self.assertEqual(second, drive_epic.newest_state(self.store, "e1"))
        self.assertEqual(second, drive_epic.find_epic(self.store, owner))
        self.assertEqual(["e1"], drive_epic.epic_ids(self.snapshot()))
        drive_epic.anchor_state(self.store, owner, second, "epic.advanced", "again")   # identical bytes after a kill
        self.assertEqual(3, len([row for row in self.snapshot()["events"] if row["kind"].startswith("epic.")]))
        self.assertIsNone(drive_epic.newest_state(self.store, "other"))
        self.coordinator.release(owner)
        other = self.coordinator.claim("drive:x", self.root / "other.token")
        with self.assertRaises(drive.Refusal) as caught:
            drive_epic.find_epic(self.store, other)
        self.assertEqual("run-unbound", caught.exception.extra["reason"])
        self.coordinator.release(other)
        stale = self.coordinator.claim("epic:e1", self.root / "stale.token")   # a new generation, no re-sealed state
        with self.assertRaises(drive.Refusal) as caught:
            drive_epic.find_epic(self.store, stale)
        self.assertEqual({"reason": "run-unbound", "action": "release --token-file <driver token>, then epic resume "
                                                             "--epic e1 --token-file <new file>"},
                         caught.exception.extra)

    def test_a_state_sealed_before_a_kill_is_replaced_and_an_anchored_state_is_never_overwritten(self):
        """Design epic-drive E9: a kill between the seal and the anchor leaves a state file no event references (an
        orphan); the next state of that revision replaces it, though its bytes differ (`advanced_at`). A state an event
        references is the record: other bytes for its revision are UNKNOWN_EVIDENCE."""
        owner = self.coordinator.claim("epic:e1", self.drive_token)
        state = {"kind": "epic-state", "format": 1, "revision": 1, "epic_id": "e1", "generation": owner.generation,
                 "stories": [], "advanced_at": "2026-10-18T00:00:00+00:00"}
        drive_epic.anchor_state(self.store, owner, state, "epic.started", "epic")
        path = self.base_dir / "epics" / "e1" / "state-2.json"
        drive._seal(self.base_dir, path, {**state, "revision": 2, "advanced_at": "2026-10-18T00:00:01+00:00"})  # kill
        again = {**state, "revision": 2, "advanced_at": "2026-10-18T00:00:02+00:00"}
        ref, digest = drive_epic.anchor_state(self.store, owner, again, "epic.advanced", "next")
        rows = [row for row in self.snapshot()["events"] if row["evidence_ref"] == "epics/e1/state-2.json"]
        self.assertEqual([("epics/e1/state-2.json", digest, again)],
                         [(row["evidence_ref"], row["payload_digest"], drive._sealed(self.base_dir, row))
                          for row in rows])
        self.assertEqual(("epics/e1/state-2.json", again), (ref, json.loads(path.read_text())))
        self.assertEqual(again, drive_epic.newest_state(self.store, "e1"))
        with self.assertRaises(DomainError) as caught:
            drive_epic.anchor_state(self.store, owner, {**again, "advanced_at": "2026-10-18T00:00:03+00:00"},
                                    "epic.advanced", "late")
        self.assertEqual("UNKNOWN_EVIDENCE", caught.exception.code)
        self.assertEqual(again, json.loads(path.read_text()))
        self.assertEqual(2, len([row for row in self.snapshot()["events"] if row["kind"].startswith("epic.")]))

    def test_a_summary_sealed_before_a_kill_is_replaced_by_the_next_end(self):
        """Design epic-drive E10: an unanchored summary-<n>.json (a kill between its seal and `epic.ended`) does not
        break the next run's end: it is replaced and anchored."""
        owner = self.coordinator.claim("epic:e1", self.drive_token)
        state = {"kind": "epic-state", "format": 1, "revision": 1, "epic_id": "e1", "generation": owner.generation,
                 "epic": 1, "branch": "coordinator/epic-1", "worktree": str(self.epic_worktree()),
                 "head_sha": self.base, "base_branch": None, "base_synced_sha": self.base, "stories": []}
        drive._seal(self.base_dir, self.base_dir / "epics" / "e1" / "summary-1.json",
                    {"kind": "epic-summary", "run_id": "run-001-killed"})
        ends = []
        for _ in range(2):
            epic = drive_epic.Epic(self.repo, self.store, owner, state, drive_epic.check_template(self.template()),
                                   drive.RunBounds(60, 3600), time.monotonic)
            summary, code = epic._end(drive.Outcome("needs-human", "signal-stop", {}))
            self.assertEqual((11, None), (code, summary.get("end_error")), summary)
            ends.append(summary)
        rows = [row for row in self.snapshot()["events"] if row["kind"] == "epic.ended"]
        self.assertEqual([("epics/e1/summary-1.json", ends[0]), ("epics/e1/summary-2.json", ends[1])],
                         [(row["evidence_ref"], drive._sealed(self.base_dir, row)) for row in rows])
        self.assertEqual(["run-001", "run-002"], [item["run_id"][:7] for item in ends])
        self.assertEqual("active", self.snapshot()["owner"]["state"])

    def test_a_story_roles_override_is_checked_and_merged_into_the_filled_template(self):
        """Design epic-drive E14: `stories.<key>.roles` takes timeout and max_turns per template role; the filled
        pipeline carries the merged role and a different digest."""
        over = {"1-2-farewell": {"roles": {"worker": {"timeout": 90}}}}
        value = drive_epic.check_input(self.repo, drive_epic.EpicInput(**self.epic_input(stories=over)))
        self.assertEqual(over, value["stories"])
        template = self.template()
        filled = drive_epic.fill_template(template, "f" * 32, "1-2-farewell", {"worker": {"timeout": 90}})
        self.assertEqual((90, 60), (filled["roles"]["worker"]["timeout"], filled["roles"]["reviewer"]["timeout"]))
        plain = drive_epic.fill_template(template, "f" * 32, "1-2-farewell")
        self.assertEqual((60, 1), (plain["roles"]["worker"]["timeout"], plain["mandate_revision"]))
        self.assertEqual(3, drive_epic.fill_template(template, "f" * 32, "1-2-farewell", None, 3)["mandate_revision"])
        for bad in ({"roles": {"author": {"timeout": 9}}}, {"roles": {"worker": {"model": "x"}}},
                    {"roles": {"worker": {"timeout": 0}}}, {"roles": {"worker": {"max_turns": 1001}}},
                    {"roles": {"worker": {}}}, {"roles": []}, {"roles": {"worker": {"timeout": "90"}}}):
            with self.subTest(bad=bad):
                with self.assertRaises(DomainError) as caught:
                    drive_epic.check_input(self.repo, drive_epic.EpicInput(
                        **self.epic_input(stories={"1-2-farewell": bad})))
                self.assertEqual("INVALID_INPUT", caught.exception.code)
        with self.assertRaises(DomainError) as caught:   # a role the template lacks
            drive_epic.fill_template({**template, "roles": {"worker": template["roles"]["worker"]}}, "f" * 32,
                                     "1-2-farewell", {"reviewer": {"timeout": 90}})
        self.assertEqual("INVALID_INPUT", caught.exception.code)
        with self.assertRaises(DomainError) as caught:   # ruling I2: a refusal while checking an override names it
            drive_epic._check_overrides({**template, "roles": {"worker": template["roles"]["worker"]}},
                                        {"1-2-farewell": {"roles": {"reviewer": {"timeout": 90}}}},
                                        drive.RunBounds(60, 3600))
        self.assertEqual(("INVALID_INPUT", "Story 1-2-farewell: roles override names a role the template lacks: "
                                           "reviewer"), (caught.exception.code, caught.exception.message))
        owner = self.coordinator.claim("epic:e1", self.drive_token)   # the floor: 200 s holds 60 + 120, not 90 + 120
        epic = drive_epic.Epic(self.repo, self.store, owner, {"epic_id": "e1", "stories_input": over},
                               drive_epic.check_template(template), drive.RunBounds(60, 200), time.monotonic)
        self.assertEqual((False, False, True), (epic._below_floor(), epic._below_floor({"key": "1-3-shout"}),
                                                epic._below_floor({"key": "1-2-farewell"})))

    def test_a_restart_carries_the_stops_recorded_evidence_and_the_brief_renders_it_quoted(self):
        """Design epic-drive E15 (ruling H8): the blocking items of the review a `critical-finding` stop names (the
        named pointers of an `open-high-findings` stop), the failed command names of a `correction-budget` stop,
        nothing for any other stop; under `accept_on_high` the blocking items are the critical ones; a review the store
        does not hold is BLOCKED_EVIDENCE. The brief section quotes every name (the location and the contributor too)
        and is absent without a carry."""
        finding = {"lens": "edge", "location": "pkg/farewell.py:1", "trigger_condition": "t", "guard_snippet": "g",
                   "potential_consequence": "c"}
        view = {"findings": [{**finding, "severity": "critical"}, {**finding, "severity": "low"},
                             {**finding, "severity": "high", "location": "pkg/`x`.py"}],
                "dispositions": [{"contributor_id": "story-worker", "kind": "question", "index": 0,
                                  "severity": "high"}]}
        snapshot = {"reviews": [{"id": "r1", "finding_json": json.dumps({"finding": view})}]}
        critical = {"reason": "critical-finding", "subjects": {"review_id": "r1"}}
        carry = drive_epic._restart_carry(critical, snapshot, False)
        self.assertEqual({"kind": "review", "review_id": "r1", "items": [
            {**view["findings"][0], "pointer": "/findings/0"}, {**view["findings"][2], "pointer": "/findings/2"},
            {**view["dispositions"][0], "pointer": "/dispositions/0"}]}, carry)
        self.assertEqual([{**view["findings"][0], "pointer": "/findings/0"}],   # final review M2
                         drive_epic._restart_carry(critical, snapshot, True)["items"])
        high = {"reason": "open-high-findings", "subjects": {"review_id": "r1", "finding_ids": ["/findings/2"]}}
        self.assertEqual([{**view["findings"][2], "pointer": "/findings/2"}],
                         drive_epic._restart_carry(high, snapshot, False)["items"])
        with self.assertRaises(DomainError) as caught:   # Task 4 o4: never a StopIteration
            drive_epic._restart_carry({"reason": "critical-finding", "subjects": {"review_id": "r9"}}, snapshot, False)
        self.assertEqual(("BLOCKED_EVIDENCE", "The stop names review r9, which the store does not hold"),
                         (caught.exception.code, caught.exception.message))
        budget = {"reason": "correction-budget", "subjects": {"verify_ref": "story/verify.json", "failed": ["unit"]}}
        self.assertEqual({"kind": "verify", "verify_ref": "story/verify.json", "failed": ["unit"]},
                         drive_epic._restart_carry(budget, snapshot, False))
        self.assertIsNone(drive_epic._restart_carry({"reason": "attempt-unknown", "subjects": {}}, snapshot, False))
        self.assertEqual([], impl._restart_lines({}))
        self.assertEqual(["", "## Carried from the restart", impl.RESTART_INTRO,
                          "- Review `r1` of the earlier revision:",
                          '- `/findings/0` (critical, lens `edge`) at "pkg/farewell.py:1": trigger: "t"; guard: "g"; '
                          'consequence: "c"',
                          '- `/findings/2` (high, lens `edge`) at "pkg/`x`.py": trigger: "t"; guard: "g"; '
                          'consequence: "c"',
                          '- `/dispositions/0` (high) carried item of contributor "story-worker", kind `question`, '
                          'index 0'], impl._restart_lines({"restart_carry": carry}))
        self.assertEqual(["", "## Carried from the restart", impl.RESTART_INTRO,
                          '- Failed verification commands of the earlier revision (receipt `story/verify.json`): '
                          '"unit", "lint `x`"'],
                         impl._restart_lines({"restart_carry": {"kind": "verify", "verify_ref": "story/verify.json",
                                                                "failed": ["unit", "lint `x`"]}}))


class EpicRunTests(EpicDriveCase):
    """Task 4: epic runs on fake clients through the CLI."""

    def test_three_stories_reach_done_on_the_integration_branch_and_the_epic_ends_done(self):
        code, summary, err = self.epic_start(code=0)
        self.assertEqual(("done", "epic-done", "released", []),
                         (summary["outcome"], summary["reason"], summary["ownership"], summary["subjects"]["skipped"]),
                         (summary, err))
        self.assertEqual([("1-1-greeting", "already-done"), ("1-2-farewell", "done"), ("1-3-shout", "done"),
                          ("1-4-whisper", "done")], self.states(summary))
        self.assertEqual({"runs": 3, "merges": 3, "syncs": 0, "stories_done": 3, "stories_skipped": 0,
                          "stories_restarted": 0},
                         summary["counts"])
        self.assertIsNone(summary["operator_action"]); self.assertIsNone(summary["epic_action"])
        self.assertIsNone(summary["story"])
        head = git(self.main, "rev-parse", "coordinator/epic-1")
        self.assertEqual((head, head), (summary["epic"]["head_sha"], git(self.main, "rev-parse", "story/1-4-whisper")))
        log = self.integration_log()
        self.assertEqual("story(1-4-whisper): status done|BMAD Coordinator", log[0])
        self.assertIn("story(1-2-farewell): spec ready-for-dev|BMAD Coordinator", log)
        self.assertFalse(any(line.startswith("epic(1):") for line in log))   # fast-forwards only
        sprint = git(self.main, "show", f"coordinator/epic-1:{ARTIFACTS}/sprint-status.yaml")
        for line in ("  epic-1: done", "  1-2-farewell: done", "  1-3-shout: done", "  1-4-whisper: done"):
            self.assertIn(line, sprint)
        self.assertEqual(("released", self.base),
                         (self.snapshot()["owner"]["state"], git(self.main, "rev-parse", self.base_branch)))
        leases = sorted({row["subject_id"] for row in self.snapshot()["events"] if row["kind"] == "drive.leased"})
        self.assertEqual(3, len(leases))
        for lease_id in leases:
            lease = drive._sealed(self.base_dir, next(row for row in self.snapshot()["events"] if
                                                      row["kind"] == "drive.leased" and row["subject_id"] == lease_id))
            self.assertEqual((f"epic:{summary['epic_id']}", summary["epic_id"]),
                             (lease["session_id"], lease["epic_id"]))
        checks = unattended_checks(self.t.repo, leases)
        for name in ("driver-claimed-generation", "acceptance-current", "drive-events-anchored",
                     "zero-interventions-inbox-empty"):
            self.assertEqual("pass", checks[name], name)
        for lease_id in leases:   # the story criteria read the records of one story feature per call
            checks = unattended_checks(self.t.repo, [lease_id])
            for name in ("status-synced", "attempt-committed", "verify-observed"):
                self.assertEqual("pass", checks[name], (lease_id, name))
        brief = self.brief_of("1-3-shout", "# BMAD story spec 1-3-shout")
        self.assertIn("- DW-1 — the decision on the exclamation mark\n  - `status: open`\n"
                      "  - `decision: 2026-10-01 one — one exclamation mark`", brief)
        self.assertIn("DW-1 met. A decision that does not settle", brief)
        self.assertIn("## Preconditions\n- none\n", self.brief_of("1-2-farewell", "# BMAD story spec 1-2-farewell"))
        for key in ("1-2-farewell", "1-3-shout", "1-4-whisper"):
            self.assertTrue((self.worktrees / f"story-{key}" / "pkg").is_dir(), key)
        states = sorted(self.base_dir.joinpath("epics", summary["epic_id"]).glob("state-*.json"),
                        key=lambda path: int(path.stem.split("-")[1]))
        self.assertGreaterEqual(len(states), 12)   # list, add, lease, run, merge per story at least
        self.assertEqual(self.states(summary),
                         [(item["key"], item["state"]) for item in json.loads(states[-1].read_bytes())["stories"]])
        self.assertTrue((self.base_dir / "epics" / summary["epic_id"] / "summary-1.json").is_file())

    def test_a_story_stop_passes_through_with_the_epic_command_and_the_accept_resumes_to_the_end(self):
        self.base = self.commit_files({f"{ARTIFACTS}/sprint-status.yaml":
                                       sprint_file(**{"1-3-shout": "done", "1-4-whisper": "done"})}, "sprint")
        self.template_file = self.write_template(spec_auto_accept=False)
        code, summary, _ = self.epic_start(code=11)
        self.assertEqual(("needs-human", "spec-decisions", "1-2-farewell", "retained"),
                         (summary["outcome"], summary["reason"], summary["story"]["key"], summary["ownership"]),
                         summary)
        self.assertEqual([], summary["subjects"]["questions"])
        self.assertIn("then epic resume", summary["operator_action"])
        self.assertNotIn("drive resume", summary["operator_action"])
        self.assertIn("epic resume --skip 1-2-farewell", summary["epic_action"])
        self.assertIn(f"epic resume --epic {summary['epic_id']}", summary["epic_action"])
        self.assertEqual(("active", f"epic:{summary['epic_id']}"),
                         (self.snapshot()["owner"]["state"], self.snapshot()["owner"]["session_id"]))
        self.assertEqual([("1-1-greeting", "already-done"), ("1-2-farewell", "current"),
                          ("1-3-shout", "already-done"), ("1-4-whisper", "already-done")], self.states(summary))
        subjects = summary["subjects"]
        self.run_cli("bmad", "accept", "--token-file", str(self.drive_token),
                     data={"bundle_id": subjects["bundle_id"], "review_id": subjects["review_id"]}, code=0)
        code, summary, _ = self.epic_resume(code=0)
        self.assertEqual(("done", "epic-done",
                          {"runs": 1, "merges": 1, "syncs": 0, "stories_done": 1, "stories_skipped": 0,
                           "stories_restarted": 0}),
                         (summary["outcome"], summary["reason"], summary["counts"]), summary)
        self.assertEqual([("1-1-greeting", "already-done"), ("1-2-farewell", "done"), ("1-3-shout", "already-done"),
                          ("1-4-whisper", "already-done")], self.states(summary))
        self.assertEqual(2, len([row for row in self.snapshot()["events"] if row["kind"] == "epic.ended"]))
        self.assertEqual(2, len([row for row in self.snapshot()["events"]
                                 if row["kind"] == "drive.started"]))   # one lease, two runs
        self.assertEqual(1, len({row["subject_id"] for row in self.snapshot()["events"]
                                 if row["kind"] == "drive.leased"}))

    def test_an_unmet_precondition_stops_before_the_story_and_a_decision_on_the_base_branch_is_synced_at_resume(self):
        self.base = self.commit_files({f"{ARTIFACTS}/sprint-status.yaml": sprint_file(**{"1-4-whisper": "done"}),
                                       f"{ARTIFACTS}/deferred-work.md": LEDGER_OPEN}, "open")
        code, summary, _ = self.epic_start(code=11)
        self.assertEqual(("needs-human", "precondition-unmet"), (summary["outcome"], summary["reason"]), summary)
        self.assertEqual(("1-3-shout", "DW-1", "DW-1 — the decision on the exclamation mark",
                          "a keep-open decision does not settle"),
                         (summary["subjects"]["key"], summary["subjects"]["entry"], summary["subjects"]["bullet"],
                          summary["subjects"]["detail"]))
        self.assertIn("epic resume --skip 1-3-shout", summary["operator_action"])
        self.assertIn("DW-1", summary["operator_action"])
        self.assertEqual([("1-1-greeting", "already-done"), ("1-2-farewell", "done"), ("1-3-shout", "current"),
                          ("1-4-whisper", "already-done")], self.states(summary))
        self.assertIsNone(summary["story"]["feature_id"])
        self.assertEqual(1, len({row["subject_id"] for row in self.snapshot()["events"]
                                 if row["kind"] == "drive.leased"}))
        self.assertEqual(4, len(self.launch_log()))   # story 1.2 only: spec, spec review, implement, code review
        decided = self.commit_files({f"{ARTIFACTS}/deferred-work.md": LEDGER_FILE}, "decide DW-1")   # the base branch
        code, summary, _ = self.epic_resume(code=0)
        self.assertEqual(("done", "epic-done",
                          {"runs": 1, "merges": 1, "syncs": 1, "stories_done": 1, "stories_skipped": 0,
                           "stories_restarted": 0}),
                         (summary["outcome"], summary["reason"], summary["counts"]), summary)
        self.assertEqual(decided, summary["epic"]["base_synced_sha"])
        log = self.integration_log()
        self.assertIn(f"epic(1): sync {self.base_branch} at {decided[:12]}|BMAD Coordinator",
                      log)   # the base moved after 1.2: a merge
        brief = self.brief_of("1-3-shout", "# BMAD story spec 1-3-shout")
        self.assertIn("- `decision: 2026-10-01 one — one exclamation mark`", brief)
        self.assertIn("DW-1 met", brief)
        git(self.main, "merge-base", "--is-ancestor", decided,
            "coordinator/epic-1")   # raises unless the decision reached the branch

    def test_a_story_outside_backlog_stops_story_status_before_any_launch_and_skip_leaves_it_out(self):
        self.base = self.commit_files({f"{ARTIFACTS}/sprint-status.yaml":
                                       sprint_file(**{"1-2-farewell": "in-progress", "1-4-whisper": "done"})}, "sprint")
        code, summary, _ = self.epic_start(code=11)
        self.assertEqual(("needs-human", "story-status", "1-2-farewell", "1.2"),
                         (summary["outcome"], summary["reason"], summary["subjects"]["key"],
                          summary["subjects"]["number"]))
        self.assertIn("is 'in-progress' in sprint-status, not backlog or done", summary["subjects"]["detail"])
        self.assertIn("epic resume --skip 1-2-farewell", summary["operator_action"])
        self.assertEqual([], self.launch_log())
        self.assertEqual([("1-1-greeting", "already-done"), ("1-2-farewell", "blocked"), ("1-3-shout", "pending"),
                          ("1-4-whisper", "already-done")], self.states(summary))
        code, _, error = self.epic_resume("--skip", "1-3-shout", code=2)
        self.assertEqual("skip-refused", error["reason"])
        code, summary, _ = self.epic_resume("--skip", "1-2-farewell", code=0)
        self.assertEqual(("done", "epic-done", ["1-2-farewell"]),
                         (summary["outcome"], summary["reason"], summary["subjects"]["skipped"]))
        self.assertEqual([("1-1-greeting", "already-done"), ("1-2-farewell", "skipped"), ("1-3-shout", "done"),
                          ("1-4-whisper", "already-done")], self.states(summary))
        self.assertEqual({"runs": 1, "merges": 1, "syncs": 0, "stories_done": 1, "stories_skipped": 1,
                          "stories_restarted": 0},
                         summary["counts"])
        self.assertFalse((self.worktrees / "story-1-2-farewell").exists())

    def test_a_deadline_below_the_floor_at_a_boundary_ends_continue_before_the_next_story(self):
        self.base = self.commit_files({f"{ARTIFACTS}/sprint-status.yaml": sprint_file(**{"1-4-whisper": "done"})},
                                      "sprint")
        slow = {"files": [["pkg/farewell.py", FAREWELL], ["tests/test_farewell.py", FAREWELL_TEST]],
                "block": self.block_file("slow")}   # never released: story 1.2's implementer waits its 30 s
        self.scenario(stories=self.story_scenarios(**{"1-2-farewell": {"impl-1": slow}}))
        code, summary, _ = self.run_cli(*self.epic_args("--deadline", "185"), data=self.epic_input(), code=10,
                                        timeout=900)   # the last --deadline wins
        self.assertEqual(("continue", "deadline"), (summary["outcome"], summary["reason"]), summary)
        self.assertEqual("epic resume", summary["operator_action"])
        # story 1.2 used over 30 s of the 185: under the 180 s floor (role timeout plus MARGIN) 1.3 did not start
        self.assertEqual("1-3-shout", summary["story"]["key"])
        self.assertEqual([("1-1-greeting", "already-done"), ("1-2-farewell", "done"), ("1-3-shout", "current"),
                          ("1-4-whisper", "already-done")], self.states(summary))
        self.assertEqual(4, len(self.launch_log()))
        self.assertIsNone(summary["story"]["run_id"])
        self.assertEqual((None, None), (summary["story"]["feature_id"], summary["story"]["lease_id"]))   # no add
        self.assertIsNone(drive_epic._rev(self.repo.worktree, "refs/heads/story/1-3-shout"))
        code, summary, _ = self.epic_resume("--deadline", "3600", code=0)
        self.assertEqual(("done", "epic-done"), (summary["outcome"], summary["reason"]))
        self.assertEqual([("1-1-greeting", "already-done"), ("1-2-farewell", "done"), ("1-3-shout", "done"),
                          ("1-4-whisper", "already-done")], self.states(summary))

    def test_a_story_with_its_own_worker_box_runs_on_the_merged_pipeline_and_the_others_on_the_template(self):
        """Design epic-drive E14: the override reaches the story's sealed pipeline only."""
        over = {"1-3-shout": {"roles": {"worker": {"timeout": 90}}}}
        code, summary, _ = self.epic_start(data=self.epic_input(stories=over), code=0)
        self.assertEqual(("done", "epic-done"), (summary["outcome"], summary["reason"]), summary)
        timeouts = {}
        for item in drive_epic.newest_state(self.store, summary["epic_id"])["stories"]:
            if item.get("lease_id"):
                lease = json.loads((self.base_dir / "drives" / item["lease_id"] / "lease.json").read_bytes())
                pipeline = drive_pipeline.load(self.base_dir, lease["pipeline_sha256"])
                timeouts[item["key"]] = pipeline["roles"]["worker"]["timeout"]
        self.assertEqual({"1-2-farewell": 60, "1-3-shout": 90, "1-4-whisper": 60}, timeouts)


class EpicStopTests(EpicDriveCase):
    """Task 5: the integration branch under change, conflicts, drift, kills, the re-claim after a release, refusals."""

    ONE = {"1-3-shout": "done", "1-4-whisper": "done"}   # only story 1.2 pending

    def one_story(self, **more):
        self.base = self.commit_files({f"{ARTIFACTS}/sprint-status.yaml": sprint_file(**{**self.ONE, **more})},
                                      "sprint")

    def paused(self, point: str):
        child = self.child_driver(self.epic_args(), pause_at=point, stdin=json.dumps(self.epic_input()))
        name = point.partition("@")[0].partition(":")[0]
        self.wait_for(lambda: any(item["point"] == name for item in self.hook_log()), 600, point)
        return child

    def release_pause(self):
        self.pause_release.write_text("go\n")

    def hand_merge(self, ref: str) -> str:
        """The operator resolves a conflict in the epic worktree, taking the merged side's version of every file."""
        worktree = self.epic_worktree()
        subprocess.run(["git", "-C", str(worktree), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                        "merge", ref], capture_output=True)   # conflicts: exit 1; the identity: a bare runner has none
        conflicted = git(worktree, "diff", "--name-only", "--diff-filter=U").split()
        for path in conflicted:
            git(worktree, "checkout", "--theirs", "--", path)
            git(worktree, "add", "--", path)
        git(worktree, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm",
            f"hand merge of {ref}")
        return git(worktree, "rev-parse", "HEAD")

    def story_record(self, epic_id: str, key: str) -> dict:
        """The story's record in the newest sealed, anchored epic state."""
        return next(item for item in drive_epic.newest_state(self.store, epic_id)["stories"] if item["key"] == key)

    def test_a_commit_on_the_integration_branch_during_a_story_makes_the_integration_a_merge_commit(self):
        self.one_story()
        child = self.paused("after-story-add@key=1-2-farewell")
        moved = self.commit_files({"docs/note.md": "an operator note\n"}, "operator note", repo=self.epic_worktree())
        self.release_pause()
        code, summary, err = self.finish(child, timeout=900)
        self.assertEqual((0, "done", "epic-done", 1),
                         (code, summary["outcome"], summary["reason"], summary["counts"]["merges"]), (summary, err))
        head = summary["epic"]["head_sha"]
        shown = git(self.main, "log", "-1", "--format=%s|%an|%P", head).split("|")
        self.assertEqual(["epic(1): merge story/1-2-farewell", "BMAD Coordinator"], shown[:2])
        self.assertEqual({moved, git(self.main, "rev-parse", "story/1-2-farewell")}, set(shown[2].split()))
        self.assertEqual(head, git(self.main, "rev-parse", "coordinator/epic-1"))
        self.assertEqual([("1-1-greeting", "already-done"), ("1-2-farewell", "done"), ("1-3-shout", "already-done"),
                          ("1-4-whisper", "already-done")], self.states(summary))

    def test_a_conflicting_integration_stops_and_the_operators_hand_merge_is_recorded_at_resume(self):
        self.one_story()
        child = self.paused("after-story-add@key=1-2-farewell")
        operator = self.commit_files({"pkg/farewell.py": "def farewell(n):\n    return 'Bye'\n"}, "conflicting",
                                     repo=self.epic_worktree())
        self.release_pause()
        code, summary, err = self.finish(child, timeout=900)
        self.assertEqual((11, "needs-human", "integration-conflict", "story", "refs/heads/story/1-2-farewell"),
                         (code, summary["outcome"], summary["reason"], summary["subjects"]["merge"],
                          summary["subjects"]["ref"]), (summary, err))
        self.assertIn("conflict", summary["subjects"]["detail"].lower())
        self.assertEqual("", git(self.epic_worktree(), "status", "--porcelain"))
        self.assertIn(f"git -C {self.epic_worktree()} merge refs/heads/story/1-2-farewell", summary["operator_action"])
        self.assertEqual(("1-2-farewell", "done"), (summary["story"]["key"], summary["story"]["outcome"]))
        self.assertIn(("1-2-farewell", "current"), self.states(summary))
        record = self.story_record(summary["epic_id"], "1-2-farewell")
        self.assertEqual(("current", "done"), (record["state"], record["outcome"]))
        self.assertEqual(operator, git(self.main, "rev-parse", "coordinator/epic-1"))   # the merge was aborted
        merged = self.hand_merge("story/1-2-farewell")
        code, summary, _ = self.epic_resume(code=0)
        self.assertEqual(("done", "epic-done", 0, merged),
                         (summary["outcome"], summary["reason"], summary["counts"]["merges"],
                          summary["epic"]["head_sha"]), summary)
        self.assertEqual(merged, next(item["head_sha"] for item in summary["epic"]["stories"]
                                      if item["key"] == "1-2-farewell"))
        self.assertEqual("", git(self.main, "merge-base", "--is-ancestor", "story/1-2-farewell", "coordinator/epic-1"))

    def test_a_base_branch_change_that_conflicts_stops_integration_conflict_at_the_boundary(self):
        self.base = self.commit_files({f"{ARTIFACTS}/sprint-status.yaml": sprint_file(**{"1-4-whisper": "done"})},
                                      "sprint")
        child = self.paused("after-epic-merge@key=1-2-farewell")
        # the base branch: another farewell that conflicts with story 1.2's yet keeps its test green, so the hand merge
        # taking the base's side leaves story 1.3 a passing baseline
        farewell = "def farewell(n):\n    return 'Goodbye, %s!' % (n or 'friend')\n"
        conflicting = self.commit_files({"pkg/farewell.py": farewell}, "base change")
        self.release_pause()
        code, summary, err = self.finish(child, timeout=900)
        self.assertEqual((11, "needs-human", "integration-conflict", "base", f"refs/heads/{self.base_branch}",
                          conflicting),
                         (code, summary["outcome"], summary["reason"], summary["subjects"]["merge"],
                          summary["subjects"]["ref"], summary["subjects"]["sha"]), (summary, err))
        self.assertEqual([("1-1-greeting", "already-done"), ("1-2-farewell", "done"), ("1-3-shout", "pending"),
                          ("1-4-whisper", "already-done")], self.states(summary))
        self.assertIsNone(summary["story"])
        self.assertEqual("", git(self.epic_worktree(), "status", "--porcelain"))
        self.assertEqual(git(self.main, "rev-parse", "story/1-2-farewell"), git(self.main, "rev-parse",
                                                                                 "coordinator/epic-1"))
        self.assertEqual(self.base, drive_epic.newest_state(self.store, summary["epic_id"])["base_synced_sha"])
        self.hand_merge(self.base_branch)
        code, summary, _ = self.epic_resume(code=0)
        self.assertEqual(("done", "epic-done", conflicting),
                         (summary["outcome"], summary["reason"], summary["epic"]["base_synced_sha"]), summary)
        self.assertEqual([("1-1-greeting", "already-done"), ("1-2-farewell", "done"), ("1-3-shout", "done"),
                          ("1-4-whisper", "already-done")], self.states(summary))
        git(self.main, "merge-base", "--is-ancestor", conflicting, "coordinator/epic-1")   # raises unless merged
        self.assertEqual(farewell.strip(), git(self.main, "show", "coordinator/epic-1:pkg/farewell.py"))

    def test_a_detached_base_is_not_tracked(self):
        self.one_story()
        extra = self.commit_files({"docs/extra.md": "later\n"}, "later on the branch")
        code, summary, _ = self.epic_start(data=self.epic_input(base=self.base), code=0)
        self.assertEqual(("epic-done", None, 0),
                         (summary["reason"], summary["epic"]["base_branch"], summary["counts"]["syncs"]), summary)
        self.assertNotEqual(0, subprocess.run(["git", "-C", str(self.main), "merge-base", "--is-ancestor", extra,
                                               "coordinator/epic-1"],
                                              capture_output=True).returncode)   # the later base commit never came
        self.assertEqual(git(self.main, "rev-parse", "story/1-2-farewell"), git(self.main, "rev-parse",
                                                                                 "coordinator/epic-1"))

    def test_a_dirty_epic_worktree_stops_worktree_drift_before_the_integration_and_resumes_after_a_cleanup(self):
        self.one_story()
        child = self.paused("after-story-add@key=1-2-farewell")
        (self.epic_worktree() / "docs" / "house-rules.md").write_text("edited by hand\n")
        self.release_pause()
        code, summary, err = self.finish(child, timeout=900)
        self.assertEqual((11, "needs-human", "worktree-drift", str(self.epic_worktree()), "coordinator/epic-1"),
                         (code, summary["outcome"], summary["reason"], summary["subjects"]["worktree"],
                          summary["subjects"]["branch"]), (summary, err))
        self.assertIn("uncommitted changes", summary["subjects"]["detail"])
        self.assertIn(f"restore the epic worktree {self.epic_worktree()}", summary["operator_action"])
        self.assertEqual(self.base, git(self.main, "rev-parse", "coordinator/epic-1"))   # nothing merged
        record = self.story_record(summary["epic_id"], "1-2-farewell")
        self.assertEqual(("current", "done"), (record["state"], record["outcome"]))
        git(self.epic_worktree(), "checkout", "--", ".")
        code, summary, _ = self.epic_resume(code=0)
        self.assertEqual(("done", "epic-done", 1), (summary["outcome"], summary["reason"], summary["counts"]["merges"]))
        self.assertEqual(git(self.main, "rev-parse", "story/1-2-farewell"), git(self.main, "rev-parse",
                                                                                 "coordinator/epic-1"))

    def test_an_existing_story_branch_fails_story_add_and_keeps_the_session(self):
        self.one_story()
        git(self.main, "branch", "story/1-2-farewell", self.base)
        code, summary, _ = self.epic_start(code=12)
        self.assertEqual(("failed", "story-add", "1-2-farewell", "INVALID_INPUT", "unchanged"),
                         (summary["outcome"], summary["reason"], summary["subjects"]["key"],
                          summary["subjects"]["gate"], summary["ownership"]), summary)
        self.assertIn("Branch story/1-2-farewell exists", summary["subjects"]["detail"])
        self.assertEqual("remove story/1-2-farewell and its worktree (or epic resume --skip 1-2-farewell), then epic "
                         "resume --token-file <driver token>", summary["operator_action"])
        self.assertTrue(summary["epic_action"].startswith("epic resume continues story 1-2-farewell"))
        self.assertEqual("active", self.snapshot()["owner"]["state"])
        self.assertEqual([], [row for row in self.snapshot()["features"] if row["branch"] == "story/1-2-farewell"])
        self.assertEqual(self.base, git(self.main, "rev-parse", "story/1-2-farewell"))
        self.assertFalse((self.worktrees / "story-1-2-farewell").exists())
        record = self.story_record(summary["epic_id"], "1-2-farewell")
        self.assertEqual(("current", None), (record["state"], record.get("feature_id")))

    def test_refusals_write_nothing(self):
        git(self.main, "branch", "coordinator/epic-1", self.base)
        code, _, error = self.epic_start(code=2)
        self.assertEqual("branch-exists", error["reason"])
        git(self.main, "branch", "-D", "coordinator/epic-1")
        self.epic_worktree().mkdir(parents=True)
        code, _, error = self.epic_start(code=2)
        self.assertEqual("worktree-exists", error["reason"])
        self.epic_worktree().rmdir()
        bad = self.root / "bad-template.json"
        bad.write_text(json.dumps(self.template(steps=self.template()["steps"][5:])))
        code, _, error = self.epic_start(template=bad, code=2)
        self.assertIn("spec step", error["message"])
        code, _, error = self.epic_start(data={"epics_path": EPICS_PATH, "epic": 1}, code=2)
        self.assertEqual("INVALID_INPUT", error["error"])
        code, _, error = self.epic_start("--deadline", "100", code=2)
        self.assertEqual("bounds-insufficient", error["reason"])
        attended = self.coordinator.claim("attended", self.root / "attended.token")
        code, _, error = self.epic_start(code=3)
        self.assertEqual("OWNERSHIP_CONFLICT", error["error"])
        self.coordinator.release(attended)
        self.assertEqual(("released", None), (self.snapshot()["owner"]["state"],
                                              drive_epic._rev(self.repo.worktree, "refs/heads/coordinator/epic-1")))
        self.assertFalse(self.epic_worktree().exists())
        self.assertFalse(self.drive_token.exists())
        self.assertEqual([], [row for row in self.snapshot()["events"] if row["kind"].startswith("epic.")])
        code, _, error = self.epic_resume(code=2)   # no session, no token file, no --epic
        self.assertEqual(("INVALID_INPUT", "run-unbound"), (error["error"], error["reason"]))

    def killed(self, kill_at: str) -> dict:
        self.one_story()
        child = self.child_driver(self.epic_args(), kill_at=kill_at, stdin=json.dumps(self.epic_input()))
        self.assertEqual(-signal.SIGKILL, self.finish(child, timeout=900)[0])
        self.assertEqual(kill_at.partition("@")[0].partition(":")[0], self.hook_log()[-1]["point"])   # killed there
        code, summary, _ = self.epic_resume(code=0)
        self.assertEqual(("done", "epic-done", [("1-1-greeting", "already-done"), ("1-2-farewell", "done"),
                                                ("1-3-shout", "already-done"), ("1-4-whisper", "already-done")]),
                         (summary["outcome"], summary["reason"], self.states(summary)), summary)
        self.assertEqual(1, len({row["subject_id"] for row in self.snapshot()["events"]
                                 if row["kind"] == "drive.leased"}))
        self.assertEqual(1, len([row for row in self.snapshot()["features"] if row["branch"] == "story/1-2-farewell"]))
        self.assertEqual(git(self.main, "rev-parse", "story/1-2-farewell"), git(self.main, "rev-parse",
                                                                                 "coordinator/epic-1"))
        return summary

    def test_kill_after_story_add_resumes_by_adopting_the_feature(self):
        summary = self.killed("after-story-add@key=1-2-farewell")
        self.assertEqual(1, summary["counts"]["merges"])

    def test_kill_after_the_merge_resumes_by_recording_it(self):
        summary = self.killed("after-epic-merge@key=1-2-farewell")
        self.assertEqual(0, summary["counts"]["merges"])   # found already integrated
        self.assertEqual(summary["epic"]["head_sha"], next(item["head_sha"] for item in summary["epic"]["stories"]
                                                           if item["key"] == "1-2-farewell"))

    def test_kill_after_the_story_add_state_resumes_the_loop_without_a_second_feature(self):
        summary = self.killed("after-epic-advance:3")   # 1 started, 2 listed, 3 the story added and recorded
        self.assertEqual(1, summary["counts"]["merges"])
        self.assertEqual((3, "1-2-farewell"), (self.hook_log()[-1]["revision"], self.hook_log()[-1]["source"]))
        self.assertEqual(["after-story-add"], [item["point"] for item in self.hook_log()
                                               if item["point"] == "after-story-add"])   # the add came before

    def test_kill_after_story_add_and_a_base_move_resumes_by_adopting_the_story_branch(self):
        self.one_story()
        child = self.child_driver(self.epic_args(), kill_at="after-story-add@key=1-2-farewell",
                                  stdin=json.dumps(self.epic_input()))
        self.assertEqual(-signal.SIGKILL, self.finish(child, timeout=900)[0])
        self.assertEqual("after-story-add", self.hook_log()[-1]["point"])   # the feature exists, its record not yet
        moved = self.commit_files({"docs/base-note.md": "a base note\n"}, "base moved")   # the base branch
        code, summary, _ = self.epic_resume(code=0)
        self.assertEqual(("done", "epic-done",
                          {"runs": 1, "merges": 1, "syncs": 1, "stories_done": 1, "stories_skipped": 0,
                           "stories_restarted": 0}),
                         (summary["outcome"], summary["reason"], summary["counts"]), summary)
        self.assertEqual([("1-1-greeting", "already-done"), ("1-2-farewell", "done"), ("1-3-shout", "already-done"),
                          ("1-4-whisper", "already-done")], self.states(summary))
        features = [row for row in self.snapshot()["features"] if row["branch"] == "story/1-2-farewell"]
        self.assertEqual(1, len(features))
        record = self.story_record(summary["epic_id"], "1-2-farewell")
        self.assertEqual((features[0]["id"], self.base), (record["feature_id"], record["base_sha"]))   # adopted
        self.assertEqual(1, len({row["subject_id"] for row in self.snapshot()["events"]
                                 if row["kind"] == "drive.leased"}))
        ended = [drive._sealed(self.base_dir, row) for row in self.snapshot()["events"] if row["kind"] == "epic.ended"]
        self.assertEqual([("done", "epic-done")], [(item["outcome"], item["reason"]) for item in ended])
        # the adoption syncs nothing; the base is merged at the next boundary, before the listing that ends the epic
        self.assertEqual(moved, summary["epic"]["base_synced_sha"])
        self.assertEqual([f"epic(1): sync {self.base_branch} at {moved[:12]}|BMAD Coordinator"],
                         self.integration_log("-1"))
        self.assertEqual(summary["epic"]["head_sha"], git(self.main, "rev-parse", "coordinator/epic-1"))
        git(self.main, "merge-base", "--is-ancestor", moved, "coordinator/epic-1")   # raises unless merged
        git(self.main, "merge-base", "--is-ancestor", "story/1-2-farewell", "coordinator/epic-1")

    def test_after_a_release_epic_resume_with_the_epic_id_and_a_fresh_token_continues(self):
        self.one_story()
        self.template_file = self.write_template(spec_auto_accept=False)
        code, summary, _ = self.epic_start(code=11)
        epic_id, first = summary["epic_id"], summary["generation"]
        self.assertEqual("spec-decisions", summary["reason"])
        self.run_cli("release", "--token-file", str(self.drive_token), code=0)
        code, _, error = self.epic_resume(code=3)   # the old token: no longer the owner
        self.assertEqual("OWNERSHIP_CONFLICT", error["error"])
        fresh = self.root / "fresh.token"
        code, summary, _ = self.epic_resume("--epic", epic_id, token=fresh, code=11)
        self.assertEqual(("spec-decisions", first + 1, epic_id),
                         (summary["reason"], summary["generation"], summary["epic_id"]), summary)
        lease_id = summary["story"]["lease_id"]
        rows = [row for row in self.snapshot()["events"]
                if row["kind"] == "drive.leased" and row["subject_id"] == lease_id]
        self.assertEqual(2, len(rows))
        self.assertEqual(first + 1, drive._sealed(self.base_dir, rows[-1])["generation"])
        self.assertEqual(first + 1, drive_epic.newest_state(self.store, epic_id)["generation"])
        subjects = summary["subjects"]
        self.run_cli("bmad", "accept", "--token-file", str(fresh),
                     data={"bundle_id": subjects["bundle_id"], "review_id": subjects["review_id"]}, code=0)
        code, summary, _ = self.epic_resume(token=fresh, code=0)
        self.assertEqual(("done", "epic-done", first + 1),
                         (summary["outcome"], summary["reason"], summary["generation"]))
        self.assertEqual("pass", unattended_checks(self.t.repo, [lease_id])["driver-claimed-generation"])
        code, _, error = self.epic_resume("--epic", "nope", token=self.root / "another.token", code=2)
        self.assertEqual("run-unbound", error["reason"])

    def test_skip_of_a_story_with_a_feature_is_allowed_after_a_needs_human_stop_and_merges_nothing(self):
        self.one_story()
        self.template_file = self.write_template(spec_auto_accept=False)
        code, summary, _ = self.epic_start(code=11)
        self.assertEqual(("needs-human", "spec-decisions"), (summary["outcome"], summary["reason"]), summary)
        feature_id = summary["story"]["feature_id"]
        self.assertIsNotNone(feature_id)
        self.assertIsNone(drive_pipeline.fence_holder(self.snapshot(), feature_id))
        code, summary, _ = self.epic_resume("--skip", "1-2-farewell", code=0)
        self.assertEqual(("done", "epic-done", ["1-2-farewell"],
                          {"runs": 0, "merges": 0, "syncs": 0, "stories_done": 0, "stories_skipped": 1,
                           "stories_restarted": 0}),
                         (summary["outcome"], summary["reason"], summary["subjects"]["skipped"], summary["counts"]),
                         summary)
        self.assertEqual([("1-1-greeting", "already-done"), ("1-2-farewell", "skipped"), ("1-3-shout", "already-done"),
                          ("1-4-whisper", "already-done")], self.states(summary))
        record = self.story_record(summary["epic_id"], "1-2-farewell")
        self.assertEqual(("skipped", "needs-human", "spec-decisions", feature_id),
                         (record["state"], record["outcome"], record["reason"], record["feature_id"]))
        self.assertEqual((self.base, self.base), (summary["epic"]["head_sha"],
                                                  git(self.main, "rev-parse", "coordinator/epic-1")))
        self.assertIsNotNone(drive_epic._rev(self.repo.worktree, "refs/heads/story/1-2-farewell"))
        self.assertTrue((self.worktrees / "story-1-2-farewell").is_dir())
        self.assertEqual(1, len([row for row in self.snapshot()["features"] if row["branch"] == "story/1-2-farewell"]))

    def test_skip_is_refused_while_an_attempt_holds_the_fence(self):
        self.one_story()
        self.scenario(stories=self.story_scenarios(**{"1-2-farewell": {"impl-1": {"block": self.block_file(), "files": [
            ["pkg/farewell.py", FAREWELL], ["tests/test_farewell.py", FAREWELL_TEST]]}}}))
        child = self.child_driver(self.epic_args(), stdin=json.dumps(self.epic_input()))
        launches = self.wait_launches(3, timeout=600)   # the spec author, the spec reviewer, the blocked implementer
        child.kill()
        child.wait()
        code, _, error = self.epic_resume("--skip", "1-2-farewell", code=2)   # the run never ended
        self.assertEqual("skip-refused", error["reason"])
        code, summary, _ = self.epic_resume(code=11)   # the story run resumes, reconciles the live attempt and stops
        self.assertEqual(("needs-human", "attempt-unknown"), (summary["outcome"], summary["reason"]), summary)
        code, _, error = self.epic_resume("--skip", "1-2-farewell", code=4)
        self.assertEqual(("BLOCKED_EVIDENCE", "skip-refused"), (error["error"], error["reason"]))
        self.assertIn("holds the fence", error["message"])
        self.assertEqual("current", self.story_record(summary["epic_id"], "1-2-farewell")["state"])
        self.unblock()
        kill_group(launches[-1]["pid"])

    def test_a_roles_override_is_validated_at_start_before_anything_is_created(self):
        """Design epic-drive E14, review focus 4: a box the deadline cannot hold, max_turns for a codex worker or a
        role timeout out of range are refused before the branch or a worktree exists."""
        cases = [(["--deadline", "200"], {"1-2-farewell": {"roles": {"worker": {"timeout": 100}}}},
                  "bounds-insufficient", "Deadline must be at least", 2),
                 ([], {"1-2-farewell": {"roles": {"worker": {"max_turns": 5}}}}, None, "takes no max_turns", 2),
                 ([], {"1-2-farewell": {"roles": {"reviewer": {"timeout": 0}}}}, None, "roles.reviewer timeout", 2)]
        for extra, stories, reason, fragment, code in cases:
            with self.subTest(stories=stories):
                exit_code, _, error = self.epic_start(*extra, data=self.epic_input(stories=stories), code=code)
                self.assertEqual("INVALID_INPUT", error["error"])
                self.assertIn("1-2-farewell", error["message"])
                self.assertIn(fragment, error["message"])
                if reason:
                    self.assertEqual(reason, error["reason"])
                self.assertIsNone(drive_epic._rev(self.repo.worktree, "refs/heads/coordinator/epic-1"))
                self.assertFalse(self.worktrees.exists())

    def test_a_resume_checks_only_the_overrides_of_stories_still_to_run(self):
        """Ruling H6: an overridden story that reached done does not refuse a resume whose deadline is below its box."""
        self.base = self.commit_files({f"{ARTIFACTS}/sprint-status.yaml": sprint_file(**{"1-4-whisper": "done"})},
                                      "sprint")
        data = self.epic_input(stories={"1-2-farewell": {"roles": {"worker": {"timeout": 1000}}}})
        child = self.child_driver(self.epic_args(), kill_at="after-story-add@key=1-3-shout", stdin=json.dumps(data))
        self.assertEqual(-signal.SIGKILL, self.finish(child, timeout=900)[0])
        code, summary, _ = self.epic_resume("--deadline", "1000", code=0)   # 1000 s < 1000 + MARGIN
        self.assertEqual(("done", "epic-done"), (summary["outcome"], summary["reason"]), summary)
        self.assertEqual([("1-1-greeting", "already-done"), ("1-2-farewell", "done"), ("1-3-shout", "done"),
                          ("1-4-whisper", "already-done")], self.states(summary))

    def test_a_skip_of_an_overridden_story_is_not_refused_for_its_own_box(self):
        """Ruling H6: the story `--skip` names counts as finished; without the skip its box still refuses the resume."""
        self.one_story()
        self.template_file = self.write_template(spec_auto_accept=False)
        data = self.epic_input(stories={"1-2-farewell": {"roles": {"worker": {"timeout": 1000}}}})
        code, summary, _ = self.epic_start(data=data, code=11)
        self.assertEqual(("needs-human", "spec-decisions"), (summary["outcome"], summary["reason"]), summary)
        code, _, error = self.epic_resume("--deadline", "1000", code=2)
        self.assertEqual(("bounds-insufficient", True), (error["reason"], "Story 1-2-farewell" in error["message"]))
        code, summary, _ = self.epic_resume("--skip", "1-2-farewell", "--deadline", "1000", code=0)
        self.assertEqual(("done", "epic-done", ["1-2-farewell"]),
                         (summary["outcome"], summary["reason"], summary["subjects"]["skipped"]), summary)

    CLAIM = "farewell drops the name"   # the critical finding's claim, carried by a restart (ruling H8)

    def restart_fixture(self, data=None):
        """Story 1.2 alone, a template with no correction budget, a critical first code review: the story stops
        `critical-finding` with its corrections 0/0 (a terminal stop inside the epic, F14). A review brief names no
        story, so the reviewer's finding goes in the scenario's `steps`."""
        self.one_story()
        self.template_file = self.write_template(budgets={"max_updates": 3, "max_judgments": 8, "max_corrections": 0})
        self.scenario(stories=self.story_scenarios(), steps={"impl-review-1": {"findings": [
            {"severity": "critical", "trigger_condition": self.CLAIM}]}})
        code, summary, _ = self.epic_start(data=data, code=11)
        self.assertEqual(("needs-human", "critical-finding", {"used": 0, "max": 0}),
                         (summary["outcome"], summary["reason"], summary["subjects"]["corrections"]), summary)
        self.assertEqual(1, summary["subjects"]["finding_counts"]["critical"])
        self.assertIn("epic resume --restart 1-2-farewell [--roles <json file>]", summary["epic_action"])
        return summary

    def restarted_scenario(self):
        """The restarted story's first attempt (a create at the new revision, on the branch as it is) changes the
        farewell; the reviewer finds nothing."""
        self.scenario(stories=self.story_scenarios(**{"1-2-farewell": {"impl-1": {"files": [
            ["pkg/farewell.py", FAREWELL + "# restarted\n"], ["tests/test_farewell.py", FAREWELL_TEST]]}}}))

    def feature_of(self, summary) -> dict:
        return next(row for row in self.snapshot()["features"] if row["id"] == summary["story"]["feature_id"])

    def restarted_brief(self, epic_id: str, step: str) -> str:
        """The first brief of `step` under the pipeline the restarted story's lease was bound with."""
        lease_id = self.story_record(epic_id, "1-2-farewell")["lease_id"]
        lease = json.loads((self.base_dir / "drives" / lease_id / "lease.json").read_bytes())
        folder = self.worktrees / "story-1-2-farewell" / "_bmad-output" / "coordinator" / "briefs"
        return (folder / f"drv-{lease['pipeline_sha256'][:12]}-{step}-1.md").read_text()

    def test_restart_after_a_terminal_stop_binds_a_new_revision_with_the_roles_file_and_reaches_done(self):
        """Design epic-drive E15: the mandate revised (`constraints.restarts`), a new lease on a pipeline at the new
        revision with the merged roles, corrections at zero, the spec chain untouched, the story merged."""
        first = self.restart_fixture()
        feature_id = first["story"]["feature_id"]
        self.assertEqual(2, self.feature_of(first)["mandate_revision"])   # the carry
        self.restarted_scenario()
        roles = self.root / "roles.json"
        roles.write_text("[]")
        code, _, error = self.epic_resume("--restart", "1-2-farewell", "--roles", str(roles), code=2)
        self.assertEqual(("INVALID_INPUT", "Roles file must be one readable JSON object"),
                         (error["error"], error["message"]))
        roles.write_text(json.dumps({"worker": {"timeout": 1000}}))   # the new box is checked like a start's
        code, _, error = self.epic_resume("--restart", "1-2-farewell", "--roles", str(roles), "--deadline", "1000",
                                          code=2)
        self.assertEqual(("bounds-insufficient", True), (error["reason"], "Story 1-2-farewell" in error["message"]))
        self.assertNotIn("restarts", self.story_record(first["epic_id"], "1-2-farewell"))   # refused, nothing recorded
        roles.write_text(json.dumps({"worker": {"timeout": 90}}))
        code, summary, _ = self.epic_resume("--restart", "1-2-farewell", "--roles", str(roles), code=0)
        self.assertEqual(("done", "epic-done", {"runs": 1, "merges": 1, "syncs": 0, "stories_done": 1,
                                                "stories_skipped": 0, "stories_restarted": 1}),
                         (summary["outcome"], summary["reason"], summary["counts"]), summary)
        feature = self.feature_of(first)
        constraints = json.loads(feature["constraints_json"])
        self.assertEqual((3, 1, "review", first["subjects"]["review_id"]),
                         (feature["mandate_revision"], constraints["restarts"], constraints["restart_carry"]["kind"],
                          constraints["restart_carry"]["review_id"]))
        brief = self.restarted_brief(summary["epic_id"], "impl")   # ruling H8: the stop's evidence, quoted
        self.assertIn("## Carried from the restart\n" + impl.RESTART_INTRO + "\n- Review "
                      f"`{first['subjects']['review_id']}` of the earlier revision:\n- `/findings/0` (critical", brief)
        self.assertIn(f"trigger: {json.dumps(self.CLAIM)}", brief)
        self.assertLess(brief.index("## Carried from the restart"), brief.index("## Verification"))
        decisions = [row for row in self.snapshot()["decisions"] if row["feature_id"] == feature_id and
                     row["source_ref"] == "epic-restart:1"]
        self.assertEqual([("operator-reported", 2, {"restart": 1})],
                         [(row["source_kind"], row["mandate_revision"], json.loads(row["choice_json"]))
                          for row in decisions])
        leases = {row["subject_id"] for row in self.snapshot()["events"]
                  if row["kind"] == "drive.leased" and row["source_id"] == feature_id}
        self.assertEqual(2, len(leases))
        record = self.story_record(summary["epic_id"], "1-2-farewell")
        self.assertEqual(("done", 1), (record["state"], record["restarts"]))
        self.assertNotEqual(first["story"]["lease_id"], record["lease_id"])
        self.assertEqual(1, next(item for item in summary["epic"]["stories"]
                                 if item["key"] == "1-2-farewell")["restarts"])
        lease = json.loads((self.base_dir / "drives" / record["lease_id"] / "lease.json").read_bytes())
        pipeline = drive_pipeline.load(self.base_dir, lease["pipeline_sha256"])
        self.assertEqual((3, 90, f"epic:{summary['epic_id']}"),
                         (pipeline["mandate_revision"], pipeline["roles"]["worker"]["timeout"], lease["session_id"]))
        self.assertEqual({"roles": {"worker": {"timeout": 90}}},
                         drive_epic.newest_state(self.store, summary["epic_id"])["stories_input"]["1-2-farewell"])
        prefix = f"drv-{lease['pipeline_sha256'][:12]}-"   # chains per revision: a fresh create, no spec launch
        self.assertEqual(["impl-1", "impl-review-1"], [item["key"].removeprefix(prefix) for item in self.launch_log()
                                                       if item["key"].startswith(prefix)])
        log = git(self.main, "log", "--format=%s", f"{self.base}..story/1-2-farewell").split("\n")
        self.assertEqual(3, len([line for line in log if "attempt" in line and "[completed]" in line]), log)
        self.assertEqual("story(1-2-farewell): status done", log[0])
        self.assertEqual(1, len([line for line in log if "spec ready-for-dev" in line]), log)   # the spec untouched
        self.assertEqual(git(self.main, "rev-parse", "story/1-2-farewell"),
                         git(self.main, "rev-parse", "coordinator/epic-1"))   # integrated, a fast-forward
        self.assertEqual("pass", unattended_checks(self.repo, [record["lease_id"]], summary["epic_id"])
                         ["integration-current"])
        self.assertEqual({**dict.fromkeys(CRITERIA, "pass"), "judgment-backed-decision": "blocked",   # F17: per story
                          "update-observed": "blocked", "no-duplicate-after-restart": "trivial",
                          "correction-observed": "trivial", "deferred-recorded": "trivial"},
                         epic_checks(self.repo, summary["epic_id"]))

    def test_a_resolved_unknown_attempt_stops_attempt_resolved_and_a_restart_reaches_done_without_a_carry(self):
        """Rulings H10 and H12 (live hook 20, Story 3.3): a story attempt whose worker exited without a return is
        unknown; the operator resolves it with the epic's token; the next run stops `attempt-resolved`; `--restart`
        binds a new revision with no carry (H8 names review and verify stops only) and reaches `epic-done`. The
        epic scorer (F17) reads the resolution as an intervention."""
        self.one_story()
        self.scenario(stories=self.story_scenarios(**{"1-2-farewell": {"impl-1": {"status": "blocked",
                                                                                     "mode": "truncated"}}}))   # D29: an exit is a stop; a broken stream is still unknown
        code, summary, _ = self.epic_start(code=11)
        self.assertEqual(("needs-human", "attempt-unknown"), (summary["outcome"], summary["reason"]), summary)
        holder, feature_id = summary["subjects"]["holder"], summary["story"]["feature_id"]
        self.run_cli("attempt", "resolve", holder, "--token-file", str(self.drive_token), "--confirm-unknown",
                     data={"reason": "worker host rebooted", "source_ref": "ops:ticket-7"}, code=0)
        code, summary, _ = self.epic_resume(code=11)
        self.assertEqual(("needs-human", "attempt-resolved"), (summary["outcome"], summary["reason"]), summary)
        self.assertIn("epic resume --restart 1-2-farewell", summary["epic_action"])
        self.restarted_scenario()
        code, summary, _ = self.epic_resume("--restart", "1-2-farewell", code=0)
        self.assertEqual(("done", "epic-done", {"runs": 1, "merges": 1, "syncs": 0, "stories_done": 1,
                                                "stories_skipped": 0, "stories_restarted": 1}),
                         (summary["outcome"], summary["reason"], summary["counts"]), summary)
        feature = next(row for row in self.snapshot()["features"] if row["id"] == feature_id)
        constraints = json.loads(feature["constraints_json"])
        self.assertEqual((3, 1, None), (feature["mandate_revision"], constraints["restarts"],
                                        constraints.get("restart_carry")))
        self.assertNotIn("## Carried from the restart", self.restarted_brief(summary["epic_id"], "impl"))
        self.assertEqual(("done", 1), (lambda record: (record["state"], record["restarts"]))(
            self.story_record(summary["epic_id"], "1-2-farewell")))
        checks = epic_checks(self.repo, summary["epic_id"])
        self.assertEqual(("fail", "pass", "pass", "pass"), (checks["zero-interventions-inbox-empty"],
                                                            checks["integration-current"], checks["status-synced"],
                                                            checks["attempt-committed"]), checks)

    def test_a_spec_question_judged_before_the_carry_counts_as_judgment_backed(self):
        """Live hook 21 (F20): a spec author's question answered by a judgment at mandate revision 1; the carry
        revises the mandate, so the verdict reads invalid against the current one while the recorded decision
        stands; the epic scorer requires `valid` at the decision's own revision only."""
        self.one_story()
        answer = {"schema": "coordinator-judgment-v3", "verdict": "answer", "deferred": [], "rationale": "P1 delegates",
                  "refusal": "", "answers": [{"index": 1, "text": "keep", "clause": {
                      "authority_id": "P1", "field": "authority", "pointer": "/delegated/0/text",
                      "quote": "The coordinator commits; never ask whether"}}]}
        self.scenario(stories=self.story_scenarios(**{"1-2-farewell": {
            "spec-1": {"files": [[SPEC_PATH, SPEC_WITH_QUESTION]], "questions": ["Strip whitespace from the name?"],
                       "options": ["strip: callers get a trimmed name", "keep: the name is used exactly as given"],
                       "open_questions": Q1},
            "spec-2": {"files": [[SPEC_PATH, SPEC_READY]], "open_questions": []}}}),
            judgments={"default": {"payload": answer}})
        code, summary, _ = self.epic_start(code=0)
        self.assertEqual(("done", "epic-done", 1), (summary["outcome"], summary["reason"],
                                                    summary["counts"]["stories_done"]), summary)
        self.assertEqual(1, len([item for item in self.launch_log() if item["key"].endswith("-spec-j1")]))
        feature_id = self.story_record(summary["epic_id"], "1-2-farewell")["feature_id"]
        decision = next(row for row in self.snapshot()["decisions"]
                        if row["feature_id"] == feature_id and row["source_ref"].startswith("mandate:r1:"))
        feature = next(row for row in self.snapshot()["features"] if row["id"] == feature_id)
        self.assertEqual((1, 2), (decision["mandate_revision"], feature["mandate_revision"]))
        found = judgment.verdict(self.store, decision["request_id"])
        self.assertEqual(("observed", "answer", False, 1),   # invalid against the current mandate only
                         (found["state"], found["verdict"], found["valid"], found["mandate_revision"]))
        self.assertEqual("pass", epic_checks(self.repo, summary["epic_id"])["judgment-backed-decision"])

    def test_restart_is_refused_like_skip_and_excludes_skip(self):
        """Design epic-drive E15, review focus 3: no feature, both flags, a roles file alone."""
        self.one_story(**{"1-2-farewell": "in-progress"})   # story-status stop: a current story without a feature
        code, summary, _ = self.epic_start(code=11)
        self.assertEqual("story-status", summary["reason"], summary)
        code, _, error = self.epic_resume("--restart", "1-2-farewell", code=2)
        self.assertEqual(("INVALID_INPUT", "restart-refused"), (error["error"], error["reason"]))
        code, _, error = self.epic_resume("--skip", "1-2-farewell", "--restart", "1-2-farewell", code=2)
        self.assertEqual(("INVALID_INPUT", "--skip and --restart exclude each other"),
                         (error["error"], error["message"]))
        roles = self.root / "roles.json"
        roles.write_text(json.dumps({"worker": {"timeout": 90}}))
        code, _, error = self.epic_resume("--roles", str(roles), code=2)
        self.assertEqual(("INVALID_INPUT", "--roles goes with --restart"), (error["error"], error["message"]))
        code, _, error = self.epic_resume("--roles", str(self.root / "missing.json"), code=2)   # final review M1
        self.assertEqual(("INVALID_INPUT", "--roles goes with --restart"), (error["error"], error["message"]))
        self.assertEqual("blocked", self.story_record(summary["epic_id"], "1-2-farewell")["state"])
        self.assertNotIn("restarts", self.story_record(summary["epic_id"], "1-2-farewell"))

    def test_restart_is_refused_at_a_stop_with_a_designed_answer_and_not_advertised(self):
        """Ruling H10: `--restart` follows a terminal stop only; at `spec-decisions` the accept or the revise decision
        is the path, so the epic action leaves `--restart` out and a restart is refused with nothing recorded."""
        self.one_story()
        self.template_file = self.write_template(spec_auto_accept=False)
        code, summary, _ = self.epic_start(code=11)
        self.assertEqual(("needs-human", "spec-decisions"), (summary["outcome"], summary["reason"]), summary)
        self.assertIn("epic resume --skip 1-2-farewell", summary["epic_action"])
        self.assertIn("release --token-file <driver token>", summary["epic_action"])
        self.assertNotIn("--restart", summary["epic_action"])
        code, _, error = self.epic_resume("--restart", "1-2-farewell", code=2)
        self.assertEqual(("INVALID_INPUT", "restart-refused",
                          "Story 1-2-farewell stopped spec-decisions, not a terminal stop: epic resume continues it "
                          "(a decision stop takes an inbox decision or an accept first); --restart follows a terminal "
                          "stop"),
                         (error["error"], error["reason"], error["message"]))
        record = self.story_record(summary["epic_id"], "1-2-farewell")
        self.assertEqual(("current", None), (record["state"], record.get("restarts")))

    def test_restart_is_refused_while_an_attempt_holds_the_fence(self):
        """Design epic-drive E15, review focus 3: a run that never ended, then an attempt that holds the fence: the
        refusal names the attempt and the reconcile command."""
        self.one_story()
        self.scenario(stories=self.story_scenarios(**{"1-2-farewell": {"impl-1": {"block": self.block_file(), "files": [
            ["pkg/farewell.py", FAREWELL], ["tests/test_farewell.py", FAREWELL_TEST]]}}}))
        child = self.child_driver(self.epic_args(), stdin=json.dumps(self.epic_input()))
        launches = self.wait_launches(3, timeout=600)   # the spec author, the spec reviewer, the blocked implementer
        child.kill()
        child.wait()
        code, _, error = self.epic_resume("--restart", "1-2-farewell", code=2)   # the run never ended
        self.assertEqual(("INVALID_INPUT", "restart-refused"), (error["error"], error["reason"]))
        code, summary, _ = self.epic_resume(code=11)   # the story run resumes, reconciles the live attempt and stops
        self.assertEqual(("needs-human", "attempt-unknown"), (summary["outcome"], summary["reason"]), summary)
        code, _, error = self.epic_resume("--restart", "1-2-farewell", code=4)
        holder = launches[-1]["assignment_id"]
        self.assertEqual(("BLOCKED_EVIDENCE", "restart-refused",
                          f"worker reconcile {holder} --token-file <driver token>, then epic resume (a restart "
                          "follows the run's terminal stop)"), (error["error"], error["reason"], error["action"]))
        self.assertIn(f"assignment {holder} holds the fence", error["message"])
        record = self.story_record(summary["epic_id"], "1-2-farewell")
        self.assertEqual(("current", None), (record["state"], record.get("restarts")))
        self.assertEqual(1, len({row["subject_id"] for row in self.snapshot()["events"]
                                 if row["kind"] == "drive.leased"}))
        self.unblock()
        kill_group(launches[-1]["pid"])

    def test_a_kill_between_the_restart_record_and_its_revise_is_finished_at_the_next_resume(self):
        """Design epic-drive E15 (b): the recorded restart is revised before the story is leased."""
        first = self.restart_fixture()
        self.restarted_scenario()
        child = self.child_driver(["epic", "resume", "--token-file", str(self.drive_token), "--max-turns", "60",
                                   "--restart", "1-2-farewell"], kill_at="after-restart-record")
        self.assertEqual(-signal.SIGKILL, self.finish(child, timeout=600)[0])
        self.assertEqual("after-restart-record", self.hook_log()[-1]["point"])
        self.assertEqual(2, self.feature_of(first)["mandate_revision"])   # recorded, not yet revised
        self.assertEqual((1, None), (self.story_record(first["epic_id"], "1-2-farewell")["restarts"],
                                     self.story_record(first["epic_id"], "1-2-farewell")["lease_id"]))
        for verb in ("--skip", "--restart"):
            code, _, error = self.epic_resume(verb, "1-2-farewell", code=2)
            self.assertEqual(("Story 1-2-farewell has a restart pending; epic resume runs it first",
                              verb[2:] + "-refused"), (error["message"], error["reason"]))
        code, summary, _ = self.epic_resume(code=0)
        self.assertEqual(("done", "epic-done", 1, 0),
                         (summary["outcome"], summary["reason"],
                          self.story_record(summary["epic_id"], "1-2-farewell")["restarts"],
                          summary["counts"]["stories_restarted"]), summary)
        feature = self.feature_of(first)
        self.assertEqual((3, 1), (feature["mandate_revision"], json.loads(feature["constraints_json"])["restarts"]))
        self.assertEqual(1, len([row for row in self.snapshot()["decisions"] if row["source_ref"] == "epic-restart:1"]))

    def test_a_kill_after_the_restart_revise_binds_the_new_revision_at_the_next_resume(self):
        """Design epic-drive E15 (ruling H9): the restart's decision below the feature's revision means the revise is
        done; the next resume binds a pipeline at the new revision."""
        first = self.restart_fixture()
        self.restarted_scenario()
        child = self.child_driver(["epic", "resume", "--token-file", str(self.drive_token), "--max-turns", "60",
                                   "--restart", "1-2-farewell"], kill_at="after-restart-revise")
        self.assertEqual(-signal.SIGKILL, self.finish(child, timeout=600)[0])
        self.assertEqual("after-restart-revise", self.hook_log()[-1]["point"])
        self.assertEqual(3, self.feature_of(first)["mandate_revision"])   # revised, nothing leased yet
        code, summary, _ = self.epic_resume(code=0)
        self.assertEqual(("done", "epic-done"), (summary["outcome"], summary["reason"]), summary)
        self.assertEqual(3, self.feature_of(first)["mandate_revision"])
        self.assertEqual(1, len([row for row in self.snapshot()["decisions"] if row["source_ref"] == "epic-restart:1"]))
        record = self.story_record(summary["epic_id"], "1-2-farewell")
        lease = json.loads((self.base_dir / "drives" / record["lease_id"] / "lease.json").read_bytes())
        self.assertEqual((1, 3), (record["restarts"],
                                  drive_pipeline.load(self.base_dir, lease["pipeline_sha256"])["mandate_revision"]))
        self.assertEqual(2, len({row["subject_id"] for row in self.snapshot()["events"]
                                 if row["kind"] == "drive.leased"}))

    def test_a_restart_is_numbered_after_the_features_recorded_restarts(self):
        """Design epic-drive E15: `n` follows the feature's `epic-restart:<n>` decisions (a feature adopted from an
        earlier epic keeps its own), so the new restart never collides with an earlier one."""
        first = self.restart_fixture()
        feature = self.feature_of(first)
        Coordinator(self.store).record_decision(self.driver_owner(), DecisionInput(
            feature["id"], feature["mandate_revision"], "operator-reported", "epic-restart:1", "an earlier epic",
            {"restart": 1}))
        self.restarted_scenario()
        code, summary, _ = self.epic_resume("--restart", "1-2-farewell", code=0)
        self.assertEqual(("done", "epic-done"), (summary["outcome"], summary["reason"]), summary)
        feature = self.feature_of(first)
        self.assertEqual((2, 3, 2), (self.story_record(summary["epic_id"], "1-2-farewell")["restarts"],
                                     feature["mandate_revision"], json.loads(feature["constraints_json"])["restarts"]))
        self.assertEqual([(2, {"restart": 2})], [(row["mandate_revision"], json.loads(row["choice_json"]))
                                                 for row in self.snapshot()["decisions"]
                                                 if row["source_ref"] == "epic-restart:2"])

    def test_a_restart_checks_the_storys_effective_box(self):
        """Design epic-drive E15 (ruling H6 amended): the restart target is left out of the resume's override check
        and `restart` checks the story's effective box: the old one still refuses a shorter deadline, naming the
        story; a new one that fits replaces it."""
        first = self.restart_fixture(data=self.epic_input(stories={"1-2-farewell": {"roles": {"worker": {
            "timeout": 1000}}}}))
        self.restarted_scenario()
        code, _, error = self.epic_resume("--restart", "1-2-farewell", "--deadline", "1000", code=2)
        self.assertEqual(("bounds-insufficient", True), (error["reason"], "Story 1-2-farewell" in error["message"]))
        self.assertNotIn("restarts", self.story_record(first["epic_id"], "1-2-farewell"))
        roles = self.root / "roles.json"
        roles.write_text(json.dumps({"worker": {"timeout": 90}}))
        code, summary, _ = self.epic_resume("--restart", "1-2-farewell", "--roles", str(roles), "--deadline", "1000",
                                            code=0)
        self.assertEqual(("done", "epic-done", {"roles": {"worker": {"timeout": 90}}}),
                         (summary["outcome"], summary["reason"],
                          drive_epic.newest_state(self.store, summary["epic_id"])["stories_input"]["1-2-farewell"]),
                         summary)

    def test_a_restart_before_the_carry_writes_the_spec_again_with_the_carried_review(self):
        """Design epic-drive E15 (ruling H8): a critical spec review with no correction budget stops the spec chain
        at revision 1; after the restart the spec author writes the spec again at revision 2 with the review
        carried, the carry replaces the constraints (neither `restarts` nor `restart_carry` stays, ruling H9) and the
        epic ends done."""
        self.one_story()
        self.template_file = self.write_template(budgets={"max_updates": 3, "max_judgments": 8, "max_corrections": 0})
        claim = "the spec leaves out the empty name"
        self.scenario(stories=self.story_scenarios(), steps={"spec-review-1": {"findings": [
            {"severity": "critical", "trigger_condition": claim}]}})
        code, first, _ = self.epic_start(code=11)
        self.assertEqual(("critical-finding", "spec-accept", 1),
                         (first["reason"], first["subjects"]["step_id"], self.feature_of(first)["mandate_revision"]),
                         first)
        again = SPEC_READY.replace("the new function", "the new function, written again")
        self.scenario(stories=self.story_scenarios(**{"1-2-farewell": {"spec-1": {"files": [[SPEC_PATH, again]],
                                                                                  "open_questions": []}}}))
        code, summary, _ = self.epic_resume("--restart", "1-2-farewell", code=0)
        self.assertEqual(("done", "epic-done"), (summary["outcome"], summary["reason"]), summary)
        brief = self.restarted_brief(summary["epic_id"], "spec")
        self.assertIn("## Carried from the restart\n" + impl.RESTART_INTRO + "\n- Review "
                      f"`{first['subjects']['review_id']}` of the earlier revision:", brief)
        self.assertIn(f"trigger: {json.dumps(claim)}", brief)
        self.assertLess(brief.index("## Carried from the restart"), brief.index("## Spec template"))
        feature = self.feature_of(first)
        constraints = json.loads(feature["constraints_json"])
        self.assertEqual((3, False, False), (feature["mandate_revision"], "restarts" in constraints,
                                             "restart_carry" in constraints))
        self.assertEqual([1], [row["mandate_revision"] for row in self.snapshot()["decisions"]
                               if row["source_ref"] == "epic-restart:1"])
        self.assertIn("the new function, written again", git(self.main, "show", f"story/1-2-farewell:{SPEC_PATH}"))


class EpicStatusTests(EpicDriveCase):
    """Task 6: `epic status` and the `integration-current` criterion."""

    def test_epic_status_lists_epics_newest_first_with_the_current_story_and_the_last_summary(self):
        with patch.object(drive_epic, "open_store", side_effect=DomainError("UNINITIALIZED", "no store")):
            self.assertEqual({"state": "uninitialized"}, drive_epic.status(self.repo))
        fresh = self.root / "fresh"   # the CLI answers before the store opens and creates nothing
        fresh.mkdir()
        git(fresh, "init", "-q")
        saved, self.drive_repo = self.drive_repo, fresh
        try:
            code, view, _ = self.run_cli("epic", "status", code=0)
        finally:
            self.drive_repo = saved
        self.assertEqual({"state": "uninitialized"}, view)
        self.assertFalse((fresh / ".git" / "coordinator").exists())
        code, view, _ = self.run_cli("epic", "status", code=0)
        self.assertEqual([], view["epics"])
        self.base = self.commit_files({f"{ARTIFACTS}/sprint-status.yaml":
                                       sprint_file(**{"1-3-shout": "done", "1-4-whisper": "done"})}, "sprint")
        self.template_file = self.write_template(spec_auto_accept=False)
        code, summary, _ = self.epic_start(code=11)
        code, view, _ = self.run_cli("epic", "status", code=0)
        self.assertEqual([summary["epic_id"]], [item["epic_id"] for item in view["epics"]])
        item = view["epics"][0]
        self.assertEqual(("coordinator/epic-1", 1, "spec-decisions", None),
                         (item["state"]["branch"], item["runs"], item["last_summary"]["reason"], item["current_story"]))
        self.assertIn("then epic resume", item["last_summary"]["operator_action"])
        self.assertEqual(f"epic:{summary['epic_id']}", view["owner"]["session_id"])
        fd = drive.acquire_lock(self.t.repo, exclusive=True)   # status takes no lock: it reads beside a driver
        try:
            code, view, _ = self.run_cli("epic", "status", "--epic", summary["epic_id"], code=0)
        finally:
            os.close(fd)
        self.assertEqual({"held": True, "mode": "exclusive", "pids": [os.getpid()]}, view["lock"])
        current = view["epics"][0]["current_story"]
        self.assertEqual(summary["story"]["feature_id"], current["features"][0]["feature_id"])
        self.assertEqual(1, len(current["leases"]))
        code, _, error = self.run_cli("epic", "status", "--epic", "nope", code=2)
        self.assertEqual("NOT_FOUND", error["error"])
        # no story of the epic is done yet: the criterion has nothing to score
        lease_id = current["leases"][0]["lease_id"]
        self.assertEqual("blocked", unattended_checks(self.t.repo, [lease_id],
                                                      epic_id=summary["epic_id"])["integration-current"])
        # a later epic is listed first
        drive_epic.anchor_state(self.store, self.driver_owner(), {"epic_id": "f" * 32, "revision": 1, "stories": []},
                                "epic.started", "epic")
        code, view, _ = self.run_cli("epic", "status", code=0)
        self.assertEqual(["f" * 32, summary["epic_id"]], [item["epic_id"] for item in view["epics"]])
        self.assertEqual((0, None), (view["epics"][0]["runs"], view["epics"][0]["last_summary"]))

    def test_integration_current_scores_the_integration_branch(self):
        code, summary, _ = self.epic_start(code=0)
        leases = sorted({row["subject_id"] for row in self.snapshot()["events"] if row["kind"] == "drive.leased"})
        checks = unattended_checks(self.t.repo, leases, epic_id=summary["epic_id"])
        self.assertEqual("pass", checks["integration-current"])
        self.assertEqual("blocked", unattended_checks(self.t.repo, leases)["integration-current"])
        # an `epic(1):` commit by the project identity above the last story's head stays coordinator-current
        git(self.epic_worktree(), "-c", "user.name=BMAD Coordinator", "-c", "user.email=coordinator@example.invalid",
            "commit", "--allow-empty", "-qm", "epic(1): sync by the coordinator")
        self.assertEqual("pass", unattended_checks(self.t.repo, leases,
                                                   epic_id=summary["epic_id"])["integration-current"])

        def score():
            return unattended_checks(self.t.repo, leases, epic_id=summary["epic_id"])["integration-current"]

        # the newest anchored state no longer reads back: the evidence contradicts, never merely missing
        newest = [row for row in self.snapshot()["events"] if row["kind"] in ("epic.started", "epic.advanced") and
                  row["subject_id"] == summary["epic_id"]][-1]
        path = self.base_dir / newest["evidence_ref"]
        sealed = path.read_bytes()
        path.write_bytes(sealed + b" ")
        self.assertEqual("fail", score())
        path.write_bytes(sealed)
        self.assertEqual("pass", score())
        # the integration branch reset below the done stories' heads
        head = git(self.main, "rev-parse", "coordinator/epic-1")
        git(self.main, "update-ref", "refs/heads/coordinator/epic-1", self.base)
        self.assertEqual("fail", score())
        git(self.main, "update-ref", "refs/heads/coordinator/epic-1", head)
        self.assertEqual("pass", score())
        # a commit on the integration branch by someone else after the last story: not coordinator-current
        self.commit_files({"docs/after.md": "after\n"}, "after the epic", repo=self.epic_worktree())
        self.assertEqual("fail", unattended_checks(self.t.repo, leases,
                                                   epic_id=summary["epic_id"])["integration-current"])
