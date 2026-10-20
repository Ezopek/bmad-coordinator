# Independent review of the v1 coordinator design

Reviewer: native agent `/root/design_review`, separate from document author `/root/design_author`. This file summarizes the returned findings and their disposition; it is not a full tool trace.

**Status:** design review, 2026-10-04. The independent reviewer found **no remaining design blocker after the revisions below**. This is a review of the [technical proposal](v1-design.md) and [delivery outline](v1-delivery-outline.md), not evidence that the coordinator works or that the operator approved a detailed specification. The reviewer did not run the six-scenario pilot.

| Reviewer finding | Disposition in the design |
| --- | --- |
| A coordinator can pass an epoch check, pause, and launch a worker after takeover. Database fencing alone cannot prevent that external launch or late file writes. | Handoff/recovery requires the old launcher to terminate or relinquish, pending launches to be reconciled, and affected writers to be quiescent or actually isolated before new writes. An unprovable case blocks the ownership scenario. |
| Local database and portable files risk becoming competing editable truth; a crash can leave an incomplete or stale export. | SQLite is canonical for local coordinator decisions and operations. A versioned JSON checkpoint and Markdown view derive from one revision; atomic replacement, readback, export-pending state, and validated import define recovery. Imported unfinished work remains unknown. |
| Review of moving worktree files cannot reliably bind a finding to exact content and dependencies. | The reviewer reads a verified content-addressed bundle with a digest manifest and mandate/contract versions. Acceptance records that immutable bundle; current files are checked separately at acceptance and handoff. |
| Worker launch plus persistence does not establish overnight coordination if the main conversation sleeps or exits. | The later foreground driver must run or resume bounded coordinator decision turns under transferred ownership, reconcile uncertain work, honor permissions, and return explicit `continue`, `needs-human`, `done`, or `failed` outcomes. It is the next gate after the first planning pilot. |

**Evidence still required:** actual old-writer stop/isolation, interrupted and permission-denied Codex CLI behavior, ordinary opt-in activation, all six pilot scenarios in the named client path, and prolonged driver liveness/recovery. Those are tests to perform, not design issues resolved by wording. The [delivery outline](v1-delivery-outline.md) puts state/core implementation planning first, then build and evidence collection. No code, installer, or completed pilot exists yet.

**Subsequent plan refinement:** The later state/core implementation plan received its own independent review. It specifies immutable whole-directory checkpoint publication with a fenced SQLite pointer, refining this review's earlier atomic-replacement wording. The original design reviewer did not review that later mechanism; no implementation or pilot evidence is implied.
