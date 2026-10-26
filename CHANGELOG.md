# Changelog

Dates, with one tag: `v0.1.0` marks the first public release (2026-10-20, the entry below, published as the single-root history of this repository); the installed package carries a `package_revision` digest, not a version. Each entry names the design document (under `docs/`) whose decisions it implements; the document's status line records the evidence.

## 2026-10-26

- Issue #6, the managed `AGENTS.md` block's position: `doctor`, the base check before every worker launch, and the installer accept the block anywhere in the file, exactly once and unchanged, so project sections may follow it; an upgrade replaces it in place and keeps what surrounds it. Before, anything after the end marker was reported as "Installed AGENTS block differs" (or "Base AGENTS block differs") and refused an upgrade.

## 2026-10-25

- Issue #4, the planning path on a gitignored BMAD install ([BMAD snapshot design](docs/bmad-snapshot-design.md)): `bmad materialize` copies the main worktree's `_bmad/` and client root into a sealed, content-addressed snapshot and from it into the feature worktree, proving the copy by its listing and its fingerprint; the planning and planning-review bindings record `bmad_source`, every prepare, derive, reservation and return compares the materialized trees with the snapshot before any installed code runs, replay checks the stored snapshot, and a boundary writer gets the roots read-only. `doctor --bmad` checks the install state and the ignore rules. A committed install keeps its behavior and its binding digests.
- Issue #5, Codex's SQLite state ([launch boundary design](docs/launch-boundary-design.md) L3): every Codex launch sets `CODEX_SQLITE_HOME` to the run's own `codex-sqlite/`, so Codex's databases (its cross-session memories among them) and their `-wal`/`-shm` sidecars are the run's, created empty and discarded with it, instead of being opened in the shared `CODEX_HOME`, where a sidecar beside the read-only memories store ended a finished review `unknown`. The guard on persistent entries is unchanged. An unclean integrity record's evidence need now names what it shows (the client-state entries by name) instead of listing every possible cause.
- Issue #3, the Copilot token's kind ([launch boundary design](docs/launch-boundary-design.md) L3): a classic personal access token (`ghp_`), which the Copilot CLI does not support, is refused before any reservation, whether it is in the documented variable Copilot uses first or is the `gh auth token` a sandboxed launch takes; `doctor --client copilot` reports it as not ready. Receipts record the token's kind (`oauth`, `fine-grained-pat`, `app-user`, `unknown`) beside its source, never the value. A Copilot run that exits non-zero names the authentication error it logged with the cause.

## 2026-10-24

- D30: `max_judgments` bounds the judgment launches of one step (a story's spec step and implement step each, and each author step of a planning pipeline) instead of the whole drive. The budget's shape and digest are unchanged.
- D29: a story attempt that the coordinator itself ended at the role's time box, or whose process exited non-zero without a client stop, is the client stop `client-failure` when the process group is drained and the integrity record is clean: committed, continued with the cause in the brief, and bounded by the new story budget `client_failures` (default 2; absent keeps every pipeline digest), at which the story stops `client-failure` for the operator. A kill of the coordinator, a surviving process group or an unclean integrity record still leave the attempt `unknown` for `attempt resolve`.
- D28: the files a story brief tells the worker to read first, the project's `rules` and the spec's (or, for the spec author, the project's) `context` paths, are sealed as the assignment's contract files: present at prepare, current at reserve, unchanged at return, protected from the worker's writes.
- D27: a story worker's return that the coordinator refuses after the scope checks (the JSON outside the schema, or the profile refusing it, as in F18, F21, F22 and F25) is no longer an `unknown` attempt for the operator to resolve. It is the client stop `invalid-return`, committed and continued like a turn-limit stop, with the refusal quoted in the continuation brief and counted in `max_updates`. Scope and integrity violations, timeouts, non-zero exits and refused reviewer or judgment returns stay `unknown`.
- The planning review in read mode ([design](docs/copilot-reviewer-design.md) §10 R4): a `reviewer` role or `bmad review prepare` in read mode now takes a planning bundle too. The prepare stages the sealed artifacts and dependencies (read in full) and the review skill's own contract files, overrides and config layers (read as needed, never resolved), and the brief names the lenses. The profile `planning-review` passed its qualification on Copilot CLI 1.0.102 (a seeded contradiction with an authority input, found as critical; replay agreed), so every read-mode profile is qualified on the installed CLI.

## 2026-10-23

- The read mode's repair round and two parser relaxations ([design](docs/copilot-reviewer-design.md) §10 R1 to R3): a read-mode review whose first answer is refused for something a second answer can fix (prose around the JSON, a missing field, a required file not read in full, a review-profile refusal) resumes its own session once, under the same sandbox and tools, with the exact refusal as its prompt; the repair's stream, envelope and record are sealed and checked at replay, and there is no third round. A repeated JSON key with equal values is one reading. A finding located by a staged alias is recorded by its manifest path. With these, the spec review (live, through the repair round) and the code review (a retained live stream under the final parser) are qualified on Copilot CLI 1.0.102; the judgment, qualified on 1.0.91, passed its run on 1.0.102 too, so all three read-mode profiles are qualified on the installed CLI.
- Copilot as reviewer, the read mode for spec and code reviews ([design](docs/copilot-reviewer-design.md) §10): implemented. A `reviewer` role or `bmad review prepare` may name `copilot` with `"mode": "read"` and `"boundary": "bwrap"`. The prepare seals the inputs (the sealed files, plus the diff and the spec for a code review, or the rules and context files for a spec review, at the bundle's commit), the run stages them under inert aliases in the run directory and launches Copilot with only `view`, `grep` and `glob`, and the coordinator records the observed full reads as the review's `bundle_read`. Qualification is per profile; in the first live pass (1.0.102) every review found its seeded defect, but three of the four answers the coordinator could check broke the JSON contract, which led to the repair round above. The read-mode contract now takes `grep`'s `paths` argument, and a full read is kept once observed.
- F25: a spec author's open questions are checked against the spec file by ids, each entry's number of options and the number of questions asked; the words are the file's. A Copilot spec author dropped one word from an option label, and the return was refused although it named the file's entry; the fourth case of the F18 family. The record, the decisions stop and the judgment now carry the file's words.
- F26: under the launch boundary, Codex's global instruction files (`AGENTS.md`, `AGENTS.override.md` in `CODEX_HOME`) are shown empty and read-only, so a worker follows the project's rules, not the operator's own. The operator's files are never written. The launch record names them with the new mount mode `empty`.

## 2026-10-22

- Copilot as judge ([design](docs/copilot-reviewer-design.md), revision 6, the judgment slice): a `judgment` role may name `copilot` with `"mode": "read"` and `"boundary": "bwrap"`. The isolated launch runs under the launch boundary with only `view`, `grep` and `glob`. The answer is the final message, exactly one JSON object with no repeated key and no `NaN`. A strict parser covers the record types of the qualified contract, tool paths limited to the staged files, full reads of question, mandate and decisions, one fresh session, and no subagent or loaded MCP server. The mode runs on qualified versions only (1.0.91, qualified 2026-10-23: a judgment answered and cited its clause; an injected request to read an outside file was ignored and its file was never mounted; replay agreed); `COORDINATOR_QUALIFY_READ=1` runs a qualification, never observed. Copilot as reviewer is not implemented yet.
- The launch boundary ([design](docs/launch-boundary-design.md), L0 to L10): `"boundary": "bwrap"` on a role runs that client and all its descendants in a bubblewrap sandbox. It holds the system read-only, an empty home, the client's runtime and state, the run's own directory and the launch kind's project paths (a worker's worktree read-write with Git's metadata read-only over it; a reviewer's worktree and sealed copy read-only; a judgment's scratch only); the coordinator's store and other runs never exist inside. The client's persistent state (settings, hooks, instructions, plugins, skills, memories) is read-only, and an entry created by a run is quarantined. The environment is an allowlist; uv's cache is private; the Copilot token comes from `gh` when no variable is set and is never recorded. Mounts are validated by real path before any reservation, the record sits in the anchored `launch.json` and is checked at replay, and `doctor --boundary` checks it all without a model call. `project.boundary` adds a worker's own mounts; `worker run` and `bmad run` take `--boundary`.
- Issue #1, the BMAD 6.12 architecture Reviewer Gate ([design](docs/claude-adapter-design.md) §5, amended): a bound Claude or Copilot architecture author may run the gate's `lint_spine.py`, bound with its report written to `<workspace>/.lint-spine.json`, so the Copilot launcher refuses any other `--output` and the report lies in the audited delta. Every bound Claude or Copilot planning brief says that no subagent tool is offered, that the skills' subagent steps take their sequential fallback (each review written under `<workspace>/reviews/`, one `assumptions` entry) and that this gate is a self-check, not the independent review. `bmad discover` lists in `facts.unbound_scripts` every other `uv run` script the required skills document (6.12: `bmad-review`'s `word_metrics.py`, whose word counts fall back to estimates), before any model call.
- F24, a security fix: every Git call the coordinator makes in a worktree passes `-c core.fsmonitor=false -c core.hooksPath=/dev/null`. The inventory's `git ls-files` ran a repository-configured fsmonitor, so a worker that could write `.git/config` (an author in a main worktree, where `.git/**` is not a protected path) got the coordinator to run its command after the run, as the operator and outside every client policy. Hooks are off for `worktree add`, merges and the rest as they already were for commits. Every worker run also snapshots the worktree's Git metadata (config, `config.worktree`, `commondir`, `gitdir`, attributes, exclude, sparse-checkout, every hook, the `.git` pointer) with the D16 baseline. Any change ends the run `unknown` before the coordinator's next Git call (`integrity.json` lists the paths), so a planted filter driver, hook or exclude entry is never run or obeyed by the coordinator ([launch-boundary design](docs/launch-boundary-design.md) L0).
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
