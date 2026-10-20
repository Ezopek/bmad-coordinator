import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from coordinator_core.model import DecisionInput, DomainError, FeatureInput, OwnerRef
from coordinator_core.repo import resolve_repo
from coordinator_core.service import Coordinator
from coordinator_core.store import open_store
from tests.support import ext4_mountinfo


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.main = self.root / "main"
        self.main.mkdir()
        git("init", "-q", cwd=self.main)
        git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-qm", "initial", cwd=self.main)
        self.repo = resolve_repo(self.main)
        with patch("coordinator_core.store.read_mountinfo", return_value=ext4_mountinfo(self.root)):
            self.store = open_store(self.repo, create=True)
        self.coordinator = Coordinator(self.store)
        self.token_path = self.root / "owner.token"

    def claim(self, path=None):
        with patch("coordinator_core.store.read_mountinfo", return_value=ext4_mountinfo(self.root)):
            return self.coordinator.claim("session", path or self.token_path)

    def mutate(self, method, *args):
        with patch("coordinator_core.store.read_mountinfo", return_value=ext4_mountinfo(self.root)):
            return method(*args)

    def feature(self):
        return FeatureInput(outcome="ship slice", exclusions=["deploy"], constraints=["local"],
                            completion=["tests pass"], authority=["local edits"], branch="main",
                            base_ref="HEAD", base_commit="abc", worktree=str(self.main),
                            dirty_paths=[], writable_scopes=["coordinator/**"])

    def test_two_independent_claim_processes_one_winner(self):
        script = """import pathlib, sys, time
from unittest.mock import patch
from coordinator_core.model import DomainError
from coordinator_core.repo import resolve_repo
from coordinator_core.store import open_store
from coordinator_core.service import Coordinator
from tests.support import ext4_mountinfo
root, main, ready, go, token = map(pathlib.Path, sys.argv[1:])
store = open_store(resolve_repo(main))
ready.touch()
while not go.exists(): time.sleep(.005)
try:
 with patch('coordinator_core.store.read_mountinfo', return_value=ext4_mountinfo(root)):
  Coordinator(store).claim(token.name, token)
except DomainError as error:
 sys.exit(3 if error.code == 'OWNERSHIP_CONFLICT' else 2)
"""
        processes = []
        for n in (1, 2):
            process = subprocess.Popen([sys.executable, "-c", script, str(self.root), str(self.main),
                                        str(self.root / f"ready{n}"), str(self.root / "go"),
                                        str(self.root / f"token{n}")], cwd=Path(__file__).parents[1])
            processes.append(process)
        try:
            for _ in range(1000):
                if all((self.root / f"ready{n}").exists() for n in (1, 2)):
                    break
                __import__("time").sleep(.005)
            else:
                self.fail("claim processes did not reach barrier")
            (self.root / "go").touch()
            self.assertEqual([0, 3], sorted(p.wait(timeout=10) for p in processes))
            self.assertEqual(1, len(list(self.root.glob("token[12]"))))
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.wait()

    def test_stale_generation_cannot_mutate(self):
        first = self.claim()
        self.mutate(self.coordinator.release, first)
        second = self.claim(self.root / "second.token")
        before = self.store.read_snapshot()["revision"]
        with self.assertRaises(DomainError) as caught:
            self.mutate(self.coordinator.create_feature, first, self.feature())
        self.assertEqual("OWNERSHIP_CONFLICT", caught.exception.code)
        self.assertEqual(before, self.store.read_snapshot()["revision"])
        self.assertEqual(first.generation + 1, second.generation)

    def test_clean_release_with_no_assignments(self):
        owner = self.claim()
        self.mutate(self.coordinator.release, owner)
        self.assertEqual("released", self.coordinator.recover_status().state)
        self.assertEqual("released", self.store.read_snapshot()["owner"]["state"])

    def test_expired_heartbeat_does_not_take_over(self):
        self.claim()
        with closing(sqlite3.connect(self.store.path)) as conn, conn:
            conn.execute("UPDATE owner SET acquired_at='2000-01-01T00:00:00+00:00'")
        self.assertEqual("blocked_launcher_unknown", self.coordinator.recover_status().state)
        with self.assertRaises(DomainError) as caught:
            self.claim(self.root / "second.token")
        self.assertEqual("OWNERSHIP_CONFLICT", caught.exception.code)

    def test_operator_decision_stays_reported(self):
        owner = self.claim()
        feature_id = self.mutate(self.coordinator.create_feature, owner, self.feature())
        decision_id = self.mutate(self.coordinator.record_decision, owner,
            DecisionInput(feature_id=feature_id, mandate_revision=1, source_kind="operator-reported",
                          source_ref="conversation:message-42", reason="scope", choice={"value": "yes"}))
        detail = self.coordinator.detail("decision", decision_id)
        self.assertEqual("operator-reported", detail["source_kind"])
        self.assertEqual("reported", detail["provenance_class"])
        self.assertEqual(1, self.coordinator.detail("feature", feature_id)["mandate_revision"])
        with self.assertRaises(DomainError):
            self.mutate(self.coordinator.record_decision, owner,
                DecisionInput(feature_id=feature_id, mandate_revision=1, source_kind="operator-reported",
                              source_ref="", reason="bad", choice={"value": "yes"}))

    def test_mandate_revision_changes_only_on_revise(self):
        owner = self.claim()
        feature_id = self.mutate(self.coordinator.create_feature, owner, self.feature())
        decision_id = self.mutate(self.coordinator.record_decision, owner,
            DecisionInput(feature_id=feature_id, mandate_revision=1, source_kind="operator-reported",
                          source_ref="conversation:message-42", reason="new scope", choice={"value": "yes"}))
        self.assertEqual(2, self.mutate(self.coordinator.revise_mandate, owner, feature_id, 1,
                                        {"outcome": "ship both slices"}, decision_id))
        with self.assertRaises(DomainError) as caught:
            self.mutate(self.coordinator.revise_mandate, owner, feature_id, 1,
                        {"outcome": "stale"}, decision_id)
        self.assertEqual("REVISION_CONFLICT", caught.exception.code)
        self.assertEqual(2, self.coordinator.detail("feature", feature_id)["mandate_revision"])

    def test_failed_claim_cleans_token_file(self):
        with patch("coordinator_core.store.read_mountinfo", return_value=ext4_mountinfo(self.root)), \
             patch("coordinator_core.service.os.fsync", side_effect=OSError("sync failed")):
            with self.assertRaises(DomainError):
                self.coordinator.claim("session", self.token_path)
        self.assertFalse(self.token_path.exists())
        self.assertEqual("released", self.store.read_snapshot()["owner"]["state"])

    def test_status_is_bounded_and_token_free(self):
        owner = self.claim()
        feature_id = self.mutate(self.coordinator.create_feature, owner,
                                 replace(self.feature(), branch="b" * 5000,
                                         outcome="ship slice" + "x" * 5000,
                                         exclusions=["deploy" + "x" * 5000],
                                         authority=["local edits" + "x" * 5000]))
        status = self.coordinator.status()
        feature = status["features"][0]
        self.assertEqual(owner.generation, status["owner"]["generation"])
        self.assertIn(feature_id, status["feature_ids"])
        self.assertIn("detail", feature)
        self.assertEqual({"kind": "feature", "id": feature_id}, feature["detail"])
        self.assertTrue(feature["mandate_summary"]["outcome"].startswith("ship slice"))
        self.assertTrue(feature["mandate_summary"]["exclusions"].startswith('["deploy'))
        self.assertTrue(feature["mandate_summary"]["authority"].startswith('["local edits'))
        self.assertTrue(all(len(value) <= 160 for value in feature["mandate_summary"].values()))
        self.assertNotIn(owner.token, json.dumps(status))
        self.assertNotIn("x" * 200, json.dumps(status))
        self.assertLess(len(json.dumps(status)), 2000)
        self.assertEqual("ship slice" + "x" * 5000, self.coordinator.detail("feature", feature_id)["outcome"])
        self.assertEqual(0o600, self.token_path.stat().st_mode & 0o777)
        self.assertEqual({"token": owner.token, "generation": owner.generation}, json.loads(self.token_path.read_text()))


if __name__ == "__main__":
    unittest.main()
