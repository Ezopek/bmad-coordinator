# Changelog

Dates, with one tag: `v0.1.0` marks the first public release (2026-10-20, the entry below, published as the single-root history of this repository); the installed package carries a `package_revision` digest, not a version. Each entry names the design document (under `docs/`) whose decisions it implements; the document's status line records the evidence.

## 2026-10-22

- Issue #1, the BMAD 6.12 architecture Reviewer Gate ([design](docs/claude-adapter-design.md) §5, amended): a bound Claude or Copilot architecture author may run the gate's `lint_spine.py`, bound with its report written to `<workspace>/.lint-spine.json`, so the Copilot launcher refuses any other `--output` and the report lies in the audited delta. Every bound Claude or Copilot planning brief says that no subagent tool is offered, that the skills' subagent steps take their sequential fallback (each review written under `<workspace>/reviews/`, one `assumptions` entry) and that this gate is a self-check, not the independent review. `bmad discover` lists in `facts.unbound_scripts` every other `uv run` script the required skills document (6.12: `bmad-review`'s `word_metrics.py`, whose word counts fall back to estimates), before any model call.
- F23: a relative central config path resolves against the project root, as BMAD reads it. The BMAD 6.12.0 installer writes `output_folder = "_bmad-output"`, which `bmad prepare` and `bmad review prepare` refused before (`UNSUPPORTED_CONTRACT`, found by the issue #1 live check); a path outside the worktree is still refused.

## 2026-10-20 (v0.1.0)

- The backlog adapter ([design](docs/backlog-design.md), A1 to A6): the drives read and edit the backlog through one contract (stories, entries, preconditions, status files and edits, protected directories); the BMAD layout is the code of before moved behind it unchanged, the one shipped and the default; `project.backlog: {"kind"}` names a layout from the closed registry; a contract test every layout must pass; the name stays until a second layout lands.
- The bill ([design](docs/bill-design.md), B1 to B8): `bill [--epic | --feature | --lease]` reads every client launch from the sealed receipts and prints the coordinator's observations (launches, wall clock, runs, driver turns) apart from the clients' reports (tokens, USD from Claude Code, premium requests from Copilot, the client's turns), with the count of launches that reported nothing beside every sum; the client facts record each client's own usage at return (Codex returns carried none before); the drive and epic summaries, `epic status` and the spec's `## Coordinator Result` carry the same numbers; `budgets.max_cost_usd` in the pipeline (per story) and in the epic input (per epic) stops a run `cost-budget` before the launch that would exceed the reported money, `epic resume --restart KEY --budgets FILE` and `epic resume --max-cost-usd N` are the way out; no price of the coordinator's own.
- F22: a spec author's question is compared with a trailing full stop dropped (live hook 22); F21 and F22 are the F18 family.
- Python 3.11, the declared floor, holds again: the status step used `Path.read_text(newline=)`, which exists since 3.13 (found by the first CI run).
- Public documentation: the README as the entry, getting started (prerequisites, installation, the quickstart, your own repository, stops, costs, safety), concepts, troubleshooting, the example project `examples/greetings/` with its pipeline and epic input, this changelog and the contributing guide.
- The MIT license, also copied into every installed package.
- The development history (plans, live-run reports, experiments, research) left the public tree.
- The README says why the coordinator and not the agent CLI alone, and what that costs; getting started and troubleshooting give the reason for the ext4 rule; the suite runs on GitHub Actions (`.github/workflows/tests.yml`, Python 3.11); [a recorded run](docs/example-run.md) of the quickstart.
- F21: a spec author's option label is compared with code quotes and whitespace aside, as its question is since F18; the recorded run had stopped `attempt-unknown` on a label written with code marks in the file and without them in the return.

## 2026-10-19

- Implementation drive D25 and D26, epic drive E14 and E15: a turn limit per role (`max_turns`) and the Claude client stop (`client-stopped`, committed and continued); `finding_counts` on the approval stops and in `detail review`; the correction repeated in a continuation after a decision; a story's own worker box (`stories.<key>.roles`); `epic resume --restart KEY [--roles PATH]`.
- Live hooks 19 to 21: Codex and Copilot workers through whole epics on a fixture, the first epic to `epic-done` on a real project, the client stop live. Fixes F16 (reviewers attesting the sealed path), F18 (a spec author's question quoted without code marks), F20 (the per-story scorer after a carry); `{main_worktree}` in setup and verify commands (F6); F19 records the two turn units.

## 2026-10-18

- Epic drive E1 to E13: `epic start`, `epic resume`, `epic status`; one coordinator-owned integration branch, the base branch synced at story boundaries, preconditions checked against `deferred-work.md`, every remaining story through the story drive, merged at `done`.

## 2026-10-15 to 2026-10-17

- The spec author step (implementation drive D24): a story added from its epics entry gets its spec written, reviewed and accepted inside the drive (your accept by default, `spec_auto_accept` otherwise), then the carry revises the mandate from the accepted spec. F9 and F10: the code scopes known to the spec author and reviewer; `acceptance-current` with a carried spec acceptance.

## 2026-10-13

- D22: a story drive stands without the planning contract (no `compat` step, no BMAD installation needed in the story worktree).
- D23: `web: true` on the worker role (Claude `WebFetch` and `WebSearch`, network inside the Codex sandbox).
- `install.py --upgrade` (F8): an in-place package upgrade with preview and apply.

## 2026-10-11 to 2026-10-12

- Implementation drive D1 to D21: `story add`; the story pipeline (setup and baseline, implement, attempt commits by the coordinator, verify as a gate, commit-bound bundles, code review with fixed lenses, acceptance, the status steps, deferred work); judgment schema v3 with `defer`; the story stop catalogue. Live proof on a fixture, then the first story on a real project after the fixes F1 to F3.

## 2026-10-10

- The model selection guide, from the retained live evidence.

## 2026-10-08 to 2026-10-09

- Correction round: `budgets.max_corrections`; a blocking review triggers bounded corrections that carry the findings; the first unattended `done` on real clients, kill-and-restart under `drive_loop`, a Copilot author through acceptance, the correction round on Codex.

## 2026-10-07

- Judgment schema v2: a clause per answer, so a multi-question request is answered when every question is covered.

## 2026-10-05 to 2026-10-06

- Unattended driver: `drive start`, `drive resume`, `drive status`; sealed pipelines, the inbox, bounds, the closed stop catalogue, judgment schema v1; the first live hook with Claude Code workers.
- Claude Code (`claude -p`) and GitHub Copilot CLI (`copilot -p`) worker adapters beside Codex CLI; the closed client registry.

## 2026-10-04

- Behavior contract, v1 design, delivery outline, assignment protocol.
- State core: the SQLite store under the Git common directory; ownership (claim, token, generation, release); features and mandates; assignments, decisions, bundles, reviews, acceptance and `verify`; export and import; the installer with preview and apply; `doctor`.
- Worker adapter for Codex CLI (`codex exec`): one attempt per process, sealed receipts, `worker run`, `worker inspect`, `worker reconcile`.
- BMAD planning integration: `bmad discover`, `prepare`, `run`, `bundle`, `review prepare`, `review record`, `accept`, `verify`.
