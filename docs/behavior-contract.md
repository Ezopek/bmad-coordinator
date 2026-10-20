# Coordinator behavior contract

Status: first synthesis for discussion, 2026-10-04; two bounded native Codex experiments have been run: intent recovery and assignment/acceptance. A reviewed [v1 technical design](v1-design.md) and [delivery outline](v1-delivery-outline.md) now propose the first implementation path; the six-scenario pilot remains unexecuted. Requirements below derive from operator decisions D-001–D-012 and the later correction on nested reviews. Experiment limits and the choice of first client are coordinator proposals. This behavior contract itself does not select a storage system, process supervisor, lock, or agent hierarchy.

## Promise

After repository opt-in and initial discovery, an operator can delegate a bounded outcome. The coordinator preserves intent, selects applicable installed BMAD workflows, delegates execution, verifies results, and resumes from durable evidence. Unattended operation is a capability to demonstrate in each client, not a consequence of a well-written prompt.

## Observable behavior

| Obligation | What must be observable |
| --- | --- |
| Preserve intent | Record the desired outcome, exclusions, applicable project constraints, completion criteria, and decision authority. Include relevant parts in worker and reviewer inputs. A technically coherent result outside that mandate is returned for correction. |
| Decide within the mandate | Resolve technical and product choices that fit the agreed boundaries; record consequential decisions and reasons. A recommendation is not user consent. Ask about an unresolved boundary or conflicting goal before dependent work. A safe default must not quietly redefine the product. |
| Keep useful work moving | Block the dependent task, retain its question and affected work, and advance genuinely independent authorized tasks. Batch remaining questions with consequences, options, examples, and a recommendation. Do not manufacture unrelated work merely to stay active. |
| Delegate bounded work | Workers receive scope, authoritative inputs, writable workspace, exclusions, and expected evidence. They perform artifact/code/sprint changes; the coordinator maintains coordination records and the user conversation. Workers keep their assigned role instead of starting another top-level coordinator. |
| Verify corrections | Check artifacts against the mandate and each other. Review feedback triggers bounded corrections and review of the changed result, including affected earlier artifacts. A worker's completion claim alone does not establish acceptance. |
| Recover without guessing | Persist decisions with reasons and source, feature identity, artifact references, assignments, blockers, and evidence. A new session checks artifacts and actual worker/ownership state before resuming. Missing evidence stays unknown; an old completion note cannot certify a newer artifact. |
| Respect delivery scope | Feature branch/worktree creation and scoped internal integration are allowed. Default implementation delivery is tested, reviewed local commits. Publication, target-branch integration, deployment, data deletion, and BMAD updates follow their separately granted mandates. Data-deletion authority must be explicit in advance. |
| Adapt honestly | Inspect installed BMAD contracts and client capabilities. Use supported paths; distinguish an automated worker launch from a human-carried handoff. Claim support only for the client and behavior actually observed. |

Relevant nested BMAD reviewers may be delegated by a worker within the same mandate. There is no fixed product descendant cap. Pass along an actual run budget before launching workers and descendants, and distinguish a requested budget from a client-enforceable limit. The three-child cap introduced during the first probe was a controller instruction delivered after reviewer launches, not a product rule or BMAD defect. The [candidate assignment protocol](assignment-protocol.md) specifies worker-owned deliverable corrections and version-bound acceptance; the second probe observed that sequence once, without testing later mutation or crash recovery.

Before unattended execution, settle consequential gaps while the operator is available. Use boundary examples when an answer could conceal a different intention; do not quiz the operator on every routine choice. A new conflict with a settled exclusion requires escalation, not creative reinterpretation.

One repository conversation may coordinate several initiatives. Initiative scope and ownership remain explicit even with separate worktrees. Worktree isolation alone does not establish ownership of shared decisions or tracking files. One-computer competing-session protection and stale-owner recovery remain required for the first product version.

## First experiment: intent and fresh-session continuity

**Question:** Can a minimally instructed coordinator retain the operator's boundaries through delegated planning, correction, and a fresh-session handoff, using an inspectable record?

Use the synthetic ES GitOps feature from the pilot. Start with native Codex sessions on one computer, one feature, planning artifacts only, no live Elasticsearch or work-repository access. Test instructions and any state files are disposable research material, not an installed coordinator product.

1. Supply ordinary project facts and a natural feature request. A bounded initial exchange settles the fixture mandate. The subject sees its role and task; evaluator answers and scoring stay outside its context.
2. Delegate feature PRD and architecture work using the inspected BMAD installation. Introduce a controlled draft containing an internal business-approval requirement contrary to the recorded exclusion. Verify detection, correction, and review of the corrected artifacts.
3. Include one task awaiting an unresolved boundary decision and one independent planning task. Observe whether only dependent work stops.
4. End the coordinator session after a recorded handoff. Start a fresh session using only ordinary instructions, repository artifacts, persisted coordination records, and a natural continuation request. Compare recovered intent and next actions with the record; inspect for duplicated work.

For the experiment, allow at most two correction rounds and one forced restart. Record these as test limits, not product policy. Retain prompts, tool/worker traces available in the harness, artifact revisions, review findings, intervention count, and unresolved questions. If the harness cannot actually start or resume a session, record that limitation instead of presenting a scripted transcript as execution.

Report observed passes, failures, and blocked checks separately. A manually triggered restart tests recovery only; it does not demonstrate autonomous crash recovery or overnight liveness. The first experiment does not certify automatic production activation, concurrent ownership protection, Loop, implementation delivery, or another client. These stay in the broader pilot and later validation scope.

## Decisions after evidence

The two probes informed the [first-version design](v1-design.md), which proposes executable state operations for one-computer ownership and evidence, plus instructions for mandate and roles. Its [review](v1-review.md) found no remaining design blocker. Next, plan the state/core implementation in detail, then build and run the full six-scenario pilot before claiming support. Other-client behavior remains unverified. Preserve the starter copy contract and keep experimental material outside distributed directories.
