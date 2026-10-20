"""Private, bounded filesystem and process receipts for one worker attempt."""

import hashlib
import json
import os
import secrets
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .model import BindingEvidence, DomainError, LaunchEvidence, ReturnEvidence, ReviewEvidence
from .repo import RepoIdentity
from .store import Store, open_store


@dataclass(frozen=True)
class FileStamp:
    kind: str
    digest: str
    mode: int


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def run_name(assignment_id: str, request_fingerprint: str) -> str:
    return hashlib.sha256((assignment_id + "\0" + request_fingerprint).encode()).hexdigest()


def run_root(repo: RepoIdentity) -> Path:
    return repo.common_dir / "coordinator" / "runs"


def receipt_ref(name: str, kind: str) -> str:
    return f"runs/{name}/{kind}.json"


def _safe_path(text: str) -> bool:
    path = PurePosixPath(text)
    return (isinstance(text, str) and bool(text) and not path.is_absolute() and
            text == str(path) and all(part not in (".", "..") for part in path.parts))


def inventory(worktree: Path) -> dict[str, FileStamp]:
    """Snapshot tracked and nonignored untracked paths, including symlink bytes."""
    try:
        raw = subprocess.run(["git", "-C", str(worktree), "ls-files", "-z", "--cached", "--others",
                              "--exclude-standard"], check=True, capture_output=True).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise DomainError("UNKNOWN_EVIDENCE", "Cannot inventory Git worktree") from exc
    found = {}
    for name in raw.split(b"\0"):
        if not name:
            continue
        try:
            relative = os.fsdecode(name)
            if not _safe_path(relative):
                raise ValueError("unsafe Git path")
            path = worktree / relative
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                kind, content = "symlink", os.fsencode(os.readlink(path))
            elif stat.S_ISREG(mode):
                kind, content = "file", path.read_bytes()
            else:
                kind, content = "other", b""
            found[relative] = FileStamp(kind, hashlib.sha256(content).hexdigest(), stat.S_IMODE(mode))
        except FileNotFoundError:
            # A removed tracked path is represented by absence; the delta remains visible.
            continue
        except (OSError, ValueError) as exc:
            raise DomainError("UNKNOWN_EVIDENCE", "Cannot inventory worktree path") from exc
    return found


def delta(before: dict[str, FileStamp], after: dict[str, FileStamp]) -> list[str]:
    return sorted(name for name in before.keys() | after.keys() if before.get(name) != after.get(name))


def stamp_json(values: dict[str, FileStamp]) -> dict:
    return {name: {"kind": item.kind, "digest": item.digest, "mode": item.mode}
            for name, item in sorted(values.items())}


def private_file(path: Path, data: bytes) -> str:
    """Atomic exclusive publication (design D16): a dot-prefixed temp in the same directory, fsync, then a hard
    link to the final name, which raises FileExistsError exactly as O_EXCL did. A reader sees absent or whole."""
    path = Path(path)
    staging = path.parent / f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.link(staging, path)
    finally:
        staging.unlink(missing_ok=True)
        try:
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            pass
    return hashlib.sha256(data).hexdigest()


def seal(path: Path, value: dict) -> str:
    return private_file(path, canonical(value))


def read_receipt(repo: RepoIdentity, ref: str, expected_digest: str) -> dict:
    if not _safe_path(ref) or len(expected_digest) != 64 or any(c not in "0123456789abcdef" for c in expected_digest):
        raise DomainError("UNKNOWN_EVIDENCE", "Receipt reference is invalid")
    parts = PurePosixPath(ref).parts
    if len(parts) != 3 or parts[0] != "runs" or len(parts[1]) != 64 or parts[2] not in ("launch.json", "return.json"):
        raise DomainError("UNKNOWN_EVIDENCE", "Receipt reference is outside a run")
    base = repo.common_dir / "coordinator"
    path = base.joinpath(*parts)
    try:
        if any(parent.is_symlink() for parent in (path, path.parent, path.parent.parent, base)):
            raise ValueError("symlink")
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError("not private regular file")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected_digest:
            raise ValueError("digest differs")
        parsed = json.loads(data)
        if not isinstance(parsed, dict) or canonical(parsed) != data:
            raise ValueError("noncanonical receipt")
        return parsed
    except (OSError, ValueError, TypeError) as exc:
        raise DomainError("UNKNOWN_EVIDENCE", "Receipt is unavailable or changed") from exc


def _is_hex(text: object) -> bool:
    return isinstance(text, str) and len(text) == 64 and all(c in "0123456789abcdef" for c in text)


def seal_binding(base: Path, value: dict) -> BindingEvidence:
    """Publish content-addressed binding bytes exclusively; identical bytes are accepted."""
    data = canonical(value)
    digest = hashlib.sha256(data).hexdigest()
    directory, final = base / "bindings", base / "bindings" / (digest + ".json")
    if base.is_symlink() or directory.is_symlink():
        raise DomainError("UNSAFE_PATH", "Binding directory is unsafe")
    directory.mkdir(mode=0o700, exist_ok=True)
    staging = directory / f".{digest}.{os.getpid()}.tmp"
    try:
        private_file(staging, data)
        try:
            os.link(staging, final)
        except FileExistsError:
            pass
    finally:
        staging.unlink(missing_ok=True)
    try:
        read_binding(base, f"bindings/{digest}.json", digest)
    except DomainError as exc:
        raise DomainError("BLOCKED_EVIDENCE", "Binding file conflicts with different bytes") from exc
    return BindingEvidence(str(value.get("profile")), f"bindings/{digest}.json", digest)


def read_binding(base: Path, ref: str, expected_digest: str) -> dict:
    parts = PurePosixPath(ref).parts if isinstance(ref, str) else ()
    if not _is_hex(expected_digest) or parts != ("bindings", expected_digest + ".json"):
        raise DomainError("UNKNOWN_EVIDENCE", "Binding reference is invalid")
    path = base.joinpath(*parts)
    try:
        if any(parent.is_symlink() for parent in (path, path.parent, base)):
            raise ValueError("symlink")
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError("not private regular file")
        data = path.read_bytes()
        parsed = json.loads(data)
        if hashlib.sha256(data).hexdigest() != expected_digest or not isinstance(parsed, dict) or \
           canonical(parsed) != data:
            raise ValueError("binding bytes differ")
        return parsed
    except (OSError, ValueError, TypeError) as exc:
        raise DomainError("UNKNOWN_EVIDENCE", "Binding is unavailable or changed") from exc


class ReceiptEvidenceSource:
    def __init__(self, repo: RepoIdentity, assignment_id: str, request_fingerprint: str,
                 store: Store | None = None, historical: bool = False):
        self.repo = repo
        self.assignment_id = assignment_id
        self.request_fingerprint = request_fingerprint
        self.name = run_name(assignment_id, request_fingerprint)
        self.store = store or open_store(repo)
        self.historical = historical  # internal: receipt-internal checks only, no current-byte equality

    def binding(self) -> dict:
        row = self._stored_event("assignment.bound")
        value = read_binding(self.repo.common_dir / "coordinator", row["evidence_ref"], row["payload_digest"])
        if (value.get("assignment_id"), value.get("request_fingerprint"), value.get("profile")) != \
           (self.assignment_id, self.request_fingerprint, row["source_id"]):
            raise DomainError("UNKNOWN_EVIDENCE", "Binding identity differs")
        return value

    def _stored_event(self, kind: str) -> dict:
        rows = [row for row in self.store.read_snapshot()["events"]
                if row["subject_id"] == self.assignment_id and row["kind"] == kind]
        if len(rows) != 1 or not rows[0]["payload_digest"]:
            raise DomainError("UNKNOWN_EVIDENCE", "Durable receipt anchor is missing or ambiguous")
        return rows[0]

    def terminal_digest(self) -> str:
        row = self._stored_event("assignment.return-sealed")
        if row["evidence_ref"] != receipt_ref(self.name, "return"):
            raise DomainError("UNKNOWN_EVIDENCE", "Terminal anchor refers to another receipt")
        return row["payload_digest"]

    def _bound(self, ref: str | None, digest: str, kind: str) -> dict:
        if ref != receipt_ref(self.name, kind):
            raise DomainError("UNKNOWN_EVIDENCE", "Receipt does not belong to this attempt")
        value = read_receipt(self.repo, ref, digest)
        if (value.get("assignment_id"), value.get("request_fingerprint"), value.get("kind")) != \
           (self.assignment_id, self.request_fingerprint, kind):
            raise DomainError("UNKNOWN_EVIDENCE", "Receipt identity differs")
        if value.get("worktree") != str(self.repo.worktree) or value.get("common_dir") != str(self.repo.common_dir):
            raise DomainError("UNKNOWN_EVIDENCE", "Receipt repository differs")
        return value

    def _verify_envelope(self, launch: dict) -> None:
        directory = run_root(self.repo) / self.name
        path = directory / "envelope.json"
        try:
            if directory.is_symlink():
                raise ValueError("aliased run directory")
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
                    raise ValueError("envelope is not a private regular file")
                content = stream.read()
            if hashlib.sha256(content).hexdigest() != launch.get("envelope_sha256"):
                raise ValueError("frozen envelope bytes differ")
        except (OSError, ValueError) as exc:
            raise DomainError("UNKNOWN_EVIDENCE", "Frozen worker envelope is missing or changed") from exc

    def classify_launch(self, event: LaunchEvidence):
        value = self._bound(event.event_ref, event.payload_digest, "launch")
        if value.get("pid") != event.worker_ref or value.get("source_id") != event.source_id:
            raise DomainError("UNKNOWN_EVIDENCE", "Launch receipt differs")
        self._verify_envelope(value)
        return "observed"

    def classify_return(self, event: ReturnEvidence):
        terminal = self._stored_event("assignment.return-sealed")
        if (terminal["evidence_ref"], terminal["payload_digest"], terminal["source_id"]) != \
           (event.event_ref, event.payload_digest, event.source_id):
            raise DomainError("UNKNOWN_EVIDENCE", "Terminal receipt differs from durable anchor")
        value = self._bound(event.event_ref, event.payload_digest, "return")
        launch_event = self._stored_event("assignment.launch-observed")
        if launch_event["evidence_ref"] != receipt_ref(self.name, "launch") or \
           launch_event["payload_digest"] != value.get("launch_sha256"):
            raise DomainError("UNKNOWN_EVIDENCE", "Launch receipt differs from durable observation")
        launch = self._bound(launch_event["evidence_ref"], launch_event["payload_digest"], "launch")
        if launch["source_id"] != launch_event["source_id"]:
            raise DomainError("UNKNOWN_EVIDENCE", "Launch source differs from durable observation")
        self._verify_envelope(launch)
        if (value.get("result") != event.result or value.get("source_id") != event.source_id or
            value.get("changed_paths") != event.changed_paths or value.get("checks") != event.checks or
            value.get("descendants") != event.descendants or
            value.get("unresolved_question") != event.unresolved_question or
            value.get("options") != event.options or value.get("affected_ids") != event.affected_ids or
            value.get("client_stop") != event.client_stop):
            raise DomainError("UNKNOWN_EVIDENCE", "Return receipt differs")
        if self.historical:
            after = value.get("after")
            if not isinstance(after, dict) or any(
                    after.get(item["path"], {}).get("digest") != item["digest"] for item in event.changed_paths):
                raise DomainError("UNKNOWN_EVIDENCE", "Returned digests differ from terminal inventory")
            return "observed"
        if stamp_json(inventory(self.repo.worktree)) != value.get("after"):
            raise DomainError("UNKNOWN_EVIDENCE", "Current worktree differs from terminal inventory")
        for entry in value.get("immutable_inputs", []):
            path = self.repo.worktree / entry["path"]
            if path.is_symlink() or not path.is_file() or \
               hashlib.sha256(path.read_bytes()).hexdigest() != entry["digest"]:
                raise DomainError("UNKNOWN_EVIDENCE", "Immutable source bytes changed")
        for item in event.changed_paths:
            path = self.repo.worktree / item["path"]
            if not _safe_path(item["path"]) or path.is_symlink() or not path.is_file() or \
               hashlib.sha256(path.read_bytes()).hexdigest() != item["digest"]:
                raise DomainError("UNKNOWN_EVIDENCE", "Returned bytes changed")
        return "observed"

    def classify_review(self, event: ReviewEvidence):
        return "reported"
