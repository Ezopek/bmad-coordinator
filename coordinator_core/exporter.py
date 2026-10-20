"""Portable one-revision checkpoints with immutable whole-directory publication."""

import ctypes
import errno
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import uuid
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .bundles import _manifest_bytes
from .model import DomainError, OwnerRef
from .repo import RepoIdentity, resolve_repo
from .store import SCHEMA, Store, TABLES, _connect, _require_local_ext4, open_store


RECORD_FIELDS = {
    "decision_requests": ("id", "feature_id", "assignment_id", "question_json", "affected_json", "state", "resolution_decision_id"),
    "decisions": ("id", "feature_id", "mandate_revision", "source_kind", "source_ref", "reason", "choice_json", "request_id"),
    "assignments": ("id", "feature_id", "idempotency_key", "request_fingerprint", "brief_ref", "mandate_revision", "source_digests_json", "contract_files_json", "contract_digest", "paths_json", "permission_mode", "author_id", "depends_on_json", "blocking_requests_json", "return_json"),
    "events": ("id", "revision", "kind", "subject_id", "source_id", "evidence_ref", "payload_digest"),
    "bundles": ("id", "feature_id", "manifest_digest", "mandate_revision", "contract_files_json", "contract_digest", "source_assignments_json", "manifest"),
    "reviews": ("id", "bundle_id", "reviewer_id", "provenance_ref", "provenance_class", "finding_json", "critical_open"),
    "acceptances": ("id", "bundle_id", "review_id", "dependency_digests_json", "revision"),
}
FEATURE_COLUMNS = ("id", "mandate_revision", "outcome", "exclusions_json", "constraints_json", "completion_json", "authority_json", "branch", "base_ref", "base_commit", "dirty_paths_json", "writable_paths_json")
FEATURE_FIELDS = (*FEATURE_COLUMNS, "file_digests")
JSON_FIELDS = {"exclusions_json", "constraints_json", "completion_json", "authority_json", "dirty_paths_json", "writable_paths_json", "question_json", "affected_json", "choice_json", "source_digests_json", "contract_files_json", "paths_json", "depends_on_json", "blocking_requests_json", "return_json", "finding_json", "source_assignments_json", "dependency_digests_json"}
PATH_LISTS = {"dirty_paths_json", "writable_paths_json", "paths_json"}
FILE_LISTS = {"source_digests_json", "contract_files_json", "dependency_digests_json"}


@dataclass(frozen=True)
class ExportReceipt:
    directory: Path
    revision: int
    digest: str


@dataclass(frozen=True)
class Checkpoint:
    source_repository_id: str
    revision: int
    features: list[dict]
    records: dict[str, list[dict]]
    digest: str


@dataclass(frozen=True)
class ImportReport:
    repository_id: str
    source_repository_id: str
    origin_checkpoint_id: str
    worktrees: dict[str, str]
    unknown_features: list[str]


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_path(path: str) -> None:
    if not isinstance(path, str) or not path or Path(path).is_absolute() or ".." in Path(path).parts or \
       path in (".", "") or "\\" in path:
        raise DomainError("INVALID_CHECKPOINT", "Portable path must stay relative to its feature worktree")


def _check_refs(row: dict) -> None:
    for key, value in row.items():
        if key in JSON_FIELDS and value is not None:
            if not isinstance(value, str):
                raise DomainError("INVALID_CHECKPOINT", "Stored JSON field must be text")
            try:
                parsed = json.loads(value)
                if json.dumps(parsed, sort_keys=True, separators=(",", ":"), allow_nan=False) != value:
                    raise ValueError("noncanonical JSON")
            except (ValueError, TypeError) as exc:
                raise DomainError("INVALID_CHECKPOINT", "Invalid structured record JSON") from exc
            if key in PATH_LISTS:
                if not isinstance(parsed, list):
                    raise DomainError("INVALID_CHECKPOINT", "Path field must be a list")
                for path in parsed:
                    _safe_path(path)
            if key in FILE_LISTS:
                if not isinstance(parsed, list):
                    raise DomainError("INVALID_CHECKPOINT", "File digest field must be a list")
                for entry in parsed:
                    if not isinstance(entry, dict) or "path" not in entry or "digest" not in entry:
                        raise DomainError("INVALID_CHECKPOINT", "Invalid file digest reference")
                    _safe_path(entry["path"])
                    if not isinstance(entry["digest"], str) or len(entry["digest"]) != 64 or \
                       any(c not in "0123456789abcdef" for c in entry["digest"]):
                        raise DomainError("INVALID_CHECKPOINT", "Invalid file digest")
    if "brief_ref" in row:
        _safe_path(row["brief_ref"])
    if "return_json" in row and row["return_json"]:
        result = json.loads(row["return_json"])
        if not isinstance(result, dict):
            raise DomainError("INVALID_CHECKPOINT", "Invalid return evidence")
        for entry in result.get("changed_paths", []):
            if not isinstance(entry, dict) or "path" not in entry:
                raise DomainError("INVALID_CHECKPOINT", "Invalid returned path")
            _safe_path(entry["path"])
            if "canonical_path" in entry:
                _safe_path(entry["canonical_path"])


def _portable(snapshot: dict) -> dict:
    from .bundles import read_manifest

    features = []
    for source in snapshot["features"]:
        row = {field: source[field] for field in FEATURE_COLUMNS}
        if source["state"] == "unresolved" and source["portable_file_digests_json"] is not None:
            file_digests = json.loads(source["portable_file_digests_json"])
        else:
            if not source["worktree"]:
                raise DomainError("UNKNOWN_EVIDENCE", "Active feature has no mapped worktree")
            worktree = Path(source["worktree"])
            file_digests = []
            for relative in dict.fromkeys(json.loads(source["dirty_paths_json"])):
                _safe_path(relative)
                path = (worktree / relative).resolve()
                if not path.is_relative_to(worktree):
                    raise DomainError("UNSAFE_PATH", "Feature file escapes its worktree")
                try:
                    digest = _digest(path.read_bytes())
                except OSError:
                    digest = None
                file_digests.append({"path": relative, "digest": digest})
        row["file_digests"] = file_digests
        _check_refs(row)
        features.append(row)
    records = {}
    for table, fields in RECORD_FIELDS.items():
        records[table] = []
        for source in snapshot[table]:
            row = {field: source[field] for field in fields if field != "manifest"}
            if table == "bundles":
                try:
                    row["manifest"] = read_manifest(Path(source["path"]), source["manifest_digest"])
                except DomainError:
                    row["manifest"] = None
            _check_refs(row)
            records[table].append(row)
    return {"format": 1, "source_repository_id": snapshot["repository_id"],
            "revision": snapshot["revision"], "features": features, "records": records}


def _markdown(revision: int, digest: str) -> bytes:
    return f"# Coordinator checkpoint\n\nRevision: {revision}\nDigest: {digest}\n\nThis handoff is historical evidence. Resolve worktrees and verify files before continuing.\n".encode()


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_fsynced(path: Path, data: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _publish_directory(staging: Path, destination: Path) -> bool:
    """Linux renameat2 RENAME_NOREPLACE; return False when version already exists."""
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = libc.renameat2
    renameat2.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    renameat2.restype = ctypes.c_int
    if renameat2(-100, os.fsencode(staging), -100, os.fsencode(destination), 1) == 0:
        return True
    error = ctypes.get_errno()
    if error == errno.EEXIST:
        return False
    raise OSError(error, os.strerror(error), str(destination))


def _validate_semantics(features: list[dict], records: dict[str, list[dict]]) -> None:
    by_feature = {row["id"]: row for row in features}
    if len(by_feature) != len(features):
        raise ValueError("duplicate feature ID")
    for feature in features:
        required = json.loads(feature["dirty_paths_json"])
        entries = feature["file_digests"]
        if len(entries) != len({entry["path"] for entry in entries}) or \
           {entry["path"] for entry in entries} != set(required):
            raise ValueError("feature file digest coverage mismatch")
    assignments = {row["id"]: row for row in records["assignments"]}
    requests = {row["id"]: row for row in records["decision_requests"]}
    bundles = {row["id"]: row for row in records["bundles"]}
    reviews = {row["id"]: row for row in records["reviews"]}
    for row in records["decision_requests"]:
        if row["feature_id"] not in by_feature or (row["assignment_id"] and
            assignments.get(row["assignment_id"], {}).get("feature_id") != row["feature_id"]):
            raise ValueError("decision request feature mismatch")
    for row in records["decisions"]:
        if row["feature_id"] not in by_feature or (row["request_id"] and
            requests.get(row["request_id"], {}).get("feature_id") != row["feature_id"]):
            raise ValueError("decision feature mismatch")
    for row in records["assignments"]:
        if row["feature_id"] not in by_feature or any(
            assignments.get(ref, {}).get("feature_id") != row["feature_id"]
            for ref in json.loads(row["depends_on_json"])) or any(
            requests.get(ref, {}).get("feature_id") != row["feature_id"]
            for ref in json.loads(row["blocking_requests_json"])):
            raise ValueError("assignment feature mismatch")
    for row in records["bundles"]:
        if row["manifest"] is not None and not isinstance(row["manifest"], dict):
            raise ValueError("invalid bundle manifest")
        if row["feature_id"] not in by_feature or any(
            assignments.get(ref, {}).get("feature_id") != row["feature_id"]
            for ref in json.loads(row["source_assignments_json"])) or (
            row["manifest"] is not None and row["manifest"].get("feature_id") != row["feature_id"]):
            raise ValueError("bundle feature mismatch")
    for row in records["acceptances"]:
        if row["bundle_id"] not in bundles or \
           reviews.get(row["review_id"], {}).get("bundle_id") != row["bundle_id"]:
            raise ValueError("acceptance review is for another bundle")


def validate_checkpoint(source: Path, *, _temporary: bool = False) -> Checkpoint:
    source = Path(source)
    try:
        if not source.is_dir() or source.is_symlink() or \
           {item.name for item in source.iterdir()} != {"checkpoint.json", "checkpoint.md"} or \
           any(item.is_symlink() or not item.is_file() for item in source.iterdir()):
            raise ValueError("checkpoint pair is incomplete")
        raw = (source / "checkpoint.json").read_bytes()
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict) or set(data) != {"format", "source_repository_id", "revision", "features", "records", "digest"} or \
           data["format"] != 1 or not isinstance(data["source_repository_id"], str) or \
           not isinstance(data["revision"], int) or data["revision"] < 0 or \
           not isinstance(data["features"], list) or not isinstance(data["records"], dict) or \
           set(data["records"]) != set(RECORD_FIELDS) or not isinstance(data["digest"], str):
            raise ValueError("checkpoint schema mismatch")
        digest = data.pop("digest")
        if _digest(_canonical(data)) != digest or raw != _canonical({**data, "digest": digest}):
            raise ValueError("checkpoint digest or canonical encoding mismatch")
        if (not _temporary and source.name != f"checkpoint-r{data['revision']}-{digest}") or \
           (source / "checkpoint.md").read_bytes() != _markdown(data["revision"], digest):
            raise ValueError("checkpoint Markdown or version name mismatch")
        for row in data["features"]:
            if not isinstance(row, dict) or set(row) != set(FEATURE_FIELDS):
                raise ValueError("invalid feature record")
            _check_refs(row)
            if not isinstance(row["file_digests"], list):
                raise ValueError("invalid feature file digests")
            for entry in row["file_digests"]:
                if not isinstance(entry, dict) or set(entry) != {"path", "digest"}:
                    raise ValueError("invalid feature digest entry")
                _safe_path(entry["path"])
                if entry["digest"] is not None and (not isinstance(entry["digest"], str) or
                    len(entry["digest"]) != 64 or any(c not in "0123456789abcdef" for c in entry["digest"])):
                    raise ValueError("invalid feature digest")
        for table, rows in data["records"].items():
            if not isinstance(rows, list):
                raise ValueError("invalid record list")
            for row in rows:
                if not isinstance(row, dict) or set(row) != set(RECORD_FIELDS[table]):
                    raise ValueError("invalid record fields")
                _check_refs(row)
                if table == "bundles" and row["manifest"] is not None:
                    manifest = row["manifest"]
                    if _digest(_manifest_bytes(manifest)) != row["manifest_digest"]:
                        raise ValueError("bundle manifest digest mismatch")
                    for entry in manifest["files"]:
                        _safe_path(entry["path"])
        _validate_semantics(data["features"], data["records"])
        return Checkpoint(data["source_repository_id"], data["revision"], data["features"],
                          data["records"], digest)
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, DomainError) as exc:
        raise DomainError("INVALID_CHECKPOINT", "Checkpoint pair or structured records are invalid") from exc


def export_checkpoint(store: Store, owner: OwnerRef, target: Path) -> ExportReceipt:
    snapshot = store.read_export_snapshot(owner)
    portable = _portable(snapshot)
    digest = _digest(_canonical(portable))
    data = _canonical({**portable, "digest": digest})
    target = Path(target)
    if not target.is_dir() or target.is_symlink():
        raise DomainError("UNSAFE_PATH", "Export target must be an existing directory")
    _require_local_ext4(target)
    destination = target / f"checkpoint-r{snapshot['revision']}-{digest}"
    if destination.exists():
        try:
            checkpoint = validate_checkpoint(destination)
        except DomainError as exc:
            raise DomainError("CHECKPOINT_CONFLICT", "Published checkpoint is modified") from exc
        if checkpoint.digest != digest or (destination / "checkpoint.json").read_bytes() != data:
            raise DomainError("CHECKPOINT_CONFLICT", "Published checkpoint differs")
    else:
        staging = Path(tempfile.mkdtemp(prefix=".checkpoint-", dir=target))
        try:
            _write_fsynced(staging / "checkpoint.json", data)
            _write_fsynced(staging / "checkpoint.md", _markdown(snapshot["revision"], digest))
            _fsync_dir(staging)
            validate_checkpoint(staging, _temporary=True)
            if not _publish_directory(staging, destination):
                try:
                    checkpoint = validate_checkpoint(destination)
                except DomainError as exc:
                    raise DomainError("CHECKPOINT_CONFLICT", "Published checkpoint is modified") from exc
                if checkpoint.digest != digest or (destination / "checkpoint.json").read_bytes() != data:
                    raise DomainError("CHECKPOINT_CONFLICT", "Published checkpoint differs")
            _fsync_dir(target)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    validate_checkpoint(destination)
    store.commit_export(owner, snapshot["revision"], destination.name)
    return ExportReceipt(destination, snapshot["revision"], digest)


def _insert_records(conn: sqlite3.Connection, checkpoint: Checkpoint, mapped: dict[str, str],
                    unknown: set[str]) -> None:
    for row in checkpoint.features:
        values = {field: row[field] for field in FEATURE_COLUMNS}
        values.update(worktree=mapped.get(row["id"], ""),
                      state="unresolved" if row["id"] in unknown else "active",
                      portable_file_digests_json=_canonical(row["file_digests"]).decode())
        fields = tuple(values)
        conn.execute(f"INSERT INTO features({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})",
                     tuple(values.values()))
    for table in TABLES[1:]:
        for row in checkpoint.records[table]:
            values = {key: value for key, value in row.items() if key != "manifest"}
            if table == "assignments":
                values.update(owner_generation=0, state="unknown", external_start_seen=0, launch_ref=None)
            if table == "bundles":
                values["path"] = ""
            fields = tuple(values)
            conn.execute(f"INSERT INTO {table}({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})",
                         tuple(values.values()))


def import_checkpoint(repo: RepoIdentity, source: Path,
                      worktree_map: Mapping[str, Path] | None = None) -> ImportReport:
    checkpoint = validate_checkpoint(source)
    state_path = repo.common_dir / "coordinator" / "state.sqlite3"
    if state_path.parent.is_symlink():
        raise DomainError("UNSAFE_PATH", "Destination coordinator directory is a symlink")
    if state_path.exists():
        if open_store(repo).repository_id == checkpoint.source_repository_id:
            raise DomainError("STALE_INPUT", "Cannot import a checkpoint into its source repository")
        raise DomainError("NONEMPTY_STATE", "Destination coordinator state already exists")
    if state_path.parent.exists() and any(state_path.parent.iterdir()):
        raise DomainError("NONEMPTY_STATE", "Destination coordinator directory is not empty")
    ids = {row["id"] for row in checkpoint.features}
    mapping = dict(worktree_map or {})
    if set(mapping) - ids:
        raise DomainError("INVALID_CHECKPOINT", "Worktree map contains an unknown feature")
    mapped = {}
    for feature_id, path in mapping.items():
        path = Path(path).resolve()
        identity = resolve_repo(path)
        if identity.worktree != path or identity.common_dir != repo.common_dir.resolve():
            raise DomainError("INVALID_REPOSITORY", "Mapped worktree must share destination Git common directory")
        mapped[feature_id] = str(path)
    unknown = ids - set(mapped)
    for feature in checkpoint.features:
        feature_id = feature["id"]
        if feature_id not in mapped:
            continue
        worktree = Path(mapped[feature_id])
        entries = list(feature["file_digests"])
        for table in ("assignments", "bundles", "acceptances"):
            for row in checkpoint.records[table]:
                if row.get("feature_id") != feature_id:
                    continue
                for field in ("source_digests_json", "contract_files_json", "dependency_digests_json"):
                    if row.get(field):
                        entries.extend(json.loads(row[field]))
                if table == "bundles" and row["manifest"]:
                    entries.extend(row["manifest"]["files"])
        for entry in entries:
            path = (worktree / entry["path"]).resolve()
            if not path.is_relative_to(worktree):
                unknown.add(feature_id)
                break
            try:
                actual = _digest(path.read_bytes())
            except OSError:
                actual = None
            if actual != entry["digest"] or actual is None:
                unknown.add(feature_id)
                break
    # Validate references and foreign keys in memory before creating destination state.
    try:
        with closing(sqlite3.connect(":memory:")) as probe:
            probe.execute("PRAGMA foreign_keys=ON")
            probe.executescript(SCHEMA)
            _insert_records(probe, checkpoint, mapped, unknown)
    except sqlite3.Error as exc:
        raise DomainError("INVALID_CHECKPOINT", "Checkpoint records violate database schema") from exc
    _require_local_ext4(repo.common_dir)
    store = open_store(repo, create=True, exclusive=True)
    try:
        with closing(_connect(store.path, "rw")) as conn:
            conn.execute("BEGIN IMMEDIATE")
            meta = conn.execute("SELECT revision, origin_checkpoint_id FROM meta").fetchone()
            owner = conn.execute("SELECT generation, token_hash, state FROM owner").fetchone()
            if meta is None or tuple(meta) != (0, None) or owner is None or \
               tuple(owner) != (0, None, "released") or any(
                   conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None for table in TABLES):
                raise DomainError("NONEMPTY_STATE", "Destination state changed during import")
            _insert_records(conn, checkpoint, mapped, unknown)
            conn.execute("UPDATE meta SET revision=?, export_pending=1, origin_checkpoint_id=?",
                         (checkpoint.revision, checkpoint.digest))
            conn.commit()
    except DomainError:
        raise
    except sqlite3.Error as exc:
        raise DomainError("CORRUPT_STATE", "Cannot import validated checkpoint") from exc
    return ImportReport(store.repository_id, checkpoint.source_repository_id, checkpoint.digest,
                        mapped, sorted(unknown))
