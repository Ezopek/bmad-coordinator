"""Driver Task 3: the slice-3 path end to end (bundle, bound review, acceptance, architecture from the accepted PRD,
handoff export), the remaining kill rows, the documented drive-loop and the proved-unattended pair. Fake binaries behind
the production registry entries prove coordinator logic on this host only; no live client or model ran."""

import copy
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import uuid
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from coordinator_core import bmad_review, clients, drive, drive_pipeline, exporter, judgment, service
from coordinator_core.bmad_planning import PLAN, REVIEW
from coordinator_core.model import DecisionInput, DomainError, FeatureInput
from coordinator_core.run_receipt import read_binding, receipt_ref, run_name
from coordinator_core.service import DRIVE_EVENTS, binding_anchor
from tests import test_drive_judgment as judged
from tests.drive_support import COORDINATOR, CRITERIA, kill_group, unattended_checks
from tests.support import ext4_mountinfo
from tests.test_bmad_planning import ARCH, PRD, PRD_FILE

USAGE = COORDINATOR / "docs" / "state-core-usage.md"
ROLES = {"worker": {"client": "codex-cli", "model": "fake-model", "timeout": 30},
         "reviewer": {"client": "codex-cli", "model": "fake-review", "timeout": 30},
         "judgment": {"client": "codex-cli", "model": "fake-judge", "timeout": 30}}
# The documented order of the proved run: one action per turn.
PROVED = ["prepare", "launch", "judge", "decide", "prepare", "launch", "bundle", "review-prepare", "launch",
          "review-record", "accept", "prepare", "launch", "bundle", "review-prepare", "launch", "review-record",
          "accept", "export"]
# Every criterion passes except the correction one, which is `trivial` in a run that no review blocked (design D7),
# the four story criteria, which a planning run has no evidence for, and `integration-current`, which needs an epic.
ALL_PASS = {**dict.fromkeys(CRITERIA, "pass"), "correction-observed": "trivial", "attempt-committed": "blocked",
            "verify-observed": "blocked", "status-synced": "blocked", "deferred-recorded": "blocked",
            "integration-current": "blocked"}
# The short pipeline (no question): no judgment and no update to observe; a restart makes the duplicate check `pass`.
SHORT = {**ALL_PASS, "judgment-backed-decision": "blocked", "update-observed": "blocked"}
LONG = 900  # bounded but generous: the host runs other suites and a live smoke concurrently


class Base(judged.Base):
    """The Task 2 judgment base (mandate authority list at revision 2, its compatibility decision, an ext4 judgment
    root, the attended owner released) with reviewer and judgment roles and an export directory outside every feature
    writable scope. No question is asked unless a test calls ask()."""

    def setUp(self):
        super().setUp()
        self.handoff = self.root / "handoff"
        self.handoff.mkdir()
        self.scenario()

    def steps(self, arch=False, export=True, reviewer="reviewer-1"):
        steps = [{"id": "compat", "kind": "compat"},
                 {"id": "prd", "kind": "author", "skill": PRD, "slug": "gitops", "author_id": "prd-author",
                  "inputs": {"docs/input.md": "driving-brief"}},
                 {"id": "prd-bundle", "kind": "bundle", "of": "prd"},
                 {"id": "prd-review", "kind": "review", "of": "prd-bundle", "reviewer_id": reviewer},
                 {"id": "prd-accept", "kind": "accept", "of": "prd-review"}]
        if arch:
            steps += [{"id": "arch", "kind": "author", "skill": ARCH, "slug": "gitops", "altitude": "feature",
                       "purpose": "build-substrate", "prd_from": "prd-accept", "author_id": "arch-author",
                       "inputs": {}},
                      {"id": "arch-bundle", "kind": "bundle", "of": "arch"},
                      {"id": "arch-review", "kind": "review", "of": "arch-bundle", "reviewer_id": reviewer},
                      {"id": "arch-accept", "kind": "accept", "of": "arch-review"}]
        if export:
            steps.append({"id": "handoff", "kind": "export", "to": str(self.handoff)})
        return steps

    def pipeline(self, steps=None, **over):
        return super().pipeline(steps if steps is not None else self.steps(), **{"roles": ROLES, **over})

    def full(self):
        return self.pipeline(self.steps(arch=True))

    # Runs ------------------------------------------------------------------------------------------------------
    def fresh(self, value=None, name="drive2.token"):
        """A new `drive start` (new lease) after a released run, in process."""
        return drive.start(self.t.repo, self.fid, value or self.pipeline(), self.root / name,
                           drive.RunBounds(60, 3600), str(self.jroot))

    def fresh_again(self, name="drive2.token"):
        return drive.resume(self.t.repo, self.driver_owner(self.root / name), drive.RunBounds(60, 3600))

    def child_start(self, pipeline=None, kill_at=None):
        return self.child_driver(self.start_args(pipeline, None, *self.root_args(), "--max-turns", "60"),
                                 kill_at=kill_at)

    def killed_run(self, kill_at, pipeline=None):
        child = self.child_start(pipeline, kill_at)
        self.assertEqual(-signal.SIGKILL, self.finish(child, timeout=LONG)[0])

    def cli_resume(self):
        code, summary, _ = self.run_cli(*self.resume_args(None, "--max-turns", "60"), timeout=LONG)
        return code, summary

    def assert_resumed_unattended(self, code, summary):
        """Review M1: a kill row resumes to done with one restart, zero interventions and the unattended criteria."""
        self.assertEqual((0, "done", 1, []), (code, summary["outcome"], summary["restarts"],
                                              summary["interventions"]), summary)
        self.assertEqual(SHORT, unattended_checks(self.t.repo, [summary["lease_id"]]))

    def snippet(self) -> str:
        match = re.search(r"^# drive-loop: begin\n.*?^# drive-loop: end\n", USAGE.read_text(), re.S | re.M)
        self.assertIsNotNone(match, "the drive-loop snippet is marked in the usage docs")
        return match.group(0)

    # State -----------------------------------------------------------------------------------------------------
    def actions(self, summary) -> list[dict]:
        run = self.base_dir / "drives" / summary["lease_id"] / summary["run_id"]
        turns = sorted(run.glob("turn-*.json"), key=lambda path: int(path.stem.split("-")[1]))
        return [json.loads(path.read_bytes())["action"] for path in turns]

    def bound(self, profile):
        found = []
        snapshot = self.snapshot()
        for row in snapshot["assignments"]:
            if row["feature_id"] != self.fid or row["state"] == "cancelled-before-launch":
                continue
            try:
                name, anchor = binding_anchor(snapshot["events"], row["id"])
            except DomainError:  # a half-finished prepare: required profile, no binding yet
                continue
            if name == profile and anchor is not None:
                found.append((row, read_binding(self.base_dir, anchor["evidence_ref"], anchor["payload_digest"])))
        return found

    def one(self, table):
        rows = self.snapshot()[table]
        self.assertEqual(1, len(rows), rows)
        return rows[0]

    def checkpoints(self) -> list[Path]:
        return sorted((path for path in self.handoff.iterdir() if path.name.startswith("checkpoint-r")),
                      key=lambda path: int(path.name.split("-")[1][1:]))

    def tree_bytes(self, path: Path) -> dict:
        return {item.name: item.read_bytes() for item in sorted(path.iterdir())}

    def verified(self) -> dict:
        return {item["acceptance_id"]: item["current"] for item in bmad_review.verify_bmad(self.store)["checked"]}

    def source(self, row) -> str:
        return json.loads(row["return_json"])["source_id"]

    def lease(self, summary) -> dict:
        return json.loads((self.base_dir / "drives" / summary["lease_id"] / "lease.json").read_bytes())

    def reviewer_launches(self):
        return [item for item in self.workers() if "-review-" in item["key"]]


class StepTests(Base):
    def test_bundle_review_accept_export_steps_wrap_slice3_gates_and_replay_idempotently(self):
        summary, code = self.run_in(max_turns=2)
        self.assertEqual((10, "max-turns"), (code, summary["reason"]), summary)
        prd, observed = self.t.path / PRD_FILE, (self.t.path / PRD_FILE).read_bytes()
        prd.write_text("# hand edit after the observed return\n")
        summary, code = self.again(max_turns=60)  # the bundle gate of slice 3, relayed with its code
        self.assertEqual((11, "stale-input", "BLOCKED_EVIDENCE"), (code, summary["reason"],
                                                                  summary["subjects"]["gate"]), summary)
        self.assertEqual([], self.snapshot()["bundles"])
        prd.write_bytes(observed)
        summary, code = self.again(max_turns=60)
        self.assertEqual((0, "done", "released"), (code, summary["outcome"], summary["ownership"]), summary)
        self.assertEqual(["bundle", "review-prepare", "launch", "review-record", "accept", "export"],
                         [item["kind"] for item in self.actions(summary)])
        bundle, review, acceptance = self.one("bundles"), self.one("reviews"), self.one("acceptances")
        (author, _), = self.bound(PLAN)
        (reviewer, binding), = self.bound(REVIEW)
        sha = drive_pipeline.digest(self.pipeline())
        self.assertEqual([author["id"]], json.loads(bundle["source_assignments_json"]))
        self.assertEqual((f"drv-{sha[:12]}-prd-review-1", bundle["id"], self.compat, "reviewer-1", "read-only"),
                         (reviewer["idempotency_key"], binding["bundle_id"], binding["compat_decision_id"],
                          reviewer["author_id"], reviewer["permission_mode"]))
        self.assertEqual((bundle["id"], "observed", 0,
                          receipt_ref(run_name(reviewer["id"], reviewer["request_fingerprint"]), "return")),
                         (review["bundle_id"], review["provenance_class"], review["critical_open"],
                          review["provenance_ref"]))
        self.assertEqual(((bundle["id"], review["id"]), {acceptance["id"]: "current"}),
                         ((acceptance["bundle_id"], acceptance["review_id"]), self.verified()))
        (checkpoint,) = self.checkpoints()
        self.assertEqual(checkpoint.name, self.snapshot()["export_dir_name"])
        # Replay: the slice-3 wrappers return the stored rows, and a second drive start finds every step satisfied.
        attended = self.coordinator.claim("attended", self.root / "attended.token")
        again = bmad_review.review_prepare(self.t.repo, self.store, attended, bmad_review.ReviewPrepare(
            bundle["id"], "reviewer-1", reviewer["idempotency_key"], self.compat, client="codex-cli"))
        self.assertEqual(reviewer["id"], again["assignment_id"])
        self.assertEqual(review["id"], bmad_review.record_bound_review(self.t.repo, self.store, attended,
                                                                       reviewer["id"])["id"])
        self.coordinator.release(attended)
        before = {table: len(self.snapshot()[table]) for table in ("assignments", "bundles", "reviews", "acceptances")}
        summary, code = self.fresh()
        self.assertEqual((0, "done", 0, {}), (code, summary["outcome"], summary["counts"]["turns"],
                                              summary["counts"]["launches"]), summary)
        self.assertEqual(before, {table: len(self.snapshot()[table]) for table in before})
        self.assertEqual([checkpoint], self.checkpoints())

    def test_architecture_step_consumes_accepted_prd_from_pipeline(self):
        summary, code = self.run_in(max_turns=60, value=self.pipeline(self.steps(arch=True, export=False)))
        self.assertEqual((0, "done"), (code, summary["outcome"]), summary)
        snapshot = self.snapshot()
        bundles = {json.loads(row["source_assignments_json"])[0]: row for row in snapshot["bundles"]}
        plans = {binding["skill"]: (row, binding) for row, binding in self.bound(PLAN)}
        prd_bundle, arch_bundle = bundles[plans[PRD][0]["id"]], bundles[plans[ARCH][0]["id"]]
        accepted = {row["bundle_id"]: row["id"] for row in snapshot["acceptances"]}
        arch = plans[ARCH][1]
        self.assertEqual((accepted[prd_bundle["id"]], "accepted-prd"),
                         (arch["prd_acceptance"], arch["input_roles"][PRD_FILE]))
        files = self.coordinator.detail("bundle", arch_bundle["id"])["files"]
        self.assertIn({"kind": "dependency", "path": PRD_FILE},
                      [{"kind": item["kind"], "path": item["path"]} for item in files])
        self.assertEqual({accepted[prd_bundle["id"]]: "current", accepted[arch_bundle["id"]]: "current"},
                         self.verified())
        self.assertEqual(["satisfied"] * 9, [step["state"] for step in summary["steps"]])

    def test_reviewer_distinct_from_authors_and_judgment_run(self):
        self.ask()
        summary, code = self.run_in(max_turns=60, value=self.pipeline(self.steps(export=False)))
        self.assertEqual((0, "done"), (code, summary["outcome"]), summary)
        (reviewer, binding), = self.bound(REVIEW)
        authors = self.bound(PLAN)
        self.assertEqual((2, {"prd-author"}), (len(authors), {row["author_id"] for row, _ in authors}))
        self.assertNotIn(reviewer["author_id"], {row["author_id"] for row, _ in authors})
        self.assertNotIn(reviewer["id"], binding["contributor_assignment_ids"])
        (decision,) = judgment.decisions(self.snapshot(), self.fid)
        returned = drive_pipeline.read_sealed_json(self.base_dir, decision["receipt_ref"], decision["receipt_sha256"])
        sources = {self.source(row) for row, _ in authors}
        self.assertEqual(2, len(sources))
        self.assertNotIn(self.source(reviewer), sources | {returned["source_id"]})
        self.assertNotIn(returned["source_id"], sources)
        self.assertEqual(sorted(sources), binding["contributor_source_ids"])
        self.code("INVALID_INPUT", drive_pipeline.validate, self.pipeline(self.steps(reviewer="prd-author")))

    def test_non_critical_findings_do_not_block_acceptance(self):
        """Medium and low findings never block; `high` ones block by default (follow-up `accept_on_high`, below)."""
        self.scenario(steps={"prd-review": {"findings": [{"severity": "medium"}, {"severity": "low"}]}})
        summary, code = self.run_in(max_turns=60, value=self.pipeline(self.steps(export=False)))
        self.assertEqual((0, "done"), (code, summary["outcome"]), summary)
        review, acceptance = self.one("reviews"), self.one("acceptances")
        self.assertEqual((0, ["medium", "low"]),
                         (review["critical_open"], [item["severity"] for item in
                                                    json.loads(review["finding_json"])["finding"]["findings"]]))
        self.assertEqual({acceptance["id"]: "current"}, self.verified())

    def test_open_high_findings_need_human_by_default_and_attended_accept_lists_them(self):
        """Follow-up `accept_on_high` (Claude smoke attempt 8): by default an open `high` finding stops the driver at
        the accept step with the finding ids and no acceptance; the attended `bmad accept` keeps the critical-only rule
        and lists the open high findings."""
        self.scenario(steps={"prd-review": {"findings": [{"severity": "high"}, {"severity": "medium"}]}})
        summary, code = self.run_in(max_turns=60)
        self.assertEqual((11, "open-high-findings", ["/findings/0"], False),
                         (code, summary["reason"], summary["subjects"]["finding_ids"],
                          self.lease(summary)["accept_on_high"]), summary)
        review, bundle = self.one("reviews"), self.one("bundles")
        self.assertEqual((0, review["id"], bundle["id"], [], []),
                         (review["critical_open"], summary["subjects"]["review_id"], summary["subjects"]["bundle_id"],
                          self.snapshot()["acceptances"], self.checkpoints()))
        self.assertIn("with the driver's token while no driver runs, then drive resume", summary["operator_action"])
        self.assertIn(f"bmad accept review {review['id']}", summary["operator_action"])  # the path this test takes
        summary, code = self.again(max_turns=60)
        self.assertEqual((11, "open-high-findings", 0, 1), (code, summary["reason"], summary["counts"]["turns"],
                                                            len(self.reviewer_launches())), summary)
        accepted = self.run_cli("bmad", "accept", "--token-file", str(self.drive_token),
                                data={"bundle_id": bundle["id"], "review_id": review["id"]}, code=0)[1]
        self.assertEqual((["/findings/0"], {accepted["id"]: "current"}),
                         (accepted["open_high_findings"], self.verified()))
        summary, code = self.again(max_turns=60)
        self.assertEqual((0, "done", ["export"]), (code, summary["outcome"],
                                                   [item["kind"] for item in self.actions(summary)]), summary)

    def test_open_high_findings_accepted_when_the_pipeline_sets_accept_on_high(self):
        self.code("INVALID_INPUT", drive_pipeline.validate, self.pipeline(accept_on_high="yes"))
        view = {"finding": {"findings": [{"severity": "low"}], "dispositions": [{"severity": "high"}]}}
        self.assertEqual((["/dispositions/0"], ["/"]), (drive_pipeline.open_high_findings(json.dumps(view)),
                                                        drive_pipeline.open_high_findings("not json")))
        self.scenario(steps={"prd-review": {"findings": [{"severity": "high"}]}})
        summary, code = self.run_in(max_turns=60, value=self.pipeline(accept_on_high=True))
        self.assertEqual((0, "done", True), (code, summary["outcome"], self.lease(summary)["accept_on_high"]), summary)
        acceptance = self.one("acceptances")
        self.assertEqual(({acceptance["id"]: "current"}, 1), (self.verified(), len(self.checkpoints())))

    def test_critical_finding_needs_human_with_review_id_and_no_acceptance(self):
        self.scenario(steps={"prd-review": {"findings": [{"severity": "critical"}]}})
        summary, code = self.run_in(max_turns=60)
        self.assertEqual((11, "needs-human", "critical-finding"), (code, summary["outcome"], summary["reason"]),
                         summary)
        review, bundle = self.one("reviews"), self.one("bundles")
        self.assertEqual((1, review["id"], bundle["id"]),
                         (review["critical_open"], summary["subjects"]["review_id"], summary["subjects"]["bundle_id"]))
        self.assertEqual(([], []), (self.snapshot()["acceptances"], self.checkpoints()))
        self.assertEqual(f"release --token-file {drive.TOKEN}, attended repair, then drive start",
                         summary["operator_action"])  # default budget: no correction round
        self.assertEqual(({"used": 0, "max": 0}, 0), (summary["subjects"]["corrections"],
                                                       summary["counts"]["corrections"]))  # default budget: as before
        summary, code = self.again(max_turns=60)
        self.assertEqual((11, "critical-finding", 0, {}), (code, summary["reason"], summary["counts"]["turns"],
                                                           summary["counts"]["launches"]), summary)
        self.assertEqual((1, 1, []), (len(self.reviewer_launches()), len(self.snapshot()["reviews"]),
                                      self.snapshot()["acceptances"]))

    def test_acceptance_turned_stale_needs_human_and_is_not_redone(self):
        value = self.pipeline(self.steps(export=False))
        summary, code = self.run_in(max_turns=60, value=value)
        self.assertEqual((0, "done"), (code, summary["outcome"]), summary)
        acceptance = self.one("acceptances")
        (self.t.path / PRD_FILE).write_text("# edited after acceptance\n")
        self.assertEqual({acceptance["id"]: "stale"}, self.verified())
        before = {table: len(self.snapshot()[table]) for table in ("assignments", "bundles", "reviews", "acceptances")}
        for run in (lambda: self.fresh(value), self.fresh_again):
            summary, code = run()
            self.assertEqual((11, "acceptance-stale", acceptance["id"], 0),
                             (code, summary["reason"], summary["subjects"]["acceptance_id"],
                              summary["counts"]["turns"]), summary)
        self.assertEqual(before, {table: len(self.snapshot()[table]) for table in before})
        self.assertEqual(1, len(self.reviewer_launches()))

    def test_export_step_satisfied_after_last_domain_event_and_never_overwrites(self):
        summary, code = self.run_in(max_turns=60)
        self.assertEqual((0, "done"), (code, summary["outcome"]), summary)
        (first,) = self.checkpoints()
        kept = self.tree_bytes(first)
        snapshot = self.snapshot()
        exported = snapshot["export_revision"]
        self.assertEqual((first.name, 1), (snapshot["export_dir_name"], snapshot["export_pending"]))
        self.assertLessEqual(drive_pipeline.last_domain_revision(snapshot, self.fid), exported)
        self.assertEqual("satisfied", drive.status(self.t.repo, self.fid)["features"][0]["steps"][-1]["state"])
        attended = self.coordinator.claim("attended", self.root / "attended.token")
        self.coordinator.record_decision(attended, DecisionInput(
            self.fid, 2, "operator-reported", "chat:later", "a later attended decision", {"note": "later"}))
        self.coordinator.release(attended)
        handoff = drive.status(self.t.repo, self.fid)["features"][0]["steps"][-1]
        self.assertEqual(("ready", "export"), (handoff["state"], handoff["action"]["kind"]))
        summary, code = self.fresh()
        self.assertEqual((0, "done"), (code, summary["outcome"]), summary)
        self.assertEqual(["export"], [item["kind"] for item in self.actions(summary)])
        older, newer = self.checkpoints()
        self.assertEqual((first, kept), (older, self.tree_bytes(older)))
        self.assertGreater(int(newer.name.split("-")[1][1:]), exported)
        self.assertEqual(newer.name, self.snapshot()["export_dir_name"])

    def test_export_currentness_ignores_later_drive_and_owner_events(self):
        summary, code = self.run_in(max_turns=60)
        self.assertEqual((0, "done"), (code, summary["outcome"]), summary)
        attended = self.coordinator.claim("attended", self.root / "attended.token")
        self.coordinator.release(attended)
        summary, code = self.fresh()
        self.assertEqual((0, "done", 0), (code, summary["outcome"], summary["counts"]["turns"]), summary)
        snapshot = self.snapshot()
        later = {row["kind"] for row in snapshot["events"] if row["revision"] > snapshot["export_revision"]}
        self.assertTrue(later)
        self.assertTrue(all(kind in DRIVE_EVENTS or kind.startswith("owner.") for kind in later), later)
        self.assertEqual((1, 1), (snapshot["export_pending"], len(self.checkpoints())))
        # Pure derivation: a later drive event leaves the export current; a later domain event of the feature does not.
        value = self.pipeline()
        facts = drive_pipeline.gather(self.t.repo, self.store, snapshot, value, snapshot["owner"]["generation"])
        decision = snapshot["decisions"][0]["id"]
        for kind, subject, state in (("drive.turn", "lease", "satisfied"), ("owner.claimed", "9", "satisfied"),
                                     ("decision.recorded", decision, "ready")):
            changed = copy.deepcopy(snapshot)
            changed["events"].append({"id": "e", "revision": snapshot["revision"] + 1, "kind": kind,
                                      "subject_id": subject, "source_id": None, "evidence_ref": None,
                                      "payload_digest": None})
            steps = drive_pipeline.derive(value, drive_pipeline.digest(value), changed, facts)
            self.assertEqual(state, steps[-1].state, kind)

    def test_steps_outside_slice3_path_refused_at_bind(self):
        author = self.steps()[1]
        compat, wrappers = self.steps()[0], self.steps()[2:]
        cases = {
            "epics": [compat, author, {"id": "epics", "kind": "epics"}],
            "sprint status": [compat, author, {"id": "sprint", "kind": "sprint-status"}],
            "loop": [compat, author, {"id": "loop", "kind": "loop"}],
            "bmad-spec": [compat, {**author, "skill": "bmad-spec"}],
            "bmad-ux": [compat, {**author, "skill": "bmad-ux"}],
            "validate": [compat, {**author, "intent": "validate"}],
            "validate key": [compat, author, wrappers[0], {**wrappers[1], "validate": True}],
            "intent update": [compat, {**author, "intent": "update"}],
            "branches": [compat, {**author, "branches": ["a", "b"]}],
            "bundle branches": [compat, author, {**wrappers[0], "branches": ["a"]}],
            "review skill": [compat, author, wrappers[0], {**wrappers[1], "skill": "bmad-review"}],
        }
        readonly = {name for name, client in clients.REGISTRY.items() if client.write_free_readonly}
        for name in sorted(set(clients.REGISTRY) - readonly):  # review M6: e.g. the author-only `copilot`
            roles = {**ROLES, "reviewer": {**ROLES["reviewer"], "client": name}}
            self.code("UNSUPPORTED_STEP", drive_pipeline.validate, self.pipeline(roles=roles))
        revision = self.snapshot()["revision"]
        listing = sorted(str(path) for path in self.base_dir.rglob("*"))
        for name, steps in cases.items():
            with self.subTest(name=name):
                self.code("UNSUPPORTED_STEP", drive_pipeline.validate, self.pipeline(steps))
                pipeline = self.pipeline_file(self.pipeline(steps), "refused.json")
                self.assertEqual("UNSUPPORTED_STEP", self.go(code=4, pipeline=pipeline)[2]["error"])
        self.assertEqual(revision, self.snapshot()["revision"])
        self.assertEqual(listing, sorted(str(path) for path in self.base_dir.rglob("*")))
        self.assertFalse(self.drive_token.exists())


class CorrectionTests(Base):
    """Correction-round design §5: a blocking review triggers a bounded correction of the author step, which is
    re-bundled, re-reviewed and accepted; the budget spent, the run stops as before with the count."""

    CORRECTED = ["prepare", "launch", "bundle", "review-prepare", "launch", "review-record",
                 "prepare", "launch", "bundle", "review-prepare", "launch", "review-record", "accept"]

    def correcting(self, max_corrections=1, max_judgments=0, max_updates=2, **over):
        return self.pipeline(self.steps(export=False), budgets={"max_updates": max_updates,
                                                                "max_judgments": max_judgments,
                                                                "max_corrections": max_corrections}, **over)

    def plans(self):
        return sorted(self.bound(PLAN), key=lambda pair: pair[0]["idempotency_key"])

    def reviews(self):
        """Recorded reviews in recording order."""
        order = {row["subject_id"]: row["revision"] for row in self.snapshot()["events"]
                 if row["kind"] == "review.recorded"}
        return sorted(self.snapshot()["reviews"], key=lambda row: order[row["id"]])

    def lease_of(self):
        """The one lease id, from its `drive.leased` event (readable before the run ends)."""
        (row,) = [row for row in self.snapshot()["events"] if row["kind"] == "drive.leased"]
        return row["subject_id"]

    def test_critical_finding_corrected_re_reviewed_and_accepted(self):
        self.scenario(steps={"prd-review-1": {"findings": [{"severity": "critical"}]}, "prd-review-2": {}})
        summary, code = self.run_in(max_turns=60, value=self.correcting())
        self.assertEqual((0, "done", 1, {"worker": 2, "reviewer": 2}),
                         (code, summary["outcome"], summary["counts"]["corrections"], summary["counts"]["launches"]),
                         summary)
        self.assertEqual(self.CORRECTED, [item["kind"] for item in self.actions(summary)])
        (first, _), (correction, binding) = self.plans()
        critical, clean = self.reviews()
        self.assertEqual(("update", {"update_of": first["id"], "request": None, "corrects_review": critical["id"],
                                     "finding_ids": ["/findings/0"]}), (binding["intent"], binding["continues"]))
        self.assertEqual((1, 0), (critical["critical_open"], clean["critical_open"]))
        self.assertEqual(critical["id"], self.actions(summary)[6]["corrects_review"])
        bundles = {tuple(json.loads(row["source_assignments_json"])): row["id"] for row in self.snapshot()["bundles"]}
        self.assertEqual({(first["id"],): critical["bundle_id"],
                          tuple(sorted((first["id"], correction["id"]))): clean["bundle_id"]}, bundles)
        acceptance = self.one("acceptances")
        self.assertEqual((clean["bundle_id"], clean["id"], {acceptance["id"]: "current"}),
                         (acceptance["bundle_id"], acceptance["review_id"], self.verified()))
        brief = (self.t.path / correction["brief_ref"]).read_text()
        self.assertIn("## Correction after a recorded review", brief)
        self.assertIn('- `/findings/0` (critical, lens `adversarial`) at "prd.md:1"', brief)
        self.assertEqual(1, drive.status(self.t.repo, self.fid)["leases"][0]["last_run"]["corrections"])
        self.assertEqual({1}, set(Counter(item["key"] for item in self.launch_log()).values()))

    def test_persistent_critical_stops_after_the_budget_with_the_count(self):
        self.scenario(steps={"prd-review": {"findings": [{"severity": "critical"}]}})  # every review is critical
        summary, code = self.run_in(max_turns=60, value=self.correcting(max_corrections=1))
        self.assertEqual((11, "critical-finding", {"used": 1, "max": 1}, 1),
                         (code, summary["reason"], summary["subjects"]["corrections"],
                          summary["counts"]["corrections"]), summary)
        self.assertEqual((2, 2, 2, []), (len(self.bound(PLAN)), len(self.reviewer_launches()),
                                         len(self.snapshot()["reviews"]), self.snapshot()["acceptances"]))
        self.assertEqual(self.reviews()[1]["id"], summary["subjects"]["review_id"])
        summary, code = self.again(max_turns=60)
        self.assertEqual((11, "critical-finding", 0, 0, {"used": 1, "max": 1}),
                         (code, summary["reason"], summary["counts"]["turns"], summary["counts"]["corrections"],
                          summary["subjects"]["corrections"]), summary)
        self.assertEqual((2, 2), (len(self.bound(PLAN)), len(self.snapshot()["reviews"])))

    def test_high_finding_is_corrected_only_without_accept_on_high(self):
        self.scenario(steps={"prd-review-1": {"findings": [{"severity": "high"}, {"severity": "medium"}]},
                             "prd-review-2": {}})
        summary, code = self.run_in(max_turns=60, value=self.correcting(accept_on_high=True))
        self.assertEqual((0, "done", 0, 1, 1), (code, summary["outcome"], summary["counts"]["corrections"],
                                                 len(self.reviewer_launches()), len(self.bound(PLAN))), summary)

    def test_high_finding_without_accept_on_high_is_corrected_naming_only_blocking_items(self):
        self.scenario(steps={"prd-review-1": {"findings": [{"severity": "medium"}, {"severity": "high"}]},
                             "prd-review-2": {}})
        summary, code = self.run_in(max_turns=60, value=self.correcting())
        self.assertEqual((0, "done", 1), (code, summary["outcome"], summary["counts"]["corrections"]), summary)
        (_, binding) = self.plans()[-1]
        self.assertEqual(["/findings/1"], binding["continues"]["finding_ids"])  # the medium item is not named

    def test_spent_budget_then_attended_accept_resumes_without_a_third_correction(self):
        self.scenario(steps={"prd-review": {"findings": [{"severity": "high"}]}})
        summary, code = self.run_in(max_turns=60, value=self.correcting(max_corrections=1))
        self.assertEqual((11, "open-high-findings", {"used": 1, "max": 1}),
                         (code, summary["reason"], summary["subjects"]["corrections"]), summary)
        review, bundle = summary["subjects"]["review_id"], summary["subjects"]["bundle_id"]
        self.assertEqual(self.reviews()[1]["id"], review)
        accepted = self.run_cli("bmad", "accept", "--token-file", str(self.drive_token),
                                data={"bundle_id": bundle, "review_id": review}, code=0)[1]
        self.assertEqual(["/findings/0"], accepted["open_high_findings"])
        summary, code = self.again(max_turns=60)
        self.assertEqual((0, "done", 0, []), (code, summary["outcome"], summary["counts"]["corrections"],
                                              self.actions(summary)), summary)
        self.assertEqual(2, len(self.bound(PLAN)))

    def test_blocked_correction_needs_human_as_any_attempt(self):
        self.scenario(steps={"prd-review-1": {"findings": [{"severity": "critical"}]}, "prd-2": {"status": "blocked"}})
        summary, code = self.run_in(max_turns=60, value=self.correcting())
        self.assertEqual((11, "worker-blocked", 1), (code, summary["reason"], summary["counts"]["corrections"]),
                         summary)
        self.assertEqual(self.plans()[-1][0]["id"], summary["subjects"]["assignment_id"])

    def test_correction_that_asks_a_question_is_judged_and_continued(self):
        question = next(iter(judged.QUESTION.values()))  # the one-question create entry, reused for the correction
        self.ask(steps={"prd-review-1": {"findings": [{"severity": "critical"}]}, "prd-2": question})
        value = self.correcting(max_judgments=1, max_updates=1)  # the budgets are disjoint (design D5)
        summary, code = self.run_in(max_turns=60, value=value)
        self.assertEqual(1, drive_pipeline.load(self.base_dir, summary["pipeline_sha256"])["budgets"]["max_updates"])
        self.assertEqual((0, "done", 1, {"worker": 3, "reviewer": 2, "judgment": 1}),
                         (code, summary["outcome"], summary["counts"]["corrections"], summary["counts"]["launches"]),
                         summary)
        self.assertEqual(self.CORRECTED[:8] + ["judge", "decide", "prepare", "launch"] + self.CORRECTED[8:],
                         [item["kind"] for item in self.actions(summary)])
        (first, _), (correction, corrects), (update, continues) = self.plans()
        self.assertEqual((self.reviews()[0]["id"], None), (corrects["continues"]["corrects_review"],
                                                            corrects["continues"]["request"]))
        self.assertEqual((correction["id"], None), (continues["continues"]["update_of"],
                                                    continues["continues"].get("corrects_review")))
        self.assertTrue(continues["continues"]["request"])
        self.assertEqual(["A1"], [item["authority_id"] for item in summary["decisions"]
                                  if item["source_kind"] == "coordinator"])
        # Ruling R7: a correction that asked is scored through its chain end, the continuation a later bundle names.
        checks = unattended_checks(self.t.repo, [summary["lease_id"]])
        self.assertEqual(("pass", "pass"), (checks["correction-observed"], checks["update-observed"]), checks)

    def test_correction_brief_carries_the_chain_decision_and_the_create_inputs(self):
        """Ruling R6: the correction shows, through the existing brief sections, the frozen inputs the reviewer rated
        against and the decision that resolved the chain's question before the correction."""
        critical = {"findings": [{"severity": "critical"}]}
        self.ask(steps={**judged.QUESTION, "prd-review-1": critical, "prd-review-2": {}})
        summary, code = self.run_in(max_turns=60, value=self.correcting(max_judgments=1, max_updates=1))
        self.assertEqual((0, "done", 1, {"worker": 3, "reviewer": 2, "judgment": 1}),
                         (code, summary["outcome"], summary["counts"]["corrections"], summary["counts"]["launches"]),
                         summary)
        (_, created), (update, continues), (correction, corrects) = self.plans()
        self.assertEqual((update["id"], True), (corrects["continues"]["update_of"],
                                                bool(continues["continues"]["request"])))
        # The head (the request continuation) binds no inputs; the correction carries its chain's create's.
        self.assertEqual(({"docs/input.md": "driving-brief"}, {}, created["input_roles"]),
                         (created["input_roles"], continues["input_roles"], corrects["input_roles"]))
        (decision,) = [row for row in self.snapshot()["decisions"] if row["request_id"]]
        brief = (self.t.path / correction["brief_ref"]).read_text()
        frozen = brief.split("## Frozen inputs (read only)\n")[1].split("\n\n")[0]
        self.assertEqual("- `docs/input.md` — declared source: driving-brief", frozen)
        self.assertIn("## Recorded decisions", brief)
        self.assertIn(f"- `{decision['id']}` (coordinator, {decision['source_ref']}); reason: ", brief)
        self.assertNotIn("## Caller summary", brief)  # an empty text adds no caller line

    def test_prd_correction_then_architecture_and_export_reach_done(self):
        """Review M6 probe (a): a corrected PRD is accepted, then the architecture consumes it and the export runs."""
        self.scenario(steps={"prd-review-1": {"findings": [{"severity": "critical"}]}, "prd-review-2": {}})
        value = self.pipeline(self.steps(arch=True), budgets={"max_updates": 2, "max_judgments": 0,
                                                              "max_corrections": 1})
        summary, code = self.run_in(max_turns=60, value=value)
        self.assertEqual((0, "done", 1, {"worker": 3, "reviewer": 3}),
                         (code, summary["outcome"], summary["counts"]["corrections"], summary["counts"]["launches"]),
                         summary)
        acceptances = self.snapshot()["acceptances"]
        self.assertEqual((2, 1), (len(acceptances), len(self.checkpoints())))
        (_, arch), = [pair for pair in self.plans() if pair[1]["skill"] == ARCH]
        self.assertIn(arch["prd_acceptance"], {row["id"] for row in acceptances})

    def test_architecture_correction_keeps_the_accepted_prd_and_its_inherited_decision(self):
        """Review M6 probe (b): an architecture correction carries the create's PRD acceptance and accepted-prd input,
        so the decision sealed in the accepted PRD bundle reaches it as an inherited decision."""
        critical = {"findings": [{"severity": "critical"}]}
        self.ask(steps={**judged.QUESTION, "arch-review-1": critical, "arch-review-2": {}})
        value = self.pipeline(self.steps(arch=True), budgets={"max_updates": 1, "max_judgments": 1,
                                                              "max_corrections": 1})
        summary, code = self.run_in(max_turns=60, value=value)
        self.assertEqual((0, "done", 1, {"worker": 4, "reviewer": 3, "judgment": 1}),
                         (code, summary["outcome"], summary["counts"]["corrections"], summary["counts"]["launches"]),
                         summary)
        (create, created), (correction, corrects) = [pair for pair in self.plans() if pair[1]["skill"] == ARCH]
        self.assertEqual(create["id"], corrects["continues"]["update_of"])
        self.assertTrue(created["prd_acceptance"])
        self.assertEqual((created["prd_acceptance"], "accepted-prd"),
                         (corrects["prd_acceptance"], corrects["input_roles"][PRD_FILE]))
        (decision,) = [row for row in self.snapshot()["decisions"] if row["request_id"]]
        brief = (self.t.path / correction["brief_ref"]).read_text()
        self.assertIn(f"- `{PRD_FILE}` — declared source: accepted-prd", brief)
        self.assertIn("## Inherited decisions", brief)
        self.assertIn(f"- `{decision['id']}` (coordinator, {decision['source_ref']}, mandate revision 2; accepted "
                      "bundle ", brief)
        self.assertNotIn("## Recorded decisions", brief)  # the architecture chain asked nothing

    def test_unattended_checks_score_the_correction(self):
        self.scenario(steps={"prd-review-1": {"findings": [{"severity": "critical"}]}, "prd-review-2": {}})
        summary, code = self.run_in(max_turns=60, value=self.correcting())
        self.assertEqual((0, "done"), (code, summary["outcome"]), summary)
        lease = [summary["lease_id"]]
        expected = {**ALL_PASS, "judgment-backed-decision": "blocked", "update-observed": "blocked",
                    "correction-observed": "pass", "no-duplicate-after-restart": "trivial"}  # one run, no restart
        self.assertEqual(expected, unattended_checks(self.t.repo, lease))
        # Anchored evidence that no longer reads back is a contradiction: a tampered correction binding fails the
        # criterion (through the scorer's existing except clause), whatever else it breaks.
        (_, (correction, _)) = self.plans()
        anchor = binding_anchor(self.snapshot()["events"], correction["id"])[1]
        path = self.base_dir / anchor["evidence_ref"]
        path.chmod(0o600)
        with path.open("ab") as out:
            out.write(b" ")
        self.assertEqual("fail", unattended_checks(self.t.repo, lease)["correction-observed"])

    def test_unattended_checks_block_an_unfinished_correction(self):
        self.scenario(steps={"prd-review-1": {"findings": [{"severity": "critical"}]},
                             "prd-2": {"block": self.block_file()}})
        child = self.child_start(self.pipeline_file(self.correcting()))
        self.wait_launches(3, timeout=300)  # create, review, correction (blocked in the fake until released)
        self.assertEqual("blocked", unattended_checks(self.t.repo, [self.lease_of()])["correction-observed"])
        self.unblock()
        code, summary, _ = self.finish(child, timeout=LONG)
        self.assertEqual((0, "done", "pass"),
                         (code, summary["outcome"],
                          unattended_checks(self.t.repo, [summary["lease_id"]])["correction-observed"]), summary)

    def test_kill_inside_the_correction_prepare_resumes_with_the_same_key_and_one_launch_per_key(self):
        """Design D5: the author step's half-finished-prepare handling covers the correction. `before-bind-assignment`
        fires for the create (1), the review prepare (2) and the correction (3)."""
        self.scenario(steps={"prd-review-1": {"findings": [{"severity": "critical"}]}, "prd-review-2": {}})
        self.killed_run("before-bind-assignment:3", self.pipeline_file(self.correcting()))
        half = [row for row in self.prd_rows() if row["state"] == "prepared"]
        self.assertEqual(1, len(half), half)
        self.assertTrue(half[0]["idempotency_key"].endswith("-prd-2"), half[0])  # the correction, not the create
        self.assertEqual(1, len(self.snapshot()["reviews"]))  # the blocking review was recorded before the kill
        with self.assertRaises(DomainError):
            binding_anchor(self.snapshot()["events"], half[0]["id"])  # created, not bound
        code, summary = self.cli_resume()
        self.assertEqual((0, "done", 1, 1), (code, summary["outcome"], summary["restarts"],
                                             summary["counts"]["corrections"]), summary)
        first = self.actions(summary)[0]
        self.assertEqual(("prepare", half[0]["id"]), (first["kind"], first["assignment_id"]))  # same attempt, same key
        self.assertEqual((2, 0), (len(self.bound(PLAN)), len([row for row in self.prd_rows()
                                                               if row["state"] == "cancelled-before-launch"])))
        self.assertEqual({1}, set(Counter(item["key"] for item in self.launch_log()).values()))
        self.assertEqual({**ALL_PASS, "judgment-backed-decision": "blocked", "update-observed": "blocked",
                          "correction-observed": "pass", "no-duplicate-after-restart": "pass"},
                         unattended_checks(self.t.repo, [summary["lease_id"]]))


class ExportTargetTests(Base):
    def test_export_target_not_on_local_ext4_refused_before_claim_without_writing(self):
        """Review I1 and fix-round N1/N4: a handoff directory off local ext4 is refused at bind, before the lock and the
        claim, and nothing is written. A tmpfs mount entry is faked, so this runs on every host."""
        handoff = self.root / "tmpfs-handoff"
        handoff.mkdir()
        tmpfs = ext4_mountinfo(self.root) + f"37 36 0:33 / {handoff} rw,relatime - tmpfs tmpfs rw\n"
        value = self.pipeline(self.steps()[:-1] + [{"id": "handoff", "kind": "export", "to": str(handoff)}])
        revision = self.snapshot()["revision"]
        listing = sorted(str(path) for path in self.base_dir.rglob("*"))
        with patch("coordinator_core.store.read_mountinfo", return_value=tmpfs):
            self.code("UNSUPPORTED_FILESYSTEM", drive_pipeline.check_bindable, self.snapshot(), value)
            self.code("UNSUPPORTED_FILESYSTEM", drive.start, self.t.repo, self.fid, value, self.drive_token,
                      drive.RunBounds(60, 3600), str(self.jroot))
        self.assertEqual((revision, listing, False, "released"),
                         (self.snapshot()["revision"], sorted(str(path) for path in self.base_dir.rglob("*")),
                          self.drive_token.exists(), self.snapshot()["owner"]["state"]))

    def test_export_step_errors_need_a_human_while_ownership_or_corrupt_state_still_fail(self):
        """Review I1 and fix-round N2: an exporter error or an OSError at the export step is needs-human with its gate,
        never failed/internal-error; ownership loss and corrupt state still end the run failed."""
        failure = DomainError("UNSUPPORTED_FILESYSTEM", "Local mutation requires ext4")
        with patch.object(exporter, "_require_local_ext4", side_effect=failure):
            summary, code = self.run_in(max_turns=60)
        self.assertEqual((11, "needs-human", "stale-input", "UNSUPPORTED_FILESYSTEM", "handoff"),
                         (code, summary["outcome"], summary["reason"], summary["subjects"]["gate"],
                          summary["subjects"]["step_id"]), summary)
        with patch.object(exporter, "export_checkpoint", side_effect=OSError(28, "No space left on device")):
            summary, code = self.again(max_turns=60)
        self.assertEqual((11, "stale-input", "OS_ERROR"), (code, summary["reason"], summary["subjects"]["gate"]),
                         summary)
        with patch.object(exporter, "export_checkpoint", side_effect=DomainError("CORRUPT_STATE", "broken store")):
            summary, code = self.again(max_turns=60)
        self.assertEqual((12, "failed", "corrupt-state"), (code, summary["outcome"], summary["reason"]), summary)
        self.assertEqual([], self.checkpoints())
        summary, code = self.again(max_turns=60)
        self.assertEqual((0, "done", ["export"]), (code, summary["outcome"],
                                                   [item["kind"] for item in self.actions(summary)]), summary)

    def test_export_target_probe_covers_published_checkpoint_files_under_glob_scopes(self):
        """Review M5: `*` crosses `/`, so a top-level glob scope reaches the checkpoint files published below `to`."""
        handoff = self.t.path / "handoff"
        handoff.mkdir()
        steps = self.steps()[:-1] + [{"id": "handoff", "kind": "export", "to": str(handoff)}]
        self.assertFalse(drive_pipeline.check_bindable(self.snapshot(), self.pipeline(steps)))
        attended = self.coordinator.claim("attended", self.root / "attended.token")
        self.coordinator.create_feature(attended, FeatureInput(
            "notes", [], [], ["review"], ["worker only"], "globbed", self.base, self.base, str(self.t.path), [],
            ["*.md"]))
        self.coordinator.release(attended)
        self.code("INVALID_INPUT", drive_pipeline.check_bindable, self.snapshot(), self.pipeline(steps))


class KillTests(Base):
    def test_kill_after_bundle_commit_resume_does_not_duplicate(self):
        self.killed_run("after-action@action=bundle")
        bundle = self.one("bundles")
        self.assertEqual([], [row for row in self.hook_log() if row["point"] == "after-turn-event" and
                              row["turn"] > 2])
        code, summary = self.cli_resume()
        self.assert_resumed_unattended(code, summary)
        self.assertEqual(bundle, self.one("bundles"))
        self.assertEqual(["review-prepare", "launch", "review-record", "accept", "export"],
                         [item["kind"] for item in self.actions(summary)])

    def test_kill_after_review_record_commit_resume_does_not_duplicate(self):
        self.killed_run("after-action@action=review-record")
        review = self.one("reviews")
        code, summary = self.cli_resume()
        self.assert_resumed_unattended(code, summary)
        self.assertEqual((review, 1, 1), (self.one("reviews"), len(self.reviewer_launches()), len(self.bound(REVIEW))))
        self.assertEqual(["accept", "export"], [item["kind"] for item in self.actions(summary)])

    def test_kill_after_accept_commit_resume_does_not_duplicate(self):
        self.killed_run("after-action@action=accept")
        acceptance = self.one("acceptances")
        code, summary = self.cli_resume()
        self.assert_resumed_unattended(code, summary)
        self.assertEqual((acceptance, {acceptance["id"]: "current"}), (self.one("acceptances"), self.verified()))
        self.assertEqual(["export"], [item["kind"] for item in self.actions(summary)])

    def test_kill_during_export_resume_publishes_new_revision_without_overwrite(self):
        self.killed_run("before-export-commit")
        (orphan,) = self.checkpoints()
        kept = self.tree_bytes(orphan)
        self.assertEqual((None, None), (self.snapshot()["export_revision"], self.snapshot()["export_dir_name"]))
        code, summary = self.cli_resume()
        self.assert_resumed_unattended(code, summary)
        older, newer = self.checkpoints()
        self.assertEqual((orphan, kept), (older, self.tree_bytes(older)))
        self.assertGreater(int(newer.name.split("-")[1][1:]), int(orphan.name.split("-")[1][1:]))
        self.assertEqual((newer.name, ["export"]), (self.snapshot()["export_dir_name"],
                                                    [item["kind"] for item in self.actions(summary)]))

    def test_kill_during_reviewer_attempt_needs_human_with_holder_and_no_relaunch(self):
        """Design section 4 `worker or reviewer launch` row, reviewer role: unknown, fence held, never relaunched."""
        self.scenario(steps={"prd-review": {"block": self.block_file()}})
        child = self.child_start()
        launched = self.wait_for(self.reviewer_launches, timeout=300, message="reviewer launch")[0]
        self.addCleanup(kill_group, launched["pid"])
        os.kill(child.pid, signal.SIGKILL)
        self.assertEqual(-signal.SIGKILL, self.finish(child, timeout=LONG)[0])
        kill_group(launched["pid"])
        code, summary = self.cli_resume()
        self.assertEqual((11, "attempt-unknown", launched["assignment_id"]),
                         (code, summary["reason"], summary["subjects"]["holder"]), summary)
        self.assertEqual((1, [], []), (len(self.reviewer_launches()), self.snapshot()["reviews"],
                                       self.snapshot()["acceptances"]))

    def test_kill_before_review_bind_finishes_half_prepared_review_without_duplicate(self):
        """Design section 4 `prepare` row for the review step: the same key finishes the identical review spec."""
        self.killed_run("before-bind-assignment:2")  # the first bind is the PRD create's, the second the review's
        (half,) = [row for row in self.snapshot()["assignments"] if "-prd-review-" in row["idempotency_key"]]
        self.assertEqual([], self.bound(REVIEW))
        code, summary = self.cli_resume()
        self.assert_resumed_unattended(code, summary)
        (reviewer, _), = self.bound(REVIEW)
        self.assertEqual((half["id"], half["idempotency_key"], 1),
                         (reviewer["id"], reviewer["idempotency_key"], len(self.reviewer_launches())))
        self.assertEqual("review-prepare", self.actions(summary)[0]["kind"])
        self.assertNotIn("cancelled-before-launch", [row["state"] for row in self.snapshot()["assignments"]])

    def test_kill_between_judgment_launch_record_and_spawn_is_unknown_and_never_launched(self):
        """Task 2 re-review deferred gap: the launch record is sealed, the isolated run never spawned."""
        self.ask()
        self.killed_at("before-isolated-run")
        key = self.key()
        self.assertEqual(([], False), (self.judged(), (self.scratch(key) / "receipts").exists()))
        self.assertEqual(1, len(list(self.jdir(key).glob("launching-*.json"))))
        code, summary, _ = self.drive_resume()
        self.assertEqual((11, "judgment-unknown"), (code, summary["reason"]), summary)
        self.assertEqual([("launch-unverified", key)],
                         [(row["source_id"], row["subject_id"]) for row in self.jevents("judgment.unknown")])
        self.assertEqual([], self.judged())


LOOP_COMMAND = r'''
fake() {
  echo "$1" >> calls.log
  if [ "$1" = status ]; then cat status.json; return 0; fi
  code=$(head -n 1 codes)
  tail -n +2 codes > codes.next && mv codes.next codes
  if [ "$code" = kill ]; then sh -c 'kill -9 $$'; return $?; fi
  return "$code"
}
'''


class LoopTests(Base):
    def loop(self, codes, status=None, restarts=2, call="drive_loop --max-restarts {n} fake"):
        work = self.root / "loop"
        work.mkdir(exist_ok=True)
        for name in ("calls.log", "codes", "status.json"):
            (work / name).unlink(missing_ok=True)
        (work / "codes").write_text("".join(f"{code}\n" for code in codes))
        (work / "status.json").write_text(json.dumps(status if status is not None else {"leases": []}))
        script = work / "loop.sh"
        script.write_text(self.snippet() + LOOP_COMMAND + call.format(n=restarts) + "\n")
        result = subprocess.run(["sh", str(script)], capture_output=True, text=True, cwd=work, timeout=60)
        calls = (work / "calls.log").read_text().split() if (work / "calls.log").exists() else []
        return result.returncode, calls, result.stderr

    def test_documented_drive_loop_bounded_by_max_restarts_and_stops_on_terminal_or_ended_runs(self):
        ended = {"leases": [{"last_run": {"ended": True}}]}
        open_run = {"leases": [{"last_run": {"ended": False}}]}
        self.assertEqual((10, ["start", "resume", "resume"]), self.loop([10, 10, 10, 10])[:2])
        self.assertIn("stopping after 2 restarts", self.loop([10, 10, 10, 10])[2])
        self.assertEqual((0, ["start", "resume"]), self.loop([10, 0], restarts=3)[:2])
        for code in (0, 2, 3, 4, 11, 12):
            self.assertEqual((code, ["start"]), self.loop([code])[:2])
        self.assertEqual((137, ["start", "status"]), self.loop(["kill"], ended)[:2])
        self.assertEqual((137, ["start", "status"]), self.loop(["kill"], {"leases": []})[:2])
        self.assertEqual((0, ["start", "status", "resume"]), self.loop(["kill", 0], open_run)[:2])
        self.assertEqual((137, ["start", "status", "resume", "status"]),
                         self.loop(["kill", "kill", "kill"], open_run, restarts=1)[:2])
        self.assertEqual((10, ["start"]), self.loop([10], restarts=0)[:2])
        for call in ("drive_loop fake", "drive_loop --max-restarts x fake", "drive_loop --max-restarts 2"):
            self.assertEqual((2, []), self.loop([0], call=call)[:2])


class ProvedTests(Base):
    """The only "proved unattended" evidence of this slice: fake binaries behind the production registry entries,
    a real child driver, the operator absent. It proves coordinator logic on this host only."""

    def setUp(self):
        super().setUp()
        self.ask()  # the PRD create returns partial with one question that the delegated clause A1 covers
        self.assertIs(clients.CODEX, clients.REGISTRY[ROLES["worker"]["client"]])  # production entry, no injection

    def attended_prework(self):
        snapshot = self.snapshot()
        feature = next(row for row in snapshot["features"] if row["id"] == self.fid)
        self.assertEqual((2, judged.AUTHORITY, "released"), (feature["mandate_revision"],
                                                              json.loads(feature["authority_json"]),
                                                              snapshot["owner"]["state"]))
        self.assertIn(self.compat, [row["id"] for row in snapshot["decisions"] if row["mandate_revision"] == 2])

    def assert_unattended(self, summary, restarts):
        self.assertEqual(("done", 0, "released", restarts, []),
                         (summary["outcome"], drive.EXIT[summary["outcome"]], summary["ownership"],
                          summary["restarts"], summary["interventions"]), summary)
        expected = {**ALL_PASS, "no-duplicate-after-restart": "pass" if restarts else "trivial"}  # review M3
        self.assertEqual(expected, unattended_checks(self.t.repo, [summary["lease_id"]]))
        snapshot = self.snapshot()
        self.assertEqual(("released", {}), (snapshot["owner"]["state"], self.listing()))
        decisions = [item for item in summary["decisions"] if item["source_kind"] == "coordinator"]
        self.assertEqual(["A1"], [item["authority_id"] for item in decisions])
        (row,) = [row for row in snapshot["decisions"] if row["request_id"]]
        choice = json.loads(row["choice_json"])
        self.assertEqual(("coordinator", [{"authority_id": "A1", "field": "authority", "pointer": "/delegated/0/text"}],
                          judged.QUOTE, "model-claim"),
                         (row["source_kind"], choice["clauses"], choice["answers"][0]["clause"]["quote"],
                          choice["judgment"]["quality"]))
        self.assertGreaterEqual(len(choice["answers"][0]["clause"]["quote"]), judgment.MIN_QUOTE)
        self.assertNotIn("clause", choice)
        self.assertEqual({1}, set(Counter(item["key"] for item in self.launch_log()).values()))
        self.assertEqual(1, len(self.judged()))
        self.assertEqual((2, ["current", "current"]), (len(snapshot["acceptances"]), sorted(self.verified().values())))
        self.assertEqual((3, 2, 1, 2, 2), (len(self.bound(PLAN)), len(self.bound(REVIEW)), len(self.answered()),
                                           len(snapshot["reviews"]), len(snapshot["bundles"])))
        self.assertEqual(1, len(self.checkpoints()))

    def test_proved_unattended_run_operator_absent(self):
        self.attended_prework()
        child = self.child_start(self.pipeline_file(self.full()))
        code, summary, _ = self.finish(child, timeout=LONG)
        self.assertEqual(0, code, summary)
        self.assert_unattended(summary, restarts=0)
        self.assertEqual(PROVED, [item["kind"] for item in self.actions(summary)])
        self.assertEqual({"worker": 3, "reviewer": 2, "judgment": 1}, summary["counts"]["launches"])

    def test_proved_unattended_kill_and_restart_continuation(self):
        self.attended_prework()
        pipeline = self.pipeline_file(self.full())
        harness = f'"{sys.executable}" "{self.harness}" --repo "{self.t.path}" drive'
        command = (f"cmd() {{\n  case $1 in\n"
                   f"    start) DRIVE_KILL_AT=after-turn-event:5 {harness} start --feature {self.fid} "
                   f'--pipeline "{pipeline}" --token-file "{self.drive_token}" --judgment-root "{self.jroot}" '
                   f"--max-turns 60 ;;\n"
                   f'    resume) {harness} resume --token-file "{self.drive_token}" ;;\n'
                   f'    status) "{sys.executable}" -m coordinator_core --repo "{self.t.path}" drive status '
                   f"--feature {self.fid} ;;\n  esac\n}}\n")
        script = self.root / "drive-loop.sh"
        script.write_text(self.snippet() + command + "drive_loop --max-restarts 3 cmd\n")
        env = self.child_env(DRIVE_HOOK_LOG=str(self.hooks), DRIVE_PAUSE_RELEASE=str(self.pause_release))
        result = subprocess.run(["sh", str(script)], capture_output=True, text=True, cwd=COORDINATOR, env=env,
                                timeout=LONG, stdin=subprocess.DEVNULL)
        for text in ("Warning", "Exception ignored", "Traceback"):
            self.assertNotIn(text, result.stderr)
        self.assertEqual((0, ["drive_loop: restart 1 of 3 after exit 137"]),
                         (result.returncode, [line for line in result.stderr.splitlines()
                                              if line.startswith("drive_loop:")]), result.stderr)
        summary = json.loads(result.stdout.strip().splitlines()[-1])
        first = [row for row in self.hook_log() if row["pid"] == self.hook_log()[0]["pid"]]
        actions = {row["turn"]: row["action"] for row in first if row["point"] == "after-action"}
        self.assertEqual(("decide", "prepare"), (actions[4], actions[5]))  # killed on the first turn after the decision
        self.assertEqual(("after-turn-event", 5), (first[-1]["point"], first[-1]["turn"]))
        self.assert_unattended(summary, restarts=1)
        self.assertEqual(PROVED[5:], [item["kind"] for item in self.actions(summary)])


class MultiQuestionTests(Base):
    """Judgment-v2 design §5: a three-question request answered under two clauses reaches `done` unattended; one
    uncovered question refuses the whole request and the refusal text reaches `drive status`."""

    def test_three_questions_answered_under_two_clauses_reach_done_unattended(self):
        self.ask(judged.THREE_ANSWER, steps=judged.THREE)
        self.assertIs(clients.CODEX, clients.REGISTRY[ROLES["judgment"]["client"]])  # production entry
        child = self.child_start(self.pipeline_file(self.full()))
        code, summary, _ = self.finish(child, timeout=LONG)
        self.assertEqual((0, "done", "released", [], {"worker": 3, "reviewer": 2, "judgment": 1}),
                         (code, summary["outcome"], summary["ownership"], summary["interventions"],
                          summary["counts"]["launches"]), summary)
        self.assertEqual(PROVED, [item["kind"] for item in self.actions(summary)])
        self.assertEqual({**ALL_PASS, "no-duplicate-after-restart": "trivial"},
                         unattended_checks(self.t.repo, [summary["lease_id"]]))
        self.assertEqual(["A1+A2"], [item["authority_id"] for item in summary["decisions"]
                                     if item["source_kind"] == "coordinator"])
        (row,) = [row for row in self.snapshot()["decisions"] if row["request_id"]]
        choice = json.loads(row["choice_json"])
        self.assertTrue(row["source_ref"].startswith("mandate:r2:A1+A2:authority;receipt="), row["source_ref"])
        self.assertEqual(([1, 2, 3], ["A1", "A1", "A2"], ["A1", "A2"]),
                         ([item["index"] for item in choice["answers"]],
                          [item["clause"]["authority_id"] for item in choice["answers"]],
                          [item["authority_id"] for item in choice["clauses"]]))
        request = self.coordinator.detail("decision-request", row["request_id"])
        self.assertEqual(("resolved", row["id"]), (request["state"], request["resolution_decision_id"]))
        update = self.workers()[1]["assignment_id"]
        brief = (self.t.path / self.coordinator.detail("assignment", update)["brief_ref"]).read_text()
        self.assertIn(f"from coordinator `{row['source_ref']}`", brief)

    def test_uncovered_question_refuses_the_whole_request_and_names_the_indexes(self):
        refusal = "covered: 1 (A1), 2 (A1); uncovered: 3 (no delegated clause names the budget)"
        self.ask({**judged.REFUSE, "refusal": refusal}, steps=judged.THREE)
        child = self.child_start(self.pipeline_file(self.full()))
        code, summary, _ = self.finish(child, timeout=LONG)
        self.assertEqual((11, "question-refused", refusal, [], 1),
                         (code, summary["reason"], summary["subjects"]["refusal"], summary["subjects"]["defects"],
                          summary["counts"]["launches"]["judgment"]), summary)
        self.assertEqual(([], 1, 1), (self.answered(), len(self.judged()), len(self.prd_rows())))
        status = self.run_cli("drive", "status", "--feature", self.fid, code=0)[1]
        (step,) = [item for feature in status["features"] for item in feature["steps"] if item["id"] == "prd"]
        self.assertEqual(("needs-human", "question-refused", refusal),
                         (step["state"], step["reason"], step["subjects"]["refusal"]))
        self.assertEqual([self.open_request()], status["open_requests"])
        self.assertIn(f"inbox add decision for request {self.open_request()}", summary["operator_action"])

    def test_partial_answer_set_is_refused_not_recorded(self):
        two = judged.answer(answers=[{"index": 1, "text": "the existing provider"}, {"index": 2, "text": "EU west"}])
        self.ask(two, steps=judged.THREE)
        child = self.child_start(self.pipeline_file(self.full()))
        code, summary, _ = self.finish(child, timeout=LONG)
        self.assertEqual((11, "question-refused", ["missing-index"], ""),
                         (code, summary["reason"], summary["subjects"]["defects"], summary["subjects"]["refusal"]),
                         summary)
        self.assertEqual([], self.answered())


class EvidenceTests(Base):
    def test_unattended_checks_scores_missing_evidence_blocked_and_contradiction_fail(self):
        self.assertEqual(dict.fromkeys(CRITERIA, "blocked"), unattended_checks(self.t.repo, []))
        summary, code = self.run_in(max_turns=60)
        self.assertEqual((0, "done"), (code, summary["outcome"]), summary)
        lease = [summary["lease_id"]]
        expected = {**SHORT, "no-duplicate-after-restart": "trivial"}  # one run: nothing was restarted
        self.assertEqual(expected, unattended_checks(self.t.repo, lease))
        self.assertEqual("blocked", unattended_checks(self.t.repo, ["no-such-lease"])["driver-claimed-generation"])
        self.inbox_add("stop", {"feature_id": self.fid, "reason": "operator present", "source_ref": "operator:x"})
        expected["zero-interventions-inbox-empty"] = "fail"
        self.assertEqual(expected, unattended_checks(self.t.repo, lease))
        turn = self.base_dir / "drives" / summary["lease_id"] / summary["run_id"] / "turn-1.json"
        turn.chmod(0o600)
        with turn.open("ab") as out:
            out.write(b" ")
        expected["drive-events-anchored"] = "fail"
        self.assertEqual(expected, unattended_checks(self.t.repo, lease))
        # Review M2: a second review assignment of the same bundle is a duplicate ...
        attended = self.coordinator.claim("attended", self.root / "attended.token")
        bundle = self.one("bundles")["id"]
        bmad_review.review_prepare(self.t.repo, self.store, attended, bmad_review.ReviewPrepare(
            bundle, "reviewer-2", "attended-review-2", self.compat, client="codex-cli"))
        expected["no-duplicate-after-restart"] = "fail"
        self.assertEqual(expected, unattended_checks(self.t.repo, lease))
        # ... and a second launch reservation of one assignment (seeded by construction: the core never allows it).
        (author, _), = self.bound(PLAN)
        with sqlite3.connect(self.base_dir / "state.sqlite3") as conn:
            revision = conn.execute("SELECT revision FROM meta").fetchone()[0] + 1
            conn.execute("UPDATE meta SET revision=?", (revision,))
            conn.execute("INSERT INTO events(id, revision, kind, subject_id) VALUES (?, ?, ?, ?)",
                         (str(uuid.uuid4()), revision, "assignment.launch-reserved", author["id"]))
        conn.close()
        # The forged reservation also breaks the worker's historical-return proof and, through it, the acceptance gates.
        expected.update(dict.fromkeys(("one-launch-per-key", "worker-observed-complete", "acceptance-current"), "fail"))
        self.assertEqual(expected, unattended_checks(self.t.repo, lease))

    def test_release_failure_after_anchored_summary_prints_retained_and_resume_releases(self):
        """Task 1 deferred M5: the sealed summary records the release decided under the lock; owner.released is the
        fact, and a failed release is reported as retained instead of contradicting the state."""
        failure = DomainError("CORRUPT_STATE", "store went away")
        with patch.object(service.Coordinator, "release", side_effect=failure):
            summary, code = self.run_in(max_turns=60)
        sealed = json.loads(self.summary_file(summary).read_bytes())
        self.assertEqual((0, "done", "retained", "CORRUPT_STATE", "released"),
                         (code, summary["outcome"], summary["ownership"], summary["end_error"]["code"],
                          sealed["ownership"]))
        self.assertEqual("active", self.snapshot()["owner"]["state"])
        summary, code = self.again(max_turns=60)
        self.assertEqual((0, "done", "released", 0), (code, summary["outcome"], summary["ownership"],
                                                      summary["counts"]["turns"]), summary)
        self.assertEqual("released", self.snapshot()["owner"]["state"])

    def test_drive_status_after_done_shows_released_owner_and_zero_interventions(self):
        code, summary, _ = self.go("--max-turns", "60")
        self.assertEqual((0, "done"), (code, summary["outcome"]), summary)
        status = self.run_cli("drive", "status", "--feature", self.fid, code=0)[1]
        (lease,) = status["leases"]
        self.assertEqual(("released", False, []), (status["owner"]["state"], status["lock"]["held"],
                                                    status["inbox"]))
        self.assertEqual((summary["lease_id"], True, "drive.ended", "done", None, 0),
                         (lease["lease_id"], lease["last_run"]["ended"], lease["last_run"]["end_event"],
                          lease["last_run"]["outcome"], lease["last_run"]["reason"],
                          lease["last_run"]["interventions"]))
        self.assertEqual(["satisfied"] * 6, [step["state"] for step in status["features"][0]["steps"]])
        self.assertEqual(([], [], 0), (status["open_requests"], [item for item in status["windows"] if item["open"]],
                                       len(summary["interventions"])))
