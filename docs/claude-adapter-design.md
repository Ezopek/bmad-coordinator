# Coordinator: Claude Code worker adapter (second client)

Status: technical design, 2026-10-05, revised after design review. Not implementation evidence. Baseline `322752a`: 273 passing tests, SQLite schema 1, Python 3.11+ standard library. It adds Claude Code CLI `claude -p` as a second one-attempt worker beside `codex exec` ([worker adapter](worker-adapter-design.md), [BMAD integration](bmad-integration-design.md), [client records](v1-design.md#client-adapter-and-execution-envelope)). Flag classes come from a local capability record for Claude Code 2.1.289 (`.superpowers/sdd/2026-10-05-claude-adapter/claude-cli-capabilities.md`): **obs** means seen in one live call, **help** means listed by `--help` only, and **docs** means documentation only. Only **obs** counts as verified. A Claude result is Claude-adapter evidence, never Codex evidence.

## 1. Scope

In:
- a closed client registry with a documented adapter contract;
- the client as a recorded field;
- a binary preflight before reservation for both clients;
- a client-agnostic sealed-evidence integrity check;
- an isolated-run entry point for the unattended driver;
- the Claude transport (argv, isolation, structured return, session freshness, coordinator timeout);
- cross-client review independence;
- a fake-client conformance harness;
- a bounded Claude S0–S6 smoke.

Also in, as plan Task 4: the GitHub Copilot CLI as a third registry entry, for authors only (§14).

Out: Copilot IDE chat, driver, Loop, remote/push/merge, BMAD edits, an OS sandbox for Claude, auth probing, retry, and any claim that Claude results prove Codex behavior.

## 2. Decisions

| # | Decision | Reason |
| --- | --- | --- |
| D1 | `coordinator_core/clients.py` holds a closed registry `{codex-cli, claude-code}`, with `copilot` added in plan Task 4 (§14), of frozen records. Tests may inject entries; production never does. | Same pattern as `profiles.py`; no plugin framework. |
| D2 | The client is recorded without migration: in the sealed binding (`client`, written at bind), in the `assignment.launch-reserved` event's `source_id` for every attempt, and in the receipts. `NULL` or absent means `codex-cli`. | `AssignmentInput` feeds the request fingerprint, a column needs a migration, and the prepared event holds the required profile. (R1) |
| D3 | Selection: `client` in `bmad prepare` and `bmad review prepare` JSON; `--client` on `worker run`, `bmad run` and `assignment reserve-launch`. Without a flag, the binding's client applies, else `codex-cli`. A flag that conflicts with the binding is `INVALID_INPUT` before reservation. | One attempt, one client, fixed before any process. (R1) |
| D4 | Codex stays byte-compatible: argv tail, briefs, bindings and spec digests are unchanged, because a default `client` is left out of `spec_sha256` and the binding. New Codex receipts gain only `client` and `binary`; ruling (ii) (Task 4) adds one sidecar, `env-scrub.json`, beside a new Codex `launch.json`, and changes the Codex worker environment: `session_scrub=PARENT_MARKERS` removes a parent agent session's `CLAUDE*` and `ANTHROPIC*` variables and markers before the spawn (§14 notes), while `env_unset`, the argv and the `launch.json` key set stay unchanged. Legacy receipts without `client` replay with the Codex parser. Golden fixtures captured at `322752a` pin all of this. Exception (post-smoke contract fixes): every planning brief, Codex included, now ends its rules with `bmad_planning.RETURN_RULES`; every review brief, Codex included, states `AFFECTED_IDS_RULE` and, after smoke attempt 8, the severity rubric's `SEVERITY_RULES` (a finding that contradicts a mandate clause, or makes output invalid under a standard the mandate requires, is `critical` and cites the clause id; a `critical` disposition sets `critical_open`; no golden pins the review brief); a planning brief that consumes an accepted PRD (`prd_acceptance` or an `accepted-prd` input) lists the inherited decisions from their stored rows with provenance and accepted bundle id (attempt 8), and omits the section otherwise, so the golden PRD brief is unchanged; every envelope's `instructions` state the generic return contract and `affected_ids`. Only the golden `brief.md` and the envelopes' `brief` and `instructions` values were regenerated (fixture README notes), receipts unchanged. | Old receipts and idempotency keys keep validating. (R1) |
| D5 | Both clients are preflighted before `reserve_launch`: resolve the executable on `PATH` to an absolute path and run `--version` (3 s). Failure is `BLOCKED_EVIDENCE`; the assignment stays `prepared` with no sidecar. The absolute path becomes `argv[0]`. | Fixes the missing-binary `launch-pending` defect and PATH re-resolution. (R7) |
| D6 | Thread identity `claude-code:<session_id>`, launch source `claude-code:process`. Independence compares whole source ids across clients; the reviewer prefix must be registered and equal its bound client. | Removes the `codex-cli:`-only check. (R2) |
| D7 | Freshness: the coordinator presets a uuid4 `--session-id`, never passes `--resume`, `--continue` or `--fork-session`, and requires init and result `session_id` to equal the preset. `--no-session-persistence` leaves nothing to resume. | Freshness as far as the client reports it. (R4, R5) |
| D8 | Transport `--output-format stream-json --verbose`, captured to `events.jsonl`. | Init reports tools, servers, mode and cwd; tool records allow audits. A fallback to verified `json` needs a reviewed change. |
| D9 | `--json-schema` takes the existing `_schema(profile)`. After exit the runner writes `canonical(structured_output)` exclusively to `last.json`; `_final` and profile checks stay single; replay recomputes the bytes. | One return receipt shape. (R4) |
| D10 | Confinement class `tool-policy` (Codex: `os-sandbox-requested`). Authors get `acceptEdits`, denied prompts, restricted built-ins and Bash rules for exactly three installed-script prefixes plus a stdin-only `sha256sum` (approved). Reviewers get no write tools, write-capable Bash denied and every Bash call audited. Some read-only shell commands may be auto-allowed by the client without any rule (`git status` was allowed in P0/P0r; an author's `git status … | head; git check-ignore …` was denied in attempt 8); the coordinator never relies on it, and no rule limits reads. Bash rules are **advisory**. The inventory delta, the contract digests and D16 are the audit. | No OS sandbox covers Claude tools. (R3) |
| D11 | Isolation: `--setting-sources ""`, `--strict-mcp-config --mcp-config '{"mcpServers":{}}'`, `--disallowedTools "mcp__*"`, plus env `DISABLE_AUTOUPDATER=1`, `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` and `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`. Every inherited `CLAUDE*` variable (including `CLAUDECODE` and a parent session's messaging socket and token) is removed except the documented authentication and provider variables (`CLAUDE_CONFIG_DIR`, OAuth tokens, `CLAUDE_CODE_USE_*`, `CLAUDE_CODE_SKIP_*_AUTH`, mTLS client credentials). Everything is recorded as `isolation` in `launch.json`: the names (not values) of the inherited `CLAUDE*`/`ANTHROPIC*` variables passed on, of those removed and of those kept. | `""` is verified and stricter than `project`, whose hooks or allow rules could widen the policy. The brief names contract files by path; no project skill or hook auto-loading is relied on. (R5) |
| D12 | Timeout: the existing process-group TERM, 0.5 s grace, KILL; `unknown`; fence kept; no retry. | Claude has no wall-clock flag. (R6) |
| D13 | `--model` alias and `--max-turns` (Claude only, default 60, refused for Codex) are recorded. Model ids, turns and reported cost are client facts. No budget or fallback-model flag. | Recorded parameters; no unverified limit is relied on. (R8) |
| D14 | Claude briefs differ from Codex briefs; Codex text is unchanged apart from the shared `RETURN_RULES`, the review brief's `AFFECTED_IDS_RULE` and `SEVERITY_RULES`, the planning brief's inherited decisions and the envelope instructions (D4):<br>- one client-label sentence;<br>- no `PYTHONDONTWRITEBYTECODE` rule, since the runner sets it in the process env;<br>- the exact allowed commands;<br>- "`uv` is denied, use the skill's documented fallback";<br>- the reviewer runs exactly `sha256sum <sealed paths>` with no env prefix, pipe, redirect or chaining.<br>A `manifest-echo` fallback (digests copied from `manifest.json`, labelled `echo`) is designed but not built in this slice. | An attestation needs a command run; general Bash stays denied. `bundle_read` stays a model claim. |
| D15 | `client_root` stays `.agents/skills` for both clients. | It locates the BMAD contract, not the transport. |
| D16 | **Sealed-evidence integrity**, for every client and run:<br>- At launch (after the inventory, before spawn) the runner records digests of the files already present under `<common-dir>/coordinator/{bindings/, bundles/, runs/*/, judgments/}` and the driver's sealed folders `pipelines/`, `drives/` and `resolutions/`, excluding `runs/<this run>/` and any path with a dot-prefixed component (staging and temp files). In another run directory it records:<br>  - every file except those under the recorded client's `private_subdirs`, if the run has ended. The client is resolved from that run's `launch.json`. A run without `launch.json`, a legacy run (`codex-cli`) or an unregistered client name gets empty `private_subdirs`: nothing is excluded, and the lookup never raises. A run counts as ended when `end.json`, `timeout.json` or `interrupted.json` is present; these are written only after the group drain (§3 step 4);<br>  - otherwise only its `envelope.json`, `return.schema.json`, `launch-intent.json` and `launch.json`.<br>- On **every** exit path (clean or non-zero exit, timeout, interrupt, kill), the process group is drained first (§3 step 4). Then, before any terminal classification, the runner re-digests that list and always writes a private `integrity.json` with `checked`, `modified`, `deleted`, `group_alive_after_exit` and `group_terminated`.<br>- Any modified or deleted file makes the attempt `unknown` with the fence held.<br>- Every sealed file is published atomically, the way `seal_binding` already works: a dot-prefixed temp file in the same directory, fsync, `os.link` to the final name, then removal of the temp. A baseline therefore never sees a half-written sealed file: the file is either absent or complete.<br>- Out of scope: additions (concurrent attempts, operator commands), `state.sqlite3` and its `-journal`/`-wal`/`-shm`, `inbox/` and lock files. | Claude Bash and `uv` are unconfined. D16 detects tampering with sealed evidence, not every common-dir write. The DB is protected by owner fencing and anchored receipts, and every reader ignores unanchored forged additions. Concurrent attempts in other worktrees stay legal. Codex is covered too. |
| D17 | **Isolated run.** `clients.run_isolated` refuses a client whose `write_free_readonly` is False (`UNSUPPORTED_CLIENT_MODE`). Otherwise it launches a read-only process with:<br>- a caller-supplied root, which must lie outside the target project's worktrees and Git common dir; unrelated ancestor repositories are allowed;<br>- under that root, a fresh git-initialised cwd and a receipt directory;<br>- the same envelope, schema, session-freshness, timeout and receipt rules as an attempt.<br>There is no assignment, fence or DB anchor. Receipts stay in the receipt directory during the run; the caller copies and anchors them afterwards. Claude gets `Read,Grep,Glob` only; Codex gets `--sandbox read-only` and `--skip-git-repo-check` (docs). D16 does not apply because no project is involved. A non-empty cwd delta is `unknown`. | The driver's coordinator-judgment seam (driver plan Task 2) needs a judgment process that cannot write to any project's common dir. |

## 3. Client registry

```python
@dataclass(frozen=True)
class LaunchSpec:      # decided by the coordinator; a client only renders it
    binary: str; mode: str; worktree: Path; run_dir: Path; schema: dict
    model: str; max_turns: int | None; session_id: str | None; read_dirs: tuple[str, ...]
    commands: tuple[str, ...]                    # exact author Bash prefixes from the binding, else ()
    isolated: bool = False                       # D17: read-only, no Bash, no add-dir
    return_path: str | None = None               # worker-file clients only; also named in the envelope

@dataclass(frozen=True)
class Terminal:        # replayable from retained bytes
    thread_id: str; final: bytes; facts: dict

@dataclass(frozen=True)
class Client:
    name: str; executable: str; confinement: str
    presets_session: bool
    return_source: str                           # "client-file" | "stdout" | "worker-file" (T1)
    envelope_via: str                            # "stdin" | "argv"; argv uses ENVELOPE_ARG (T2)
    max_envelope_bytes: int | None               # checked before reserve_launch (T3)
    write_free_readonly: bool                    # False: no reviewer or isolated launch (T6)
    max_turns_default: int | None                # None: --max-turns is refused
    env: dict[str, str]                          # static env; also used by preflight; recorded
    env_unset: frozenset[str]                    # removed from the inherited env; names recorded (T4)
    run_subdirs: tuple[str, ...]                 # created empty, 0700, in the run dir before spawn (T4)
    private_subdirs: tuple[str, ...]             # subset of run_subdirs the client may keep mutating; excluded
                                                 # from other runs' D16 baselines; never `worker-return`
    observation_name: str | None                 # sealed post-run observation, e.g. "freshness.json"
    internal_tools: frozenset[str]               # tool names the client reports beyond those requested;
                                                 # set only from probe observation by a reviewed change
    run_env: Callable[[LaunchSpec], dict[str, str]]       # per-run env, e.g. a home inside run_subdirs (T4)
    allowed_tools: Callable[[str], frozenset[str]]        # mode -> requested tools | internal_tools (T5)
    argv: Callable[[LaunchSpec], list[str]]
    launch_facts: Callable[[LaunchSpec], dict]
    observe: Callable[[Path, dict], dict] | None # once, at return time: live post-run facts (folders, $HOME);
                                                 # the runner seals the result as observation_name
    terminal: Callable[[Path, dict], Terminal]   # pure function of sealed or digest-bound run-dir bytes
                                                 # (stdout capture, observation, return file); never reads
                                                 # live folders or $HOME; raises UNKNOWN_EVIDENCE (T5)
```

Helpers:
- `get(name)`;
- `preflight(client) -> (binary, version)`, run with the static `env` only;
- `source_id(name, thread)` and `client_of(source_id)`;
- `read_worker_return(run_dir) -> bytes` (T1), used by `run_assignment`. It checks transport shape only:
  1. it opens `RUN/worker-return` with `O_RDONLY|O_DIRECTORY|O_NOFOLLOW` and requires the listing to be exactly `["return.json"]`;
  2. it opens the file with `os.open("return.json", O_RDONLY|O_NOFOLLOW|O_NONBLOCK, dir_fd=fd)` and requires `fstat` to show a regular file with `st_nlink == 1` and size at most 1 MB;
  3. it reads the file as UTF-8 and pre-scans nesting depth outside strings, at most 64;
  4. it requires `json.loads` to yield one object, and returns canonical bytes.

  `ValueError`, `RecursionError`, `UnicodeError` or `OSError` becomes `UNKNOWN_EVIDENCE`. Field validation is not its job: `_final` checks the exact generic key set, types and identity, and the profile `check` validates only the extras it reads. No full JSON-schema validator exists, so an extra field the profile does not read is not type-checked.

Return sources:
- `client-file`: Codex writes `last.json`.
- `stdout`: the runner writes `last.json` from `Terminal.final` (Claude).
- `worker-file`: the runner writes `last.json` from `read_worker_return`, and `return.json` records `worker_return_sha256`. On replay, `Terminal.final` is recomputed from the retained file through `read_worker_return` and must equal `last.json`. The envelope gains an optional `return_path` key only for this source, so Codex envelope bytes are unchanged.

**Sealed observations (N1).** A client with `observe` gets its live post-run facts collected once, after the process group is gone and before `terminal`. The runner seals them as `observation_name`, and `return.json` records `observation_sha256`. Replay, `inspect` and `reconcile` read only that seal. Codex and Claude have no `observe`, so their receipts are unchanged.

With `envelope_via == "argv"`, the runner substitutes the `ENVELOPE_ARG` placeholder at spawn and records `@envelope.json` in `launch.json`; `envelope_sha256` binds it.

The Codex entry is today's argv and `_terminal`, moved unchanged: `return_source="client-file"`, `envelope_via="stdin"`, `write_free_readonly=True`.

`run_assignment(..., client=None, max_turns=None)`:
1. resolves the client (flag, else binding, else default). Before any preflight, it refuses with `UNSUPPORTED_CLIENT_MODE` (exit 4) a read-only assignment for a client whose `write_free_readonly` is False. It also renders a size-equivalent envelope, with the inventory digest replaced by 64 placeholder characters, and refuses one above `max_envelope_bytes` with `INVALID_INPUT`. Only then does it preflight. None of these refusals costs an attempt;
2. calls `reserve_launch(owner, id, client)`, then takes the inventory and the D16 baseline;
3. creates the run directory exclusively, creates `run_subdirs` empty with mode 0700, and spawns with `{**inherited − env_unset, **env, **run_env(spec), **profile.env}`. It writes `launch-intent.json` first;
4. on every exit path, after the lead process ends, drains the process group (N2). Every worker spawn, including `run_isolated` and the P0/P0c probes, uses `start_new_session=True`, as `worker.py` already does. The group is therefore exclusively the run's, and the runner asserts `os.getpgid(pid) == pid` before any `killpg`.
   - It waits up to 2 s for `killpg(pgid, 0)` to fail. If a member is still alive, it sends SIGTERM, waits 2 s, sends SIGKILL and polls 2 s more. The timeout path keeps its TERM/KILL and then does the same check.
   - `end.json`, `timeout.json` and `interrupted.json` are written only after this drain completes, so D16 counts a run as ended only once no lingering writer exists. Their bytes are unchanged.
   - Only then does it take the worktree `after` inventory, run D16 and write `integrity.json`. That file records `group_alive_after_exit` (a member outlived the leader), `group_signals` and `group_terminated` (the group is gone, naturally or by signal).
   - A group that survives SIGKILL gives `group_terminated=false`, and the attempt is `unknown` with the fence held.
   - A **clean** `integrity.json` has empty `modified` and `deleted` and `group_terminated=true`.
   - The launch and return receipts are unchanged, so Codex byte-identity holds. Then `observe` runs, if the client has one;
5. only then calls `terminal`, takes the return per `return_source` into `last.json`, and runs `_final` and `_return_evidence`.

A D16 failure, timeout or interrupt returns `unknown` before step 5. Replay of a receipt that has a `client` key requires a clean `integrity.json`.

`read_dirs` is the binding's `sealed_root` when present. `_validated_receipt` takes the client from `launch.json` (absent: Codex). It checks the client against the reservation event, the return, the source prefix and the binding, then replays the same `terminal`. Facts are compared with `value.get("client_facts", {})`. The Claude `terminal` reads one file outside the run directory: for the reviewer Bash audit, the sealed binding named by `launch.json`'s `binding_sha256`, digest-checked by `read_binding`.

**Isolated run (D17).**

```python
def run_isolated(client: str, target: RepoIdentity, root: Path, cwd: Path, receipt_dir: Path, envelope: dict,
                 schema: dict, model: str, timeout_seconds: int, max_turns: int | None = None) -> IsolatedRun
# IsolatedRun(state: "observed"|"unknown", source_id, final: dict | None, receipt_dir,
#             launch_sha256, return_sha256, needs)
```

Before any spawn, `UNSUPPORTED_CLIENT_MODE` refuses a client whose `write_free_readonly` is False, and `INVALID_INPUT`/`UNSAFE_PATH` refuses:
- a `root`, `cwd` or `receipt_dir` that is not absolute or is a symlink;
- a `cwd` or `receipt_dir` that is not under `root`;
- any of the three inside a worktree of `target` (from `git worktree list --porcelain`) or inside its Git common dir;
- a `cwd` that is itself a worktree of `target`;
- a `cwd` whose git toplevel and common dir are not its own (`cwd/.git`);
- a `receipt_dir` that already exists or lies inside `cwd`.

An unrelated ancestor repository above `root`, such as a developer's test tree, is not refused; doctor may only warn about it. Tests pass an ext4 root under the test temp tree.

The run then follows the attempt sequence: the envelope size check, preflight, exclusive 0700 `receipt_dir` and `run_subdirs`, sealed `envelope.json` and `launch.json`, process-group timeout and drain, `observe`, `terminal`, `last.json` per `return_source` (`client-file` or `stdout` only, because `worker-file` clients are never write-free), and sealed `return.json`. It adds a top-level `required`/`additionalProperties` check against `schema` and an empty-delta check of `cwd`. Deeper schema validation is the caller's.

`observed` means well-formed, sealed client transport only; it is unanchored until the caller records the digests. `stop_group` moves to `clients.py`, and `worker._stop_group` aliases it.

## 4. Adapter contract

A client entry must provide:

1. name (also the source-id prefix) and executable;
2. a preflight: absolute path and version, no model call;
3. argv for a `workspace-write` author from a `LaunchSpec`, plus, only when `write_free_readonly`, a `read-only` reviewer and an `isolated` run (no write approval at all, no shell, flags for a non-project directory). Each is an argv array with the given cwd and the envelope via `envelope_via` (stdin, or the `ENVELOPE_ARG` placeholder), with no resume, continue or bypass flags;
4. `max_envelope_bytes`, the static `env`, `env_unset`, `run_env(spec)`, `run_subdirs` and `private_subdirs`, all recorded (names only for inherited secrets);
5. a deterministic terminal-proof rule, yielding one thread id and the final bytes or `UNKNOWN_EVIDENCE`. **Replay must be a pure function of sealed or digest-bound bytes.** Any client-specific post-run observation of live state (folders, `$HOME`) happens once in `observe`, at return time, and is sealed; `terminal` never reads live state. Tool checks are by declaration:
   - a client with `allowed_tools` fails closed on any reported tool outside `allowed_tools(mode)`; MCP names are isolation gaps, not failures;
   - such a client that reports no tool information records the check `blocked`, never passed;
   - Codex declares none and records `not-checked`;
6. the id under the `<client>:<id>` convention;
7. a `return_source`, so the structured return reaches the shared `last.json`;
8. `write_free_readonly`: True only when read-only modes need no write approval of any kind;
9. a confinement class string;
10. timeout notes (the runner's TERM/KILL applies; adapters add no retry);
11. doctor checks;
12. a fake executable and a conformance subclass (§10).

`run_isolated` is generic over these items, so a new adapter gets it without client-specific code.

An adapter never decides provenance class, scope or delta validity, D16, fence release, independence, `critical_open`, profile validity or acceptance. It returns data; `ReceiptEvidenceSource`, the profiles and the core classify it.

**Model-written adapters.** An adapter encodes how raw client output becomes evidence. A grammar that accepts a missing terminal record or a foreign session id would make invalid output look observed. A model-written adapter is therefore a model claim. Conformance tests prove only that the coordinator rules hold for the protocol as the fake encodes it. A live pilot must show that the real client emits that protocol, and independent review must compare the fake with the observed bytes. Even then the evidence covers that client's transport only.

## 5. Claude argv

Both roles: `ABS_CLAUDE -p --output-format stream-json --verbose --session-id UUID --no-session-persistence --model ALIAS --max-turns N --json-schema SCHEMA --setting-sources "" --strict-mcp-config --mcp-config {"mcpServers":{}} --disallowedTools "mcp__*"`. Stdin carries the envelope; cwd is the worktree `W`.

Author (`workspace-write`): under `acceptEdits` any tool, including write-capable Bash, may write inside the worktree except where a deny rule applies, and writes outside it are denied (smoke attempt 2); the worktree audit is the inventory delta, D16 and the contract digests. The single `--disallowedTools` continues after `mcp__*` with 24 deny rules: `Edit`, `Write`, `MultiEdit` and `NotebookEdit` on each of `_bmad/**`, `.agents/**`, `.claude/**`, `.bmad-coordinator/**`, `AGENTS.md` and `CLAUDE.md` (cwd-relative path syntax, docs-only, advisory). Then `--tools Read,Grep,Glob,Edit,Write,Bash --permission-mode acceptEdits --permission-prompts none`, and for a bound author `--allowedTools` with one argument per rule, exactly these:
- `Bash(uv run --no-project W/_bmad/scripts/resolve_customization.py --skill W/ROOT/SKILL *)`;
- `Bash(uv run --no-project W/_bmad/scripts/resolve_config.py --project-root W *)`, the architecture skill's activation call;
- `Bash(uv run --no-project W/_bmad/scripts/memlog.py init --workspace WS *)` and `Bash(uv run --no-project W/_bmad/scripts/memlog.py append --workspace WS *)`;
- for `bmad-architecture` only (amended 2026-10-22, issue #1): `Bash(uv run --no-project W/ROOT/bmad-architecture/scripts/lint_spine.py --workspace WS --output WS/.lint-spine.json *)`, the Reviewer Gate's lint. Its report goes to a workspace file rather than stdout, so the Copilot launcher (§14) refuses any other `--output` (repeated, abbreviated or short) and the report lies in the audited delta; the skill's own stdout form is not bound;
- `Bash(sha256sum)`, stdin only, as the pipe target for the customization digest. This rule is advisory and audited only by the inventory delta and D16.

A generic (unbound) author gets no `--allowedTools`: Bash stays in `--tools`, and `--permission-prompts none` denies every call that would need approval. Paths with `*?[]{},()` or control characters are refused before any rule is built. The scripts are stdlib-only with inline metadata, so every prefix carries `--no-project`.

The brief spells out these exact forms. The skills' own calls without `--no-project` are denied; `resolve_customization.py` falls back to reading `customize.toml`, as the skills document.

Amendment 2026-10-22 (issue #1, BMAD 6.12): a bound author has no subagent tool (no `Agent` in the author tools; Copilot's pinned set drops `task`), while the PRD and architecture skills dispatch subagents for research, input reconciliation and the Reviewer Gate's rubric walker and lenses. Every bound Claude or Copilot planning brief therefore says that no subagent tool is offered, that those steps take the skills' documented sequential fallback in the author's own context, each review written to its file under `WS/reviews/` first, and that the author adds one `assumptions` entry for the sequential gate. That gate is the author's self-check and is never counted as independent review; the pipeline's review step is. `bmad discover` reports in `facts.unbound_scripts` every other `uv run` script the required skills document (6.12: `bmad-review/scripts/word_metrics.py`, whose word counts fall back to estimates), outside the fingerprint, so an operator sees before any model call what a bound author must do without. The one-result rule of §6 is unchanged: background subagents would need their own design (their lifecycle bound to receipts), not a looser parser.

Reviewer (`read-only`): `--tools Read,Grep,Glob,Bash --permission-mode dontAsk --allowedTools "Read,Grep,Glob,Bash(sha256sum *)"`, plus `--add-dir SEALED_ROOT` for a bound review. The reviewer policy is: no write tools, write-capable Bash denied (a `>` redirect was denied in P0r), every Bash call recorded and audited. It does not limit Bash to `sha256sum`: read-only commands such as `git status`, `… | cat` or `…; git status` ran without any rule in P0r. Such an allowance is the client's and may differ by command (attempt 8 denied an author's `git status … | head; git check-ignore …`); the coordinator never relies on it. Only a bare `sha256sum` over sealed paths counts as read evidence (§6).

Isolated (D17): `--tools Read,Grep,Glob --permission-mode dontAsk --allowedTools Read,Grep,Glob`, with no Bash and no `--add-dir`. The init allowlist is then `Read,Grep,Glob` plus `internal_tools`.

| Flag or mechanism | Class |
| --- | --- |
| `-p` with the prompt on stdin, `--model` alias, `--max-turns`, `--setting-sources ""`, `--version` | obs |
| `--output-format stream-json`, `--verbose` (record shapes; whether `--verbose` is required stays unknown) | obs (2.1.289 smoke) |
| `--session-id` echoed with `--no-session-persistence` | obs; duplicate-id rejection unknown |
| `--json-schema` (both profile schemas accepted, `structured_output` returned) | obs; failure subtype unknown |
| `--strict-mcp-config`, `--mcp-config` (empty `mcp_servers`) | obs |
| `--disallowedTools` one occurrence with `mcp__*` and the author deny rules; `Write(_bmad/**)` fired | obs; `mcp__*` survival masked by the empty config |
| `--tools` (init lists them plus `StructuredOutput`), `--permission-mode acceptEdits` and `dontAsk`, `--permission-prompts none`, both `--allowedTools` forms, `--add-dir` reads | obs |
| Bash patterns: ` *` matched a bare call, a pipe matched two rules, `>` denied; some read-only commands auto-allowed without a rule, another denied (attempt 8); a `$M` variable form allowed (attempt 8) | obs; env prefix, `&&`, `$( )` docs; variable expansion before matching unknown (§13 #21) |
| `DISABLE_AUTOUPDATER`, `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS`, `CLAUDE_CODE_DISABLE_AUTO_MEMORY` (auto memory: docs say no memory files are created or loaded) | docs, bin |

**Bash confinement is advisory.** Some read-only commands may run without any rule (the coordinator never relies on it); matching against `&&`, env prefixes and `$( )` is docs-only, and whether a shell variable is expanded before matching is unknown (§13 #21). An allowed `uv run` executes a script that can reach anything the user can, including coordinator state in the git common dir, which Codex's requested sandbox would exclude for a linked worktree. D16 (tampering with pre-existing sealed evidence) and the inventory delta are the checks. Neither sees reads, network, DB writes, additions or writes elsewhere; owner fencing and anchored receipts protect the DB.

## 6. Event and evidence mapping

| Concern | Codex | Claude |
| --- | --- | --- |
| stdout capture | `events.jsonl`, JSONL events | `events.jsonl`, stream-json records |
| start, identity | one `thread.started.thread_id`, client-chosen | first record `system/init`, `session_id` = preset |
| terminal | one `turn.completed`, last, no `turn.failed` | one `result`, last, `subtype: success`, `is_error: false` |
| final result | `--output-last-message last.json` | `result.structured_output`; runner writes `last.json` |
| source ids | `codex-cli:process`, `codex-cli:<thread>` | `claude-code:process`, `claude-code:<session>` |
| confinement | `--sandbox` (requested OS sandbox) | tool policy; init `tools`, `permissionMode`, `cwd` |
| denials | not reported | `permission_denials`, a client fact |
| reads | command strings | Read and `sha256sum` `tool_use` records with their `tool_result` |
| timeout | coordinator TERM/KILL | same; 143 expected, no result |

Claude terminal rule: every line is a JSON object with a string `type`; exactly one init, first; exactly one result, last; every `session_id` equals the preset; the result succeeds with an object `structured_output`; exit 0. Init `cwd` and `permissionMode` must match when present; their absence is a recorded gap. **Allowlist:** init `tools` must be a subset of the requested `--tools`, `internal_tools` (empty until P0 shows what the client adds) and `mcp__*` names. Any other tool makes the run `unknown`. MCP names and servers go into `isolation_gaps`.

**Reviewer Bash audit** (downgrade only): `bundle_read_basis` is `command` only when every Bash `tool_use`, denied or not, is `sha256sum` followed by paths under `SEALED_ROOT`. Those paths may use only `[A-Za-z0-9._/-]`, no `..`, and no shell metacharacters. Every sealed file must also appear with a non-error result. Otherwise the basis is `claim`; a missing stream also gives `claim`. The basis never promotes `bundle_read` beyond a model claim.

## 7. Receipts and schema

| Record | Codex | Claude |
| --- | --- | --- |
| binding | unchanged, no `client` key | `client: "claude-code"` |
| reservation event source | `codex-cli` (legacy `NULL`) | `claude-code` |
| `launch.json` | adds `client`, `binary` | Codex keys without `requested_sandbox`; adds `client`, `binary`, `requested_mode`, `confinement`, `isolation` (flags, env, inherited names), `session_id`, `max_turns` |
| `return.json` | adds `client` | adds `client`, `client_facts`: denials, turns, terminal reason, model ids, reported cost, init tools/servers/mode, `isolation_gaps`, `bundle_read_basis` |
| `last.json` | written by Codex | runner-written canonical `structured_output` |
| `integrity.json` | always, on every exit path | same |
| `env-scrub.json` | new sidecar (parent-session scrub patterns and names; an unanchored record, §14 notes) | not written |

**Backward compatibility (R1).** R1 protects existing receipts: pre-change Codex receipts must keep replaying and validating unchanged. New Codex receipts may gain `client`, `binary` and an absolute `argv[0]`. The single key-set test edit this causes is pre-authorized.

No DB migration is needed: `events.source_id` is unused on reservation rows, bindings and receipts are files, and export carries every event column. Rejected: a new column (migration), an `AssignmentInput` field (it changes every request fingerprint), and overloading the prepared event.

## 8. Evidence classes

An **observed** Claude return means:
- a coordinator-launched process with recorded argv and binary ran in the recorded worktree and exited 0;
- the retained stream names the preset session in init and result;
- the coordinator's `_final` and profile check accepted the structured output;
- the anchored receipt replays;
- the git-visible delta equals the claimed in-scope paths;
- no pre-existing sealed evidence under the common dir was modified or deleted (D16).

**Client-reported**: schema validation, init facts, denials, tool records, model ids, turns, cost. **Requested policy**: argv flags and Bash rules (advisory). **Unobserved**: effective enforcement, reads, network, writes outside the inventory, DB writes and common-dir additions, descendants, reading and understanding, account-context injection.

## 9. Failure behavior

| Situation | Behavior |
| --- | --- |
| Missing binary or failed `--version` | `BLOCKED_EVIDENCE` before reservation; `prepared`; no sidecar (both clients) |
| Unknown client, Codex `--max-turns`, flag versus binding conflict | `INVALID_INPUT` before reservation |
| Envelope above `max_envelope_bytes` (size-equivalent render) | `INVALID_INPUT` before reservation; no attempt consumed |
| Reviewer, read-only or isolated request for a client without `write_free_readonly` | `UNSUPPORTED_CLIENT_MODE` (exit 4) before reservation |
| Auth missing or expired | not preflighted. P0 (§11) runs before any assignment, so failing there costs no attempt and holds no fence. Later: an `unknown` attempt, fence kept by design. |
| Permission denied | recorded in `client_facts`; not invalid by itself; delta and profile decide |
| Max turns hit (`error_max_turns`) | `unknown`, fence kept |
| Timeout | TERM/KILL group, `timeout.json`, `unknown`, fence kept |
| Session mismatch or missing init | `unknown` |
| Schema invalid (error subtype, no `structured_output`, `_final` mismatch) | `unknown` |
| Truncated stream or non-JSON line | `unknown` |
| Init tool outside the allowlist, cwd or mode mismatch | `unknown` |
| Pre-existing sealed evidence modified or deleted during the run (D16), on any exit path | `unknown`, `integrity.json`, fence kept (both clients); additions, DB, inbox and locks are out of scope |
| MCP tools or servers present | `isolation_gaps`; valid transport evidence; smoke connector gate `fail` with a follow-up |
| Reviewer Bash outside the audit grammar | `bundle_read_basis: claim`; smoke S4 read gate `fail` |
| Raw bytes edited after the anchor | `inspect` and `reconcile` give `unknown` |
| Isolated run: project, nested or non-git cwd, or reused receipt dir | refused before spawn |
| Isolated run: timeout, schema-invalid, bad stream, cwd delta | `unknown`; caller decides; no fence exists |

## 10. Package, CLI and tests

A new error code, `UNSUPPORTED_CLIENT_MODE`, exits 4. `coordinator_core/clients.py` joins `package/manifest.json`; the package revision changes, so fixtures reinstall. The CLI gains `--client` and `--max-turns` and a client-neutral description. `doctor --base REF --client NAME` adds `client: {name, binary, version, ready, reasons}` and folds it into readiness.

**Golden fixtures.** Before any edit, golden Codex fixtures are captured at `322752a` with the fake `codex`:
- receipts, normalized for paths, pids, times and uuids;
- argv tail;
- the brief and binding;
- `spec_sha256`.

**Fakes.** `tests/fake_clients.py` holds one fake per client, sharing the side-effect knobs `FAKE_COUNT`, `FAKE_MODE`, `FAKE_WRITE`, `FAKE_LINK`, `FAKE_DELETE`, `FAKE_EXTRA`, `FAKE_ENV_OUT`, `FAKE_OUTSIDE_WRITE`, `FAKE_TAMPER`, `FAKE_WAIT_FOR` and `FAKE_LINGER`. `FAKE_LINGER` (`write|hang`) forks a child that writes a worktree file about 0.5 s after the parent exits, then exits (`write`) or sleeps until signalled (`hang`).
- The fake `claude` asserts the §5 argv contract, including the absence of resume, continue, fork and bypass flags.
- It emits init, optional Read and Bash `tool_use`/`tool_result` pairs (`FAKE_READ`, `FAKE_BASH`) and a result.
- Its other knobs are `FAKE_SESSION`, `FAKE_SUBTYPE`, `FAKE_DENIALS`, `FAKE_TOOLS` and `FAKE_MCP`.
- Its modes are `truncated`, `no-init`, `no-structured`, `is-error`, `auth-fail`, `bad-schema`, `sleep` and `exit-1`.

**Test clients.** Two test-only adapters are injected with `patch.dict(clients.REGISTRY)` and removed by `addCleanup`. Their fakes speak a minimal JSONL protocol:
- `alt-stdout`: argv `run --cwd C --model M --session S --prompt ENVELOPE_ARG`. It emits `{"type":"start","session":S}`, optional `{"type":"tool","name":N,"path":P}`, and `{"type":"end","session":S,"ok":true,"final":{…}}`. It is write-free and has `allowed_tools`.
- `alt-file`: argv `go --cwd C --session S --return R` with the envelope on stdin. It writes `R`, emits `start`, `tool` and `end` without `final`, and uses `run_env` `ALT_HOME=RUN/alt-home`, `private_subdirs=("alt-home",)`, `env_unset={"ALT_ALLOW_ALL"}` and an `observe` that seals `freshness.json`. It is not write-free and has a small `max_envelope_bytes`.

**Conformance.** `tests/test_client_conformance.py` runs one mixin per subclass: Codex and the two test clients (Task 1), Claude (Task 2) and Copilot (Task 4). The production adapters always run with their fakes. Each test asserts the client's declared behaviour, or the declared absence of a feature. The mixin covers:
- fresh id and observed replay;
- read-only zero delta;
- kill to `unknown` with the fence kept;
- invalid runs to `unknown`;
- missing or failing binary refused before reservation;
- D16 tampering with sealed receipts and bindings on every exit path, and concurrent legal writes that must not trip it;
- cross-client independence;
- isolated runs: fresh id, kill to `unknown`, schema-invalid to `unknown`;
- every `return_source` and `envelope_via` variant, and `UNSUPPORTED_CLIENT_MODE` for a client without `write_free_readonly`;
- the tool-check declaration;
- replay from sealed bytes only, with live folders and `HOME` changed after return;
- a lingering group member: `FAKE_LINGER=write` writes after the leader exits and then ends; `FAKE_LINGER=hang` writes and stays until signalled. The late write is caught by the delta, and `end.json` appears only after the member is gone.

A guard test requires a subclass for every registry entry. No test may reach a real client.

## 11. Claude S0–S6 smoke

Changes from the Codex probe plan:

- **Runs:** at most seven.
  - P0 runs first, on `haiku`, in a throwaway directory with no assignment and no worktree. It uses the author argv rendered by the adapter and the `bmad-review-v1` schema, filled with placeholders. Its Bash rules point at a harmless stdlib `probe.py` in the throwaway directory, with the stdin-only `sha256sum` rule. It runs the bare form and the piped form `… | sha256sum` (both expected allowed). It also runs the negative controls of plan Task 3 (piped and chained `touch` outside the cwd, `touch` under a deny-ruled path; all expected denied) and the variable-expansion check (§13 #21): `X=<non-allowed script>; uv run --no-project $X` must be denied, and `M=<allowed script>; uv run --no-project $M` is denied unless the CLI expands variables before matching. `run_isolated` is read-only and not used for P0. The adapter's terminal rule checks it offline. P0 also makes one file write and one outside write, which is expected denied with the outside file absent. Its observed init tools set `internal_tools` through a reviewed constant change before S1, and that is the only code change the smoke may wait for.
  - Up to six S-runs, five planned: S1 and S3 authors on `sonnet`, S4 reviewer on `opus`, S6 architecture on `sonnet` with an optional review on `opus`. The sixth slot is only for a reviewer-argv throwaway probe before S4, under P0's rules; it is never a retry.
- **Setup:** a fresh ext4 fixture under `/var/tmp`; `doctor --client claude-code`; prepare JSON with `"client":"claude-code"`.
- **`uv`:** it runs unsandboxed with its default cache, outside the inventory and unobserved; a `uv` denial is `blocked`.
- **Isolation gates:**
  - connectors, checked in this order: `fail` when any `mcp__*` tool or non-empty `mcp_servers` is present; `pass` when `mcp_servers` is present and empty; `blocked` when the field is absent;
  - account context: `fail` if observed, else `blocked (unobserved)`, never `pass`.
- **S4 read gate:** `pass` when `bundle_read_basis` is `command`; `fail` when it is `claim` with a stream; `blocked` without a stream.
- **Scoring:** missing evidence is `blocked`; contradicted evidence is `fail`. The smoke stops at the first `unknown` or invalid run. Codex gates stay not run.

## 12. What this slice does not prove

- Codex behavior;
- OS confinement;
- that Bash or `uv` did nothing outside the inventory, nothing to the DB, and added no unanchored files to the common dir;
- fresh model context beyond the absence of resume;
- removal of account-level context;
- independence beyond distinct processes and ids (same vendor is weak; the smoke report records it);
- reading or understanding;
- cost accuracy;
- fresh-request activation under Claude;
- for Copilot: write scoping, the tool vocabulary when no tool events appear, and a schema-enforced return.

Fake tests prove coordinator logic only.

## 13. Unknowns requiring the live probe

1. A prompt on stdin with no prompt argument. **Verified** (3/3 smoke runs).
2. Stream-json shapes: `system/init` first with `session_id`, `cwd`, `tools`, `mcp_servers`, `permissionMode` and `model`; one final `result` with `subtype`, `is_error`, `structured_output`, `permission_denials`, `num_turns` and `modelUsage`; no non-JSON lines; the `--verbose` requirement. **Verified**: init first with all six fields, one final result with all six fields, 0 non-JSON lines; in between `assistant`, `user`, `rate_limit_event`, `system/thinking_tokens` and `system/permission_denied`. Whether `--verbose` is required stays **unknown**.
3. `--session-id` echoed, also with `--no-session-persistence`; duplicate-id rejection. Echo **verified** with `--no-session-persistence`; duplicate-id rejection **unknown**.
4. `--json-schema` accepting the coordinator schemas (`additionalProperties: false`, `enum`, nullable strings, nested profile objects); its failure subtype and exit code. Acceptance **verified** for `bmad-review-v1` and `bmad-planning-v1`; the failure subtype and exit code stay **unknown**.
5. Strict empty MCP config plus `--disallowedTools "mcp__*"` removing account connectors. Whether injected account context can be seen at all. Empty `mcp_servers` and no `mcp__*` tool **verified**; account-context visibility stays **unknown** (gate `blocked (unobserved)`).
6. Which internal tools init lists beyond `--tools` (this sets `internal_tools`). **Verified**: `StructuredOutput` only (added by `--json-schema`); `internal_tools` set by a reviewed change.
7. `acceptEdits` with `--permission-prompts none`: in-cwd edits approved; out-of-cwd Write and unlisted Bash denied and listed. **Partly**: inside the cwd any tool may write, including write-capable Bash such as `touch` (bare, piped or chained after an allowed prefix; attempt 2), except the Write deny-ruled paths; out-of-cwd Write and Bash writes (`tee`, a `python3` heredoc) are denied and listed; some read-only Bash commands were auto-allowed without any rule, but attempt 8 denied an author's `git status … | head; git check-ignore …`, so the coordinator never relies on such an allowance. Whether the file-tool deny rules also stop a Bash write under `_bmad/` is **unknown** (P0 control). The worktree audit is the inventory delta, D16 and the contract digests; `acceptEdits` stays for authors (coordinator ruling).
8. `dontAsk` with the reviewer allowlist and `--add-dir`: worktree and sealed-root reads allowed; other reads are treated as unconfined. **Partly**: worktree and add-dir reads allowed; outside reads also allowed (unconfined, as designed).
9. Bash pattern semantics: env prefix, pipes (`uv run … | sha256sum` against two rules), `;`, `&&`, redirects and `$( )`. **Partly**: a pipe matched against two rules; `>` denied; env prefix, `&&` and `$( )` **unknown**. Attempt 8 (2.1.290): `<allowed> | sha256sum; <allowed>` allowed; `| tee /tmp/…` and a `cd … && python3 - <<'EOF'` heredoc denied again; variable forms are #21.
10. Exit codes and records for max turns, schema failure, auth failure and SIGTERM. **Unknown** (no failing run reached these paths).
11. Resolution of the `sonnet`, `opus` and `haiku` aliases. **Partly**: `haiku` and `sonnet` resolved; `opus` never ran.
12. Whether the architecture skill's `resolve_config.py` call matches its exact-prefix rule as the model writes it, since the skill documents no fallback. S6 may still block for other reasons. **Unknown** (S1 stopped before S6).
13. Whether `--allowedTools` rules ending in ` *` match bare invocations with no further arguments (P0 probes it). **Verified**: a ` *` rule matched a bare call.
14. Whether Claude writes into the worktree on its own, and `claude -p` started from inside a Claude Code session. **Partly**: no `.claude/` written; a nested launch from inside a Claude Code session works; 11 parent `CLAUDE*` names were inherited (now scrubbed, #20).
15. Whether one `--disallowedTools` with several values keeps `mcp__*` and the author deny rules, and whether a deny rule fires: the P0 author probe attempts one Write under `_bmad/` and expects it denied, listed and absent. Deny rule firing **verified** (`Write(_bmad/**)` denied and listed); `mcp__*` survival in the merged flag **unknown** (masked by the empty MCP config).
16. Records before init: whether any record (for example a `system` hook or policy event) precedes `system/init`. The terminal rule makes such a run `unknown`, so P0 records the exact stream prefix. **Verified**: no record precedes init (3/3).
17. `uv run --no-project` reach: whether uv still reads worktree `uv.toml` or `pyproject.toml` `[tool.uv]` settings or a `.venv` interpreter. `--no-project` stops project discovery and sync; configuration discovery is not disabled. Until P0 shows otherwise, a model-written uv configuration is a documented limit: if left in place, the inventory delta catches it; if restored, it is undetected. **Unknown**: no uv configuration existed anywhere on the host, so reach was not exercised. For Copilot authors the launcher narrows this to what uv reads from outside the worktree. It runs `uv run --no-config --no-project --python <coordinator interpreter>`, so uv reads no worktree `uv.toml` or `pyproject.toml` (fix round 3), and uv never discovers a worktree or parent `.venv` for the interpreter (security round); a coordinator interpreter that itself lies under the worktree or its common directory is refused before reservation (return-schema round, m1). Both verified on uv 0.12.5: a planted malformed `uv.toml` fails a plain run and a planted `cache-dir` is honoured, and on a fresh cache a worktree `.venv` with a planted `.pth` runs it under `--no-config --no-project`. None of them affects the launcher's run. uv still uses its cache under the sealed `HOME` and runs the script with the user's access. Claude's prefix rules keep the limit.
18. Argv forms: the reviewer rule is one comma-joined `--allowedTools` value with an embedded space, while author rules are separate arguments. P0 probes the author form. P0r probes the reviewer form (`dontAsk`, `--add-dir`, `sha256sum` matching) and is required before S4 is scored. **Verified**: P0 (author form) and P0r (reviewer form) both parsed.
19. Auto memory: with `--setting-sources ""` and `--no-session-persistence`, init still reported an auto-memory path and `~/.claude/projects/<repo>/memory/` was created. The worker now sets the documented `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` (docs: no memory files are created or loaded); **docs-only** until P0 shows no memory path in init and no new memory directory. Auto memory is an isolation limit, since the directory is shared by all worktrees of the repository; the account-context gate is never `pass` (`fail` if observed, else `blocked (unobserved)`).
20. Environment scrub: no parent `CLAUDE*` value, nor the parent markers `AI_AGENT`, `GIT_EDITOR`, `COREPACK_ENABLE_AUTO_PIN` and `NoDefaultCurrentDirectoryInExePath` (removed by name), may reach the worker's tools; P0 plants a sentinel to prove it. Claude Code sets its own `CLAUDECODE`, `CLAUDE_CODE_SESSION_ID` and messaging variables for its tools, so P0 compares value digests, never values. The documented authentication and provider variables are kept (`isolation.kept_names`) so environment-based authentication keeps working.
21. Shell variable expansion before rule matching. Attempt 8 (2.1.290) saw an author's `M=<abs memlog.py>; W=<workspace>; uv run --no-project $M append --workspace $W … && …` **allowed twice** under the exact-prefix rules. If the CLI expands variables before matching, that call was the exact prefix and harmless; if it matches the unexpanded form loosely, `$X` holding a script no rule names widens the rules. P0 check: `X=<non-allowed script>; uv run --no-project $X` must be denied (allowed is `fail` for the exact-prefix rules, with a follow-up), and `M=<allowed script>; uv run --no-project $M` is expected denied unless the CLI expands before matching (allowed with `$X` denied shows expansion). **Unknown** until that check runs.
22. Claude runs load built-in plugins and skills (3 plugins and 17 skills in the driver live hook) that the isolation gaps do not record; their influence is unverified. **Open.**

Resolved offline: installed `bmad-review/SKILL.md` step 1 runs `resolve_customization.py` via `uv` and documents a fallback (read `customize.toml`, use defaults). The reviewer therefore needs no shell beyond `sha256sum`.

**Copilot CLI unknowns** (§14):
- **C1.** `--session-id` with a caller-chosen id in a fresh `COPILOT_HOME`; the `workspace.yaml` fields `cwd` and `created_at`.
- **C2.** Auth with a clean `COPILOT_HOME` and only `COPILOT_GITHUB_TOKEN`.
- **C3.** Tool-event shapes: whether `tool.execution_start/complete` and `permission.*` carry tool names, paths, commands and exit codes.
- **C4.** `-p` without `--allow-all-tools` when a tool needs approval: denied, failed or hung.
- **C5.** `--allow-tool 'shell(<multiword prefix>:*)'` and `write` approval reaching an `--add-dir` directory.
- **C6.** Whether `-C`, `--add-dir` and path rules bound writes, and `--disallow-temp-dir`.
- **C7.** Whether `--disable-builtin-mcps` removes the GitHub MCP server, and whether `.github/hooks` or `.mcp.json` in the cwd load; whether `hook.*` and `subagent.*` records appear (recorded as `isolation_gaps`).
- **C8.** Exit codes for denial, auth failure, quota exhaustion and kill.
- **C9.** Premium requests per run and the available models.
- **C10.** Whether the resolved model appears in the events.
- **C11.** A full-size envelope in `-p` up to 100 KB, and the stdin form.
- **C12.** Path-scoped write approval, or a write-free return channel. This is the first follow-up probe and the condition for `write_free_readonly=True`.
- **C13.** Whether `copilot --version` makes a network or update check within 3 s.
- **C14.** Whether `--allow-all-tools` is required in non-interactive mode, as help says.
- **C15.** Whether `--deny-tool 'write(<relative path>/**)'` is honoured: P0c attempts one file-tool write under `_bmad/` and one under `.github/`, expected denied, listed and absent. Until this is verified, Copilot authors with shell rules are `blocked`; if a write is allowed, they stay `blocked`.

Status after live probe 1 (P0c, §14 corrections): C1, C3, C4, C10, C14 and C15 verified (C1 with the lock folder; C3: denials only in `tool.execution_complete`); C5 verified for `write`, failed for a multiword shell prefix (hence the launcher); C6, C7, C8, C9, C11 and C13 partly; C2 and C12 open. Smoke 2 (P0c with the launcher): C5's shell rule matches the bare identifier only (an absolute-path call is denied); C6 outside-path shell arguments are denied by Copilot's path check; C11 verified at 99,900 bytes; C2 and C8 stay open.

## 14. Copilot CLI client (third registry entry)

Source: `.superpowers/sdd/2026-10-05-claude-adapter/copilot-cli-capabilities.md`. Tags: **obs** (one live prompt), **help** (1.0.91 help), **docs**, **schema** (bundled event schema). Everything except **obs** is unverified. Copilot results are Copilot-adapter evidence only.

**Author-only in this slice (K1).** `write_free_readonly=False`. Returning a result needs write approval (`--allow-tool write`), and path-scoped write is unknown (C5, C6). The inventory also misses `cwd/.git/**` and ignored paths, so a read-only Copilot run could write without being caught. `run_isolated` and every reviewer launch therefore refuse `copilot` with `UNSUPPORTED_CLIENT_MODE`; `bmad review prepare` refuses `"client":"copilot"` the same way. A Copilot author's work is reviewed by a Claude or Codex reviewer; cross-client independence is allowed by R2, and each run is scored as its own client's evidence. Lifting this needs the first follow-up probe: path-scoped write approval, or a write-free return channel. Issue #2 proposes the second, an inline pack with no tools and the verdict in the final assistant message: [Copilot as reviewer and judge](copilot-reviewer-design.md) (proposed 2026-10-22, not implemented).

| Contract item | `copilot` entry | Class |
| --- | --- | --- |
| Binary and version | `shutil.which("copilot")`, an npm shim; `copilot --version` gives `GitHub Copilot CLI 1.0.91.` plus an update hint; the first line is recorded | obs |
| Envelope | `envelope_via="argv"`: `-p ENVELOPE_ARG`; `max_envelope_bytes=100_000`; recorded as `@envelope.json`. Same-uid processes can read it in `/proc/<pid>/cmdline`; it holds no secrets | obs (argv form); size C11 |
| Output | `--output-format json` gives JSONL ending in a `result` record with `sessionId`, `exitCode` and `usage` | obs |
| Non-interactive | `--no-ask-user`, `--no-auto-update`, `--log-dir RUN/copilot-logs`. `FORBIDDEN["copilot"]`: `--allow-all-tools`, `--allow-all-paths`, `--yolo`, `--resume`, `--continue`, `--enable-memory`, `--additional-mcp-config`, `--agent`. Help says `--allow-all-tools` is "required for non-interactive mode" (help); P0c decides, and if it proves required, the transport is `blocked`, never adopted | obs / help |
| cwd | process cwd and `-C` set to the worktree | help (`-C` unexercised) |
| Env | `env={"COPILOT_AUTO_UPDATE": "false"}`; `env_unset={"COPILOT*", "COPILOT_ALLOW_ALL"}` with `env_keep={"COPILOT_GITHUB_TOKEN"}`; `session_scrub` removes the parent-session markers (`CLAUDE*`, `ANTHROPIC*`, `AI_AGENT`, `GIT_EDITOR`, `COREPACK_ENABLE_AUTO_PIN`, `NoDefaultCurrentDirectoryInExePath`); `run_env` sets `COPILOT_HOME=RUN/copilot-home`; `run_subdirs=("copilot-home", "copilot-logs", "worker-return")`. Token precedence is `COPILOT_GITHUB_TOKEN` > `GH_TOKEN` > `GITHUB_TOKEN` (docs); only names are recorded | help, docs |
| Isolation flags | `--no-custom-instructions`, `--disable-builtin-mcps`, `--disallow-temp-dir`, all honoured in P0c. They do not stop the worktree's `.agents/skills` from loading as project skills (P0c system prompt: `location: project`), so `launch.json` `isolation.gaps` records `project-skills:.agents/skills` when that tree exists | obs (P0c) |
| `--add-dir` trust | `--add-dir` makes `.github/skills` and `.github/agents` under that directory trusted (help). It is used only for the coordinator-created `RUN/worker-return`, which holds no `.github` and may hold only `return.json` after exit | help |
| Model | `--model NAME`: a required explicit run parameter with no coordinator default, recorded in the argv and `launch.json` `model`. It must be a model the account can use: P0c's `gpt-6-sol` was unavailable (exit 1, stderr, no `result`, a consumed attempt in a coordinator run), and `claude-sonnet-5` is the default observed on this account only. `max_turns_default=None`; no `--max-ai-credits` or autopilot flag relied on | obs (P0c) |
| Author policy | `--available-tools` pinned to `internal_tools`, the narrowed author tool set (one comma-joined value); `--allow-tool write`; for a bound author `--allow-tool 'shell(bmad-script)'`, the bare name of the coordinator's script launcher (never `uv`), and `shell(sha256sum)`; `RUN/worker-tools` first on the worker's `PATH` | write: obs; a multiword `shell(<prefix>:*)` never matched (P0c); the pin, the shell tool inheriting `PATH`, and which launcher call form the rule approves: help, verify next probe |
| Author deny rules | `--deny-tool 'write(<path>)'` for Claude's protected paths (`_bmad/**`, `.agents/**`, `.claude/**`, `.bmad-coordinator/**`, `AGENTS.md`, `CLAUDE.md`) plus `.github/**` (Copilot instructions, agents, skills, hooks), cwd-relative, for every author; recorded as `isolation.deny_rules`. Help: denials beat every allow rule, a relative `write(path)` matches by trailing path components, and shell invocations are not write tools. `**` subtree matching is undocumented. Advisory: the contract digests rechecked at return are the fail-closed backstop (a rewritten allowed script makes the attempt `unknown`) | obs (P0c: `write(_bmad/**)` and `write(.github/**)` each denied a `create`, C15) |
| Confinement class | `approval-policy-advisory`: shell runs with the user's access; inventory delta and D16 audit | help |

**Deny rules and P0c.** `--allow-tool write` plus the `shell(...)` prefixes would let an author rewrite an allowed script and run it; the deny rules above mirror Claude's `AUTHOR_DENIED`. P0c therefore also attempts one write under `_bmad/` and one under `.github/`, both expected denied, listed and absent. If P0c shows the deny syntax is not honoured, the Copilot transport stays `blocked` for authors that receive shell rules (every bound planning author); a generic author without shell rules still has the digest backstop only. Live probe 1: both writes were denied by rule and absent (C15 verified).

**Return.** `return_source="worker-file"`. The envelope names `return_path = RUN/worker-return/return.json`. `read_worker_return` (§3) takes it into `last.json`, and `_final` and the profile check validate it as for other clients. The final `assistant.message` text is never parsed as a return. The return is model-written and coordinator-validated, a weaker class than Codex or Claude schema enforcement. Copilot has no schema flag (smoke 2: S1 omitted `checks` and `descendants` and ended `unknown`), so every worker-file envelope, and a bound Copilot brief, lists every required key with its type (enums included), the profile extras and a JSON skeleton. The skeleton gives the id fields as the envelope's own `assignment_id` and `request_fingerprint`, or as their shapes (`<uuid4>`, `<sha256 hex>`) in a brief. A skeleton pasted as it is (placeholders left in) is refused by `_final`. Copilot attempt 4's correction update (S3″) dropped the inner `workflow_json` keys `prd` and `memlog` and ended `unknown`. Since then a worker-file planning brief and its envelope also print the `workflow_json` key set: the intent, the status, the four lists, the skill's artifact key (`prd` or `spine`) and `memlog`, with the exact absolute paths. They are rendered by `bmad_planning.workflow_skeleton_text` from the constants and path derivation `check` uses (`WORKFLOW_LISTS`, `WORKFLOW_STATUSES`, `MEMLOG_KEY`, `MEMLOG_FILE`, `SKILLS`, `workflow_files`; `check` keeps its explicit status branches); the envelope gets it through the profile's `hint`. The brief also says not to call `python3`, `node` or `uv` directly or redirect to `/tmp` (the workflow_json skeleton round). The listing is rendered by `worker.return_schema_text` from the same schema object the run writes to `return.schema.json` and `_final` and the profile check enforce, so the two cannot drift (return-schema round, ruling 1). Codex and Claude envelopes are unchanged.

**Identity and freshness.** The coordinator generates a uuid4 `--session-id`. A duplicate id silently resumes (help), so freshness rests on the per-run empty `COPILOT_HOME`. The launch facts record `pre_spawn_at`, taken before `Popen`, and `home_session_preexisted` (whether `$HOME/.copilot/session-state/<id>` existed before spawn); `observe` records only after-run facts.

`observe` runs once, after the group drain, and seals `freshness.json` with:
- the `session-state/` listing of `COPILOT_HOME` and, when it lists `.session-operation-locks`, that folder's listing as `locks`;
- the `workspace.yaml` digest, with its parsed `cwd` and `created_at`;
- whether `$HOME/.copilot/session-state/<id>` exists after the run.

`terminal` then requires, from `freshness.json` and the stream only:
- `result.sessionId` equal to the preset;
- exactly one listed session, named by the id; beside it only Copilot's `.session-operation-locks/` folder, holding only `<id>.lock` (P0c: created on every launch, ruling R1); any other entry is `unknown`;
- the worktree as `cwd`;
- `created_at` not earlier than `pre_spawn_at` truncated to milliseconds;
- no `$HOME` session for the id, before or after.

Otherwise the run is `unknown`. Replay never reads `COPILOT_HOME` or `$HOME`. The thread id is `copilot:<sessionId>`.

**Terminal rule.** Every line is a JSON object with a string `type`. Exactly one `result`, as the last line, has `exitCode: 0` and the preset `sessionId`, and the process exits 0. No `session.error` appears (schema).

Tool events (`tool.execution_*`, `permission.*`, schema; C3):
- **Present:** every tool name must be in `allowed_tools("workspace-write")`, which equals `internal_tools`, the narrowed author vocabulary below, also pinned with `--available-tools`. The return write must show in a start record. Either a `create`/`edit` start names `return_path` (basis `tool`), or a `bash` start's command writes that exact path (basis `shell`). Write-shaped means the path is the target of `>`, `>>`, `>|`, `&>` or `tee [-a]`, optionally quoted, and ends on a shell-word terminator. Smoke 3 saw Copilot approve `cat > <return_path> << 'EOF'` into the added directory with no rule and no permission record. A start whose own `tool.execution_complete` was denied never counts (of the five S3 commands naming the path, four were denied), and neither does a start without a `toolCallId`, which cannot be matched to a denial (fail closed).

  The basis is sealed in the facts as `return_write_basis`. These fail closed: a `view`; a read (`cat P`, `ls P`); `rm`, `cp` or `mv`; a `$VAR` path; a bare mention (without a write shape) in a heredoc body or a comment; and a longer or suffixed path. A write-shaped text inside a comment or heredoc body still counts, so the basis labels provenance, not intent. Otherwise the run is `unknown`. The freshness check names a malformed `freshness.json` as such, never as a stream defect.

  Copilot receipts sealed before the return-write round replay `unknown`, because their client facts differ: disabled MCP servers are no longer gaps, and `return_write_basis` is new. Re-run such runs; do not migrate them (return-write fix 1, F6). Every terminal refusal names its specific cause in the run's evidence needs, for example a missing `result` record, a session mismatch, a missing or invalid return file, no return write, a tool outside the pin or a lock-folder anomaly. The envelope's `RETURN_FILE` and the bound brief prefer the file-creation tool and call a shell redirect accepted but discouraged; that is a preference, not a gate (return-write round, rulings 1 and 2).

**Author tool vocabulary (ruling R2a).** P0c offered the model 21 tools (the `session.usage_checkpoint` record; the stream has no init record): `bash`, `read_bash`, `stop_bash`, `list_bash`, `view`, `create`, `edit`, `web_fetch`, `fetch_copilot_cli_documentation`, `search_code_subagent`, `skill`, `run_dynamic_workflow`, `dynamic_workflows_manage`, `sql`, `session_store_sql`, `read_agent`, `list_agents`, `write_agent`, `grep`, `glob`, `task`. `COPILOT_TOOLS` keeps 11: `bash`, `read_bash`, `stop_bash`, `list_bash`, `view`, `create`, `edit`, `grep`, `glob`, `skill`, `sql`. It drops every tool that reaches the network or spawns or drives agents or workflows: `web_fetch`, `fetch_copilot_cli_documentation` (its source is unverified, so it is treated as network), `task`, `search_code_subagent`, `read_agent`, `list_agents`, `write_agent`, `run_dynamic_workflow` and `dynamic_workflows_manage`. It also drops `session_store_sql` ("Querying past sessions"): past sessions are an account-context leak route, the same class as Claude's auto memory, even though a fresh per-run home should hold none. The pin is help-only. A reported tool outside the set is `unknown`, and so is a run whose `session.usage_checkpoint` says the model was offered a tool outside it (fix round 2, M3; recorded as `offered_tools`), so a pin that did not hold fails closed.
- **Absent:** the tool check is recorded `blocked`, never passed. Run validity then rests on return-file validation, the worktree delta and D16.

`session.mcp_*` connections go into `isolation_gaps`. A server that `session.mcp_servers_loaded` reports `disabled` is not one: smokes 1–3 listed both built-in servers as disabled, and recording them over-reported (return-write round, ruling 4). A `tool.execution_complete` whose `error.code` is `denied` becomes a denial `{toolCallId, toolName, error}`, the name taken from the start record with that `toolCallId` (P0c: the only denial record in stdout; `permission.*` and `session.start` appear only in the per-run home's persisted log). A denied `permission.completed`, when present, still counts. `client_facts` gains `usage` (premium requests, durations), `copilotVersion` from `session.start` (absent from stdout in P0c, so `null`) and `tool_check`.

**Timeout, D16, doctor.**
- Timeout: coordinator TERM/KILL of the process group. `COPILOT_TASK_WAIT_TIMEOUT_SECONDS` only bounds background waits and is recorded.
- D16 applies, with `private_subdirs=("copilot-home", "copilot-logs")`. `worker-return` is not private: once the run has ended, other runs' baselines cover it.
- Copilot has background tasks and no flag to disable them, so the §3 step 4 group drain matters here. P0c records `group_alive_after_exit`.
- `doctor --client copilot` reports the binary, the version, and the presence of `COPILOT_GITHUB_TOKEN` (name only, a warning when absent).

**Failure behavior.** All §9 rows apply, plus:
- `UNSUPPORTED_CLIENT_MODE` for any reviewer or isolated request;
- missing, symlinked, multiply linked, oversized, too deeply nested or invalid return file, invalid generic fields, or extra entries in `worker-return/`: `unknown`;
- a pre-existing, second or leaked session directory, a `cwd` mismatch, or an early `created_at`: `unknown`;
- `session.error` or a non-zero `result.exitCode`: `unknown`;
- a tool outside the allowlist, or no write event naming `return_path` when tool events exist: `unknown`;
- a hang on an approval prompt (C4): timeout, `unknown`.

**Evidence.** An observed Copilot return proves process, stream and session facts as in §8. It also proves a coordinator-validated, model-written return file, the worktree delta and D16. It does not prove write scoping, the tool vocabulary when no events appear, OS confinement, or anything about Claude or Codex.

**Budget.** Premium requests per run are unknown (C9); the one observed trivial prompt counted one. The first Copilot smoke allows 5 throwaway probes and 7 coordinator runs in total, including the Claude or Codex reviewer runs. Spare slots are only for P1c and P2c (plan Task 4) and are never retries. The report records `usage.premiumRequests` per run.

**Fake.** The fake `copilot`:
- asserts the argv above, the absence of `FORBIDDEN["copilot"]`, and an empty `COPILOT_HOME` without `COPILOT_ALLOW_ALL`;
- prints `GitHub Copilot CLI 9.9.9.` for `--version`;
- reads the envelope from `-p`;
- writes the return file per `FAKE_RETURN`: `ok|missing|symlink|hardlink|oversize|deep-nesting|invalid|generic-invalid|extra-file`. `generic-invalid` breaks a generic field; profile extras are checked only as far as the profile check reads them;
- emits `session.start`, optional tool and permission events (`FAKE_TOOL_EVENTS`), a decoy JSON `assistant.message`, and `result`;
- creates `session-state/<id>/workspace.yaml`;
- supports the modes `truncated|no-result|wrong-session|exit-code-1|session-error|sleep|preexisting-session|cwd-mismatch|early-created|home-leak`, plus the shared knobs. Tests set `HOME` to a temp directory.

The conformance mixin maps shared mode names per client; for example, `exit-1` maps to `exit-code-1` for Copilot.

**Implementation notes (plan Task 4 code; fakes only, no live Copilot evidence).**
- Env scrub: `env_unset={"COPILOT*", "COPILOT_ALLOW_ALL"}` removes a parent Copilot session's `COPILOT*` variables (allow-all, instruction dirs, model, telemetry, task-wait timeout) except the documented `COPILOT_GITHUB_TOKEN` (the per-client `env_keep`); `GH_TOKEN` and `GITHUB_TOKEN` pass. Ruling (ii): `session_scrub=PARENT_MARKERS` also removes a parent agent session's `CLAUDE*` and `ANTHROPIC*` variables and markers for Copilot and for Codex (Claude keeps its own scrub and `ANTHROPIC*`). `isolation` records `inherited_names` (`COPILOT*`, `GH_*`, `GITHUB*` passed on), `removed_names`, `kept_names` and `token_names` (present names in precedence order), never values; for every client with `session_scrub` the runner also seals `env-scrub.json` (patterns, removed and kept names) after `launch.json`, so a Codex `launch.json` keeps its golden key set. `env-scrub.json` is an unanchored record, not evidence: no receipt or event carries its digest and it is not in `RUNNING_SEALED`, so its own worker can rewrite it (a later run's D16 baseline covers it only once its run has ended). The anchored copy of the names is Copilot's `launch.json` `isolation`; Codex has none.
- Ruling (i): a worker-file envelope's `instructions` end with `worker.RETURN_FILE`, naming the exact `return_path`; Codex and Claude envelopes are unchanged.
- `launch_facts` is taken before the spawn by both runners, so `pre_spawn_at` and `home_session_preexisted` precede `Popen`; Codex and Claude facts are unchanged by the move. `launch_facts` also records `return_path`.
- `preflight` records the first line of `--version` for every client (single-line outputs are unchanged).
- Event fields read (the bundled 1.0.91 `session-events.schema.json`, read from disk; unverified live): `data.sessionId`, `data.copilotVersion` (first `session.start`); `data.toolName` (required) and `data.arguments` of `tool.execution_start` only (`tool.execution_complete` carries no name); the return write is a `tool.execution_start` of a `COPILOT_WRITE_TOOLS` tool (the bundle's edit category: `create`, `edit`, `str_replace_editor`, `apply_patch`) whose `arguments` has a string value equal to `return_path`, so a `view` of the file does not count, or (basis `shell`) a `bash` start whose `arguments.command` writes `return_path` through `>`, `>>`, `>|`, `&>` or `tee [-a]` (`clients._writes_path`; smoke 3); neither counts when the start's own completion was denied or it has no `toolCallId`; `tool.execution_complete` `data.error` with `code` `denied` (observed in P0c; recorded with `data.toolCallId`, the matching start's `toolName` and the error) and, as a fallback, `permission.completed` `data.result.kind` (a kind starting with `denied` goes into `denials`; a missing kind is `unknown`); `session.mcp_*` (`data.serverName`, or each `data.servers[].name`), `hook.*` (`data.hookType`) and `subagent.*` (`data.agentName`) become `isolation_gaps`, else the canonical `data`. Any top-level or `data` `sessionId` other than the preset is `unknown`; `tools` lists the sorted distinct names.
- `observe` lists `copilot-home/session-state` (and `.session-operation-locks/` when listed) and reads `<id>/workspace.yaml` (at most 64 KB, no symlink at any step); `cwd` and `created_at` are single top-level `key: value` lines, optionally quoted. Timestamps must carry a zone.
- `doctor --client copilot` adds `token_names` and `warnings` (a warning when `COPILOT_GITHUB_TOKEN` is absent; readiness is unchanged); other clients' doctor output is unchanged.
- The fake also takes `FAKE_TOOL_EVENTS` as raw `[type, data]` records (`@return` becomes the return path), maps the shared `FAKE_TOOLS` pairs to `tool.execution_*` records, and adds `FAKE_RETURN=github-dir` (a `.github/agents` file beside the return), `FAKE_LOCKS` (`own`: the live lock folder; `extra`, `foreign`, `empty`, `file`, `other-dot`: refused variants) and `FAKE_LAUNCH` (argv lists run as given: `@launcher` becomes the absolute launcher path, a bare `bmad-script` resolves through `PATH`). It asserts the pinned `--available-tools` and, for a bound author, the launcher rule, a `0500` launcher before the spawn and its directory first on `PATH`.

**Script launcher (live probe 1, ruling R3).** Copilot 1.0.91 approves a shell command by its bare command name (P0c: `uv run --no-project <abs>…` parsed as identifier `uv`; the exact multiword `shell(<prefix>:*)` rules never matched and every bound script command was denied), so no prefix rule can confine `uv`. For a bound author (binding `commands`; the binding keeps the `uv` forms, no migration) the runner therefore writes `RUN/worker-tools/bmad-script` before the spawn: a stdlib Python launcher (imports only `os` and `sys`), directory `0700`, file `0500`, created exclusively without following links. It takes nothing from the worker's environment except what the dynamic loader reads before Python starts (`LD_*` on an env-prefixed call; a next-probe item). Its shebang is the coordinator's own interpreter run isolated and without `site` (`#!<sys.executable> -IS`, fix round 2 and the security round): no `PYTHON*` variable, site or user-site `.pth`, `sitecustomize` or script directory is read. It then `os.execve`s the absolute `uv` the coordinator resolved on its own `PATH` at launch, in the worktree, as `uv run --no-config --no-project --python <that interpreter>`:

- `--no-config` (fix round 3, I3) means no `uv.toml` or `pyproject.toml` the author may have written is read;
- `--python` (security round, I1) means uv uses the coordinator's own interpreter and never discovers a `.venv` in the worktree or a parent. An interpreter that itself lies under the feature worktree or its common directory (the coordinator run from a worktree venv, whose `.pth` files and `pyvenv.cfg` the author can write) is refused with `BLOCKED_EVIDENCE` before reservation (return-schema round, m1).

The child environment holds only the `PATH` and `HOME` sealed at launch and `PYTHONDONTWRITEBYTECODE=1`. No `XDG_*`, `LD_*`, `PYTHON*`, `UV_*`, proxy, SSL or other inherited variable reaches `uv` or the script; a script needing network dependencies behind a proxy would fail, but the BMAD scripts are stdlib. No GitHub token reaches them either (security round): they need none, and `launch.json` `isolation.token_names` keeps the names. An interpreter path unfit for a shebang (whitespace, control characters, more than 120 UTF-8 bytes, not UTF-8 at all), or no `uv`, is refused with `BLOCKED_EVIDENCE` before reservation. Its bytes derive only from the bound commands, interpreter, `uv`, sealed `PATH` and `HOME` and worktree, filled in one pass (`clients.launcher_bytes`), and `launch.json` `script_launcher` records `{path, rule, path_prepend, python, uv, sha256}`. `run_env` puts `RUN/worker-tools` first on the worker's `PATH` (ruling R3a; only the prepended directory is recorded), so the launcher answers to its bare name. Rules:
- a form is a bound command without `uv run --no-project `, split on single spaces; a command without that prefix, with an empty token, or whose script is not an absolute `.py` path is refused with `INVALID_INPUT` before reservation;
- the launcher runs only when its arguments start with one form, token for token; any further arguments must follow the full form, and none may repeat or abbreviate an option of that form (`--workspace`, `--work`, `--workspace=…`: argparse keeps the last value and accepts unique prefixes; fix round 2, M1) or be a short option (`-p`, `-s`, `-pfoo`: the scripts' short aliases; fix round 3). A value such as `- a bullet` or `-1` passes. It then execs `uv run --no-config --no-project --python <interpreter> <arguments>` with no shell, so trailing arguments reach only the script, and in the worktree, so a relative bound workspace never follows the shell's `cd`. Claude's prefix rules keep both gaps (a trailing option can rebind a bound one there);
- anything else (no or partial form, a re-ordered, re-spelled, joined, relative or `..` token, an extra argument inside the form, a leading `uv` or `--`, another script or command) exits 2 with `bmad-script: refused, nothing was run` and the allowed forms on stderr; a missing `uv` exits 127.
The author gets `--allow-tool 'shell(bmad-script)'` and `shell(sha256sum)`, never `uv`. The brief and the envelope print each form by the bare name only, `` `bmad-script <form> …` ``. Smoke 2 showed Copilot matches an absolute-path call as the full path and always denies it, so the absolute-path fallback and the envelope's `script_launcher` field were dropped (return-schema round, ruling 2). After the drain and before the terminal (fix round 2, I2), the runner reads `RUN/worker-tools` without following a link at any step and seals the result as `launcher-check.json` (its digest is `launcher_check_sha256` in the return receipt, and replay rechecks it). The folder must hold exactly `bmad-script`, a single-link regular `0500` file with the sealed digest; anything else, such as a rewritten launcher or an extra file beside it, ends the attempt `unknown` with the fence held. The launcher confines only what it runs: a launcher run of a rewritten script still ends `unknown` through the contract digests, and D16 covers an ended run's launcher in later baselines. Some read-only commands may run without any rule (P0c auto-approved `git status`); the coordinator never relies on that, the shell rules never limit reads, and the brief claims no more than that. Live findings (smoke 2, P0c with the launcher, Copilot 1.0.91; `task-4-smoke-report-2.md`). Every approval is advisory:

- **Approved:**
  - the bare-name call, which ran in the worktree;
  - the same call piped to `sha256sum`;
  - an env-prefixed call (`PYTHONPATH=… bmad-script …`): it ran, and a planted `sitecustomize.py` did not, so `-IS` held;
  - a `cd … && bmad-script …` call: it ran, and the launcher still used the worktree as cwd.

  The prepended `PATH` is visible to the shell, since the bare name resolved.
- **Denied:**
  - the absolute-path call, whose identifier is the full path;
  - `uv run …` and `S=…; uv run $S`;
  - a redirect to an outside file, and `| touch` or `; touch` to an outside file;
  - `bmad-script <outside script>`, caught by Copilot's path check even though the identifier matched.
- **Approved but refused by the launcher:** `S=<outside script>; bmad-script $S`. The variable hides the path from Copilot, and the launcher refused it (exit 2, nothing ran), so the launcher is the real gate.

**Limits.** An env-prefixed `LD_*` variable still reaches the launcher's interpreter before Python starts; there is no OS confinement. In-worktree shell writes and redirects are untested. C2 and C8 stay open. Claude keeps its prefix rules (a follow-up may unify them).

**Live probe 1 corrections (P0c, `task-4-smoke-report-1.md`).**
- R1: freshness accepts `.session-operation-locks/` holding only `<id>.lock` beside the one session folder; the listing is sealed in `freshness.json` as `locks`.
- R2, R2a: `internal_tools` is the narrowed P0c vocabulary (11 of 21 names, `COPILOT_TOOLS`, listed above), pinned by `--available-tools` (help-only); any other tool stays `unknown`.
- R3, R3a: the script launcher above, called by its bare name through the prepended `PATH` (smoke 2 dropped the absolute-path alternative, which Copilot always denies).
- R4: denials come from `tool.execution_complete` `error.code == "denied"`; the `permission.*` path stays as a fallback. A probe that expects a denied call but records no denial scores that gate `blocked`.
- R5: `--model` is required with no coordinator default and recorded; availability is per account (`claude-sonnet-5` observed on this account only).
- R6: the worktree's `.agents/skills` load as Copilot project skills, so the Copilot brief label no longer says the client does not auto-discover the skill, and launches record the gap. C2 (the auth source of a clean per-run home) stays open.
