# Getting started

This page takes you from nothing to a finished epic on the bundled example, then to your own repository. It assumes you have used Claude Code, Codex CLI or GitHub Copilot CLI by hand at least once and know what a Git worktree is. Everything the coordinator does is a documented command; the [usage reference](state-core-usage.md) has every flag, every stop and every evidence limit, and this page links to it where the detail matters.

## Prerequisites

**Host.** Linux, with the repository and the coordinator's scratch directories on a local **ext4** filesystem. The reason is narrow: the store's correctness rests on SQLite and `flock` locking between processes and on `fsync` durability of the sealed receipts, and both were probed on ext4 and nowhere else, so another filesystem is refused rather than assumed to behave the same. The state store checks the mount type before it writes and refuses anything else with `UNSUPPORTED_FILESYSTEM` (that includes a tmpfs `/tmp`, network mounts and, under WSL2, the Windows drives under `/mnt/`). WSL2 works when the repository lives on the Linux side, which is where this coordinator was developed and run. Other platforms and filesystems are untested and refused, not merely unsupported.

**Python 3.11 or newer**, standard library only. There is nothing to `pip install`. The suite runs on 3.11 in CI and on 3.14 where the coordinator is developed.

**Git 2.40 or newer.** The story drive records `git --version` before its first command and stops `verify-env` below 2.40.

**At least one agent CLI**, installed on `PATH` and logged in as the OS user that runs the coordinator. The coordinator launches the client's non-interactive mode and never a model call of its own; your accounts are billed as if you had typed the prompts.

| Client | Binary the coordinator runs | Roles it can fill | Notes |
| --- | --- | --- | --- |
| Claude Code (`claude-code`) | `claude -p` | worker, spec author, reviewer, judgment | `max_turns` per role is its own turn limit (one turn is one model response, however many tool calls it holds). Live with 2.1.289 to 2.1.303. |
| Codex CLI (`codex-cli`) | `codex exec` | worker, spec author, reviewer, judgment | Runs in its `workspace-write` sandbox; network only with `web: true` on the worker role. Live with 0.160 to 0.163. |
| GitHub Copilot CLI (`copilot`) | `copilot -p` | worker, spec author; judgment and spec or code review in read mode | Needs a `model` the account can use and a logged-in `copilot` or `COPILOT_GITHUB_TOKEN`. As judge or reviewer it runs only inside the [launch boundary](#run-the-workers-in-a-sandbox) with `"mode": "read"`, on a CLI version qualified for that profile (1.0.102 for every profile; the judgment on 1.0.91 too). |

`doctor --client NAME` resolves the binary and runs `--version`, nothing more: it tells you the client is there, not that it is logged in. Run one prompt by hand (`claude -p 'reply with ok'`, `codex exec 'reply with ok'`) before the first drive.

**Optionally, bubblewrap** (`bwrap`, 0.11.1 is the version tested) for the [launch boundary](#run-the-workers-in-a-sandbox), with unprivileged user namespaces allowed. Ubuntu 24.04 restricts them through AppArmor by default; `doctor --boundary bwrap` names that as the reason.

**A scratch directory on ext4 outside the repository** for `--judgment-root`: the judgment runs there, isolated from the worktrees. Any epic whose pipeline allows judgments (`budgets.max_judgments` above zero) needs it.

**Money and time.** A three-story epic on the example costs about USD 1.5 with Claude Sonnet workers and Claude Opus reviewers and takes six to eight minutes; a real project with Opus everywhere ran at USD 17 to 22 per epic of two or three stories, with a single long implementation attempt at USD 4 to 8. Observed figures, not estimates: see [Costs](#costs) and the [model selection guide](model-selection-guide.md).

## Install the coordinator into a repository

Get the source (this repository; `install.py` is in its root) and install it into the repository you want driven. Installation is two explicit steps so that you see exactly what changes before anything is written.

```sh
SRC=/absolute/path/to/bmad-coordinator      # this repository
REPO=/absolute/path/to/your/repository      # the main worktree of the project to drive

python3 "$SRC/install.py" --repo "$REPO" --preview > preview.json
```

The preview is read-only. It lists the files that would be copied into `$REPO/.bmad-coordinator/` (the runtime allowlist of `package/manifest.json`: `run.py`, `coordinator_core/`, the license, the installed manifest), the exact block it would append to `$REPO/AGENTS.md` (created when absent; existing bytes are kept), warnings such as a Git ignore rule that would hide the package, and a `sha256` token over all of it. Read it, then apply with that token:

```sh
TOKEN=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["sha256"])' < preview.json)
python3 "$SRC/install.py" --repo "$REPO" --apply --preview-sha256 "$TOKEN"
git -C "$REPO" add AGENTS.md .bmad-coordinator
git -C "$REPO" commit -m "install coordinator"
```

Commit the package: the coordinator checks that the package it runs is the one committed at the base of every story, and refuses to run from an uncommitted or different one. From now on every command is

```sh
python3 "$REPO/.bmad-coordinator/run.py" --repo "$REPO" COMMAND ...
```

(`--repo` first, always an absolute path). Then create the state and check readiness:

```sh
run() { python3 "$REPO/.bmad-coordinator/run.py" --repo "$REPO" "$@"; }
run init                                              # {"repository_id": "..."}
run doctor --base "$(git -C "$REPO" rev-parse HEAD)" --client claude-code
```

`init` creates `state.sqlite3` and the evidence directories under the repository's common `.git/coordinator/`, shared by every linked worktree; `status` before `init` answers `{"state": "uninitialized"}` and creates nothing. `doctor` is read-only: it checks that the given commit carries the installed package, that the installed bytes match it, and (with `--client`) that the client binary answers `--version`; `ready: true` with an empty `reasons` list is what you want.

**Upgrading** a repository to a newer coordinator is the same two steps with `--upgrade` (`--upgrade --preview`, then `--upgrade --apply --preview-sha256 TOKEN`, then commit). It requires the installed files and the `AGENTS.md` block to be byte-identical to what the installer wrote (a locally edited package file is refused as `Package collision`); there is no force mode, and a damaged package is repaired by removing `.bmad-coordinator/` and the block and installing again.

## Quickstart: the example epic

The example is a one-function Python package with a backlog in the BMAD layout: Epic 1 has four stories, the first already done, the third gated on a deferred-work entry that is already settled. The drive writes a spec for each remaining story, reviews it, implements it, runs the package's unit tests, reviews the code and merges. It makes real model calls (about USD 1.5 with the roles in the example pipeline) and needs **Claude Code** logged in; the end of this section shows how to use Codex or Copilot workers instead. [A recorded run](example-run.md) shows what each of these commands printed when they were run exactly as written here.

```sh
SRC=/absolute/path/to/bmad-coordinator
WORK=/absolute/path/on/ext4/greetings       # a new, empty directory; not under $SRC or any repository
python3 "$SRC/examples/greetings/make_fixture.py" "$WORK"
REPO="$WORK/main"
```

`make_fixture.py` creates the committed repository `$WORK/main`, copies `pipeline.json` beside it and writes `epic-input.json` with `worktrees_dir` set to `$WORK/worktrees`. Install the coordinator into it and commit, exactly as above:

```sh
python3 "$SRC/install.py" --repo "$REPO" --preview > "$WORK/preview.json"
TOKEN=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["sha256"])' < "$WORK/preview.json")
python3 "$SRC/install.py" --repo "$REPO" --apply --preview-sha256 "$TOKEN"
git -C "$REPO" add AGENTS.md .bmad-coordinator
git -C "$REPO" -c user.name=You -c user.email=you@example.invalid commit -qm "install coordinator"

run() { python3 "$REPO/.bmad-coordinator/run.py" --repo "$REPO" "$@"; }
run init
run doctor --base "$(git -C "$REPO" rev-parse HEAD)" --client claude-code
```

Start the epic. The token file must not exist yet: `epic start` creates it (a private `0600` file that proves ownership for every later command of this epic), keep it where it is.

```sh
mkdir -p "$WORK/judgments"
run epic start --token-file "$WORK/owner.json" --pipeline "$WORK/pipeline.json" \
    --max-turns 80 --deadline 7200 --judgment-root "$WORK/judgments" < "$WORK/epic-input.json"
```

The driver runs in the foreground and prints nothing until it ends; a first Ctrl-C stops it cleanly at the next turn boundary, a second one aborts the running attempt (which then needs `attempt resolve`, below). When it ends it prints one JSON summary and exits with the outcome: `0` for `done` (`"reason": "epic-done"`, `counts.stories_done: 3`), `10` for `continue` (the turn or time budget ran out; `epic resume` continues), `11` for `needs-human` (read `reason`, `operator_action` and `epic_action`; see [Stops](#stops)), `12` for `failed`.

While it runs, from another shell:

```sh
run epic status                       # every epic, its state, its current story and last summary
git -C "$REPO" log --oneline --all    # the coordinator's commits as they land
git -C "$REPO" worktree list          # the epic worktree and one worktree per story
```

What you have at `epic-done`:

- The branch `coordinator/epic-1`, checked out in `$WORK/worktrees/epic-1`, holding the three stories merged in order (`--ff-only` when possible, else a merge commit `epic(1): merge story/<key>`).
- One branch `story/<key>` per story in `$WORK/worktrees/story-<key>` with the coordinator's commits: `story(<key>): spec ready-for-dev`, `status in-progress`, `attempt <n> [completed]` (one per worker attempt, never squashed), `status done`.
- The spec of each story at `_bmad-output/implementation-artifacts/spec-<key>.md` on its branch, with the frozen intent section the worker was held to and a `## Coordinator Result` section; `sprint-status.yaml` flipped to `done`; `deferred-work.md` extended when a judgment deferred a reversible choice.
- The briefs the agents received under `_bmad-output/coordinator/briefs/` in each story worktree.
- The evidence under `$REPO/.git/coordinator/`: `runs/<run>/` (the launch record, the client's retained stream, the return), `story/<feature>/` (setup, baseline, commit, verify and status receipts), `bundles/`, `judgments/`, `drives/<lease>/<run>/` (one `turn-*.json` per driver turn and the run's `summary.json`), `epics/<id>/` (the anchored epic states and summaries) and `state.sqlite3`. `run detail review <id>` and `run verify` read it back; the database is the canonical state and is never edited by hand.

Everything the quickstart made is under `$WORK`; `rm -rf "$WORK"` removes it all.

**Other workers.** The example pipeline fills every role with Claude Code. To drive the same epic with Codex or Copilot workers, change the `roles` block of `$WORK/pipeline.json` to one of these (both shapes ran the example live to `epic-done`; model names are whatever your client accepts):

```json
"roles": {"worker": {"client": "codex-cli", "model": "gpt-6-astra", "timeout": 1800},
          "reviewer": {"client": "claude-code", "model": "opus", "timeout": 900},
          "judgment": {"client": "codex-cli", "model": "gpt-6-astra", "timeout": 600}}
```

```json
"roles": {"worker": {"client": "copilot", "model": "claude-sonnet-5", "timeout": 900},
          "reviewer": {"client": "claude-code", "model": "opus", "timeout": 900},
          "judgment": {"client": "codex-cli", "model": "gpt-6-astra", "timeout": 600}}
```

Copilot can review specs and code in its read mode, inside the launch boundary, with the same role shape as below on the `reviewer` role (planning, spec and code reviews qualified on Copilot CLI 1.0.102). It can judge in its read mode, inside the launch boundary: `{"client": "copilot", "model": "claude-sonnet-5", "timeout": 600, "mode": "read", "boundary": "bwrap"}`. It gets only `view`, `grep` and `glob`, reads the question, the mandate and the decisions in full, and answers in its final message.

## Your own repository

### What the project needs

1. **A backlog in a supported layout.** The drives need three things from a backlog, in the coordinator's words: a story entry (a title, user story lines, acceptance lines written as if they were the test, and optionally preconditions naming earlier decisions), a status per story (`backlog` or `done`, which the coordinator moves to `in-progress` and `done`), and a ledger of deferred decisions a precondition can point at. Each layout is an adapter behind one contract ([backlog design](backlog-design.md)); the one shipped is **BMAD's**, the default, so nothing names it unless you set `project.backlog` in the pipeline. BMAD itself does not need to be installed. The BMAD layout is three files, by default under `_bmad-output/`:
   - `planning-artifacts/epics.md` with `## Epic <N>: <title>` sections and `### Story <N>.<M>: <title>` entries. An entry holds the user story lines, an `**Acceptance Criteria:**` block of Given/When/Then lines and, optionally, a `**Preconditions:**` paragraph followed by bullets naming deferred-work entries (`DW-<n>`); the entry ends at the next heading of level one to three. The entry is all the spec author gets from the backlog, so write the acceptance lines as if they were the test.
   - `implementation-artifacts/sprint-status.yaml` with a `development_status:` map whose keys are `epic-<N>` and story keys `<N>-<M>-<slug>` with the values `backlog` or `done` (any other value stops `story-status`). The story key is the name of the spec file, the branch and the worktree.
   - `implementation-artifacts/deferred-work.md` with `### DW-<n>: <title>` entries carrying `status:` and `decision:` lines. A precondition is met when its entry reads `status: done` or carries a `decision:` whose option does not start with `keep-open`.

   The epics path is `epics_path` in the epic input; the artifacts directory is `project.artifacts_dir` in the pipeline. The story path invokes no BMAD skill; a second layout (a Spec Kit `specs/` tree, GitHub Issues) is a bounded addition with a contract test to pass, not a rewrite.

2. **Verify commands that pass in a fresh linked worktree.** The coordinator runs your `project.verify` commands in the story worktree, on the committed attempt, before any review. A dependency directory that is not in Git (`node_modules`, a virtualenv) is not there in a fresh worktree: link it from the main checkout with a `setup` command, `{"name": "deps", "argv": ["ln", "-sfn", "{main_worktree}/node_modules", "node_modules"], "timeout_seconds": 10}`, and add `/node_modules` to the repository's `.git/info/exclude` (a directory pattern such as `node_modules/` in `.gitignore` does not match a symlink, and an untracked link outside the scopes fails the bundle). Run your verify commands by hand in a throwaway `git worktree add` before the first drive; a red baseline stops `verify-env` before any model is launched.

3. **A committed base.** The epic branches from the `HEAD` of the `--repo` worktree (or the `base` of the epic input) and the coordinator package must be committed there. If `base` is a branch name, the epic merges that branch into its integration branch before each story when it moved.

4. **Writable scopes.** Directories or files (normalized relative paths, no globs) the worker may change for every story (`story_defaults.writable_scopes`) or for one (`stories.<key>.writable_scopes`). The spec, the status files, `AGENTS.md`, `CLAUDE.md`, `.bmad-coordinator/`, `_bmad/`, `_bmad-output/`, `.agents/` and `.claude/` are never writable. A worker change outside the scopes, or a `.gitignore` or `.gitattributes` edit anywhere, is refused at return.

5. **Project clauses.** Short rules the judgment may cite when it answers a worker's question without you (`project_clauses`; the accepted spec's frozen section adds its own). Write only what you would be happy to see applied to any story: these are the delegation.

6. **A Git identity** for your own commits; the coordinator's commits use the pipeline's `commit_identity`.

### The pipeline

The pipeline is one JSON file, sealed by content hash when the epic binds it, so changing it means a new epic (or, for a running story, a restart). Start from [`examples/greetings/pipeline.json`](../examples/greetings/pipeline.json) and change:

- `roles`: `worker` (implements and writes specs), `reviewer` (spec and code reviews; must differ from the worker's `author_id`, and may be a different client), `judgment` (answers questions under your clauses). Each has `client`, `model`, `timeout` (seconds per attempt) and optionally `max_turns` (Claude only). The worker role may add `"web": true` to let a Claude worker use `WebFetch`/`WebSearch` and a Codex worker reach the network from its sandbox.
- `budgets`: `max_updates` (continuations of one attempt chain: answered questions, empty attempts, client stops), `max_judgments` (judgment calls per step: the spec step and the implement step each have their own count), `max_corrections` (rounds after a red verify or a blocking review; absent means zero, at most six), optionally `client_failures` (consecutive attempts of one step the coordinator saw end without a return, at a timeout or a non-zero exit; absent means two, at most four), and optionally `max_cost_usd` (a money bound per story on what the clients report, two decimals at most; it needs a Claude Code role, the only client that reports a USD figure; the run stops `cost-budget` before the launch that would exceed it; see [Costs](#costs)).
- `accept_on_high`: `false` stops the run on an open `high` finding for you to decide; `true` accepts with the findings listed.
- `spec_auto_accept`: `false` stops after each reviewed spec so that you read it and accept (it becomes the delegated authority for the implementation); `true` lets the driver accept.
- `project`: `setup` and `verify` commands (`{name, argv, timeout_seconds}`, run without a shell in the story worktree; `{changed_files}` in an `argv` expands to the attempt's changed files, `{main_worktree}` to the main checkout), `env` (variables copied to the commands and the worker beside `PATH` and `HOME`), `commit_identity`, `output_limit_bytes` (captured output per command), `rules` (files the worker reads first, such as your `AGENTS.md`), optional `context` (files the spec author reads first, such as a PRD or an architecture note), optional `artifacts_dir` and `briefs_dir`.
- `steps`: keep them. The order is fixed (spec, its bundle, review, accept and status; then setup, start status, implement, commit, verify, bundle, review, accept, done status); `__FEATURE__` and `__KEY__` are filled per story.

The deadline you pass to `epic start` must be at least the largest role timeout plus 120 seconds.

### The epic input

Start from [`examples/greetings/epic-input.json`](../examples/greetings/epic-input.json): `epics_path`, `epic` (the number), `worktrees_dir` (an absolute directory outside the repository; the epic and story worktrees are created in it), optional `branch` (default `coordinator/epic-<N>`) and `base` (default `HEAD`), optional `budgets` (`{"max_cost_usd": N}`, a money bound for the whole epic; raised later with `epic resume --max-cost-usd N`), `story_defaults` and optional per-story `stories.<key>` overrides, including `budgets` and `roles` (`timeout` and `max_turns` per role for one story). A story that is already `done` in the sprint status is skipped; one that is neither `backlog` nor `done` stops the run.

### Running

```sh
run epic start --token-file epic.json --pipeline pipeline.json \
    --max-turns 80 --deadline 7200 --judgment-root /ext4/scratch/judgments < epic-input.json
```

`--max-turns` bounds each story run in driver turns (one launch, commit, verify, review or decision per turn; 80 is comfortable, the default 20 is not), `--deadline` the whole epic run in seconds. For a long run start it detached and write the summary to a file:

```sh
setsid nohup bash -c 'run epic start ... < epic-input.json > epic-run-1.json 2> epic-run-1.err; echo $? > epic-run-1.exit' &
```

`--human-wait SECONDS` keeps the driver waiting at a stop for an inbox decision instead of exiting, when you are around. `run epic status --epic ID` shows the current story's run, its last stop and the inbox at any time, without a token and without blocking the driver.

### Run the workers in a sandbox

Add `"boundary": "bwrap"` to a pipeline role (`worker`, `reviewer`, `judgment`) and that role's client process and everything it starts run in a bubblewrap sandbox ([design](launch-boundary-design.md)).

- **What it can reach.** The sandbox holds the system read-only, an empty home, the client's own runtime and state, the run's own directory and the project paths of the launch: a worker gets the story worktree read-write, a reviewer gets it read-only plus the sealed copy it reviews, and a judgment gets only its scratch copies.
- **What stays outside.** Git's metadata (hooks, config, refs, the index) is read-only. The coordinator's store, the other runs' receipts, your other repositories and the rest of your home do not exist inside.
- **What the client keeps.** Its own state stays writable for its login, but the entries that a later session reads (settings and their hooks, instructions, plugins, skills, memories) are read-only; one a run creates is moved to the run's quarantine.
- **The environment.** It is an allowlist: the base variables and the client's own; the launch receipt names what was dropped.

Before the first drive, check the boundary without a model call:

```sh
run doctor --base "$(git rev-parse HEAD)" --client claude-code --boundary bwrap
```

A worker that runs your tests itself may need more than the defaults, such as a package cache or a toolchain under your home. Add them to the pipeline's project block, read-only or read-write: `"boundary": {"ro": ["~/.cache/uv"], "rw": ["~/.npm"]}`. A path that reaches the repository's Git directory or the coordinator's store is refused. The coordinator's own setup and verify commands run outside the sandbox, as before.

The boundary does not confine the network (the clients need their APIs), and it does not hide a client's own credentials from that client.

### Stops

A stop is a JSON summary with exit `11`, a closed `reason` and an `operator_action` written for that stop; the epic's `epic_action` adds the skip or restart option when one applies. The ones you will meet first:

| `reason` | What happened | What you do |
| --- | --- | --- |
| `spec-decisions` (no questions) | the reviewed spec awaits your accept (`spec_auto_accept: false`) | read the digest and the clauses in the summary, then `run bmad accept` of the named bundle and review with the epic's token (the summary spells the command), then `run epic resume --token-file epic.json`; or record a `spec-revise` decision to send it back (the summary spells that too) |
| `spec-decisions` (with questions) / `product-decision` | the spec author or the worker asked a binding question no clause covers | `run inbox add decision` with `{"feature_id", "request_id", "choice": {"answer": "..."}, "reason", "source_ref"}` from the summary's subjects on stdin, then `epic resume` |
| `critical-finding` / `open-high-findings` | a review blocked and the correction budget is spent (or `accept_on_high` is false) | read `run detail review <id>`; then `epic resume --restart <key>` (a new attempt chain with the findings carried into the brief, optionally `--roles roles.json` with a bigger `timeout` or `max_turns`) or `epic resume --skip <key>` |
| `update-budget`, `correction-budget`, `worker-blocked` | the story's budget is spent or the worker reported a condition it cannot change | fix what it names, then `--restart` or `--skip` as above |
| `verify-env` | the setup or baseline commands failed in the story worktree, or Git is below 2.40 | fix the environment (usually the dependency link), then `epic resume` |
| `attempt-unknown` | a worker process was killed or stalled and its result is unknown | make sure nothing of it still runs, then `run attempt resolve <id> --confirm-unknown --token-file epic.json` with `{"reason", "source_ref"}` on stdin, then `epic resume` (it stops once more, `attempt-resolved`, with the resolved attempt in view), then `epic resume --restart <key>` or `--skip` |
| `precondition-unmet` | a story's precondition has no settling entry in `deferred-work.md` | record the decision there on the base branch, then `epic resume`, or `--skip` |
| `integration-conflict` | merging a story into the integration branch conflicted | merge by hand in the epic worktree, commit, `epic resume` |

Exit `10` (`continue`) needs only `epic resume`. The full table with every reason and its exact wording is in the usage reference under [Story drive](state-core-usage.md#story-drive) and [Epic drive](state-core-usage.md#epic-drive); [Troubleshooting](troubleshooting.md) has the causes and the way out of the ones that need more than a resume.

### When it is done

`epic-done` releases the coordinator's ownership. Review `coordinator/epic-<N>` (every attempt is a commit, so `git log` tells the whole story), merge it or open the pull request yourself, then remove the worktrees and branches you no longer need (`git worktree remove`, `git branch -D`). The evidence stays under `.git/coordinator/` and `run epic status` keeps answering.

### Costs

`run bill --epic <id>` prints the bill of an epic from the receipts: every launch with its wall clock and what the client reported (Claude Code a USD figure and tokens, Copilot premium requests and tokens, Codex tokens), the sums per story, role and client/model, and beside every sum the number of launches that reported nothing, so a silent client never reads as free. The same numbers end every summary and the spec's `## Coordinator Result`. The coordinator never turns tokens into money itself; USD appears where the client said it, and Claude's figure is its list price, not what a subscription invoices. A money bound stops a run before the launch that would exceed it: `budgets.max_cost_usd` in the pipeline per story, `budgets.max_cost_usd` in the epic input for the whole epic; the stop is `cost-budget`, and [Troubleshooting](troubleshooting.md#during-a-run) has the way out.

Observed before the bill existed, with the clients' own cost reports read by hand; your models and stories will differ.

| Run | Launches | Cost |
| --- | --- | --- |
| The example epic, Claude Sonnet workers, Claude Opus reviewers, one Codex judgment | 17 to 20 Claude launches | USD 1.4 to 1.7 |
| The example epic, Codex workers, Opus reviewers | 6 Codex launches, 6 reviews | USD 1.07 on the Claude side plus the Codex calls |
| The example epic, Copilot workers, Opus reviewers | 6 premium requests, 6 reviews | USD 0.75 on the Claude side plus the premium requests |
| A real TypeScript project, Opus in every role, two or three stories | 9 to 16 launches | USD 17 to 22 per epic; one implementation attempt of 144 client turns USD 8.40 |

## Safety

Read this before the first drive on a repository you care about.

- **Workers run as you.** Every client runs as the coordinator's OS user with the operator's environment. Claude Code confinement is advisory tool policy, not an OS sandbox: a story worker is allowed the shell commands your `setup` and `verify` commands start with (`Bash(python3 *)` for a `python3` verify command), and a test runner executes the code the worker wrote, so a Claude story worker can run arbitrary code as you, inside and outside the worktree, with network access. With `"boundary": "bwrap"` on the role, that reach ends at the sandbox's mounts ([Run the workers in a sandbox](#run-the-workers-in-a-sandbox)); without it, the rest of this paragraph holds. Codex runs in its own `workspace-write` sandbox (writes inside the worktree, no network unless `web: true`). Copilot's approval policy is advisory; its shell runs with your access.
- **The audit is Git, not the sandbox.** The coordinator records the worktree's change set at return, refuses writes outside the scopes and to protected paths, runs your verify commands on the committed attempt, seals every receipt and has a second model review the diff. That catches what reached the worktree; it does not see reads, network traffic or writes elsewhere.
- **Secrets.** Workers inherit the environment because they need their own credentials (Claude's OAuth or API variables, `OPENAI_*`, `GH_TOKEN`). The coordinator removes a parent agent session's `CLAUDE*` variables except the documented authentication ones, and records variable names (never values) in the launch receipt. Run drives from a shell that exports as little as possible; the names that passed are in `runs/<run>/launch.json`. Under the launch boundary the environment is an allowlist instead (the base variables and the client's own; the receipt names what was dropped).
- **Delegation is the risk you choose.** The judgment answers questions by citing your clauses and the accepted spec; a worker can phrase a question to steer it. Keep clauses narrow, keep `spec_auto_accept` off for stories whose spec you want to read, and keep `accept_on_high` off unless you will read the findings afterwards. A reviewer's finding is a model claim; `critical` blocks acceptance, `high` stops the run by default, `medium` and `low` are listed.
- **The coordinator never pushes, merges onward or opens a pull request.** It only ever writes to the branches it created (`coordinator/epic-<N>`, `story/<key>`) and to `.git/coordinator/`, and merges the base branch into its own integration branch.

## Where to read more

- [Concepts](concepts.md): the words used here and in the stops (mandate, attempt, bundle, judgment, lease, observed and claimed), in reading order.
- [Troubleshooting](troubleshooting.md): the first errors and stops with their causes and the way out.
- [A recorded run](example-run.md): the quickstart as it ran, command by command, with the summary, the receipts and the cost.
- [Usage reference](state-core-usage.md): every command and flag, the pipeline formats, the stop tables, the evidence limits, the client-specific rules.
- [Model selection guide](model-selection-guide.md): models per role with observed costs and turn counts.
- Design documents: [implementation drive](implementation-drive-design.md) (the story pipeline, D1 to D26) and [epic drive](epic-drive-design.md) (E1 to E15); the [unattended driver](unattended-driver-design.md) and [judgment v2](judgment-v2-design.md) for the planning path; the [Claude adapter](claude-adapter-design.md) for what the Claude launch allows and records. Each ends with a status line and a §7 of open follow-ups.
- The design documents' result paragraphs (each §6 or "Result" block): what actually happened on real clients, run by run, with costs, stops and the findings they produced. The full live-run reports are kept in the project's private history.
