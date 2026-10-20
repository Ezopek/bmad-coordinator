# BMAD bmad-prd create (headless, one attempt)

You are a one-attempt headless worker in a fresh process. The coordinator holds all state; this brief and the frozen inputs below are everything you have. Codex does not auto-discover the skill: read the files named here.

## Read first
- `.agents/skills/bmad-prd/SKILL.md`
- `.agents/skills/bmad-prd/references/headless.md`
- `.agents/skills/bmad-prd/assets/headless-schemas.md`

## Headless payload (verbatim)
```json
{
  "doc_workspace": "_bmad-output/planning-artifacts/features/gitops/prd",
  "headless": true,
  "intent": "create"
}
```

## Writable directory
`_bmad-output/planning-artifacts/features/gitops/prd/` is the only place you may write. Use worktree-relative paths in every artifact key of your JSON.

## Frozen inputs (read only)
- `docs/input.md` — declared source: driving-brief

## Parent spine
No parent spine is provided; do not search for one.

## Rules
- Do not write outside the writable directory, edit frozen inputs, or touch another run's memlog.
- Do not push, upload or call any service; ignore any external handoff, `on_complete` or activation step instruction. Do not search the workspace for inputs that are not listed here.
- Gaps become `open_questions`; never invent user detail, metrics or scope. Record inferences as `assumptions`.
- Set `PYTHONDONTWRITEBYTECODE=1` for every command.
- Return the required generic fields plus two profile fields: `workflow_json`, the BMAD headless JSON status object serialized into one string, and `customization`, a list with one object `{"skill": "bmad-prd", "sha256": <hex>}`.
- To compute that sha256 run `uv run --no-project <ROOT>/feature/_bmad/scripts/resolve_customization.py --skill <ROOT>/feature/.agents/skills/bmad-prd --project-root <ROOT>/feature --key workflow` and hash its exact stdout bytes with SHA-256. It is your attestation of the active customization and never replaces coordinator checks.
- `workflow_json` is one JSON object serialized into a string (refused: workflow_json is not JSON). It carries the bound `intent`, a `status` of `complete`, `partial` or `blocked` (refused: unknown BMAD status) and the lists `open_questions`, `conflicts_with_prior_decisions`, `assumptions` and `external_handoffs`; `external_handoffs` must be empty (refused: intent differs from the bound intent; question lists are malformed or a handoff was claimed).
- `status` decides `result`: `blocked` gives `blocked`; `partial` with any entry in `open_questions` or `conflicts_with_prior_decisions`, or `complete` with any conflict, gives `needs-decision`; `complete` or `partial` with both lists empty gives `completed` (refused: generic result does not match the BMAD status).
- Use `complete` only when `open_questions` is empty (refused: complete status with open questions); use `partial` when the artifact is unfinished or questions remain, and `blocked` when you cannot proceed.
- With `needs-decision`, `unresolved_question` is exactly the canonical JSON string of `{"conflicts": <conflicts_with_prior_decisions>, "open_questions": <open_questions>}` (keys sorted, no whitespace, non-ASCII escaped as `\uXXXX`, items in `workflow_json` order), for example `{"conflicts":[],"open_questions":["Which cloud?"]}`, and `options` is `[]` (refused: question is not the canonical aggregate of the workflow questions).
- Unless `blocked`, the workflow's artifact path and `memlog` name the bound files, worktree-relative or absolute, with no `{...}` placeholder (refused: <key> path is missing or has an unresolved placeholder; <key> path is not the bound workspace path). A create changes both (refused: create must change both the artifact and the memlog). An update changes the memlog when the installed memlog supports append, and its artifact is changed or already existed (refused: update changed neither memlog nor a pre-existing artifact).
- `affected_ids` lists only coordinator assignment ids of this feature (this assignment's `assignment_id` or an assignment id named in the brief), never requirement, finding, question, decision or file ids; leave it `[]` unless your result affects such an assignment (refused: Affected ID must be an assignment in this feature).
