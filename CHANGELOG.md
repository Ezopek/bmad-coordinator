# Changelog

Dates, not versions: the coordinator has no release numbers yet, and the installed package carries a `package_revision` digest instead. Each entry names the design document (under `docs/`) whose decisions it implements; the document's status line records the evidence.

## 2026-10-20

- Public documentation: the README as the entry, getting started (prerequisites, installation, the quickstart, your own repository, stops, costs, safety), concepts, troubleshooting, the example project `examples/greetings/` with its pipeline and epic input, this changelog and the contributing guide.
- The MIT license, also copied into every installed package.
- The development history (plans, live-run reports, experiments, research) left the public tree.

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
