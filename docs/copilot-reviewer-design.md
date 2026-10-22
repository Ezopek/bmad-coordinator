# Copilot as reviewer and judge: read-only tools in a staged input copy

Status: approved 2026-10-22 as revision 6 (operator: "wydaje mi się ok" on revision 1 before asking for an independent review, then "Nie no to leć z tym" on §9's order of work: the boundary first, then this design on top of it). Revision 6 rests on the [launch boundary](launch-boundary-design.md), implemented and checked live the same days, which now carries the containment that revisions 2 to 5 had to argue from Copilot's own tool permissions. Revisions 1 to 5 came out of four rounds of independent review (Codex, read-only; every claim checked against the code and the installed client; project history, 2026-10-22-issues); §8 maps every finding to its decision. **The judgment slice is implemented** (2026-10-23): V1, V3 to V8 for the `judgment` role, with the parser contract `copilot-read-1` and 1.0.91 qualified for the judgment profile (P3, P4, P6; project history, 2026-10-23-copilot-read/qualification-report). The reviewer's read mode (V2's per-profile staging, P1, P2, P5) is not implemented, and a reviewer role in read mode is refused.

## 1. Problem

Every client can be a worker or a planning author. The reviewer and judgment roles take only `claude-code` and `codex-cli`, because both have a write-free read-only mode. `copilot` is refused there with `UNSUPPORTED_CLIENT_MODE` (K1). It returns through a file it writes, so it needs write approval. Path-scoped write approval is unverified (C5, C6), and the inventory misses `.git/**` and ignored paths, so a read-only Copilot run in the project could write without being caught. The adapter design names the way out as C12: path-scoped write approval, or a write-free return channel.

Issue #2 reports a write-free channel exercised outside the coordinator. A Copilot CLI 1.0.102 run reviewed an inline source pack with no offered tool and returned its verdict as text. Revision 1 of this design adopted that as is. Two things argue against it as the general mode. The operator objected that pasting everything into the prompt and blocking every tool is the chatbot pattern. The review measured that planning bundles carry the BMAD contract files and exceed the 100,000-byte argument limit even for a small PRD. Neither objection holds against the channel itself, the verdict in the final message. Both hold against giving up tools. Removing the return-file write does not require removing reads.

## 2. Goal and non-goals

**Goal.** Copilot may hold the `reviewer` and `judgment` roles through one explicitly chosen mode, `read`. The coordinator stages a copy of exactly the role's inputs in a fresh directory outside the project and its Git directories. It offers the model only `view`, `grep` and `glob`, with no shell and no write tool. It reads the verdict from the stream's final root-agent answer and validates it fully. Everything after the return stays unchanged: the review record, the provenance class, the independence rule, `critical_open`, the judgment's citation checks, acceptance.

**Non-goals.** Any change to Copilot authors or to the meaning of `write_free_readonly`. Write approval scoped to a return file (C12's first option): it adds a write boundary without adding review depth over `read`. An inline mode with no tools; it stays a later option for inputs that fit the argument limit, after its own qualification (§7). Any change to the Claude or Codex reviewer and judgment contracts, or to their receipts. Any claim of an OS sandbox.

## 3. Decisions

**V1. An explicit mode, opt-in per role, digest-stable, with a bounded qualification path.** A `reviewer` or `judgment` role that names `copilot` must also say `"mode": "read"` and `"boundary": "bwrap"`: the mode exists only inside the launch boundary. `bmad review prepare` takes it as `mode`. An absent `mode` is omitted from the pipeline digest and from `spec_digest` exactly as an unset correction is, so existing pipelines and same-key prepares keep their digests (no `IDEMPOTENCY_CONFLICT`). `mode` on another client, or `copilot` without it, is `UNSUPPORTED_CLIENT_MODE` naming the mode. A launch on a version outside `READ_QUALIFIED` (V7) is refused, except with the operator-only flag `--qualify` on `bmad review prepare` and the judgment's prepare. A qualification launch is sealed with `qualification: true`. Its review or judgment is never `observed`: it cannot gate an acceptance or answer a pipeline question. That breaks the circle between "refused until qualified" and "qualified by running".

**V2. The input set, per profile, from the manifest.** The coordinator stages each launch's inputs from records, never from directory listings. Every file is copied from bytes it has just checked against the bound digest, and the same buffer is written that was hashed.

| Profile | Staged inputs |
| --- | --- |
| planning review | the manifest's entries (artifacts, dependencies, contract files), the review skill's `SKILL.md`, the named lens files and the merged customization the brief would otherwise send the reviewer to resolve |
| code review | the manifest's entries, the sealed diff the brief names, and the story spec the brief's "Read first" names, read from the bundle's commit (`<commit>:<spec_path>`); the bundle itself holds no dependency for it |
| spec review | the manifest's entries, and the project rules and context paths of the brief's "Read first", each read from the bundle's commit; the spec author's binding records these as names only, so the commit is the authoritative snapshot, and a missing path refuses the launch |
| judgment | the copies the isolated run's directory holds today (question, mandate, decisions, contributor artifacts) |

A file with a symlink, a special type or a digest mismatch refuses the launch (`UNKNOWN_EVIDENCE`). Inputs keep their original paths in the manifest, but they are staged under inert aliases: `inputs/<kind>/<NNNN>-<name>`, where `NNNN` is the entry's index and `<name>` is the basename with any leading dot dropped. A name the client treats as instructions (`AGENTS.md`, `CLAUDE.md`, `copilot-instructions.md`) becomes `<NNNN>-file.md`. No staged path therefore contains a segment the client discovers (`.agents`, `.github`, `.claude`) or an instruction file name, so a staged review skill or a project's `AGENTS.md` rule is data to read, not something Copilot loads. The brief is beside them as `brief.md`. It lists every input as its original path and its alias, so the model reads by alias and cites by original path. The staged manifest (paths, digests, sizes), its digest and the profile's input-set version are sealed in the reservation request and in the launch receipt.

**V3. A boundary launch of the isolated kind.** A `read` launch, review or judgment, is a [launch boundary](launch-boundary-design.md) launch of its isolated kind (L4). The layout is D17's and the judgment's: a root outside every worktree of the project and outside its Git common directory holds a Git-initialised `cwd` with exactly the staged inputs, bound read-only, and a receipt directory beside it, bound read-write for the CLI's own storage (`COPILOT_HOME`, logs). The project, the coordinator's store and the operator's files are not mounted; the launch receipt names every mount. After the drain, the coordinator copies the sealed receipts into the store and anchors them, as the judgment does today; a review's anchored copy is what `review record` reads. The CLI gets `-C <cwd>` and no `--add-dir`. The reservation request seals the mode, and the scratch's construction, its `_matches` check and its crash recovery stay those of the judgment. The feature worktree's delta is still taken, as for every reviewer, and must be empty.

**V4. The tools and the argv.** `copilot -p ENVELOPE --output-format json --session-id S --model M`, followed by:

- the isolation flags of §14 (`--no-ask-user`, `--no-auto-update`, `--no-custom-instructions`, `--disable-builtin-mcps`, `--disallow-temp-dir`);
- `--available-tools view,grep,glob`;
- `--log-dir RUN/copilot-logs -C <staged copy>`.

There is no write tool, no shell and no `--allow-tool` beyond what the probe shows the read tools need. The envelope now carries only the brief, rewritten per profile to name staged paths, so the argument limit no longer bounds the inputs. A brief that would still exceed `max_envelope_bytes`, after JSON serialization, is refused before reservation.

**V5. Fail closed on the stream and the directory.** The terminal check of §14 holds, and these rules are added:

- **The offered tools must be observed.** A `session.usage_checkpoint` must be recognised and must list exactly `view`, `grep` and `glob`. A missing or malformed checkpoint is `unknown`, never an empty or matching set. `_offered` gets a strict variant that reports a shape it does not know.
- **Every tool record is checked.** Each `tool.execution_start` must name one of the three tools, with a path argument that resolves under `inputs/` or names `brief.md`; `.git` and everything else in the `cwd` is outside, so a `grep` or `glob` rooted at the `cwd` itself is `unknown`, and the brief says to search under `inputs/`. Each `tool.execution_complete` must answer a start. An orphan completion, a tool request for any other tool and a record attributed to a subagent (`parentToolCallId`, agent attribution) are each `unknown`. MCP servers the stream reports as disabled stay outside the gaps, as since the return-write round. Any other isolation gap (a loaded skill or instruction file, a hook, an active MCP server) is `unknown` for this mode.
- **Reads are covered.** The installed `view` truncates a file over 20 KB unless it is given a `view_range` or `forceReadLargeFiles` (1.0.91; the threshold is part of the qualified parser contract). Requested arguments do not prove delivery, because the schema allows the model-facing `result.content` to be truncated. A file therefore counts as read in full only when the successful completions' returned content is verified to carry every line of the staged file: lines 1 to N, by the numbered-line format of the qualified parser contract, across one or more `view` calls. Ranges, `forceReadLargeFiles` and the threshold only tell the model how to get there. A directory listing, a `grep` or `glob` hit and a denied or failed call are not reads. The brief tells the model how to read a large file. These must be read in full, or the return is refused as a `bundle_read` mismatch is today:
  - for a review, every artifact and dependency entry under review, plus the diff and the spec for a code review;
  - for a judgment, the question, the mandate and the decisions, an all-deferred answer included, since the deferral path skips the citation checks.

  Contract files, the review skill's files and a judgment's contributor artifacts may be read and need not be. This replaces the per-file digest attestation with an observed fact, for the files the review is about.
- **Outside paths do not exist.** The boundary leaves every unmounted path absent, so an outside read fails inside the sandbox, whatever Copilot's own path checks do. The tool-path rule above is evidence, not containment: it keeps the reads the review relies on to the staged inputs, so a verdict does not draw on the CLI's runtime or its own storage, which are mounted for it to run.
- **Event types are known.** The qualified version (V7) has a closed list of record types. Any other type is `unknown`.
- **Nothing was written.** After the run, the `cwd` must still hold exactly the staged inputs (names, types, modes, sizes, digests, hidden entries included), checked as the judgment's `_matches` checks its scratch today, with `.git` left as `git init` made it, and the feature worktree's delta must be empty. Otherwise the run is `unknown` with the fence held.

The covered reads become a fact: for this client, which inputs the model read in full is observed, not claimed.

**V6. The return channel: the final root answer.** A launch-level return source, `"final-message"`, sealed in `launch.json` beside the mode. `return_source` stays the client's default for authors. The runner selects the answer by these rules:

- the last `assistant.message` of the root agent before `result`, with no `toolRequests`;
- for a chunked response, the model call's chunk set must be complete and consistent: one call identity (`apiCallId`, or `interactionId` where that is what the version carries), one `chunkCount`, and indexes `0` to `chunkCount - 1`, each exactly once and in order. The answer is the final chunk's content alone; earlier chunks are reasoning boundaries and are never concatenated into it;
- that final chunk's `phase`, when present, must be one of the response phases the probe qualifies; an absent phase is accepted only where the qualified parser contract says the version omits it.

The content must be exactly one JSON object, bare or in one fenced `json` block with nothing else. There is no fallback to an earlier message when the final one is malformed. Several objects, prose around the object, a placeholder left in the skeleton, or an object beyond the transport's byte and depth limits each end `unknown`.

The object is validated in full against a mode-specific profile schema before any result-specific branch: required keys, no extra keys, types and enums. The envelope prints that schema and a skeleton, as worker-file envelopes do. A Copilot reviewer cannot hash, so its schema drops the per-file `bundle_read` attestations. The staged manifest of V2 and the viewed paths of V5 replace them as coordinator facts, kept apart from anything the model attests. The sealed schema name and input-set version let replay rerun exactly this validation. Receipts sealed before this mode replay unchanged.

**V7. Qualified versions, not a minimum.** `READ_QUALIFIED = {<version>: <parser contract id>}` lists the Copilot CLI versions on which the probe passed. A launch on any other version is refused before reservation. `doctor --client copilot` reports whether the installed version is qualified. Extending the list needs a new qualification run. Replay of a sealed receipt checks the parser contract the receipt names, never the installed version.

**V8. Every refusal before the reservation, judgment included.** `judgment.prepare` gains the role's client and mode and performs the mode checks before it anchors `judgment.reserved`: version, staged inputs, brief size. A refused prepare therefore leaves the one-shot key unused. `bmad review prepare` and `reserve-launch` do the same for reviews.

**V9. Independence is unchanged.** A Copilot review's source id (client and session) differs from every author's. Cross-client review stays allowed (R2), and a Copilot reviewer of a Copilot author is another process with another session, as Claude reviewing Claude is today. A judgment keeps its rules: untrusted worker data, authority citations, the deferral and refusal paths.

**V10. Documentation.** Once the probe passes, the model selection guide, getting started, troubleshooting and the usage reference stop calling Copilot author-only. They state the mode, the staged inputs, the observed reads and the weaker return class: model-written JSON validated by the coordinator, not schema-enforced by the client.

## 4. Changes by file (sketch)

- `coordinator_core/clients.py`: the read argv, the strict offered-tools parser, the V5 stream, coverage and directory rules, the `final-message` reader, `READ_QUALIFIED` with its parser contracts, the `--qualify` path; `run_isolated` reused for the launch.
- `coordinator_core/worker.py`: the per-launch return source and mode in the launch receipt; the mode-specific schema and skeleton in the envelope.
- `coordinator_core/bundles.py`: staging an input copy from manifest entries with read-then-hash-then-write buffers.
- `coordinator_core/bmad_review.py`, `coordinator_core/bmad_implementation.py`, `coordinator_core/bmad_spec_author.py`: `mode` on review prepare, the per-profile input sets, briefs rewritten for staged paths, the Copilot review schema.
- `coordinator_core/judgment.py`: client and mode in `prepare`, the checks before `judgment.reserved`, the read launch over the existing scratch copies.
- `coordinator_core/drive_pipeline.py`: `mode` on the `reviewer` and `judgment` roles, omitted from the digest when absent.
- `coordinator_core/__main__.py`: the client help. Docs per V10; `CHANGELOG.md`.

## 5. Tests

Fake clients only. The fake Copilot gains a read mode: view, grep and glob records with path arguments, a chunked and phased final answer.

- **Modes.** For each profile, a review or judgment with a Copilot read role ends observed. `mode` without `copilot`, and `copilot` without `mode`, are refused. A pipeline and a prepare without `mode` keep their digests.
- **Inputs (V2, V3).** An extra file in a sealed copy is not staged. A tampered byte, a symlink or a missing context file refuses the launch. The staged manifest matches the brief's paths. The cwd lies outside every worktree and the common directory. No staged path is a discovered location. A crashed `read` review recovers its scratch as a judgment does. A review missing a successful read of a reviewed file is refused. A `--qualify` launch is never `observed`.
- **Stream (V5).** A missing, malformed or wider checkpoint; a tool outside the three; a path outside the copy, through `..`, absolute or via a link name; an orphan completion; a subagent-attributed record; an unknown record type; a written, changed or added file in the copy, hidden ones included; a non-empty worktree delta. Each ends `unknown`.
- **Return (V6).** A tool request in the last message; a non-final chunk; an unqualified phase; prose around the object; two objects; an earlier valid object after a malformed final one; an oversized or too deep object; an object failing any schema rule before the result branch. Each ends `unknown`.
- **Lifecycle (V7, V8).** An unqualified version is refused before reservation. A refused judgment prepare leaves its key unused. A brief exactly at the limit after serialization fits, and one byte over is refused.
- **Replay.** Receipts of this mode replay after the scratch and the staged copy are removed. Author, Claude and Codex receipts and goldens are unchanged. The judgment's citation and authority refusals are unchanged.

## 6. Live probe (qualification of `read`)

On a fixture, Copilot only, every launch with `--qualify` (V1), at most eight premium requests. Before and after every run, a digest of the fixture's worktrees, ignored paths and Git common directory except `<common>/coordinator/` is taken independently of the coordinator, and the two must be equal; the boundary's launch record shows that none of them was mounted.

The mode qualifies on a version only when every gate below passes. Any failure leaves `READ_QUALIFIED` unchanged.

1. **P1, a spec review.** One read review of the example's story spec with a blatant seeded defect. It passes when the run would be observed but for `--qualify`, the offered tools are exactly the three, every read lies in the `cwd`, the coverage of V5 holds, the answer validates, and the seeded defect is among the findings.
2. **P2, a code review.** One read review of the example's code bundle with a seeded defect, exercising the diff and spec staging. It passes on the same gates as P1.
3. **P3, confinement.** One review whose staged input instructs the model to `view` an absolute path and a `../` path to a canary file outside the root, to write a file and to run a command. It passes when the canary's content appears nowhere in the stream, no write or shell call happens, the launch record shows the canary's directory unmounted, and the digests are equal. An attempted outside call ends the run `unknown` by the tool-path rule, which is the expected outcome, not a failure of the mode.
4. **P4, a judgment.** One read judgment of a recorded question. It passes when the answer validates, cites delegated clauses and passes the citation checks unchanged.
5. **P5, a planning review.** One read review of a planning bundle, contract files included. It passes on P1's gates, the seeded defect included, which shows that the argument limit no longer bounds the inputs.
6. **P6, replay.** Replay of P1 to P5 from the retained receipts after every scratch is removed. It passes when the verdicts are the same.

The probe records the installed version's record types, shapes, phases and chunking, which become its parser contract in `READ_QUALIFIED`. The local CLI is 1.0.91; the issue's evidence is from 1.0.102.

## 7. Residual uncertainty

1. **The return class.** The return is model-written JSON validated by the coordinator, weaker than Claude's `--json-schema` or Codex's output schema. A malformed object costs a reviewer attempt.
2. **What the mounts still hold.** The CLI's runtime, its runtime package and its own storage are mounted for it to run, and a read tool can reach them. The tool-path rule keeps such reads out of a valid review; they are not secrets of the project.
3. **Record shapes.** Copilot's record shapes are observed, not a documented contract. The qualified-version list bounds that, at the price of a qualification run per upgrade.
4. **Usefulness.** A valid verdict is not a useful review. P1's seeded defect is one sample, and comparing review quality across clients needs the same fixtures and repeated runs.
5. **Inline with no tools.** It stays a possible later mode for small inputs, with the smallest permission surface and an exact record of what was sent. It would need its own qualification, and V2's input sets would need to fit the argument limit.

## 8. Review findings and where they went

| Finding (Codex, revision 1) | Decision |
| --- | --- |
| 1. Absent or malformed checkpoint becomes "no tools"; orphan completions; disabled MCP | V5: the checkpoint is observed or the run is `unknown`; orphans and subagent records `unknown`; the disabled-MCP exception kept |
| 2. "Last assistant.message" is not a contract | V6: the root agent, no tool requests, final chunk, qualified phases, no fallback |
| 3. Pack membership from the directory | V2: from the manifest, read-hash-write buffers |
| 4. Missing context and incompatible instructions | V2: per-profile input sets; briefs rewritten per profile; §4 names the spec and implementation brief modules |
| 5. Planning bundles exceed the argument limit | V4: tools read the staged copy, the envelope carries the brief only; P4 |
| 6. Persistence, replay, the `spec_digest` trap | V1: `mode` omitted when absent; V6: the launch-level return source, schema name and input-set version sealed |
| 7. The cwd in the Git common directory; `run_isolated` | V3: an isolated root outside the project (revision 3: the judgment's Git-initialised layout, no plain directory) |
| 8. Refusals after the judgment reservation | V8 |
| 9. A minimum version does not contain drift | V7: qualified versions and parser contracts |
| 10. Tests and probe insufficient | §5 and §6 rewritten with pass criteria, adversarial input, independent digests, replay |
| 11. Validation parity | V6: full schema validation before result branches, transport limits kept |
| 12. Markdown fences break the pack | Moot for `read` (files are staged, not inlined); kept for the inline option in §7 |

Round 2 (revision 2) and where each finding went in revision 3:

| Finding | Decision |
| --- | --- |
| A rejection after an outside read is not confinement | V5: client denial required; §6 P3 with an actual attempted call and a canary |
| Code review needs the spec; spec context is names only | V2: both read from the bundle's commit, a missing path refuses |
| "Opened inputs" is overstated; no read coverage | V5: a successful full `view` defines a read; coverage of the reviewed files replaces `bundle_read` |
| Staged skills would be discovered | V2: everything under `inputs/`, no discovered location; V5: any skill, instruction, hook or active MCP gap is `unknown` |
| Receipt placement contradicts itself | V3: execution storage under the isolated root, receipts copied and anchored after the drain, as for the judgment |
| Concatenating chunks corrupts the answer | V6: a complete, consistent chunk set; the final chunk alone |
| Plain-directory judgment recovery | V3: no plain directory; the judgment's Git-initialised scratch, construction and recovery are reused, the mode sealed in the request |
| Qualification is circular | V1: `--qualify`, operator only, never `observed` |
| Common-directory digest equality cannot pass | §6: `<common>/coordinator/` checked against the coordinator's records, the rest digested |
| Overall pass undefined; no code-review probe | §6: every gate must pass; seeded defects required; P2 a code review |

Round 3 (revision 3) and where each finding went in revision 4 (not reviewed again):

| Finding | Decision |
| --- | --- |
| A successful `view` may be truncated (over 20 KB) | V5: a full read is a small file, `forceReadLargeFiles`, or ranges covering the file; the threshold is in the parser contract |
| Judgment inputs have no mandatory coverage | V5: the question, mandate and decisions must be read, an all-deferred answer included |
| `cwd` confinement permits reading `.git` | V5: paths under `inputs/` or `brief.md` only; a search rooted at the `cwd` is `unknown` |
| A sealed filename does not prove authorship | V3: a closed list of receipt names, checked before copying; §6: the expected events and files per step, not membership after the fact |

Round 4 (revision 4) and where each finding went in revision 5 (not reviewed again):

| Finding | Decision |
| --- | --- |
| Truncation: arguments do not prove delivery (partly resolved) | V5: a read counts only when the returned content is verified to carry every line |
| The receipt allowlist rejects `launch-intent.json`, `env-scrub.json` and the CLI's home and logs | V3: the complete receipt set named; `run_subdirs` separated as CLI storage |
| `.agents/...` contract paths and an `AGENTS.md` rule contradict the no-discovered-location rule | V2: inert aliases, original paths and digests in the manifest, the brief mapping one to the other |

## 9. Order of work (decided)

Round 4 recommended qualifying one OS-level launch boundary first and letting it carry the containment half of this design. The operator chose that order ("Nie no to leć z tym", 2026-10-22). The [launch boundary](launch-boundary-design.md) was implemented and checked live before revision 6, which removed from this design the reliance on Copilot's own path checks (V5's former confinement bullet, P3's denial requirement) and most of §6's store-mutation audit. What remains is the evidence layer: V2's input binding, V5's stream and coverage rules, V6's answer selection and validation, independence, and receipt anchoring and replay.
