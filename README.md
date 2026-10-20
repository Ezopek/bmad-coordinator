# Portable BMAD Coordinator

A command-line coordinator that drives AI coding agents through a project's backlog one story at a time, in Git worktrees it owns, with every step recorded as evidence you can read back later. It is a Python standard-library CLI with a SQLite state store under the repository's `.git/`: no package to install, no service to run, nothing added to your project but one directory and one block in `AGENTS.md`.

It works with the agent CLIs you already have: **Claude Code** (`claude`), **Codex CLI** (`codex`) and **GitHub Copilot CLI** (`copilot`). Each story is specified by one agent, reviewed by another, implemented, verified with your project's own commands, code-reviewed and merged into an integration branch, on your machine, with your accounts, under limits you set.

## What it does

- **Epic drive.** `epic start` takes the remaining stories of one epic from a BMAD-style `epics.md`, in order, and runs each through the story drive on a branch `coordinator/epic-<N>`. At `epic-done` the branch holds every story; you open the pull request.
- **Story drive.** One story goes from its epic entry to a reviewed, committed branch `story/<key>`: a spec author writes the spec from the entry, a reviewer checks it, the spec is accepted (by you, or by the pipeline), a worker implements it in a linked worktree, the coordinator commits every attempt, runs your `verify` commands on that commit, bundles the result for a code review and accepts it when nothing blocks.
- **Questions without you.** When a worker asks a question, a separate model call, the *judgment*, may answer it only by citing a clause you delegated (from the accepted spec or your project clauses). A binding question (persisted data, external contracts, security, anything a later change cannot undo) stops the run and waits for your decision through a file inbox.
- **Evidence.** Every launch, return, commit, verify run, review and decision is a sealed receipt under `.git/coordinator/`; `epic status`, `drive status`, `detail` and `verify` read them back. What a model claims stays marked as a claim.
- **Attended planning** with BMAD's PRD and architecture skills, and the lower-level assignment and review commands, are also available: see the [usage reference](docs/state-core-usage.md).

## What it does not do

- It never pushes, never merges onward and never opens a pull request. You review `coordinator/epic-<N>` and decide.
- It is not a sandbox. Workers run as your OS user with your environment; read [Safety](docs/getting-started.md#safety) before running it on a repository that matters.
- It is not a supervisor. A run is a bounded foreground process; when it stops, you read the stop and run `epic resume`.
- It does not understand your project. Your verify commands, your writable scopes and your clauses are the guard rails.

## Requirements in short

Linux with the repository on a local ext4 filesystem (WSL2 is fine when the repository lives on the Linux side), Python 3.11 or newer with the standard library only, Git 2.40 or newer, and at least one agent CLI installed and logged in. The full list, with what each client can do: [Prerequisites](docs/getting-started.md#prerequisites).

## Start here

1. [Getting started](docs/getting-started.md): prerequisites, installation into a repository, a ten-minute run on the bundled example, then your own repository.
2. [`examples/greetings/`](examples/greetings/): the example project, pipeline and epic input the quickstart uses.
3. [Concepts](docs/concepts.md): the words the commands, stops and receipts use (mandate, attempt, bundle, judgment, stop, observed and claimed), in reading order.
4. [Troubleshooting](docs/troubleshooting.md): the first errors and stops, their causes and the way out.
5. [Usage reference](docs/state-core-usage.md): every command, every stop with its operator action, the pipeline format and the evidence limits.
6. [Model selection guide](docs/model-selection-guide.md): which model for which role, with observed costs.

## Maturity

The coordinator is tested with fake clients (over a thousand tests, no model calls) and has run live on fixtures with real Claude Code, Codex and Copilot workers and on one real TypeScript project, where it drove single stories and a whole epic to `done`. The [changelog](CHANGELOG.md) lists what landed when. Each design document ends with a status line and a §7 of open follow-ups; the plans and live-run reports they cite by name are kept in the project's private history, not in this tree. Nothing here has run overnight or without someone able to read a stop: "unattended" means a bounded run with zero interventions, not continuous operation.

## Repository layout

- `coordinator_core/`: the CLI and the state core. `package/` and `install.py`: the installer and the files it copies into a project as `.bmad-coordinator/`.
- `docs/`: [getting started](docs/getting-started.md), [concepts](docs/concepts.md), [troubleshooting](docs/troubleshooting.md), the [usage reference](docs/state-core-usage.md), the design documents ([behavior contract](docs/behavior-contract.md), [v1 design](docs/v1-design.md), [worker adapter](docs/worker-adapter-design.md), [Claude adapter](docs/claude-adapter-design.md), [BMAD integration](docs/bmad-integration-design.md), [unattended driver](docs/unattended-driver-design.md), [judgment v2](docs/judgment-v2-design.md), [correction round](docs/correction-round-design.md), [implementation drive](docs/implementation-drive-design.md), [epic drive](docs/epic-drive-design.md)).
- `examples/`: the example project and the pipeline and epic input for it.
- `tests/`: the suite, run from this directory with `PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 python3 -W error::Warning -m unittest discover -s tests -t .` (several minutes; it needs Git and an ext4 working directory, never a model).

## Scope and distribution

This directory is developed on branch `codex/bmad-coordinator` of the `bmad-starter` repository and published as a repository of its own from a `git subtree split` of it; the development history (plans, live-run reports, experiments, research and the session handoff) lives beside it and is not published. The installer copies only the runtime allowlist (`package/manifest.json`) into a project: `run.py`, `coordinator_core/`, the license and the installed manifest; tests and examples are never installed. The code is released under the [MIT License](LICENSE); [CONTRIBUTING](CONTRIBUTING.md) says what a change needs.
