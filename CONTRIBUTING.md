# Contributing

The coordinator is a small project with one maintainer and a long test suite. Issues and pull requests are welcome on the repository where you found it; what follows is what a change needs in order to be accepted.

## Setup

Linux, a local ext4 filesystem, Python 3.11 or newer and Git 2.40 or newer, and nothing else: the coordinator is standard library only, and that is a rule (below), not an accident. Clone the repository and run the suite from its root:

```sh
PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 python3 -W error::Warning -m unittest discover -s tests -t .
```

It takes about half an hour, needs Git and an ext4 working directory, and never calls a model: every client is a fake executable the tests put on `PATH` (`tests/fake_clients.py`). One module runs in seconds with the same environment, for example `python3 -m unittest tests.test_examples`. Warnings are errors (`-W error::Warning`), so a change that raises one fails the suite.

## Rules

- **Standard library only.** No `pip install`, no vendored code, no optional dependency. The installer copies the coordinator into other people's repositories; a dependency would become theirs.
- **Every behaviour change comes with a test on the fake clients**, and the suite stays green on a clean checkout. A fix for something seen live comes with the fake-client test that reproduces it.
- **The design documents are the specification.** A behaviour change amends the decision it changes in place (the `D<n>` or `E<n>` paragraph of the design document under `docs/`), updates that document's status line, and updates the usage reference (`docs/state-core-usage.md`) and, where a user meets the change, getting started, concepts or troubleshooting, in the same change. A new finding from a live run gets the next `F<n>` number in the design's §7.
- **Facts and claims stay apart.** What the code observed (a process returned, a file changed, a command exited) and what a model wrote (tasks, findings, rationale) are never merged in code, receipts or documentation; nothing upgrades a claim. A reviewer's finding is a finding, not a decision.
- **Each client's evidence is its own.** Something observed with Claude Code is Claude evidence, not Codex or Copilot evidence, and the other way round; a claim of support for a client needs a run on that client.
- **The audit is not widened quietly.** A change to what a worker may write, what a launch allows, what an inbox file may do or what a receipt proves needs a design decision first.
- **Nothing private in the tree.** Documents and reports never carry local paths, real project names, account names or secrets; the project the coordinator was proven on is "a real TypeScript project". Search your diff for your home directory and your machine's names before you send it.

## Reporting live evidence

A live run (a real client) is reported with: the exact source commit, the client names and versions, the model names, the pipeline with its budgets, every stop with its reason, the interventions (inbox decisions, accepts, resolves) in order, the costs the clients reported (marked as client-reported), and the findings with their `F<n>` numbers. "Zero interventions" means a run that ended `done` with none; "unattended" never means unsupervised overnight operation. The receipts under `.git/coordinator/` of the run are the evidence: keep them and quote them, never retype them. Each design document's result paragraphs show the form.

## Commits

One change per commit, the message in the form `type(coordinator): what changed and why`, with `feat`, `fix`, `test`, `docs` or `chore`; the subject may be long, since it is read as the record. A commit that changes behaviour names the design decision it implements or amends.

## Security

Workers run as the OS user who runs the coordinator, with that user's environment and network; the safety model is in [Getting started](docs/getting-started.md#safety). A way for a worker, a reviewer or an inbox file to defeat the audit (the scope checks, the sealed receipts, the token fence) is a security finding: send it to the maintainer privately before opening a public issue.

## License

The project is released under the [MIT License](LICENSE); a contribution is accepted under the same terms.
