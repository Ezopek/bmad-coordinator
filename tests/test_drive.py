"""Driver Task 1: persisted pipeline, inbox, locks, turn loop and operator-only attempt resolve. Fake clients behind
the production registry entries prove coordinator logic only."""

import fcntl
import hashlib
import io
import json
import os
import re
import signal
import sqlite3
import stat
import subprocess
import sys
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from coordinator_core import __main__ as cli
from coordinator_core import bmad_planning as planning
from coordinator_core import drive, drive_pipeline, inbox, worker
from coordinator_core.model import DecisionInput, ReturnEvidence
from coordinator_core.run_receipt import canonical, run_name, run_root, seal
from coordinator_core.service import HOLDING_STATES
from tests.drive_support import COORDINATOR, DriveCase, kill_group, pid_alive
from tests.test_bmad_planning import ARCH, PRD, flow

KEY = re.compile(r"^drv-[0-9a-f]{12}-(.+)-([0-9]+)$")
# Another owner-fenced command: LOCK_SH on drive.lock until stdin closes.
SHARED_HOLDER = ("import fcntl, os, sys\nfd = os.open(sys.argv[1], os.O_RDWR)\nfcntl.flock(fd, fcntl.LOCK_SH)\n"
                 "print('ready', flush=True)\nsys.stdin.read()\n")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def start_time(pid: int) -> int:
    text = Path(f"/proc/{pid}/stat").read_text()
    return int(text[text.rindex(")") + 2:].split()[19])


class Base(DriveCase):
    def asked(self):
        """A driven create that returns one open question: needs-human/judgment-budget (max_judgments is 0)."""
        self.scenario(steps={"prd-1": {"status": "partial", "questions": ["Which cloud?"]}})
        code, summary, _ = self.drive_start()
        self.assertEqual((11, "judgment-budget"), (code, summary["reason"]), summary)
        return summary

    def stranded(self):
        """A driven create whose worker exits 1 after writing nothing: unknown, fence held."""
        self.scenario(steps={"prd-1": {"status": "blocked", "mode": "exit-1"}})
        code, summary, _ = self.drive_start()
        self.assertEqual((11, "attempt-unknown"), (code, summary["reason"]), summary)
        return summary["subjects"]["holder"], summary

    def resolve(self, assignment, confirm=True, code=None, data=None, token=None):
        args = ["attempt", "resolve", assignment, "--token-file", str(token or self.drive_token)]
        if confirm:
            args.append("--confirm-unknown")
        return self.run_cli(*args, data=data or {"reason": "worker host rebooted", "source_ref": "ops:ticket-7"},
                            code=code)

    def run_dir(self, assignment):
        row = self.coordinator.detail("assignment", assignment)
        return run_root(self.t.repo) / run_name(assignment, row["request_fingerprint"])

    def prepare_cli(self, key, token=None, skill=PRD, **over):
        data = dict(feature_id=self.fid, key=key, skill=skill, intent="create", author_id="op-author",
                    compat_decision_id=self.compat, slug="gitops", inputs=["docs/input.md"],
                    input_roles={"docs/input.md": "driving-brief"})
        data.update(over)
        return self.run_cli("bmad", "prepare", "--token-file", str(token or self.drive_token), data=data, code=0)[1]

    def listing(self):
        return {item["name"]: item for item in self.run_cli("inbox", "list", code=0)[1]["messages"]}

    def stop_data(self):
        return {"feature_id": self.fid, "reason": "operator pause", "source_ref": "ops:stop-1"}

    def claim_cli(self, name):
        token = self.root / f"{name}.token"
        self.run_cli("claim", "--session", name, "--token-file", str(token), code=0)
        return token

    def interventions(self, summary):
        return [item["type"] for item in summary["interventions"]]

    def drive_events(self, kind):
        return [row for row in self.snapshot()["events"] if row["kind"] == kind]

    def key_suffixes(self):
        return [(KEY.match(item["key"]).group(1) + "-" + KEY.match(item["key"]).group(2))
                if KEY.match(item["key"]) else item["key"] for item in self.launch_log()]


class ReleaseLockHandoffTests(Base):
    def test_release_allowed_after_observed_terminals_and_refused_for_prepared_holding_or_reported(self):
        self.owner = self.coordinator.claim("attended-2", self.root / "a2.token")
        self.question_run()  # observed needs-decision: an observed terminal return
        prepared = self.assign()
        self.code("RECOVERY_BLOCKED", self.coordinator.release, self.owner)
        self.coordinator.cancel_prepared(self.owner, prepared)
        self.coordinator.release(self.owner)
        self.assertEqual("released", self.snapshot()["owner"]["state"])
        self.owner = self.coordinator.claim("attended-3", self.root / "a3.token")
        held = self.assign(idempotency_key="two")
        self.coordinator.reserve_launch(self.owner, held)
        self.code("RECOVERY_BLOCKED", self.coordinator.release, self.owner)
        self.coordinator.record_return(self.owner, held, ReturnEvidence(
            "test", "return:1", [], [], [], "completed", None, [], [], sha(b"reported")))
        self.assertEqual("completed-claim", self.state(held))
        self.code("RECOVERY_BLOCKED", self.coordinator.release, self.owner)
        self.assertIn("descendants", self.coordinator.release.__doc__)

    def test_anchor_event_closed_kinds_rehashes_file_and_keeps_single_event_per_transaction(self):
        owner = self.coordinator.claim("op", self.root / "op.token")
        directory = self.base_dir / "drives" / "lease-x"
        directory.mkdir(parents=True)
        digest = seal(directory / "run.json", {"run": 1})
        before = self.snapshot()["revision"]
        self.coordinator.anchor_event(owner, "drive.started", "lease-x", ("run-1", "drives/lease-x/run.json", digest))
        snapshot = self.snapshot()
        self.assertEqual(before + 1, snapshot["revision"])
        mine = [row for row in snapshot["events"] if row["revision"] == before + 1]
        self.assertEqual([("drive.started", "lease-x", "run-1", "drives/lease-x/run.json", digest)],
                         [(row["kind"], row["subject_id"], row["source_id"], row["evidence_ref"],
                           row["payload_digest"]) for row in mine])
        for kind, ref in (("assignment.prepared", "drives/lease-x/run.json"), ("judgment.reserved",
                          "drives/lease-x/run.json"), ("drive.turn", "runs/x/launch.json"),
                          ("drive.turn", "drives/../state.sqlite3"), ("drive.turn", "/abs/drives/run.json")):
            with self.subTest(kind=kind, ref=ref):
                self.code("INVALID_INPUT", self.coordinator.anchor_event, owner, kind, "lease-x",
                          ("run-1", ref, digest))
        (directory / "other.json").write_bytes(b"other bytes")
        self.code("BLOCKED_EVIDENCE", self.coordinator.anchor_event, owner, "drive.turn", "lease-x",
                  ("run-1", "drives/lease-x/other.json", digest))
        (directory / "link.json").symlink_to(directory / "run.json")
        self.code("BLOCKED_EVIDENCE", self.coordinator.anchor_event, owner, "drive.turn", "lease-x",
                  ("run-1", "drives/lease-x/link.json", digest))
        self.assertEqual(before + 1, self.snapshot()["revision"])

    def test_lock_file_opened_nofollow_and_never_unlinked(self):
        self.assertEqual(0, self.drive_start()[0])
        lock = self.base_dir / "drive.lock"
        info = lock.lstat()
        self.assertTrue(stat.S_ISREG(info.st_mode))
        self.assertEqual(0o600, stat.S_IMODE(info.st_mode))
        conv = self.claim_cli("conv")
        self.run_cli("release", "--token-file", str(conv), code=0)
        self.assertEqual(info.st_ino, lock.lstat().st_ino)
        fd = drive.acquire_lock(self.t.repo, exclusive=True)
        try:
            self.assertTrue(fcntl.fcntl(fd, fcntl.F_GETFD) & fcntl.FD_CLOEXEC)
        finally:
            os.close(fd)
        lock.unlink()
        elsewhere = self.root / "elsewhere.lock"
        lock.symlink_to(elsewhere)
        conv2 = self.claim_cli("conv2")
        self.assertEqual("UNSAFE_PATH", self.run_cli("release", "--token-file", str(conv2), code=2)[2]["error"])
        self.assertEqual("UNSAFE_PATH", self.drive_start(code=2, token=self.root / "d2.token")[2]["error"])
        self.assertFalse(elsewhere.exists())
        self.assertFalse((self.root / "d2.token").exists())
        self.assertTrue(lock.is_symlink())

    def test_drive_start_requires_released_owner_and_old_conversation_token_turns_stale(self):
        conv = self.claim_cli("conv")
        code, _, err = self.drive_start(code=3)
        self.assertEqual("OWNERSHIP_CONFLICT", err["error"])
        self.assertFalse(self.drive_token.exists())
        self.assertEqual([], self.drive_events("drive.leased"))
        self.run_cli("release", "--token-file", str(conv), code=0)
        self.assertEqual("done", self.drive_start(code=0)[1]["outcome"])
        generation = self.driver_owner().generation
        self.assertEqual(json.loads(conv.read_text())["generation"] + 1, generation)
        err = self.run_cli("decision", "add", "--token-file", str(conv), data={
            "feature_id": self.fid, "mandate_revision": 1, "source_kind": "operator-reported",
            "source_ref": "chat:1", "reason": "late", "choice": "x"}, code=3)[2]
        self.assertEqual("OWNERSHIP_CONFLICT", err["error"])

    def test_two_drive_start_processes_exactly_one_claims(self):
        tokens = [self.root / "a.token", self.root / "b.token"]
        children = [self.child_driver(self.start_args(token=token), pause_at="after-claim") for token in tokens]
        paused = self.wait_for(lambda: [item for item in self.hook_log() if item["point"] == "after-claim"])
        winner = next(child for child in children if child.pid == paused[0]["pid"])
        loser = next(child for child in children if child is not winner)
        code, _, err = self.finish(loser)
        self.assertEqual(3, code, err)
        self.assertIn("OWNERSHIP_CONFLICT", err)
        self.pause_release.write_text("go\n")
        code, summary, err = self.finish(winner)
        self.assertEqual((0, "done"), (code, summary["outcome"]), err)
        claims = self.drive_events("owner.claimed")
        self.assertEqual(["1", "2"], [row["subject_id"] for row in claims])
        self.assertEqual([True, False], [token.exists() for token in
                                         (tokens if winner is children[0] else tokens[::-1])])

    def test_same_token_second_driver_refused_by_exclusive_lock(self):
        child = self.child_driver(self.start_args(), pause_at="run-started")
        self.wait_for(lambda: any(item["point"] == "run-started" for item in self.hook_log()))
        before = self.snapshot()["revision"]
        err = self.drive_resume(code=3)[2]
        self.assertEqual("OWNERSHIP_CONFLICT", err["error"])
        self.assertIn("lock", err["message"])
        self.assertEqual(before, self.snapshot()["revision"])
        self.pause_release.write_text("go\n")
        self.assertEqual(0, self.finish(child)[0])

    def test_fenced_cli_holding_shared_lock_makes_drive_start_and_resume_refuse(self):
        conv = self.claim_cli("conv")
        prepared = self.prepare_cli("op-1", token=conv)
        self.scenario(keys={"op-1": {"status": "complete", "block": self.block_file()}})
        run = self.child_driver(["bmad", "run", prepared["assignment_id"], "--token-file", str(conv),
                                 "--model", "fake-model", "--timeout", "30"])
        self.wait_launches(1)
        self.assertEqual([("READ", str(run.pid))], [(item[3], item[4]) for item in self.lock_entries()])
        err = self.drive_start(code=3)[2]
        self.assertEqual(("OWNERSHIP_CONFLICT", True), (err["error"], "lock" in err["message"]))
        self.assertFalse(self.drive_token.exists())
        err = self.drive_resume(code=3, token=conv)[2]
        self.assertEqual(("OWNERSHIP_CONFLICT", True), (err["error"], "lock" in err["message"]))
        self.unblock()
        code, out, err = self.finish(run)
        self.assertEqual((0, "completed-claim"), (code, out["state"]), err)

    def test_driver_exclusive_lock_makes_fenced_cli_refuse(self):
        self.scenario(steps={"prd-1": {"status": "complete", "block": self.block_file()}})
        child = self.child_driver(self.start_args())
        self.wait_launches(1)
        self.assertEqual([("WRITE", str(child.pid))], [(item[3], item[4]) for item in self.lock_entries()])
        before = self.snapshot()["revision"]
        for args, data in ((("release",), None), (("decision", "add"), {
                "feature_id": self.fid, "mandate_revision": 1, "source_kind": "operator-reported",
                "source_ref": "chat:1", "reason": "x", "choice": "x"})):
            err = self.run_cli(*args, "--token-file", str(self.drive_token), data=data, code=3)[2]
            self.assertEqual(("OWNERSHIP_CONFLICT", True), (err["error"], "lock" in err["message"]))
        self.unblock()
        code, summary, err = self.finish(child)
        self.assertEqual((0, "done"), (code, summary["outcome"]), err)
        self.assertLess(before, self.snapshot()["revision"])

    def test_run_lock_released_when_driver_is_killed_with_orphan_worker_alive(self):
        self.scenario(steps={"prd-1": {"status": "complete", "block": self.block_file()}})
        child = self.child_driver(self.start_args())
        worker_pid = self.wait_launches(1)[0]["pid"]
        self.addCleanup(kill_group, worker_pid)
        os.kill(child.pid, signal.SIGKILL)
        self.assertEqual(-signal.SIGKILL, self.finish(child)[0])
        self.assertTrue(pid_alive(worker_pid))
        self.assertEqual([], self.lock_entries())
        self.assertEqual(False, self.run_cli("drive", "status", code=0)[1]["lock"]["held"])
        assignment = self.launch_log()[0]["assignment_id"]
        out = self.run_cli("worker", "reconcile", assignment, "--token-file", str(self.drive_token), code=0)[1]
        self.assertEqual("unknown", out["state"])
        self.assertTrue(pid_alive(worker_pid))

    def test_operator_release_with_driver_token_after_driver_death(self):
        child = self.child_driver(self.start_args(), kill_at="after-lease")
        self.assertEqual(-signal.SIGKILL, self.finish(child)[0])
        self.assertTrue(self.snapshot()["owner"]["session_id"].startswith("drive:"))
        self.run_cli("release", "--token-file", str(self.drive_token), code=0)
        self.assertEqual("released", self.snapshot()["owner"]["state"])

    def test_handback_after_needs_human_release_attended_work_and_new_drive_start(self):
        request = self.asked()["subjects"]["request_id"]
        self.run_cli("release", "--token-file", str(self.drive_token), code=0)
        conv = self.claim_cli("conv")
        self.run_cli("decision", "add", "--token-file", str(conv), data={
            "feature_id": self.fid, "mandate_revision": 1, "source_kind": "operator-reported",
            "source_ref": "chat:answer", "reason": "attended answer", "choice": {"cloud": "EU"},
            "request_id": request}, code=0)
        self.run_cli("release", "--token-file", str(conv), code=0)
        code, summary, _ = self.drive_start(token=self.root / "drive2.token")
        self.assertEqual((0, "done"), (code, summary["outcome"]), summary)
        self.assertIn("attended-generation", self.interventions(summary))
        self.assertEqual(["prd-1", "prd-2"], self.key_suffixes())
        self.assertEqual("released", self.snapshot()["owner"]["state"])

    def test_resume_discovers_lease_from_owner_session(self):
        self.scenario(steps={"prd-1": {"status": "partial", "questions": ["Which cloud?"]}})
        root, other = self.root / "judgments", self.root / "elsewhere"
        root.mkdir()
        other.mkdir()
        code, first, _ = self.drive_start("--judgment-root", str(root))
        self.assertEqual((11, "judgment-budget"), (code, first["reason"]))
        self.assertEqual("drive:" + first["lease_id"], self.snapshot()["owner"]["session_id"])
        leased = self.drive_events("drive.leased")
        self.assertEqual([(first["lease_id"], f"drives/{first['lease_id']}/lease.json")],
                         [(row["subject_id"], row["evidence_ref"]) for row in leased])
        lease = json.loads((self.base_dir / leased[0]["evidence_ref"]).read_bytes())
        self.assertEqual((os.path.realpath(root), self.fid, first["pipeline_sha256"]),
                         (lease["judgment_root"], lease["feature_id"], lease["pipeline_sha256"]))
        err = self.drive_resume("--judgment-root", str(other), code=2)[2]
        self.assertEqual(("INVALID_INPUT", "judgment-root-mismatch"), (err["error"], err["reason"]))
        self.assertEqual(1, len(self.drive_events("drive.started")))
        code, second, _ = self.drive_resume()
        self.assertEqual((11, first["lease_id"], 1), (code, second["lease_id"], second["restarts"]))
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertEqual(1, len(self.launch_log()))

    def test_ownership_replaced_between_turns_fails_without_further_write_or_launch(self):
        seen = {}

        def hook(point, **info):
            if point == "after-turn-event" and not seen:
                with closing(sqlite3.connect(self.store.path)) as conn, conn:
                    conn.execute("UPDATE owner SET generation=generation+1, token_hash=?, session_id='intruder'",
                                 (sha(b"intruder"),))
                seen["revision"] = self.snapshot()["revision"]
                seen["files"] = sorted(str(path) for path in self.base_dir.rglob("*"))

        with patch.object(drive, "HOOK", hook):
            summary, code = drive.start(self.t.repo, self.fid, self.pipeline(), self.drive_token,
                                        drive.RunBounds(10, 3600))
        self.assertEqual((12, "failed", "ownership-lost"), (code, summary["outcome"], summary["reason"]))
        self.assertEqual(seen["revision"], self.snapshot()["revision"])
        self.assertEqual(seen["files"], sorted(str(path) for path in self.base_dir.rglob("*")))
        self.assertEqual([], self.launch_log())


class PipelineLoopTests(Base):
    def test_pipeline_bind_seals_content_addressed_file_and_is_idempotent(self):
        owner = self.coordinator.claim("op", self.root / "op.token")
        value = self.pipeline()
        data = canonical(drive_pipeline.validate(value))
        bound = drive_pipeline.bind(self.t.repo, self.store, owner, value)
        self.assertEqual((sha(data), f"pipelines/{sha(data)}.json", True),
                         (bound["sha256"], bound["ref"], bound["bound"]))
        path = self.base_dir / bound["ref"]
        self.assertEqual((data, 0o600), (path.read_bytes(), stat.S_IMODE(path.stat().st_mode)))
        events = self.drive_events("pipeline.bound")
        self.assertEqual([(self.fid, bound["ref"], sha(data))],
                         [(row["subject_id"], row["evidence_ref"], row["payload_digest"]) for row in events])
        revision = self.snapshot()["revision"]
        self.assertFalse(drive_pipeline.bind(self.t.repo, self.store, owner, value)["bound"])
        self.assertEqual((revision, 1), (self.snapshot()["revision"], len(self.drive_events("pipeline.bound"))))
        self.assertEqual(sha(data), drive_pipeline.active(self.base_dir, self.snapshot(), self.fid)["sha256"])
        # Correction-round design D1: the key is optional; absent, the canonical bytes carry no default.
        self.assertNotIn("max_corrections", drive_pipeline.validate(value)["budgets"])
        corrected = self.pipeline(budgets={"max_updates": 2, "max_judgments": 0, "max_corrections": 1})
        self.assertEqual(1, drive_pipeline.validate(corrected)["budgets"]["max_corrections"])
        self.assertNotEqual(sha(data), drive_pipeline.digest(corrected))
        view = {"finding": {"findings": [{"severity": "high"}, {"severity": "low"}, {"severity": "critical"}],
                            "dispositions": [{"severity": "high"}]}}
        self.assertEqual((["/findings/0", "/findings/2", "/dispositions/0"], ["/findings/2"], ["/"]),
                         (drive_pipeline.blocking_findings(json.dumps(view), False),
                          drive_pipeline.blocking_findings(json.dumps(view), True),
                          drive_pipeline.blocking_findings("not json", False)))

    def bad_pipelines(self):
        author = {"id": "prd", "kind": "author", "skill": PRD, "slug": "gitops", "author_id": "a",
                  "inputs": {"docs/input.md": "driving-brief"}}
        compat = {"id": "compat", "kind": "compat"}
        return [
            ("UNSUPPORTED_STEP", self.pipeline([compat, {"id": "epics", "kind": "epics"}])),
            ("UNSUPPORTED_STEP", self.pipeline([compat, {**author, "kind": "bundle"}])),
            ("UNSUPPORTED_STEP", self.pipeline([compat, {**author, "skill": "bmad-spec"}])),
            ("UNSUPPORTED_STEP", self.pipeline([compat, {**author, "skill": "bmad-ux"}])),
            ("UNSUPPORTED_STEP", self.pipeline([compat, {**author, "intent": "update"}])),
            ("UNSUPPORTED_STEP", self.pipeline([compat, {**author, "intent": "validate"}])),
            ("UNSUPPORTED_STEP", self.pipeline([compat, {**author, "branches": ["x"]}])),
            ("UNSUPPORTED_STEP", self.pipeline([compat, {**author, "skill": ARCH, "altitude": "system",
                                                         "purpose": "build-substrate"}])),
            ("UNSUPPORTED_STEP", self.pipeline(roles={"worker": {"client": "copilot-cli", "model": "m",
                                                                 "timeout": 30}})),
            ("INVALID_INPUT", self.pipeline([compat, {**author, "skill": ARCH, "altitude": "feature",
                                                      "purpose": "build-substrate", "prd_from": "later"},
                                             {"id": "later", "kind": "compat"}])),
            ("INVALID_INPUT", self.pipeline([compat, {**author, "skill": ARCH, "altitude": "feature",
                                                      "purpose": "build-substrate", "prd_from": "prd"}])),
            ("INVALID_INPUT", self.pipeline([compat, author, dict(author)])),
            ("INVALID_INPUT", self.pipeline([author, compat])),
            ("INVALID_INPUT", self.pipeline(budgets={"max_updates": 2, "max_judgments": 1})),
            ("INVALID_INPUT", self.pipeline(budgets={"max_updates": -1, "max_judgments": 0})),
            ("INVALID_INPUT", self.pipeline(budgets={"max_updates": 2, "max_judgments": 0, "max_corrections": 5})),
            ("INVALID_INPUT", self.pipeline(budgets={"max_updates": 2, "max_judgments": 0, "max_corrections": -1})),
            ("INVALID_INPUT", self.pipeline(budgets={"max_updates": 2, "max_judgments": 0, "max_corrections": "1"})),
            ("INVALID_INPUT", self.pipeline(budgets={"max_updates": 2, "max_judgments": 0, "max_corrections": True})),
            ("INVALID_INPUT", self.pipeline(roles={"worker": {"client": "codex-cli", "model": "m", "timeout": 0}})),
            ("INVALID_INPUT", self.pipeline([compat, {**author, "inputs": {"docs/input.md": "boss"}}])),
            ("INVALID_INPUT", self.pipeline(steps="not a list")),
            ("INVALID_INPUT", {**self.pipeline(), "format": 2}),
            ("INVALID_INPUT", {**self.pipeline(), "extra": 1}),
        ]

    def test_pipeline_refuses_unknown_kind_skill_intent_forward_reference_and_client_without_writing(self):
        for index, (expected, value) in enumerate(self.bad_pipelines()):
            with self.subTest(index=index, expected=expected):
                self.code(expected, drive_pipeline.validate, value)
        revision = self.snapshot()["revision"]
        listing = sorted(str(path) for path in self.base_dir.rglob("*"))
        for expected, value in self.bad_pipelines()[::4]:
            code = 4 if expected == "UNSUPPORTED_STEP" else 2
            self.assertEqual(expected, self.drive_start(code=code, pipeline=self.pipeline_file(value))[2]["error"])
        self.assertEqual(revision, self.snapshot()["revision"])
        self.assertEqual(listing, sorted(str(path) for path in self.base_dir.rglob("*")))
        self.assertFalse(self.drive_token.exists())
        self.assertEqual("released", self.snapshot()["owner"]["state"])

    def test_refusal_codes_unsupported_step_exit_4_and_invalid_input_exit_2(self):
        unsupported = self.pipeline_file(self.bad_pipelines()[0][1], "unsupported.json")
        self.assertEqual("UNSUPPORTED_STEP", self.drive_start(code=4, pipeline=unsupported)[2]["error"])
        malformed = self.root / "malformed.json"
        malformed.write_text("{not json")
        self.assertEqual("INVALID_INPUT", self.drive_start(code=2, pipeline=malformed)[2]["error"])
        other = self.pipeline_file({**self.pipeline(), "feature_id": self.t.feature_id}, "other.json")
        self.assertEqual("INVALID_INPUT", self.drive_start(code=2, pipeline=other)[2]["error"])
        self.assertFalse(self.drive_token.exists())

    def test_pipeline_rebind_other_digest_same_revision_conflicts_and_revised_mandate_needs_rebind(self):
        owner = self.coordinator.claim("op", self.root / "op.token")
        first = drive_pipeline.bind(self.t.repo, self.store, owner, self.pipeline())
        other = self.pipeline(budgets={"max_updates": 1, "max_judgments": 0})
        self.code("IDEMPOTENCY_CONFLICT", drive_pipeline.bind, self.t.repo, self.store, owner, other)
        decision = self.coordinator.record_decision(owner, DecisionInput(
            self.fid, 1, "operator-reported", "chat:scope", "narrow scope", {"scope": "EU"}))
        self.assertEqual(2, self.coordinator.revise_mandate(owner, self.fid, 1, {"constraints": ["EU only"]},
                                                            decision))
        self.assertIsNone(drive_pipeline.active(self.base_dir, self.snapshot(), self.fid))
        self.code("STALE_INPUT", drive_pipeline.bind, self.t.repo, self.store, owner, self.pipeline())
        second = drive_pipeline.bind(self.t.repo, self.store, owner, {**other, "mandate_revision": 2})
        self.assertNotEqual(first["sha256"], second["sha256"])
        self.assertEqual(second["sha256"], drive_pipeline.active(self.base_dir, self.snapshot(), self.fid)["sha256"])

    def test_derive_is_pure_and_recognizes_attended_prework_by_binding_content(self):
        self.owner = self.coordinator.claim("attended", self.root / "attended.token")
        prepared = self.prep("p1")
        report = self.execute(prepared["assignment_id"], flow(), self.first_files())
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        value = self.pipeline()
        bound = drive_pipeline.bind(self.t.repo, self.store, self.owner, value)
        snapshot = self.store.read_export_snapshot(self.owner)
        facts = drive_pipeline.gather(self.t.repo, self.store, snapshot, value, self.owner.generation)
        revision = self.snapshot()["revision"]
        first = drive_pipeline.derive(value, bound["sha256"], snapshot, facts)
        self.assertEqual(first, drive_pipeline.derive(value, bound["sha256"], snapshot, facts))
        self.assertEqual(revision, self.snapshot()["revision"])
        self.assertEqual([("compat", "satisfied"), ("prd", "satisfied")], [(s.step_id, s.state) for s in first])
        self.assertEqual(prepared["assignment_id"], first[1].subjects["assignment_id"])
        self.coordinator.release(self.owner)
        summary, code = drive.start(self.t.repo, self.fid, value, self.drive_token, drive.RunBounds(10, 3600))
        self.assertEqual((0, "done", 0), (code, summary["outcome"], summary["counts"]["turns"]))
        self.assertEqual([], self.launch_log())

    def resolved_head(self):
        holder, _ = self.stranded()
        self.resolve(holder, code=0)
        code, summary, _ = self.drive_resume()
        self.assertEqual((11, "attempt-resolved"), (code, summary["reason"]), summary)
        return holder, summary

    def test_resolved_unknown_head_derives_needs_human_attempt_resolved_and_is_never_relaunched(self):
        holder, summary = self.resolved_head()
        self.assertEqual(("prd", holder), (summary["subjects"]["step_id"], summary["subjects"]["assignment_id"]))
        self.assertEqual(0, summary["counts"]["turns"])
        code, again, _ = self.drive_resume()
        self.assertEqual((11, "attempt-resolved"), (code, again["reason"]))
        self.assertEqual(1, len(self.launch_log()))
        self.assertEqual([holder], [row["id"] for row in self.prd_rows()])
        self.assertEqual("resolved-unknown", self.state(holder))

    def test_attempt_resolved_outcome_names_bmad_prepare_for_step_or_mandate_revision(self):
        _, summary = self.resolved_head()
        action = summary["operator_action"]
        for text in ("bmad prepare", "create", "update", "step prd", "driver's token", "drive resume",
                     "revising the mandate", "new pipeline"):
            self.assertIn(text, action)

    def test_newer_operator_prepared_attempt_supersedes_resolved_head_and_driver_proceeds_without_loop(self):
        holder, _ = self.resolved_head()
        retry = self.prepare_cli("op-retry")["assignment_id"]
        code, summary, _ = self.drive_resume()
        self.assertEqual((0, "done"), (code, summary["outcome"]), summary)
        self.assertEqual(["prd-1", "op-retry"], self.key_suffixes())
        self.assertEqual(("resolved-unknown", "completed-claim"), (self.state(holder), self.state(retry)))
        self.assertEqual(1, summary["counts"]["turns"])

    def test_lock_exempts_drive_start_resume_from_shared_and_status_reads_proc_locks_without_acquiring(self):
        self.scenario(steps={"prd-1": {"status": "partial", "questions": ["Which cloud?"]}})
        for args in (self.start_args(), self.resume_args()):
            self.pause_release.unlink(missing_ok=True)
            child = self.child_driver(args, pause_at="run-started")
            self.wait_for(lambda: any(item["point"] == "run-started" and item["pid"] == child.pid
                                      for item in self.hook_log()))
            entries = [(item[3], item[4]) for item in self.lock_entries()]
            self.assertEqual([("WRITE", str(child.pid))], entries)
            status = self.run_cli("drive", "status", code=0)[1]
            self.assertEqual({"held": True, "mode": "exclusive", "pids": [child.pid]}, status["lock"])
            self.assertEqual(entries, [(item[3], item[4]) for item in self.lock_entries()])
            self.pause_release.write_text("go\n")
            self.assertEqual(11, self.finish(child)[0])

    def test_turn_records_sealed_anchored_and_one_action_per_turn(self):
        code, summary, _ = self.drive_start()
        self.assertEqual(0, code)
        run_dir = self.base_dir / "drives" / summary["lease_id"] / summary["run_id"]
        turns = sorted(run_dir.glob("turn-*.json"))
        events = [row for row in self.drive_events("drive.turn") if row["source_id"] == summary["run_id"]]
        self.assertEqual(2, summary["counts"]["turns"])
        self.assertEqual(len(turns), len(events))
        actions = []
        for number, event in enumerate(events, 1):
            path = run_dir / f"turn-{number}.json"
            self.assertEqual(f"drives/{summary['lease_id']}/{summary['run_id']}/turn-{number}.json",
                             event["evidence_ref"])
            self.assertEqual((sha(path.read_bytes()), 0o600), (event["payload_digest"],
                                                               stat.S_IMODE(path.stat().st_mode)))
            record = json.loads(path.read_bytes())
            self.assertEqual(number, record["turn"])
            actions.append(record["action"]["kind"])
        self.assertEqual(["prepare", "launch"], actions)

    def test_summary_sealed_and_anchored_by_drive_ended_before_release(self):
        code, summary, _ = self.drive_start()
        self.assertEqual((0, "released"), (code, summary["ownership"]))
        path = self.summary_file(summary)
        self.assertEqual(summary, json.loads(path.read_bytes()))
        ended = self.drive_events("drive.ended")
        self.assertEqual([(summary["lease_id"], summary["run_id"], sha(path.read_bytes()))],
                         [(row["subject_id"], row["source_id"], row["payload_digest"]) for row in ended])
        released = self.drive_events("owner.released")[-1]
        self.assertLess(ended[0]["revision"], released["revision"])

    def test_max_turns_and_deadline_exit_continue_and_attempt_not_started_past_deadline(self):
        summary, code = drive.start(self.t.repo, self.fid, self.pipeline(), self.drive_token, drive.RunBounds(1, 3600))
        self.assertEqual((10, "continue", "max-turns"), (code, summary["outcome"], summary["reason"]))
        assignment = self.prd_rows()[0]["id"]
        self.assertEqual("prepared", self.state(assignment))
        clock = [1000.0]
        for jump, turns in ((130.0, 1), (200.0, 0)):
            with self.subTest(jump=jump):
                def hook(point, **info):
                    if point == "run-started":
                        clock[0] += jump
                with patch.object(drive, "HOOK", hook):
                    summary, code = drive.resume(self.t.repo, self.driver_owner(), drive.RunBounds(5, 150),
                                                 clock=lambda: clock[0])
                self.assertEqual((10, "continue", "deadline"), (code, summary["outcome"], summary["reason"]))
                self.assertEqual("prepared", self.state(assignment))
                self.assertEqual([], self.launch_log())

    def test_deadline_below_largest_role_timeout_plus_margin_refused_before_run(self):
        err = self.drive_start("--deadline", "149", code=2)[2]
        self.assertEqual(("INVALID_INPUT", "bounds-insufficient"), (err["error"], err["reason"]))
        self.assertFalse(self.drive_token.exists())
        self.assertEqual("released", self.snapshot()["owner"]["state"])
        self.asked()
        started = len(self.drive_events("drive.started"))
        err = self.drive_resume("--deadline", "100", code=2)[2]
        self.assertEqual(("INVALID_INPUT", "bounds-insufficient"), (err["error"], err["reason"]))
        self.assertEqual(started, len(self.drive_events("drive.started")))

    def test_two_turns_without_progress_fail_no_progress(self):
        with patch.object(planning, "prepare", return_value={"assignment_id": "none"}):
            summary, code = drive.start(self.t.repo, self.fid, self.pipeline(), self.drive_token,
                                        drive.RunBounds(10, 3600))
        self.assertEqual((12, "failed", "no-progress", 2), (code, summary["outcome"], summary["reason"],
                                                             summary["counts"]["turns"]))
        self.assertEqual("unchanged", summary["ownership"])
        self.assertEqual([], self.launch_log())
        self.assertEqual("active", self.snapshot()["owner"]["state"])

    def test_create_completes_and_done_releases_ownership(self):
        code, summary, _ = self.drive_start()
        self.assertEqual((0, "done", "released"), (code, summary["outcome"], summary["ownership"]))
        self.assertEqual("released", self.run_cli("recover", code=0)[1]["state"])
        rows = self.prd_rows()
        self.assertEqual(["completed-claim"], [row["state"] for row in rows])
        self.assertEqual("observed", json.loads(rows[0]["return_json"])["provenance_class"])
        self.assertEqual(["prd-1"], self.key_suffixes())
        self.assertEqual([("compat", "satisfied"), ("prd", "satisfied")],
                         [(item["id"], item["state"]) for item in summary["steps"]])

    def test_question_with_zero_judgment_budget_exits_needs_human_with_request_id(self):
        summary = self.asked()
        request = self.open_request()
        self.assertEqual(request, summary["subjects"]["request_id"])
        self.assertIn(f"inbox add decision for request {request}", summary["operator_action"])
        self.assertEqual("retained", summary["ownership"])
        self.assertEqual([], [row for row in self.snapshot()["decisions"] if row["request_id"]])

    def test_human_wait_consumes_inbox_answer_without_a_turn_and_continues(self):
        request = self.asked()["subjects"]["request_id"]
        polls = []

        def sleep(seconds):
            if not polls:
                inbox.add(self.t.repo, "decision", self.decision_message(request))
            polls.append(seconds)

        with patch.object(drive, "_sleep", sleep):
            summary, code = drive.resume(self.t.repo, self.driver_owner(), drive.RunBounds(10, 3600, 5, 0.05))
        self.assertEqual((0, "done", 2), (code, summary["outcome"], summary["counts"]["turns"]))
        self.assertEqual(1, len(polls))
        self.assertEqual(["inbox-decision", "resume-after-needs-human"], sorted(self.interventions(summary)))

    def test_fence_held_by_other_feature_needs_human_fenced_by_other(self):
        self.assertEqual(10, self.drive_start("--max-turns", "1")[0])
        self.owner = self.driver_owner()
        other = self.assign(idempotency_key="other-feature")
        self.coordinator.reserve_launch(self.owner, other)
        code, summary, _ = self.drive_resume()
        self.assertEqual((11, "fenced-by-other", other), (code, summary["reason"], summary["subjects"]["holder"]))
        self.assertEqual([], self.launch_log())

    def test_mandate_revised_between_runs_needs_human(self):
        self.asked()
        owner = self.driver_owner()
        decision = self.coordinator.record_decision(owner, DecisionInput(
            self.fid, 1, "operator-reported", "chat:scope", "narrow scope", {"scope": "EU"}))
        self.coordinator.revise_mandate(owner, self.fid, 1, {"constraints": ["EU only"]}, decision)
        code, summary, _ = self.drive_resume()
        self.assertEqual((11, "mandate-revised"), (code, summary["reason"]))
        self.assertIn("pipeline", summary["operator_action"])
        self.assertEqual(1, len(self.launch_log()))

    def test_reconcile_unknown_precedes_inbox_consumption(self):
        holder, _ = self.stranded()
        message = self.inbox_add("decision", self.decision_message("request-unknown"))
        code, summary, _ = self.drive_resume()
        self.assertEqual((11, "attempt-unknown", holder), (code, summary["reason"], summary["subjects"]["holder"]))
        self.assertEqual("pending", self.listing()[message["name"]]["state"])
        self.assertEqual([], self.drive_events("inbox.rejected"))

    def test_drive_status_read_only_creates_nothing_and_reports_lock_steps_inbox_and_last_outcome(self):
        summary = self.asked()
        stop = self.inbox_add("stop", self.stop_data())
        listing = sorted((str(path), path.stat().st_mtime_ns) for path in self.base_dir.rglob("*"))
        revision = self.snapshot()["revision"]
        status = self.run_cli("drive", "status", code=0)[1]
        self.assertEqual(listing, sorted((str(path), path.stat().st_mtime_ns) for path in self.base_dir.rglob("*")))
        self.assertEqual(revision, self.snapshot()["revision"])
        self.assertEqual({"held": False, "mode": None, "pids": []}, status["lock"])
        lease = status["leases"][0]
        self.assertEqual((summary["lease_id"], True, "needs-human", "judgment-budget"),
                         (lease["lease_id"], lease["last_run"]["ended"], lease["last_run"]["outcome"],
                          lease["last_run"]["reason"]))
        feature = next(item for item in status["features"] if item["feature_id"] == self.fid)
        self.assertEqual([("compat", "satisfied"), ("prd", "needs-human")],
                         [(item["id"], item["state"]) for item in feature["steps"]])
        self.assertEqual([(stop["name"], "pending")], [(item["name"], item["state"]) for item in status["inbox"]])
        self.assertEqual([self.open_request()], status["open_requests"])
        fresh = self.root / "fresh"
        subprocess.run(["git", "init", "-q", str(fresh)], check=True)
        result = subprocess.run([sys.executable, "-m", "coordinator_core", "--repo", str(fresh), "drive", "status"],
                                capture_output=True, text=True, cwd=COORDINATOR, env=self.child_env())
        self.assertEqual((0, {"state": "uninitialized"}), (result.returncode, json.loads(result.stdout)))
        self.assertFalse((fresh / ".git" / "coordinator").exists())

    def test_cli_exit_codes_and_summary_fields(self):
        self.scenario(steps={"prd-1": {"status": "partial", "questions": ["Which cloud?"]}})
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", ["coordinator", "--repo", str(self.t.path), *self.start_args()]), \
             patch.object(planning, "prepare", return_value={"assignment_id": "none"}), \
             redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(12, cli.main())
        self.assertEqual(("failed", "no-progress"), (json.loads(out.getvalue())["outcome"],
                                                     json.loads(out.getvalue())["reason"]))
        code, summary, _ = self.drive_resume("--max-turns", "1")
        self.assertEqual((10, "continue", "max-turns"), (code, summary["outcome"], summary["reason"]))
        code, summary, _ = self.drive_resume()
        self.assertEqual((11, "needs-human"), (code, summary["outcome"]))
        self.inbox_add("decision", self.decision_message(summary["subjects"]["request_id"]))
        code, summary, _ = self.drive_resume()
        self.assertEqual((0, "done"), (code, summary["outcome"]))
        self.assertEqual({"kind", "format", "lease_id", "run_id", "feature_id", "generation", "pipeline_sha256",
                          "outcome", "reason", "subjects", "operator_action", "ownership", "ownership_reason",
                          "decisions", "counts", "steps", "restarts", "interventions", "acknowledged_stops",
                          "ended_at", "bill"}, set(summary))   # bill design B4
        self.assertEqual((3, {"worker": 1}), (summary["restarts"], summary["counts"]["launches"]))

    def test_interventions_typed_including_attempt_resolve_and_attended_generation(self):
        holder, first = self.stranded()
        self.assertEqual([], first["interventions"])
        self.resolve(holder, code=0)
        code, second, _ = self.drive_resume()
        self.assertEqual(11, code)
        self.assertIn({"type": "attempt-resolve", "assignment_id": holder}, second["interventions"])
        self.assertIn("resume-after-needs-human", self.interventions(second))
        self.run_cli("release", "--token-file", str(self.drive_token), code=0)
        conv = self.claim_cli("conv")
        self.run_cli("release", "--token-file", str(conv), code=0)
        code, third, _ = self.drive_start(token=self.root / "drive2.token")
        self.assertEqual((11, "attempt-resolved"), (code, third["reason"]))
        attended = [item for item in third["interventions"] if item["type"] == "attended-generation"]
        self.assertEqual([json.loads(conv.read_text())["generation"]], [item["generation"] for item in attended])
        self.assertNotIn("attempt-resolve", self.interventions(third))

    def test_installed_wrapper_exposes_drive_inbox_and_attempt_resolve(self):
        manifest = json.loads((self.t.path / ".bmad-coordinator" / "manifest.json").read_text())
        for name in ("drive.py", "drive_pipeline.py", "inbox.py"):
            self.assertIn(f"coordinator_core/{name}", manifest["files"])
        self.assertEqual(0, self.run_cli("drive", "status", wrapper=True)[0])
        self.assertEqual(0, self.run_cli("inbox", "add", "stop", data=self.stop_data(), wrapper=True)[0])
        self.assertEqual(1, len(self.run_cli("inbox", "list", wrapper=True)[1]["messages"]))
        err = self.run_cli("attempt", "resolve", "x", "--token-file", str(self.drive_token), wrapper=True)[2]
        self.assertEqual("INVALID_INPUT", err["error"])
        code, summary, _ = self.run_cli(*self.start_args(), wrapper=True)
        self.assertEqual((0, "done"), (code, summary["outcome"]))


class InboxTests(Base):
    def test_inbox_decision_consumed_atomically_as_operator_reported_then_update_runs(self):
        request = self.asked()["subjects"]["request_id"]
        message = self.inbox_add("decision", self.decision_message(request))
        code, summary, _ = self.drive_resume()
        self.assertEqual((0, "done"), (code, summary["outcome"]), summary)
        decision = next(row for row in self.snapshot()["decisions"] if row["request_id"] == request)
        self.assertEqual(("operator-reported", f"inbox/{message['name']}#{message['sha256']}"),
                         (decision["source_kind"], decision["source_ref"]))
        row = self.coordinator.detail("decision-request", request)
        self.assertEqual(("resolved", decision["id"]), (row["state"], row["resolution_decision_id"]))
        self.assertEqual(["prd-1", "prd-2"], self.key_suffixes())
        update = self.launch_log()[1]["assignment_id"]
        self.assertEqual(request, planning.read_assignment_binding(self.store, self.t.repo, update)
                         ["continues"]["request"])
        self.assertEqual("consumed", self.listing()[message["name"]]["state"])
        self.assertIn({"type": "inbox-decision", "name": message["name"], "request_id": request},
                      summary["interventions"])
        self.assertEqual([decision["id"]], [item["id"] for item in summary["decisions"]])

    def test_inbox_rejects_stale_foreign_tampered_or_nonprivate_message_once(self):
        stale = self.inbox_add("stop", self.stop_data())["name"]
        value = {"kind": "coordinator-inbox", "format": 1, "type": "decision", "repository_id": "other-repository",
                 "feature_id": self.fid, "generation": 1, "reason": "x", "source_ref": "y",
                 "request_id": "r", "mandate_revision": 1, "choice": "z"}
        foreign = inbox.publish(self.t.repo, value)["name"]
        tampered = self.inbox_add("decision", self.decision_message("request-a"))["name"]
        path = self.base_dir / "inbox" / tampered
        path.write_bytes(path.read_bytes().replace(b"EU region", b"US region"))
        nonprivate = self.inbox_add("decision", self.decision_message("request-b"))["name"]
        (self.base_dir / "inbox" / nonprivate).chmod(0o644)
        self.asked()
        expected = {stale: "stale-generation", foreign: "foreign", tampered: "tampered", nonprivate: "nonprivate"}
        rejected = {row["subject_id"]: row["source_id"] for row in self.drive_events("inbox.rejected")}
        self.assertEqual(expected, rejected)
        self.assertEqual(11, self.drive_resume()[0])
        self.assertEqual(4, len(self.drive_events("inbox.rejected")))
        listing = self.listing()
        self.assertEqual(expected, {name: listing[name]["reason"] for name in expected})
        self.assertEqual({"rejected"}, {listing[name]["state"] for name in expected})

    def test_inbox_files_exclusive_private_immutable_and_never_deleted(self):
        first = self.inbox_add("stop", self.stop_data())
        second = self.inbox_add("stop", self.stop_data())
        directory = self.base_dir / "inbox"
        self.assertEqual(0o700, stat.S_IMODE(directory.stat().st_mode))
        self.assertNotEqual(first["name"], second["name"])
        before = {}
        for item in (first, second):
            path = directory / item["name"]
            data = path.read_bytes()
            self.assertRegex(item["name"], r"^[0-9]+-[0-9a-f]{16}\.json$")
            self.assertEqual((sha(data), sha(data)[:16]), (item["sha256"], item["name"].split("-")[1][:16]))
            self.assertEqual((0o600, 1), (stat.S_IMODE(path.stat().st_mode), path.stat().st_nlink))
            self.assertEqual(canonical(json.loads(data)), data)
            before[item["name"]] = data
        self.assertEqual(sorted(before), sorted(os.listdir(directory)))
        ns = int(first["name"].split("-")[0])
        with patch.object(inbox.time, "time_ns", return_value=ns):
            self.code("INVALID_INPUT", inbox.add, self.t.repo, "stop", self.stop_data())
        code, summary, _ = self.drive_start()
        self.assertEqual((0, "done"), (code, summary["outcome"]))
        self.assertEqual({"rejected"}, {self.listing()[name]["state"] for name in before})
        self.assertEqual(before, {name: (directory / name).read_bytes() for name in os.listdir(directory)})

    def test_inbox_add_refuses_uninitialized_and_never_creates_coordinator_dir(self):
        fresh = self.root / "fresh"
        subprocess.run(["git", "init", "-q", str(fresh)], check=True)
        for args, data in ((("inbox", "add", "stop"), self.stop_data()),
                           (("inbox", "add", "decision"), self.decision_message("r")), (("inbox", "list"), None)):
            result = subprocess.run([sys.executable, "-m", "coordinator_core", "--repo", str(fresh), *args],
                                    input=json.dumps(data) if data else None, capture_output=True, text=True,
                                    cwd=COORDINATOR, env=self.child_env())
            if args[1] == "list":
                self.assertEqual((0, {"state": "uninitialized"}), (result.returncode, json.loads(result.stdout)))
            else:
                self.assertEqual((4, "UNINITIALIZED"), (result.returncode, json.loads(result.stderr)["error"]))
        self.assertFalse((fresh / ".git" / "coordinator").exists())

    def test_inbox_decision_written_during_attempt_rejected_named_and_resent_after_window_closes_consumed(self):
        request = self.asked()["subjects"]["request_id"]
        attended = self.prepare_cli("op-a1", skill=ARCH, author_id="arch-op", inputs=["docs/authority.md"],
                                    input_roles={"docs/authority.md": "authority"}, altitude="feature",
                                    purpose="build-substrate")["assignment_id"]
        self.scenario(keys={"op-a1": {"status": "complete", "block": self.block_file()}})
        run = self.child_driver(["bmad", "run", attended, "--token-file", str(self.drive_token),
                                 "--model", "fake-model", "--timeout", "30"])
        self.wait_launches(2)
        written = self.inbox_add("decision", self.decision_message(request))
        self.unblock()
        self.assertEqual(0, self.finish(run)[0])
        code, summary, _ = self.drive_resume()
        self.assertEqual((11, "judgment-budget"), (code, summary["reason"]))
        item = self.listing()[written["name"]]
        self.assertEqual(("rejected", "written-during-attempt", f"assignment:{attended}"),
                         (item["state"], item["reason"], item["subject"]))
        windows = self.run_cli("drive", "status", code=0)[1]["windows"]
        self.assertIn({"subject": f"assignment:{attended}", "open": False},
                      [{"subject": window["subject"], "open": window["open"]} for window in windows])
        resent = self.inbox_add("decision", self.decision_message(request))
        self.assertNotEqual(written["name"], resent["name"])
        code, summary, _ = self.drive_resume()
        self.assertEqual((0, "done"), (code, summary["outcome"]), summary)
        self.assertEqual("consumed", self.listing()[resent["name"]]["state"])

    def test_inbox_decision_written_while_window_still_open_rejected_at_consumption(self):
        holder, _ = self.stranded()
        self.run_cli("assignment", "return", holder, "--token-file", str(self.drive_token), data={
            "source_id": "operator", "event_ref": None, "changed_paths": [], "checks": [], "descendants": [],
            "result": "blocked", "unresolved_question": None, "options": [], "affected_ids": [],
            "payload_digest": "0" * 64}, code=0)
        windows = self.run_cli("drive", "status", code=0)[1]["windows"]
        self.assertEqual([(f"assignment:{holder}", True)], [(item["subject"], item["open"]) for item in windows])
        message = self.inbox_add("decision", self.decision_message("request-any"))
        code, summary, _ = self.drive_resume()  # a reported terminal row passes R4, so R5 meets the open window
        self.assertEqual((11, "unobserved-return"), (code, summary["reason"]))
        item = self.listing()[message["name"]]
        self.assertEqual(("rejected", "written-during-attempt", f"assignment:{holder}"),
                         (item["state"], item["reason"], item["subject"]))
        self.assertIn(f"attempt resolve {holder}", summary["operator_action"])

    def test_stop_written_during_attempt_is_exempt_and_honoured_at_boundary(self):
        self.scenario(steps={"prd-1": {"status": "complete", "block": self.block_file()}})
        child = self.child_driver(self.start_args())
        self.wait_launches(1)
        stop = self.inbox_add("stop", self.stop_data())
        self.unblock()
        code, summary, err = self.finish(child)
        self.assertEqual((11, "operator-stop", [stop["name"]]), (code, summary["reason"],
                                                                summary["acknowledged_stops"]), err)
        self.assertEqual("completed-claim", self.state(self.launch_log()[0]["assignment_id"]))
        self.assertEqual("consumed", self.listing()[stop["name"]]["state"])
        acknowledged = self.drive_events("drive.stop-acknowledged")
        self.assertEqual([summary["run_id"]], [row["source_id"] for row in acknowledged])
        self.assertEqual([], self.drive_events("drive.ended"))

    def test_late_operator_answer_rejected_request_not_open_and_listed(self):
        request = self.asked()["subjects"]["request_id"]
        first = self.inbox_add("decision", self.decision_message(request))
        late = self.inbox_add("decision", self.decision_message(request, choice={"answer": "US"}))
        code, summary, _ = self.drive_resume()
        self.assertEqual((0, "done"), (code, summary["outcome"]))
        listing = self.listing()
        self.assertEqual("consumed", listing[first["name"]]["state"])
        self.assertEqual(("rejected", "request-not-open"), (listing[late["name"]]["state"],
                                                            listing[late["name"]]["reason"]))


class StopSignalTests(Base):
    def test_first_sigint_and_inbox_stop_wait_for_running_attempt_then_stop_at_boundary(self):
        self.scenario(steps={"prd-1": {"status": "complete", "block": self.block_file()}})
        child = self.child_driver(self.start_args())
        self.wait_launches(1)
        os.kill(child.pid, signal.SIGINT)
        self.wait_for(lambda: any(item["point"] == "signal" for item in self.hook_log()))
        stop = self.inbox_add("stop", self.stop_data())
        self.assertIsNone(child.poll())
        self.unblock()
        code, summary, err = self.finish(child)
        self.assertEqual((11, "operator-stop", [stop["name"]]),
                         (code, summary["reason"], summary["acknowledged_stops"]), err)
        self.assertIn({"type": "signal", "signal": "SIGINT", "effect": "graceful"}, summary["interventions"])
        self.assertIn("inbox-stop", self.interventions(summary))
        self.assertEqual("completed-claim", self.state(self.launch_log()[0]["assignment_id"]))

    def abort(self, child, *signals):
        worker_pid = self.wait_launches(len(self.launch_log()) or 1)[-1]["pid"]
        for number, sig in enumerate(signals, 1):
            os.kill(child.pid, sig)
            self.wait_for(lambda: len([item for item in self.hook_log() if item["point"] == "signal"
                                       and item["pid"] == child.pid]) >= number)
        code, summary, err = self.finish(child)
        self.assertEqual((11, "needs-human", "signal-abort"), (code, summary["outcome"], summary["reason"]), err)
        assignment = summary["subjects"]["holder"]
        self.assertIn(self.state(assignment), HOLDING_STATES)
        self.assertTrue((self.run_dir(assignment) / "interrupted.json").is_file())
        self.assertFalse(pid_alive(worker_pid))
        return assignment, summary

    def test_second_sigint_aborts_attempt_unknown_fence_held_needs_human_signal_abort(self):
        self.scenario(steps={"prd-1": {"status": "complete", "block": self.block_file()}})
        child = self.child_driver(self.start_args())
        self.wait_launches(1)
        assignment, summary = self.abort(child, signal.SIGINT, signal.SIGINT)
        self.assertEqual(self.launch_log()[0]["assignment_id"], assignment)
        self.assertIn("worker reconcile", summary["operator_action"])

    def test_sigterm_and_sighup_abort_like_second_sigint(self):
        self.scenario(steps={"prd-1": {"status": "complete", "block": self.block_file()}},
                      keys={"op-2": {"status": "complete", "block": self.block_file("second")}})
        child = self.child_driver(self.start_args())
        self.wait_launches(1)
        first, _ = self.abort(child, signal.SIGTERM)
        self.resolve(first, code=0)
        self.prepare_cli("op-2")
        child = self.child_driver(self.resume_args())
        self.wait_launches(2)
        second, _ = self.abort(child, signal.SIGHUP)
        self.assertEqual(self.launch_log()[1]["assignment_id"], second)

    def test_stop_pending_until_acknowledged_and_honoured_after_kill(self):
        child = self.child_driver(self.start_args(), kill_at="after-stop-seen", pause_at="run-started")
        self.wait_for(lambda: any(item["point"] == "run-started" for item in self.hook_log()))
        stop = self.inbox_add("stop", self.stop_data())
        self.pause_release.write_text("go\n")
        self.assertEqual(-signal.SIGKILL, self.finish(child)[0])
        self.assertEqual("pending", self.listing()[stop["name"]]["state"])
        self.assertEqual([], self.drive_events("drive.stop-acknowledged"))
        code, summary, _ = self.drive_resume()
        self.assertEqual((11, "operator-stop", [stop["name"]], 0),
                         (code, summary["reason"], summary["acknowledged_stops"], summary["counts"]["turns"]))
        self.assertEqual("consumed", self.listing()[stop["name"]]["state"])

    def test_pending_stop_outranks_max_turns_and_deadline_at_the_boundary(self):
        child = self.child_driver(self.start_args(None, None, "--max-turns", "1"), pause_at="after-action")
        self.wait_for(lambda: any(item["point"] == "after-action" for item in self.hook_log()))
        first = self.inbox_add("stop", self.stop_data())
        self.pause_release.write_text("go\n")
        code, summary, err = self.finish(child)
        self.assertEqual((11, "operator-stop", [first["name"]], 1),
                         (code, summary["reason"], summary["acknowledged_stops"], summary["counts"]["turns"]), err)
        clock = [1000.0]
        written = []

        def hook(point, **info):
            if point == "after-action" and not written:
                clock[0] += 200.0
                written.append(inbox.add(self.t.repo, "stop", self.stop_data())["name"])

        with patch.object(drive, "HOOK", hook):
            summary, code = drive.resume(self.t.repo, self.driver_owner(), drive.RunBounds(10, 150),
                                         clock=lambda: clock[0])
        self.assertEqual((11, "operator-stop", written), (code, summary["reason"], summary["acknowledged_stops"]))
        self.assertEqual(["prd-1"], self.key_suffixes())

    def test_graceful_sigint_outranks_max_turns_at_the_boundary(self):
        self.assertEqual(10, self.drive_start("--max-turns", "1")[0])
        self.scenario(steps={"prd-1": {"status": "complete", "block": self.block_file()}})
        child = self.child_driver(self.resume_args(None, "--max-turns", "1"))
        self.wait_launches(1)
        os.kill(child.pid, signal.SIGINT)
        self.wait_for(lambda: any(item["point"] == "signal" for item in self.hook_log()))
        self.unblock()
        code, summary, err = self.finish(child)
        self.assertEqual((11, "signal-stop", 1), (code, summary["reason"], summary["counts"]["turns"]), err)
        self.assertEqual("completed-claim", self.state(self.launch_log()[0]["assignment_id"]))
        self.assertIn("drive resume", summary["operator_action"])

    def test_human_wait_honours_a_stop_written_during_the_wait(self):
        self.asked()

        def sleep(seconds):
            inbox.add(self.t.repo, "stop", self.stop_data())

        with patch.object(drive, "_sleep", sleep):
            summary, code = drive.resume(self.t.repo, self.driver_owner(), drive.RunBounds(10, 3600, 5, 0.05))
        self.assertEqual((11, "operator-stop", 0), (code, summary["reason"], summary["counts"]["turns"]))
        self.assertEqual(1, len(summary["acknowledged_stops"]))

    def test_stop_from_older_generation_rejected_stale(self):
        stop = self.inbox_add("stop", self.stop_data())
        code, summary, _ = self.drive_start()
        self.assertEqual((0, "done"), (code, summary["outcome"]))
        item = self.listing()[stop["name"]]
        self.assertEqual(("rejected", "stale-generation"), (item["state"], item["reason"]))
        self.assertEqual([], summary["acknowledged_stops"])


class KillTests(Base):
    def test_kill_with_token_file_but_no_claim_row_resume_refuses(self):
        child = self.child_driver(self.start_args(), kill_at="before-claim-commit")
        self.assertEqual(-signal.SIGKILL, self.finish(child)[0])
        self.assertTrue(self.drive_token.exists())
        self.assertEqual(("released", ["1"]), (self.snapshot()["owner"]["state"],
                                               [row["subject_id"] for row in self.drive_events("owner.claimed")]))
        self.assertEqual("OWNERSHIP_CONFLICT", self.drive_resume(code=3)[2]["error"])
        self.assertEqual("OWNERSHIP_CONFLICT", self.drive_start(code=3)[2]["error"])
        self.drive_token.unlink()
        self.assertEqual(0, self.drive_start()[0])

    def test_kill_after_claim_before_lease_resume_refuses_run_unbound_and_release_recovers(self):
        child = self.child_driver(self.start_args(), kill_at="after-claim")
        self.assertEqual(-signal.SIGKILL, self.finish(child)[0])
        err = self.drive_resume(code=4)[2]
        self.assertEqual(("RECOVERY_BLOCKED", "run-unbound"), (err["error"], err["reason"]))
        self.assertIn("release", err["action"])
        self.run_cli("release", "--token-file", str(self.drive_token), code=0)
        self.assertEqual(0, self.drive_start(token=self.root / "drive2.token")[0])

    def test_kill_after_pipeline_bind_orphan_identical_rebind_is_noop(self):
        child = self.child_driver(self.start_args(), kill_at="after-pipeline-seal")
        self.assertEqual(-signal.SIGKILL, self.finish(child)[0])
        sealed = list((self.base_dir / "pipelines").iterdir())
        self.assertEqual((1, []), (len(sealed), self.drive_events("pipeline.bound")))
        self.assertEqual("run-unbound", self.drive_resume(code=4)[2]["reason"])
        self.run_cli("release", "--token-file", str(self.drive_token), code=0)
        second = self.root / "drive2.token"
        child = self.child_driver(self.start_args(token=second), kill_at="after-bind")
        self.assertEqual(-signal.SIGKILL, self.finish(child)[0])
        self.assertEqual((sealed, 1), (list((self.base_dir / "pipelines").iterdir()),
                                       len(self.drive_events("pipeline.bound"))))
        self.assertEqual("run-unbound", self.drive_resume(code=4, token=second)[2]["reason"])
        self.run_cli("release", "--token-file", str(second), code=0)
        self.assertEqual(0, self.drive_start(token=self.root / "drive3.token")[0])
        self.assertEqual(1, len(self.drive_events("pipeline.bound")))

    def test_kill_at_turn_boundary_resume_continues_without_duplicate_assignment(self):
        child = self.child_driver(self.start_args(), kill_at="after-turn-event")
        self.assertEqual(-signal.SIGKILL, self.finish(child)[0])
        self.assertEqual(["prepared"], [row["state"] for row in self.prd_rows()])
        code, summary, _ = self.drive_resume()
        self.assertEqual((0, "done"), (code, summary["outcome"]))
        self.assertEqual(["completed-claim"], [row["state"] for row in self.prd_rows()])
        self.assertEqual(["prd-1"], self.key_suffixes())
        run = json.loads((self.base_dir / "drives" / summary["lease_id"] / summary["run_id"] / "run.json")
                         .read_bytes())
        self.assertEqual(False, run["predecessor"]["ended"])

    def test_kill_after_action_before_turn_event_resume_continues_without_duplicate(self):
        child = self.child_driver(self.start_args(), kill_at="after-action")
        self.assertEqual(-signal.SIGKILL, self.finish(child)[0])
        self.assertEqual((["prepared"], []), ([row["state"] for row in self.prd_rows()],
                                              self.drive_events("drive.turn")))
        code, summary, _ = self.drive_resume()
        self.assertEqual((0, "done", 1), (code, summary["outcome"], summary["counts"]["turns"]))
        self.assertEqual((["completed-claim"], ["prd-1"]), ([row["state"] for row in self.prd_rows()],
                                                           self.key_suffixes()))
        run = json.loads((self.base_dir / "drives" / summary["lease_id"] / summary["run_id"] / "run.json")
                         .read_bytes())
        self.assertEqual(False, run["predecessor"]["ended"])

    def test_kill_during_attempt_resume_needs_human_with_holder_and_no_relaunch(self):
        self.scenario(steps={"prd-1": {"status": "complete", "block": self.block_file()}})
        child = self.child_driver(self.start_args())
        launched = self.wait_launches(1)[0]
        os.kill(child.pid, signal.SIGKILL)
        self.assertEqual(-signal.SIGKILL, self.finish(child)[0])
        kill_group(launched["pid"])
        for _ in range(2):
            code, summary, _ = self.drive_resume()
            self.assertEqual((11, "attempt-unknown", launched["assignment_id"]),
                             (code, summary["reason"], summary["subjects"]["holder"]))
        self.assertIn(self.state(launched["assignment_id"]), HOLDING_STATES)
        self.assertEqual(1, len(self.launch_log()))

    def test_kill_after_anchor_before_record_resume_reconciles_and_continues(self):
        child = self.child_driver(self.start_args(), kill_at="after-anchor")
        self.assertEqual(-signal.SIGKILL, self.finish(child)[0])
        assignment = self.launch_log()[0]["assignment_id"]
        self.assertEqual("running", self.state(assignment))
        self.assertEqual(1, len([row for row in self.drive_events("assignment.return-sealed")]))
        code, summary, _ = self.drive_resume()
        self.assertEqual((0, "done"), (code, summary["outcome"]), summary)
        self.assertEqual("completed-claim", self.state(assignment))
        self.assertEqual(1, len(self.launch_log()))

    def test_half_finished_prepare_finished_if_identical_else_cancelled_and_rekeyed(self):
        value = self.pipeline(self.pipeline()["steps"] + [
            {"id": "arch", "kind": "author", "skill": ARCH, "slug": "gitops", "author_id": "arch-author",
             "altitude": "feature", "purpose": "build-substrate", "inputs": {"docs/authority.md": "authority"}}])
        pipeline = self.pipeline_file(value)
        child = self.child_driver(self.start_args(pipeline), kill_at="before-bind-assignment")
        self.assertEqual(-signal.SIGKILL, self.finish(child)[0])
        orphan = self.prd_rows()
        self.assertEqual(["prepared"], [row["state"] for row in orphan])
        child = self.child_driver(self.resume_args(), kill_at="before-bind-assignment:2")
        self.assertEqual(-signal.SIGKILL, self.finish(child)[0])
        rows = {KEY.match(row["idempotency_key"]).group(1): row for row in self.prd_rows()}
        self.assertEqual((orphan[0]["id"], "completed-claim"), (rows["prd"]["id"], rows["prd"]["state"]))
        stale = rows["arch"]
        (self.t.path / "docs/authority.md").write_text("Authority input, revised.\n")
        code, summary, _ = self.drive_resume()
        self.assertEqual((0, "done"), (code, summary["outcome"]), summary)
        self.assertEqual("cancelled-before-launch", self.state(stale["id"]))
        keys = sorted(row["idempotency_key"] for row in self.prd_rows())
        self.assertEqual(["arch-1", "arch-2", "prd-1"],
                         sorted(KEY.match(key).group(1) + "-" + KEY.match(key).group(2) for key in keys))
        self.assertEqual(["prd-1", "arch-2"], self.key_suffixes())
        self.assertIn("cancel-stale", [json.loads(path.read_bytes())["action"]["kind"] for path in
                                       (self.base_dir / "drives" / summary["lease_id"] / summary["run_id"])
                                       .glob("turn-*.json")])

    def test_kill_after_drive_ended_before_release_resume_or_operator_release_returns_ownership(self):
        child = self.child_driver(self.start_args(), kill_at="after-ended")
        self.assertEqual(-signal.SIGKILL, self.finish(child)[0])
        self.assertEqual(("active", 1), (self.snapshot()["owner"]["state"], len(self.drive_events("drive.ended"))))
        code, summary, _ = self.drive_resume()
        self.assertEqual((0, "done", "released"), (code, summary["outcome"], summary["ownership"]))
        self.assertEqual("released", self.snapshot()["owner"]["state"])
        second = self.root / "drive2.token"
        child = self.child_driver(self.start_args(token=second), kill_at="after-ended")
        self.assertEqual(-signal.SIGKILL, self.finish(child)[0])
        self.assertEqual("active", self.snapshot()["owner"]["state"])
        self.run_cli("release", "--token-file", str(second), code=0)
        self.assertEqual("released", self.snapshot()["owner"]["state"])
        self.assertEqual(1, len(self.launch_log()))


class AttemptResolveTests(Base):
    def helper(self, *argv):
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", *argv])
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        return process

    def rewrite_intent(self, assignment, **values):
        path = self.run_dir(assignment) / "launch-intent.json"
        intent = {**json.loads(path.read_bytes()), **values}
        path.unlink()
        seal(path, intent)
        return intent

    def test_resolution_evidence_reads_adapter_launch_intent_fields(self):
        self.owner = self.coordinator.claim("attended", self.root / "attended.token")
        baseline = self.prep("p0", slug="other")["assignment_id"]
        self.execute(baseline, flow(slug="other"), self.first_files("other"))
        self.coordinator.release(self.owner)
        holder, _ = self.stranded()
        intent = json.loads((self.run_dir(holder) / "launch-intent.json").read_bytes())
        self.assertLessEqual({"pid", "pgid", "start_time", "boot_id", "run_dir", "observed_at"}, set(intent))
        evidence = worker.resolution_evidence(self.t.repo, self.store, holder)
        self.assertEqual(("recorded", intent, intent["pid"], [], [str(self.run_dir(holder))]),
                         (evidence["spawn"], evidence["intent"], evidence["pid"], evidence["cmdline_hits"],
                          evidence["launch_tokens"]))
        self.assertIn(evidence["pid_state"], ("gone", "exited"))
        self.assertRegex(evidence["inventory_digest"], "^[0-9a-f]{64}$")
        self.assertEqual(baseline, evidence["baseline"])
        self.assertTrue(any(path.endswith(".md") and "/briefs/" in path for path in evidence["inventory_delta"]))

    def test_attempt_resolve_refuses_without_confirm_flag(self):
        holder, _ = self.stranded()
        self.assertEqual("INVALID_INPUT", self.resolve(holder, confirm=False, code=2)[2]["error"])
        self.assertIn(self.state(holder), HOLDING_STATES)
        self.assertFalse((self.base_dir / "resolutions").exists())

    def test_attempt_resolve_refuses_non_holder(self):
        self.asked()
        assignment = self.launch_log()[0]["assignment_id"]
        self.assertEqual("BLOCKED_EVIDENCE", self.resolve(assignment, code=4)[2]["error"])
        self.assertEqual("NOT_FOUND", self.resolve("missing", code=2)[2]["error"])
        self.assertEqual("needs-decision", self.state(assignment))
        self.assertFalse((self.base_dir / "resolutions").exists())

    def test_attempt_resolve_takes_shared_lock_and_refuses_while_driver_holds_exclusive(self):
        holder, _ = self.stranded()
        child = self.child_driver(self.resume_args(), pause_at="run-started")
        self.wait_for(lambda: any(item["point"] == "run-started" for item in self.hook_log()))
        err = self.resolve(holder, code=3)[2]
        self.assertEqual(("OWNERSHIP_CONFLICT", True), (err["error"], "lock" in err["message"]))
        self.assertIn(self.state(holder), HOLDING_STATES)
        self.pause_release.write_text("go\n")
        self.assertEqual(11, self.finish(child)[0])
        self.assertEqual("resolved-unknown", self.resolve(holder, code=0)[1]["state"])

    def test_attempt_resolve_refuses_attempt_without_launch_intent(self):
        holder, _ = self.stranded()
        (self.run_dir(holder) / "launch-intent.json").unlink()
        err = self.resolve(holder, code=4)[2]
        self.assertEqual("BLOCKED_EVIDENCE", err["error"])
        self.assertIn("never recorded", err["message"])
        self.assertFalse((self.base_dir / "resolutions").exists())

    def test_attempt_resolve_refuses_while_recorded_pid_alive_with_same_start_time(self):
        holder, _ = self.stranded()
        process = self.helper()
        self.rewrite_intent(holder, pid=process.pid, pgid=process.pid, start_time=start_time(process.pid))
        err = self.resolve(holder, code=4)[2]
        self.assertEqual("BLOCKED_EVIDENCE", err["error"])
        self.assertIn("alive", err["message"])
        process.kill()
        process.wait()
        out = self.resolve(holder, code=0)[1]
        self.assertEqual(("resolved-unknown", "gone"), (out["state"], out["pid_state"]))

    def test_attempt_resolve_boot_id_mismatch_is_reused_and_unknown_boot_decides_on_start_time(self):
        holder, _ = self.stranded()
        process = self.helper()
        self.rewrite_intent(holder, pid=process.pid, start_time=start_time(process.pid), boot_id=None)
        self.assertIn("alive", self.resolve(holder, code=4)[2]["message"])
        self.rewrite_intent(holder, boot_id="another-boot")
        out = self.resolve(holder, code=0)[1]
        self.assertEqual(("resolved-unknown", "reused"), (out["state"], out["pid_state"]))
        self.assertIsNone(process.poll())

    def test_attempt_resolve_refuses_while_cmdline_scan_finds_launch_token(self):
        holder, _ = self.stranded()
        process = self.helper(str(self.run_dir(holder)))
        err = self.resolve(holder, code=4)[2]
        self.assertEqual("BLOCKED_EVIDENCE", err["error"])
        self.assertIn("cmdline", err["message"])
        process.kill()
        process.wait()
        self.assertEqual("resolved-unknown", self.resolve(holder, code=0)[1]["state"])

    def test_attempt_resolve_treats_pid_with_other_start_time_as_reused(self):
        holder, _ = self.stranded()
        process = self.helper()
        self.rewrite_intent(holder, pid=process.pid, start_time=start_time(process.pid) + 7)
        out = self.resolve(holder, code=0)[1]
        self.assertEqual(("resolved-unknown", "reused"), (out["state"], out["pid_state"]))
        self.assertIsNone(process.poll())

    def test_attempt_resolve_allows_reservation_that_never_created_run_dir(self):
        self.assertEqual(10, self.drive_start("--max-turns", "1")[0])
        assignment = self.prd_rows()[0]["id"]
        self.run_cli("assignment", "reserve-launch", assignment, "--token-file", str(self.drive_token), code=0)
        self.assertFalse(os.path.lexists(self.run_dir(assignment)))
        holder = subprocess.Popen([sys.executable, "-c", SHARED_HOLDER, str(self.base_dir / "drive.lock")],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.stdout.close)  # deferred N3: reaped even when an assertion fails first
        self.addCleanup(holder.stdin.close)
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        self.assertEqual("ready", holder.stdout.readline().strip())
        self.assertIn("shared lock", self.resolve(assignment, code=4)[2]["message"])
        holder.stdin.close()
        holder.wait()
        holder.stdout.close()
        out = self.resolve(assignment, code=0)[1]
        self.assertEqual(("resolved-unknown", "never"), (out["state"], out["spawn"]))

    def test_attempt_resolve_records_sealed_evidence_operator_decision_and_anchored_event(self):
        holder, _ = self.stranded()
        out = self.resolve(holder, code=0)[1]
        path = self.base_dir / out["evidence_ref"]
        data = path.read_bytes()
        self.assertEqual((f"resolutions/{holder}-{sha(data)}.json", sha(data), 0o600),
                         (out["evidence_ref"], out["evidence_sha256"], stat.S_IMODE(path.stat().st_mode)))
        record = json.loads(data)
        self.assertEqual((holder, "recorded", [], "worker host rebooted", "ops:ticket-7"),
                         (record["assignment_id"], record["spawn"], record["cmdline_hits"], record["reason"],
                          record["source_ref"]))
        self.assertRegex(record["inventory_digest"], "^[0-9a-f]{64}$")
        decision = self.coordinator.detail("decision", out["decision_id"])
        self.assertEqual(("operator-reported", "ops:ticket-7", "worker host rebooted", holder, None),
                         (decision["source_kind"], decision["source_ref"], decision["reason"],
                          decision["choice"]["resolved_unknown"], decision["request_id"]))
        events = self.drive_events("assignment.resolved-unknown")
        self.assertEqual([(holder, out["decision_id"], out["evidence_ref"], sha(data))],
                         [(row["subject_id"], row["source_id"], row["evidence_ref"], row["payload_digest"])
                          for row in events])
        self.assertEqual("resolved-unknown", self.state(holder))
        self.assertIn("descendant", out["cost"])

    def test_attempt_resolve_releases_fence_and_allows_release(self):
        holder, _ = self.stranded()
        self.owner = self.driver_owner()
        fenced = self.assign(idempotency_key="fenced")
        self.code("BLOCKED_EVIDENCE", self.coordinator.reserve_launch, self.owner, fenced)
        self.coordinator.cancel_prepared(self.owner, fenced)
        self.assertEqual("RECOVERY_BLOCKED", self.run_cli("release", "--token-file", str(self.drive_token),
                                                          code=4)[2]["error"])
        self.resolve(holder, code=0)
        self.run_cli("release", "--token-file", str(self.drive_token), code=0)
        self.owner = self.coordinator.claim("attended", self.root / "attended.token")
        free = self.assign(idempotency_key="free")
        self.coordinator.reserve_launch(self.owner, free)
        self.assertEqual("launch-pending", self.state(free))

    def test_driver_never_resolves_and_names_attempt_resolve_as_operator_action(self):
        holder, summary = self.stranded()
        for text in ("worker reconcile", f"attempt resolve {holder} --confirm-unknown", "only if needed"):
            self.assertIn(text, summary["operator_action"])
        code, again, _ = self.drive_resume()
        self.assertEqual((11, "attempt-unknown"), (code, again["reason"]))
        self.assertIn(self.state(holder), HOLDING_STATES)
        self.assertEqual([], self.drive_events("assignment.resolved-unknown"))
        for name in ("drive.py", "drive_pipeline.py", "inbox.py"):
            source = (COORDINATOR / "coordinator_core" / name).read_text()
            self.assertNotIn("resolve_unknown_attempt", source)
            self.assertNotIn("resolution_evidence", source)
