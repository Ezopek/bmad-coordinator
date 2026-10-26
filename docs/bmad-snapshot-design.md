# A gitignored BMAD install on the planning path: sealed snapshots materialized into worktrees

Status: implemented 2026-10-25 (plan `2026-10-25-bmad-snapshot`, project history), tested with fake clients; the operator approved revision 2 ("Leć z planem"). Revision 1 (2026-10-25) was reviewed by Codex at the operator's request (`REQUEST_CHANGES`: four high, five medium, one low finding, all checked against the code and accepted); revision 2 answers them, §6 maps each.

## 1. Problem

The planning path (headless PRD and architecture authors, the planning review) needs the BMAD install in the feature worktree it runs in: `_bmad/` and the client's skill root (`.agents/skills` by default). Contract discovery reads it there ([`bmad_contract.discover`](../coordinator_core/bmad_contract.py)), the binding seals its fingerprint, and the authors run its scripts. A linked worktree holds tracked files only, so a project that keeps the install out of Git has nothing there: discovery reports `skill is missing` and the step is refused. The install is regenerable (`npx bmad-method install`) and takes about 15 MB per client root, so keeping it out of Git is a reasonable choice for a project, and the story path already works that way (design D22).

Git cannot carry ignored files into a worktree (`git worktree add` has no option for it), a `post-checkout` hook is ruled out because every coordinator Git call runs with `core.hooksPath=/dev/null`, and a submodule or an install branch only moves the download.

## 2. What makes it possible

The contract is bound by content, not by commit: the fingerprint is a digest over the installed bytes, the resolved configuration and the absent optional layers. Measured 2026-10-25 on two clean worktrees of the same commit: the fingerprint does not depend on the worktree's path (`548e9f…` in both, `resolved` equal). That proves it for that install only; S2 therefore proves it again after every copy.

## 3. Decisions

**S1. The roots are a closed set.** The client root is `.agents/skills` or `.claude/skills`, nothing else, beside `_bmad/`. Every other value (`.`, `.git`, `_bmad`, a root overlapping another) is refused by `bmad materialize` before anything is read. (Discovery itself still accepts any normalized relative root, which is out of scope here.)

**S2. A snapshot is the whole install, read without following links.** The source is the repository's main worktree, found from `git worktree list --porcelain` (its first entry) and refused when it is bare or missing. Every regular file under `_bmad/` and the client root is read with descriptor-relative, no-follow opens from verified directory descriptors, checked as a regular file with one link on the opened descriptor, and hashed from the bytes actually copied; `__pycache__` is left out; a symlink, a special file or a hardlinked file anywhere refuses the snapshot. The copy goes into a temporary directory under `<common>/coordinator/bmad/` and is renamed to `<tree>/`. Its `snapshot.json` lists each path with its digest and its mode (executable or not), the client root and the contract fingerprint; `<tree>` is the SHA-256 of that canonical listing. An existing `<tree>/` is reused only after its every file is checked against the listing, with no extra entry. `bmad/` joins the sealed tops of the D16 baseline, so a run that changes a snapshot ends `unknown`; under the launch boundary `<common>/coordinator` is invisible to every client anyway.

**S3. Materialization copies a snapshot into the feature worktree and proves it.** It runs under the coordinator's drive lock and the owner's token. The target classification is exact: the worktree's commit (`git ls-tree -r HEAD`) and its index (`git ls-files`) must hold nothing under `_bmad/` or the client root; any tracked or staged entry there, in either root, is the committed-install case (S6) when both roots are complete and tracked, and is refused as mixed otherwise. The destination roots must be absent, or hold exactly the snapshot (same paths, digests and modes, single-link regular files, no extra entry): then nothing is written and the command only records the event (the recovery path when a copy succeeded but the event did not). Files are written descriptor-relative with `O_CREAT | O_EXCL | O_NOFOLLOW` under directories the command created itself, with the snapshot's mode; every path created is recorded, and a failure removes exactly those, deepest first, without following links. After the copy, the trees are compared with the listing, then discovery on the worktree must give the snapshot's fingerprint; otherwise the created paths are removed and the command fails.

**S4. Customization references outside the roots.** Discovery may follow a customization reference to a file elsewhere in the worktree and fingerprint it. Such a file is not copied: it must be present in the worktree's commit. If it is missing (for example an ignored template outside the roots), discovery's existing reason ("referenced file is missing or not a plain file: PATH") refuses the materialization, and the troubleshooting page says to commit that file or move it under `_bmad/custom/`.

**S5. Ignore coverage of every file, and a planning `doctor`.** In the worktree, every path to be materialized is checked with `git check-ignore --no-index --stdin -z`, so a negation such as `!_bmad/keep.md` that would let one file into the inventory is caught; any path not ignored refuses the command, with the lines to add (`_bmad/`, `.agents/skills/`) and the paths that escape them. `doctor --bmad [--client-root R]` runs the same classification and coverage check and folds it into readiness (`ready: false` with reasons); without `--bmad`, `doctor` is unchanged, so a story-only project is never told it lacks an install.

**S6. A committed install keeps today's behavior and wins.** When both roots are complete in the worktree's commit, `bmad materialize` refuses (`the install is committed`) and everything runs as today.

**S7. The binding is the authority, and the trees are checked before any installed code runs.** A planning binding prepared on a materialized worktree records `bmad_source: {"kind": "snapshot", "tree": T, "client_root": R}` (absent for a committed install, so committed bindings keep their digests); the event `bmad.materialized` (feature, worktree, `tree`, fingerprint, client root) is the log, not the authority, and a binding is refused unless the worktree's newest such event names the same `tree`. Every check derives from the binding: before discovery or any resolver runs (at prepare, at reservation, at return and at replay), the worktree's two roots are compared with the snapshot listing without executing anything, and a changed, added or removed file or a changed mode ends the step (`STALE_INPUT` before a launch, `unknown` at return); only then does discovery run its scripts. This also covers files the contract does not fingerprint, such as another skill under `.agents/skills`, which Copilot loads as a project skill. Replay verifies the stored snapshot's files against its listing and the listing's digest against the binding.

**S8. Read-only during the attempt under the launch boundary.** For a boundary launch on a materialized worktree, the worktree's own `_bmad/` and client root are bound read-only over the writable worktree, so a worker cannot change and restore them within one attempt. Without the boundary the guarantee is the endpoint comparison of S7 (and the existing protected-path write rules), and the docs say so.

**S9. Only the bound client root** is materialized. `.claude/skills` is not copied when it is not the root; discovery reports a missing sibling as `sibling_parity: absent`, a fact, not a reason. **Retention:** every snapshot is kept; pruning those no binding references is a later command. **Copilot:** a materialized `.agents/skills` is listed as project skills exactly as a committed one; the recorded isolation gap is unchanged.

**S10. An explicit command in the attended prework.** `bmad materialize [--client-root R] [--tree T]` with `--repo <feature worktree>`, before `bmad discover`. Without `--tree` it snapshots the main worktree as it is now; with `--tree` (64 lowercase hex characters, an existing snapshot) it materializes an earlier one, so a second feature can get exactly the first feature's bytes after the main checkout's install was regenerated. Explicit rather than implicit at `drive start`: the prework is already explicit (`feature add`, `bmad discover`, `decision add`), the operator creates the feature worktree, and an implicit copy would let a regenerated install silently change what a new feature binds. `bmad discover` on a worktree with no install says so and names the command.

## 4. Changes by file (sketch)

- `coordinator_core/bmad_snapshot.py` (new, in the manifest): roots (S1), snapshot (S2), materialization and rollback (S3), ignore coverage (S5), the non-executing tree comparison (S7).
- `coordinator_core/__main__.py`: `bmad materialize`; `doctor --bmad`.
- `coordinator_core/bmad_planning.py`, `bmad_review.py`: `bmad_source` in the binding and the S7 comparison before discovery; `coordinator_core/clients.py`: `bmad` in `SEALED_TOPS`, the S8 read-only mounts.
- Docs: getting-started (the planning prework), state-core usage (the command, the event, the binding field, `doctor --bmad`), troubleshooting (each refusal), the BMAD integration design (a pointer here), CHANGELOG.

## 5. Tests (fake clients and the filesystem)

- The acceptance path: a gitignored install, `bmad materialize`, `bmad discover` supported with the main worktree's fingerprint, then a planning author, its review and the acceptance as on a committed install.
- Regeneration: the main worktree's install changes after materialization; the worktree's bytes and fingerprint do not; a new materialization there is refused; a second feature gets the old bytes with `--tree`.
- Refusals before any write: a root outside the closed set; a bare repository; a symlinked, special or hardlinked file in the source; a symlinked destination parent (`.agents` a link); a destination with a hardlinked or extra file; a malformed `--tree` or `snapshot.json`; a missing ignore rule and a negation exception; a staged or partly tracked root; a referenced file outside the roots that is not committed.
- Interrupted publication: a failure after some files are written removes exactly those; a copy that succeeded without its event is completed by rerunning.
- S7: a changed non-contract skill file and a changed resolver script are both refused before the resolver runs (a resolver that writes a marker proves it never ran); S8: a worker write under the read-only roots fails under the boundary.
- A committed install: refused by the command, nothing changes, existing planning tests pass unchanged.

## 6. Review findings and where they went

| Codex finding (revision 1) | Revision 2 |
| --- | --- |
| H1: roots like `.`/`.git`, symlinked parents, unvalidated `--tree`, unsafe rollback | S1 closed root set; S2/S3 descriptor-relative no-follow I/O; S10 `--tree` format; S3 rollback of recorded paths only |
| H2: check-then-use races, hardlinks | S2 checks on opened descriptors and hashes the copied bytes; hardlinks refused at source and destination; S3 under the drive lock |
| H3: the store's trust boundary | S2 store in D16's sealed tops, full re-check on reuse, modes in the listing; S7 listing digest sealed in the binding |
| H4: verification runs installed code before comparing | S7 non-executing comparison first, everywhere; S8 read-only roots under the boundary, endpoint-only otherwise |
| M5: references outside the roots | S4 must be committed, refused with the existing reason, documented |
| M6: ignore negations; `doctor` must refuse | S5 per-file coverage; `doctor --bmad` folds into readiness |
| M7: snapshot identity and event lifecycle | S7 binding as authority, event as log, idempotent rerun as recovery |
| M8: main worktree from the common directory | S2 from `git worktree list`, bare refused |
| M9: tracked/mixed ambiguity, index vs commit | S3 commit and index both checked, exact classification |
| L10: missing security tests | §5 |

## 7. Open points for the operator

- The scope is the issue's; the review's additions are correctness work, not new features.
- A live confirmation after the fake-client tests: one planning drive on a repository with a gitignored install, costing a planning author and a review.
