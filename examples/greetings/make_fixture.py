#!/usr/bin/env python3
"""Build the example project for the quickstart (docs/getting-started.md).

    python3 examples/greetings/make_fixture.py /absolute/empty/directory

Creates `<directory>/main`, a committed Git repository with a one-function Python package, its test, a house-rules
file and the three BMAD-shaped artifacts the epic drive reads (`epics.md` with Epic 1: Story 1.1 done, 1.2 to 1.4 in
the backlog, 1.3 gated on a settled deferred-work entry; `sprint-status.yaml`; `deferred-work.md`), and beside it
`pipeline.json` (a copy of this directory's template) and `epic-input.json` with `worktrees_dir` set to
`<directory>/worktrees`. Standard library only; no model call; refuses a directory that is not empty."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ARTIFACTS = "_bmad-output/implementation-artifacts"
EPICS = "_bmad-output/planning-artifacts/epics.md"

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

SPRINT_FILE = """# Story Status

development_status:
  epic-1: in-progress
  1-1-greeting: done
  1-2-farewell: backlog
  1-3-shout: backlog
  1-4-whisper: backlog
  epic-2: backlog
  2-1-other: backlog
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

FILES = {
    EPICS: EPIC_FILE,
    f"{ARTIFACTS}/sprint-status.yaml": SPRINT_FILE,
    f"{ARTIFACTS}/deferred-work.md": LEDGER_FILE,
    "docs/house-rules.md": "House rules: return strings, never print.\n",
    "pkg/__init__.py": "",
    "pkg/greet.py": "def greet(n):\n    return 'Hello, %s!' % (n or 'stranger')\n",
    "tests/__init__.py": "",
    "tests/test_base.py": "import unittest\n\n\nclass Base(unittest.TestCase):\n    def test_base(self):\n"
                          "        self.assertTrue(True)\n",
}

IDENTITY = ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid"]


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1].startswith("-"):
        print(__doc__, file=sys.stderr)
        return 2
    target = Path(argv[1]).resolve()
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        print(f"refused: {target} exists and is not an empty directory", file=sys.stderr)
        return 2
    main_dir = target / "main"
    for rel, text in FILES.items():
        (main_dir / rel).parent.mkdir(parents=True, exist_ok=True)
        (main_dir / rel).write_text(text)
    subprocess.run(["git", "init", "-q", str(main_dir)], check=True)
    subprocess.run(["git", "-C", str(main_dir), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(main_dir), *IDENTITY, "commit", "-q", "-m", "greetings fixture"], check=True)
    shutil.copyfile(HERE / "pipeline.json", target / "pipeline.json")
    epic_input = json.loads((HERE / "epic-input.json").read_text())
    epic_input["worktrees_dir"] = str(target / "worktrees")
    (target / "epic-input.json").write_text(json.dumps(epic_input, indent=1) + "\n")
    print(f"repository: {main_dir}\npipeline:   {target / 'pipeline.json'}\nepic input: {target / 'epic-input.json'}\n"
          f"next: install the coordinator into the repository and commit it (docs/getting-started.md, Quickstart)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
