import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from coordinator_core.model import AssignmentInput, BundleInput, FeatureInput, ReturnEvidence, ReviewEvidence
from coordinator_core.repo import resolve_repo
from coordinator_core.service import Coordinator
from coordinator_core.store import open_store


PACKAGE = Path(__file__).parents[1]


class Observed:
    def classify_return(self, event):
        return "observed"

    def classify_review(self, event):
        return "observed"


class CLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=PACKAGE)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "main"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=Test", "-c",
                        "user.email=test@example.invalid", "commit", "--allow-empty", "-qm", "initial"], check=True)
        self.token = self.root / "owner.token"

    def cli(self, *args, repo=None, data=None, code=0):
        input_text = json.dumps(data) if isinstance(data, dict) else data
        result = subprocess.run([sys.executable, "-m", "coordinator_core", "--repo", str(repo or self.repo), *args],
                                input=input_text, text=True, capture_output=True, cwd=PACKAGE)
        self.assertEqual(code, result.returncode, (result.stdout, result.stderr))
        stream = result.stdout if code == 0 else result.stderr
        self.assertEqual("", result.stderr if code == 0 else result.stdout)
        self.assertEqual(1, len(stream.splitlines()), stream)
        return json.loads(stream)

    def init_claim(self):
        self.cli("init")
        return self.cli("claim", "--session", "s1", "--token-file", str(self.token))

    def feature(self):
        return self.cli("feature", "add", "--token-file", str(self.token), data={
            "outcome": "ship", "exclusions": [], "constraints": [], "completion": [], "authority": [],
            "branch": "main", "base_ref": "HEAD", "base_commit": "abc", "worktree": str(self.repo),
            "dirty_paths": [], "writable_scopes": ["*.md"]})["id"]

    def seed_bundle(self):
        self.init_claim()
        feature_id = self.feature()
        for name in ("brief.md", "contract.md", "artifact.md"):
            (self.repo / name).write_text(name)
        digest = lambda name: hashlib.sha256((self.repo / name).read_bytes()).hexdigest()
        contracts = [{"path": "contract.md", "digest": digest("contract.md")}]
        contract_digest = hashlib.sha256(json.dumps(contracts, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        owner_data = json.loads(self.token.read_text())
        from coordinator_core.model import OwnerRef
        owner = OwnerRef(**owner_data)
        coordinator = Coordinator(open_store(resolve_repo(self.repo)), Observed())
        assignment = coordinator.create_assignment(owner, AssignmentInput(
            feature_id, "key", "brief.md", 1, [{"path": "brief.md", "digest": digest("brief.md")}],
            contracts, contract_digest, ["artifact.md"], "workspace-write", "author", [], []))
        coordinator.reserve_launch(owner, assignment)
        coordinator.record_return(owner, assignment, ReturnEvidence(
            "worker", "return:1", [{"path": "artifact.md", "digest": digest("artifact.md")}],
            [], [], "completed", None, [], [], hashlib.sha256(b"return").hexdigest()))
        bundle = coordinator.capture_bundle(owner, BundleInput(feature_id, ["artifact.md"], [], [assignment], contracts))
        return coordinator, owner, bundle

    def test_readonly_missing_state_and_invalid_json_do_not_create_db(self):
        for args in (("status",), ("recover",), ("verify",), ("detail", "feature", "missing"),
                     ("assignment", "retry", "missing")):
            self.assertEqual("uninitialized", self.cli(*args)["state"])
        self.assertFalse((self.repo / ".git" / "coordinator").exists())
        self.cli("init")
        self.assertEqual("INVALID_INPUT", self.cli("feature", "add", "--token-file", str(self.token),
                                                  data='{"a":1}{"b":2}', code=2)["error"])
        self.assertEqual("INVALID_INPUT", self.cli("feature", "add", "--token-file", str(self.token),
                                                  data="[]", code=2)["error"])

    def test_linked_worktree_shares_owner_and_init_is_exclusive(self):
        linked = self.root / "linked"
        subprocess.run(["git", "-C", str(self.repo), "worktree", "add", "-qb", "linked", str(linked)], check=True)
        first = self.init_claim()
        self.assertEqual(1, first["generation"])
        self.assertEqual("OWNERSHIP_CONFLICT", self.cli("claim", "--session", "s2", "--token-file",
                                                       str(self.root / "other.token"), repo=linked, code=3)["error"])
        self.assertEqual("NONEMPTY_STATE", self.cli("init", repo=linked, code=2)["error"])
        self.assertEqual(self.cli("status")["repository_id"], self.cli("status", repo=linked)["repository_id"])

    def test_strict_shapes_and_stale_token(self):
        self.init_claim()
        feature = self.feature()
        malformed = {"outcome": "ship", "exclusions": [], "constraints": [], "completion": [], "authority": [],
                     "branch": {"bad": "type"}, "base_ref": "HEAD", "base_commit": "abc",
                     "worktree": str(self.repo), "dirty_paths": [], "writable_scopes": ["*.md"]}
        self.assertEqual("INVALID_INPUT", self.cli("feature", "add", "--token-file", str(self.token),
                                                  data=malformed, code=2)["error"])
        self.assertEqual(1, self.cli("status")["counts"]["features"])
        self.assertEqual("INVALID_INPUT", self.cli("feature", "add", "--token-file", str(self.token),
                                                  data={"outcome": "x", "verified": True}, code=2)["error"])
        self.assertEqual("INVALID_INPUT", self.cli("decision", "add", "--token-file", str(self.token),
                                                  data={"feature_id": feature, "mandate_revision": 1,
                                                        "source_kind": "operator-reported", "source_ref": "chat:1",
                                                        "reason": "choose", "choice": "yes", "extra": 1}, code=2)["error"])
        bad = self.root / "bad.token"
        bad.write_text(json.dumps({"token": "wrong", "generation": 1}))
        bad.chmod(0o600)
        self.assertEqual("OWNERSHIP_CONFLICT", self.cli("release", "--token-file", str(bad), code=3)["error"])
        alias = self.root / "alias.token"
        alias.symlink_to(self.token)
        self.assertEqual("UNSAFE_PATH", self.cli("release", "--token-file", str(alias), code=2)["error"])

    def test_feature_close_and_status_all(self):
        self.init_claim()
        feature_id = self.feature()
        kept = self.feature()
        decision = self.cli("decision", "add", "--token-file", str(self.token), data={
            "feature_id": feature_id, "mandate_revision": 1, "source_kind": "operator-reported",
            "source_ref": "chat:1", "reason": "dropped", "choice": {"outcome": "abandoned"}})["id"]
        close = {"expected_revision": 1, "decision_id": decision, "resolution": "abandoned"}
        self.assertEqual("INVALID_INPUT", self.cli("feature", "close", feature_id, "--token-file", str(self.token),
                                                  data={**close, "extra": 1}, code=2)["error"])
        bad = self.root / "bad.token"
        bad.write_text(json.dumps({"token": "wrong", "generation": 1}))
        bad.chmod(0o600)
        self.assertEqual("OWNERSHIP_CONFLICT", self.cli("feature", "close", feature_id, "--token-file", str(bad),
                                                       data=close, code=3)["error"])
        self.assertEqual({"state": "abandoned"}, self.cli("feature", "close", feature_id, "--token-file",
                                                          str(self.token), data=close))
        status = self.cli("status")
        self.assertEqual([kept], status["feature_ids"])
        self.assertEqual([feature_id], status["closed_feature_ids"])
        self.assertEqual({feature_id: "abandoned", kept: "active"},
                         {row["id"]: row["state"] for row in self.cli("status", "--all")["features"]})
        self.assertEqual("FEATURE_CLOSED", self.cli("decision", "add", "--token-file", str(self.token), data={
            "feature_id": feature_id, "mandate_revision": 1, "source_kind": "operator-reported",
            "source_ref": "chat:2", "reason": "again", "choice": {}}, code=4)["error"])

    def test_close_is_refused_while_a_driver_holds_the_lock(self):
        from coordinator_core import drive
        self.init_claim()
        feature_id = self.feature()
        decision = self.cli("decision", "add", "--token-file", str(self.token), data={
            "feature_id": feature_id, "mandate_revision": 1, "source_kind": "operator-reported",
            "source_ref": "chat:1", "reason": "merged", "choice": {}})["id"]
        fd = drive.acquire_lock(resolve_repo(self.repo), exclusive=True)
        try:
            self.assertEqual("OWNERSHIP_CONFLICT", self.cli(
                "feature", "close", feature_id, "--token-file", str(self.token),
                data={"expected_revision": 1, "decision_id": decision, "resolution": "completed"}, code=3)["error"])
        finally:
            os.close(fd)
        self.assertEqual("active", self.cli("detail", "feature", feature_id)["state"])

    def test_corrupt_state_has_one_json_error(self):
        self.cli("init")
        (self.repo / ".git" / "coordinator" / "state.sqlite3").write_bytes(b"broken")
        self.assertEqual("CORRUPT_STATE", self.cli("status", code=5)["error"])

    def test_revise_rejects_nonstring_decision_id_without_state_change(self):
        self.init_claim()
        feature_id = self.feature()
        decision = self.cli("decision", "add", "--token-file", str(self.token), data={
            "feature_id": feature_id, "mandate_revision": 1, "source_kind": "operator-reported",
            "source_ref": "chat:1", "reason": "rethink", "choice": {"outcome": "new"}})["id"]
        before = self.cli("status")["revision"]
        self.assertEqual("INVALID_INPUT", self.cli("feature", "revise", feature_id, "--token-file", str(self.token),
                                                  data={"expected_revision": 1, "patch": {"outcome": "new"},
                                                        "decision_id": {}}, code=2)["error"])
        self.assertEqual(before, self.cli("status")["revision"])
        self.assertEqual(1, self.cli("detail", "feature", feature_id)["mandate_revision"])
        self.assertEqual(decision, self.cli("detail", "decision", decision)["id"])

    def test_pending_launch_requires_evidence_and_retry_fingerprint(self):
        self.init_claim()
        feature_id = self.feature()
        for name in ("brief.md", "contract.md"):
            (self.repo / name).write_text(name)
        digest = lambda name: hashlib.sha256((self.repo / name).read_bytes()).hexdigest()
        contracts = [{"path": "contract.md", "digest": digest("contract.md")}]
        assignment = self.cli("assignment", "add", "--token-file", str(self.token), data={
            "feature_id": feature_id, "idempotency_key": "key", "brief_ref": "brief.md", "mandate_revision": 1,
            "source_digests": [{"path": "brief.md", "digest": digest("brief.md")}],
            "contract_files": contracts,
            "contract_digest": hashlib.sha256(json.dumps(contracts, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "writable_paths": ["artifact.md"], "permission_mode": "workspace-write", "author_id": "author",
            "depends_on": [], "blocking_requests": []})["id"]
        fingerprint = self.cli("detail", "assignment", assignment)["request_fingerprint"]
        self.cli("assignment", "reserve-launch", assignment, "--token-file", str(self.token))
        self.assertEqual("BLOCKED_EVIDENCE", self.cli("assignment", "cancel-prepared", assignment,
                                                      "--token-file", str(self.token), code=4)["error"])
        self.assertEqual("RECOVERY_BLOCKED", self.cli("release", "--token-file", str(self.token), code=4)["error"])
        self.assertEqual("launch-unknown", self.cli("recover")["assignment_states"][assignment])
        self.assertEqual("launch-pending", self.cli("assignment", "retry", assignment,
                                                   data={"request_fingerprint": fingerprint})["state"])
        self.assertEqual("IDEMPOTENCY_CONFLICT", self.cli("assignment", "retry", assignment,
                                                          data={"request_fingerprint": "wrong"}, code=2)["error"])

    def test_a_reported_return_never_claims_a_client_stop(self):
        """Design D25, ruling H7: `client-stopped` is the coordinator's own observed result; `assignment return` can
        neither report it nor attach a `client_stop` to another result."""
        self.init_claim()
        feature_id = self.feature()
        (self.repo / "brief.md").write_text("brief.md")
        assignment = self.cli("assignment", "add", "--token-file", str(self.token), data={
            "feature_id": feature_id, "idempotency_key": "key", "brief_ref": "brief.md", "mandate_revision": 1,
            "source_digests": [{"path": "brief.md", "digest": hashlib.sha256(b"brief.md").hexdigest()}],
            "contract_files": [], "contract_digest": hashlib.sha256(b"[]").hexdigest(),
            "writable_paths": ["artifact.md"], "permission_mode": "workspace-write", "author_id": "author",
            "depends_on": [], "blocking_requests": []})["id"]
        self.cli("assignment", "reserve-launch", assignment, "--token-file", str(self.token))
        returned = {"source_id": "operator", "event_ref": None, "changed_paths": [], "checks": [], "descendants": [],
                    "unresolved_question": None, "options": [], "affected_ids": [], "payload_digest": "0" * 64}
        stop = {"kind": "max-turns", "num_turns": 61}
        for data in ({**returned, "result": "client-stopped", "client_stop": stop},
                     {**returned, "result": "client-stopped"}, {**returned, "result": "completed", "client_stop": stop}):
            with self.subTest(data=data):
                self.assertEqual("INVALID_INPUT", self.cli("assignment", "return", assignment, "--token-file",
                                                          str(self.token), data=data, code=2)["error"])
        self.assertEqual("launch-unknown", self.cli("recover")["assignment_states"][assignment])

    def test_reported_review_cannot_accept_and_forged_provenance_is_rejected(self):
        _, _, bundle = self.seed_bundle()
        review_data = {"bundle_id": bundle, "reviewer_id": "reviewer", "source_id": "client",
                       "source_ref": "review:1", "finding": {}, "critical_open": False}
        self.assertEqual("INVALID_INPUT", self.cli("review", "add", "--token-file", str(self.token),
                                                  data={**review_data, "provenance_class": "observed"}, code=2)["error"])
        review = self.cli("review", "add", "--token-file", str(self.token), data=review_data)["id"]
        self.assertEqual("reported", self.cli("detail", "review", review)["provenance_class"])
        self.assertEqual({"critical": 0, "high": 0, "medium": 0, "low": 0},
                         self.cli("detail", "review", review)["finding_counts"])
        self.assertEqual("BLOCKED_EVIDENCE", self.cli("accept", "--token-file", str(self.token),
                                                      data={"bundle_id": bundle, "review_id": review}, code=4)["error"])

    def test_operator_reported_decision_stays_reported(self):
        self.init_claim()
        feature_id = self.feature()
        decision = self.cli("decision", "add", "--token-file", str(self.token), data={
            "feature_id": feature_id, "mandate_revision": 1, "source_kind": "operator-reported",
            "source_ref": "chat:1", "reason": "requested", "choice": {"ship": True}})["id"]
        self.assertEqual("reported", self.cli("detail", "decision", decision)["provenance_class"])

    def test_changed_artifact_blocks_acceptance(self):
        coordinator, owner, bundle = self.seed_bundle()
        review = coordinator.record_review(owner, ReviewEvidence(bundle, "reviewer", "client", "review:1", {}, False))
        (self.repo / "artifact.md").write_text("changed")
        self.assertEqual("STALE_INPUT", self.cli("accept", "--token-file", str(self.token),
                                                 data={"bundle_id": bundle, "review_id": review}, code=4)["error"])

    def test_invalid_import_and_token_never_serialized(self):
        source = self.root / "invalid"
        source.mkdir()
        self.assertEqual("INVALID_CHECKPOINT", self.cli("import", "--from", str(source), data={}, code=5)["error"])
        self.assertFalse((self.repo / ".git" / "coordinator").exists())
        self.init_claim()
        secret = json.loads(self.token.read_text())["token"]
        outputs = [self.cli("status"), self.cli("recover"),
                   self.cli("release", "--token-file", str(self.root / "missing.token"), code=2)]
        for output in outputs:
            self.assertNotIn(secret, json.dumps(output))
        target = self.root / "exports"
        target.mkdir()
        receipt = self.cli("export", "--to", str(target), "--token-file", str(self.token))
        self.assertNotIn(secret, json.dumps(receipt))
        self.assertNotIn(secret, (Path(receipt["directory"]) / "checkpoint.json").read_text())


if __name__ == "__main__":
    unittest.main()
