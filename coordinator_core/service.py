"""Owner-fenced coordinator transitions and bounded read-only views."""

import hashlib
import json
import os
import secrets
import stat
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath

from . import clients, profiles
from .model import (AssignmentInput, BindingEvidence, BundleInput, DecisionInput, DomainError, EvidenceSource,
                    FeatureInput, LaunchEvidence, OwnerRef, RecoveryReport, RetryReport, ReturnEvidence,
                    ReviewEvidence, VerificationReport)
from .repo import resolve_repo
from .run_receipt import read_binding, receipt_ref, run_name
from .store import Store

CONTINUATION_UNSUPPORTED = ("same-process continuation unsupported; resolve the request, "
                            "then add a fresh assignment with blocking_requests")
HOLDING_STATES = ("launch-pending", "launch-unknown", "running")
TERMINAL_STATES = ("completed-claim", "needs-decision", "blocked", "client-stopped")   # design D25: client-stopped
CLOSED_STATES = ("completed", "abandoned")   # feature lifecycle design F1
FEATURE_EVENTS = ("pipeline.bound", "bmad.materialized")   # F5: driver events whose subject is a feature
MANDATE_FIELDS = {"outcome", "exclusions", "constraints", "completion", "authority"}
JSON_FIELDS = MANDATE_FIELDS - {"outcome"}
DRIVE_EVENTS = frozenset({"pipeline.bound", "drive.leased", "drive.started", "drive.turn", "drive.stop-acknowledged",
                          "inbox.rejected", "drive.ended", "judgment.reserved", "judgment.returned",
                          "judgment.unknown", "judgment.failed-preflight",
                          "story.setup", "story.baseline", "story.committed", "story.verified", "story.status",
                          "epic.started", "epic.advanced", "epic.ended", "bmad.materialized"})
ANCHOR_TOPS = ("pipelines", "drives", "inbox", "judgments", "story", "epics", "bmad")
DETAIL_TABLES = {
    "feature": "features", "decision": "decisions", "decision-request": "decision_requests",
    "assignment": "assignments", "bundle": "bundles", "review": "reviews",
    "acceptance": "acceptances", "event": "events",
}


DECISION_RECORD = ("id", "feature_id", "mandate_revision", "source_kind", "source_ref", "reason", "choice_json",
                   "request_id")


def decision_digest(row) -> str:
    """Digest of a stored decision row exactly as recorded (choice text as stored, never caller text)."""
    return hashlib.sha256(_json({key: row[key] for key in DECISION_RECORD}).encode()).hexdigest()


def resolving_decisions(snapshot: dict, assignment_ids) -> list[dict]:
    """`{id, digest}` of every decision that resolved a decision request of one of these assignments: the
    continuation chain a bundle seals."""
    rows = {row["id"]: row for row in snapshot["decisions"]}
    ids = sorted({row["resolution_decision_id"] for row in snapshot["decision_requests"]
                  if row["assignment_id"] in assignment_ids and row["state"] == "resolved"
                  and row["resolution_decision_id"]})
    if any(item not in rows for item in ids):
        raise DomainError("CORRUPT_STATE", "A resolved decision request names a missing decision")
    return [{"id": item, "digest": decision_digest(rows[item])} for item in ids]


def bundle_decisions(base: Path, snapshot: dict, assignment_ids) -> list[dict]:
    """Resolving decisions plus the `{id, digest}` decisions a contributor's anchored binding seals (`bmad prepare`
    `decisions`). A bound decision whose row changed since prepare is STALE_INPUT; a missing one or a malformed
    record is UNKNOWN_EVIDENCE."""
    rows = {row["id"]: row for row in snapshot["decisions"]}
    found = {item["id"]: item for item in resolving_decisions(snapshot, assignment_ids)}
    for assignment_id in sorted(assignment_ids):
        _, bound = binding_anchor(snapshot["events"], assignment_id)
        if bound is None:
            continue
        sealed = read_binding(base, bound["evidence_ref"], bound["payload_digest"]).get("decisions", [])
        if not isinstance(sealed, list) or any(not isinstance(item, dict) or set(item) != {"id", "digest"} or
                                               item["id"] not in rows for item in sealed):
            raise DomainError("UNKNOWN_EVIDENCE", "A bound decision is malformed or missing")
        for item in sealed:
            if decision_digest(rows[item["id"]]) != item["digest"]:
                raise DomainError("STALE_INPUT", "A bound decision changed since prepare")
            found[item["id"]] = item
    return [found[key] for key in sorted(found)]


def sealed_decisions_state(manifest: dict, decisions: list[dict]) -> str:
    """`current` when every sealed decision row still exists with its sealed digest; `stale` when one changed;
    `unknown` when one is missing or the record is malformed."""
    rows = {row["id"]: row for row in decisions}
    state = "current"
    for item in manifest.get("decisions", []):
        if not isinstance(item, dict) or item.get("id") not in rows:
            return "unknown"
        if decision_digest(rows[item["id"]]) != item.get("digest"):
            state = "stale"
    return state


def _json(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise DomainError("INVALID_INPUT", "Value must be JSON serializable") from exc


def _event(conn, kind, subject_id, anchor=None):
    """Insert the transaction's single event; anchor is (source_id, evidence_ref, payload_digest)."""
    revision = conn.execute("SELECT revision FROM meta").fetchone()[0] + 1
    if conn.execute("SELECT 1 FROM events WHERE revision=?", (revision,)).fetchone() is not None:
        raise DomainError("CORRUPT_STATE", "Transaction already recorded an event")
    conn.execute("INSERT INTO events(id, revision, kind, subject_id, source_id, evidence_ref, payload_digest) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?)", (str(uuid.uuid4()), revision, kind, subject_id, *(anchor or (None,) * 3)))


def _open_feature(row) -> None:
    """F5: a closed feature refuses every write that starts or records work."""
    if row is not None and row["state"] in CLOSED_STATES:
        raise DomainError("FEATURE_CLOSED",
                          f"Feature is closed ({row['state']}): record a new feature for further work")


def _feature_of(conn, assignment_id: str) -> None:
    """F5, inside an assignment mutation's transaction: the assignment's feature must be open."""
    _open_feature(conn.execute("SELECT f.state FROM assignments a JOIN features f ON f.id=a.feature_id "
                               "WHERE a.id=?", (assignment_id,)).fetchone())


def require_open_feature(snapshot: dict, feature_id: str) -> None:
    """F5, a driver's or command's check before any work: the feature exists and is open."""
    feature = next((row for row in snapshot["features"] if row["id"] == feature_id), None)
    if feature is None:
        raise DomainError("NOT_FOUND", "Feature does not exist")
    _open_feature(feature)


def _observed(return_json) -> bool:
    return bool(return_json) and json.loads(return_json).get("provenance_class") == "observed"


def holds_fence(state: str, return_json) -> bool:
    """The attempt-fence rule: a holding attempt, or a terminal one without an observed anchored return."""
    return state in HOLDING_STATES or (state in TERMINAL_STATES and not _observed(return_json))


def releasable(state: str, return_json) -> bool:
    """D2, per assignment: cancelled before launch, operator resolved-unknown, or terminal with an observed return."""
    return state in ("cancelled-before-launch", "resolved-unknown") or \
        (state in TERMINAL_STATES and _observed(return_json))


def _anchored_bytes(base: Path, ref, digest, tops) -> bytes:
    """Bytes of a sealed file under <common-dir>/coordinator/<top>/...; no symlinked component, a regular file, the
    expected SHA-256. The one reader of anchored driver files: anchor_event, resolve_unknown_attempt and the driver's
    sealed-record and judgment readers."""
    parts = PurePosixPath(ref).parts if isinstance(ref, str) else ()
    if len(parts) < 2 or parts[0] not in tops or str(PurePosixPath(*parts)) != ref or \
       any(part in ("", ".", "..") for part in parts) or not isinstance(digest, str) or len(digest) != 64 or \
       any(c not in "0123456789abcdef" for c in digest):
        raise DomainError("INVALID_INPUT", "Anchor reference or digest is invalid")
    try:
        path = base
        for part in parts[:-1]:
            path = path / part
            if not stat.S_ISDIR(path.lstat().st_mode):
                raise ValueError("anchored file parent is not a directory")
        fd = os.open(path / parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("anchored file is not regular")
            data = stream.read()
    except (OSError, ValueError) as exc:
        raise DomainError("BLOCKED_EVIDENCE", "Anchored file is unavailable") from exc
    if hashlib.sha256(data).hexdigest() != digest:
        raise DomainError("BLOCKED_EVIDENCE", "Anchored file bytes differ from the digest")
    return data


def _anchor_rows(events, assignment_id):
    """Required profile name (prepared event) and the anchored assignment.bound events."""
    mine = [row for row in events if row["subject_id"] == assignment_id]
    required = next((row["source_id"] for row in mine if row["kind"] == "assignment.prepared"), None)
    bound = [row for row in mine if row["kind"] == "assignment.bound"]
    if len(bound) > 1 or any(not (row["source_id"] and row["evidence_ref"] and row["payload_digest"]) for row in bound):
        raise DomainError("UNKNOWN_EVIDENCE", "Binding anchor is missing or ambiguous")
    return required, (bound[0] if bound else None)


def binding_anchor(events, assignment_id):
    """(profile name or None, anchored binding event or None); a required profile is never downgraded."""
    required, bound = _anchor_rows(events, assignment_id)
    if required is not None and bound is None:
        raise DomainError("BLOCKED_EVIDENCE", "Required return profile has no anchored binding")
    if required is not None and bound["source_id"] != required:
        raise DomainError("BLOCKED_EVIDENCE", "Binding profile differs from the required profile")
    return (bound["source_id"], bound) if bound is not None else (None, None)


def _directory_scope(text: str) -> None:
    parts = text[:-1].split("/")
    if not text[:-1] or any(part in ("", ".", "..") for part in parts) or parts[0] in (".git", ".bmad-coordinator") \
       or any(c in text for c in "*?[]\\"):
        raise DomainError("UNSAFE_PATH", "Invalid directory scope")


def check_directory_alias(worktree: Path, scope: str) -> None:
    """Grammar plus real-path identity: no symlink may redirect a directory scope."""
    _directory_scope(scope)
    if _path_identity(worktree, scope[:-1]) != scope[:-1]:
        raise DomainError("UNSAFE_PATH", "Directory scope is aliased by a symlink")


def scope_covers(scope: str, path: str) -> bool:
    """A trailing-slash scope covers paths below it; any other scope is exactly one file."""
    return path.startswith(scope) and path != scope if scope.endswith("/") else path == scope


def _fence_holder(conn, assignment_id: str, worktree: str):
    mine = os.path.realpath(worktree)
    for other in conn.execute("SELECT a.id, a.state, a.return_json, f.worktree FROM assignments a "
                              "JOIN features f ON f.id=a.feature_id WHERE a.id != ? AND f.worktree != ''",
                              (assignment_id,)):
        if os.path.realpath(other["worktree"]) != mine:
            continue
        if other["state"] in HOLDING_STATES or (other["state"] in TERMINAL_STATES and (
                not other["return_json"] or json.loads(other["return_json"])["provenance_class"] != "observed")):
            return other["id"]
    return None


def _safe_token_path(path: Path) -> Path:
    path = Path(path).absolute()
    if ".." in path.parts or not path.name or not path.parent.is_dir():
        raise DomainError("UNSAFE_PATH", "Token path must have an existing directory")
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise DomainError("UNSAFE_PATH", "Token path must not use symlinks")
    return path


def _safe_feature_paths(worktree: Path, paths: list[str]) -> list[str]:
    if not isinstance(paths, list):
        raise DomainError("INVALID_INPUT", "Feature paths must be a list")
    for text in paths:
        if not isinstance(text, str) or not text or Path(text).is_absolute() or ".." in Path(text).parts:
            raise DomainError("UNSAFE_PATH", "Feature path must be relative to its worktree")
        _path_identity(worktree, text)
    return paths


def _path_identity(worktree: Path, path: str) -> str:
    try:
        return str((worktree / path).resolve().relative_to(worktree))
    except (OSError, RuntimeError, ValueError) as exc:
        raise DomainError("UNSAFE_PATH", "Feature path escapes its worktree") from exc


def _resolved_worktree(feature) -> Path:
    if not feature["worktree"] or feature["state"] == "unresolved":
        raise DomainError("UNKNOWN_EVIDENCE", "Feature worktree is unresolved")
    return Path(feature["worktree"])


def _files(worktree: Path, entries: list[dict], *, check_current: bool) -> None:
    if not isinstance(entries, list) or not entries:
        raise DomainError("INVALID_INPUT", "At least one file digest is required")
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"path", "digest"}:
            raise DomainError("INVALID_INPUT", "File digest requires path and digest")
        path, expected = entry["path"], entry["digest"]
        if not isinstance(expected, str) or len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
            raise DomainError("INVALID_INPUT", "File digest must be SHA-256")
        _safe_feature_paths(worktree, [path])
        identity = _path_identity(worktree, path)
        if identity in seen:
            raise DomainError("INVALID_INPUT", "Duplicate file path")
        seen.add(identity)
        if check_current:
            try:
                actual = hashlib.sha256((worktree / path).read_bytes()).hexdigest()
            except OSError as exc:
                raise DomainError("STALE_INPUT", "Referenced file is unavailable") from exc
            if actual != expected:
                raise DomainError("STALE_INPUT", "Referenced file changed")


def _scope_allows(path: str, scopes: list[str]) -> bool:
    return any(fnmatchcase(path, scope) or path == scope or
               (not any(c in scope for c in "*?[]") and Path(path).is_relative_to(Path(scope)))
               for scope in scopes)


def _artifact_contributors(assignments, requests, feature_id: str, worktree: Path, artifact_digests: dict[str, str],
                           mandate_revision: int, contract_files_json: str) -> set[str]:
    contributors = set()
    observed_current = set()
    artifact_by_identity = {_path_identity(worktree, path): digest for path, digest in artifact_digests.items()}
    assignments = [dict(row) for row in assignments]
    requests = [dict(row) for row in requests]

    def current_observed(row, state):
        return row["state"] == state and json.loads(row["return_json"])["provenance_class"] == "observed" and \
            row["mandate_revision"] == mandate_revision and row["contract_files_json"] == contract_files_json

    def continued(row):
        # A partial needs-decision row covers its files only once every one of its requests is resolved
        # and linked as a blocker of an observed completed continuation with the same mandate and contracts.
        mine = [request for request in requests if request["assignment_id"] == row["id"]]
        return bool(mine) and all(request["state"] == "resolved" and any(
            other["feature_id"] == feature_id and other["return_json"] and current_observed(other, "completed-claim")
            and request["id"] in json.loads(other["blocking_requests_json"]) for other in assignments)
            for request in mine)

    for row in assignments:
        if row["feature_id"] != feature_id or not row["return_json"]:
            continue
        result = json.loads(row["return_json"])
        for entry in result["changed_paths"]:
            path = entry.get("canonical_path") or _path_identity(worktree, entry["path"])
            if path not in artifact_by_identity:
                continue
            contributors.add(row["id"])
            if entry["digest"] == artifact_by_identity[path] and (
                    current_observed(row, "completed-claim") or
                    (current_observed(row, "needs-decision") and continued(row))):
                observed_current.add(path)
    if observed_current != set(artifact_by_identity):
        raise DomainError("BLOCKED_EVIDENCE", "Current artifact bytes lack current observed completion evidence")
    return contributors


class _NoChange(Exception):
    pass


class Coordinator:
    def __init__(self, store: Store, evidence_source: EvidenceSource | None = None):
        self.store = store
        self.evidence_source = evidence_source

    def claim(self, session_id: str, token_path: Path) -> OwnerRef:
        if not isinstance(session_id, str) or not session_id.strip():
            raise DomainError("INVALID_INPUT", "Session ID is required")
        path = _safe_token_path(token_path)
        token = secrets.token_hex(32)
        created = None

        def change(conn):
            nonlocal created
            generation, state = conn.execute("SELECT generation, state FROM owner").fetchone()
            if state != "released":
                raise DomainError("OWNERSHIP_CONFLICT", "Owner is active or recovery is uncertain")
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            except FileExistsError as exc:
                raise DomainError("OWNERSHIP_CONFLICT", "Token file already exists") from exc
            except OSError as exc:
                raise DomainError("UNSAFE_PATH", "Cannot create token file") from exc
            try:
                created = os.fstat(fd)
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as file:
                    file.write(_json({"token": token, "generation": generation + 1}))
                    file.flush()
                    os.fsync(file.fileno())
            except OSError as exc:
                raise DomainError("TOKEN_WRITE_FAILED", "Cannot sync token file") from exc
            owner = OwnerRef(token, generation + 1)
            conn.execute("UPDATE owner SET generation=?, token_hash=?, session_id=?, state='active', acquired_at=?",
                         (owner.generation, hashlib.sha256(token.encode()).hexdigest(), session_id,
                          datetime.now(timezone.utc).isoformat()))
            _event(conn, "owner.claimed", str(owner.generation))
            return owner

        try:
            return self.store._claim_unowned(change)
        except BaseException:
            if created is not None:
                try:
                    current = path.lstat()
                    if not path.is_symlink() and (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino):
                        path.unlink()
                except FileNotFoundError:
                    pass
            raise

    def release(self, owner: OwnerRef) -> None:
        """D2: every assignment is cancelled before launch, operator resolved-unknown, or terminal with an observed
        anchored return. An observed return proves only that the lead process ended; descendants stay unobserved."""
        def change(conn):
            if not all(releasable(row["state"], row["return_json"])
                       for row in conn.execute("SELECT state, return_json FROM assignments")):
                raise DomainError("RECOVERY_BLOCKED", "Assignments still require lifecycle evidence")
            conn.execute("UPDATE owner SET token_hash=NULL, session_id=NULL, state='released'")
            _event(conn, "owner.released", str(owner.generation))
        self.store.write(owner, change)

    def anchor_event(self, owner: OwnerRef, kind: str, subject_id: str, anchor: tuple) -> None:
        """Closed-kind driver event anchored to sealed bytes under pipelines/, drives/, inbox/, judgments/, story/ or
        epics/; the bytes are re-hashed inside the owner-fenced transaction, which keeps its single event."""
        if kind not in DRIVE_EVENTS or not isinstance(subject_id, str) or not subject_id or \
           not isinstance(anchor, tuple) or len(anchor) != 3:
            raise DomainError("INVALID_INPUT", "Driver event kind or anchor is not supported")
        source_id, ref, digest = anchor
        if not isinstance(source_id, str) or not source_id:
            raise DomainError("INVALID_INPUT", "Driver event needs a source")
        if kind.startswith("judgment.") and not (isinstance(ref, str) and ref.startswith("judgments/")):
            raise DomainError("INVALID_INPUT", "Judgment events anchor files under judgments/ only")
        base = self.store.path.parent

        def change(conn):
            feature_id = subject_id if kind in FEATURE_EVENTS else source_id if kind == "drive.leased" else None
            if feature_id is not None:
                _open_feature(conn.execute("SELECT state FROM features WHERE id=?", (feature_id,)).fetchone())
            _anchored_bytes(base, ref, digest, ANCHOR_TOPS)
            _event(conn, kind, subject_id, (source_id, ref, digest))
        self.store.write(owner, change)

    def resolve_unknown_attempt(self, owner: OwnerRef, assignment_id: str, evidence_ref: str, evidence_sha256: str,
                                reason: str, source_ref: str) -> str:
        """D15, operator only: a fence holder becomes terminal `resolved-unknown` with an operator-reported decision
        and an event anchored to its sealed evidence, in one transaction. Descendants may still write."""
        if not all(isinstance(value, str) and value.strip() for value in (reason, source_ref)):
            raise DomainError("INVALID_INPUT", "Resolution needs a reason and a source reference")
        if not isinstance(evidence_ref, str) or not evidence_ref.startswith(f"resolutions/{assignment_id}-"):
            raise DomainError("INVALID_INPUT", "Resolution evidence must be sealed under resolutions/")
        base = self.store.path.parent
        decision_id = str(uuid.uuid4())

        def change(conn):
            row = conn.execute("SELECT a.state, a.return_json, a.feature_id, f.mandate_revision FROM assignments a "
                               "JOIN features f ON f.id=a.feature_id WHERE a.id=?", (assignment_id,)).fetchone()
            if row is None:
                raise DomainError("NOT_FOUND", "Assignment does not exist")
            if not holds_fence(row["state"], row["return_json"]):
                raise DomainError("BLOCKED_EVIDENCE", "Only an attempt that holds the fence can be resolved")
            _anchored_bytes(base, evidence_ref, evidence_sha256, ("resolutions",))
            conn.execute("INSERT INTO decisions(id, feature_id, mandate_revision, source_kind, source_ref, reason, "
                         "choice_json, request_id) VALUES (?,?,?,?,?,?,?,NULL)",
                         (decision_id, row["feature_id"], row["mandate_revision"], "operator-reported", source_ref,
                          reason, _json({"resolved_unknown": assignment_id, "evidence_ref": evidence_ref,
                                         "evidence_sha256": evidence_sha256})))
            conn.execute("UPDATE assignments SET state='resolved-unknown' WHERE id=?", (assignment_id,))
            _event(conn, "assignment.resolved-unknown", assignment_id, (decision_id, evidence_ref, evidence_sha256))
        self.store.write(owner, change)
        return decision_id

    def recover_status(self) -> RecoveryReport:
        snapshot = self.store.read_snapshot()
        owner = snapshot["owner"]
        state = "released" if owner["state"] == "released" else "blocked_launcher_unknown"
        assignment_states = {row["id"]: "launch-unknown" if row["state"] == "launch-pending" else row["state"]
                             for row in snapshot["assignments"]}
        return RecoveryReport(state, owner["generation"], owner["session_id"], assignment_states)

    def create_feature(self, owner: OwnerRef, spec: FeatureInput) -> str:
        if not isinstance(spec.outcome, str) or not spec.outcome.strip():
            raise DomainError("INVALID_INPUT", "Feature outcome is required")
        worktree = Path(spec.worktree).resolve()
        identity = resolve_repo(worktree)
        if identity.worktree != worktree or identity.common_dir != self.store.path.parent.parent.resolve():
            raise DomainError("INVALID_REPOSITORY", "Feature worktree must belong to coordinator repository")
        dirty = _safe_feature_paths(worktree, spec.dirty_paths)
        writable = _safe_feature_paths(worktree, spec.writable_scopes)
        feature_id = str(uuid.uuid4())
        values = (feature_id, 1, spec.outcome, _json(spec.exclusions), _json(spec.constraints),
                  _json(spec.completion), _json(spec.authority), spec.branch, spec.base_ref,
                  spec.base_commit, str(worktree), _json(dirty), _json(writable), "active")

        def change(conn):
            conn.execute("INSERT INTO features(id, mandate_revision, outcome, exclusions_json, constraints_json, "
                         "completion_json, authority_json, branch, base_ref, base_commit, worktree, "
                         "dirty_paths_json, writable_paths_json, state) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values)
            _event(conn, "feature.created", feature_id)
        self.store.write(owner, change)
        return feature_id

    def record_decision(self, owner: OwnerRef, spec: DecisionInput) -> str:
        if spec.source_kind not in ("operator-reported", "coordinator") or not isinstance(spec.source_ref, str) or not spec.source_ref.strip():
            raise DomainError("INVALID_INPUT", "Decision requires a source kind and real source reference")
        if not isinstance(spec.reason, str) or not spec.reason.strip():
            raise DomainError("INVALID_INPUT", "Decision reason is required")
        choice = _json(spec.choice)
        decision_id = str(uuid.uuid4())

        def change(conn):
            row = conn.execute("SELECT mandate_revision, state FROM features WHERE id=?", (spec.feature_id,)).fetchone()
            if row is None:
                raise DomainError("NOT_FOUND", "Feature does not exist")
            _open_feature(row)
            if row[0] != spec.mandate_revision:
                raise DomainError("REVISION_CONFLICT", "Decision mandate revision is stale")
            if spec.request_id is not None:
                request = conn.execute("SELECT feature_id, state FROM decision_requests WHERE id=?", (spec.request_id,)).fetchone()
                if request is None or request["feature_id"] != spec.feature_id or request["state"] != "open":
                    raise DomainError("INVALID_INPUT", "Decision request does not match feature")
            conn.execute("INSERT INTO decisions(id, feature_id, mandate_revision, source_kind, source_ref, reason, choice_json, request_id) "
                         "VALUES (?,?,?,?,?,?,?,?)",
                         (decision_id, spec.feature_id, spec.mandate_revision, spec.source_kind,
                          spec.source_ref, spec.reason, choice, spec.request_id))
            if spec.request_id is not None:
                conn.execute("UPDATE decision_requests SET state='resolved', resolution_decision_id=? WHERE id=?",
                             (decision_id, spec.request_id))
            _event(conn, "decision.recorded", decision_id)
        self.store.write(owner, change)
        return decision_id

    def create_assignment(self, owner: OwnerRef, spec: AssignmentInput, required_profile: str | None = None) -> str:
        if required_profile is not None and required_profile not in profiles.MODULES:
            raise DomainError("INVALID_INPUT", "Unknown return profile")
        if not all(isinstance(value, str) and value.strip() for value in
                   (spec.idempotency_key, spec.brief_ref, spec.contract_digest, spec.permission_mode, spec.author_id)):
            raise DomainError("INVALID_INPUT", "Assignment identity and permission are required")
        if not isinstance(spec.depends_on, list) or not isinstance(spec.blocking_requests, list) or \
           any(not isinstance(value, str) or not value for value in spec.depends_on + spec.blocking_requests) or \
           len(set(spec.depends_on)) != len(spec.depends_on) or \
           len(set(spec.blocking_requests)) != len(spec.blocking_requests):
            raise DomainError("INVALID_INPUT", "Dependencies must be distinct record IDs")
        fingerprint = hashlib.sha256(_json(asdict(spec)).encode()).hexdigest()
        assignment_id = str(uuid.uuid4())
        existing_id = None

        def change(conn):
            nonlocal existing_id
            existing = conn.execute("SELECT id, request_fingerprint FROM assignments WHERE idempotency_key=?",
                                    (spec.idempotency_key,)).fetchone()
            if existing is not None:
                prior = conn.execute("SELECT source_id FROM events WHERE kind='assignment.prepared' AND subject_id=?",
                                     (existing["id"],)).fetchone()
                if existing["request_fingerprint"] != fingerprint or (prior[0] if prior else None) != required_profile:
                    raise DomainError("IDEMPOTENCY_CONFLICT", "Idempotency key has a different request")
                existing_id = existing["id"]
                raise _NoChange()
            feature = conn.execute("SELECT * FROM features WHERE id=?", (spec.feature_id,)).fetchone()
            if feature is None:
                raise DomainError("NOT_FOUND", "Feature does not exist")
            _open_feature(feature)
            if feature["mandate_revision"] != spec.mandate_revision:
                raise DomainError("STALE_INPUT", "Mandate revision changed")
            worktree = _resolved_worktree(feature)
            _files(worktree, spec.source_digests, check_current=True)
            if spec.contract_files:   # implementation drive D22: a story seals no contract files
                _files(worktree, spec.contract_files, check_current=True)
            if spec.brief_ref not in {entry["path"] for entry in spec.source_digests}:
                raise DomainError("INVALID_INPUT", "Brief must be included in source digests")
            if hashlib.sha256(_json(spec.contract_files).encode()).hexdigest() != spec.contract_digest:
                raise DomainError("INVALID_INPUT", "Contract aggregate digest does not match files")
            paths = _safe_feature_paths(worktree, spec.writable_paths)
            for path in paths:
                if path.endswith("/"):
                    check_directory_alias(worktree, path)
            scopes = json.loads(feature["writable_paths_json"])
            if (spec.permission_mode == "read-only" and paths) or \
               (spec.permission_mode != "read-only" and not paths) or \
               any(not _scope_allows(path, scopes) for path in paths):
                raise DomainError("UNSAFE_PATH", "Assignment path is outside writable feature scopes")
            for dependency in spec.depends_on:
                row = conn.execute("SELECT feature_id FROM assignments WHERE id=?", (dependency,)).fetchone()
                if row is None or row["feature_id"] != spec.feature_id:
                    raise DomainError("INVALID_INPUT", "Prerequisite must be an existing assignment in this feature")
            for request_id in spec.blocking_requests:
                row = conn.execute("SELECT feature_id FROM decision_requests WHERE id=?", (request_id,)).fetchone()
                if row is None or row["feature_id"] != spec.feature_id:
                    raise DomainError("INVALID_INPUT", "Blocker must be a decision request in this feature")
            conn.execute("INSERT INTO assignments(id, feature_id, owner_generation, idempotency_key, request_fingerprint, "
                         "brief_ref, mandate_revision, source_digests_json, contract_files_json, contract_digest, "
                         "paths_json, permission_mode, author_id, depends_on_json, blocking_requests_json, state) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'prepared')",
                         (assignment_id, spec.feature_id, owner.generation, spec.idempotency_key, fingerprint,
                          spec.brief_ref, spec.mandate_revision, _json(spec.source_digests), _json(spec.contract_files),
                          spec.contract_digest, _json(paths), spec.permission_mode, spec.author_id,
                          _json(spec.depends_on), _json(spec.blocking_requests)))
            _event(conn, "assignment.prepared", assignment_id, (required_profile, None, None))
        try:
            self.store.write(owner, change)
        except _NoChange:
            return existing_id
        return assignment_id

    def bind_assignment(self, owner: OwnerRef, assignment_id: str, evidence: BindingEvidence) -> None:
        digest = evidence.payload_digest
        if evidence.profile not in profiles.MODULES or not isinstance(digest, str) or len(digest) != 64 or \
           evidence.ref != f"bindings/{digest}.json":
            raise DomainError("INVALID_INPUT", "Binding evidence requires a known profile and content-addressed ref")
        anchor = (evidence.profile, evidence.ref, digest)

        def change(conn):
            row = conn.execute("SELECT state, request_fingerprint FROM assignments WHERE id=?", (assignment_id,)).fetchone()
            if row is None:
                raise DomainError("NOT_FOUND", "Assignment does not exist")
            required, bound = _anchor_rows(conn.execute(
                "SELECT kind, subject_id, source_id, evidence_ref, payload_digest FROM events WHERE subject_id=?",
                (assignment_id,)), assignment_id)
            if required is not None and required != evidence.profile:
                raise DomainError("BLOCKED_EVIDENCE", "Binding profile differs from the required profile")
            if bound is not None:
                if (bound["source_id"], bound["evidence_ref"], bound["payload_digest"]) == anchor:
                    raise _NoChange()
                raise DomainError("BLOCKED_EVIDENCE", "Assignment already has a different binding")
            _feature_of(conn, assignment_id)
            if row["state"] != "prepared":
                raise DomainError("BLOCKED_EVIDENCE", "Only prepared assignments can be bound")
            value = read_binding(self.store.path.parent, evidence.ref, digest)
            if (value.get("profile"), value.get("assignment_id"), value.get("request_fingerprint")) != \
               (evidence.profile, assignment_id, row["request_fingerprint"]):
                raise DomainError("BLOCKED_EVIDENCE", "Binding identity differs from the assignment")
            _event(conn, "assignment.bound", assignment_id, anchor)
        try:
            self.store.write(owner, change)
        except _NoChange:
            pass

    def cancel_prepared(self, owner: OwnerRef, assignment_id: str) -> None:
        def change(conn):
            row = conn.execute("SELECT state FROM assignments WHERE id=?", (assignment_id,)).fetchone()
            if row is None:
                raise DomainError("NOT_FOUND", "Assignment does not exist")
            _feature_of(conn, assignment_id)
            if row["state"] != "prepared":
                raise DomainError("BLOCKED_EVIDENCE", "Only prepared assignments can be cancelled")
            conn.execute("UPDATE assignments SET state='cancelled-before-launch' WHERE id=?", (assignment_id,))
            _event(conn, "assignment.cancelled", assignment_id)
        self.store.write(owner, change)

    def _bound_binding(self, bound) -> dict:
        return read_binding(self.store.path.parent, bound["evidence_ref"], bound["payload_digest"])

    def _gate_reasons(self, snapshot, stage, contributors, reviewer, feature, **extra) -> list[str]:
        """Profile gates for bound participants; contributors must be uniformly bound with one profile."""
        states = [(row, *binding_anchor(snapshot["events"], row["id"])) for row in contributors]
        if len({name for _, name, _ in states}) > 1:
            raise DomainError("BLOCKED_EVIDENCE", "Contributors mix unbound and bound or different profiles")
        if reviewer is not None:
            states.append((reviewer, *binding_anchor(snapshot["events"], reviewer["id"])))
        worktree = Path(feature["worktree"]) if feature["worktree"] and feature["state"] == "active" else None
        reasons = []
        for row, name, bound in states:
            if name is not None:
                ctx = {"stage": stage, "assignment": row, "binding": self._bound_binding(bound),
                       "feature": feature, "worktree": worktree, "store": self.store, "reviewer": reviewer, **extra}
                reasons += profiles.get(name).gate(stage, ctx)
        return reasons

    def reserve_launch(self, owner: OwnerRef, assignment_id: str, client: str | None = None) -> None:
        # Bound profiles run their reserve gate before the transaction (it may execute subprocesses).
        snapshot = self.store.read_snapshot()
        current = next((item for item in snapshot["assignments"] if item["id"] == assignment_id), None)
        if current is None:
            raise DomainError("NOT_FOUND", "Assignment does not exist")
        require_open_feature(snapshot, current["feature_id"])   # F5: before the reserve gate runs any subprocess
        if current["state"] != "prepared":
            raise DomainError("BLOCKED_EVIDENCE", "Assignment is not prepared")
        name, bound = binding_anchor(snapshot["events"], assignment_id)
        anchor = (name, bound and (bound["source_id"], bound["evidence_ref"], bound["payload_digest"]))
        # One client per attempt (design D2/D3): the flag, else the binding's client, else the default.
        bound_client = clients.bound_client(self._bound_binding(bound)) if bound is not None else None
        chosen = clients.get(client) if client is not None else bound_client or clients.get(None)
        if bound_client is not None and chosen.name != bound_client.name:
            raise DomainError("INVALID_INPUT", "Worker client conflicts with the assignment binding")
        read = bound is not None and self._bound_binding(bound).get("mode") == "read"   # Copilot reviewer design V1
        if current["permission_mode"] == "read-only" and not chosen.write_free_readonly and \
           not (read and chosen.read_mode):
            raise DomainError("UNSUPPORTED_CLIENT_MODE",
                              f"Worker client {chosen.name} has no write-free read-only mode")
        if name is not None:
            feature = next(item for item in snapshot["features"] if item["id"] == current["feature_id"])
            reasons = self._gate_reasons(snapshot, "reserve", [], current, feature)
            if reasons:
                raise DomainError("BLOCKED_EVIDENCE", "; ".join(reasons))

        def change(conn):
            row = conn.execute("SELECT * FROM assignments WHERE id=?", (assignment_id,)).fetchone()
            if row is None:
                raise DomainError("NOT_FOUND", "Assignment does not exist")
            if row["state"] != "prepared":
                raise DomainError("BLOCKED_EVIDENCE", "Assignment is not prepared")
            now_name, now_bound = binding_anchor(conn.execute(
                "SELECT kind, subject_id, source_id, evidence_ref, payload_digest FROM events WHERE subject_id=?",
                (assignment_id,)), assignment_id)
            if (now_name, now_bound and (now_bound["source_id"], now_bound["evidence_ref"],
                                         now_bound["payload_digest"])) != anchor:
                raise DomainError("BLOCKED_EVIDENCE", "Binding changed during reservation")
            feature = conn.execute("SELECT mandate_revision, worktree, state FROM features WHERE id=?", (row["feature_id"],)).fetchone()
            _open_feature(feature)
            if feature["mandate_revision"] != row["mandate_revision"]:
                raise DomainError("STALE_INPUT", "Mandate revision changed")
            worktree = _resolved_worktree(feature)
            holder = _fence_holder(conn, assignment_id, str(worktree))
            if holder is not None:
                raise DomainError("BLOCKED_EVIDENCE", f"Worktree is fenced by attempt {holder}")
            _files(worktree, json.loads(row["source_digests_json"]), check_current=True)
            contracts = json.loads(row["contract_files_json"])
            if contracts:   # implementation drive D22: a story seals no contract files
                _files(worktree, contracts, check_current=True)
            if hashlib.sha256(_json(contracts).encode()).hexdigest() != row["contract_digest"]:
                raise DomainError("STALE_INPUT", "Contract aggregate digest changed")
            for dependency in json.loads(row["depends_on_json"]):
                prerequisite = conn.execute("SELECT feature_id, state, return_json FROM assignments WHERE id=?", (dependency,)).fetchone()
                if (prerequisite is None or prerequisite["feature_id"] != row["feature_id"] or
                    prerequisite["state"] != "completed-claim" or not prerequisite["return_json"] or
                    json.loads(prerequisite["return_json"])["provenance_class"] != "observed"):
                    raise DomainError("BLOCKED_EVIDENCE", "Prerequisite completion is unobserved")
            for request_id in json.loads(row["blocking_requests_json"]):
                request = conn.execute("SELECT feature_id, state FROM decision_requests WHERE id=?", (request_id,)).fetchone()
                if request is None or request["feature_id"] != row["feature_id"] or request["state"] != "resolved":
                    raise DomainError("BLOCKED_EVIDENCE", "Decision request is unresolved")
            conn.execute("UPDATE assignments SET state='launch-pending' WHERE id=?", (assignment_id,))
            _event(conn, "assignment.launch-reserved", assignment_id, (chosen.name, None, None))
        self.store.write(owner, change)

    def retry_assignment(self, assignment_id: str, request_fingerprint: str) -> RetryReport:
        row = next((item for item in self.store.read_snapshot()["assignments"] if item["id"] == assignment_id), None)
        if row is None:
            raise DomainError("NOT_FOUND", "Assignment does not exist")
        if row["request_fingerprint"] != request_fingerprint:
            raise DomainError("IDEMPOTENCY_CONFLICT", "Request fingerprint does not match")
        needs = ["launch observation or verified isolation"] if row["state"] in ("launch-pending", "launch-unknown") else \
            [CONTINUATION_UNSUPPORTED] if row["state"] == "needs-decision" else []
        return RetryReport(assignment_id, row["state"], needs)

    def observe_launch(self, owner: OwnerRef, assignment_id: str, event: LaunchEvidence) -> None:
        if not event.source_id or not event.payload_digest:
            raise DomainError("INVALID_INPUT", "Launch evidence requires source and digest")
        classification = self.evidence_source.classify_launch(event) if self.evidence_source and event.event_ref else "reported"
        if classification not in ("reported", "observed"):
            raise DomainError("INVALID_INPUT", "Invalid launch evidence classification")
        def change(conn):
            row = conn.execute("SELECT state FROM assignments WHERE id=?", (assignment_id,)).fetchone()
            if row is None:
                raise DomainError("NOT_FOUND", "Assignment does not exist")
            _feature_of(conn, assignment_id)
            if row["state"] not in ("launch-pending", "launch-unknown"):
                raise DomainError("BLOCKED_EVIDENCE", "Launch was not reserved")
            state = "running" if classification == "observed" else "launch-unknown"
            conn.execute("UPDATE assignments SET state=?, external_start_seen=MAX(external_start_seen, ?), launch_ref=? WHERE id=?",
                         (state, int(classification == "observed"), event.event_ref, assignment_id))
            _event(conn, "assignment.launch-" + classification, assignment_id)
            conn.execute("UPDATE events SET source_id=?, evidence_ref=?, payload_digest=? WHERE revision=(SELECT revision+1 FROM meta)",
                         (event.source_id, event.event_ref, event.payload_digest))
        self.store.write(owner, change)

    def anchor_return(self, owner: OwnerRef, assignment_id: str, result: ReturnEvidence) -> None:
        """Persist a one-time terminal digest before any replay or observed return."""
        if not result.event_ref or not result.payload_digest or len(result.payload_digest) != 64:
            raise DomainError("INVALID_INPUT", "Terminal receipt requires a relative ref and SHA-256")

        def change(conn):
            row = conn.execute("SELECT state FROM assignments WHERE id=?", (assignment_id,)).fetchone()
            if row is None:
                raise DomainError("NOT_FOUND", "Assignment does not exist")
            if row["state"] not in ("launch-pending", "launch-unknown", "running"):
                raise DomainError("BLOCKED_EVIDENCE", "Assignment has no reserved launch")
            existing = conn.execute("SELECT source_id, evidence_ref, payload_digest FROM events "
                                    "WHERE kind='assignment.return-sealed' AND subject_id=?", (assignment_id,)).fetchall()
            if existing:
                if len(existing) == 1 and tuple(existing[0]) == \
                   (result.source_id, result.event_ref, result.payload_digest):
                    raise _NoChange()
                raise DomainError("BLOCKED_EVIDENCE", "Terminal receipt already has a different anchor")
            _feature_of(conn, assignment_id)
            _event(conn, "assignment.return-sealed", assignment_id)
            conn.execute("UPDATE events SET source_id=?, evidence_ref=?, payload_digest=? "
                         "WHERE revision=(SELECT revision+1 FROM meta)",
                         (result.source_id, result.event_ref, result.payload_digest))

        try:
            self.store.write(owner, change)
        except _NoChange:
            pass

    def record_return(self, owner: OwnerRef, assignment_id: str, result: ReturnEvidence) -> None:
        if result.result not in ("completed", "needs-decision", "blocked", "client-stopped") or not result.source_id:
            raise DomainError("INVALID_INPUT", "Return requires result and provenance")
        if result.result == "needs-decision" and not result.unresolved_question:
            raise DomainError("INVALID_INPUT", "Decision request needs a question")
        stopped = result.result == "client-stopped"   # design D25, ruling H7: a client_stop only on a stop, as a dict
        if stopped != (result.client_stop is not None) or (stopped and not isinstance(result.client_stop, dict)):
            raise DomainError("INVALID_INPUT", "A client stop goes with the result client-stopped, and only with it")
        classification = self.evidence_source.classify_return(result) if self.evidence_source and result.event_ref else "reported"
        if classification not in ("reported", "observed"):
            raise DomainError("INVALID_INPUT", "Invalid return evidence classification")
        if result.result == "client-stopped" and classification != "observed":   # ruling H7: never a reported claim
            raise DomainError("INVALID_INPUT", "client-stopped is the coordinator's own observed result")
        def change(conn):
            row = conn.execute("SELECT feature_id, state, paths_json FROM assignments WHERE id=?", (assignment_id,)).fetchone()
            if row is None:
                raise DomainError("NOT_FOUND", "Assignment does not exist")
            if row["state"] not in ("launch-pending", "launch-unknown", "running"):
                raise DomainError("BLOCKED_EVIDENCE", "Assignment has no reserved launch")
            feature = conn.execute("SELECT worktree, state, writable_paths_json FROM features WHERE id=?",
                                   (row["feature_id"],)).fetchone()
            _open_feature(feature)
            if result.changed_paths:
                _files(_resolved_worktree(feature), result.changed_paths, check_current=False)
                allowed = [path if path.endswith("/") else _path_identity(_resolved_worktree(feature), path)
                           for path in json.loads(row["paths_json"])]
                feature_scopes = json.loads(feature["writable_paths_json"])
                if any(not any(scope_covers(scope, _path_identity(_resolved_worktree(feature), item["path"]))
                               for scope in allowed) or
                       not _scope_allows(_path_identity(_resolved_worktree(feature), item["path"]), feature_scopes)
                       for item in result.changed_paths):
                    raise DomainError("UNSAFE_PATH", "Returned path exceeds assignment scope")
            for affected_id in result.affected_ids:
                affected = conn.execute("SELECT feature_id FROM assignments WHERE id=?", (affected_id,)).fetchone()
                if affected is None or affected["feature_id"] != row["feature_id"]:
                    raise DomainError("INVALID_INPUT", "Affected ID must be an assignment in this feature")
            state = {"completed": "completed-claim", "needs-decision": "needs-decision", "blocked": "blocked",
                     "client-stopped": "client-stopped"}[result.result]
            payload = {**asdict(result), "provenance_class": classification,
                       "trace": "observed" if classification == "observed" else "unobserved"}
            if payload["client_stop"] is None:   # design D25: only a stop's return carries the key
                del payload["client_stop"]
            payload["changed_paths"] = [
                {**entry, "canonical_path": _path_identity(_resolved_worktree(feature), entry["path"])}
                for entry in payload["changed_paths"]]
            conn.execute("UPDATE assignments SET state=?, return_json=?, external_start_seen=MAX(external_start_seen, ?) WHERE id=?",
                         (state, _json(payload), int(classification == "observed"), assignment_id))
            if result.result == "needs-decision":
                conn.execute("INSERT INTO decision_requests(id, feature_id, assignment_id, question_json, affected_json, state) "
                             "VALUES (?,?,?,?,?,'open')",
                             (str(uuid.uuid4()), row["feature_id"], assignment_id,
                              _json({"question": result.unresolved_question, "options": result.options}),
                              _json(result.affected_ids)))
            _event(conn, "assignment.return-" + classification, assignment_id)
            conn.execute("UPDATE events SET source_id=?, evidence_ref=?, payload_digest=? WHERE revision=(SELECT revision+1 FROM meta)",
                         (result.source_id, result.event_ref, result.payload_digest))
        self.store.write(owner, change)

    def revise_mandate(self, owner: OwnerRef, feature_id: str, expected_revision: int,
                       patch: dict, decision_id: str) -> int:
        if not isinstance(patch, dict) or not patch or set(patch) - MANDATE_FIELDS:
            raise DomainError("INVALID_INPUT", "Mandate patch contains unsupported fields")
        encoded = {field: _json(value) if field in JSON_FIELDS else value for field, value in patch.items()}
        if "outcome" in encoded and (not isinstance(encoded["outcome"], str) or not encoded["outcome"].strip()):
            raise DomainError("INVALID_INPUT", "Outcome must be nonempty")

        def change(conn):
            row = conn.execute("SELECT * FROM features WHERE id=?", (feature_id,)).fetchone()
            if row is None:
                raise DomainError("NOT_FOUND", "Feature does not exist")
            _open_feature(row)
            if row["mandate_revision"] != expected_revision:
                raise DomainError("REVISION_CONFLICT", "Mandate revision is stale")
            decision = conn.execute("SELECT feature_id, mandate_revision FROM decisions WHERE id=?", (decision_id,)).fetchone()
            if decision is None or tuple(decision) != (feature_id, expected_revision):
                raise DomainError("INVALID_INPUT", "Mandate revision requires a matching decision")
            changed = {field: value for field, value in encoded.items()
                       if value != row[field + "_json" if field in JSON_FIELDS else field]}
            if not changed:
                raise _NoChange()
            columns = ", ".join(f"{field + '_json' if field in JSON_FIELDS else field}=?" for field in changed)
            conn.execute(f"UPDATE features SET {columns}, mandate_revision=? WHERE id=?",
                         (*changed.values(), expected_revision + 1, feature_id))
            _event(conn, "mandate.revised", feature_id)
            return expected_revision + 1
        try:
            return self.store.write(owner, change)
        except _NoChange:
            return expected_revision

    def close_feature(self, owner: OwnerRef, feature_id: str, expected_revision: int, decision_id: str,
                      resolution: str) -> str:
        """Feature lifecycle design F1 to F6: a terminal state justified by a decision at the current revision, refused
        while work is in flight; the mandate revision is kept and the dirty paths' digests are stored for export."""
        from .exporter import file_digests_of

        if type(expected_revision) is not int or not isinstance(decision_id, str) or not decision_id:
            raise DomainError("INVALID_INPUT", "Close input has the wrong type")
        if resolution not in CLOSED_STATES:
            raise DomainError("INVALID_INPUT", "Resolution must be completed or abandoned")

        def change(conn):
            row = conn.execute("SELECT * FROM features WHERE id=?", (feature_id,)).fetchone()
            if row is None:
                raise DomainError("NOT_FOUND", "Feature does not exist")
            if row["state"] in CLOSED_STATES:
                closing = conn.execute("SELECT source_id FROM events WHERE kind IN ('feature.completed', "
                                       "'feature.abandoned') AND subject_id=? ORDER BY revision DESC LIMIT 1",
                                       (feature_id,)).fetchone()
                if row["state"] == resolution and closing is not None and closing[0] == decision_id:
                    raise _NoChange()
                _open_feature(row)
            if row["mandate_revision"] != expected_revision:
                raise DomainError("REVISION_CONFLICT", "Mandate revision is stale")
            decision = conn.execute("SELECT feature_id, mandate_revision FROM decisions WHERE id=?", (decision_id,)).fetchone()
            if decision is None or tuple(decision) != (feature_id, expected_revision):
                raise DomainError("INVALID_INPUT", "Closing a feature requires a matching decision")
            reasons = []
            for item in conn.execute("SELECT id, state, return_json FROM assignments WHERE feature_id=?",
                                     (feature_id,)):
                if item["state"] == "prepared":
                    reasons.append(f"assignment {item['id']} is prepared: assignment cancel-prepared {item['id']}")
                elif holds_fence(item["state"], item["return_json"]):
                    reasons.append(f"assignment {item['id']} holds the attempt fence ({item['state']}): recover, "
                                   f"then attempt resolve {item['id']}")
            for item in conn.execute("SELECT id FROM decision_requests WHERE feature_id=? AND state!='resolved'",
                                     (feature_id,)):
                reasons.append(f"decision request {item['id']} is open: decision add with its request_id")
            if reasons:
                raise DomainError("BLOCKED_EVIDENCE", "Feature has work in flight; " + "; ".join(reasons))
            digests = row["portable_file_digests_json"]
            if row["state"] == "active" and row["worktree"]:
                digests = _json(file_digests_of(Path(row["worktree"]), json.loads(row["dirty_paths_json"])))
            conn.execute("UPDATE features SET state=?, portable_file_digests_json=? WHERE id=?",
                         (resolution, digests, feature_id))
            _event(conn, "feature." + resolution, feature_id, (decision_id, None, None))
        try:
            self.store.write(owner, change)
        except _NoChange:
            pass
        return resolution

    def capture_bundle(self, owner: OwnerRef, spec: BundleInput) -> str:
        from . import bundles

        if not isinstance(spec.artifact_paths, list) or not spec.artifact_paths or \
           not isinstance(spec.dependency_paths, list) or \
           any(not isinstance(path, str) or not path for path in spec.artifact_paths + spec.dependency_paths):
            raise DomainError("INVALID_INPUT", "Bundle paths must be nonempty relative file names")
        if not isinstance(spec.source_assignment_ids, list) or not spec.source_assignment_ids or \
           any(not isinstance(value, str) or not value for value in spec.source_assignment_ids) or \
           len(spec.source_assignment_ids) != len(set(spec.source_assignment_ids)):
            raise DomainError("INVALID_INPUT", "Bundle requires distinct source assignments")
        snapshot = self.store.read_snapshot()
        feature = next((row for row in snapshot["features"] if row["id"] == spec.feature_id), None)
        if feature is None:
            raise DomainError("NOT_FOUND", "Feature does not exist")
        _open_feature(feature)   # F5, before the bundle store is written (a capture's replay is known only after it)
        worktree = _resolved_worktree(feature)
        _safe_feature_paths(worktree, spec.artifact_paths)
        if len({_path_identity(worktree, path) for path in spec.artifact_paths}) != len(spec.artifact_paths):
            raise DomainError("INVALID_INPUT", "Duplicate artifact path")
        try:
            artifact_digests = {path: hashlib.sha256((worktree / path).read_bytes()).hexdigest()
                                for path in spec.artifact_paths}
        except OSError as exc:
            raise DomainError("UNKNOWN_EVIDENCE", "Artifact bytes are unavailable") from exc
        contract_files_json = _json(spec.contract_files)
        contributor_ids = _artifact_contributors(snapshot["assignments"], snapshot["decision_requests"],
                                                 spec.feature_id, worktree, artifact_digests,
                                                 feature["mandate_revision"], contract_files_json)
        if set(spec.source_assignment_ids) != contributor_ids:
            raise DomainError("BLOCKED_EVIDENCE", "Bundle must name all recorded artifact contributors")
        assignments = []
        for assignment_id in spec.source_assignment_ids:
            row = next((item for item in snapshot["assignments"] if item["id"] == assignment_id), None)
            if row is None or row["feature_id"] != spec.feature_id:
                raise DomainError("INVALID_INPUT", "Source assignment does not belong to feature")
            assignments.append(row)
        reasons = self._gate_reasons(snapshot, "bundle", assignments, None, feature, spec=spec)
        if reasons:
            raise DomainError("BLOCKED_EVIDENCE", "; ".join(reasons))
        author_ids = sorted({row["author_id"] for row in assignments})
        bundle_id, path, manifest = bundles.capture(worktree, self.store.path.parent / "bundles",
            spec.feature_id, feature["mandate_revision"], spec.artifact_paths, spec.dependency_paths,
            spec.contract_files, spec.source_assignment_ids, author_ids,
            bundle_decisions(self.store.path.parent, snapshot, set(spec.source_assignment_ids)), commit=spec.commit)
        if any(entry["digest"] != artifact_digests[entry["path"]]
               for entry in manifest["files"] if entry["kind"] == "artifact"):
            raise DomainError("UNKNOWN_EVIDENCE", "Artifact changed during provenance comparison")
        contract_digest = hashlib.sha256(_json(spec.contract_files).encode()).hexdigest()

        def change(conn):
            existing = conn.execute("SELECT path, manifest_digest FROM bundles WHERE id=?", (bundle_id,)).fetchone()
            if existing is not None:
                if existing["path"] != str(path) or existing["manifest_digest"] != bundle_id:
                    raise DomainError("CORRUPT_STATE", "Bundle ID collides with a different record")
                raise _NoChange()
            current = conn.execute("SELECT mandate_revision, state FROM features WHERE id=?", (spec.feature_id,)).fetchone()
            _open_feature(current)
            if current is None or current[0] != feature["mandate_revision"]:
                raise DomainError("STALE_INPUT", "Mandate changed during capture")
            current_contributors = _artifact_contributors(
                conn.execute("SELECT * FROM assignments WHERE feature_id=?", (spec.feature_id,)),
                conn.execute("SELECT * FROM decision_requests WHERE feature_id=?", (spec.feature_id,)),
                spec.feature_id, worktree, artifact_digests, feature["mandate_revision"], contract_files_json)
            if current_contributors != contributor_ids:
                raise DomainError("BLOCKED_EVIDENCE", "Artifact contributor set changed during capture")
            for row in assignments:
                stored = conn.execute("SELECT author_id, contract_files_json, mandate_revision FROM assignments WHERE id=?",
                                      (row["id"],)).fetchone()
                if stored is None or stored["author_id"] != row["author_id"] or \
                   stored["contract_files_json"] != row["contract_files_json"] or \
                   stored["mandate_revision"] != row["mandate_revision"]:
                    raise DomainError("STALE_INPUT", "Source assignment changed during capture")
            conn.execute("INSERT INTO bundles(id, feature_id, manifest_digest, mandate_revision, "
                         "contract_files_json, contract_digest, source_assignments_json, path) "
                         "VALUES (?,?,?,?,?,?,?,?)",
                         (bundle_id, spec.feature_id, bundle_id, feature["mandate_revision"],
                          _json(spec.contract_files), contract_digest, _json(spec.source_assignment_ids), str(path)))
            _event(conn, "bundle.captured", bundle_id)
        try:
            self.store.write(owner, change)
        except _NoChange:
            pass
        return bundle_id

    def record_review(self, owner: OwnerRef, spec: ReviewEvidence) -> str:
        from . import bundles

        if not all(isinstance(value, str) and value.strip() for value in
                   (spec.reviewer_id, spec.source_id, spec.source_ref or "")) or not isinstance(spec.critical_open, bool):
            raise DomainError("INVALID_INPUT", "Review requires reviewer, source and critical finding state")
        classification = self.evidence_source.classify_review(spec) if self.evidence_source else "reported"
        if classification not in ("reported", "observed"):
            raise DomainError("INVALID_INPUT", "Invalid review evidence classification")
        review_id = str(uuid.uuid4())
        existing_review = [None]

        def change(conn):
            row = conn.execute("SELECT path, manifest_digest FROM bundles WHERE id=?", (spec.bundle_id,)).fetchone()
            if row is None:
                raise DomainError("NOT_FOUND", "Bundle does not exist")
            manifest = bundles.read_manifest(Path(row["path"]), row["manifest_digest"])
            if spec.reviewer_id in manifest["derived_author_ids"]:
                raise DomainError("INVALID_INPUT", "Reviewer authored or edited this bundle")
            if classification == "observed" and getattr(self.evidence_source, "one_review_per_receipt", False):
                # Bound review only (generic sources are unchanged): one anchored receipt is one review, so a
                # crash-window retry returns the stored row.
                prior = conn.execute("SELECT id, reviewer_id, finding_json, critical_open FROM reviews WHERE "
                                     "bundle_id=? AND provenance_ref=? AND provenance_class='observed'",
                                     (spec.bundle_id, spec.source_ref)).fetchone()
                if prior is not None:
                    if (prior["reviewer_id"], prior["finding_json"], prior["critical_open"]) != (
                            spec.reviewer_id, _json({"source_id": spec.source_id, "finding": spec.finding}),
                            int(spec.critical_open)):
                        raise DomainError("BLOCKED_EVIDENCE", "Anchored review receipt already recorded differently")
                    existing_review[0] = prior["id"]
                    raise _NoChange()
            _open_feature(conn.execute("SELECT f.state FROM bundles b JOIN features f ON f.id=b.feature_id "
                                       "WHERE b.id=?", (spec.bundle_id,)).fetchone())
            conn.execute("INSERT INTO reviews(id, bundle_id, reviewer_id, provenance_ref, provenance_class, "
                         "finding_json, critical_open) VALUES (?,?,?,?,?,?,?)",
                         (review_id, spec.bundle_id, spec.reviewer_id, spec.source_ref, classification,
                          _json({"source_id": spec.source_id, "finding": spec.finding}), int(spec.critical_open)))
            _event(conn, "review.recorded", review_id)
        try:
            self.store.write(owner, change)
        except _NoChange:
            return existing_review[0]
        return review_id

    def _bundle_currentness(self, bundle: dict, feature: dict, decisions: list[dict]) -> tuple[str, dict]:
        from . import bundles

        closed = feature["state"] in CLOSED_STATES   # F9: every check but the file comparison
        if not bundle["path"] or not closed and (not feature["worktree"] or feature["state"] == "unresolved"):
            return "unknown", {"bundle_id": bundle["id"], "reason": "imported-evidence"}
        try:
            manifest = bundles.read_manifest(Path(bundle["path"]), bundle["manifest_digest"])
            references = {"bundle_id": bundle["id"], "manifest_digest": bundle["manifest_digest"],
                          "mandate_revision": bundle["mandate_revision"],
                          "contract_digest": bundle["contract_digest"], "files": manifest["files"]}
            if not bundles.compare_copy(Path(bundle["path"]), manifest):
                return "unknown", {**references, "reason": "bundle-copy"}
            if manifest["feature_id"] != bundle["feature_id"] or \
               manifest["mandate_revision"] != bundle["mandate_revision"] or \
               [entry for entry in manifest["files"] if entry["kind"] == "contract"] != \
               [{"kind": "contract", **entry} for entry in
                sorted(json.loads(bundle["contract_files_json"]), key=lambda item: item["path"])]:
                return "unknown", {**references, "reason": "manifest-record"}
            if feature["mandate_revision"] != bundle["mandate_revision"]:
                return "stale", {**references, "reason": "mandate"}
            if hashlib.sha256(_json(json.loads(bundle["contract_files_json"])).encode()).hexdigest() != bundle["contract_digest"]:
                return "unknown", {**references, "reason": "contract-record"}
            decided = sealed_decisions_state(manifest, decisions)
            if decided != "current":
                return decided, {**references, "reason": "decision-record"}
            if closed:
                return "closed", {**references, "files_compared": False}
            source_state = bundles.source_state(Path(feature["worktree"]), manifest)
            if source_state != "current":
                return source_state, {**references, "reason": "file-digest"}
            return "current", references
        except (DomainError, KeyError, TypeError, ValueError):
            return "unknown", {"bundle_id": bundle["id"], "reason": "unverifiable"}

    def _acceptance_gates(self, snapshot, stage, bundle, review, feature) -> list[str]:
        by_id = {row["id"]: row for row in snapshot["assignments"]}
        contributors = [by_id[item] for item in json.loads(bundle["source_assignments_json"]) if item in by_id]
        reviewer = next((row for row in snapshot["assignments"] if review["provenance_ref"] ==
                         receipt_ref(run_name(row["id"], row["request_fingerprint"]), "return")), None)
        return self._gate_reasons(snapshot, stage, contributors, reviewer, feature, bundle=bundle, review=review)

    def accept(self, owner: OwnerRef, bundle_id: str, review_id: str) -> str:
        snapshot = self.store.read_snapshot()
        bundle = next((row for row in snapshot["bundles"] if row["id"] == bundle_id), None)
        review = next((row for row in snapshot["reviews"] if row["id"] == review_id), None)
        if bundle is None or review is None:
            raise DomainError("NOT_FOUND", "Bundle or review does not exist")
        if review["bundle_id"] != bundle_id or review["provenance_class"] != "observed":
            raise DomainError("BLOCKED_EVIDENCE", "Independent observed review is required")
        if any(row["critical_open"] for row in snapshot["reviews"] if row["bundle_id"] == bundle_id):
            raise DomainError("BLOCKED_EVIDENCE", "Bundle has an open critical finding")
        feature = next(row for row in snapshot["features"] if row["id"] == bundle["feature_id"])
        _open_feature(feature)
        reasons = self._acceptance_gates(snapshot, "accept", bundle, review, feature)
        if reasons:
            raise DomainError("BLOCKED_EVIDENCE", "; ".join(reasons))
        state, checked = self._bundle_currentness(bundle, feature, snapshot["decisions"])
        if state != "current":
            raise DomainError("STALE_INPUT" if state == "stale" else "UNKNOWN_EVIDENCE", "Bundle is not current")
        acceptance_id = str(uuid.uuid4())
        dependencies = [entry for entry in checked["files"] if entry["kind"] == "dependency"]

        def change(conn):
            current = conn.execute("SELECT mandate_revision, state FROM features WHERE id=?", (bundle["feature_id"],)).fetchone()
            _open_feature(current)
            if current is None or current[0] != bundle["mandate_revision"]:
                raise DomainError("STALE_INPUT", "Mandate changed before acceptance")
            if conn.execute("SELECT 1 FROM reviews WHERE bundle_id=? AND critical_open=1", (bundle_id,)).fetchone():
                raise DomainError("BLOCKED_EVIDENCE", "Bundle has an open critical finding")
            conn.execute("INSERT INTO acceptances(id, bundle_id, review_id, dependency_digests_json, revision) "
                         "VALUES (?,?,?,?,(SELECT revision+1 FROM meta))",
                         (acceptance_id, bundle_id, review_id, _json(dependencies)))
            _event(conn, "bundle.accepted", acceptance_id)
        self.store.write(owner, change)
        return acceptance_id

    def verify(self) -> VerificationReport:
        snapshot = self.store.read_snapshot()
        acceptances = snapshot["acceptances"]
        if not acceptances:
            return VerificationReport(False, "unknown", [])
        features = {row["id"]: row for row in snapshot["features"]}
        bundles_by_id = {row["id"]: row for row in snapshot["bundles"]}
        blocked_bundles = {row["bundle_id"] for row in snapshot["reviews"] if row["critical_open"]}
        checked = []
        for acceptance in acceptances:
            bundle = bundles_by_id[acceptance["bundle_id"]]
            state, details = self._bundle_currentness(bundle, features[bundle["feature_id"]], snapshot["decisions"])
            if bundle["id"] in blocked_bundles:
                state, details = "unknown", {**details, "reason": "open-critical-finding"}
            elif state in ("current", "closed"):
                review = next(row for row in snapshot["reviews"] if row["id"] == acceptance["review_id"])
                try:
                    reasons = self._acceptance_gates(snapshot, "verify", bundle, review, features[bundle["feature_id"]])
                except DomainError as exc:
                    state, details = ("stale" if exc.code == "STALE_INPUT" else "unknown"), \
                        {**details, "reason": "profile-gate", "gate": [exc.message]}
                else:
                    if reasons:
                        state, details = "unknown", {**details, "reason": "profile-gate", "gate": reasons}
            checked.append({"acceptance_id": acceptance["id"], "current": state, **details})
        current = "current" if any(item["current"] == "current" for item in checked) else \
                  "unknown" if any(item["current"] == "unknown" for item in checked) else \
                  "stale" if any(item["current"] == "stale" for item in checked) else "closed"
        return VerificationReport(True, current, checked)

    def status(self, include_closed: bool = False) -> dict:
        snapshot = self.store.read_snapshot()
        closed = [row["id"] for row in snapshot["features"] if row["state"] in CLOSED_STATES]
        features = [row for row in snapshot["features"] if include_closed or row["state"] not in CLOSED_STATES]
        questions = [item["id"] for item in snapshot["decision_requests"] if item["state"] != "resolved"]
        owner = dict(snapshot["owner"])
        if owner["session_id"] is not None:
            owner["session_id"] = owner["session_id"][:200]
        return {
            "repository_id": snapshot["repository_id"], "revision": snapshot["revision"],
            "owner": owner, "feature_ids": [row["id"] for row in features[:100]],
            "features": [{"id": row["id"], "mandate_revision": row["mandate_revision"],
                          "branch": row["branch"][:200], "state": row["state"],
                          "mandate_summary": {"outcome": row["outcome"][:160],
                                              "exclusions": row["exclusions_json"][:160],
                                              "authority": row["authority_json"][:160]},
                          "detail": {"kind": "feature", "id": row["id"]}} for row in features[:100]],
            "closed_feature_ids": closed[-100:],   # F8: the newest closed features, hidden from the list above
            "open_question_ids": questions[:100],
            "tasks": [{"id": row["id"], "feature_id": row["feature_id"], "state": row["state"],
                       "detail": {"kind": "assignment", "id": row["id"]}}
                      for row in snapshot["assignments"][:100]],
            "counts": {table: len(snapshot[table]) for table in
                       ("features", "decision_requests", "decisions", "assignments", "events", "bundles", "reviews", "acceptances")},
        }

    def detail(self, kind: str, record_id: str) -> dict:
        table = DETAIL_TABLES.get(kind)
        if table is None:
            raise DomainError("INVALID_INPUT", "Unsupported detail kind")
        for record in self.store.read_snapshot()[table]:
            if record["id"] == record_id:
                result = dict(record)
                for key, value in tuple(result.items()):
                    if key.endswith("_json") and value is not None:
                        result[key[:-5]] = json.loads(value)
                        del result[key]
                if kind == "decision":
                    result["provenance_class"] = "reported" if result["source_kind"] == "operator-reported" else "coordinator"
                if kind == "bundle":
                    from . import bundles
                    manifest = bundles.read_manifest(Path(result["path"]), result["manifest_digest"])
                    result["derived_author_ids"] = manifest["derived_author_ids"]
                    result["files"] = manifest["files"]
                if kind == "review":
                    from .bmad_review import finding_counts   # design D26: a flat per-severity summary
                    result["finding_counts"] = finding_counts(record["finding_json"])
                return result
        raise DomainError("NOT_FOUND", "Record does not exist")
