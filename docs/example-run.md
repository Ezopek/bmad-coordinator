# A recorded run of the example

The [quickstart](getting-started.md#quickstart-the-example-epic) run exactly as written, on 2026-10-20, from a clean checkout of the coordinator at commit `4b5bf3f` with Claude Code 2.1.304 as the only client, Git 2.53 and Python 3.14 on WSL2 (Ubuntu, ext4). Every command below is the quickstart's; every output is what it printed, with three edits: the absolute paths are shortened to `$SRC` (the checkout), `$WORK` (the work directory) and `$HOME`, the installer preview's `agents_diff` is replaced by a note (getting started shows the block), and nothing else. The identifiers are the run's own. The second half shows a stop from an earlier run of the same commands, with the cause and the way out.

The whole run: three stories to `epic-done`, 4 minutes 43 seconds from `epic start` to the summary, 12 client launches, USD 1.08 as reported by Claude Code, zero interventions.

## Setup

```sh
SRC=/absolute/path/to/bmad-coordinator
WORK=/absolute/path/on/ext4/greetings
REPO="$WORK/main"
run() { python3 "$REPO/.bmad-coordinator/run.py" --repo "$REPO" "$@"; }
```

```text
$ python3 $SRC/examples/greetings/make_fixture.py $WORK
repository: $WORK/main
pipeline:   $WORK/pipeline.json
epic input: $WORK/epic-input.json
next: install the coordinator into the repository and commit it (docs/getting-started.md, Quickstart)
[exit 0]

$ python3 $SRC/install.py --repo $WORK/main --preview > $WORK/preview.json
[exit 0]
(the preview file, without its `files` list of 26 entries:)
{
 "agents_diff": "(the AGENTS.md block, as getting started shows it)",
 "sha256": "2fe996a71148ce66be7e11cef007f20e54a77279f79c240d5114aa970147268b",
 "warnings": []
}

$ python3 $SRC/install.py --repo $WORK/main --apply --preview-sha256 2fe996a71148ce66be7e11cef007f20e54a77279f79c240d5114aa970147268b
{"applied": true, "package_revision": "bc4a13eb385f654193469d0a5c14395f27d464ce6007cf28d849defaa0abf3f0"}
[exit 0]

$ git -C $WORK/main add AGENTS.md .bmad-coordinator
[exit 0]

$ git -C $WORK/main -c user.name=You -c user.email=you@example.invalid commit -qm install coordinator
[exit 0]

$ run init
{"repository_id": "24c08a85-034f-4871-a7fe-2cb54242f212"}
[exit 0]

$ run doctor --base 42582ea0d4109035c72d4b979a9d078a057583a5 --client claude-code
{"base_commit": "42582ea0d4109035c72d4b979a9d078a057583a5", "client": {"binary": "$HOME/.local/bin/claude", "name": "claude-code", "ready": true, "reasons": [], "version": "2.1.304 (Claude Code)"}, "package_revision": "bc4a13eb385f654193469d0a5c14395f27d464ce6007cf28d849defaa0abf3f0", "ready": true, "reasons": []}
[exit 0]
```

`doctor` names the client binary it found and its version, the package revision it installed and the base commit; `ready: true` with empty `reasons` is the whole answer.

## The epic run

```text
$ mkdir -p $WORK/judgments
$ run epic start --token-file $WORK/owner.json --pipeline $WORK/pipeline.json --max-turns 80 --deadline 7200 --judgment-root $WORK/judgments < $WORK/epic-input.json
```

It printed nothing for 4 minutes 43 seconds (12:39:01 to 12:43:44 UTC), then one JSON summary, and exited `0`. The summary, abridged to the fields a reader looks at first (the full one also carries `format`, `generation`, `ownership` and `subjects`):

```json
{
 "kind": "epic-summary",
 "outcome": "done",
 "reason": "epic-done",
 "counts": {
  "merges": 3,
  "runs": 3,
  "stories_done": 3,
  "stories_restarted": 0,
  "stories_skipped": 0,
  "syncs": 0
 },
 "epic_id": "3cddd5e495ed417ba6aae19a457ef4a5",
 "run_id": "run-001-1a702f",
 "operator_action": null,
 "epic_action": null,
 "epic": {
  "branch": "coordinator/epic-1",
  "head_sha": "0a9bfa36e7ef27a02e810c89ba6225cfb18e5709",
  "base_branch": "master",
  "base_synced_sha": "42582ea0d4109035c72d4b979a9d078a057583a5",
  "stories": [
   {
    "key": "1-1-greeting",
    "state": "already-done",
    "outcome": null
   },
   {
    "key": "1-2-farewell",
    "state": "done",
    "outcome": "done"
   },
   {
    "key": "1-3-shout",
    "state": "done",
    "outcome": "done"
   },
   {
    "key": "1-4-whisper",
    "state": "done",
    "outcome": "done"
   }
  ],
  "worktree": "$WORK/worktrees/epic-1"
 }
}
```

`operator_action` and `epic_action` are `null` because nothing is asked of you: the run ended `done` with `reason: epic-done`. A stop puts its reason and the exact commands there instead (see [A stop, and the way out](#a-stop-and-the-way-out)).

## What each story did

Each story ran the example pipeline (`$WORK/pipeline.json`): a Sonnet spec author, an Opus spec review, an automatic accept (`spec_auto_accept: true`), the status commit, the setup and baseline, a Sonnet implementation attempt, the attempt commit, the `unit` verify command, a bundle, an Opus code review, the acceptance and the `done` status commit, then the merge into `coordinator/epic-1`. The three stories took 22 driver turns each.

| Story | Spec | Spec review (Opus) | Implementation | Verify | Code review (Opus) | Result |
| --- | --- | --- | --- | --- | --- | --- |
| 1.2 farewell | written, `completed` | 2 `low` | attempt 2, `completed` | `unit: pass` | no findings | accepted, `done`, merged |
| 1.3 shout | written, `completed` | 1 `low` | attempt 2, `completed` | `unit: pass` | no findings | accepted, `done`, merged |
| 1.4 whisper | written, `completed` | 2 `low` | attempt 2, `completed` | `unit: pass` | no findings | accepted, `done`, merged |

The `low` findings are the reviewers' claims (for 1.2: the spec has no Given/When/Then acceptance lines; the empty-name fallback is inferred from `greet`, not stated by the epic); `low` and `medium` never block. No story asked a question, so the judgment role was never launched and `$WORK/judgments` stayed empty. No verify command failed, so no correction ran. Story 1.1 was `already-done` in `sprint-status.yaml` and was skipped, as the epic input says.

## The launches and what they cost

Read from the sealed receipts under `$REPO/.git/coordinator/runs/` (one directory per launch: `launch.json` with the argv and the environment variable names, `return.json` with the validated return and `client_facts`, the retained event stream). The cost is Claude Code's own `total_cost_usd` from its result record, which the coordinator records as a client fact, not something it measured; "client turns" is the client's `num_turns`. Wall clock is the coordinator's: the launch record's time to the return's.

| # | Started (UTC) | Story | Role | Client / model | Outcome | Client turns | Cost (client-reported, USD) | Wall clock |
|---|---|---|---|---|---|---|---|---|
| 1 | 12:39:02 | 1-2-farewell | worker | claude-code / sonnet | completed | 9 | 0.06 | 26 s |
| 2 | 12:39:29 | 1-2-farewell | reviewer | claude-code / opus | completed | 6 | 0.12 | 25 s |
| 3 | 12:39:56 | 1-2-farewell | worker | claude-code / sonnet | completed | 9 | 0.05 | 23 s |
| 4 | 12:40:20 | 1-2-farewell | reviewer | claude-code / opus | completed | 6 | 0.12 | 19 s |
| 5 | 12:40:40 | 1-3-shout | worker | claude-code / sonnet | completed | 10 | 0.07 | 21 s |
| 6 | 12:41:03 | 1-3-shout | reviewer | claude-code / opus | completed | 9 | 0.13 | 37 s |
| 7 | 12:41:41 | 1-3-shout | worker | claude-code / sonnet | completed | 7 | 0.05 | 17 s |
| 8 | 12:41:59 | 1-3-shout | reviewer | claude-code / opus | completed | 6 | 0.12 | 15 s |
| 9 | 12:42:16 | 1-4-whisper | worker | claude-code / sonnet | completed | 6 | 0.05 | 17 s |
| 10 | 12:42:34 | 1-4-whisper | reviewer | claude-code / opus | completed | 6 | 0.11 | 29 s |
| 11 | 12:43:04 | 1-4-whisper | worker | claude-code / sonnet | completed | 9 | 0.05 | 18 s |
| 12 | 12:43:24 | 1-4-whisper | reviewer | claude-code / opus | completed | 6 | 0.13 | 20 s |

Launches: 12; client-reported cost: USD 1.08 over 12 launches, 0 without a reported cost; first launch to last return: 282 s.

The reviewer (Opus) costs about twice the worker (Sonnet) per launch here; the [model selection guide](model-selection-guide.md) has the figures from the other runs and clients.

## What is on disk afterwards

Every commit is the coordinator's, one per attempt and one per status change, on the story branches and merged fast-forward into the integration branch:

```text
$ run epic status
(the same stories and summary as the JSON above, with "owner": {"state": "released"}: the epic gave the repository back at epic-done)

$ git -C $WORK/main log --oneline --all
0a9bfa3 story(1-4-whisper): status done
be46854 story(1-4-whisper): attempt 2 [completed]
9af254d story(1-4-whisper): status in-progress
1b2b2ba story(1-4-whisper): spec ready-for-dev
25f6fdb story(1-4-whisper): attempt 1 [completed]
b15f471 story(1-3-shout): status done
4c2768d story(1-3-shout): attempt 2 [completed]
2e84791 story(1-3-shout): status in-progress
573dfe8 story(1-3-shout): spec ready-for-dev
c5d768a story(1-3-shout): attempt 1 [completed]
b5de29b story(1-2-farewell): status done
915c7bd story(1-2-farewell): attempt 2 [completed]
2c14d37 story(1-2-farewell): status in-progress
e0b07c7 story(1-2-farewell): spec ready-for-dev
4715024 story(1-2-farewell): attempt 1 [completed]
42582ea install coordinator
a96c6ca greetings fixture

$ git -C $WORK/main worktree list
$WORK/main                         42582ea [master]
$WORK/worktrees/epic-1             0a9bfa3 [coordinator/epic-1]
$WORK/worktrees/story-1-2-farewell b5de29b [story/1-2-farewell]
$WORK/worktrees/story-1-3-shout    b15f471 [story/1-3-shout]
$WORK/worktrees/story-1-4-whisper  0a9bfa3 [story/1-4-whisper]
```

What the three stories changed against the base commit, on `coordinator/epic-1`:

```text
$ git -C $WORK/main diff --stat 42582ea coordinator/epic-1
 .../implementation-artifacts/spec-1-2-farewell.md  | 56 +++++++++++++++++++++
 .../implementation-artifacts/spec-1-3-shout.md     | 57 ++++++++++++++++++++++
 .../implementation-artifacts/spec-1-4-whisper.md   | 56 +++++++++++++++++++++
 .../implementation-artifacts/sprint-status.yaml    |  8 +--
 pkg/farewell.py                                    |  2 +
 pkg/shout.py                                       |  2 +
 pkg/whisper.py                                     |  2 +
 tests/test_farewell.py                             | 14 ++++++
 tests/test_shout.py                                | 14 ++++++
 tests/test_whisper.py                              | 14 ++++++
 10 files changed, 221 insertions(+), 4 deletions(-)
```

One of the files the worker wrote, `pkg/shout.py` (the fallback to `stranger` is the convention the spec author took from `greet`; the reviewer listed it as a `low` finding, as inferred rather than stated):

```python
def shout(n):
    return 'HEY, %s!' % (n or 'stranger')
```

The end of each spec on its story branch, written by the coordinator at `done`:

```text
$ git -C $WORK/main show story/1-3-shout:_bmad-output/implementation-artifacts/spec-1-3-shout.md | tail -7
## Coordinator Result

- acceptance: `ece3282f-bdb8-4f21-8791-526d7494b0bc`
- accepted commit: `4c2768d36308291c1bff154038e735a0a7cb1827`
- review: `84c44373-de1b-4ec5-9dd9-e0d77d9ddc18`
- verification: unit: pass
- decisions: none
```

`run detail review 84c44373-de1b-4ec5-9dd9-e0d77d9ddc18` prints that review's findings with the exact bundle it read; `run verify` reports whether the acceptance is still current.

The evidence directory, `$REPO/.git/coordinator/`: `bindings/` (12 files, what each attempt was given), `bundles/` (6, the sealed copies the reviewers read), `drives/` (3 leases, one `turn-*.json` per driver turn and a `summary.json` per run), `epics/` (the epic's anchored states and summaries), `pipelines/` (the sealed pipeline per story and revision), `runs/` (12 launches), `story/` (3 features: setup, baseline, commit, verify and status receipts), `state.sqlite3` and `drive.lock`. Everything the quickstart made is under `$WORK`; `rm -rf "$WORK"` removes it all.

## A stop, and the way out

Twelve minutes earlier the same commands ran on the previous commit (`265697d`) and did not reach `epic-done`. Story 1.2 went `done` in three minutes, with more happening than above: its spec author asked a question (`needs-decision`), the Sonnet judgment answered it citing the project clause `P2`, the author continued and completed, and the implementation needed one correction after a `high` finding (`accept_on_high: false`); eight launches, USD 0.59. Story 1.3's spec review then raised a `critical` finding (the author had settled two intent gaps inside the frozen block as a "decision" instead of listing them as open questions), the correction attempt returned `needs-decision` with two questions, and the run stopped after 4 minutes 38 seconds with exit `11`:

```json
{
 "outcome": "needs-human",
 "reason": "attempt-unknown",
 "operator_action": "worker reconcile a0d35e0f-803e-4a42-a54a-c19f93fc28b9 --token-file <driver token>, investigate, and only if needed attempt resolve a0d35e0f-803e-4a42-a54a-c19f93fc28b9 --confirm-unknown --token-file <driver token>; then epic resume",
 "epic_action": "epic resume continues story 1-3-shout; epic resume --skip 1-3-shout (allowed once this run has ended needs-human or failed and no attempt of the story holds the fence) leaves it to the operator and continues with the next story; release --token-file <driver token> hands the repository back, after which epic resume --epic 2066048a4fa94d32bff668c47ae92cf1 --token-file <new file> continues",
 "counts": {
  "merges": 1,
  "runs": 2,
  "stories_done": 1,
  "stories_restarted": 0,
  "stories_skipped": 0,
  "syncs": 0
 },
 "story": {
  "key": "1-3-shout",
  "reason": "attempt-unknown",
  "run_id": "run-001-10095a"
 }
}
```

`attempt-unknown` means the coordinator cannot say what the attempt's result was. The receipts under `runs/f81d76fd…/` show what it could say: `end.json` (the process exited `0`, nothing of it alive), `last.json` (the client's structured return, with the two questions), `events.jsonl` (the retained stream), `spec-return.md` (the spec as sealed at return), and no `return.json`: the return was refused. The driver's last turn record, `drives/f086a9ab…/run-001-10095a/turn-9.json`, has the reason in `evidence_needs`:

```text
"Spec return invalid: open_questions differ from the spec's Open Questions section"
```

The cause was a defect, not the worker: it had written its second option as `fallback like \`greet\`'s \`stranger\`` in the spec and returned the label as `fallback like greet's stranger`; the questions were compared with code marks aside, the option labels were not (finding F21 in the [implementation drive design](implementation-drive-design.md), fixed in commit `4b5bf3f`, the commit the run above used). The documented first step confirmed the state without changing it:

```text
$ run worker reconcile a0d35e0f-803e-4a42-a54a-c19f93fc28b9 --token-file $WORK/owner.json
{"assignment_id": "a0d35e0f-803e-4a42-a54a-c19f93fc28b9", "evidence_needs": ["Durable receipt anchor is missing or ambiguous"], "run_dir": "$WORK/main/.git/coordinator/runs/f81d76fdafa20e298db461f785d872975954fcee12a631cbb62aecf963b5c065", "state": "unknown"}
```

The fix does not rescue the refused attempt: a refused return writes no terminal receipt, and `reconcile` binds only a verified one, so the attempt stays `unknown` with the story's fence held (upgrading the installed package in place, `install.py --upgrade --preview` then `--upgrade --apply`, worked while the epic held the repository; the attempt still stayed `unknown`). The way out is the one the summary and [Troubleshooting](troubleshooting.md#during-a-run) spell: make sure nothing of the attempt still runs, `run attempt resolve a0d35e0f-… --confirm-unknown --token-file $WORK/owner.json` with a reason on stdin, `run epic resume` (which stops once more, `attempt-resolved`, with the resolved attempt in view), then `run epic resume --restart 1-3-shout` for a new attempt chain or `--skip 1-3-shout` to leave the story and continue with 1.4. The resolve is deliberately yours: the fence is released on operator evidence only, never on the coordinator's own reading of a process that left no valid return.
