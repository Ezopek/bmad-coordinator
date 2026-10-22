# Copilot as reviewer and judge: inline packs, no tools

Status: proposed 2026-10-22 (issue #2; operator: "every harness should be able to take every role"), awaiting the operator's approval and the live probe of §6. Nothing here is implemented. Builds on the [client adapters](claude-adapter-design.md) (§3 the registry, §14 the Copilot client, K1, C11, C12), the [implementation drive](implementation-drive-design.md) (the review step), [judgment v2](judgment-v2-design.md) and the [behavior contract](behavior-contract.md).

## 1. Problem

Every client can be a worker or a planning author. The reviewer and judgment roles take only `claude-code` and `codex-cli`, because both have a write-free read-only mode. `copilot` is refused there with `UNSUPPORTED_CLIENT_MODE` (K1). It returns through a file it writes, so it needs write approval. Path-scoped write approval is unverified (C5, C6), and the inventory misses `.git/**` and ignored paths, so a read-only Copilot run could write without being caught. The adapter design names the way out as C12: path-scoped write approval, or a write-free return channel.

Issue #2 reports the second one, exercised outside the coordinator. A Copilot CLI 1.0.102 run reviewed an inline source pack with an empty offered-tool set (`--available-tools=`), the isolation flags the client already passes, and `--usage-output-file`. It completed with one premium request and no modified file. That is capability evidence for a transport, not evidence of a coordinator mode.

## 2. Goal and non-goals

**Goal.** Copilot may hold the `reviewer` and `judgment` roles through one new, explicitly chosen mode. The coordinator puts every byte the role may read into the prompt and offers the model no tool. It gives the CLI no project directory and reads the verdict from the stream's final assistant message, validated as strictly as any return. Everything after the return stays unchanged: the review record, the provenance class, the independence rule, `critical_open`, the judgment's citation checks, acceptance.

**Non-goals.** Any change to Copilot authors or to `write_free_readonly`. A Copilot reviewer that reads files with tools (a later mode, after its own probe). Any change to the Claude or Codex reviewer and judgment contracts. Any claim of an OS sandbox.

## 3. Decisions

**V1. An explicit mode, opt-in per role.** A `reviewer` or `judgment` role that names `copilot` must also say `"mode": "inline"`. That key is part of the pipeline digest, and `bmad review prepare` takes it as `mode`. Without it the refusal stays `UNSUPPORTED_CLIENT_MODE`, and its message now names the mode. The client gains a capability `inline_readonly`, False by default and True for `copilot` only after the probe (§6). `mode` is refused for the other clients, whose read-only modes are unchanged. Nothing defaults to Copilot.

**V2. The pack.** The coordinator renders the bytes the role may read into the envelope, one fenced block per file. Each block names its manifest path, its sha256 and its byte length.

- A **review** pack holds every file of the sealed copy (artifacts, dependencies, new and untracked files included), the installed `bmad-review/SKILL.md` and the lens files the brief names, which are contract files.
- A **judgment** pack holds the files the isolated run's working directory holds today: the question, the mandate, the decisions and the contributor artifacts.

The pack's digest and the per-file digests are sealed in the launch receipt. For an inline review, `bundle_read` is checked against the pack the coordinator sent. The reviewer's per-file digest attestations are dropped from its schema, because the model never hashes anything. What the model understood and found stays a claim.

A file that is not UTF-8, or a pack whose envelope exceeds `max_envelope_bytes` (100,000 bytes; C11 verified 99,900), is refused with `INVALID_INPUT` before any reservation. The refusal names the size and suggests `claude-code` or `codex-cli` for the role. A story review usually fits; a planning bundle with a long PRD may not.

**V3. No tools and no project.** The argv is `copilot -p ENVELOPE --output-format json --session-id S --model M`, the existing isolation flags (`--no-ask-user`, `--no-auto-update`, `--no-custom-instructions`, `--disable-builtin-mcps`, `--disallow-temp-dir`), `--available-tools=` with an empty value, `--log-dir RUN/copilot-logs` and `-C RUN/inline-cwd`. That last one is a fresh empty directory under the run directory. The argv has no `--add-dir` and no `--allow-tool`. `--no-remote-export` and any further hardening flag the probed version documents are added once the probe confirms them. The per-run `COPILOT_HOME` and the sealed `freshness.json` observation stay as they are.

**V4. The return channel: the final assistant message.** A new `return_source`, `"assistant-message"`, used only by this mode. The runner takes the last `assistant.message` before the `result` record. Its content must be exactly one JSON object, bare or in one fenced `json` block with nothing else around it. That object becomes `last.json`. This is the one bounded exception to §14's rule that an assistant message is never read as a return, and it holds only for a run that offered no tool. Copilot has no schema flag, so the envelope prints the profile schema and a skeleton, as worker-file envelopes already do. The object is then validated against the profile schema exactly like Claude's structured output: required keys, no extra keys, enums. No message, several candidate objects, prose around the object, or a placeholder left in the skeleton each end `unknown`.

**V5. Fail closed on the stream.** The terminal check of §14 applies, plus these rules:

- any `tool.execution_start` record is `unknown`;
- the offered tools a `session.usage_checkpoint` reports must be empty when it is present;
- MCP, hook or subagent records are gaps, and here a gap is `unknown`;
- `exitCode` 0, one `result` last and the preset session hold as for authors.

After the run, the feature worktree's inventory delta must be empty, as for every reviewer, and so must the inline directory. Otherwise the run is `unknown` with the fence held. The CLI's own writes (its session state in `RUN/copilot-home`, its logs in `RUN/copilot-logs`) are expected, sealed as today, and kept apart from project writes. Not offering any tool is a policy, not a sandbox; the delta and directory checks are the audit.

**V6. Facts.** `client_facts` record the mode, the pack digest, `return_basis: "assistant-message"`, the offered tools (`[]`), the Copilot version when the stream reports it, and the session usage (premium requests). The bill reads them unchanged. The model's verdict, the process exit and the coordinator's acceptance stay three separate facts.

**V7. A version gate.** `INLINE_MIN_VERSION` is the Copilot CLI version on which the probe passes. `doctor --client copilot` reports it, and an inline launch on an older or unparsable version is refused with `UNSUPPORTED_CLIENT_MODE` before reservation.

**V8. Independence is unchanged.** A Copilot review's source id (client and session) differs from every author's. Cross-client review stays allowed (R2), and a Copilot reviewer of a Copilot author is another process with another session, as Claude reviewing Claude is today. A judgment keeps its isolated-run rules, with the scratch cwd replaced by the pack.

**V9. Documentation.** The model selection guide, getting started, troubleshooting and the usage reference stop calling Copilot author-only once the probe passes. They state the mode, the pack limit and the weaker return class: model-written JSON validated by the coordinator, not schema-enforced by the client.

## 4. Changes by file (sketch)

- `coordinator_core/clients.py`: `Client.inline_readonly`, the inline argv, the `assistant-message` return source and its reader, the terminal rules of V5, the version gate.
- `coordinator_core/worker.py` (envelopes): rendering the pack and the schema skeleton for the inline mode.
- `coordinator_core/bmad_review.py`: `mode` on review prepare, the pack from the sealed copy and the review skill's files, `bundle_read` checked against the pack.
- `coordinator_core/judgment.py`: the pack from the scratch copies for an inline judgment.
- `coordinator_core/drive_pipeline.py`: `mode` on the `reviewer` and `judgment` roles, validated against the client.
- `coordinator_core/__main__.py`: the client help.
- Docs per V9; `CHANGELOG.md`.
- Tests: the fake Copilot gains an inline mode (no tool records, a final assistant message), plus refusals for each V5 rule and each malformed message of V4.

## 5. Tests

Fake clients only:

- a review and a judgment with an inline Copilot role end observed;
- each V4 malformation, each V5 stream rule and a non-empty delta end `unknown`;
- a pack over the limit and a non-UTF-8 file are refused before reservation;
- `mode` without `copilot`, and `copilot` without `mode`, are refused;
- the pipeline digest covers `mode`;
- Claude and Codex reviewer goldens are unchanged.

## 6. Live probe (C12, inline)

Authorized separately, on a fixture, Copilot only, at most five premium requests:

1. **P1.** One inline review of the example's story bundle: the offered tools empty, no tool record, the final message a valid review object, the usage recorded, the delta empty.
2. **P2.** One inline judgment of a recorded question.
3. **P3.** One pack near 99 KB, to confirm the argv limit on the installed version.

The local CLI is 1.0.91, and the issue's evidence is from 1.0.102. The probe fixes `INLINE_MIN_VERSION`.

## 7. Residual uncertainty

1. The return is model-written JSON validated by the coordinator, a weaker class than Claude's `--json-schema` or Codex's output schema; a malformed object costs a reviewer attempt.
2. The argv limit keeps large planning bundles with Claude and Codex reviewers.
3. A reviewer that cannot read beyond its pack cannot explore the repository. That suits the audit and limits the review's depth, so the review brief says so.
4. Copilot's record shapes (`assistant.message`, `session.usage_checkpoint`) are observed, not documented, and may change between versions; the version gate and V5's fail-closed rules bound that.
