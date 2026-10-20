"""Spec author profile (design D24): the first attempt of a story added from its epic entry writes the story's spec.

Thin layer over the implementation profile: `bmad_implementation.prepare` binds the author (kind `spec`) with the brief
rendered here; the return check is the implementation profile's Git-delta check plus the spec's own facts (its
`## Open Questions` entries equal the return's, ruling R2); commit, bundle and currency are the implementation
profile's. `digest` renders a spec's frozen section for the decisions stop; `review_brief` briefs the review of a
spec bundle. Nothing here accepts work."""

import hashlib
import json
import os
import re
from dataclasses import replace
from pathlib import Path

from . import bmad_implementation as implementation
from .bmad_implementation import READY, SPEC_AUTHOR
from .model import DomainError
from .profiles import ReturnProfile
from .run_receipt import private_file

# The skeleton of the BMAD build skill's spec template, as the coordinator's own text; the brief appends the
# pipeline's verify commands to its Verification section.
SPEC_TEMPLATE = """---
title: '<story title>'
type: 'feature'
status: 'draft'
context: []
---

<frozen-after-approval reason="human-owned intent">

## Intent

**Problem:** <one or two sentences: what is missing and why it matters>

**Approach:** <one or two sentences: the what, not the how>

## Boundaries & Constraints

**Always:** <an invariant rule>

**Never:** <a non-goal or a forbidden approach>

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| <name> | <input> | <expected> | <handling> |

</frozen-after-approval>

## Open Questions

- Q1: <question> — options: <label> (<consequence>) / <label> (<consequence>) — recommendation: <label>

## Code Map

- `<path>` -- <role or relevance>

## Tasks & Acceptance

- [ ] `<path>` -- <action> -- <rationale>

## Verification

**Commands:**
"""
SEALED = "spec-return.md"   # the spec's bytes at return, in the run directory
_ENTRY = re.compile(r"- (Q[0-9]{1,3}): (.+?) — options: (.+?) — recommendation: (.+)")   # ids as `_entries` (P12)
_OPTION = re.compile(r"(.+?) \((.*)\)")


def _bad(message: str) -> DomainError:
    return DomainError("UNKNOWN_EVIDENCE", "Spec return invalid: " + message)


def _open_section(text: str) -> str | None:
    """The text of the spec's `## Open Questions` section, None when absent."""
    return implementation._section(implementation._plain(text)[2], "Open Questions")


def _status(text: str) -> str | None:
    try:
        return implementation._frontmatter(implementation._plain(text)[2]).get("status")
    except DomainError:
        return None


def parse_open_questions(text: str) -> list[dict]:
    """The spec's `## Open Questions` entries in order, each `- Q<n>: <question> — options: <label> (<consequence>) /
    <label> (<consequence>) — recommendation: <label>`; [] without the section. A bullet of another shape is
    UNKNOWN_EVIDENCE: a question the coordinator cannot read is never dropped."""
    entries = []
    for line in (_open_section(text) or "").split("\n"):
        line = line.strip()
        if not line.startswith("- "):
            continue
        match = _ENTRY.fullmatch(line)
        options = [_OPTION.fullmatch(part.strip()) for part in match.group(3).split(" / ")] if match else [None]
        if None in options:
            raise _bad("an Open Questions entry is not in the template's shape: "
                       + json.dumps(line[:80], ensure_ascii=False))
        entries.append({"id": match.group(1), "question": match.group(2).strip(),
                        "options": [{"label": item.group(1).strip(), "consequence": item.group(2).strip()}
                                    for item in options],
                        "recommendation": match.group(4).strip()})
    return entries


def digest(text: str) -> str:
    """Design D24: the decisions stop's rendering of a spec's frozen section: the Intent problem and approach
    sentences, each matrix row as `when <input>, then <expected>` (code quotes dropped), the Always list (ruling P8:
    the permissive clauses the human approves) and the Never list. The text is the author's, quoted as the spec holds
    it."""
    spec = implementation.parse_spec(text)
    intent = implementation._section(spec.frozen_text, "Intent") or ""
    approach = next((line.strip()[len("**Approach:**"):].strip() for line in intent.split("\n")
                     if line.strip().startswith("**Approach:**")), "")
    rows = [[cell.replace("`", "") for cell in row.split(" | ")] for row in spec.matrix_rows]
    lines = [f"Problem: {spec.problem}", f"Approach: {approach}",
             *[f"- when {cells[1]}, then {cells[2]}" for cells in rows if len(cells) > 2]]
    lines += [f"Always: {'; '.join(spec.always)}"] if spec.always else []
    return "\n".join(lines + ([f"Never: {'; '.join(spec.never)}"] if spec.never else []))


# Return profile -------------------------------------------------------------------------------------------------------

def _entries(items) -> bool:
    """`open_questions` in the return's shape: [{id, question, options: [{label, consequence}], recommendation}], each
    id `Q<n>` (ruling P9)."""
    return isinstance(items, list) and all(
        isinstance(item, dict) and set(item) == {"id", "question", "options", "recommendation"} and
        all(type(item[key]) is str for key in ("id", "question", "recommendation")) and
        re.fullmatch(r"Q[0-9]{1,3}", item["id"]) is not None and
        isinstance(item["options"], list) and all(
            isinstance(option, dict) and set(option) == {"label", "consequence"} and
            all(type(value) is str for value in option.values()) for option in item["options"])
        for item in items)


def _same(text: str) -> str:
    """A question as compared (ruling R2, finding F18): code quotes dropped and whitespace collapsed, so a return that
    quotes the file's entry without its Markdown code marks still names it."""
    return " ".join(text.replace("`", "").split())


def _compared(items) -> list:
    """What the return and the file share entry by entry (ruling R2): the id, the question (as `_same`), the option
    labels."""
    return [(item["id"], _same(item["question"]), [option["label"] for option in item["options"]]) for item in items]


def _sealed_text(binding: dict, run_dir) -> tuple[str, str]:
    """The spec's text and digest at return. The return's own check (no `return.json` yet) seals the worktree bytes
    into the run directory, refusing a file already there; every later check (a receipt validated again after the
    worktree moved on) reads them there, so a verdict never depends on later bytes; the payload's digest pins them."""
    sealed = Path(run_dir) / SEALED
    try:
        if not os.path.lexists(Path(run_dir) / "return.json"):
            private_file(sealed, (Path(binding["worktree"]) / binding["spec_path"]).read_bytes())
        data = sealed.read_bytes()
        return data.decode("utf-8"), hashlib.sha256(data).hexdigest()
    except (OSError, UnicodeDecodeError) as exc:
        raise _bad("the spec file cannot be sealed or read") from exc


def check(final, assignment, binding, run_dir, observed_changed) -> dict:
    """Profile check (design D24, ruling R2): the implementation profile's check (notes, the question aggregate, the
    Git delta) without tasks and verification; the attempt changes the spec file only; `open_questions` and
    `assumptions` are typed; a `completed` return has no open question and changed the spec file, which has no
    `## Open Questions` section and the status `ready-for-dev`; a `needs-decision` return's `open_questions` equal the
    spec's entries (ids, questions with code quotes and whitespace aside, option labels) and its aggregate asks the
    entries' questions in order. A
    `completed` return that changed nothing is an empty claim (ruling P14): the driver's empty-attempt rule stops it."""
    payload = implementation.check({**final, "tasks": [], "verification": []}, assignment, binding, run_dir,
                                   observed_changed)
    if list(observed_changed) not in ([], [binding["spec_path"]]) or payload["deleted"]:
        raise _bad("a spec attempt changes the spec file only")
    questions, assumptions = final["open_questions"], final["assumptions"]
    if not _entries(questions):
        raise _bad("open_questions must be a list of {id, question, options: [{label, consequence}], recommendation}")
    if not isinstance(assumptions, list) or any(type(item) is not str for item in assumptions):
        raise _bad("assumptions must be a list of text")
    text, sha256 = _sealed_text(binding, run_dir) if observed_changed else (None, None)
    if final["result"] == "completed" and (questions or text is not None and (_open_section(text) is not None or
                                                                               _status(text) != READY)):
        raise _bad(f"a completed spec attempt leaves {binding['spec_path']} with no `## Open Questions` section and "
                   f"the status {READY}, and open_questions empty")
    if final["result"] == "needs-decision":
        entries = parse_open_questions(text) if text is not None else []
        if not questions or _compared(questions) != _compared(entries) or \
           [_same(item) for item in payload["questions"]] != [_same(item["question"]) for item in entries]:
            raise _bad("open_questions differ from the spec's Open Questions section")
    kept = {key: value for key, value in payload.items() if key not in ("tasks", "verification")}
    return {**kept, "open_questions": questions, "assumptions": assumptions, "spec_sha256": sha256}


PROFILE = ReturnProfile(
    SPEC_AUTHOR,
    {"open_questions": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["id", "question", "options", "recommendation"],
        "properties": {"id": {"type": "string"}, "question": {"type": "string"},
                       "options": {"type": "array", "items": {
                           "type": "object", "additionalProperties": False, "required": ["label", "consequence"],
                           "properties": {"label": {"type": "string"}, "consequence": {"type": "string"}}}},
                       "recommendation": {"type": "string"}}}},
     "assumptions": {"type": "array", "items": {"type": "string"}},
     "notes": {"type": "string"}},
    {"PYTHONDONTWRITEBYTECODE": "1"}, check, implementation.gate)


# Prepare and brief (design D24 "The brief") ---------------------------------------------------------------------------

def sources(repo, feature: dict, story: dict, project: dict) -> tuple[str, list[str], list[str], list[str]]:
    """The brief's repository inputs: the epic entry verbatim at the feature's base commit (read as `story add` checked
    it), the project block's `context` paths, the specs of the same epic in the artifacts directory whose
    frontmatter status is `done` (paths only), and the precondition state at the base commit as brief lines (design
    epic-drive E6)."""
    epic = story.get("epic_story")
    if not isinstance(epic, dict):
        raise DomainError("STALE_INPUT", "A spec attempt needs a story added from its epic entry")
    entry = implementation._epic_entry(repo, feature["base_commit"], story["story_key"], epic["epics_path"],
                                       story["spec_path"])
    done = []
    for path in sorted((Path(repo.worktree) / implementation._artifacts_dir(project)).glob(
            f"spec-{story['story_key'].split('-')[0]}-*.md")):
        try:
            status = None if path.is_symlink() else _status(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        if status == "done":
            done.append(path.relative_to(repo.worktree).as_posix())
    found = implementation.preconditions_state(repo, feature["base_commit"], implementation._artifacts_dir(project),
                                               story.get("preconditions", []))
    return entry.text, list(project.get("context", [])), done, \
        implementation.preconditions_section(found, feature["base_commit"])


def brief(spec, feature, story: dict, decisions, continuation, project, epic_text: str, context_paths, done_specs,
          preconditions) -> str:
    """Design D24: the coordinator's own text, no skill. The epic entry and the read-first paths come from the
    repository, the rest from stored rows; worker text reaches it only JSON-quoted, through the continuation."""
    path = story["spec_path"]
    reads = [f"- `{item}`" for item in [*project.get("rules", []), *context_paths]]
    reads += [f"- `{item}` (a done spec of this epic; read it if useful)" for item in done_specs]
    lines = [f"# BMAD story spec {story['story_key']} (headless, one attempt)", "",
             "You are a one-attempt headless spec author in a fresh process in a dedicated Git worktree on the story "
             "branch: you write the story's spec from its epic entry, never code. The coordinator holds all state, "
             "commits every attempt itself and has the spec reviewed; this brief is everything you have.", "",
             "## Read first", *(reads or ["- nothing beyond the repository"]), "",
             "## Epic entry (verbatim)", epic_text, "",
             *preconditions, "",
             "## Writable scope", f"- `{path}` (the spec)", "Everything else is read only.", "",
             *_story_scopes(feature, story),
             "Every task changes files inside these paths only. A change the story needs outside them is an Open "
             "Question (options: the operator adds the path to the story's scopes before implementation / the "
             "operator makes that change by hand after the story), never a task.", "",
             *implementation._authority(feature, decisions), *implementation._restart_lines(story), "",
             "## Spec template",
             "Write the spec in this shape: the `<frozen-after-approval>` block holds the intent, the sections after "
             "it the plan; its `## Verification` commands are the pipeline's, exactly as listed here.",
             "```markdown", SPEC_TEMPLATE.rstrip("\n"), *implementation._render_commands(project["verify"]), "```"]
    if continuation:
        lines += ["", continuation["heading"], *continuation["lines"]]
    lines += ["", "## Rules",
              "- Plan as the BMAD build skill's plan step does: investigate the repository first and ask nothing that "
              "the repository, this brief or the files above can settle; put what you found into `## Code Map`.",
              "- Keep to one story, one user-facing goal, in 900–1600 tokens; a second deliverable is out of scope.",
              "- Add an `## Open Questions` entry only for an intent gap: what the epic entry does not say, the code "
              "cannot settle and the user would notice in the result. Each entry is one line in the template's shape "
              "(ids `Q1`, `Q2`, …), with options, each option's consequence and your recommendation. Every other "
              "choice is yours: decide it and record it in the spec.",
              "- A task outside the story scopes is a defect of the spec; ask instead.",
              "- An unmet precondition is an entry whose question starts with `BLOCKED:` and names its ledger entry.",
              "- Frontmatter `status: 'draft'` while any entry remains; when none remains, delete the `## Open "
              "Questions` section and set `status: 'ready-for-dev'`.",
              "- A decision in the continuation answers its entry: write the answer into the `<frozen-after-approval>` "
              "block as a decision and delete the entry.",
              f"- Write only `{path}`; never edit, move or delete any other file, and run no project command.",
              implementation.GIT_RULE,
              *(["- " + implementation.WEB_RULE] if spec.web else []),
              "- Return `completed` with `open_questions` `[]` when no entry remains; else `needs-decision` with "
              "`open_questions` one `{id, question, options: [{label, consequence}], recommendation}` per entry, as in "
              "the file and in its order, `unresolved_question` the JSON object `{\"open_questions\": [\"<each entry's "
              "question>\"…], \"conflicts\": [\"<a decision you cannot follow>\"…]}` and `options` each entry's "
              "options as `\"<label>: <consequence>\"`. Use `result` `blocked` only when you cannot read this brief.",
              "- Return the required generic fields plus `open_questions`, `assumptions` (one text per choice you made "
              "that the epic entry does not state) and `notes` (surprises).",
              *implementation._return_rules(PROFILE)]
    return "\n".join(lines) + "\n"


def _story_scopes(feature: dict, story: dict) -> list[str]:
    """Design §7 F9: the implement attempts' writable paths (never the spec path), for the author and the reviewer."""
    return ["## Story scopes (the implement attempts' writable paths)",
            *([f"- `{scope}`" for scope in implementation._code_scopes(feature, story)] or ["- none"])]


def prepare(repo, store, owner, spec: implementation.ImplementationPrepare) -> dict:
    """The spec author's prepare: `bmad_implementation.prepare` with kind `spec`."""
    return implementation.prepare(repo, store, owner, replace(spec, kind="spec"))


def review_brief(spec, worktree, sealed, bundle: dict, feature: dict, manifest: dict, decisions, carried, store, repo,
                 snapshot) -> str:
    """Design D24: the spec review brief, `bmad_implementation.review_brief`'s counterpart for a spec bundle: the epic
    entry, the precondition state (design epic-drive E6), the project rules and the context paths as the author brief
    reads them, the feature's delegated clauses (M3), the story's code scopes (design §7 F9), the pipeline's verify
    commands from the author's binding, the five spec lenses; carried items, decisions, rubric and rules as the code
    review brief has them."""
    story = json.loads(feature["constraints_json"])
    project = implementation.read_assignment_binding(store, repo, manifest["commit"]["assignment_id"])["project"]
    epic_text, context_paths, _, preconditions = sources(repo, feature, story, project)
    lines = ["# BMAD review of a sealed story spec (one attempt)", "",
             "You are a fresh, read-only reviewer of one story's spec, written from its epic entry by a spec author "
             "and committed by the coordinator. The coordinator holds all state; this brief is everything you have. "
             "You read the spec, you do not run anything.", "",
             "## Read first", *([f"- `{item}`" for item in [*project.get("rules", []), *context_paths]] or
                                ["- nothing beyond this brief"]), "",
             "## Epic entry (verbatim)", epic_text, "",
             *preconditions, "",
             "## Delegated authority clauses",
             *[f"- `{item['id']}`: {item['text']}" for item in json.loads(feature["authority_json"])["delegated"]], "",
             *_story_scopes(feature, story), "",
             "## Spec (sealed bytes)", "Read this sealed copy, not the live worktree.",
             *[f"- `{item['path']}` at `{item['sealed_path']}`" for item in sealed]]
    lines += implementation._review_records(carried, decisions)
    lines += ["", "## Review focus (one lens each; a finding cites the lens)",
              "- `readiness`: every task names a file and an action, the acceptance criteria are Given/When/Then, no "
              "placeholder remains and nothing in the spec contradicts anything else in it.",
              "- `scope`: one user-facing goal, one story; a second deliverable is out of scope; a task that changes a "
              "file outside the story scopes (listed above) is `high`: the implementation cannot write there.",
              "- `verification`: the `## Verification` commands are the pipeline's, exactly these, and running them "
              "would exercise every I/O & Edge-Case Matrix row:",
              *[f"  {line}" for line in implementation._render_commands(project["verify"])],
              "- `edges`: the matrix covers the inputs a reasonable person would try; an unanswered intent gap hidden "
              "in the prose instead of `## Open Questions` is `critical`.",
              "- `fidelity`: nothing in the spec contradicts the epic entry, the files under Read first, a delegated "
              "authority clause (the project clauses) or a recorded decision.",
              *implementation._review_rules(spec, bundle)]
    return "\n".join(lines)
