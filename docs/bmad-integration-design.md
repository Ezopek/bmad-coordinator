# Coordinator slice 3: installed BMAD planning integration

Status: technical design for implementation, 2026-10-04, revised after design review. Baseline `125848a` (slice 2 done), 145 passing tests, SQLite schema 1, Python 3.11+ standard library. Codex CLI `exec` stays the only product transport. This realizes [delivery outline slice 3](v1-delivery-outline.md) on the [v1 design](v1-design.md) and [worker adapter](worker-adapter-design.md). It is derived from read-only research and direct reads of the installed skills and core source. It is not implementation evidence and claims no live BMAD run.

## 1. Scope

In: discover the **installed** PRD, architecture and review contracts and fingerprint the relevant bytes; run PRD and architecture through their installed **headless** modes as one-attempt workers; route an open question to an in-mandate coordinator decision (or the operator) and continue as a **fresh update assignment**; seal the produced files and have a distinct-process reviewer examine that sealed bundle; accept only on observed, bound, current evidence.

Out: Loop, sprint/epic topology, overnight driver, another client adapter, remote/push/merge, BMAD package update, interactive-gate resume, concurrency across worktrees, automatic recovery or quiescence of an unknown attempt, deletion authority, `bmad-spec`/`bmad-ux`/epics/`validate` intents, a generic workflow engine or plugin framework. Anything outside the supported shape fails `UNSUPPORTED_CONTRACT` with reasons; nothing is silently downgraded.

## 2. Decisions

| # | Decision | Reason |
| --- | --- | --- |
| D1 | Discovery reads **live bytes** in the feature worktree; manifest paths (`_bmad/bmm/plan/...`) are only a cross-check fact. | Skills live under `.agents/skills/<name>/`. |
| D2 | **No pinned hashes and no BMAD release dependence.** Capabilities/schema shape are recognized from the installed files; *semantic compatibility is a coordinator decision* tied to a fingerprint and the current mandate, never automatic proof. | Edits to BMAD must not need a coordinator source change; unseen versions must not be claimed. |
| D3 | The fingerprint hashes only the **semantic contract** (relevant file bytes, explicit absences, resolved config). HEAD, tool versions, sibling parity and manifest match are unhashed **facts**. | Unrelated commits must not retire acceptances (D10). |
| D4 | **Every mandatory invariant lives in shared core paths** (create/reserve/run/capture/accept/verify/reconcile). `bmad *` commands are thin wrappers. A profile requirement is written at creation, so a crash before binding cannot downgrade to generic. | Plain APIs and CLI must not bypass checks. |
| D5 | Writable workspaces use **trailing-slash directory scopes**; bundles enumerate the **observed** changed paths. | Installed outputs have slug-named files. |
| D6 | **One attempt per canonical worktree**, reserved *before* the inventory snapshot. A process that ends without anchored observed evidence **keeps** the fence (invalid output, timeout, unknown). | Conservative unknown-state ownership is intentional. |
| D7 | A generic anchored **`assignment.bound`** event selects a return profile from a closed set. Generic assignments keep the exact 9-field return. | Backward compatible; no schema migration. |
| D8 | Continuation is a **new `update` assignment** with `blocking_requests` and provenance. | No same-process resume exists. |
| D9 | `observed` review means a distinct process, an anchored structured return and exact bundle binding. Read/understanding/finding statements are **model claims**. | A brittle shell parser cannot prove cognition. |
| D10 | Acceptance validates proof pertinent to the bundle, not the whole-worktree inventory. Generic `inspect`/`reconcile` keep current-byte semantics; historical validation is a separate internal path. | Later unrelated edits must not retire valid acceptances; slice 2 stays intact. |

## 3. Core seams (Task 1)

**Closed profile registry.** `coordinator_core/profiles.py` holds the known set `{"bmad-planning-v1", "bmad-review-v1"}` plus `GENERIC`, resolving by lazy import. Each profile is a small record, not a plugin API:

```python
@dataclass(frozen=True)
class ReturnProfile:
    name: str
    extra: dict[str, dict]       # JSON-schema fragments added (all required) to the 9 generic fields
    env: dict[str, str]          # extra launch env, recorded in launch.json
    check: Callable[..., dict]   # (final, assignment, binding, run_dir, observed_changed) -> payload; raises UNKNOWN_EVIDENCE
    gate: Callable[[str, dict], list[str]]   # (stage, ctx) -> blocking reasons; [] = pass
```

Stages: `reserve`, `bundle`, `record`, `accept`, `verify`. Tests may inject a test profile into the registry dict; production code never does.

**Required profile at creation.** `Coordinator.create_assignment(owner, spec, required_profile=None)` writes the name into `events.source_id` of the same-transaction `assignment.prepared` event. `AssignmentInput` and the generic request fingerprint are unchanged; a retry with the same key but a different `required_profile` is `IDEMPOTENCY_CONFLICT`. A name outside the closed set is `INVALID_INPUT`.

**Anchored binding.** `_event(conn, kind, subject_id, anchor=None)` inserts source/ref/digest in the single INSERT and raises `CORRUPT_STATE` if another event already exists at the next revision in the transaction (existing update sites keep their behavior).

```python
@dataclass(frozen=True)
class BindingEvidence:
    profile: str; ref: str; payload_digest: str   # ref = "bindings/<64hex>.json" under <git-common-dir>/coordinator

Coordinator.bind_assignment(owner, assignment_id, evidence) -> None
```

State `prepared` only; one `assignment.bound` per assignment; identical repeat is a no-op, different is `BLOCKED_EVIDENCE`; the profile must equal the required profile when one is set. The sealed binding file (0600, exclusive; identical bytes accepted) is written first, so a crash leaves an orphan file or an unbound assignment, never a binding without bytes.

**Mandatory gates in core.**
- `reserve_launch` (plain `assignment reserve-launch`, `worker run`, `bmad run` all reach it): refuses `BLOCKED_EVIDENCE` when the assignment has a required profile without a matching anchored binding, and when the profile's `reserve` gate (fingerprint, compatibility decision, mandate) returns reasons. The gate runs before the SQLite transaction (it executes resolver subprocesses); the DB-only checks and the fence run inside it.
- `worker.run_assignment` order: static checks → `reserve_launch` → **then** `_check_inputs` re-run and the baseline `inventory()` under the reserved attempt → sidecar → launch. A recheck failure after reservation leaves the assignment `launch-pending`, which keeps the fence (no automatic release; reported with `STALE_INPUT`).
- `capture_bundle`: if any contributor carries a required profile or binding, **all** must be bound with the same profile family; each profile's `bundle` gate must pass (customization match, fingerprint, compatibility). Mixed bound/unbound is refused.
- `accept` and `verify`: whenever a bundle contributor or the review assignment carries a binding, the `accept`/`verify` gates run; plain `accept` and `verify` therefore cannot disagree with `bmad accept|verify`. Unbound generic behavior stays byte-identical.
- `inspect`/`reconcile` stay current-byte. A separate internal `historical` validation (anchor, receipt identity, launch binding, envelope, result fields, returned-file digests of the receipt; no whole-worktree equality and no immutable-input recheck) is used only by `bound-review` acceptance, never by plain `inspect`. A crash between create and bind is recovered only by finishing `bmad prepare` under the same key; the unbound assignment cannot be reserved meanwhile.

**Directory scopes.** An assignment path ending `/` is a directory scope, else one exact file as today. Grammar: relative POSIX, no empty/`.`/`..` parts, no glob characters, not the root, not under `.git/` or `.bmad-coordinator/`; stored verbatim (so `x` and `x/` fingerprint differently). `scope_covers(scope, path)`: file → equality, directory → prefix. Observed deltas must be covered; deletions, symlinks, mode-only or non-file kinds are still refused. A directory scope must not cover the brief, `AGENTS.md`, `.bmad-coordinator/`, a contract file or any path outside the feature writable scopes; the runner re-checks before spawn. A source inside a writable directory scope is a correction target (original digest kept as input provenance).

**Attempt fence.** `reserve_launch`, in its existing `BEGIN IMMEDIATE` transaction, refuses (`BLOCKED_EVIDENCE`, naming the holder) when another assignment with the same canonical `features.worktree` (any feature) is `launch-pending`, `launch-unknown`, `running`, or terminal with `provenance_class != "observed"`. `prepared` does not hold it. Only an observed, anchored terminal return releases it, and that is evidence the lead process ended, **not** that descendants stopped. A timed-out, invalid-output or otherwise unknown run keeps its worktree blocked until `worker reconcile` produces observed evidence; no automatic recovery exists. Owner `release()` is unchanged.

**Contributor chain.** In `_artifact_contributors`, a path last written by an observed `needs-decision` contributor is also covered when all of that contributor's decision requests are `resolved` and each is a `blocking_requests` entry of an observed `completed-claim` assignment with the same mandate revision and contracts. `blocked` and unresolved rows cover nothing.

## 4. Contract discovery and fingerprint (Task 2)

`coordinator_core/bmad_contract.py`:

```python
@dataclass(frozen=True)
class ContractReport:
    supported: bool
    capabilities: dict[str, bool]     # recognized from installed files, e.g. status_enum, memlog, resolver
    limitations: list[str]            # honest, e.g. "semantic compatibility not judged"
    reasons: list[str]                # why unsupported; empty when supported
    fingerprint_sha256: str | None    # sha256 of canonical(contract); None when unsupported
    contract_files: list[dict[str, str]]   # sorted [{"path","digest"}] of PRESENT closure files
    contract: dict                    # hashed part
    facts: dict                       # unhashed: head_commit, tools, sibling parity, manifest_check (matched|locally-modified|unmapped)

def discover(worktree: Path, client_root: str = ".agents/skills") -> ContractReport
def verify_fingerprint(worktree: Path, expected_sha256: str, **kw) -> list[str]   # [] identical, else changed/added/removed
```

`discover` is read-only, deterministic, timestamp-free:
1. **Catalog.** Parse `_bmad/_config/skill-manifest.csv`; required IDs `bmad-prd`, `bmad-architecture`, `bmad-review`; `Deprecated — forwards to` rows are shims, never routed. Resolve `<client_root>/<id>/` (real directory, `SKILL.md` with `name` equal to the directory, `customize.toml`). A catalog `path` column is never opened; `.claude/skills` parity is a fact only.
2. **Capability recognition (no hashes).** From the installed `references/headless.md` / `assets/headless-schemas.md` / `SKILL.md`, recognize the structural markers the return check depends on: status values `complete|partial|blocked`; `prd`/`spine` and `memlog` keys; `open_questions`, `assumptions`, `conflicts_with_prior_decisions`; `doc_workspace` with `headless: true`; the resolver and memlog scripts exist and run under `sys.executable -B`. A missing marker is a reason. Recognition says the *shape* is understood, not that behavior is compatible.
3. **Contract closure (hashed).** Present files, with `{path, digest}`: the three skill trees (skipping `__pycache__`), `_bmad/scripts/`, `_bmad/_config/{manifest.yaml,skill-manifest.csv,bmad-help.csv}`, every `_bmad/config*` and `_bmad/*/config*`, every file in `_bmad/custom/` (non-recursive; subdirectory or any symlink is unsupported), plus **every path reachable from merged config**: each `file:` workflow entry **and each merged key ending `_template`** (`prd_template`, `spine_template`, `validation_checklist_template`, ...), after substituting `{project-root}` and expanding globs; each must resolve inside the worktree, at most 200 files. `files-manifest.csv` is **not** hashed: a manifest match is a fact, and a manifest file change can still invalidate a *frozen package closure* through the contract files that did change; this is conservative by design and means an unrelated module update marks work stale for review.
4. **Explicit absence (hashed).** Checked-and-absent optional layers: `_bmad/config.user.{toml,yaml}`, `_bmad/custom/config{,.user}.toml`, `_bmad/custom/<id>{,.user}.toml` for the three skills and every forwarding shim, plus the full `_bmad/custom/` listing, so an unforeseen added override changes the fingerprint.
5. **Coordinator-side resolution (hashed).** Run the installed `resolve_customization.py --key workflow` per skill and `resolve_config.py`; the contract stores `sha256(stdout)` per skill and for central config. Nonzero exit or unparseable output is unsupported.
6. **Side-effect gates** on the merged `workflow`: non-empty `external_handoffs`, `on_complete`, `activation_steps_*`, `external_sources` → unsupported; a `skill:` entry other than `bmad-review` or any `file:`/`_template` path outside the worktree → unsupported. `persistent_facts`/`doc_standards` text is allowed (covered by the merged digest). Arbitrary URLs/MCP/web research are unfingerprinted and cannot be supported.
7. **Tools.** `uv` must be on `PATH`; its version and Python's are facts. Missing `uv` is unsupported.

A **locally modified** contract (differs from `files-manifest.csv`, or from any earlier fingerprint) is a visible fact needing a compatibility judgment, not an automatic rejection.

**Compatibility decision.** A fingerprint not yet judged needs a coordinator decision recorded with existing `decision add`: `source_kind: "coordinator"`, `source_ref: "bmad-fingerprint:<sha>"`, `choice: {"bmad_compat": "<sha>"}`, current mandate revision. `PlanningPrepare`/`ReviewPrepare` take `compat_decision_id` and seal it into the binding; the profile `reserve|bundle|accept|verify` gates require a compat decision for the bound fingerprint at the current mandate revision. The code cannot verify the judgment is right; it records who made it and against which hash. Drift (`verify_fingerprint` non-empty) is `STALE_INPUT` and needs a fresh judgment.

`contract_files` feeds `AssignmentInput.contract_files` (`contract_digest = sha256(_json(contract_files))` as today); one list serves every assignment of a feature so mixed versions cannot co-exist.

## 5. Binding and return profiles

**Binding JSON** (canonical, no timestamps): `{kind:"binding", format:1, profile, role:"planning"|"review", assignment_id, request_fingerprint, worktree, common_dir, fingerprint_sha256, contract, compat_decision_id}` plus planning `{skill, intent, workspace, artifact, altitude, purpose, parent_spine, expected_customization, continues}` (and, only when there are any, `decisions: [{id, digest}]`: the decisions `bmad prepare` names, with distinct ids and the stored rows' digests, never the caller's text, plus the decisions sealed in an accepted bundle whose artifact an `accepted-prd` input carries at the accepted bytes, whose provenance `inherited_decisions: [{id, bundle_id}]` records) or review `{bundle_id, manifest_digest, sealed_root, sealed_files:[{kind,path,digest,sealed_path}], contributor_assignment_ids, contributor_source_ids, lenses, brief_sha256, carried:{assumptions, critical_flags}}`.

`run_assignment` reads the binding (event + file + digest) and the profile; no binding and no required profile is `GENERIC`, unchanged. A non-generic launch receipt records `profile` and `binding_sha256`; the terminal receipt stores `profile_payload`; `inspect`/`reconcile` re-run `check` and a forged payload is `unknown`. Env: `PYTHONDONTWRITEBYTECODE=1` plus, **only when a startup probe shows `uv` cannot use its default cache under the actual permissions**, a small per-attempt `UV_CACHE_DIR` under permitted `/tmp` (outside every scope and the inventory; no broader claim and no permission bypass). Otherwise standard `uv run --no-project` behavior; never the git common-dir.

*`bmad-planning-v1`* extras: `workflow_json: string` (BMAD headless JSON, verbatim) and `customization: [{skill, sha256}]`. The check:

| BMAD `status` | `open_questions` / `conflicts_with_prior_decisions` | Required generic `result` | Verdict |
| --- | --- | --- | --- |
| `complete` | both empty | `completed` | `OBSERVED_COMPLETE_CLAIM` |
| `complete` | open questions non-empty | invalid, `UNKNOWN_EVIDENCE` | `UNKNOWN` |
| `complete` | conflicts only | `needs-decision` | `NEEDS_DECISION` |
| `partial` | either non-empty | `needs-decision` | `NEEDS_DECISION` |
| `partial` | both empty | `completed` | `OBSERVED_PARTIAL_CLAIM` (assumptions go to the review brief) |
| `blocked` | any | `blocked` | `BLOCKED` |

Also required: `intent` equals the bound intent; unless blocked, artifact (`prd`/`spine`) and `memlog` paths normalize into the workspace; **create** requires both in the observed changed paths; **update** requires the memlog change when the installed contract says it appends, and the artifact either changed or present with its immutable `before` digest (a no-op artifact update is valid); no unresolved `{doc_workspace}`-style placeholder; no non-empty `external_handoffs`. `customization` must hold exactly the bound skills with digests equal to `expected_customization`, else `NEEDS_COMPAT_REVIEW` (transport observed, bundle refused); the digest is a worker attestation, never disclosed in the brief, and only downgrades.

**Question format.** For `needs-decision`, the check requires `unresolved_question == canonical_json({"open_questions": [...], "conflicts": [...]})` taken from `workflow_json`, and `options == []`. This is canonical provenance, not a response schema: the generic `DecisionInput.choice` is **not** constrained to a positional list. One free-form answer may answer all questions; whether it semantically covers each is coordinator judgment recorded with its citation. The continuation carries full question and decision text, and the worker must retain any still-unresolved gap as `open_questions` rather than assume it answered.

*`bmad-review-v1`* extras: `bundle_id`, `manifest_digest`, `lenses_run`, `bundle_read: [{path, sha256}]`, `findings: [{lens, location, trigger_condition, guard_snippet, potential_consequence, severity}]`, `dispositions: [{contributor_id, kind, index, severity}]` with `severity ∈ critical|high|medium|low`. BMAD review has no severity, so severity is a coordinator-profile field assigned by the reviewer under the brief's rubric (critical = unsafe for downstream use or contradicts the mandate); the anchored receipt is what the caller cannot forge. The check requires `completed`, matching bundle ID and manifest digest, `lenses_run` equal to the bound set, a zero changed-path list, `bundle_read` digests equal to **every** sealed manifest file (structured per-artifact attestation), and `dispositions` covering **each** carried contributor assumption and each flag exactly once (below). `blocked`/`needs-decision` from a reviewer is transport state, never a review.

**Honest read claim.** The runtime proves: a distinct process, an anchored structured return, exact bundle/input binding. It does **not** prove the reviewer read or understood anything; read digests, lens text and findings are model claims. No substring or shell-command parser is used as proof. Live smoke additionally **inspects the raw command/tool events** for genuine reads of the sealed copy and scores that separately (`read_events: observed|unobserved`); a bare echo or message is not read evidence. If a stronger runtime proof proves essential, the proposal is a tiny deterministic bundle-read command, not parsing of arbitrary shell; this slice does not build it.

## 6. Planning assignments and question → update continuation

`bmad prepare` (stdin JSON, owner-fenced) runs: discovery + compat check → brief → `create_assignment(..., required_profile="bmad-planning-v1")` → sealed binding → `bind_assignment`. Idempotent on `key`; a retry finishes a half-finished prepare.

```python
@dataclass(frozen=True)
class PlanningPrepare:
    feature_id: str; key: str; skill: str       # "bmad-prd" | "bmad-architecture"
    intent: str                                  # "create" | "update"
    author_id: str; compat_decision_id: str
    slug: str | None = None                      # create only
    inputs: list[str] = []                       # frozen driving inputs (worktree-relative)
    input_roles: dict[str, str] = {}             # path -> declared dependency source/role, e.g. "authority", "accepted-prd"
    parent_spine: str | None = None              # explicit frozen spine; None -> brief says "no parent spine; do not search"
    decisions: list[dict] = []                   # {id,text,source_kind,source_ref}
    purpose: str | None = None                   # architecture: "build-substrate" | "discussion"
    altitude: str | None = None                  # architecture: only "feature"
    change_signal: str | None = None             # update only
    update_of: str | None = None                 # update only
    continues_request: str | None = None
    corrects_review: str | None = None           # update only: an observed recorded review to correct against
    finding_ids: list[str] = []                  # with corrects_review: JSON pointers (/findings/<i>, /dispositions/<i>)
    depends_on: list[str] = []
    prd_acceptance: str | None = None            # optional: ordered smoke where an accepted PRD feeds architecture
```

Requirements: at least one driving input (`inputs` or `prd_acceptance`), each with a declared source; BMAD order is not hardcoded, so architecture-first from authority inputs is allowed. If `prd_acceptance` is set it must be currently accepted (core `verify`), and its PRD files become inputs and bundle dependencies. A `parent_spine` is a frozen source and bundle dependency; there is **no** hidden workspace search.

Workspace `<planning_artifacts>/features/<slug>/prd/` or `.../architecture/`, from merged central config, a strict descendant of `planning_artifacts` and inside feature scopes; `create` requires an absent or empty directory. The brief is `<output_folder>/coordinator/briefs/<key>.md` (exclusive create, outside every scope, in `source_digests`) and contains the headless payload, explicit paths of the installed `SKILL.md`/`headless.md` (Codex auto-discovery is not relied on), writable directory, prohibitions (no edits outside the workspace or inherited sources, ignore handoffs/`on_complete`, never append to another run's memlog, set `PYTHONDONTWRITEBYTECODE=1`), "gaps become `open_questions[]`, never invented", and the profile fields.

**Continuation.** After a `needs-decision` return, the coordinator holds one open `decision_request` (question text is the worker's, not a mandate claim): within mandate, `decision add` with `source_kind: "coordinator"`, `source_ref` = the clause location (delegated, never operator approval; semantic fit is unverified by code); outside mandate, no decision is recorded, verdict `NEEDS_OPERATOR`, dependents stay blocked, and after the operator answers `source_kind: "operator-reported"` with a real message reference. Then `bmad prepare` with `intent:"update"`, `update_of=A`, `continues_request=R`: R must belong to A and be `resolved` by a decision of the current mandate revision; it sets `blocking_requests=[R]`, embeds question, decision, source, reason and "this is a fresh process; previous state is unavailable; `.memlog.md` is the record", and adds previous artifacts to `source_digests` (correction targets). Prior partial files stay and must be named in the bundle. `inspect`/`retry` say *"same-process continuation unsupported; resolve the request, then add a fresh assignment with blocking_requests"*. A third continuation, `corrects_review` with `finding_ids` (update only, exclusive with the other two), continues an observed completed attempt against an observed review of a current bundle sourced from it; the brief renders the named items from the stored review row under "Correction after a recorded review" and the binding records `continues = {update_of, request: null, corrects_review, finding_ids}` ([correction-round design](correction-round-design.md) D3–D4). Like the default `client`, an unset `corrects_review` or empty `finding_ids` is left out of `spec_sha256`, so every pre-existing prepare key replays unchanged; a correction spec digests both fields.

## 7. Bundle, independent review, acceptance (Task 3)

**Bundle** (`bmad bundle`, stdin `{feature_id, workspace, dependency_paths}`) is a thin wrapper over core `capture_bundle` (§3 gates apply to the plain command too): artifact paths are every path under the workspace claimed by an observed return of the feature and present at the claimed digest; contributors are all assignments whose observed returns touched them; contract files are the shared list; `customization_check == match` for every contributor. The manifest also seals, by id and row digest, every decision that resolved a contributor's decision request and every decision a contributor was prepared with (its binding's `decisions`); a row changed since prepare makes capture `STALE_INPUT`, a missing one `UNKNOWN_EVIDENCE`, and the key is absent when there is none.

**Review prepare** (`bmad review prepare`, stdin `{bundle_id, reviewer_id, key, lenses, compat_decision_id}`; default lenses `["adversarial","edge-case-hunter"]`) before any process exists: verify the bundle manifest, sealed copy and live bytes and the fingerprint; write the immutable brief (sealed absolute paths of every bundle file, lens list, severity rubric, "read only the sealed copy", the profile fields, the sealed decisions rendered from their stored rows (resolving or given at prepare, and separately, under "Inherited decisions" with the accepted bundle id, those inherited from an accepted PRD bundle), and **all contributor assumptions and any critical flag/partial marker carried from every contributor payload**, each with a contributor/index); create a `read-only` assignment with `required_profile="bmad-review-v1"` (sources = brief plus live bundle copies, `author_id = reviewer_id`); seal and bind. Sealed paths reach the reviewer through the frozen envelope's `brief`; `_envelope` is untouched. A completed/partial flag is never silently lost: `critical_open = any(severity == "critical")` over findings **and** dispositions, and the disposition list must cover each carried item.

**Record** (`bmad review record ID`) builds the `ReviewEvidence` itself; no caller-supplied finding or flag. `BoundReviewSource.classify_review` returns `observed` only if all hold, else raises and records nothing:
1. the review assignment is `read-only`, bound `bmad-review-v1`, `completed-claim`, with an observed anchored return whose **historical** receipt validates;
2. binding bundle ID/manifest digest equal the bundle record, `sealed_files` equal the manifest;
3. `reviewer_id` equals `author_id` and is not in `derived_author_ids`;
4. the reviewer's `codex-cli:<thread>` source is not any contributor's, and the reviewer is not a contributor;
5. payload bundle, lenses, `bundle_read` and dispositions match the binding (limits in §5);
6. `finding` equals the receipt's findings and `critical_open` equals the derived value; a lower caller value is refused, not corrected;
7. `source_ref` is the anchored return ref.

Generic `review add` and imported/manual evidence stay `reported`/`unknown`; a reported review with `critical_open=true` still blocks acceptance.

**Accept** (`bmad accept`, stdin `{bundle_id, review_id}`) is a wrapper over core `accept`, whose gates re-run: observed review tied to a bound review assignment, binding event/file/digest intact, conditions 1-7, `verify_fingerprint` for reviewer and every contributor, compat decision for each fingerprint, then core open-critical, mandate and bundle currentness. `verify` reports per acceptance `current|stale|unknown` with the same gates. An unrelated later worktree change does not retire an acceptance; a change to any bundle file, contract byte, added override, or the mandate does.

## 8. CLI (JSON in/out; `--repo`, `--token-file` as today)

| Command | Mode | Output |
| --- | --- | --- |
| `bmad discover [--client-root R]` | read-only | `ContractReport` |
| `bmad prepare` | owner-fenced | `{assignment_id, brief_ref, binding_sha256}` |
| `bmad run ID --model M --timeout S` | owner-fenced | `{assignment_id, state, run_dir, evidence_needs, verdict}` (thin wrapper over `worker run`) |
| `bmad status ID` | read-only | `BmadReturnView {verdict, bmad_status, open_questions, assumptions, conflicts, artifacts, customization_check, evidence_needs}` |
| `bmad bundle` / `bmad review prepare` / `bmad review record ID` / `bmad accept` | owner-fenced | as §7 |
| `bmad verify` | read-only | per-acceptance state with reasons |

Reuse unchanged: `worker run|inspect|reconcile`, `assignment add|reserve-launch|return|retry`, `decision add`, `bundle capture`, `review add`, `accept`, `verify`; all are covered by the core gates. New error `UNSUPPORTED_CONTRACT` (exit 4). Verdict words are conversation labels; none means accepted.

## 9. Failure behavior

| Situation | Behavior |
| --- | --- |
| Skill/marker absent, symlink, `uv` missing, side-effect customization, outside reference | `UNSUPPORTED_CONTRACT` with reasons; no assignment |
| Required profile without binding, plain `reserve`/`worker run` | `BLOCKED_EVIDENCE`; finish `bmad prepare` under the same key |
| Override added, closure byte or merged config changed; no compat decision for the fingerprint | drift: `STALE_INPUT` at reserve/bundle/accept/verify; no compat decision judging an unchanged fingerprint at the current mandate revision: `BLOCKED_EVIDENCE` (prepare, gates) |
| Another attempt, or an unknown/timed-out/invalid-output attempt, holds the worktree | `BLOCKED_EVIDENCE` naming the holder; no automatic release |
| Worker JSON violates profile, wrong intent/paths, claimed handoff | `unknown`; nothing recorded as completed |
| `customization` absent/mismatch | `NEEDS_COMPAT_REVIEW`; bundle refused |
| Open question outside mandate | `NEEDS_OPERATOR`; request open; dependents blocked |
| Live bytes or sealed copy differ before launch / live bytes after | review not launched / `unknown`; nothing recorded |
| Reviewer blocked, missing digest or disposition, reuses a contributor thread, caller lowers `critical_open` | no observed review / refused |
| Artifact, dependency, contract or mandate changes after review | `accept` refused; `verify` stale |
| Imported checkpoint | sidecars absent: receipts historical, review/return `unknown` |

## 10. What this slice does not prove

Worker checks, permission statements, descendant lists, read/understanding and finding quality remain claims. Independence is a distinct Codex process with a fresh context, bundle-only subject and recorded model; same model/host is weak independence, recorded not scored. Compatibility is a recorded coordinator judgment, not automatic proof. Network research by skills is unfingerprinted. Fake-subprocess tests prove coordinator logic only and never count as BMAD workflow evidence.

## 11. Residual technical uncertainty

1. Whether `codex exec` loads `.agents/skills` itself (the brief names file paths, unobserved).
2. Whether `read-only` Codex can read the common-dir sealed bundle, and which command/tool events show reads (smoke inspects them).
3. Network from `workspace-write` may be unavailable; headless skills then return `open_questions`, possibly masking the seeded gap.
4. A model may not return the seeded question or may violate `workflow_json`/`customization`; that is a smoke `fail`/`blocked`, not patched around.
5. Nested reviewer subagents under `codex exec` are unknown; `descendants` is a claim.
6. Existing tests that reserve several assignments in one worktree concurrently need individually justified fixture changes (attempt-fence fixtures only; no weakened assertion).
7. A stray untracked file in the skill trees fails closed as an out-of-scope delta.
8. A run that ends invalid or timed out blocks its worktree until reconciled; the smoke stops there and reports `blocked`.
