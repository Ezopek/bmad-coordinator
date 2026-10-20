# Coordinator stage C: unattended continuation driver

Status: **implemented with fake clients; live hook run 2026-10-06 (three runs, all stopped by design, none reached `done`)** (2026-10-05, driver Tasks 1–3). Design amended after independent review (fix rounds 1–3) and to match the implementation. The only "proved unattended" evidence is the fake-client test pair of §10 on this host; the live hook (§10) exercised real Claude Code workers up to the by-design stops but no live run reached `done`, and no overnight run backs any claim here. Baseline `322752a`, 273 passing tests, SQLite schema 1, Python 3.11+ standard library.

This design realizes [delivery outline §5](v1-delivery-outline.md) and the driver paragraph of the [v1 design](v1-design.md), on top of [slice 3](bmad-integration-design.md). Workers launch through the closed client registry of the [Claude adapter design](claude-adapter-design.md), and driver code never names a client. **Dependency:** driver Task 1 is sequenced after adapter Task 1. Driver Task 2 consumes two pieces of adapter Task 1: its *isolated run* entry point and its post-run common-dir integrity check.

## 1. Scope

**In scope:**

- An explicitly started, bounded, foreground `drive` process that takes ownership by orderly handoff and runs one feature's declared planning pipeline from persisted state.
- Worker questions are answered only through a model-backed coordinator judgment; otherwise they are routed to the operator.
- Operator decisions and stop requests arrive through a file inbox.
- Every turn is recorded, and the driver resumes after a kill from the database.
- An operator-only command resolves a stranded unknown attempt.

**Out of scope** (R8f and slice limits):

- daemon, cron or supervision; liveness claims;
- forced takeover or quiescence proof;
- deletion, push, merge, BMAD package updates;
- a second adapter; multiple features per run;
- Loop, epics, sprint topology;
- automatic correction after review (designed 2026-10-08, bounded by `max_corrections`: [correction-round design](correction-round-design.md)); driver-made compatibility judgments;
- a general workflow engine; schema migration;
- changes to the frozen [assignment protocol](assignment-protocol.md) or to brief bytes (adapter D4).

## 2. Decisions

| # | Decision | Reason |
| --- | --- | --- |
| D1 | (R8a) `drive start` claims the owner epoch as session `drive:<lease-id>` after the attended conversation ran `release`. No takeover path is added. | Generation fencing makes the conversation's token stale. |
| D2 | `release()` succeeds only when every assignment is `cancelled-before-launch`, operator-`resolved-unknown` (D15), or terminal with an observed anchored return (the fence rule). Prepared, holding and reported-terminal rows still refuse with `RECOVERY_BLOCKED`. An observed terminal return proves the lead process ended, **not** that its descendants stopped. | The current rule makes every handoff impossible after the first attempt. Existing refusal tests stay green. |
| D3 | `<common-dir>/coordinator/drive.lock` is opened with `O_NOFOLLOW` and never unlinked. The driver holds `LOCK_EX\|LOCK_NB` for its whole run. Every owner-fenced CLI command, including `attempt resolve`, holds `LOCK_SH\|LOCK_NB` for its whole duration. Either side refuses with `OWNERSHIP_CONFLICT` when it cannot get its lock. | Fencing cannot tell two processes sharing a token apart, and a probe-only check races. The kernel drops a dead holder's lock. The lock is advisory, so the Python API is unguarded. |
| D4 | (R8b) A turn is reconcile → derive → at most one action → record. The run is bounded by `--max-turns`, `--deadline` and stop. An attempt starts only if its timeout fits before the deadline. Start and resume refuse a deadline below the largest role timeout plus 120 s. Two consecutive turns without progress end the run `failed/no-progress`. | Every kill point stays analyzable, and no bound can produce an endless zero-action loop. |
| D5 | (R8c) Outcomes are exactly `continue`, `needs-human`, `done` and `failed`, each with a closed reason code. A holding attempt is reconciled once per run and never relaunched. | Unknown stays unknown. |
| D6 | (R8d) The pipeline is a sealed, content-addressed JSON file anchored by an owner-fenced `pipeline.bound` event. Step state is **derived** from existing rows and receipts and is never stored. No migration. | A stored step state could contradict its evidence, and it adds crash windows. |
| D7 | Steps match work by binding content (skill and workspace, bundle, review binding), not by who created it. Keys of the form `drv-<pipeline12>-<step>-<n>` serve only idempotent prepares. | Attended work done before the handoff is recognized instead of duplicated. |
| D8 | (R8e) A coordinator judgment is a bounded one-attempt run through the adapter's **isolated run** entry (`run_isolated`, adapter D17): the caller supplies the cwd and the receipt directory, confinement is read-only, and there is no worktree and no attempt fence. Both directories live in a throwaway scratch directory under `<judgment-root>/<store-id>/`, where store-id is the per-store uuid4 `repository_id`. The cwd is a fresh, git-initialised directory outside the target project. During the run, the receipts stay in the scratch directory; afterwards the driver copies them into `<common-dir>/coordinator/judgments/<key>/` and anchors them. The return schema is closed: `coordinator-judgment-v1`. A valid answer becomes a `coordinator` decision that cites an authority clause. | A killed judgment strands nothing, the receipts keep it auditable, and no heuristic answer path exists. |
| D9 | Questions with `conflicts` against prior decisions never reach the judgment seam, and neither do compatibility judgments. | Settled boundaries and contract compatibility belong to the attended session. |
| D10 | (R8e) Operator input is an immutable private file in `<common-dir>/coordinator/inbox/`. Only the owner consumes it, atomically with its effect, and only at turn boundaries after the running attempt has returned. A *decision* message linked inside an attempt or judgment window is rejected with a named reason and can be re-sent once the window closes. Stop messages are exempt. Non-owners never write the database. Same-user trust is an accepted limit. | Every database write stays owner-fenced, with no migration, and a worker cannot steer its own run through the inbox. |
| D11 | A first SIGINT asks for a graceful stop: the running attempt finishes, and the driver stops at the next turn boundary. A second SIGINT, SIGTERM or SIGHUP **aborts**: the running attempt is killed and becomes unknown, the fence stays held, and the run ends `needs-human/signal-abort`. A stop message stays pending until a `drive.stop-acknowledged` event. | Abort is honest about the cost it imposes. A persisted stop survives a kill. |
| D12 | (R8g) Each run has its own directory of sealed turn records and a sealed summary, each anchored by an event. "Proved unattended" requires the dedicated fake-client test, using the production registry entries, plus its kill-and-restart twin. The live proof reuses the Claude adapter smoke. | The claim rests on evidence, not narration. |
| D13 | An open critical review finding ends the run `needs-human`, and so does an open `high` one unless the pipeline sets `accept_on_high` (follow-up after Claude smoke attempt 8, §5). Medium and low findings do not block acceptance. Automatic correction is bounded by `max_corrections` (default 0; correction-round design); once spent, this rule stops the run. | Disposing of a finding is judgment beyond R8e's question seam. |
| D14 | Ownership is released automatically only on `done`. Otherwise the token file is the driver's lease, and `release` is the orderly return. | A needs-human run can resume without a new claim, and the operator decides when to take back control. |
| D15 | The operator-only `attempt resolve ID --confirm-unknown` moves a stranded unknown attempt to the terminal state `resolved-unknown`, which releases the fence. It first records an evidence snapshot as an operator decision plus an event. It refuses an attempt whose pid was never recorded, and it refuses while the process may live. There is no automatic path, and the driver never calls it. A `resolved-unknown` assignment is terminal for its chain and is never relaunched. While it is the newest head of its step, the step derives to `needs-human/attempt-resolved`. Once the operator prepares a newer attempt for that step, derive selects the newer head and the driver proceeds. | Without it, a kill during an attempt strands the worktree forever. The cost: an unobserved descendant may still write, and that is the operator's recorded responsibility. |

## 3. Ownership, fencing and recovery

**Handoff to the driver:**

1. The conversation runs `release --token-file conv.json`; D2 must hold.
2. `drive start --token-file drive.json` claims generation g+1. From then on, the conversation's token fails every fenced write.
3. If a third session claims in between, `drive start` refuses with exit 3 and writes nothing.

**Resume.** `drive resume --token-file drive.json` continues the same generation after an exit or a kill. It finds its lease from the owner session id (`drive:<lease-id>`) and reads `drives/<lease-id>/lease.json`, which is anchored by `drive.leased`. The lease record holds the feature, the pipeline digest, the generation, the start bounds and the realpath of `--judgment-root`. On resume `--judgment-root` is optional: the sealed value is used, and a differing value is refused with `judgment-root-mismatch` (exit 2) before anything launches. If the lease record or its anchor is missing (a kill between the claim and the lease), resume refuses with `run-unbound`. The operator then runs `release --token-file drive.json` and `drive start` again.

**Handing back.** On `done` the driver releases ownership itself. After any other outcome, the operator, or the conversation with the driver's token file, runs `release`, does the attended work, and then runs `drive start` again.

**Fencing.** Every turn re-reads ownership with `read_export_snapshot(owner)`. `OWNERSHIP_CONFLICT` ends the run `failed/ownership-lost`, with no further database write or launch. The v1 reservation-to-spawn gap remains. Such an attempt still holds the attempt fence against the new owner.

The attempt fence is unchanged, and it is the reason an unknown attempt stops the run:

- `reconcile_run` runs once per run for each holding attempt of the feature.
- Anything other than an observed return yields `needs-human/attempt-unknown`, naming the holder.
- A holder from another feature in the same worktree yields `fenced-by-other`.
- `recover` is unchanged.
- `drive status` reports whether the run lock is held by reading `/proc/locks`, never by acquiring it. That shows only that some local process holds it.
- `drive start` and `drive resume` are exempt from the generic shared lock that fenced CLI commands take. They take only the exclusive lock, on their own file description.
- D2 forbids handoff while prepared rows exist, so an older-generation prepared row (which cannot run) is only cancelled and re-keyed defensively.

**Launch intent (I2).** Adapter Task 1 implements and tests this write; the driver only consumes it. Immediately after spawn, before anything else, the runner atomically writes `<run_dir>/launch-intent.json` with:

- the pid and process group;
- the process start time from `/proc/<pid>/stat` (field 22) and `boot_id`;
- the client's unique launch token: the run directory path, plus the preset session id for Claude;
- `observed_at`.

The run directory is created exclusively before any spawn.

**Operator recovery (D15).** The command is `attempt resolve ID --confirm-unknown --token-file T`, with stdin `{reason, source_ref}`. It:

1. Holds the shared lock, so it refuses while a driver runs.
2. Accepts only a fence holder (a holding state or a non-observed terminal).
3. Refuses an attempt whose run directory exists without `launch-intent.json`, because the pid was never recorded.
4. Refuses while the process may be alive. That covers two cases: a process with the recorded pid and the same start time on the same boot, and any `/proc/*/cmdline` containing the launch token. A recorded pid whose start time differs is recorded as reused, not alive.
5. Treats a reservation with no run directory as never spawned.
6. Seals `resolutions/<id>-<sha>.json`: the liveness and scan results, the current inventory digest, the delta against the latest observed inventory of that worktree, the reason and the operator reference.
7. In one transaction: inserts an `operator-reported` decision, sets `resolved-unknown` (a new state value, not a schema change, and it holds no fence), and anchors `assignment.resolved-unknown`.

Files left behind by the attempt stay unclaimed. They are never bundled as observed work.

## 4. Turn loop

```text
P0 open drive.lock O_NOFOLLOW|O_CLOEXEC; flock(LOCK_EX|LOCK_NB) or refuse (exit 3)
P1 start: bounds check, then claim(session=drive:<lease>) [txn] | resume: session -> lease.json [fence] or refuse
   run-unbound, then bounds check (deadline >= max role timeout + 120 s, from the lease's pipeline)
P2 start: seal pipelines/<sha>.json; event pipeline.bound [txn]; seal drives/<lease>/lease.json (feature, pipeline sha,
   generation, bounds, judgment-root realpath); event drive.leased [txn]
P3 seal drives/<lease>/<run>/run.json (pid, bounds, predecessor and whether it ended); event drive.started [txn]
loop
 R1 turns >= max -> continue/max-turns; past deadline -> continue/deadline; 2 turns without progress -> failed/no-progress
 R2 S = read_export_snapshot(owner)                [fence; conflict -> failed/ownership-lost]
 R3 stop flag, or a pending stop message of this generation -> stop path (below)
 R4 feature attempts in HOLDING_STATES, once per run -> reconcile_run [txn if observed]; unknown -> needs-human
 R5 pending decision messages, name order: ctime inside a closed or open window (§6) -> inbox.rejected [txn];
    else record_decision(operator-reported, source_ref="inbox/<name>#<sha256>") [txn] | inbox.rejected [txn]
 R6 steps = derive(pipeline, S, historical receipts)            (pure)
 R7 first unsatisfied step -> one action | terminal outcome; request-answerable needs-human with
    --human-wait left -> poll inbox/stop/deadline (no turn)
 A  prepare | launch | judge | decide | cancel-stale | bundle | review-prepare | review-record | accept | export
 W  seal drives/<lease>/<run>/turn-<n>.json; event drive.turn [txn]
end seal summary.json (outcome, ownership action, decisions); event drive.ended [txn] anchored to it;
    done -> release [txn]
stop path: seal summary.json; event drive.stop-acknowledged [txn] (anchored to the summary, naming every pending stop of the generation),
    which ends the run
```

Each `[txn]` is one existing owner-fenced `BEGIN IMMEDIATE` transaction with its single event. Drive events go through a closed-kind `Coordinator.anchor_event`.

A turn makes **progress** when it commits a domain event other than `drive.*` and `inbox.rejected`, when it launches an attempt or a judgment, or when an export moves the export pointer (which commits no event).

Interruption follows D11. Abort is implemented as an exception raised from the signal handler. `run_assignment`'s existing `except BaseException` path stops the process group and records `interrupted.json`. The attempt stays holding (unknown) and the run ends `needs-human/signal-abort`.

| Kill during | State left | Next step |
| --- | --- | --- |
| claim, before commit | stray token file without a claim | Resume refuses (`OWNERSHIP_CONFLICT`). The operator removes the file and runs `drive start`. |
| after the claim, before `lease.json` and `drive.leased` | owner session without a lease | Resume refuses `run-unbound`. Run `release` with the driver token, then `drive start`. |
| pipeline bind | orphan sealed file | An identical rebind is a no-op. |
| stop seen, before `drive.stop-acknowledged` | stop still pending | The next run honours it immediately. |
| inbox consume, decide | atomic | Derived from rows; no duplicate. |
| prepare | unbound prepared row | The same key finishes an identical spec; otherwise the row is cancelled and re-keyed. |
| bundle, review record, accept, after commit | committed rows | Derived as satisfied; no duplicate (`accept` is guarded by derive). |
| worker or reviewer launch, before the anchored return | `launch-pending` or `running`; fence held | reconcile → unknown → `needs-human` with the holder. Never relaunched; only `attempt resolve` frees it. |
| judgment, after the scratch directory is created, before `judgment.reserved` | `<store-id>/.tmp-<key>-*`, marked or still empty; no reservation | Orphan: the driver deletes it and builds a fresh one. |
| judgment, after `judgment.reserved`, before the rename to `<key>` | reservation plus a marked `.tmp-<key>-*` | It is renamed if its copy digests match the sealed request; otherwise it is deleted and rebuilt. Then the never-launched rule applies. |
| judgment, after the rename, before the launch record | reservation and `K/`; no record, intent or receipts | **Never launched:** preflight, then launch exactly once. |
| judgment, after the launch record `judgments/K/launching-<sha16>.json`, before the spawn | record only | **Launch unverified:** anchor `judgment.unknown` (`launch-unverified`); never launched again. A `K/receipts/` without an intent (`run_isolated` refusing an existing one is the double-launch guard) is `intent-missing`. |
| judgment, after the launch intent, before a terminal anchor | intent; no `judgment.returned` | **Kill detected:** drain the judgment's process group with the client drain (only when the intent's pid, pgid and run directory check out), then anchor `judgment.unknown`, which closes the window; the run ends `needs-human/judgment-unknown`. |
| judgment preflight failure or pre-spawn refusal by `run_isolated` (for example, a missing client binary) | reservation; no intent | Anchor `judgment.failed-preflight` with the reason, which closes the window; the run ends `needs-human/judgment-preflight`. |
| resume with a different `--judgment-root` | lease records the original root | Refused with `judgment-root-mismatch`; nothing launches. |
| after the anchor, before the record | anchored receipt | reconcile records the observed return, and the run continues. |
| export, after the publish, before the pointer commit | orphan immutable `checkpoint-r<rev>-<digest>` directory | A new revision directory is published; nothing is overwritten. |
| between the action and the turn event | missing `drive.turn` | `run.json` records the unclean predecessor. State comes from rows. |
| after `drive.ended`, before release | owner is still the driver | Resume derives `done` and releases, or the operator runs `release`. |

## 5. Pipeline

```json
{"kind": "drive-pipeline", "format": 1, "feature_id": "F", "mandate_revision": 1,
 "roles": {"worker": {"client": "codex-cli", "model": "M", "timeout": 1800}, "reviewer": {...}, "judgment": {...}},
 "budgets": {"max_updates": 2, "max_judgments": 4, "max_corrections": 1},
 "steps": [{"id": "compat", "kind": "compat"},
  {"id": "prd", "kind": "author", "skill": "bmad-prd", "slug": "spaces", "author_id": "prd-author",
   "inputs": {"docs/request.md": "driving-brief"}},
  {"id": "prd-bundle", "kind": "bundle", "of": "prd"},
  {"id": "prd-review", "kind": "review", "of": "prd-bundle", "reviewer_id": "reviewer-1"},
  {"id": "prd-accept", "kind": "accept", "of": "prd-review"},
  {"id": "arch", "kind": "author", "skill": "bmad-architecture", "slug": "spaces", "altitude": "feature",
   "purpose": "build-substrate", "prd_from": "prd-accept", "author_id": "arch-author", "inputs": {}},
  "...arch-bundle, arch-review, arch-accept...", {"id": "handoff", "kind": "export", "to": "/abs/dir"}]}
```

The step kinds form a closed set:

- **`compat`** runs discovery. It requires a supported contract and an existing coordinator compatibility decision for that fingerprint at the bound revision. Exactly one, before every other step.
- **`author`** covers `bmad-prd` and feature-altitude `bmad-architecture`, intent `create`. Its question → decision → update chain is internal and bounded by `max_updates`. `prd_from` names an accept step whose chain starts at `bmad-prd` work; it becomes the prepare's `prd_acceptance`.
- **`bundle`** (`of` an author step), **`review`** (`of` a bundle step, with `reviewer_id`), **`accept`** (`of` a review step) and **`export`** (`to`) wrap the slice-3 calls `bundle_for_workspace`, `review_prepare` plus a `reviewer`-role launch plus `record_bound_review`, `accept_bound` and `export_checkpoint`. They run no skill: a `skill` or `intent` on them is `UNSUPPORTED_STEP`. A review step needs a `reviewer` role whose client has a write-free read-only mode, and a `reviewer_id` distinct from every author of the pipeline. The export step is last, and its `to` is a normalized absolute path that, at bind and before any claim, must be an existing directory on **local ext4** (the exporter's own rule; otherwise `UNSUPPORTED_FILESYSTEM`) outside every feature writable scope, probed down to the checkpoint files it will publish.

Steps may reference only earlier steps, and each step is taken by at most one wrapper. There is one author step per skill and slug.

Refusal codes:

- `UNSUPPORTED_STEP` (exit 4; `main()` adds it to the exit-4 set) for an unknown kind, skill or intent, an unknown client, a reviewer or judgment client without a write-free read-only mode, `max_judgments > 0` without an authority list (§7), and anything outside the slice: epics, sprint status, Loop, `bmad-spec`, `bmad-ux`, `validate`, `intent: update`, branches.
- `INVALID_INPUT` (exit 2) for a malformed shape, types, references or budgets.

A refused pipeline writes nothing.

Derived states are `pending`, `ready(action)`, `in-flight`, `waiting-decision`, `satisfied` and `needs-human(reason)`.

- **Author step.** Its *head* is the newest non-cancelled bound planning assignment with the step's skill and workspace at the current mandate revision, ordered by the revision of its `assignment.prepared` event. This holds whoever prepared it and across separate roots, because an update is always prepared after its predecessor.
  - The step is satisfied when the head is an observed `completed-claim` with verdict `OBSERVED_COMPLETE_CLAIM` or `OBSERVED_PARTIAL_CLAIM`. A blocking review of its newest bundle (critical, or `high` without `accept_on_high`) with no acceptance and corrections left makes the step prepare a correction update (`corrects_review`, `finding_ids`), with the chain's inputs and PRD acceptance (as its create bound them; the head itself may bind none) and the chain's resolving decisions, instead of staying satisfied; `max_corrections` (optional, default 0, at most 4) counts the step's bound correction attempts at the current revision, whoever prepared them; correction attempts do not consume `max_updates`, while a correction's own question continuation does ([correction-round design](correction-round-design.md)).
  - A `resolved-unknown` head is terminal for its chain, so the step derives to `needs-human/attempt-resolved` and the driver prepares nothing. Once the operator prepares a newer create or update for that step, the newer head wins and the run proceeds; it never loops on `attempt-resolved`.
- **Bundle step.** Satisfied when a bundle of that workspace at the current revision names the head; otherwise `bundle`.
- **Review step.** Satisfied by a recorded observed review of the bundle. Otherwise its head is the newest non-cancelled review-bound assignment of that bundle, ordered like an author head: `review-prepare` (key `drv-<pipeline12>-<step>-<n>`; a half-finished one is finished with its key or cancelled and re-keyed), `launch` with the `reviewer` role, or `review-record` for an observed completed return. A blocked or undecided reviewer is `reviewer-blocked`; holding, resolved and unobserved heads route as for author steps.
- **Accept step.** Satisfied when an acceptance of its bundle exists and `verify` reports it current; a non-current one is `acceptance-stale` and is never redone. With no acceptance, an open critical finding on the bundle is `critical-finding` (naming the review). Otherwise, unless the pipeline sets the optional top-level `accept_on_high: true` (default `false`, sealed with the pipeline and recorded in `lease.json`), any open `high` item of the review to be accepted is `open-high-findings`, with `finding_ids` as JSON pointers into the recorded review view (`/findings/<i>`; a review finding carries no disposition, so it stays open; `/dispositions/<i>` for a carried item disposed at `high`), and no acceptance is made. In both cases the author step corrects while `max_corrections` lasts (correction-round design); once spent, the stop carries `corrections: {used, max}`. Otherwise `accept`. Severity is the reviewer's judgment (Claude smoke attempt 8 rated a defect tied to a decided rule `high`), so the default stops. The attended `bmad accept` keeps the critical-only rule and lists the review's open high findings in its result (`open_high_findings`).
- **Export step.** Satisfied when `export_revision` is at or after the last *domain* event of the feature (any event whose subject is the feature or one of its assignments, decisions, requests, bundles, reviews or acceptances, other than `drive.*`, `pipeline.bound`, `judgment.*`, `inbox.rejected` and `owner.*`) and the `checkpoint-r<rev>-<digest>` directory exists under `to`. Later driver and owner events leave `export_pending=1` by design and do not make the export stale. An export turn commits no event, so it counts as progress by its moved pointer.

**Story pipelines (implementation drive, 2026-10-11).** The kinds `setup`, `implement`, `commit`, `verify` and `status`, the `project` block (required for a story pipeline, refused for a planning one) and the stop reasons `verify-env`, `worktree-drift`, `status-shape`, `correction-budget` and `product-decision` are specified in the [implementation-drive design](implementation-drive-design.md); there `bundle` may take a `verify` step. `max_corrections` is capped at `MAX_CORRECTIONS = 6` for a story pipeline only; a planning pipeline keeps the cap 4, so a sealed planning pipeline validates, digests and derives unchanged. The story receipts are anchored by the driver events `story.setup`, `story.baseline`, `story.committed`, `story.verified` and `story.status` under the anchor top `story/`, so, like every driver event, they never make an export stale.

**No new table.** A `pipeline_steps` table would need schema 2, because `_check_state` rejects other versions, plus a migration rule. A kill could also leave it contradicting assignments, receipts and acceptances. The derivation already *is* the reconciliation. The only new persisted items are content-addressed files under the common directory, anchored the same way bindings are. Binding a different pipeline at the same mandate revision is `IDEMPOTENCY_CONFLICT`. A mandate revision requires a new binding (`mandate-revised`).

## 6. Needs-human routing and inbox

**Reason codes.** The closed set:

- `question-refused`, `question-conflict`, `judgment-budget`, `judgment-unknown`, `judgment-preflight`, `update-budget`;
- `attempt-unknown`, `attempt-resolved`, `fenced-by-other`, `unobserved-return`;
- `worker-blocked`, `reviewer-blocked`;
- `compat-missing`, `compat-review`, `contract-drift`;
- `critical-finding`, `open-high-findings`, `acceptance-stale`, `stale-input`, `mandate-revised`;
- `operator-stop`, `signal-stop`, `signal-abort`.

The failure reasons are `ownership-lost`, `no-progress`, `corrupt-state` and `internal-error`. The pre-run refusals are `run-unbound`, `bounds-insufficient`, `judgment-root-required` and `judgment-root-mismatch`.

Each outcome names its subject ids and one operator action:

- the question and judgment codes (except `update-budget`): an inbox decision for request R. For `judgment-preflight`, fix the client and resume instead;
- `attempt-unknown`: `worker reconcile`, investigation and, only if needed, `attempt resolve`;
- `attempt-resolved`: either `bmad prepare` a new `create` or `update` attempt for step X, using the driver's token while no driver runs, then `drive resume`, or drop the step or feature by revising the mandate and binding a new pipeline. The resolution evidence lists the leftover files. A new `create` needs an empty workspace, and clearing it is the operator's decision;
- `open-high-findings`: either an attended `bmad accept` of review R (it lists the open high findings) with the driver's token while no driver runs, then `drive resume`, which finds the acceptance; or `release`, an attended correction and re-review of bundle B, then `drive start` (`corrections` counts the step's correction attempts at this revision, driver-made or attended);
- all other codes: `release`, then attended repair, then `drive start` (for `critical-finding`, `corrections` counts the step's correction attempts at this revision).

**The inbox** is the directory `<common-dir>/coordinator/inbox/`, mode 0700.

- **Writing.** `inbox add` refuses `UNINITIALIZED` when no store exists; it never creates the coordinator directory, so a fresh clone can still `import`. It writes canonical JSON to a private temp file, fsyncs it, links it exclusively to `<created_ns>-<sha256[:16]>.json` and fsyncs the directory. Files are never edited or deleted.
- **Message fields.** Every message has `{kind, format, type: decision|stop, repository_id, feature_id, generation, reason, source_ref}`; a decision adds `{request_id, mandate_revision, choice}`.
- **Stop scope.** A stop applies to the owner generation current when it was written. A stop from an older generation is rejected as stale. One acknowledgement covers every pending stop of the generation.
- **Validation.** The owner re-validates at consumption: a regular private file of this uid, the digest, the repository, the feature, an open request, the current revision and the ctime window.
- **Consumption.** It is atomic with its effect. "Consumed" for a decision derives from the decision's `source_ref` (`inbox/<name>#<sha256>`). For a stop it derives from `drive.stop-acknowledged`, and for a rejection from `inbox.rejected`. Provenance is always `operator-reported`.
- **Precedence and override.** An operator answer present at a turn boundary preempts any unused judgment. An answer that arrives after R was already resolved is rejected with `request-not-open`, which `inbox list` shows. To override a recorded decision: stop, `release`, then an attended superseding decision or mandate revision.
- **Timing.** Messages are consumed only at R3/R5 or during a human wait. A **window** runs from the launch-intent write to the terminal classification, for both worktree attempts and judgments of the driven feature. The writer cannot set ctime.
  - **Attempt window:** closes when the observed return is recorded or at `attempt resolve`.
  - **Judgment window:** starts at the sealed request's `reserved_at`, which is never later than the launch intent. It closes at the first terminal anchor: `judgment.returned` (observed, or unknown such as a timeout), `judgment.unknown` (kill detected), or `judgment.failed-preflight`. Judgments hold no fence. A killed key is classified at the start of the next run (R4j also runs once at run start), so its window closes then even when a stop or a revised mandate ends that run. A reserved key that never launched stays open until it launches or is classified `failed-preflight` (its request closed or its mandate changed), so a run that ends at a stop or a bound before launching it keeps rejecting inbox answers for that request.
  - **Rejection.** A *decision* message whose `st_ctime` falls inside a window, or inside a window still open at consumption time, is rejected with `written-during-attempt`. `inbox list` shows it with the window's subject. Re-send it with `inbox add decision` (same body, new file) once `drive status` shows the window closed; the new message is consumed at the next boundary.
  - **Stop messages are exempt.** A stop written during an attempt is legitimate and is acted on at the next boundary.

**Known limit (accepted): same-user trust.** The inbox has no table, because that needs a migration and unfenced writes. It has no signature, because a key readable by the same uid adds nothing. Defenses:

- client confinement;
- the existing brief prohibitions: planning briefs forbid writing outside the writable directory, and review briefs forbid writing at all;
- the ctime window above;
- the adapter's post-run common-dir integrity check (adapter Task 1), which makes an attempt that wrote the common dir unknown.

Adapter D16 excludes `inbox/`, lock files and additions, which operators may legitimately write during an attempt. The ctime rule handles decisions in that directory instead.

## 7. Coordinator-judgment seam

**When a judgment runs.** For an observed `needs-decision` author head with open questions, no conflicts, an open request, no pending operator answer, and budget left.

**Where it runs.** `--judgment-root` is required when `max_judgments > 0`; without it, bind refuses with `INVALID_INPUT` `judgment-root-required`. The root must be ext4.

The driver validates the root **before reservation**, using the same rule as `run_isolated` (adapter D17). The rule relied on:

> refuse a cwd inside the **target** repository's worktrees (`git worktree list`) or common dir, or a cwd that is itself a worktree of that repository; an unrelated ancestor repository is not refused (doctor may warn).

A root inside an unrelated repository is recorded in `run.json` as a warning (`judgment-root-in-repository`). Production should use a root outside every repository.

Layout for key K:

```text
<judgment-root>/<store-id>/K/
  .coordinator-judgment   marker: repository_id, K
  cwd/                    git-initialised; no AGENTS.md or CLAUDE.md; copies only
  receipts/               run_isolated receipt_dir; not inside cwd/, not in the common dir
```

`cwd/` receives copies only:

- `question.json`, verbatim and numbered;
- `mandate.json`, the mandate at the bound revision;
- `decisions.json`, with provenance;
- the cited step artifacts;
- `brief.md`.

**The brief** marks the question and the artifacts as **untrusted data**. It states the rule: answer only where a delegated authority clause authorizes the choice, otherwise refuse; never present an answer as operator approval; never widen scope; read only this directory.

**Reservation and run.**

1. The driver builds the scratch directory under the temporary name `<judgment-root>/<store-id>/.tmp-K-<random>/`, marker first. The key K is `drv-<pipeline12>-<step>-j<n>`, matching `bmad_planning.KEY`. The store-id subtree keeps keys from different repositories apart.
2. It seals `<common-dir>/coordinator/judgments/K/request-<sha16>.json`, which lists the digest of every copy plus `{request_id, question_sha256, mandate_revision, pipeline_sha256, step_id}`. Unanchored request files are ignored.
3. It anchors `judgment.reserved`.
4. Only then does it rename the scratch directory to `<judgment-root>/<store-id>/K/`.
5. It calls `run_isolated(client, cwd=K/cwd, receipt_dir=K/receipts, ...)` with pipeline role `judgment`.
6. Afterwards it copies the receipts into `<common-dir>/coordinator/judgments/K/receipts/`, verifies their digests, and anchors `judgment.returned`, observed or unknown.

Each key is launched at most once. During the run, the receipts never live in the common dir.

**Resume state.** The judgment's state comes from anchors, the launch intent and the unanchored launch record, never from directory shape alone. For each reserved key without a terminal anchor:

- **No launch intent, no `K/receipts/`:** never launched. Run preflight, then launch exactly once.
- **No launch intent, `K/receipts/` present:** `run_isolated` would refuse it (the double-launch guard). Classify it unknown (`intent-missing`).
- **Launch intent present:** kill detected. The judgment's process group is drained with the client drain, and the result is sealed with `judgment.unknown`.
- **A launch record present without an intent or `K/receipts/`:** the driver seals `judgments/K/launching-<sha16>.json` just before it calls `run_isolated`, so a key whose scratch was deleted after the launch is classified unknown (`launch-unverified`) and never relaunched.
- **Preflight failure or pre-spawn refusal by `run_isolated`:** anchor `judgment.failed-preflight`.

Every unknown or failed judgment ends the run `needs-human` (`judgment-unknown` or `judgment-preflight`), and the request stays open for an inbox answer. There is no automatic replacement.

**Orphan scratch.** On resume, the driver uses `lstat` and never follows a symlink. It looks only inside its own `<judgment-root>/<store-id>/` subtree and never leaves it. There, as an extra check, it deletes a directory only if it meets one of these conditions:

- it is named `.tmp-*`, holds a marker naming this repository, and its key has no anchored reservation;
- it is an empty `.tmp-*` left by a kill before the marker was written;
- it is a marked `.tmp-<key>-*` superseded by `K/`.

This is coordinator scratch outside any project and never user data, and these are the driver's only deletions. A reserved key with no `K/` directory is restored from its marked `.tmp-<key>-*` when the copy digests match the sealed request; otherwise it is rebuilt from the request. Completed scratch directories are kept; retention is a deferred observation.

Client-level user instruction files may still load. Whether they do is decided by the isolated entry, and it is unverified (§12).

**Return schema.** New judgments are offered `coordinator-judgment-v2` ([judgment v2 design](judgment-v2-design.md), 2026-10-07): `{schema: "coordinator-judgment-v2", judgment_key, request_id, verdict: answer|refuse, answers: [{index, text, clause: {authority_id, field, pointer, quote}}], rationale, refusal}`. Requests sealed earlier as `coordinator-judgment-v1` (`{judgment_key, request_id, verdict, answers: [{index, text}], clause, rationale, refusal}`, one clause for every question) keep their rules: the `schema` sealed in the request record selects them, nothing is migrated, and a v2 payload whose `schema` literal differs from the request's is the defect `schema-mismatch`. A schema violation, a wrong key or missing terminal evidence makes the judgment unknown. Semantic checks never raise. For every clause (the one v1 clause, or each v2 answer's clause, whose defect codes are suffixed `@<index>`) they require that:

- `authority_id` is the `id` of an entry `i` in the mandate's authority list (`authority.delegated: [{id, text}]`); pipeline bind refuses judgments when that list is absent;
- `field` is `authority`, and `pointer` is exactly `/delegated/<i>/text` (RFC 6901), the text of that same entry at the bound revision; a clause that cites any other field, another entry or another member (such as the `id`) is a defect;
- `quote` is a verbatim substring of that entry's text, at least 24 characters long, or the entry's whole text when it is shorter;
- every question index is answered exactly once (a per-request rule; `empty-answer`, `unknown-index`, `missing-index` and `duplicate-index` are never suffixed).

A failed check sets `valid: false`, which the driver treats as a refusal. Under v2 one clause may authorize several answers and two answers may cite different clauses; a request with any uncovered question is refused as a whole, and the refusal text (which the brief asks to name the covered and uncovered indexes) is carried, truncated to 400 characters, as the `question-refused` subject `refusal`, shown by `drive status`.

**Recording.** A valid answer becomes a decision with `source_kind: coordinator`, the rationale plus key as `reason`, the `request_id` and `choice.judgment: {key, receipt_ref, receipt_sha256, quality: "model-claim"}`. v1: `source_ref: mandate:r<rev>:<authority_id>:<field><pointer>;receipt=<ref>#<sha256>` and `choice: {answers, clause, judgment}`. v2: `source_ref: mandate:r<rev>:<id1>+<id2>+…:authority;receipt=<ref>#<sha256>` (the distinct cited ids, ascending) and `choice: {answers (with their clauses), clauses: [{authority_id, field, pointer}], judgment}`. `summary.json` lists every such decision with `authority_id` as the source_ref authority segment (`A1`, or `A1+A2`). The slice-3 update continuation follows.

**Bounds.**

- An observed verdict is final; a restart never re-asks.
- An unknown or failed-preflight judgment is never replaced automatically; the operator answers through the inbox.
- `max_judgments` counts every launch.
- The role timeout and the deadline apply.

**Model claim.** The code proves only that a distinct process returned an anchored, structured verdict citing an existing authority entry and mandate text. It does not prove that the clause authorizes the answer, or that the answer is right.

## 8. Status and exits

| Outcome | Exit | Meaning | Ownership |
| --- | --- | --- | --- |
| `done` | 0 | Every step is satisfied and the export is current. | Released if D2 holds; otherwise retained, with the reason. |
| `continue` | 10 | The next step needs no human; the run stopped on `max-turns` or `deadline`. | Retained |
| `needs-human` | 11 | A §6 reason, including stops and aborts. | Retained |
| `failed` | 12 | `ownership-lost`, `no-progress`, `corrupt-state` or `internal-error`. | Unchanged |

Gate errors (`STALE_INPUT`, `BLOCKED_EVIDENCE`, `UNKNOWN_EVIDENCE`, `INVALID_REPOSITORY`, `UNSAFE_PATH`, `UNSUPPORTED_CLIENT_MODE`, `CHECKPOINT_CONFLICT`, `INVALID_CHECKPOINT`) map to `needs-human` (`stale-input`, or `compat-review` for `UNSUPPORTED_CONTRACT`) with the gate code and text; any exporter error at the export step does too, including `UNSUPPORTED_FILESYSTEM`. Only `OWNERSHIP_CONFLICT`, corrupt state and unexpected errors are `failed`. Pre-run refusals keep exit codes 2-5 and write nothing.

The documented shell loop `drive-loop --max-restarts N` (in the usage docs) works like this:

- It restarts on exit 10.
- After a death by signal (exit > 128), it restarts only if `drive status` shows the last run without an end event.
- It stops on any other exit and once N restarts are exceeded.

`summary.json` records:

- counts by role and source kind, `corrections`, step states, the ownership action, the coordinator decisions, and `restarts`;
- typed `interventions`: `inbox-decision`, `inbox-stop`, `signal`, `attempt-resolve`, `attended-generation` (a claim between runs without `drive.started`), and `resume-after-needs-human`.

## 9. CLI and installed package

| Command | Mode |
| --- | --- |
| `drive start --feature F --pipeline FILE --token-file T [--max-turns N] [--deadline S] [--human-wait S] [--judgment-root DIR]` | Claims ownership, binds the pipeline and runs; prints the summary. `--judgment-root` is required when `max_judgments > 0`. |
| `drive resume --token-file T [bounds] [--judgment-root DIR]` | Runs the owner session's lease. The judgment root comes from the lease; a different value is refused with `judgment-root-mismatch`. |
| `drive status [--feature F]` | Read-only; creates nothing. Reports steps, the lock, the last outcome, the inbox and open requests. |
| `inbox add decision` / `inbox add stop` | stdin JSON; no token; writes one file only. |
| `inbox list` | Read-only: `pending`, `consumed` or `rejected`, with the reason. |
| `attempt resolve ID --confirm-unknown --token-file T` | Operator only; stdin `{reason, source_ref}`; holds the shared lock. |

`--human-wait` is kept, because it is the only way an operator answer can reach a running driver without a restart.

The installed package gains:

- `drive.py`, `drive_pipeline.py`, `inbox.py` and `judgment.py` in the manifest allowlist;
- these commands through `.bmad-coordinator/run.py`;
- the launch-intent file, the `resolved-unknown` state and the D2 rule.

Unchanged: the schema, `AGENTS.block.md`, brief bytes and the assignment protocol.

## 10. Testing strategy

Tests use disposable ext4 repositories with the slice-3 synthetic BMAD tree, and an ext4 judgment root created under the test temp tree, which is passed to child-process drivers with `--judgment-root` (allowed by the §7 rule), and fake client binaries driven through the **production** registry entries, including the isolated entry. The fake can block on a FIFO. Kills are real `SIGKILL`s of a child driver at named hook points. The `after-anchor` point is reached by wrapping `Coordinator.record_return` in the child process.

Every row of the §4 matrix, each lock and signal case, both `attempt resolve` liveness cases, and the shell loop has a named test in the plan (`tests/test_drive.py`, `tests/test_drive_judgment.py`, `tests/test_drive_e2e.py`). Fake clients prove coordinator logic only.

**Proved unattended (fake clients, this host).** `test_proved_unattended_run_operator_absent` runs a real child `drive start` through the production registry entries, including the isolated entry, with fake binaries on `PATH` and no registry injection: PRD create (one open question) → in-mandate judgment → `coordinator` decision → update → bundle → bound review → acceptance → architecture from the accepted PRD → bundle → review → acceptance → export, with no inbox message, no stdin and no test action after the start. `test_proved_unattended_kill_and_restart_continuation` runs the same scenario under the documented `drive-loop`, SIGKILLs the child on the first turn after the judgment decision, and the loop resumes it to `done` with one restart. Both score every `unattended_checks` criterion (`tests/drive_support.py`) `pass`, except that `no-duplicate-after-restart` is `trivial` in the run without a restart and `pass` in the twin, and `correction-observed` is `trivial` in both (no review blocked): the claimed generation, an observed worker completion, a judgment-backed decision through the isolated entry, an observed update, an observed bound review, current acceptances, zero interventions and an empty inbox, digest-valid driver anchors, one launch per key, no duplicate across runs. `correction-observed` scores the correction round (correction-round design D7) and is `trivial` when no review blocked.

**Live hook (R8g; run 2026-10-06).** The later Claude adapter smoke reuses this procedure; this plan makes no model call. The procedure below is unchanged; the results follow it:

1. **Fixture.** A clean detached worktree of the commit under test, with the full suite green there, supplies `install.py` and the package. A disposable repository on local ext4 is built as in the slice-3 probe and the Claude smoke fixture (client-adapters report, attempt 5 setup): installed package, BMAD skills and `_bmad` configuration copied read-only, a committed base, a feature worktree, and `doctor --base BASE --client claude-code` ready. Nothing runs in a real project.
2. **Attended prework** (operator present, then gone): `init`; `claim --session attended --token-file conversation.json`; `feature add` with stdin `FeatureInput` (`coordinator_core/model.py`): `{outcome, exclusions, constraints, completion, authority: {"delegated": [{"id": "M1", "text": "<a clause that delegates exactly one point the brief leaves open>"}]}, branch, base_ref, base_commit, worktree, dirty_paths: [], writable_scopes: ["_bmad-output/planning-artifacts/features"]}`; `bmad discover`, then `decision add` with `{feature_id, mandate_revision, source_kind: "coordinator", source_ref: "bmad-fingerprint:<sha>", reason, choice: {"bmad_compat": "<sha>"}}` ([usage](state-core-usage.md#bmad-planning-and-review)); `release --token-file conversation.json`.
3. **Pipeline.** The usage-doc example with every `roles.*.client = "claude-code"`, bounded role timeouts, `max_updates: 2`, `max_judgments: 1`, and `to` a fresh directory on local ext4 (for example under `/var/tmp`, not tmpfs `/tmp`) outside the fixture's writable scope. The usage-doc example now also carries `max_corrections: 1`; runs 1–5 ran without it, when the pipeline bounded launches to at most 3 workers, 2 reviewers and 1 judgment. A correction adds one worker launch per author step and one reviewer launch per review step.
4. **Run, operator absent.** `drive start --feature F --pipeline pipeline.json --token-file drive.json --judgment-root /var/tmp/<fresh ext4 dir outside every repository> --max-turns 40` with stdin closed and no inbox message; record `drive status --feature F`.
5. **Score.** From the source worktree's `coordinator/`: `PYTHONPATH=. python3 -c 'import json, sys; from pathlib import Path; from coordinator_core.repo import resolve_repo; from tests.drive_support import unattended_checks; print(json.dumps(unattended_checks(resolve_repo(Path(sys.argv[1])), sys.argv[2:])))' <feature-worktree> <lease-id>...`. Each criterion is `pass`, `fail` or `blocked`, and `no-duplicate-after-restart` is `trivial` without a restart (`correction-observed` likewise when no review blocked); a missing client capability (for example the judgment schema refused, giving `judgment-preflight`) is `blocked`, never patched. A critical review finding ends the run `needs-human/critical-finding` by design and leaves the acceptance criterion `blocked`.
6. **Kill-and-restart variant** in a second fresh fixture: the same steps under the usage doc's `drive_loop --max-restarts 3`, with the `start` run executed by the test harness text (`tests/drive_support.HARNESS`, written to a file and run with `DRIVE_SOURCE=<source>/coordinator` and `DRIVE_HOOK_LOG`): that run imports the source tree of the commit under test, while step 4 and the `resume`/`status` runs use the fixture's installed `.bmad-coordinator/run.py` from the same commit. The intended kill point is the first `after-turn-event` after the `decide` action. `DRIVE_KILL_AT=after-turn-event:5` lands there only when the create asks exactly one question, so record from the hook log which action turn 5 took and score the variant against that. Then score again and record `restarts` from the final summary.

**Live hook results (2026-10-06, code `38fcb3b`).** Three unattended `drive start` runs with real Claude Code workers (every role `claude-code`), zero interventions after the start. No code change was needed. Step 6 (kill-and-restart) was not run, because no run ended `done`.

| Run | Authority | Stop | Turns | What happened |
| --- | --- | --- | --- | --- |
| 1 | `accept_on_high` false; one clause `LZ1` | `needs-human/open-high-findings` | 10 | create, in-mandate judgment (`LZ1`, verbatim quote, coordinator decision), update, bundle, observed bound opus review (2 high), stop by design. `unattended_checks`: 9 pass, `acceptance-current` blocked, `no-duplicate-after-restart` trivial. |
| 2 | `accept_on_high` true; `LZ1` | `needs-human/question-refused` | 3 | The author asked 3 questions; `LZ1` covers one, so the judgment refused correctly. |
| 3 | `accept_on_high` true; `LZ1`–`LZ4` | `needs-human/question-refused` | 3 | 4 questions in one request. The judgment mapped OQ-1 to `LZ1`, OQ-2 to `LZ2` and `LZ3`, OQ-3 to `LZ4`, found OQ-4 (success metrics) uncovered, and refused. |

Totals: 16 turns, 8 model calls, $1.10 client-reported; five-hour window 73% to 78%, no 429. Evidence: the live-hook report and the driver report in the project history.

**Design finding.** The judgment seam answers one request with one `authority_id` and one quote (§7), but a worker puts all its questions in one request, so a multi-question request cannot be answered clause by clause. Splitting the mandate into clauses does not help, and a higher `max_judgments` does not either, because a refusal is final. An unattended `done` therefore needs one broad clause or `accept_on_high` plus an author that asks one question, until judgment schema v2 (§12). Authors asked 1, 3 and 4 questions from the same inputs, so wording the mandate cannot guarantee coverage. Resolved on 2026-10-07 by judgment schema v2 ([design](judgment-v2-design.md)), which cites one clause per answer and is proved with fake clients (`MultiQuestionTests`); its live verification is recorded in that design's status line.

**Observations.** Workers mark their own questions as "covered by LZ1" (the worker-steered judgment limit of §11, seen harmlessly: every refusal held). This opus reviewer rated RFC 8259-related defects high, not critical, so a default pipeline stops at highs. Claude runs load 3 built-in plugins and 17 skills that the isolation gaps do not record. Evidence caveat: the retained scrubbed copies of `launch.json`, `doctor.json`, the export listing and the command log no longer match their recorded digests; each run's `scrub-log.txt` lists the pre-scrub digests.

**Live hook results (2026-10-07, code `e89d84c`, judgment schema v2).** Two more unattended `drive start` runs, zero interventions, no code change needed. Run 4 (every role `claude-code`, `LZ1`–`LZ4`, `accept_on_high: true`): the author asked 4 questions in one request; the v2 judgment answered all four, each citing its own clause, and one coordinator decision `mandate:r1:LZ1+LZ2+LZ3+LZ4:authority` resolved the request; update, bundle and a bound opus review followed; the review rated one finding critical (the PRD's decimal grammar admitted `007.5`, contradicting the recorded `LZ1` answer), so the run stopped `needs-human/critical-finding` after 10 turns; `unattended_checks` 8 pass, `acceptance-current` blocked. Run 5 (judgment on `codex-cli`, `--max-turns 4`): the same shape of request answered under v2 through Codex `--output-schema`, one decision, stopped by the turn cap. Step 6 was not run (no `done`).

**Live hook results (2026-10-08/09, code `06bb054`, correction round).** Two more runs with run 4's shape plus `max_corrections: 1`. Run 6 (`accept_on_high: true`): the first unattended `done` on real clients, 19 turns, worker 3 / reviewer 2 / judgment 1, zero interventions, export published, `unattended_checks` 9 pass and 2 trivial (both opus reviews rated highs only, so the correction never triggered). Run 7 (`accept_on_high: false`): after a refused five-question request (inbox decision) and a worker killed by the account's 429 session limit (`worker reconcile`, `attempt resolve`, an attended re-prepare), the resumed run recorded a critical review of the PRD's decimal grammar, prepared the correction (brief with the frozen input, the recorded decision and the finding), launched it, re-bundled, re-reviewed (no critical, three highs) and stopped `open-high-findings` with `corrections: {used: 1, max: 1}`; `correction-observed` pass, `zero-interventions` and `no-duplicate-after-restart` fail by the documented recovery. Run 8 (step 6, the kill-and-restart variant): the `start` run through the test harness with `DRIVE_KILL_AT=after-turn-event:5` under the usage doc's `drive_loop --max-restarts 3`; SIGKILL after turn 5 (the prepared update), one restart, the resumed driver launched the update under the same key and reached `done` after 14 more turns, zero interventions, all ten criteria pass.

Copilot smoke 6 (2026-10-09): the same driver with `worker: copilot` reached `done` with two driver-prepared corrections on the architecture chain and two inbox decisions (an out-of-mandate question, then a conflict the worker raised against the first answer).

Live hook 9 (2026-10-09): the correction round on Codex. Run 7's shape with the author and the judgment on `codex-cli` (`gpt-6-astra`) and the reviewer on opus, `max_corrections 2`: the Codex judgment answered the PRD's four questions; the opus review rated one high; the first Codex correction asked a question outside every clause after the single judgment was spent, so the driver stopped `needs-human/judgment-budget` (first live occurrence; recovered by the documented inbox decision and `drive resume`); the second correction cleared a critical and a high, and the PRD was accepted under `accept_on_high: false`; the architecture's question hit the spent budget again (second inbox decision); `done` after 35 turns, 8 Codex launches and 4 opus reviews (USD 2.71); `unattended_checks` 10 pass, `zero-interventions` fail. Observation: `max_judgments` counts every judgment launch of the drive across steps, so a budget of 1 serves one step's questions only.

Not proven live: a zero-intervention `done` that includes a correction (run 9's needed two inbox decisions), the `update-budget` path, overnight operation, a Codex reviewer of Claude-authored work.

## 11. What this slice does not prove

This slice does not prove any of the following:

- liveness or overnight operation, since there is no supervisor;
- judgment quality or in-mandate correctness;
- that any model read or understood anything;
- the operator identity behind an inbox file or an `attempt resolve`, which cannot tell the operator from the attended model conversation;
- that workers cannot reach the common directory;
- quiescence of descendants, before or after `attempt resolve`;
- live client behavior or Claude Code support;
- multi-feature or cross-clone safety.

**Explicit limit: worker-steered judgment.** A judgment reads worker-authored question text and artifacts, so a worker can try to argue its own answer. The untrusted-data marking, the authority-id requirement and the minimum quote length narrow this risk but do not remove it. Each resulting decision is listed for review.

**Explicit limit: reviewer-steered correction.** A correction brief carries a reviewer's recorded finding text to an author, so a hostile or wrong finding can steer the correction, the mirror of worker-steered judgment ([correction-round design](correction-round-design.md) D8, §7.3). The brief marks the items as the reviewer's claims, not decisions, and JSON-quotes their text (the `location` locator stays a backticked raw string); the mandate, the recorded decisions and the `open_questions` rule bound it, nothing else does. Both reviews stay recorded.

**Follow-ups (explicitly not in scope):**

- ~~a bounded automatic correction round after critical findings, within mandate~~ designed 2026-10-08 ([correction-round design](correction-round-design.md));
- driver-made compatibility judgments;
- late sealing of a killed attempt's return;
- operator-decided pipeline supersession;
- multi-feature runs;
- supervision and overnight liveness.

### Deferred review minors

- **M4 (pipeline supersession without a mandate revision).** This needs an operator-decided supersession rule and derive semantics for superseded steps; it is a follow-up.
- **M7 (`/dev/tty` confirmation for `attempt resolve`).** The confirmation was ruled to be a flag. A TTY prompt would break scripted operator use, so the limit is named in §11 instead.
- **Task 1 M5 remainder.** The sealed summary records `"ownership": "released"` when D2 holds under `LOCK_EX`, before `release()` runs; `owner.released` is the fact. A release that fails afterwards prints `retained` with `end_error`, and `drive resume` derives `done` and releases (tested).
- **Task 1 M6 remainders.** `service._fence_holder` (the `reserve_launch` authority) still re-codes `holds_fence`, and `worker._proc_stat` duplicates the `clients` start-time reader because it also needs the state letter. Both are identical today, so only a drift risk; they sit outside the driver's files.

## 12. Residual uncertainty

1. D2, like the fence, trusts an observed terminal return to mean the lead process ended; descendants stay unproven.
2. A kill between the spawn and the launch-intent write, or between the run-directory creation and the spawn, strands the worktree permanently: `attempt resolve` refuses a pid that was never recorded. That includes attempts launched before adapter Task 1, whose run directories have no intent. Liveness scans are point in time, and a detached descendant with a different cmdline escapes them.
3. `flock` and SQLite locking are trusted only on local ext4, as probed.
4. Same-user inbox trust remains. Its residual is a message that escapes both the ctime window and the integrity check. The windows compare wall-clock times, and WSL2 can resync its clock after host sleep.
5. Whether each client accepts the judgment schema, and whether the isolated entry suppresses user-level and ancestor instruction files. Tests deliberately place the judgment root inside an unrelated repository.
6. By design, a long human wait holds ownership and blocks attended work.
7. The judgment launch record is unanchored by ruling. Deleting both `K/` and `judgments/K/launching-*` re-enables a relaunch of that key, and a judgment still alive when `K/` was deleted cannot be drained (its intent was inside `K/`).
8. The quote rule binds the cited authority entry, not its relevance: any 24-character span of the entry passes, including a boilerplate prefix. The model-claim label covers this.
9. Carried from Task 2 (accepted): a kill inside the judgment marker's atomic publish can leave a `.tmp-*` holding only a dot-staging file, which is neither empty nor marked and so is kept (a scratch leak outside any project, never a deletion); and a SIGINT that arrives while the summary is being sealed is recorded but the run can still exit 10 (a microsecond window).
10. Fake-client evidence encodes the fakes' protocol, including a reviewer that attests the sealed files it hashes. The live hook (§10) is the only route to client evidence; it ran 2026-10-06 but stopped by design before acceptance, so that evidence stops there.
11. **Judgment schema v2.** `coordinator-judgment-v1` carries one `authority_id` and one quote for the whole request, so a multi-question request is refused whenever one clause cannot cover every question (live runs 2 and 3). Candidate fix: `answers[i].authority_id` plus `answers[i].quote`, each validated against its own mandate entry, and one decision carrying all answers, keeping refuse-all when any question is uncovered. A question no clause covers (run 3's OQ-4) would still refuse. Applied 2026-10-07 as [judgment schema v2](judgment-v2-design.md): a clause per answer, one decision carrying every answer, refuse-all when any question is uncovered. A question no clause covers still refuses by design.
