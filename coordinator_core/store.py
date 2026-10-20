"""Fail-closed local SQLite state for a Git common directory."""

import hashlib
import hmac
import json
import os
import platform
import re
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from contextlib import closing
from pathlib import Path
from typing import Callable, TypeVar
from urllib.parse import quote

from .model import DomainError, OwnerRef
from .repo import RepoIdentity


SCHEMA_VERSION = 1
SCHEMA = """
CREATE TABLE meta (schema_version INTEGER NOT NULL, repository_id TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 0, export_revision INTEGER, export_dir_name TEXT,
 export_pending INTEGER NOT NULL DEFAULT 0, origin_checkpoint_id TEXT);
CREATE TABLE owner (generation INTEGER NOT NULL DEFAULT 0, token_hash TEXT,
 session_id TEXT, state TEXT NOT NULL DEFAULT 'released', acquired_at TEXT);
CREATE TABLE features (id TEXT PRIMARY KEY, mandate_revision INTEGER NOT NULL, outcome TEXT NOT NULL,
 exclusions_json TEXT NOT NULL CHECK(json_valid(exclusions_json)),
 constraints_json TEXT NOT NULL CHECK(json_valid(constraints_json)),
 completion_json TEXT NOT NULL CHECK(json_valid(completion_json)),
 authority_json TEXT NOT NULL CHECK(json_valid(authority_json)), branch TEXT NOT NULL,
 base_ref TEXT NOT NULL, base_commit TEXT NOT NULL, worktree TEXT NOT NULL,
 dirty_paths_json TEXT NOT NULL CHECK(json_valid(dirty_paths_json)),
 writable_paths_json TEXT NOT NULL CHECK(json_valid(writable_paths_json)), state TEXT NOT NULL,
 portable_file_digests_json TEXT CHECK(portable_file_digests_json IS NULL OR json_valid(portable_file_digests_json)));
CREATE TABLE decision_requests (id TEXT PRIMARY KEY, feature_id TEXT NOT NULL REFERENCES features(id),
 assignment_id TEXT, question_json TEXT NOT NULL CHECK(json_valid(question_json)),
 affected_json TEXT NOT NULL CHECK(json_valid(affected_json)), state TEXT NOT NULL,
 resolution_decision_id TEXT);
CREATE TABLE decisions (id TEXT PRIMARY KEY, feature_id TEXT NOT NULL REFERENCES features(id),
 mandate_revision INTEGER NOT NULL, source_kind TEXT NOT NULL, source_ref TEXT NOT NULL,
 reason TEXT NOT NULL, choice_json TEXT NOT NULL CHECK(json_valid(choice_json)), request_id TEXT);
CREATE TABLE assignments (id TEXT PRIMARY KEY, feature_id TEXT NOT NULL REFERENCES features(id),
 owner_generation INTEGER NOT NULL, idempotency_key TEXT NOT NULL UNIQUE,
 request_fingerprint TEXT NOT NULL, brief_ref TEXT NOT NULL, mandate_revision INTEGER NOT NULL,
 source_digests_json TEXT NOT NULL CHECK(json_valid(source_digests_json)),
 contract_files_json TEXT NOT NULL CHECK(json_valid(contract_files_json)), contract_digest TEXT NOT NULL,
 paths_json TEXT NOT NULL CHECK(json_valid(paths_json)), permission_mode TEXT NOT NULL,
 author_id TEXT NOT NULL, depends_on_json TEXT NOT NULL CHECK(json_valid(depends_on_json)),
 blocking_requests_json TEXT NOT NULL CHECK(json_valid(blocking_requests_json)), state TEXT NOT NULL,
 external_start_seen INTEGER NOT NULL DEFAULT 0, launch_ref TEXT,
 return_json TEXT CHECK(return_json IS NULL OR json_valid(return_json)));
CREATE TABLE events (id TEXT PRIMARY KEY, revision INTEGER NOT NULL, kind TEXT NOT NULL,
 subject_id TEXT, source_id TEXT, evidence_ref TEXT, payload_digest TEXT);
CREATE TABLE bundles (id TEXT PRIMARY KEY, feature_id TEXT NOT NULL REFERENCES features(id),
 manifest_digest TEXT NOT NULL, mandate_revision INTEGER NOT NULL,
 contract_files_json TEXT NOT NULL CHECK(json_valid(contract_files_json)), contract_digest TEXT NOT NULL,
 source_assignments_json TEXT NOT NULL CHECK(json_valid(source_assignments_json)), path TEXT NOT NULL);
CREATE TABLE reviews (id TEXT PRIMARY KEY, bundle_id TEXT NOT NULL REFERENCES bundles(id),
 reviewer_id TEXT NOT NULL, provenance_ref TEXT NOT NULL, provenance_class TEXT NOT NULL,
 finding_json TEXT NOT NULL CHECK(json_valid(finding_json)), critical_open INTEGER NOT NULL);
CREATE TABLE acceptances (id TEXT PRIMARY KEY, bundle_id TEXT NOT NULL REFERENCES bundles(id),
 review_id TEXT NOT NULL REFERENCES reviews(id),
 dependency_digests_json TEXT NOT NULL CHECK(json_valid(dependency_digests_json)), revision INTEGER NOT NULL);
"""
JSON_COLUMNS = {
    "features": ("exclusions_json", "constraints_json", "completion_json", "authority_json", "dirty_paths_json", "writable_paths_json", "portable_file_digests_json"),
    "decision_requests": ("question_json", "affected_json"),
    "decisions": ("choice_json",),
    "assignments": ("source_digests_json", "contract_files_json", "paths_json", "depends_on_json", "blocking_requests_json", "return_json"),
    "bundles": ("contract_files_json", "source_assignments_json"),
    "reviews": ("finding_json",),
    "acceptances": ("dependency_digests_json",),
}
TABLES = ("features", "decision_requests", "decisions", "assignments", "events", "bundles", "reviews", "acceptances")
T = TypeVar("T")


def read_mountinfo() -> str:
    return Path("/proc/self/mountinfo").read_text()


def _require_local_ext4(path: Path) -> None:
    if platform.system() != "Linux":
        raise DomainError("UNSUPPORTED_PLATFORM", "Local mutation requires Linux")
    try:
        mounts = []
        for line in read_mountinfo().splitlines():
            before, after = line.split(" - ", 1)
            mount_text = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), before.split()[4])
            mount = Path(mount_text)
            if path.is_relative_to(mount):
                mounts.append((len(mount.parts), after.split()[0]))
        if not mounts or max(mounts)[1] != "ext4":
            raise DomainError("UNSUPPORTED_FILESYSTEM", "Local mutation requires ext4")
    except (OSError, ValueError, IndexError) as exc:
        raise DomainError("UNSUPPORTED_FILESYSTEM", "Cannot verify filesystem type") from exc


def probe_sqlite_lock(directory: Path) -> bool:
    """Verify BEGIN IMMEDIATE contention from an independent process."""
    fd, name = tempfile.mkstemp(prefix=".coordinator-lock-", dir=directory)
    os.close(fd)
    path = Path(name)
    try:
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("CREATE TABLE probe (value INTEGER)")
            conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            code = ("import sqlite3,sys\n"
                    "try:\n c=sqlite3.connect(sys.argv[1],timeout=0)\n c.execute('BEGIN IMMEDIATE')\n sys.exit(1)\n"
                    "except sqlite3.OperationalError as e:\n sys.exit(0 if 'locked' in str(e).lower() else 2)\n")
            result = subprocess.run([sys.executable, "-c", code, str(path)], capture_output=True, timeout=5)
            conn.rollback()
            return result.returncode == 0
    except (OSError, sqlite3.Error, subprocess.SubprocessError):
        return False
    finally:
        path.unlink(missing_ok=True)


def _connect(path: Path, mode: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{quote(str(path))}?mode={mode}", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _check_store_path(path: Path) -> None:
    if path.parent.is_symlink() or path.is_symlink():
        raise DomainError("UNSAFE_PATH", "Coordinator state path is a symlink")
    if not path.is_file():
        raise DomainError("CORRUPT_STATE", "Coordinator database is missing or not a regular file")


def _check_state(conn: sqlite3.Connection) -> tuple[str, int]:
    try:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise DomainError("CORRUPT_STATE", "SQLite integrity check failed")
        if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise DomainError("CORRUPT_STATE", "Broken foreign key")
        rows = conn.execute("SELECT schema_version, repository_id, revision FROM meta").fetchall()
        if len(rows) != 1:
            raise DomainError("CORRUPT_STATE", "Missing or duplicate metadata")
        version, repository_id, revision = rows[0]
        if version != SCHEMA_VERSION:
            raise DomainError("INCOMPATIBLE_SCHEMA", "Unsupported coordinator schema")
        if not repository_id or revision < 0 or conn.execute("SELECT count(*) FROM owner").fetchone()[0] != 1:
            raise DomainError("CORRUPT_STATE", "Invalid coordinator metadata")
        for table, columns in JSON_COLUMNS.items():
            for column in columns:
                for row in conn.execute(f"SELECT {column} FROM {table} WHERE {column} IS NOT NULL"):
                    json.loads(row[0])
        return repository_id, revision
    except (sqlite3.Error, ValueError, TypeError) as exc:
        raise DomainError("CORRUPT_STATE", "Invalid coordinator database") from exc


class Store:
    repository_id: str

    def __init__(self, path: Path, repository_id: str):
        self.path = path
        self.repository_id = repository_id

    def read_snapshot(self) -> dict:
        _check_store_path(self.path)
        try:
            with closing(_connect(self.path, "ro")) as conn:
                conn.execute("BEGIN")
                repository_id, revision = _check_state(conn)
                meta = dict(conn.execute("SELECT * FROM meta").fetchone())
                owner = dict(conn.execute("SELECT generation, session_id, state, acquired_at FROM owner").fetchone())
                snapshot = {**meta, "repository_id": repository_id, "revision": revision, "owner": owner}
                for table in TABLES:
                    snapshot[table] = [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]
                return snapshot
        except sqlite3.Error as exc:
            raise DomainError("CORRUPT_STATE", "Cannot read coordinator database") from exc

    @staticmethod
    def _assert_owner(conn: sqlite3.Connection, owner: OwnerRef) -> None:
        generation, token_hash, state = conn.execute("SELECT generation, token_hash, state FROM owner").fetchone()
        actual = hashlib.sha256(owner.token.encode()).hexdigest()
        if generation != owner.generation or state != "active" or not token_hash or not hmac.compare_digest(token_hash, actual):
            raise DomainError("OWNERSHIP_CONFLICT", "Owner generation or token is stale")

    def read_export_snapshot(self, owner: OwnerRef) -> dict:
        _check_store_path(self.path)
        try:
            with closing(_connect(self.path, "ro")) as conn:
                conn.execute("BEGIN")
                repository_id, revision = _check_state(conn)
                self._assert_owner(conn, owner)
                snapshot = {"repository_id": repository_id, "revision": revision}
                for table in TABLES:
                    snapshot[table] = [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]
                return snapshot
        except sqlite3.Error as exc:
            raise DomainError("CORRUPT_STATE", "Cannot read export snapshot") from exc

    def commit_export(self, owner: OwnerRef, revision: int, directory_name: str) -> None:
        _check_store_path(self.path)
        _require_local_ext4(self.path.parent)
        try:
            with closing(_connect(self.path, "rw")) as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    _check_state(conn)
                    self._assert_owner(conn, owner)
                    if conn.execute("SELECT revision FROM meta").fetchone()[0] != revision:
                        raise DomainError("STALE_INPUT", "Domain revision changed during export")
                    conn.execute("UPDATE meta SET export_revision=?, export_dir_name=?, export_pending=0",
                                 (revision, directory_name))
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise
        except sqlite3.Error as exc:
            raise DomainError("CORRUPT_STATE", "Cannot commit export pointer") from exc

    def write(self, owner: OwnerRef, change: Callable[[sqlite3.Connection], T]) -> T:
        """Run a domain change; callbacks may not control transactions/savepoints."""
        return self._change(change, owner)

    def _claim_unowned(self, change: Callable[[sqlite3.Connection], T]) -> T:
        """Run the owner claim under the same transaction rules as fenced writes."""
        return self._change(change, None)

    def _change(self, change: Callable[[sqlite3.Connection], T], owner: OwnerRef | None) -> T:
        _check_store_path(self.path)
        _require_local_ext4(self.path.parent)
        try:
            with closing(_connect(self.path, "rw")) as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    _check_state(conn)
                    if owner is not None:
                        self._assert_owner(conn, owner)
                    def deny_transaction(action, arg1, arg2, database, trigger):
                        if action in (sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT):
                            return sqlite3.SQLITE_DENY
                        return sqlite3.SQLITE_OK

                    conn.set_authorizer(deny_transaction)
                    try:
                        result = change(conn)
                    except sqlite3.DatabaseError as exc:
                        if getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_AUTH:
                            raise DomainError("INVALID_TRANSACTION", "Callback cannot control transactions") from exc
                        raise
                    finally:
                        conn.set_authorizer(None)
                    revision = conn.execute("SELECT revision FROM meta").fetchone()[0] + 1
                    conn.execute("UPDATE meta SET revision=?, export_pending=1", (revision,))
                    if conn.execute("SELECT 1 FROM events WHERE revision=? LIMIT 1", (revision,)).fetchone() is None:
                        conn.execute("INSERT INTO events(id, revision, kind) VALUES (?, ?, 'mutation')", (str(uuid.uuid4()), revision))
                    conn.commit()
                    return result
                except BaseException:
                    conn.rollback()
                    raise
        except sqlite3.Error as exc:
            raise DomainError("CORRUPT_STATE", "Cannot write coordinator database") from exc


def open_store(repo: RepoIdentity, *, create: bool = False, exclusive: bool = False) -> Store:
    directory = repo.common_dir / "coordinator"
    path = directory / "state.sqlite3"
    if directory.is_symlink() or path.is_symlink():
        raise DomainError("UNSAFE_PATH", "Coordinator state path is a symlink")
    if not create and not path.exists():
        raise DomainError("UNINITIALIZED", "Coordinator has not been initialized")
    if create:
        _require_local_ext4(directory)
        directory.mkdir(mode=0o700, exist_ok=True)
        if not directory.is_dir() or directory.is_symlink() or path.is_symlink():
            raise DomainError("UNSAFE_PATH", "Coordinator state path is unsafe")
        if not probe_sqlite_lock(directory):
            raise DomainError("UNSUPPORTED_FILESYSTEM", "SQLite process lock probe failed")
        if exclusive and path.exists():
            raise DomainError("NONEMPTY_STATE", "Coordinator database was initialized concurrently")
        if not path.exists():
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                os.close(fd)
                with closing(_connect(path, "rw")) as conn:
                    conn.executescript(SCHEMA)
                    conn.execute("INSERT INTO meta(schema_version, repository_id) VALUES (?, ?)", (SCHEMA_VERSION, str(uuid.uuid4())))
                    conn.execute("INSERT INTO owner DEFAULT VALUES")
                    conn.commit()
            except FileExistsError as exc:
                if exclusive:
                    raise DomainError("NONEMPTY_STATE", "Coordinator database was initialized concurrently") from exc
            except (OSError, sqlite3.Error) as exc:
                path.unlink(missing_ok=True)
                raise DomainError("CORRUPT_STATE", "Cannot initialize coordinator database") from exc
    if path.is_symlink() or not path.is_file():
        raise DomainError("UNSAFE_PATH", "Coordinator database is not a regular file")
    try:
        with closing(_connect(path, "ro")) as conn:
            repository_id, _ = _check_state(conn)
    except sqlite3.Error as exc:
        raise DomainError("CORRUPT_STATE", "Cannot open coordinator database") from exc
    return Store(path, repository_id)
