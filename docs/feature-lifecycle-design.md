# Closing a feature: the end of a feature's lifecycle

Status: revision 2, 2026-10-26 (issue #7), implemented 2026-10-26 (plan `2026-10-26-feature-close`, project history), tested with the store and fake clients; the operator approved revision 2. Revision 1 was reviewed by Codex at the operator's request (`REQUEST_CHANGES`: two high, three medium findings, all checked against the code and accepted); revision 2 answers them, §6 maps each.

## 1. Problem

A feature can never end. `feature add` stores it `active`, `feature revise` changes the mandate but not the state, and the only other state is `unresolved`, which an import gives a feature whose worktree it cannot map. After the work is merged, every finished feature still shows as `active` in `status` next to the features that are in progress, and the only way to say "done" is a free-form decision that `status`, `drive` and `epic` cannot read. A feature whose base commit can no longer pass `doctor` (issue #6, or any upgrade, since the base check compares the committed package with the installed one) stays `active` although nothing will ever launch on it.

## 2. Decisions

**F1. Two closed states, terminal.** A feature is `active`, `unresolved` (imported, unmapped) or closed: `completed` (the outcome was delivered) or `abandoned` (it will not be). The resolution is the state itself, so `status` shows it without reading decisions. A closed feature never reopens; changing one's mind is a new feature with `feature add`. Both open states can be closed: an imported feature that was finished elsewhere needs no worktree to be closed.

**F2. The command.** `feature close <id> --token-file T` with stdin `{"expected_revision": N, "decision_id": D, "resolution": "completed" | "abandoned"}`, owner-only like every write. The decision must belong to the feature at the current mandate revision, the same rule as `feature revise`: the operator records why the feature ends (`decision add`, `choice` free-form, for example the merge commit), then closes it citing that decision. A stale revision is `REVISION_CONFLICT`; a decision of another feature or revision is `INVALID_INPUT`. Closing a closed feature with the same resolution and decision is a no-op that returns the state; anything else on a closed feature is `FEATURE_CLOSED`.

**F3. Nothing may be in flight.** In the same transaction as the state change, the close is refused (`BLOCKED_EVIDENCE`, the reason naming the records and the command that clears each) while:
- an assignment of the feature is prepared (`assignment cancel-prepared`) or holds the attempt fence (`holds_fence`: a holding attempt, or a terminal one without an observed return; `recover`, then `attempt resolve`). An imported assignment is `unknown`: the record of an attempt in the source repository, which neither holds a fence here nor can be resolved, so it does not block the close;
- a decision request of the feature is not resolved.

A driver cannot run during the close: like every owner-fenced command, `feature close` holds the coordinator's drive lock shared for the whole command, so it is refused (`OWNERSHIP_CONFLICT`) while a driver holds it exclusively, and no driver can start until the close has committed or rolled back. A drive or epic that stopped and waits for `drive resume` holds no lock; closing its feature is how the operator abandons it, and the resume is then refused (F5).

Bundles, reviews and acceptances need nothing: they are records, not work in flight.

**F4. The mandate revision does not change.** Closing does not bump `mandate_revision`, so an accepted bundle stays what it was; a bump would make every acceptance of a completed feature `stale` in `verify`. The guard is the state (F5), not revision staleness.

**F5. A closed feature refuses every write that starts or records work.** `FEATURE_CLOSED` ("Feature is closed (completed): record a new feature for further work"), checked inside the write's transaction against the feature the record belongs to:
- the feature's own records: `decision add`, `feature revise`;
- every assignment mutation, through the assignment's `feature_id`: `assignment add` (and through it every prepare of `bmad`, `story`, `worker` and the drive), the binding, `cancel-prepared`, the reservation of a launch, the launch observation, the return's anchoring and its record. After a close none of the last five has an attempt to act on (F3), and they refuse rather than rely on that;
- the evidence chain, through the bundle's `feature_id`: `bundle capture` and `bmad bundle`, `review add` and `bmad review record`, `accept` and `bmad accept`;
- `anchor_event` for the kinds whose subject or source is a feature id (`pipeline.bound`, `drive.leased`, `bmad.materialized`).

Every other drive, epic, judgment and story event is written by a driver holding the drive lock exclusively. `drive start`, `drive resume` and the epic loop check the feature's state after taking the lock and before their first write, and the close holds the lock shared (F3), so no driver event can follow a close; the events need no per-kind resolution of their feature. `bmad materialize` checks first for a clear message before any work (`story add` always creates a new feature).

**Replays and recovery.** An exact replay that changes nothing returns its earlier result on a closed feature as on an open one: `assignment add` with an existing idempotency key and the same request returns the existing id (its lookup comes before the state check, as today), and a repeated close with the same resolution and decision returns the state. A replay that would write is refused. `attempt resolve` stays allowed: it records operator evidence about a past attempt, and F3 leaves no fence holder on a closed feature to apply it to. Reads (`status`, `detail`, `verify`, `recover`, `drive status`, `epic status`, `bill`, `inbox`) are unchanged.

**F6. The event, and the file digests for export.** The close writes the event `feature.completed` or `feature.abandoned` (subject the feature, `source_id` the decision). It is a plain event like `mandate.revised`: there is no sealed record to anchor, and the event's kind carries the resolution so an import can restore it. The same transaction stores the feature's `portable_file_digests_json`, the digests of its dirty paths read from the worktree as the export reads them today (a missing file's digest is `null`), when the worktree is mapped; an `unresolved` feature keeps the digests its import stored.

**F7. Export and import keep the state without the worktree.** The checkpoint format does not change. Export takes a closed feature's file digests from `portable_file_digests_json`, as it already does for `unresolved`, so a worktree removed after the merge is never read. Import sets a feature closed when the checkpoint's events hold its `feature.completed` or `feature.abandoned`; a closed feature needs no worktree mapping, and a mapping given for one is recorded but does not reopen it.

**F8. `status` shows open features; `--all` shows every one.** `status` lists only `active` and `unresolved` features in `features` and `feature_ids`, adds `closed_feature_ids` (the newest 100, so the operator can still find them) and keeps `counts` over every record. `status --all` lists every feature in `features` with its state. `detail feature <id>` shows any feature, closed or not. `drive status`, `epic status`, `bill` and `inbox` are read-only and unchanged.

**F9. `verify` keeps every check that does not read a closed feature's worktree.** For an acceptance of a closed feature, `verify` runs the bundle copy, manifest, mandate, contract and sealed-decision checks, the open-critical-finding check and the profile gates (with no worktree, as for an `unresolved` feature, so the gates skip their worktree checks as they already do) exactly as today; only the comparison of the feature's files with the worktree is skipped, since the worktree may be gone and after a merge its branch has moved on. An acceptance that passes is reported `"current": "closed"` with `"files_compared": false`; one that fails is `stale` or `unknown` with its reason, as today. Closing a feature therefore hides no failed evidence. The aggregate keeps its order (`current` if any acceptance is current, else `unknown`, else `stale`) and is `closed` only when every acceptance is `closed`. The callers that read one acceptance (`bmad_planning`, `bmad_review`, `drive_pipeline`) run only on open features (F5).

## 3. Changes by file (sketch)

- `coordinator_core/service.py`: `close_feature`, the open-feature check in every write F5 lists, `status(all=False)`, `verify` (F9).
- `coordinator_core/exporter.py`: closed features in `_portable` and `_insert_records` (F7).
- `coordinator_core/__main__.py`: `feature close`, `status --all`.
- `coordinator_core/drive.py`, `drive_epic.py`: the state check after the lock; `bmad_snapshot.py`: the early check (F5).
- Docs: concepts ("Feature and mandate" gains the lifecycle), state-core usage (the command, the states, the event, `status --all`, `verify`'s `closed`), getting started (closing after the merge), troubleshooting (`FEATURE_CLOSED`, each F3 refusal), CHANGELOG.

## 4. Tests (fake clients and the store)

- Close after a merged, accepted planning feature: state `completed`, event with the decision, `status` hides it and lists it in `closed_feature_ids`, `status --all` and `detail` show it, `verify` reports the acceptance `closed` with `files_compared: false` after the worktree is removed.
- `verify` on a closed feature whose acceptance has an open critical finding, or a damaged bundle copy, still reports it `unknown`.
- Refusals: stale revision, another feature's decision, a decision of an older revision, a prepared assignment, a running attempt, an unobserved terminal attempt, an open decision request, a driver holding the lock; nothing changes on refusal.
- After close: each write in F5 is refused `FEATURE_CLOSED`, including a `drive resume` of a stopped drive and an `anchor_event` naming the feature; an exact `assignment add` replay returns its id; a repeated close is a no-op; a different resolution is refused; `attempt resolve` is not blocked by the state.
- Export with the worktree deleted, then import with no mapping: the feature is closed with its resolution and digests, its assignments `unknown`.
- An imported `unresolved` feature with assignments (all `unknown` after import) closes without a worktree.

## 5. Open points for the operator

- Two closed states rather than one `closed` with a separate resolution field: the state is what `status` shows, and no schema change is needed.
- No reopen, no pruning of closed features' records; both can be added later without changing this.

## 6. Review findings and where they went

| Codex finding (revision 1) | Revision 2 |
| --- | --- |
| High 1: imported assignments are all `unknown`, never releasable, so an imported feature with history could never close | F3 blocks on prepared and fence-holding attempts only; `unknown` does not block; a test with imported assignments |
| High 2: `closed` in `verify` replaced every check, hiding failed evidence | F9 keeps every check that does not read the worktree; only the file comparison is skipped; a failing acceptance keeps its state |
| Medium 3: the `anchor_event` guard misses events that name a feature indirectly | F5: the kinds that name a feature are checked; every other driver event is excluded by the drive lock and the driver's check after taking it |
| Medium 4: assignment mutations and recovery were not enumerated | F5 lists every assignment mutation; "Replays and recovery" says what stays allowed |
| Medium 5: idempotent `assignment add` replays were ambiguous | F5: exact replays return their result; writes are refused |
| The drive lock's lifetime | F3: held shared for the whole command, as for every owner-fenced command |
