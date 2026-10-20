from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


class DomainError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class OwnerRef:
    token: str
    generation: int


@dataclass(frozen=True)
class FeatureInput:
    outcome: str
    exclusions: Any
    constraints: Any
    completion: Any
    authority: Any
    branch: str
    base_ref: str
    base_commit: str
    worktree: str
    dirty_paths: list[str]
    writable_scopes: list[str]


@dataclass(frozen=True)
class DecisionInput:
    feature_id: str
    mandate_revision: int
    source_kind: str
    source_ref: str
    reason: str
    choice: Any
    request_id: str | None = None


@dataclass(frozen=True)
class RecoveryReport:
    state: str
    generation: int
    session_id: str | None
    assignment_states: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class AssignmentInput:
    feature_id: str
    idempotency_key: str
    brief_ref: str
    mandate_revision: int
    source_digests: list[dict[str, str]]
    contract_files: list[dict[str, str]]
    contract_digest: str
    writable_paths: list[str]
    permission_mode: str
    author_id: str
    depends_on: list[str]
    blocking_requests: list[str]


@dataclass(frozen=True)
class BindingEvidence:
    profile: str
    ref: str    # "bindings/<64hex>.json" under <git-common-dir>/coordinator
    payload_digest: str


@dataclass(frozen=True)
class LaunchEvidence:
    source_id: str
    event_ref: str | None
    observed_at: str
    worker_ref: str | None
    payload_digest: str


@dataclass(frozen=True)
class ReturnEvidence:
    source_id: str
    event_ref: str | None
    changed_paths: list[dict[str, str]]
    checks: list[str]
    descendants: list[str]
    result: str
    unresolved_question: str | None
    options: list[str]
    affected_ids: list[str]
    payload_digest: str
    client_stop: dict | None = None   # design D25: the client stop of a `client-stopped` story attempt


@dataclass(frozen=True)
class BundleInput:
    feature_id: str
    artifact_paths: list[str]
    dependency_paths: list[str]
    source_assignment_ids: list[str]
    contract_files: list[dict[str, str]]
    commit: dict | None = None   # implementation drive D13: {sha, parent, assignment_id, diff_sha256}


@dataclass(frozen=True)
class ReviewEvidence:
    bundle_id: str
    reviewer_id: str
    source_id: str
    source_ref: str | None
    finding: Any
    critical_open: bool


@dataclass(frozen=True)
class VerificationReport:
    historical_accepted: bool
    current: Literal["current", "stale", "unknown"]
    checked: list[dict[str, Any]] = field(default_factory=list)


class EvidenceSource(Protocol):
    def classify_launch(self, event: LaunchEvidence) -> Literal["reported", "observed"]: ...
    def classify_return(self, event: ReturnEvidence) -> Literal["reported", "observed"]: ...
    def classify_review(self, event: ReviewEvidence) -> Literal["reported", "observed"]: ...


@dataclass(frozen=True)
class RetryReport:
    assignment_id: str
    state: str
    evidence_needs: list[str]
