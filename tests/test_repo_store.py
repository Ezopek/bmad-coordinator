import hashlib
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from coordinator_core.model import DomainError, OwnerRef
from coordinator_core.repo import resolve_artifact, resolve_repo
from coordinator_core.store import open_store
from tests.support import ext4_mountinfo


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class RepoStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.main = self.root / "main"
        self.main.mkdir()
        git("init", "-q", cwd=self.main)
        git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-qm", "initial", cwd=self.main)
        self.repo = resolve_repo(self.main)

    def mountinfo(self, fstype="ext4"):
        return ext4_mountinfo(self.root, fstype)

    def test_linked_worktrees_share_common_dir(self):
        linked = self.root / "linked"
        git("worktree", "add", "--detach", "-q", str(linked), cwd=self.main)
        self.assertEqual(self.repo.common_dir, resolve_repo(linked).common_dir)
        self.assertNotEqual(self.repo.worktree, resolve_repo(linked).worktree)
        with patch("coordinator_core.store.read_mountinfo", return_value=self.mountinfo()):
            first = open_store(self.repo, create=True)
        second = open_store(resolve_repo(linked))
        self.assertEqual(first.repository_id, second.repository_id)

    def test_no_db_created_by_read(self):
        db = self.repo.common_dir / "coordinator" / "state.sqlite3"
        with self.assertRaises(DomainError) as caught:
            open_store(self.repo, create=False)
        self.assertEqual("UNINITIALIZED", caught.exception.code)
        self.assertFalse(db.exists())
        self.assertFalse(db.parent.exists())

    def test_corrupt_and_future_schema_fail_closed(self):
        db = self.repo.common_dir / "coordinator" / "state.sqlite3"
        db.parent.mkdir()
        db.write_bytes(b"sentinel: definitely not SQLite")
        with self.assertRaises(DomainError) as caught:
            open_store(self.repo)
        self.assertEqual("CORRUPT_STATE", caught.exception.code)
        self.assertEqual(b"sentinel: definitely not SQLite", db.read_bytes())
        db.unlink()
        with patch("coordinator_core.store.read_mountinfo", return_value=self.mountinfo()):
            open_store(self.repo, create=True)
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("UPDATE meta SET schema_version=999")
        with self.assertRaises(DomainError) as caught:
            open_store(self.repo)
        self.assertEqual("INCOMPATIBLE_SCHEMA", caught.exception.code)

    def test_symlink_and_parent_escape_rejected(self):
        outside = self.root / "outside.txt"
        outside.write_bytes(b"sentinel")
        (self.main / "link.txt").symlink_to(outside)
        for candidate in (self.main / "link.txt", self.main / ".." / "outside.txt"):
            with self.subTest(candidate=candidate), self.assertRaises(DomainError) as caught:
                resolve_artifact(self.repo, candidate)
            self.assertEqual("UNSAFE_PATH", caught.exception.code)
        self.assertEqual(b"sentinel", outside.read_bytes())

    def test_relative_artifact_path_uses_registered_worktree(self):
        artifact = self.main / "inside.txt"
        artifact.write_text("inside")
        self.assertEqual(artifact, resolve_artifact(self.repo, Path("inside.txt")))
        with self.assertRaises(DomainError) as caught:
            resolve_artifact(self.repo, Path("../outside.txt"))
        self.assertEqual("UNSAFE_PATH", caught.exception.code)

    def test_symlink_database_rejected_without_touching_target(self):
        outside = self.root / "outside.db"
        outside.write_bytes(b"sentinel")
        db = self.repo.common_dir / "coordinator" / "state.sqlite3"
        db.parent.mkdir()
        db.symlink_to(outside)
        with self.assertRaises(DomainError) as caught:
            open_store(self.repo, create=True)
        self.assertEqual("UNSAFE_PATH", caught.exception.code)
        self.assertEqual(b"sentinel", outside.read_bytes())

    def test_non_ext4_or_failed_lock_probe_rejected(self):
        db = self.repo.common_dir / "coordinator" / "state.sqlite3"
        with patch("coordinator_core.store.read_mountinfo", return_value=self.mountinfo("nfs")):
            with self.assertRaises(DomainError) as caught:
                open_store(self.repo, create=True)
        self.assertEqual("UNSUPPORTED_FILESYSTEM", caught.exception.code)
        self.assertFalse(db.exists())
        with patch("coordinator_core.store.read_mountinfo", return_value=self.mountinfo()), patch("coordinator_core.store.probe_sqlite_lock", return_value=False):
            with self.assertRaises(DomainError) as caught:
                open_store(self.repo, create=True)
        self.assertEqual("UNSUPPORTED_FILESYSTEM", caught.exception.code)
        self.assertFalse(db.exists())

    def test_nested_unsupported_mount_rejects_init_and_later_write(self):
        nested = f"37 36 0:33 / {self.repo.common_dir / 'coordinator'} rw - nfs server:/share rw\n"
        with patch("coordinator_core.store.read_mountinfo", return_value=self.mountinfo() + nested):
            with self.assertRaises(DomainError) as caught:
                open_store(self.repo, create=True)
        self.assertEqual("UNSUPPORTED_FILESYSTEM", caught.exception.code)
        with patch("coordinator_core.store.read_mountinfo", return_value=self.mountinfo()):
            store = open_store(self.repo, create=True)
        with patch("coordinator_core.store.read_mountinfo", return_value=self.mountinfo() + nested):
            with self.assertRaises(DomainError) as caught:
                store.write(OwnerRef(token="unused", generation=0), lambda conn: None)
        self.assertEqual("UNSUPPORTED_FILESYSTEM", caught.exception.code)
        self.assertEqual(0, store.read_snapshot()["revision"])

    def test_snapshot_pins_revision_across_concurrent_commit(self):
        with patch("coordinator_core.store.read_mountinfo", return_value=self.mountinfo()):
            store = open_store(self.repo, create=True)
        db = self.repo.common_dir / "coordinator" / "state.sqlite3"
        finished = threading.Event()
        writer_errors = []

        def write_later():
            try:
                with closing(sqlite3.connect(db, timeout=2)) as conn, conn:
                    conn.execute("UPDATE meta SET revision=1")
                    conn.execute("INSERT INTO events(id, revision, kind) VALUES ('later', 1, 'later')")
            except Exception as exc:
                writer_errors.append(exc)
            finally:
                finished.set()

        class InterleaveTables:
            def __iter__(self):
                worker = threading.Thread(target=write_later)
                worker.start()
                self.worker = worker
                finished.wait(0.2)
                return iter(("events",))

        interleave = InterleaveTables()
        with patch("coordinator_core.store.TABLES", interleave):
            snapshot = store.read_snapshot()
        interleave.worker.join(3)
        self.assertFalse(writer_errors)
        self.assertTrue(finished.is_set())
        self.assertEqual((0, []), (snapshot["revision"], snapshot["events"]))

    def test_store_rejects_db_or_directory_symlink_after_open(self):
        for link_directory in (False, True):
            with self.subTest(link_directory=link_directory):
                with patch("coordinator_core.store.read_mountinfo", return_value=self.mountinfo()):
                    store = open_store(self.repo, create=True)
                directory = self.repo.common_dir / "coordinator"
                db = directory / "state.sqlite3"
                if link_directory:
                    saved = self.root / "saved-coordinator"
                    directory.rename(saved)
                    directory.symlink_to(saved, target_is_directory=True)
                else:
                    saved = self.root / "saved-state.sqlite3"
                    db.rename(saved)
                    db.symlink_to(saved)
                before = saved.read_bytes() if not link_directory else (saved / "state.sqlite3").read_bytes()
                try:
                    with self.assertRaises(DomainError) as caught:
                        store.read_snapshot()
                    self.assertEqual("UNSAFE_PATH", caught.exception.code)
                    with self.assertRaises(DomainError) as caught:
                        store.write(OwnerRef(token="unused", generation=0), lambda conn: None)
                    self.assertEqual("UNSAFE_PATH", caught.exception.code)
                    self.assertEqual(before, saved.read_bytes() if not link_directory else (saved / "state.sqlite3").read_bytes())
                finally:
                    if link_directory:
                        directory.unlink()
                        saved.rename(directory)
                    else:
                        db.unlink()
                        saved.rename(db)

    def test_callback_cannot_commit_or_rollback_transaction(self):
        with patch("coordinator_core.store.read_mountinfo", return_value=self.mountinfo()):
            store = open_store(self.repo, create=True)
        db = self.repo.common_dir / "coordinator" / "state.sqlite3"
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("UPDATE owner SET generation=1, token_hash=?, state='active'", (hashlib.sha256(b"secret").hexdigest(),))
        owner = OwnerRef(token="secret", generation=1)
        for action in ("commit", "rollback"):
            with self.subTest(action=action):
                with closing(sqlite3.connect(db)) as conn, conn:
                    conn.execute("UPDATE meta SET revision=0")
                def escape(conn):
                    conn.execute("UPDATE meta SET revision=77")
                    getattr(conn, action)()
                    raise ValueError("callback failed")
                with patch("coordinator_core.store.read_mountinfo", return_value=self.mountinfo()):
                    with self.assertRaises(DomainError) as caught:
                        store.write(owner, escape)
                self.assertEqual("INVALID_TRANSACTION", caught.exception.code)
                self.assertEqual(0, store.read_snapshot()["revision"])

    def test_write_fences_owner_and_rolls_back(self):
        with patch("coordinator_core.store.read_mountinfo", return_value=self.mountinfo()):
            store = open_store(self.repo, create=True)
        db = self.repo.common_dir / "coordinator" / "state.sqlite3"
        owner = OwnerRef(token="secret", generation=1)
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("UPDATE owner SET generation=1, token_hash=?, state='active'", (hashlib.sha256(b"secret").hexdigest(),))
        with patch("coordinator_core.store.read_mountinfo", return_value=self.mountinfo()):
            self.assertEqual("ok", store.write(owner, lambda conn: "ok"))
            self.assertEqual(1, store.read_snapshot()["revision"])
            self.assertEqual(1, len(store.read_snapshot()["events"]))
            def abort(conn):
                conn.execute("INSERT INTO events(id, revision, kind) VALUES ('rolled-back', 2, 'test')")
                raise ValueError("abort")
            with self.assertRaises(ValueError):
                store.write(owner, abort)
            self.assertEqual(1, store.read_snapshot()["revision"])
            self.assertEqual(1, len(store.read_snapshot()["events"]))
            with self.assertRaises(DomainError) as caught:
                store.write(OwnerRef(token="wrong", generation=1), lambda conn: None)
            self.assertEqual("OWNERSHIP_CONFLICT", caught.exception.code)
            self.assertEqual(1, store.read_snapshot()["revision"])

    def test_write_refuses_schema_changed_after_store_open(self):
        with patch("coordinator_core.store.read_mountinfo", return_value=self.mountinfo()):
            store = open_store(self.repo, create=True)
        db = self.repo.common_dir / "coordinator" / "state.sqlite3"
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("UPDATE owner SET generation=1, token_hash=?, state='active'", (hashlib.sha256(b"secret").hexdigest(),))
            conn.execute("UPDATE meta SET schema_version=999")
        with patch("coordinator_core.store.read_mountinfo", return_value=self.mountinfo()):
            with self.assertRaises(DomainError) as caught:
                store.write(OwnerRef(token="secret", generation=1), lambda conn: conn.execute("UPDATE meta SET revision=42"))
        self.assertEqual("INCOMPATIBLE_SCHEMA", caught.exception.code)
        with closing(sqlite3.connect(db)) as conn, conn:
            self.assertEqual(0, conn.execute("SELECT revision FROM meta").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
