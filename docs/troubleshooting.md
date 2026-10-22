# Troubleshooting

Symptoms in the order you are likely to meet them, each with its cause and the way out. An error is one JSON object on stderr with a stable `error` code (exit 2 for invalid input, 3 for an ownership conflict, 4 for a refusal on evidence or state, 5 for a corrupt store); a stop is the run's summary on stdout with exit 11, a `reason` and an `operator_action`. The exact wording of every stop is in the usage reference ([Story drive](state-core-usage.md#story-drive), [Epic drive](state-core-usage.md#epic-drive)); the words themselves are explained in [Concepts](concepts.md).

## Before the first run

**`UNSUPPORTED_FILESYSTEM` ("Local mutation requires ext4", "Cannot verify filesystem type", "SQLite process lock probe failed") on `init`, or later on a write.** The repository's `.git/` directory is not on a local ext4 mount: a tmpfs `/tmp`, a Windows drive under `/mnt/` in WSL2, a network share, another filesystem. Move the repository to a local ext4 directory (in WSL2, the Linux side, such as a directory under your home) and run `init` there. The same rule holds for the judgment root (`--judgment-root`; checked before the first judgment, and the stop `judgment-preflight` then says "nothing was reserved": fix the directory and resume) and for an export directory ("Export directory must be on local ext4"). `UNSUPPORTED_PLATFORM` ("Local mutation requires Linux") is the same rule one level up. The rule exists because the store relies on locking between processes (SQLite `BEGIN IMMEDIATE`, `flock`) and on `fsync` durability of the sealed receipts, which were probed on ext4 only; there is no override flag.

**`{"state": "uninitialized"}` from `status`, `drive status` or `epic status`; `UNINITIALIZED` ("Coordinator has not been initialized", exit 4) from any other command.** The repository has no state yet. Run `init` once in it. Each clone has its own state; linked worktrees of one clone share it, so `init` in the main checkout serves every story worktree.

**The installer refuses.** "Package collision: <path>" means a file already exists where the package would be written (a half-done earlier install, or an edited package file on upgrade): remove `.bmad-coordinator/` and the managed block from `AGENTS.md`, then preview and apply again. "Ignored package destination; installation refused", with `warnings` naming "Ignored destination: <path>", means a Git ignore rule hides the package directory; the coordinator checks that the package is committed at the base of every story, so an ignored package can never pass. Remove the rule in `.gitignore` or `.git/info/exclude` that matches `.bmad-coordinator/` and apply again. "Preview is stale; run --preview again" means the repository changed between the preview and the apply.

**`doctor` reports `ready: false`.** Its `reasons` say which check failed: "Base AGENTS.md is missing" or "Base AGENTS block differs" (the install was not committed, or the block was edited: commit it, or reinstall); "Base package differs at <name>" or "Base package entries differ from installed allowlist" (the committed package and the installed bytes are not the same version: commit, or upgrade); "Base commit is not an ancestor of actual HEAD" (the `--base` you passed is not in the history of the checkout). With `--client`, `client.ready: false` names the executable that is not on `PATH` for the user that runs the coordinator, or a `--version` that failed; install the client or fix `PATH`. For Copilot the client object also warns when `COPILOT_GITHUB_TOKEN` is absent, which matters only when no logged-in `copilot` session exists.

**The client is installed but not logged in.** `doctor` cannot tell: it runs `--version` only. The first attempt then ends without a return: the client exits non-zero, the attempt is recorded `unknown` with its fence held ("Worker process exited unsuccessfully" among its evidence needs) and the run stops `attempt-unknown`. Read the client's own output in `.git/coordinator/runs/<run>/` (the retained stream), log in by hand (one prompt, `claude -p 'reply with ok'` or `codex exec 'reply with ok'`, as the same OS user), then free the attempt as under [A killed or stalled attempt](#a-killed-or-stalled-attempt). A Copilot `model` the account cannot use ends the same way.

### The launch boundary

`UNSUPPORTED_CLIENT_MODE` (exit 4) naming `boundary bwrap is not usable`, before any reservation:

- `bwrap is not on PATH`: install bubblewrap.
- `a no-op sandbox failed: setting up uid map: Permission denied` (or similar): unprivileged user namespaces are denied, on Ubuntu 24.04 by AppArmor (`kernel.apparmor_restrict_unprivileged_userns`). Lifting that is your call.
- `<client> --version fails inside it`: the client needs a runtime path the defaults miss. The message has its last output line.
- `<path> reaches the protected <path>`: a state or project mount is, or leads by a symlink to, the repository's Git directory, the coordinator's store or another worktree.
- `no Copilot token`: set `COPILOT_GITHUB_TOKEN` (narrowest: a fine-grained token with Copilot access only), or log in with `gh`.

A drive's judgment refused the same way stops `needs-human` with `judgment-preflight` and leaves the judgment key unused.

A run that ends `unknown` with "Git metadata … changed" or "a persistent entry of the client's state" is listed in its `integrity.json` (`git_metadata_changed`, `client_state_changed`). A client-state entry the run created was moved to `runs/<run>/quarantine/` (`client_state_quarantined`); look at it before you move anything back.

## Environment stops

**`verify-env`.** The setup commands or the baseline (your verify commands on the base commit, before any model is launched) failed in the story worktree, or Git is older than 2.40. The summary's `failed` lists the command names, or "git >= 2.40 required"; the records under `.git/coordinator/story/<feature>/` (`setup-*.json`, `baseline-*.json`) hold each command's exit code and output. The usual causes:

- A dependency directory that is not in Git (`node_modules`, a virtualenv) does not exist in a fresh linked worktree. Link it from the main checkout with a `setup` command, `{"name": "deps", "argv": ["ln", "-sfn", "{main_worktree}/node_modules", "node_modules"], "timeout_seconds": 10}`, and add `/node_modules` to the repository's `.git/info/exclude`: a directory pattern such as `node_modules/` in `.gitignore` does not match a symlink, and an untracked link outside the scopes fails the bundle later.
- The commands run without a shell, with only `PATH`, `HOME`, `LANG`, `LC_ALL` and the variables named in `project.env`. A command that needs another variable, a shell alias, `cd` or a pipe does not work as written: name the variable in `env`, or put the command in a script in the repository and run that.
- The baseline itself is red: the tests fail on the base commit. Fix the project first; the coordinator refuses to attribute a pre-existing failure to a worker.
- Git below 2.40 on the machine: upgrade Git.

Fix it, then `epic resume` (or `drive resume`): setup and baseline run again. The commands are sealed with the pipeline, so changing them is a new pipeline. For a story driven alone, release and `drive start` a pipeline bound to a new mandate revision. Inside an epic the template is sealed at `epic start`, so start a new epic with the new template (another `branch` name, or remove the old branch and worktree first; `base` may name the old integration branch so that the stories already merged are kept).

**`UNSAFE_PATH` on `epic start`: "worktrees_dir must lie outside the repository worktree" or "worktrees_dir must not use symlinks".** The worktrees directory is inside the repository or reached through a symlink; give the absolute path of a directory outside the repository, on ext4.

## Ownership and tokens

**`OWNERSHIP_CONFLICT` (exit 3).** The message says which rule:

- "Coordinator drive lock is held by another process": a driver is running in this repository (a detached `epic start`, or one in another terminal). Wait for it, or stop it with one Ctrl-C (a clean stop at the next turn boundary; a second Ctrl-C aborts the running attempt, which then needs `attempt resolve`). `epic status` and `drive status` work while it runs.
- "Owner is active or recovery is uncertain": a session owns the repository (an epic that has not reached `epic-done`, a story drive that stopped, a `claim` never released). `status` shows the owner's `session_id`. Continue it with its token file (`epic resume`, `drive resume`), or `release --token-file <that file>`. Ownership is never taken over by elapsed time.
- "Token file already exists": `epic start` and `drive start` create their token file and refuse an existing path. Use a new path; the old file belongs to the earlier session.
- "Owner generation or token is stale": the token file is from a session that was released or replaced. Use the current session's file; after a `release`, continue an epic with `epic resume --epic <id> --token-file <new path>`, which claims again.

**`RECOVERY_BLOCKED` ("Assignments still require lifecycle evidence") on `release`.** An attempt is still `unknown`; resolve it first ([below](#a-killed-or-stalled-attempt)).

**`epic resume` refuses with `run-unbound` ("The owner session is not an epic run", "The owner session has no anchored epic state").** Either the token is not an epic's (a story driven alone is continued with `drive resume`), or the run was killed between creating the integration branch and its first state record: release the session, remove the epic worktree and branch by hand (`git worktree remove <worktrees_dir>/epic-<N>`, `git branch -D coordinator/epic-<N>`) and `epic start` again with a new token file. `drive resume` with an epic's token refuses with `epic-session`: use `epic resume`.

**Pre-run refusals (nothing is written):** `bounds-insufficient` (the deadline is below the largest role timeout plus 120 seconds), `judgment-root-required` (`max_judgments` is above zero and no `--judgment-root` was given), `judgment-root-mismatch` (a resume with a different root than the start; omit the flag or pass the same directory).

## During a run

### A killed or stalled attempt

**`attempt-unknown`.** A worker, reviewer or judgment process ended without an observed return: killed (a second Ctrl-C, a reboot, an out-of-memory kill), timed out (the role's `timeout`), crashed, exited non-zero (a client that is not logged in), or returned something the schema refused. The attempt holds the feature's fence, so nothing of that feature launches until you resolve it:

1. Read what happened: `worker inspect <assignment id>` (the `holder` in the summary's subjects) prints the state and `evidence_needs`; `.git/coordinator/runs/<run>/` has the launch record and the client's own output.
2. Make sure nothing of it still runs. `attempt resolve` refuses while the recorded process, or any process whose command line holds the launch token, may still live; wait, or stop it.
3. `attempt resolve <id> --confirm-unknown --token-file <token>` with `{"reason": "...", "source_ref": "..."}` on stdin, while no driver runs. It seals the evidence and records an operator-reported decision; files the process left in the worktree stay there, unclaimed.
4. `epic resume`: the run stops once more, `attempt-resolved`, with the resolved attempt in view; then `epic resume --restart <key>` (a new attempt chain; the next attempt commit takes whatever the killed one left in the scopes) or `epic resume --skip <key>`. For a story driven alone, the usage reference's stranded-attempts paragraph under [Unattended driver](state-core-usage.md#unattended-driver) has the attended path.

An attempt whose process id was never recorded (a kill in the instant between reservation and launch) cannot be resolved and strands that worktree: `--skip` the story and finish it by hand on a fresh worktree.

### Turn limits

Two different limits are both called turns. `--max-turns` of `epic start` and `drive start` bounds the driver's turns per story run (one launch, commit, verify, review or decision each); reaching it ends the run `continue` (exit 10) and `epic resume` carries on. A role's `max_turns` in the pipeline is the Claude client's own limit, one turn per model response however many tool calls it holds (the result's `num_turns` counts tool rounds, a different number). A Claude worker or spec author that hits it ends `client-stopped`: the coordinator commits what it wrote and gives it a continuation, counted in `max_updates`; when that budget is spent the run stops `update-budget`, and the subjects carry `client_stop`. A reviewer or a judgment at its turn limit is `unknown` instead (above). Raise the box for one story with `epic resume --restart <key> --roles roles.json` (`{"worker": {"max_turns": 200, "timeout": 2700}}`), or in the epic input's `stories.<key>.roles` before the start. Codex and Copilot have no turn limit, only the role's `timeout`.

### Money

**`cost-budget`.** Before a worker, reviewer or judgment launch, the story's reported cost (the USD figures its launches' clients gave, over every chain and revision of the story) reached `budgets.max_cost_usd` of the pipeline, or the epic's reached `budgets.max_cost_usd` of the epic input; the subjects say which (`scope`), `used`, `max`, and how many launches reported no figure (`unreported_launches`: those never count, so a Codex or Copilot launch is free to this bound, and a bound on a pipeline without a Claude role is refused at bind). Nothing was launched. A plain resume stops at the same place. The way out for a story's bound: `epic resume --restart <key> --budgets budgets.json` with `{"max_cost_usd": N}` (a new attempt chain under the wider bound; the accepted spec and the earlier launches stay, and the bill keeps counting them), or `epic resume --skip <key>`. For the epic's bound: `epic resume --max-cost-usd N` raises it (the raise is recorded in the epic state's `interventions`) and continues; `--skip <key>` leaves the story, and the next story's first launch stops again under the same bound. `run bill --epic <id>` shows where the money went. The figure is the client's own list-price accounting (Claude Code's `total_cost_usd`), not an invoice.

### Stops that need a restart or a skip

`update-budget`, `correction-budget`, `cost-budget`, `worker-blocked`, and `critical-finding` or `open-high-findings` with the corrections spent are terminal for a plain resume: resuming stops at the same place. Read the cause (`detail review <id>` for the findings and their counts, the red verify record for `correction-budget`, the attempt's return for `worker-blocked`), fix what is yours to fix in the story worktree without committing, then `epic resume --restart <key>`: the mandate is revised, the blocking findings or the failed command names are carried into the next briefs, and the next attempt commit takes your uncommitted changes. `--skip <key>` leaves the story and its branch to you and continues with the next one.

### Epic-level stops

- **`precondition-unmet`.** The story's `**Preconditions:**` bullet names a `DW-<n>` entry that `deferred-work.md` does not settle at the integration head: no entry, no `decision:` line, a `keep-open` decision, or two decision lines that disagree; `detail` in the subjects says which. Record the decision in the ledger on the base branch (it is merged in before the story) or on the integration branch, then `epic resume`; or `--skip`.
- **`story-status`.** A story's sprint key is neither `backlog` nor `done` (an `in-progress` left by an earlier run, a key missing or listed twice). Set it right in `sprint-status.yaml` on the integration branch, then `epic resume`; or `--skip`.
- **`integration-conflict`.** Merging the base branch in, or a done story out, conflicted; the epic worktree is left clean. Run the merge by hand in `<worktrees_dir>/epic-<N>`, commit, `epic resume`; the resume recognises the done merge.
- **`worktree-drift` of the epic worktree.** It is off its branch or has uncommitted changes to tracked files. Restore the branch and clean the tree, then `epic resume`. A story's own `worktree-drift` concerns the story worktree: its `operator_action` names the expected commit and the `git reset --mixed` that keeps the working tree.
- **`failed` with `story-add`** ("Branch story/<key> exists", "Story worktree path exists", "branch story/<key> exists with another base"). A branch or worktree of that story is left over from an earlier run (an aborted epic, the story driven alone). Remove them (`git worktree remove <worktrees_dir>/story-<key>`, `git branch -D story/<key>`), then `epic resume`; or `epic resume --skip <key>`.

### Decisions and the inbox

- An `inbox add decision` is rejected with `written-during-attempt` when it lands while an attempt or a judgment is running (`inbox list` shows it `rejected`). The driver reads the inbox only at turn boundaries; re-send it once `drive status` shows the window closed, or keep the driver waiting at the stop with `--human-wait`.
- `spec-decisions` with `questions: []` is not a question: it is the reviewed spec waiting for your accept (the summary spells the `bmad accept` command and the `spec-revise` alternative). Set `spec_auto_accept: true` in the pipeline to let the driver accept.
- `judgment-preflight`: the judgment root or the judgment client failed before the judgment was reserved (the action says "nothing was reserved": fix it and resume) or after it (answer the request through the inbox, then resume).

### Copilot

`UNSUPPORTED_CLIENT_MODE` or `UNSUPPORTED_STEP` (exit 4) at bind: a `reviewer` or `judgment` role names `copilot` without `"mode": "read"` and `"boundary": "bwrap"`, or a planning review gets a read-mode reviewer. A read-mode reviewer is refused at the start while its review profile is not qualified ("read mode is not qualified for spec-review"): no version is yet, so give the reviewer role to Claude or Codex. A read-mode judgment on a Copilot CLI version not in the qualified list is refused before its key is reserved; `COORDINATOR_QUALIFY_READ=1` runs it as a qualification run, which is never observed. A read-mode judgment ends `unknown` when the model reads a path outside its staged files, skips one of the three files, or wraps its JSON answer in prose. The last also happens when it notices an injection in the question, which then goes to you. A read-mode review (a qualification run) ends `unknown` the same ways, when it does not `view` a reviewed file, the diff or the spec whole ("did not read … in full"), and when a finding lacks a field or repeats a key. A Copilot role needs `model`; a model the account cannot use consumes the attempt as `unknown`.

## Reading the evidence

- `epic status --epic <id>`: the stories' states, the current story's run, its last stop and the inbox; `drive status --feature <id>` for one feature.
- `detail review <id>`: a review's findings with `finding_counts`; `detail <kind> <id>` for other rows.
- `worker inspect <assignment id>`: an attempt's state and what evidence it lacks.
- `verify`: every acceptance with `current`, `stale` or `unknown` and the reason.
- Files: `.git/coordinator/runs/<run>/` (`launch.json` with the argv and the environment variable names, the client's retained stream, `return.json`), `story/<feature>/` (setup, baseline, commit and verify records, the attempt patches), `drives/<lease>/<run>/summary.json` and `turn-*.json`, `epics/<id>/state-*.json`.
- `git -C <story worktree> log --oneline`: every attempt and status commit; `git diff <parent> <sha>` shows what an attempt changed.

If a sealed file reads `unknown`, nothing in the database was lost: the file was changed, deleted or never anchored, and the record says which; `verify` and `recover` expose it rather than guess.
