# Choosing models per adapter and role

Status: guidance distilled on **2026-10-10** from the retained live evidence (80 client launches across the driver live hooks 1–9, the attended Claude smoke attempts 1–8, Copilot smoke 6 and Codex smoke 1, all on the csv2json planning fixture). Every number below comes from a receipt or a report in this repository; a cell is empty where nothing was measured. Model identifiers age faster than the rest of this page: §5 is dated, and a `doctor` run plus one receipt (`launch.json`, the result record) is the way to re-verify a name before trusting it.

## 1. Where the model is set and how each client receives it

- **Unattended:** `roles.<role>.{client, model, timeout}` in the sealed pipeline (`drive start`), one triple per role: `worker`, `reviewer`, `judgment`. **Attended:** `bmad run ID --model M --timeout S [--client C]` and `worker run …`.
- **`claude-code`** gets `--model M`. The aliases `sonnet` and `opus` are resolved by the CLI; the result record names the model that actually ran (`claude-sonnet-5-5`, `claude-opus-5-5` in every retained run, Claude Code 2.1.289–2.1.295).
- **`codex-cli`** gets `-m M`; the operator's `~/.codex/config.toml` default (`gpt-6-astra`, `model_reasoning_effort = "high"`) applies only to what the pipeline leaves unset, which is the reasoning effort.
- **`copilot`** gets `--model M` with Copilot's own labels (`claude-sonnet-5` in every retained run); routing is plan-dependent and each launch is one premium request.
- The client, not the model, decides the mode: `claude-code` and `codex-cli` have a write-free read-only mode (reviews, judgments); `copilot` is an author, and a judge only in its read mode inside the launch boundary (`"mode": "read"`, `"boundary": "bwrap"` on the `judgment` role). Its read mode is qualified on 1.0.102 for the judgment and for spec and code reviews (one live sample each; a first review answer the coordinator refuses gets one repair round), and on 1.0.91 for the judgment. A role whose profile is not qualified on the installed version is refused at the start (`UNSUPPORTED_CLIENT_MODE`), and a planning review in read mode always (`UNSUPPORTED_STEP`).

## 2. What each role needs from a model

| Role | Mode | What matters | What does not |
| --- | --- | --- | --- |
| `worker` (author) | workspace-write, deny rules | following the BMAD skill, writing the artifact and memlog, raising gaps as `open_questions` instead of inventing, returning a valid structured result | eloquence; speed beyond the timeout |
| `reviewer` | read-only, one read of the sealed bundle | finding the real defects with a defensible severity (`critical` blocks acceptance; `high` blocks unless `accept_on_high`), reading the bundle it was bound to | agreeing with the author |
| `judgment` | isolated read-only, structured output (schema v2) | answering only what a mandate clause covers, citing the clause, refusing the rest; cheap, short | reasoning depth |

## 3. Measured per client, model and role

Retained launches by `(client, model, mode)`; cost is the client-reported `total_cost_usd` (Claude only); Codex reports tokens, Copilot reports premium requests. Wall time is the launch-to-end receipt time.

| Client / model | Role (mode) | Launches | Cost or usage | Turns or commands | Wall time | Timeouts, errors |
| --- | --- | --- | --- | --- | --- | --- |
| `claude-code` / `sonnet` (`claude-sonnet-5-5`) | worker (workspace-write) | 33 | USD 0.09 / 0.19 / 0.23 (min / median / max) | 9 / 15 / 20 turns | 15 / 41 / 89 s | 0 timeouts; one `429` "session limit" (the account window, run 7) |
| `claude-code` / `opus` (`claude-opus-5-5`) | reviewer (read-only) | 22 | USD 0.35 / 0.45 / 0.87 | 9 / 11 / 19 turns | 74 / 102 / 173 s | none |
| `claude-code` / `sonnet` | judgment (isolated) | 5 | USD 0.02 / 0.06 / 0.09 | 5 / 6 / 9 turns | 7 / 12 / 30 s | none |
| `codex-cli` / `gpt-6-astra` | worker (workspace-write) | 11 | 562 k – 1 611 k input tokens (91–96 % cached), 6.5 k – 14 k output | 9–25 commands | about 3–7 min | 0 timeouts (1800 s in run 9, 900 s in smoke 1); every exit 0 |
| `codex-cli` / `gpt-6-astra` | reviewer (read-only) | 1 | 366 k input, 3.2 k output | 3 commands | 1 min 43 s | none |
| `codex-cli` / `gpt-6-astra` | judgment (isolated) | 2 | 56 k – 67 k input, about 1 k output | 2 commands | | none; both returned a valid v2 answer |
| `copilot` / `claude-sonnet-5` | worker (workspace-write) | 6 | 1 premium request each (6 of about 1 500) | 18–25 tool calls | | 0 timeouts; every exit 0; 2–5 advisory denials per run, none harmful |

## 4. Behaviour seen per model, by role

- **`sonnet` as author** is fast and cheap and asks many questions at once: 1, 3, 4 and 5 questions in one request from the same inputs across runs. One request with any question outside the delegated clauses is refused whole (judgment v2 answers clause by clause, but a refusal is final), so a sonnet author needs either a mandate that covers its usual questions or `max_judgments` headroom. It accepted driver-prepared corrections in place (run 7).
- **`opus` as reviewer** returns 13–20 findings per review. Of the 15 reviews recorded by the drives: 5 rated a `critical`, 12 rated at least one `high`, 2 rated neither (both after a second Codex correction, run 9). The same decimal-grammar defect was `critical` in runs 4 and 7 and unrated in runs 6 and 8: severity is a model claim, so `accept_on_high` is the practical lever, not the model. Cost per review rises with bundle size (USD 0.87 for the run 9 architecture).
- **`gpt-6-astra` as author** (Codex, reasoning high) asks fewer questions per request (3 then 1 in smoke 1; 4, 1 and 1 in run 9) and, in both corrections of run 9, raised a question instead of silently changing a rule the review disputed. It takes 3–7 minutes per launch; the 900 s timeout held in smoke 1, 1800 s was used in run 9. The first unattended acceptance of its work needed two corrections; the second cleared a `critical` and a `high`.
- **`gpt-6-astra` as reviewer**: one sample (smoke 1, S4): 1 medium finding where opus returned 13–20 on comparable bundles. One sample is not a ranking; a Codex reviewer of Claude-authored work has not run.
- **`gpt-6-astra` as judgment**: two valid v2 answers (runs 5 and 9), each citing its clause; usage well under a tenth of an author run.
- **`claude-sonnet-5` under Copilot** as author: 6 of 6 launches completed, asked one question twice (both outside the delegated clauses) and once raised a valid conflict against a coordinator decision that contradicted the accepted PRD. No read-only mode, so never a reviewer or judgment.
- **Judgment refusals** were about mandate coverage every time (a question outside every clause), never about the model: sonnet refused in runs 2, 3 and 7 and in Copilot smoke 6; Codex never faced an uncovered request.

## 5. Defaults and fallbacks (dated 2026-10-10)

| Role | First choice | Fallback | Do not use | Timeout seen to hold |
| --- | --- | --- | --- | --- |
| `worker` | `claude-code` / `sonnet` | `codex-cli` / `gpt-6-astra` (slower, asks less, fewer premium or cost surprises) | `copilot` only when a premium budget is set aside (author-only) | 900 s (Claude, Copilot); 1800 s (Codex) |
| `reviewer` | `claude-code` / `opus` | `codex-cli` / `gpt-6-astra` read-only (one sample, lighter findings) | `copilot` / `claude-sonnet-5` in read mode (spec and code reviews, one live sample each, the seeded defect found); `sonnet` as reviewer is unmeasured | 900 s |
| `judgment` | `claude-code` / `sonnet` | `codex-cli` / `gpt-6-astra` | `copilot` | 600 s |

- **Vendor independence.** Put the author and the reviewer on different vendors when it costs nothing: the proven pair is a Codex author with an opus reviewer (run 9). Copilot's `claude-sonnet-5` with an opus reviewer is single-vendor despite the two clients.
- **Budgets that fitted the fixture:** `max_updates 2`; `max_corrections 2` (run 9 used both; `1` stopped run 7 on remaining highs); `max_judgments` at least the number of author steps, because the budget counts every judgment launch of the drive (run 9 stopped twice with `max_judgments 1`); `--max-turns 50`.
- **Cost of one slice** (PRD, architecture, export, all roles on Claude): USD 1.50–1.76 without a correction (runs 6 and 8), USD 2.40–2.71 with corrections (Copilot smoke 6 with 6 premium requests; run 9 with 8 Codex launches).
- **Identifier aging.** `sonnet` → `claude-sonnet-5-5` and `opus` → `claude-opus-5-5` as resolved by Claude Code 2.1.289–2.1.295 between 2026-10-05 and 2026-10-09; `gpt-6-astra` is the operator's Codex default on codex-cli 0.161–0.162; `claude-sonnet-5` is Copilot CLI 1.0.91's label. When a name stops resolving, `doctor --client C` reports readiness and the first launch's `end.json` and result record show what ran.

## 6. Not covered by the evidence

- `opus` as author, `sonnet` as reviewer, any Codex model other than `gpt-6-astra`, any Copilot model other than `claude-sonnet-5`, a Codex reviewer of Claude-authored work, Codex cost in money (tokens only), overnight operation, fixtures other than csv2json.
- Reasoning-effort settings were never varied (Codex `high` throughout; Claude and Copilot defaults).

## Sources

Unattended driver report and its live-hook report (runs 1–3); judgment v2 (runs 4–5); correction round and its live-hook report (runs 6–9); client adapters with Copilot smoke 6 and Codex smoke 1. The aggregate in §3 was computed read-only over the retained receipts of those runs (`launch.json`, `events.jsonl`, `end.json`).
