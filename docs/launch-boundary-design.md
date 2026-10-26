# The launch boundary: every client process in a bubblewrap sandbox

Status: authorized for implementation 2026-10-22 (operator: "Nie no to leć z tym", the go on the order of work of the [Copilot reviewer design](copilot-reviewer-design.md) §9: the boundary first, then a slimmer Copilot reviewer on it; the operator has not read this document itself); revision 2 the same day after an independent review (Codex, read-only, `REQUEST_CHANGES`: one critical, eight important and one minor finding, all checked; §8 maps them). The critical finding is a gap that exists today without any boundary: F24, decided in L0. Implemented 2026-10-22 and 23 (plan 2026-10-22-boundary, project history). Six background security reviews of the implementation commits were answered with fixes: the persistent client state locked and quarantined, uv's cache private, the environment an allowlist, the quarantine out of the sandbox's reach, the Git metadata snapshot widened. The full suite is green with `bwrap` required (1087 tests). Live (project history, 2026-10-22-boundary/live-report), each run with every role under the boundary and every receipt clean:

- the example epic with Claude reached `epic-done`, at the same cost and time as without the boundary;
- the example epic with Codex reached `epic-done`;
- a Copilot worker story merged, and its second story stopped on an F18-family return, not on the boundary;
- a bound Claude planning author ran its `uv` forms inside. Builds on the [client adapters](claude-adapter-design.md) (§3 the registry, §5 the Claude argv, §14 the Copilot client, D16, D17), the [implementation drive](implementation-drive-design.md) (§7 F4, D21) and the [behavior contract](behavior-contract.md). Standard library only; `bwrap` is an external executable, as `git` and the clients are.

## 1. Problem

Every client process runs as the operator, with the operator's whole filesystem in reach. The coordinator's audit is Git: the worktree inventory before and after, the sealed copies, the contract digests. Git sees tracked and untracked, non-ignored files of the worktree, after the fact. It does not see:

- the Git directory itself: hooks, config, and the coordinator's own store, receipts and sealed copies under `<common>/coordinator/`;
- ignored paths;
- anything outside the repository, such as the operator's home, keys and other repositories;
- reads;
- a write restored before the run ends.

The client policies close part of the gap. Claude authors run under an advisory tool policy, and a Claude story worker runs the project's commands and therefore arbitrary code (D21, F4). Codex runs in its own OS sandbox. Copilot runs under an advisory approval policy. The [Copilot reviewer design](copilot-reviewer-design.md) needed four review rounds largely because, without an OS boundary, all containment rested on Copilot's tool permissions and an audit of its stream.

A spike on the development machine (2026-10-22, WSL2, kernel 6.18, `bwrap` 0.11.1, unprivileged) ran all three clients inside one mount policy: system directories read-only, an empty tmpfs home, no project and the network shared.

| Client | Runtime and state mounted | Trivial prompt |
| --- | --- | --- |
| Claude Code 2.1.308 | its version directory read-only, `~/.claude` read-write, `~/.claude.json` as a private writable copy | answered, exit 0 |
| Codex CLI 0.163.0 | the Node prefix read-only, `~/.codex` read-write | answered under its own `read-only` sandbox, exit 0, 7,935 tokens |
| Copilot CLI 1.0.91 | the Node prefix and `~/.cache/copilot` read-only, the per-run `COPILOT_HOME`, a token in the environment | answered, exit 0, one premium request |

In the same policy, a shell could not see `~/repos` or `~/.ssh`, could not write `/usr`, resolved DNS, and saw only its own `cwd` and run directory beside each other.

## 2. Goal and non-goals

**Goal.** One opt-in launch boundary, `bwrap`. When a role or a run names it, the client process and all its descendants see only:

- the system read-only;
- the client's own runtime and state;
- the run's own storage;
- the project paths its launch kind needs (§3 L4).

Everything else is absent from the sandbox, not merely denied: the rest of the home, other repositories, the coordinator's store and receipts. Without the key nothing changes: argv, receipts, digests and goldens stay as they are.

**Non-goals.**

- Network confinement: the clients need their APIs.
- Hiding the client's own credentials from the client: it needs them, so they stay reachable by the process and therefore by its tools.
- A defence against kernel exploits.
- macOS or native Windows: the coordinator is Linux-only already.
- Any change to the evidence rules, the audit or the client policies, which stay as the inner layer.
- Containers or images.

## 3. Decisions

**L0. F24: the coordinator's own Git calls never run a repository's commands.** A repository's `core.fsmonitor` is a command Git runs on `ls-files --others` and `status`. A harmless probe on 2026-10-22 confirmed that the inventory's plain `git ls-files` runs it twice. A worker that can write the repository's config therefore gets the coordinator to run its command after the run, as the operator, outside every client policy and every OS sandbox. A Claude or Copilot author can reach it today: `.git/**` is not among the protected paths, and in a main worktree `.git/` lies inside the writable tree.

Every Git call the coordinator makes in a worktree gains one shared prefix, `-c core.fsmonitor=false -c core.hooksPath=/dev/null`: the inventory, the change listing, the commit helpers and the worktree listings. The prefix holds boundary or not. Other config-driven commands, such as a clean filter that `.gitattributes` names, cannot be turned off by a flag. So every worker run also snapshots, with the D16 baseline, the Git metadata that can run or hide something: `config`, `config.worktree`, `commondir`, `gitdir`, `info/attributes`, `info/exclude`, `info/sparse-checkout` and every hook (a symlinked hooks directory is read through its real path), in the common directory and in the worktree's own Git directory; the worktree's `.git` entry; the operator-level files (`GIT_CONFIG_GLOBAL` or `~/.gitconfig`, and `config`, `attributes` and `ignore` under `$XDG_CONFIG_HOME/git`); and every file Git itself reports reading configuration from (`git config --list --show-origin`, included files too), with the files and directories that `core.attributesFile`, `core.excludesFile` and `core.hooksPath` name. An entry that cannot be read counts as changed. The run compares the snapshot after the drain, additions included, before the coordinator's first Git call. Any change ends the run `unknown` with the fence held (`integrity.json` lists the paths), so a planted filter or hook is never run by the coordinator's later `git diff`, `git add` or `worktree add`. The boundary goes further and makes that metadata read-only (L4).

**L1. Opt-in per role, recorded where the client is.**

- A pipeline role (`worker`, `reviewer`, `judgment`) may say `"boundary": "bwrap"`. The key is part of the pipeline digest when present and absent otherwise, so existing pipelines keep their digests.
- The epic, story and planning prepares carry the role's boundary into the assignment binding beside the client, and into the judgment's reservation request. Each is recorded only when present, so existing bindings keep their bytes.
- `worker run`, `bmad run` and `assignment reserve-launch` take `--boundary bwrap`. A flag that differs from the binding is `INVALID_INPUT`, as `--client` is.
- The only kind is `bwrap`; any other value is `INVALID_INPUT`.

**L2. The common policy.** Every path is mounted at its host path, so the argv, the cwd and every receipt path mean the same inside and outside.

- `/usr` is read-only, with the merged-usr links `/bin`, `/lib`, `/lib64` and `/sbin` as symlinks, or as read-only binds on a system where they are real directories.
- `/etc` is read-only, and so is the resolver file's target when it lies outside `/etc` and `/usr` (WSL2: `/mnt/wsl/resolv.conf`).
- `/proc`, `/dev`, and a fresh tmpfs at `/tmp`.
- A fresh tmpfs at `$HOME`, so the home exists and is empty but for what L3 and L4 mount into it.
- The network is shared. PID, IPC, UTS and cgroup namespaces are unshared (`--unshare-cgroup-try`).
- `--die-with-parent`, and no `--new-session` (L6).
- The environment is an allowlist, not the whole inherited environment. It holds the base variables (`PATH`, `HOME`, the locale and `LC_*`, `TERM`, `TZ`, the proxy and certificate variables, the XDG directories), the client's own names (`CLAUDE*`/`ANTHROPIC*`, `OPENAI*`/`CODEX*`, `COPILOT*`/`GH_*`/`GITHUB*`), its static env, and the prefixes `COORDINATOR_BOUNDARY_PASS` lists. The receipt names what was dropped, so another client's key, a D-Bus address or an agent socket stays outside. `GIT_OPTIONAL_LOCKS=0` is added, so a read-only Git does not try its optional index refresh. A secret is never put in the bwrap argv (`--setenv`): it reaches the sandbox only through the inherited environment.
- **Precedence and validation.** Mounts are applied parents before children, so a bind refines the tmpfs or read-only tree above it and never the other way round. A path mounted twice with two modes is refused. Every source is resolved with `realpath` first. A client-state, runtime or project mount is refused before reservation when its real path equals, contains or lies inside any of these: the common directory, the coordinator directory, the worktree's Git entry, or another worktree of the repository. This catches an innocent-looking `~/cache` that is a symlink to `<common>/coordinator`. Symlinks inside a mounted tree do not escape: their targets exist inside only if they are mounted themselves.

**L3. The client's runtime and state, declared in the registry.** Each client gains `boundary_mounts(binary, spec) -> list[Mount]`.

- **Every executable keeps its spelling.** Each executable path the launch invokes is mounted together with its symlink chain, recreated inside: the client binary as `PATH` resolved it, `node` for a script client, `uv`, and the interpreter sealed into the Copilot launcher's shebang. So is the real file's directory, read-only. An interpreter outside `/usr` also gets `sys.prefix`, `sys.base_prefix` and `sys.exec_prefix` read-only, for its standard library and extension modules. A layout that still fails the preflight's probe (L7) is refused, not guessed.
- **Persistent entries are read-only.** What a client reads at the start of a later session could carry a sandboxed run's command out of the boundary, so those entries are bound read-only inside the read-write state directory: settings and their hooks, a status line command, instructions and every top-level `*.md` they import, executable files, plugins, skills, agents, commands, Claude's `projects` (its auto-memory) and Codex's `config.toml`, rules, prompts and `memories*` stores (both clients answered with these read-only, 2026-10-22). One created during the run, where none existed, ends the run `unknown` (`integrity.json` `client_state_changed`) and is moved into `RUN/quarantine/` (`client_state_quarantined`), so the operator's next session never reads it; the move is reversible. The state directory itself stays writable, because an OAuth refresh rotates the credential, and a discarded refresh (a temporary overlay) would log the operator out.
- **`claude-code`.** `~/.claude` is read-write but for its persistent entries, so a credential refresh and the client's working state land where they do today. `~/.claude.json` becomes a private writable copy, mode 0600 (`bwrap --perms 0600 --file FD`). The client rewrites it with a temporary file and a rename in `$HOME`, which works inside the tmpfs, and the operator's file is never written by a sandboxed run. The descriptor is opened immediately before each spawn (the probe's and the launch's), passed with `pass_fds` and closed on every path. The receipt names the copy's path, never its content. `CLAUDE_CONFIG_DIR`, when set, is mounted read-write instead of both.
- **`codex-cli`.** The Node prefix of the binary's real path, read-only: the nearest ancestor holding `bin/node`, which carries the script and its native package. `CODEX_HOME` (default `~/.codex`) is read-write. Its global instruction files, `AGENTS.md` and `AGENTS.override.md`, are shown empty and read-only (mode `empty`, `bwrap --ro-bind-data` from `/dev/null`, bound over the file so the host's file is never written; F26, 2026-10-23, operator's decision). The live runs of 2026-10-22 showed Codex loading the operator's own `~/.codex/AGENTS.md` and trying to read the files it names, so a worker followed the operator's private rules beside the project's. A symlinked instruction file stays a link to a target the sandbox does not hold.

  **Its SQLite state is the run's own (issue #5, 2026-10-25).** Codex opens its SQLite databases at every start, whatever its features say (codex-rs `StateRuntime::init` opens `memories_1.sqlite` unconditionally; the `memories` feature only governs extraction): `state_5`, `logs_2`, `goals_1`, `queue_1`, `thread_history_1` and `memories_1`, each with a `-wal` and a `-shm` beside it. With `memories_1.sqlite` bound read-only in a writable `CODEX_HOME`, SQLite created the two sidecars beside it, the guard above saw new `memories*` entries, quarantined them and ended a finished review `unknown`; and the shared databases are open in the operator's own Codex sessions at the same time. Every Codex launch now sets `CODEX_SQLITE_HOME` to `RUN/codex-sqlite`, a run-owned directory created empty (a private run subdirectory, out of other runs' baselines), checked on 0.164 to take every database and sidecar: the run reads none of the operator's cross-session memory or thread state, writes none of it, and its databases are discarded with the run. The guard is unchanged: a `memories*` entry created in the shared `CODEX_HOME` still ends the run `unknown` and is quarantined, and the cause now names it. Authentication stays where it is: Codex rewrites `auth.json` in place (`FileAuthStorage::save`, truncate and write), so a refresh inside the read-write `CODEX_HOME` reaches the operator's file.
- **`copilot`.** The Node prefix, read-only, and `~/.cache/copilot` (the runtime package), read-only. `COPILOT_HOME` is the per-run directory it already is. A documented token variable (`COPILOT_GITHUB_TOKEN`, `GH_TOKEN`, `GITHUB_TOKEN`) is passed through when set. Otherwise the coordinator takes `gh auth token --hostname github.com` at launch, for `gh`'s active account, and sets `COPILOT_GITHUB_TOKEN` in the spawn environment. The value appears in no argv, receipt, stderr or diagnostic; the receipt records the variable's name, `token_source: "gh"` and the host. A token that cannot be obtained refuses the launch at the preflight.

  **The token's kind (issue #3, 2026-10-25).** The Copilot CLI docs list the supported kinds by prefix: OAuth (`gho_`), fine-grained PAT (`github_pat_`, with the *Copilot Requests* permission) and GitHub App user tokens (`ghu_`); a classic PAT (`ghp_`) is unsupported. The sandbox holds no stored Copilot login (the keychain and `~/.copilot` stay outside), so a classic token from `gh` ended a real review attempt before inference, exit 1 and no stream. The coordinator now classifies the token Copilot will use, the first documented variable set or else `gh`'s, and refuses a classic one before any reservation, in every Copilot launch (the client preflight checks the variables, the boundary also `gh`'s token). The receipt records `token_kind` (`oauth`, `fine-grained-pat`, `app-user` or `unknown` for any other prefix) beside the source: `{"token_source": "env", "token_name": NAME}` or `{"token_source": "gh", "token_host": ...}`. Observed 2026-10-25 on 1.0.102 (the version the coordinator runs: with `COPILOT_AUTO_UPDATE=false` the CLI starts its installed base, not a newer auto-updated package) outside the sandbox: an unsupported or invalid variable is logged (`Classic PATs are not supported`, `Failed to fetch PAT user login (401)`) and Copilot then authenticates with another stored credential without saying which, so the docs' "an environment variable silently overrides a stored OAuth token" does not hold for a refused one. A run that exits non-zero after Copilot logged an authentication error names it in the cause with a fixed phrase (a classic token refused, GitHub's 401, or another authentication error), never the log's text, which could echo a credential of an unrecognized shape; the log is the client's, so this is a diagnostic, not evidence.

  **The authority is the token's.** The spike authenticated with it, and every process of the run can use it over the shared network. `gh`'s token carries its scopes, `repo` on the development machine. `~/.config/gh` stays absent, but that does not narrow what the token can do. The documented narrowing is to set `COPILOT_GITHUB_TOKEN` to a fine-grained token limited to Copilot. Whether Copilot falls back to the same `gh` credential when it runs unsandboxed is unverified: retained receipts show no documented token variable.
- **A bound planning author** (Claude, Copilot) also gets `uv` with its chain, the coordinator's interpreter with its prefixes and uv's managed Pythons read-only, so its bound script forms run. uv's cache is private to the run (`UV_CACHE_DIR` in the sandbox's own `/tmp`), never the operator's, so a run cannot poison what uv later runs outside.
- A path that does not exist is left out and recorded as absent. A missing runtime path is a preflight failure (L7).

**L4. The project paths per launch kind.**

- **Isolated** (a judgment, and the Copilot `read` launch of the reviewer design): the `cwd` read-only and the receipt directory read-write. Nothing of the project.
- **Reviewer** (`read-only` assignment): the worktree read-only, the whole common directory read-only, `<common>/coordinator` as an empty tmpfs, the bound sealed root read-only and the run directory read-write over it. Git in the worktree works for reading.
- **Author and worker** (`workspace-write`): the worktree read-write, with its Git entry (the linked worktree's `.git` pointer file, or the main worktree's `.git` directory) bound read-only over it. The whole common directory is read-only. `<common>/coordinator` is an empty tmpfs, with the run directory read-write over it. No part of Git's metadata is writable: no hooks, config, `config.worktree`, `HEAD`, `commondir`, index or lock. The store, other runs' receipts and the sealed copies cannot even be seen. Workers never commit, and `git status` and `git diff` work without writing (`GIT_OPTIONAL_LOCKS=0`). A Git command that must write, such as `git add`, `git stash` or `git checkout`, fails inside, which also keeps a worker from discarding work.
- **Project mounts.** A story pipeline's `project.boundary` may add `{"ro": [...], "rw": [...]}`: absolute paths or `~/`-relative ones, normalized, with no glob, outside the worktree and the common directory, part of the digest. They carry what a worker's own test runs need, such as a uv cache or a Node toolchain. The coordinator's setup and verify commands run outside the boundary, as today, so a project needs these only for the worker's own iterations.

**L5. Receipts, anchored, and checked at replay.** `launch.json` keeps `argv` as the client argv, so every terminal parser and replay read it unchanged, and gains `boundary`. The record holds:

- `kind`, the `bwrap` path and version;
- `mounts`, each with its mode (`ro`, `rw`, `copy`, `tmpfs`, `symlink`, `empty`) and path, plus the paths found `absent`;
- `env_names` added by the boundary, and `token_source` and host for Copilot;
- `argv`, the full wrapped argv with the envelope placeholder left unexpanded and the copy descriptors as numbers.

Because the record is inside `launch.json`, the launch digest anchored at return covers it, and there is no separate sidecar to substitute. Replay requires a binding or request that names a boundary to have a `launch.json` boundary record of that kind whose wrapped argv ends with the recorded client argv. A missing, malformed or disagreeing record makes the run `unknown`. A run without a boundary has no record, so Codex and Claude goldens stay byte-identical. An isolated run records the same in its own `launch.json`.

**L6. The process group and the namespace's lifetime.** The coordinator spawns `bwrap` as before, with `start_new_session=True`, so `bwrap` leads the group and `own_group`, `stop_group` and `drain_group` act on it unchanged. Without `--new-session`, descendants inherit that group unless they leave it with `setsid` or `setpgid`. What covers those is the PID namespace with `--die-with-parent`: when `bwrap` or its child, the namespace's first process, dies, the kernel kills everything left in the namespace. `launch-intent.json` records the leader's pid, as today. Tests cover:

- a background descendant;
- a `setsid` descendant;
- `bwrap` killed;
- a timeout;
- a child that exits at once.

**L7. Preflight and doctor, before every reservation.** A boundary launch:

1. resolves `bwrap`;
2. runs a no-op sandbox (`bwrap … /bin/true`);
3. validates the whole mount set of the launch (L2), the project mounts included;
4. obtains the Copilot token when one is needed;
5. runs the client's own `--version` inside that full mount set, and for a bound planning author also `uv --version`.

No model is called. A failure is `UNSUPPORTED_CLIENT_MODE` (exit 4) with the reason, for example user namespaces denied by an AppArmor restriction.

The checks run where the client preflight already runs. That is in `worker run` before `reserve_launch`, and in `judgment.prepare` before it anchors `judgment.reserved` (and again in the judgment's run, which is cheap), so a refusal costs no attempt and no one-shot key. `run_isolated` repeats them. `assignment reserve-launch` only reserves; the boundary is applied and checked by the `worker run` that launches. `doctor --base REF --client C --boundary bwrap` reports the same as a `boundary` object (`binary`, `version`, `ready`, `reasons`).

**L8. What the boundary proves.** In a boundary run, no process of the run can open a path outside the mounted set, because that path does not exist in its mount namespace. That covers reads as well as writes, the Git directory's hooks and config, ignored paths outside the worktree, and the operator's home. The boundary does not hide the client's own state and credentials, the network or the mounted project paths. Inside the worktree, the existing audit (inventory, scopes, contract digests) stays the check. The launch receipt names every mount, so what was reachable is a recorded fact.

**L9. CI.** The workflow installs `bubblewrap`, lifts the runner's AppArmor restriction on unprivileged user namespaces (`sysctl kernel.apparmor_restrict_unprivileged_userns=0`), asserts that a no-op sandbox runs, and sets `COORDINATOR_REQUIRE_BWRAP=1`. With that variable set, a boundary test that cannot run fails instead of skipping, so CI cannot pass with the boundary untested. On a developer machine without a usable `bwrap`, the tests skip and name the reason.

**L10. Documentation.**

- Getting started gains "Run the workers in a sandbox": the role key, `doctor --boundary`, the project mounts.
- Concepts gains a paragraph on what the boundary does and does not hide.
- Troubleshooting gains the preflight failures.
- The usage reference documents the keys, the flags and the receipt fields.
- The Copilot reviewer design is rewritten on top of L4's isolated kind.

## 4. Changes by file (sketch)

- `coordinator_core/boundary.py` (new): `Mount`, the common policy, validation and ordering, `wrap`, the descriptors, the preflight and probe, the receipt record and its replay check.
- `coordinator_core/run_receipt.py`, `bmad_implementation.py`, `clients.py`: the L0 Git prefix on every worktree call.
- `coordinator_core/clients.py`: `boundary_mounts` per client; `run_isolated` wraps when its request names the boundary; the Copilot token from `gh` for a boundary launch.
- `coordinator_core/worker.py`: the wrap at the spawn, the receipt fields, `--boundary`.
- `coordinator_core/drive_pipeline.py`: the role key and `project.boundary`, validated and digest-stable.
- `coordinator_core/bmad_planning.py`, `bmad_review.py`, `bmad_implementation.py`, `bmad_spec_author.py`, `judgment.py`: the boundary carried into bindings and requests.
- `coordinator_core/__main__.py`: the flags and `doctor --boundary`.
- `package/manifest.json`: `boundary.py`.
- `.github/workflows/tests.yml`: L9.
- Docs per L10; `CHANGELOG.md`.

## 5. Tests

Fake clients, under a real `bwrap` where usable:

- **F24.** Every coordinator Git call on a worktree carries the prefix. A repository whose config names an fsmonitor and hooks has neither run by the inventory, the change listing or a commit.
- **Mount sets.** For each launch kind, in a synthetic main worktree and a linked one, the sandbox shows exactly its mounts.
  - A fake client fails to read `~/.ssh`, another repository, `<common>/coordinator/store.db`, a sibling run's receipts or another worktree.
  - It fails to write `.git/config`, `.git/hooks`, `config.worktree`, the linked `.git` pointer or the index.
  - Each failure surfaces as the client's own error.
  - In an isolated run, the cwd's `.git` is read-only.
- **Validation.** A project or state mount whose real path is or contains the common directory, the coordinator directory or a worktree's Git entry is refused, including through a symlinked alias. Two modes for one path are refused. Parents are applied before children.
- **Paths and Git.** The argv and receipt paths are unchanged inside. `git status` and `git diff` work in a reviewer's and a worker's worktree. A worker's edits land in the worktree.
- **Process lifetime.** The five cases of L6.
- **Preflight.** These are refused before reservation and before a judgment key is reserved: a missing `bwrap`, a failing no-op, a failing client `--version`, an invalid mount, a missing Copilot token, and a failing `uv --version` for a bound author.
- **Descriptors.** The copy descriptor is open for the spawn and closed after it on success, on a spawn error and on a timeout.
- **Digests and receipts.** The role key and `project.boundary` are digest-stable when absent. Receipts without a boundary are byte-identical to today's (goldens).
- **Replay.** A boundary run replays from its receipts. A `launch.json` whose boundary record is missing, malformed, of another kind, or whose wrapped argv does not end with the client argv makes the run `unknown`.
- **Copilot token.** The token is taken from `gh` only when no token variable is set. Its value appears in no argv, receipt, stderr or diagnostic. Its absence refuses the launch at the preflight.

## 6. Live verification

On fixtures, within modest caps:

1. The quickstart epic (`examples/greetings`) with every role under the boundary: Claude worker, reviewer and judgment. It passes when it reaches `epic-done` and the receipts name the mounts. Cost: about one dollar, as the recorded run.
2. One Codex worker attempt and one Codex isolated judgment under the boundary, on the same fixture.
3. One Copilot worker attempt under the boundary: one premium request.
4. One adversarial check: a Claude story worker whose project command tries to read `~/.ssh`, to write `.git/hooks/pre-commit` and to set `core.fsmonitor` in `.git/config`. It passes when all three fail and the receipts show the mounts. (Decided at implementation: not run live. A model-driven attempt tests the client's policy rather than the boundary, and a project command would run the same actions outside, as the coordinator's verify step. The sandbox tests under a real `bwrap` exercise these reads and writes, and the live receipts show what was mounted.)
5. One bound Claude planning author under the boundary, whose `uv` script forms must run, as in the issue #1 live check.
6. One Codex worker attempt in which Codex runs a shell command inside its own sandbox inside the boundary, not merely an answer without tools.

## 7. Residual uncertainty

0. **F24 outside the boundary.** Without a boundary, a worker that can write Git's metadata is detected, not prevented. The run ends `unknown` before any coordinator Git call, but the planted files stay until the operator removes them. A Git command the operator runs by hand in that repository would still run them. The snapshot is detection, not prevention, and it is bounded by what it lists. A symlink's entry carries what it finally points to, so a write through a symlinked `~/.gitconfig` shows. Outside it stay, for example, a submodule's own Git directory, object alternates, and any file that Git reads only for commands the coordinator does not run. After four background security reviews of this control (2026-10-22), each answered with a fix, the decision is to stop extending detection there: the boundary, which makes all of this unreachable, is the control, and an operator who runs workers unbounded accepts these residuals.

1. **The client's working state is still writable.** Persistent entries are read-only or detected (L3). A file the clients start reading in a later version, outside those lists, would not be. A full overlay with only the credential file passed through needs the clients' credential write patterns, which the spike did not map.
2. **Credentials stay reachable.** The process needs them, so its tools can reach them through the mounted state or `/proc/self/environ`.
3. **The network is open.** Exfiltration is unchanged; the receipts record that the boundary shares the network. Sharing the network namespace also leaves the host's abstract Unix sockets reachable, for example a D-Bus session bus that listens on one. The allowlist drops the address variables, but a process that knows an address can still connect. Unsharing the network needs a user-space network stack (`pasta`, `slirp4netns`), which is the next tightening.
4. **The client's own integrations still load.** A sandboxed Codex worker reads the operator's `config.toml`, so its MCP servers, plugins and hooks start inside the boundary (issue #5 asked for their explicit treatment). They are kept on the operator's decision (2026-10-25): they run with the worker's mounts and the shared network, and a project that wants them off sets that in the Codex configuration.
5. **Kernels and hosts differ.** Ubuntu 24.04's AppArmor restriction blocks unprivileged namespaces by default; L7 names it, and lifting it is the operator's call.
6. **Workers' own test runs need project mounts,** and a project that declares none may see its worker's local test runs fail where they passed before, while the coordinator's verify still runs outside.

## 8. Review findings and where they went

| Finding (Codex, revision 1) | Decision |
| --- | --- |
| Critical: a writable worktree Git directory lets a worker set `core.fsmonitor`, which the coordinator's unsandboxed inventory then runs | L0 (F24, all runs); L4: all Git metadata read-only, the `.git` pointer too |
| Mount precedence and alias validation underspecified | L2: parents before children, conflicts refused, real-path validation against the common directory, the coordinator directory and Git entries |
| The main worktree's index exception is not implementable | L4: no exception; Git metadata entirely read-only; `GIT_OPTIONAL_LOCKS=0` |
| Executable mounts do not make a complete runtime | L3: every invoked executable with its chain; interpreter prefixes; the probe of L7 runs `uv`; §6 items 5 and 6 |
| The `--file` descriptor needs a lifecycle | L3: opened per spawn, `pass_fds`, closed on every path, mode 0600; §5 |
| Boundary evidence has no replay contract | L5: the record inside the anchored `launch.json`, no sidecar, checked at replay; tamper tests |
| Preflight after the judgment's reservation | L7: in `judgment.prepare` before `judgment.reserved`; complete launch-policy readiness, not only `--version` |
| Hiding `~/.config/gh` does not remove the token's authority | L3: the authority stated, host and source recorded, never in argv or receipts, the narrowing documented |
| CI can pass with the tests skipped; live coverage misses the hard paths | L9: `COORDINATOR_REQUIRE_BWRAP=1` fails instead of skipping; §5 synthetic worktrees; §6 items 5 and 6 |
| Minor: group membership versus namespace lifetime | L6 rewritten; five lifetime tests |

