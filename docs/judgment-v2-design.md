# Judgment schema v2: a clause per answer

Status: implemented 2026-10-07 with fake clients (plan 2026-10-07-judgment-v2); §6 live verification run 2026-10-07 on `e89d84c`: a four-question request was answered under `LZ1`–`LZ4` with one decision `LZ1+LZ2+LZ3+LZ4` by a Claude Code judgment (run 4, then update, bundle and a bound opus review; stopped by design at `needs-human/critical-finding`) and by a Codex judgment (run 5, capped after the decision); an unattended `done` and the kill-and-restart variant remain unproven live. Extends the [unattended driver design](unattended-driver-design.md) §7 and resolves its §12 item 11. Nothing here changes a numbered decision of that design; the driver's states, bounds and inbox rules stay as they are.

## 1. Problem

`coordinator-judgment-v1` carries one `clause` for the whole request. When a worker puts several questions into one decision request, the judgment can cite only one mandate clause, so it must refuse whenever one clause does not authorize every answer. The live hook (runs 2 and 3) showed this with four delegated clauses `LZ1`–`LZ4` that covered three of four questions: the judgment mapped each question to its clause and still had to refuse. Splitting the authority into clauses cannot help under v1, and `max_judgments` cannot either, because an observed refusal is final.

## 2. Goal and non-goals

**Goal.** A judgment answers a multi-question request when every question is authorized by some delegated clause, citing for each answer the clause that authorizes it, with the same verbatim-quote rule as today. A request with any uncovered question is still refused as a whole.

**Non-goals.** Partial decisions (answering some questions and leaving others open) are not introduced; the slice-3 request → decision → update chain stays one decision per request. The driver does not ask the author to re-raise a narrower request. Worker-steered judgment (driver design §11) is unchanged. Client plugin and skill loading (adapter design §13 #22) is unrelated.

## 3. Decisions

**D1. Schema `coordinator-judgment-v2`.** The return object has exactly these keys, all required:

```json
{"schema": "coordinator-judgment-v2", "judgment_key": "drv-…-j1", "request_id": "…",
 "verdict": "answer" | "refuse",
 "answers": [{"index": 1, "text": "…", "clause": {"authority_id": "LZ1", "field": "authority",
                                                   "pointer": "/delegated/0/text", "quote": "…"}}],
 "rationale": "…", "refusal": "…"}
```

`schema` is a constant literal (`enum` of one value) so the payload names its own rules. The top-level `clause` of v1 is gone; each answer carries its own `clause` with the v1 members `authority_id`, `field`, `pointer`, `quote`. `additionalProperties` is `false` at every level. On `refuse`, `answers` is empty and `refusal` is non-empty; on `answer`, `refusal` is empty.

**D2. Semantic checks (per answer).** `check` keeps its contract (never raises; any defect gives `valid: false`, which the driver treats as a refusal) and applies the v1 clause rules to every answer's clause:

- `authority_id` is the `id` of an entry `i` of `authority.delegated` at the bound revision, with the mandate unchanged since the request was sealed;
- `field` is `authority` and `pointer` is exactly `/delegated/<i>/text` for that same entry;
- `quote` is a verbatim substring of that entry's text, at least 24 characters, or the whole text when shorter;
- `text` is non-empty.

The index rules are unchanged: every question index answered exactly once, no unknown index, no duplicate. One clause may authorize several answers; two answers may cite different clauses. Defect codes keep their v1 names, each suffixed with the answer index it was found on (`unknown-authority-id@2`), so a report names the failing answer. The `rationale` is free text and is not checked.

**D3. Dispatch by sealed schema, no migration.** The request record sealed at `prepare` gains `schema: "coordinator-judgment-v2"`. `check` selects its rules from the request record: a request without a `schema` key is v1 and is checked by the v1 rules, unchanged; a v2 request requires a payload whose `schema` equals the request's, otherwise the defect is `schema-mismatch`. Sealed v1 verdicts, their anchors and their decisions stay valid and replay as before; nothing is rewritten. New judgments are offered only v2 (`--json-schema` for Claude Code, the brief's return section for every client).

**D4. Recording.** A valid v2 answer becomes one `coordinator` decision as today, with:

- `source_ref` = `mandate:r<rev>:<id1>+<id2>+…:authority;receipt=<receipt_ref>#<sha256>`, where the ids are the distinct cited `authority_id`s in ascending order (a single-clause v2 answer therefore reads `mandate:r1:LZ1:authority;…`, which differs from v1's `…:LZ1:authority/delegated/0/text` only by omitting the pointer, since the pointers now live per answer);
- `choice.answers` = the v2 answers verbatim, clause included;
- `choice.clauses` = the distinct cited clauses in `source_ref` order, each `{authority_id, field, pointer}` (no quotes, which stay with their answers);
- `choice.judgment` unchanged (`{key, receipt_ref, receipt_sha256, quality: "model-claim"}`);
- `reason` unchanged.

`choice.clause` (singular) is not written for v2. Readers of decisions (`summary.json`, `drive status`, the attended listing) show `clauses` for v2 and `clause` for v1; neither is rewritten.

**D5. Brief.** The judgment brief's rule section reads: answer only when every numbered question is authorized by a delegated clause; cite, for each answer, the one clause that authorizes it; if any question has no authorizing clause, refuse the whole request and name in `refusal` which indexes are covered and which are not. The return section describes v2. The untrusted-data marking, the read-only rule and the "never operator approval" sentence stay.

**D6. Driver.** No new state, reason code or bound. `max_judgments` counts launches as before. `needs-human/question-refused` keeps its routing; the refusal text now carries the covered and uncovered indexes, which `drive status` prints.

**D7. Model claim.** Unchanged in substance and restated: the code proves that a distinct process returned an anchored, structured verdict whose every answer cites an existing authority entry and mandate text. It does not prove that a clause authorizes its answer, that the answer is right, or that the question-to-clause mapping is the only reasonable one.

## 4. Changes by file

- `coordinator_core/judgment.py`: `SCHEMA_NAME_V2`, `SCHEMA_V2`; `prepare` seals `schema`; `run` offers the request's schema; `check` dispatches on the request's `schema` (v1 path byte-for-byte as today); `record` builds the v2 `source_ref` and `choice`; `_brief` v2 text; `facts`/`verdict` expose `schema` and the per-answer clauses.
- `coordinator_core/drive.py` / `drive_pipeline.py`: only presentation (`clauses` vs `clause`, refusal text in status). No state change.
- `docs/unattended-driver-design.md`: §7 return-schema paragraph points here; §12 item 11 marked resolved by this design.
- `docs/state-core-usage.md`: the judgment paragraph shows a v2 verdict and the new `source_ref`.
- `tests/test_drive_judgment.py`, `tests/test_drive_e2e.py`, `tests/fake_clients.py`: see §5.

## 5. Tests

Unit (`check`, v2 request): three questions answered under two clauses, valid; one clause for all three, valid; an answer whose clause cites another entry's pointer, `bad-pointer@<i>`; a short quote on one answer only, `short-quote@<i>`; a missing index, `missing-index`; a refusal with empty answers, `valid: true` and verdict `refuse`; a v1-shaped payload on a v2 request, `schema`; a v2 payload whose `schema` literal differs, `schema-mismatch`.

Compatibility: a sealed v1 request plus its v1 verdict from the existing fixtures still checks valid with the v1 rules and records the v1 `source_ref` and `choice.clause` unchanged (pinned literals).

Recording: `source_ref` and `choice.clauses` for a two-clause answer, pinned literals.

End to end (fake clients, production registry entries): `drive start` with a fake author that raises three questions in one request and a fake judgment that answers them under `LZ1`+`LZ2`, reaching `done`; the same with one uncovered question, ending `needs-human/question-refused` with the covered and uncovered indexes in `drive status`; kill-and-restart on the judgment turn resumes without a second launch (existing test shape).

Gate: the full suite with zero failures, errors, skips and warnings, as for every round.

## 6. Live verification

Repeat the driver live hook (driver design §10) on the committed code with clauses `LZ1`–`LZ4` and `accept_on_high: true`, aiming for `done`; then the kill-and-restart variant if the first run ends `done` and the account window allows. A refusal caused by a question outside every clause remains a correct stop and is reported as such, not retried.

## 7. Residual uncertainty

1. A model may cite a clause that exists but does not authorize that answer; D7 names this and the attended listing shows every decision.
2. Authors vary in how many and which questions they ask from the same inputs (1, 3 and 4 in the live runs); v2 removes the single-clause limit, not that variance.
3. The `schema-mismatch` defect depends on the request record; a request record that cannot be read makes the judgment unknown, as today.
4. A delegated authority id containing `+` would make the v2 `source_ref` authority segment ambiguous (`A1+A2` could be one id or two); `choice.clauses` is the authoritative list, and ids are not refused at bind. Deferred.
