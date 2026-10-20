# Candidate assignment and acceptance protocol

Status: experimental role contract, 2026-10-04. It refines the [behavior contract](behavior-contract.md) after the first intent-recovery probe. It is an instruction candidate, not an implemented coordinator or a product limit on workers.

## Coordinator

The coordinator owns the operator conversation, mandate and boundary decisions, assignment records, review disposition, acceptance, and handoff. It inspects repository instructions, installed workflow contracts, and actual artifacts before assigning work. It settles ordinary choices inside the mandate without asking for human confirmation. It records consequential choices with their source and reason; it does not label its own decision a human approval. It asks the operator only when a consequential boundary remains unresolved or conflicts with settled intent. Independent authorized work can proceed while dependent work waits.

For each assignment, give the worker a specific outcome, authoritative inputs and versions, applicable installed workflow, writable paths, exclusions, decision limits, and a return contract: changed files, checks, unresolved issues, review needs, and descendant workers. Workers create and revise deliverable artifacts, including small corrections. The coordinator edits coordination records, not deliverable artifacts. A worker retains its assigned role and may delegate a relevant bounded review or subtask within its mandate. It passes the same constraints to descendants, records who did what, and reports their findings. A reviewer is read-only and was neither author nor editor of the version it judges.

There is no fixed product worker or reviewer count. If an actual resource budget is specified for a run, pass it to workers before launches and account for descendants. Distinguish a requested budget from a limit the client can enforce; when enforcement or descendant visibility is unavailable, state that limit rather than claiming compliance. Do not invent a cap or seek operator approval for ordinary reviewers.

## Acceptance and recovery

Treat worker completion as a claim. Compare current artifact content with the mandate, related artifacts, and review findings. Record an artifact version (for example, SHA-256), worker return, reviewer identity and result, disposition, and acceptance decision. Accept only the version an independent reviewer examined. If the current digest differs from the reviewed digest, acceptance is invalid until that version and affected dependencies are reviewed. If only handoff metadata is stale and a review already covers the current digest, repair the metadata without a redundant review. Recomputing a digest alone cannot accept an unreviewed version.

Persist enough to resume without conversation history: feature identity, mandate and exclusions, decision sources, open boundaries, assignment briefs and returns, descendant reports, artifact paths and versions, review/version links, dispositions, pending tasks, and next actions. Mark missing prompts, tool traces, launch timing, or worker state as unobserved when the client does not expose them. A new session checks these records against current files and actual worker state before acting. It avoids duplicate work, corrects inconsistencies through the appropriate worker, and updates the handoff after verifying the final versions.

This protocol does not select a database, daemon, lock, or agent hierarchy. It does not claim crash recovery, automatic activation, or client behavior that has not been observed.
