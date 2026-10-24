"""Copilot as reviewer, the read mode (Copilot reviewer design revision 6, V1 to V8): a code or spec review staged from
the sealed bundle and the bundle's commit into the run directory, launched under the launch boundary with only view,
grep and glob, the return from the final answer and `bundle_read` from the coordinator's observation. A fake Copilot
emits the record shapes the 1.0.91 probe showed; the tests need a usable bwrap (COORDINATOR_REQUIRE_BWRAP=1 makes its
absence a failure)."""
import json
import os
import shutil
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from coordinator_core import bmad_implementation as impl
from coordinator_core import bmad_review, clients, worker
from coordinator_core.model import DomainError
from coordinator_core.run_receipt import read_binding
from coordinator_core.service import binding_anchor
from tests.bmad_fixture import write
from tests.test_bmad_implementation import GREET, PROJECT, SPEC_PATH, SPEC_READY, TEST, StoryCase
from tests.test_bmad_review import ReviewCase
from tests.test_boundary import REASON

FAKE = r'''#!__PYTHON__
import json, os, pathlib, sys
from datetime import datetime, timezone
args = sys.argv[1:]
if args == ["--version"]:
    print("GitHub Copilot CLI 9.9.9.")
    sys.exit(0)
def flag(name):
    return args[args.index(name) + 1]
assert flag("--available-tools") == "glob,grep,view", args
assert "--allow-tool" not in args and "--add-dir" not in args, args
cwd, home = pathlib.Path(flag("-C")), pathlib.Path(os.environ["COPILOT_HOME"])
repair = "--resume" in args   # the one repair round resumes the run's session; the state already exists
session = flag("--resume") if repair else flag("--session-id")
state = home / "session-state" / session
if repair:
    assert state.is_dir() and "--session-id" not in args, args
    assert json.loads(flag("-p"))["kind"] == "coordinator-read-repair", args
    envelope = json.loads((home.parent / "envelope.json").read_text())
    (home.parent / "repair-count").write_text(str(int((home.parent / "repair-count").read_text() or 0) + 1)
                                              if (home.parent / "repair-count").exists() else "1")
else:
    envelope = json.loads(flag("-p"))
    state.mkdir(parents=True)
    stamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    (state / "workspace.yaml").write_text("id: %s\ncwd: %s\ncreated_at: %s\n" % (session, cwd, stamp))
    (state / "events.jsonl").write_text(json.dumps({"type": "session.shutdown", "data": {"requests": {"m": 1}}}) + "\n")
def emit(kind, data=None, **top):
    print(json.dumps({"type": kind, "data": data or {}, **top}), flush=True)
emit("session.mcp_servers_loaded", {"servers": [{"name": "github-mcp-server", "status": "disabled"}]})
emit("session.usage_checkpoint", {"promptCacheBreakState": [{"models": {"m": {"tools": [
    {"name": n} for n in ("view", "grep", "glob")]}}}]})
views = os.environ.get("FAKE_READ_REPAIR_VIEWS", "[]") if repair else os.environ.get("FAKE_READ_VIEWS", "*")
staged = sorted(str(p) for p in (cwd / "inputs").rglob("*") if p.is_file())
paths = staged if views == "*" else [p for p in staged if any(part in p for part in json.loads(views))]
skip = "" if repair else os.environ.get("FAKE_READ_SKIP", "")
for index, path in enumerate(item for item in paths if not (skip and skip in item)):
    emit("tool.execution_start", {"toolCallId": "c%d" % index, "toolName": "view", "arguments": {"path": path}})
    content = pathlib.Path(path).read_text() if os.path.isfile(path) else ""
    emit("tool.execution_complete", {"toolCallId": "c%d" % index, "success": True, "result": {"content": content}})
answer = {**json.loads(os.environ["FAKE_READ_REPAIR" if repair else "FAKE_READ_REVIEW"]),
          "assignment_id": envelope["assignment_id"], "request_fingerprint": envelope["request_fingerprint"]}
text = json.dumps(answer)
if not repair and os.environ.get("FAKE_READ_PROSE"):
    text = "Here is my review.\n\n```json\n" + text + "\n```"
emit("assistant.message", {"content": text})
emit("result", None, sessionId=session, exitCode=0, usage={"premiumRequests": 1})
'''
QUALIFIED = {"9.9.9": {"code-review": clients.READ_CONTRACT, "spec-review": clients.READ_CONTRACT}}


@unittest.skipIf(REASON and os.environ.get("COORDINATOR_REQUIRE_BWRAP") != "1", f"bwrap unusable: {REASON}")
class ReadReviewTests(StoryCase):
    def setUp(self):
        if REASON:
            self.fail(f"COORDINATOR_REQUIRE_BWRAP=1 but bwrap is unusable: {REASON}")
        super().setUp()
        (self.fake_dir / "copilot").write_text(FAKE.replace("__PYTHON__", sys.executable))
        (self.fake_dir / "copilot").chmod(0o755)
        self.read_env = {"COPILOT_GITHUB_TOKEN": "fake-token", "COORDINATOR_BOUNDARY_PASS": "FAKE_"}
        qualified = patch.dict(clients.READ_QUALIFIED, QUALIFIED, clear=True)
        qualified.start()
        self.addCleanup(qualified.stop)

    def code_bundle(self):
        assignment = impl.prepare(self.story_repo, self.store, self.owner, impl.ImplementationPrepare(
            feature_id=self.fid, key="i1", author_id="story-worker", project=PROJECT))["assignment_id"]
        files = [("pkg/greet.py", GREET), ("tests/test_greet.py", TEST)]
        env = {"FAKE_WRITE": json.dumps([list(item) for item in files]), "FAKE_EXTRA": json.dumps({
            "result": "completed", "unresolved_question": None, "notes": "did the work",
            "verification": [{"command": "unit", "exit_code": 0, "summary": "ok"}],
            "tasks": [{"task": "Implement `greet` in `pkg/greet.py`", "done": True}],
            "changed_paths": [path for path, _ in files]})}
        with patch.dict(os.environ, env):
            self.assertEqual("completed-claim", worker.run_assignment(
                self.story_repo, self.store, self.owner, assignment, "fake-model", 5).state)
        commit = impl.commit_attempt(self.story_repo, self.store, self.owner, self.feature(), assignment, PROJECT)
        impl.verify_attempt(self.story_repo, self.store, self.owner, self.feature(), assignment, commit, PROJECT)
        return impl.bundle_for_commit(self.story_repo, self.store, self.owner, self.fid, assignment)

    def prepare(self, bundle, repo=None, lenses=bmad_review.CODE_LENSES, key="r1", **env):
        with patch.dict(os.environ, {**self.read_env, **env}):
            return bmad_review.review_prepare(repo or self.story_repo, self.store, self.owner, bmad_review.ReviewPrepare(
                bundle["id"], "code-reviewer", key, lenses=list(lenses), client="copilot", boundary="bwrap",
                mode="read"))

    def binding(self, assignment):
        _, bound = binding_anchor(self.store.read_snapshot()["events"], assignment)
        return read_binding(self.base_dir, bound["evidence_ref"], bound["payload_digest"])

    def review(self, assignment, repo=None, findings=(), **env):
        binding = self.binding(assignment)
        answer = {"result": "completed", "changed_paths": [], "checks": [], "descendants": [],
                  "unresolved_question": None, "options": [], "affected_ids": [], "bundle_id": binding["bundle_id"],
                  "manifest_digest": binding["manifest_digest"], "lenses_run": list(binding["lenses"]),
                  "findings": [{"lens": binding["lenses"][0], "location": "pkg/greet.py:1", "trigger_condition": "t",
                                "guard_snippet": "g", "potential_consequence": "c", "severity": "low", **item}
                               for item in findings], "dispositions": []}
        repair = {**answer, **json.loads(env.pop("FAKE_REPAIR_EXTRA", "{}"))}   # the repair round's answer
        answer.update(json.loads(env.pop("FAKE_ANSWER_EXTRA", "{}")))
        with patch.dict(os.environ, {**self.read_env, "FAKE_READ_REVIEW": json.dumps(answer),
                                     "FAKE_READ_REPAIR": json.dumps(repair), **env}):
            return worker.run_assignment(repo or self.story_repo, self.store, self.owner, assignment, "fake-model", 30)

    def repaired(self, report, reason):
        """The one repair round happened for `reason`, and the receipts carry it."""
        run_dir = Path(report.run_dir)
        record = json.loads((run_dir / clients.REPAIR_RECORD).read_text())
        self.assertIn(reason, record["reason"])
        self.assertEqual(("--resume", "1"), (record["argv"][1], (run_dir / "repair-count").read_text()))
        self.assertTrue((run_dir / clients.REPAIR_STREAM).exists())
        return record

    def test_a_code_review_in_read_mode_is_observed_recorded_and_replays_without_its_staged_copy(self):
        prepared = self.prepare(self.code_bundle())
        binding = self.binding(prepared["assignment_id"])
        self.assertEqual(("read", "code-review"), (binding["mode"], binding["read_profile"]))
        kinds = [(item["kind"], item["required"]) for item in binding["read_inputs"]]
        self.assertEqual([("artifact", True), ("artifact", True), ("diff", True), ("spec", True)], kinds)
        self.assertTrue(all(item["alias"].startswith("inputs/") and "/." not in item["alias"]
                            for item in binding["read_inputs"]))
        brief = (self.story_path / prepared["brief_ref"]).read_text()
        self.assertIn("## Read-mode inputs (your only files)", brief)
        self.assertNotIn("`bundle_read` holds one object", brief)
        alias = next(item["alias"] for item in binding["read_inputs"] if item["path"] == "pkg/greet.py")
        report = self.review(prepared["assignment_id"], findings=[{"severity": "medium", "location": f"{alias}:1"}])
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        sealed = json.loads((Path(report.run_dir) / "return.json").read_text())
        self.assertEqual(["pkg/greet.py:1"], [item["location"] for item in sealed["profile_payload"]["findings"]])
        launch = json.loads((Path(report.run_dir) / "launch.json").read_text())
        self.assertEqual(("read", False, str(Path(report.run_dir) / worker.READ_CWD)),
                         (launch["requested_mode"], launch["qualification"], launch["cwd"]))
        modes = {item["path"]: item["mode"] for item in launch["boundary"]["mounts"]}
        self.assertNotIn(str(self.story_path), modes)   # nothing of the project but the run's own directory
        self.assertNotIn(str(self.story_repo.common_dir), modes)
        recorded = bmad_review.record_bound_review(self.story_repo, self.store, self.owner, prepared["assignment_id"])
        self.assertEqual(("observed", False), (recorded["provenance_class"], recorded["critical_open"]))
        shutil.rmtree(Path(report.run_dir) / worker.READ_CWD)
        self.assertEqual("completed-claim", worker.inspect_run(self.story_repo, self.store,
                                                               prepared["assignment_id"]).state)

    def test_a_spec_review_stages_the_rules_from_the_commit(self):
        epic_fid, epic_repo = self.epic_story()
        spec = impl.ImplementationPrepare(feature_id=epic_fid, key="s1", author_id="story-worker", kind="spec",
                                          project={**PROJECT, "context": ["docs/house-rules.md"]})
        from coordinator_core import bmad_spec_author
        first = bmad_spec_author.prepare(epic_repo, self.store, self.owner, spec)["assignment_id"]
        final = {"result": "completed", "notes": "wrote the spec", "open_questions": [], "assumptions": [],
                 "unresolved_question": None, "options": []}
        with patch.dict(os.environ, {"FAKE_WRITE": json.dumps([[SPEC_PATH, SPEC_READY]]),
                                     "FAKE_EXTRA": json.dumps(final)}):
            self.assertEqual("completed-claim", worker.run_assignment(epic_repo, self.store, self.owner, first,
                                                                      "fake-model", 5).state)
        feature = next(row for row in self.store.read_snapshot()["features"] if row["id"] == epic_fid)
        impl.commit_attempt(epic_repo, self.store, self.owner, feature, first, PROJECT)
        bundle = impl.bundle_for_commit(epic_repo, self.store, self.owner, epic_fid, first)
        prepared = self.prepare(bundle, epic_repo, bmad_review.SPEC_LENSES)
        inputs = self.binding(prepared["assignment_id"])["read_inputs"]
        self.assertEqual([("artifact", SPEC_PATH, True), ("context", "docs/house-rules.md", False)],
                         [(item["kind"], item["path"], item["required"]) for item in inputs])
        report = self.review(prepared["assignment_id"], epic_repo, FAKE_READ_SKIP="house-rules")   # needed no rules
        self.assertEqual("completed-claim", report.state, report.evidence_needs)

    def refused(self, need, **env):
        """A refused return fences the worktree, so each refusal is its own test."""
        prepared = self.prepare(self.code_bundle())
        report = self.review(prepared["assignment_id"], **env)
        self.assertEqual("unknown", report.state)
        self.assertIn(need, " ".join(report.evidence_needs))

    def test_a_reviewed_file_not_read_in_either_round_is_unknown(self):
        prepared = self.prepare(self.code_bundle())
        report = self.review(prepared["assignment_id"], FAKE_READ_SKIP="greet.py")
        self.assertEqual("unknown", report.state)
        self.assertIn("did not read pkg/greet.py in full", " ".join(report.evidence_needs))
        self.repaired(report, "did not read pkg/greet.py in full")   # asked once, not answered by a read

    def test_a_missed_read_is_made_up_in_the_repair_round(self):
        prepared = self.prepare(self.code_bundle())
        report = self.review(prepared["assignment_id"], FAKE_READ_SKIP="greet.py",
                             FAKE_READ_REPAIR_VIEWS=json.dumps(["greet.py"]))
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        self.repaired(report, "did not read pkg/greet.py in full")

    def test_a_refused_answer_is_repaired_once_recorded_and_replays(self):
        """§10 R1: a finding missing a field is refused, the resumed session answers again, and the review stands.
        The receipts carry the repair record and stream, and replay checks them."""
        prepared = self.prepare(self.code_bundle())
        bad = {"findings": [{"lens": "acceptance", "location": "pkg/greet.py:1", "guard_snippet": "g",
                             "potential_consequence": "c", "severity": "high"}]}   # no trigger_condition
        report = self.review(prepared["assignment_id"], findings=[{"severity": "high"}],
                             FAKE_ANSWER_EXTRA=json.dumps(bad))
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        record = self.repaired(report, "finding entry has the wrong fields")
        sealed = json.loads((Path(report.run_dir) / "return.json").read_text())
        self.assertEqual((True, record["envelope_sha256"]), (sealed["client_facts"]["repair"], json.loads(
            (Path(report.run_dir) / clients.REPAIR_ENVELOPE).read_bytes()) and record["envelope_sha256"]))
        self.assertIn("repair_sha256", sealed)
        self.assertEqual(["high"], [item["severity"] for item in sealed["profile_payload"]["findings"]])
        bmad_review.record_bound_review(self.story_repo, self.store, self.owner, prepared["assignment_id"])
        shutil.rmtree(Path(report.run_dir) / worker.READ_CWD)
        self.assertEqual("completed-claim", worker.inspect_run(self.story_repo, self.store,
                                                               prepared["assignment_id"]).state)
        (Path(report.run_dir) / clients.REPAIR_STREAM).write_bytes(b"")   # a tampered repair stream is caught
        self.assertEqual("unknown", worker.inspect_run(self.story_repo, self.store, prepared["assignment_id"]).state)

    def test_prose_around_the_first_answer_is_repaired(self):
        prepared = self.prepare(self.code_bundle())
        report = self.review(prepared["assignment_id"], FAKE_READ_PROSE="1")
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        self.repaired(report, "Expecting value")

    def test_a_second_refused_answer_is_unknown_with_no_third_round(self):
        prepared = self.prepare(self.code_bundle())
        bad = json.dumps({"bundle_read": []})   # an attestation the read schema has no room for
        report = self.review(prepared["assignment_id"], FAKE_ANSWER_EXTRA=bad, FAKE_REPAIR_EXTRA=bad)
        self.assertEqual("unknown", report.state)
        self.assertIn("does not match", " ".join(report.evidence_needs))
        self.repaired(report, "does not match")

    def test_an_unqualified_version_refuses_the_prepare_and_a_qualification_run_is_never_observed(self):
        bundle = self.code_bundle()
        with patch.dict(clients.READ_QUALIFIED, {"9.9.9": {"judgment": clients.READ_CONTRACT}}, clear=True):
            with self.assertRaises(DomainError) as caught:
                self.prepare(bundle)
            self.assertEqual("UNSUPPORTED_CLIENT_MODE", caught.exception.code)
            self.assertFalse(any(row["idempotency_key"] == "r1" for row in self.store.read_snapshot()["assignments"]))
            prepared = self.prepare(bundle, COORDINATOR_QUALIFY_READ="1")
            report = self.review(prepared["assignment_id"], COORDINATOR_QUALIFY_READ="1")
        self.assertEqual("unknown", report.state)
        self.assertIn("qualification run", " ".join(report.evidence_needs))
        self.assertTrue((Path(report.run_dir) / "return.json").exists())   # every check ran, nothing was anchored
        reconciled = worker.reconcile_run(self.story_repo, self.store, self.owner, prepared["assignment_id"])
        self.assertEqual("unknown", reconciled.state)   # the sealed return has no anchor, so it is never bound
        row = self.coordinator.detail("assignment", prepared["assignment_id"])
        self.assertNotIn(row["state"], worker.RETURNED)

    def test_a_planning_review_and_a_client_without_a_read_mode_are_refused(self):
        bundle = self.code_bundle()
        with self.assertRaises(DomainError) as caught:
            bmad_review.review_prepare(self.story_repo, self.store, self.owner, bmad_review.ReviewPrepare(
                bundle["id"], "code-reviewer", "r1", lenses=list(bmad_review.CODE_LENSES), client="codex-cli",
                boundary="bwrap", mode="read"))
        self.assertEqual("UNSUPPORTED_CLIENT_MODE", caught.exception.code)
        with self.assertRaises(DomainError) as caught:
            bmad_review.review_prepare(self.story_repo, self.store, self.owner, bmad_review.ReviewPrepare(
                bundle["id"], "code-reviewer", "r1", lenses=list(bmad_review.CODE_LENSES), client="copilot",
                mode="read"))
        self.assertEqual("INVALID_INPUT", caught.exception.code)


@unittest.skipIf(REASON and os.environ.get("COORDINATOR_REQUIRE_BWRAP") != "1", f"bwrap unusable: {REASON}")
class ReadPlanningReviewTests(ReviewCase):
    """V2's planning row: a planning bundle reviewed in read mode stages the sealed files and the review skill's own
    contract files (never resolved, read as copies), and the review record is the ordinary one."""

    def setUp(self):
        if REASON:
            self.fail(f"COORDINATOR_REQUIRE_BWRAP=1 but bwrap is unusable: {REASON}")
        super().setUp()
        (self.fake_dir / "copilot").write_text(FAKE.replace("__PYTHON__", sys.executable))
        (self.fake_dir / "copilot").chmod(0o755)
        patcher = patch.dict(os.environ, {"COPILOT_GITHUB_TOKEN": "fake-token", "COORDINATOR_BOUNDARY_PASS": "FAKE_"})
        patcher.start()
        self.addCleanup(patcher.stop)
        qualified = patch.dict(clients.READ_QUALIFIED, {"9.9.9": {"planning-review": clients.READ_CONTRACT}},
                               clear=True)
        qualified.start()
        self.addCleanup(qualified.stop)

    def test_a_planning_review_in_read_mode_stages_the_review_skill_and_is_recorded(self):
        write(self.t.path, "_bmad/custom/bmad-review.toml", "[workflow]\n")   # an override, part of the contract
        self.compat = self.judge()
        self.authored()
        bundle = self.bundled(dependency_paths=["docs/authority.md"])["id"]
        prepared = self.prepared(bundle, client="copilot", boundary="bwrap", mode="read")
        binding = self.binding_of_review(prepared["assignment_id"])
        inputs = {item["path"]: (item["kind"], item["required"]) for item in binding["read_inputs"]}
        self.assertEqual(("planning-review", ("dependency", True), ("contract", False), ("contract", False)),
                         (binding["read_profile"], inputs["docs/authority.md"],
                          inputs[".agents/skills/bmad-review/SKILL.md"], inputs["_bmad/custom/bmad-review.toml"]))
        self.assertIn(".agents/skills/bmad-review/references/lens-adversarial.md", inputs)
        self.assertFalse(any(path.startswith(".agents/skills/bmad-prd/") for path in inputs))
        self.assertTrue(all(kind == "contract" or required for kind, required in inputs.values()))
        brief = self.brief_of(prepared)
        for text in ("## Read-mode inputs (your only files)", "resolve no customization yourself"):
            self.assertIn(text, brief)
        for text in ("sha256sum", "`bundle_read` holds", "sealed root:"):
            self.assertNotIn(text, brief)
        final = {key: value for key, value in self.final_for(prepared["assignment_id"]).items() if key != "bundle_read"}
        final.update(changed_paths=[], checks=[], descendants=[], unresolved_question=None, options=[],
                     affected_ids=[])
        with patch.dict(os.environ, {"FAKE_READ_REVIEW": json.dumps(final), "FAKE_READ_REPAIR": json.dumps(final)}):
            report = worker.run_assignment(self.t.repo, self.store, self.owner, prepared["assignment_id"],
                                           "fake-model", 30)
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        self.assertEqual("observed", self.record(prepared["assignment_id"])["provenance_class"])
        shutil.rmtree(Path(report.run_dir) / worker.READ_CWD)
        self.assertEqual("completed-claim", worker.inspect_run(self.t.repo, self.store, prepared["assignment_id"]).state)


if __name__ == "__main__":
    unittest.main()
