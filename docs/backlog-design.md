# The backlog adapter: the coordinator's own input contract, BMAD as the shipped layout

Status: approved 2026-10-20 (operator: "No dobra - to leć"); plan 2026-10-20-backlog (project history); implemented 2026-10-20 with fake clients (plan 2026-10-20-backlog: A1/A2 the move and the contract, A3/A4 the call sites, the pipeline key and the contract test, A5 the documentation), the full suite green (1040 tests) and the quickstart on the example fixture `epic-done` with the status files, commits and result sections in the same shape as the recorded run (project history, 2026-10-20-backlog/quickstart-report). Amended at implementation: the layout is named in the pipeline's `project` block (`project.backlog`), beside `artifacts_dir` where the status step and `story add` already look, rather than in the epic input (A3); the epic input keeps `epics_path`. Baseline `1618172`. Builds on the [epic drive](epic-drive-design.md) (E5, E6, E13), the [implementation drive](implementation-drive-design.md) (D18, D19, D24) and the [behavior contract](behavior-contract.md). Standard library only, as everything else.

## 1. Problem

The epic and story drives read and write the backlog through BMAD's file layout, wired into the code in four places: `drive_epic.plan_stories` (the `### Story` headings of the epics file and the `development_status` map of `sprint-status.yaml`, the artifacts directory hard-wired there), `bmad_implementation._epic_entry` and `parse_epic_story` (the story entry the spec author gets), `preconditions_state` and `_ledger_entries` (`deferred-work.md` and its archive), and `sync_status` with `sync_sprint_status` and `append_deferred_work` (the status commits). `PROTECTED_DIRS` and `ARTIFACTS_DIR` carry the names too. The documentation says, truthfully, that BMAD need not be installed and that the files are "BMAD-shaped"; but the shapes are BMAD's and nothing names the contract in the coordinator's own words, so a second layout (GitHub Spec Kit's `specs/<feature>/`, GitHub Issues, a plain `backlog.md`) would mean touching all four places and every test around them. The repository's name follows from the same fact: `bmad-coordinator` is true today and would be false the day a second layout lands.

The market makes the question timely: spec-driven development has converged on specify, plan, tasks, implement, with several young, churning frameworks (Spec Kit, Kiro, OpenSpec, Tessl, BMAD v6) and no standard among them. What is durable underneath is older and vendor-neutral: a story with testable acceptance lines, a status per story, a ledger of decisions. That is all the drives need.

## 2. Goal and non-goals

**Goal.** One module boundary, `backlog`, behind which the drives see stories, entries, statuses, preconditions and deferred decisions in the coordinator's own words; the BMAD layout as the one shipped adapter, byte for byte the behaviour of today (the status commits, the briefs, the sealed receipts and their digests unchanged); the contract documented so that a second adapter is a bounded task with a contract test to pass; the epic input able to name the layout, the default unchanged.

**Non-goals.** A second adapter (it comes with the first user who needs one, and it will be the moment to rename); renaming the installed directory `.bmad-coordinator/` or the managed block `bmad-coordinator:begin v1`; any change to the attended planning front (`bmad_contract`, `bmad_planning`, the `bmad` commands), which stays BMAD's by nature and optional; any change to the spec file, whose frozen section and `## Coordinator Result` are the coordinator's own format already (D24, D18).

## 3. Decisions

**A1. The contract.** `coordinator_core/backlog.py` defines `Backlog`, what a layout must answer, each in terms of a commit (the drives never read the working tree for the backlog):

- `stories(repo, commit, epic) -> list[dict]`: the epic's stories in the backlog's order, each `{number, title, key, keys, status}` as `plan_stories` returns them today (`key` None and `keys` the candidates when the status map has none or several for the story; `status` the map's value).
- `entry(repo, commit, key, spec_path) -> EpicStory`: the story's entry for the spec author (title, user story lines, acceptance lines, precondition bullets, the entry's text), with today's refusals (the key not listed at `backlog` or `ready-for-dev`, no acceptance lines, the spec already at the commit).
- `preconditions(repo, commit, bullets) -> list[dict]`: the mechanical state of each precondition bullet (`met` or `unmet` with a reason and the lines for a brief), as `preconditions_state` returns it.
- `status_files(key) -> list[str]`: the files the status step reads and commits (today the sprint map and the ledger when entries are appended).
- `status_edits(texts, key, phase, entries) -> dict[str, str]`: the new text of each status file for a phase (`spec`, `start`, `done`): the map edit with the epic's own status moved as today (D18), the ledger appended with canonical entries (D19); comments, order, a BOM and CRLF preserved. The spec's own edits (`write_spec_result`) stay outside the adapter: the spec is the coordinator's file.
- `protected() -> tuple[str, ...]`: the directories a worker may never write beside the coordinator's own list.

Nothing else moves: briefs, mandates, clauses, bundles, reviews, receipts, the bill, the epic state and the pipeline keep their shapes, and the spec path keeps coming from the pipeline's `story` block.

**A2. The BMAD adapter is today's code moved, not rewritten.** `coordinator_core/backlog_bmad.py` holds `plan_stories`, `sprint_entries`, `parse_epic_story`, `_in_sprint_status`, `_epic_entry`, `_ledger_entries`, `preconditions_state`, `preconditions_section`, `sync_sprint_status` and `append_deferred_work` as they are, with `ARTIFACTS_DIR`, `_LEDGER` and `_ARCHIVE`; `drive_epic`, `bmad_implementation` and `bmad_spec_author` call them through the `Backlog` the epic input selected. The tests that pin their bytes (the status commits, the briefs' precondition sections, the ledger numbering) move with them and keep passing unchanged: zero behavioural change is the acceptance criterion of this stage, checked by the full suite and by the example run.

**A3. The layout is named in the pipeline's project block, the default unchanged.** `project.backlog: {"kind": "bmad"}`, optional; absent means `bmad`, and a pipeline without it keeps its digest (no default inserted). The block sits beside `artifacts_dir`, which already names the status files' directory and stays where it is (the spec path lives there; a story added from an epic keeps the default directory, as today). The epic input keeps `epics_path` (the epics file is the epic's by nature). A `kind` the registry does not hold is `INVALID_INPUT` ("backlog kind <x> is not supported"), the closed-registry rule of the clients. `story add` resolves the layout the same way from the pipeline's project block it is given, and the epic driver from its template.

**A4. The contract test.** `tests/test_backlog_contract.py` runs one set of checks against every registered adapter, on fixtures each adapter supplies: the stories of an epic in order with their keys and statuses, a story with two candidate keys and one with none, the entry's parts and its text, preconditions met, unmet, keep-open, repeated and archived, the status edits for the three phases idempotent and preserving comments, a BOM and CRLF, the ledger append's numbering after the greatest existing entry. Today it runs against `bmad` alone; a second adapter passes it or is not registered.

**A5. Documentation in the coordinator's words.** Getting started's "What the project needs" §1 becomes "A backlog in a supported layout": the contract in plain terms (a story entry with a title, user story lines, acceptance lines written as the test, optional preconditions naming decisions; a status per story, `backlog` or `done`; a ledger of deferred decisions), then the BMAD layout as the one shipped, with its three files as today. Concepts gets a paragraph "The backlog". The usage reference gets the `backlog` block and the registry. The README says "a backlog in a supported layout (BMAD's today)" where it says "BMAD-style". This design records the BMAD layout's rules as the reference for the next adapter.

**A6. The name stays, for now.** The coupling that matters is not the repository's name but the installed directory `.bmad-coordinator/` and the managed block markers `bmad-coordinator:begin v1`: the installer, `doctor`, `--upgrade`, the protected paths and every installed project carry them, so a rename is an installer change with an upgrade path (`v2` markers), to be done once. A repository rename alone now would gain nothing (no users yet, GitHub redirects the old name for years) and would leave the real coupling in place. The rename is scheduled with the second adapter, as one operation: repository, installed directory, markers, documentation. Until then the name is true: the one shipped layout is BMAD's.

## 4. Changes by file

- `coordinator_core/backlog.py` (new): the `Backlog` protocol, the registry (`get(kind)`), `resolve(epic_input, project)` to the adapter with its paths.
- `coordinator_core/backlog_bmad.py` (new): today's functions moved from `drive_epic.py` and `bmad_implementation.py`, unchanged, wrapped as the `bmad` adapter.
- `coordinator_core/drive_epic.py`: `EpicInput.backlog`, `check_input` validates the block, the state records it, `plan_stories` and the precondition check go through the adapter.
- `coordinator_core/bmad_implementation.py`: `story_add` and `sync_status` go through the adapter (`status_files`, `status_edits`); `PROTECTED_DIRS` adds the adapter's `protected()`.
- `coordinator_core/bmad_spec_author.py`: the entry through the adapter.
- `coordinator_core/__main__.py`: nothing new on the command line; the epic input carries the block.
- `docs/getting-started.md`, `docs/concepts.md`, `docs/state-core-usage.md`, `README.md`, `CHANGELOG.md`, this design; `examples/greetings/epic-input.json` unchanged (the default).
- `tests/test_backlog_contract.py` (new); the moved tests in `test_drive_epic.py` and `test_bmad_implementation.py` re-homed with the code; a test that an epic input without the block seals the same state digest as before, and that an unknown `kind` is refused.

## 5. Tests

Fake clients only. The full suite green with the moved code (every pinned byte unchanged); the contract test against `bmad`; the epic input's `backlog` block validated (an unknown kind refused, a bad path refused, the defaults resolved to today's paths); a sealed epic state without the block read as `bmad`; `story add` with and without the block.

## 6. Live verification

The quickstart on the example fixture once, with Claude Code, on the commit of this stage: `epic-done`, the status commits and the result sections byte-identical in shape to the recorded run, the bill as before. Nothing else: this stage changes no behaviour.

## 7. Residual uncertainty

1. The contract is drawn from one layout; the second adapter will show what it missed (a layout without a status map, such as Spec Kit's `tasks.md` or GitHub Issues' labels, needs a status the coordinator can edit, or a mapping).
2. `project.artifacts_dir` and the pipeline's spec path template keep BMAD-flavoured defaults (`_bmad-output/implementation-artifacts/`); they are explicit and changeable per project, so the defaults alone are not coupling, only convention.
3. The attended planning front stays BMAD's (`bmad discover`, `prepare`, `run`); a user of another layout plans with that layout's own tooling and never touches it.
