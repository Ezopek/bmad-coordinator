"""Bundle wrapper, bound review and acceptance. A fake `codex` proves coordinator logic only; none of this is
evidence about live BMAD behavior."""

import json
import os
import shutil
import unittest
from pathlib import Path

from coordinator_core import bmad_planning as planning
from coordinator_core import bmad_review as review
from coordinator_core.bmad_review import finding_counts
from coordinator_core.exporter import export_checkpoint, import_checkpoint
from coordinator_core.model import BundleInput, DecisionInput, DomainError, ReviewEvidence
from coordinator_core.repo import resolve_repo
from coordinator_core.run_receipt import canonical, read_binding, run_name, run_root, seal_binding
from coordinator_core.service import Coordinator, binding_anchor
from coordinator_core.store import open_store
from tests.bmad_fixture import write
from tests.test_bmad_core import FAKE, Observed, digest, git, writes
from tests.test_bmad_planning import ARCH, MEMLOG, PRD, PRD_FILE, WS, PlanningCase, flow, paths, question_text

THREAD_FAKE = FAKE.replace("'thread_id': 'thread-1'", "'thread_id': os.environ.get('FAKE_THREAD', 'thread-1')")
LENSES = ["adversarial", "edge-case-hunter"]


def binding_for(**over):
    value = {"bundle_id": "b", "manifest_digest": "m", "lenses": list(LENSES),
             "sealed_files": [{"kind": "artifact", "path": "a.md", "digest": "d" * 64, "sealed_path": "/x/a.md"}],
             "carried": {"assumptions": [{"contributor_id": "c1", "kind": "assumption", "index": 0, "text": "x"}],
                         "critical_flags": [{"contributor_id": "c1", "kind": "partial", "index": 0, "text": "p"}]}}
    value.update(over)
    return value


def good_final(**over):
    final = {"result": "completed", "bundle_id": "b", "manifest_digest": "m", "lenses_run": list(LENSES),
             "bundle_read": [{"path": "a.md", "sha256": "d" * 64}], "findings": [],
             "dispositions": [{"contributor_id": "c1", "kind": "assumption", "index": 0, "severity": "low"},
                              {"contributor_id": "c1", "kind": "partial", "index": 0, "severity": "medium"}]}
    final.update(over)
    return final


def finding(severity="low", **over):
    value = {"lens": "adversarial", "location": "prd.md:1", "trigger_condition": "t", "guard_snippet": "g",
             "potential_consequence": "c", "severity": severity}
    value.update(over)
    return value


class CheckTests(unittest.TestCase):
    """Pure profile-check logic; no worker process."""

    def run_check(self, final=None, binding=None, changed=()):
        return review.check(final or good_final(), {}, binding or binding_for(), None, list(changed))

    def assertUnknown(self, **kwargs):
        with self.assertRaises(DomainError) as caught:
            self.run_check(**kwargs)
        self.assertEqual("UNKNOWN_EVIDENCE", caught.exception.code, caught.exception.message)

    def test_critical_open_is_derived_from_findings_and_dispositions(self):
        self.assertFalse(self.run_check()["critical_open"])
        self.assertTrue(self.run_check(good_final(findings=[finding("critical")]))["critical_open"])
        disposition = good_final()["dispositions"]
        disposition[1]["severity"] = "critical"
        self.assertTrue(self.run_check(good_final(dispositions=disposition))["critical_open"])
        self.assertTrue(review.critical_open({"findings": [], "dispositions": disposition}))

    def test_dispositions_must_cover_each_carried_item_once_and_critical_sets_critical_open(self):
        both = good_final()["dispositions"]
        self.assertUnknown(final=good_final(dispositions=both[:1]))  # the partial marker is missing
        self.assertUnknown(final=good_final(dispositions=both + both[:1]))  # duplicate
        self.assertUnknown(final=good_final(dispositions=both + [{**both[0], "index": 7}]))  # invented item
        self.assertUnknown(final=good_final(dispositions=[{**both[0], "severity": "fatal"}, both[1]]))
        self.assertUnknown(final=good_final(dispositions=[{**both[0], "index": True}, both[1]]))
        self.assertUnknown(final=good_final(dispositions=[{**both[0], "extra": 1}, both[1]]))
        self.assertEqual(2, len(self.run_check()["dispositions"]))
        self.assertTrue(self.run_check(good_final(dispositions=[both[0], {**both[1], "severity": "critical"}]))
                        ["critical_open"])

    def test_bundle_binding_lenses_and_per_file_digests_must_match(self):
        self.assertUnknown(final=good_final(bundle_id="other"))
        self.assertUnknown(final=good_final(manifest_digest="other"))
        self.assertUnknown(final=good_final(lenses_run=["adversarial"]))
        self.assertUnknown(final=good_final(lenses_run=LENSES + ["extra"]))
        self.assertUnknown(final=good_final(lenses_run=["adversarial", "adversarial"]))
        self.assertUnknown(final=good_final(bundle_read=[]))
        self.assertUnknown(final=good_final(bundle_read=[{"path": "a.md", "sha256": "e" * 64}]))
        self.assertUnknown(final=good_final(bundle_read=[{"path": "a.md", "sha256": "d" * 64},
                                                         {"path": "b.md", "sha256": "d" * 64}]))
        self.assertUnknown(final=good_final(bundle_read=[{"path": "a.md"}]))
        # F16 (live hook 19): the sealed location is an alias of the manifest path; the record keeps the manifest path
        payload = self.run_check(final=good_final(bundle_read=[{"path": "/x/a.md", "sha256": "d" * 64}]))
        self.assertEqual([{"path": "a.md", "sha256": "d" * 64}], payload["bundle_read"])
        self.assertUnknown(final=good_final(bundle_read=[{"path": "/x/a.md", "sha256": "e" * 64}]))
        self.assertUnknown(final=good_final(bundle_read=[{"path": "/y/a.md", "sha256": "d" * 64}]))
        # malformed items are an invalid return (UNKNOWN_EVIDENCE), never a TypeError out of the worker
        self.assertUnknown(final=good_final(lenses_run=["adversarial", 3]))
        self.assertUnknown(final=good_final(lenses_run=[["adversarial"], "edge-case-hunter"]))
        self.assertUnknown(final=good_final(bundle_read=[{"path": 1, "sha256": "d" * 64}]))
        self.assertUnknown(final=good_final(bundle_read=["a.md"]))

    def test_findings_are_typed_and_use_bound_lenses(self):
        self.assertUnknown(final=good_final(findings=[finding(lens="other")]))
        self.assertUnknown(final=good_final(findings=[finding("fatal")]))
        self.assertUnknown(final=good_final(findings=[finding(location=3)]))
        self.assertUnknown(final=good_final(findings=[{"lens": "adversarial"}]))
        self.assertEqual(1, len(self.run_check(good_final(findings=[finding()]))["findings"]))

    def test_changed_files_or_non_completed_result_are_never_a_valid_review(self):
        self.assertUnknown(changed=["prd.md"])
        for result in ("blocked", "needs-decision"):
            payload = self.run_check(good_final(result=result, bundle_id="garbage", dispositions=[]))
            self.assertFalse(payload["valid"])


class ReviewCase(PlanningCase):
    def setUp(self):
        super().setUp()
        (self.fake_dir / "codex").write_text(THREAD_FAKE)
        self.author_thread = "codex-cli:thread-1"

    def authored(self, key="p1", assumptions=("EU only",)):
        assignment = self.prep(key)["assignment_id"]
        report = self.execute(assignment, flow(assumptions=list(assumptions)), self.first_files())
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        return assignment

    def bundled(self, **kwargs):
        return review.bundle_for_workspace(self.t.repo, self.store, self.owner,
                                           review.BundleRequest(self.fid, WS, **kwargs))

    def prepared(self, bundle, key="r1", reviewer="independent-reviewer", **over):
        data = dict(bundle_id=bundle, reviewer_id=reviewer, key=key, compat_decision_id=self.compat)
        data.update(over)
        return review.review_prepare(self.t.repo, self.store, self.owner, review.ReviewPrepare(**data))

    def binding_of_review(self, assignment):
        _, bound = binding_anchor(self.store.read_snapshot()["events"], assignment)
        return read_binding(self.base_dir, bound["evidence_ref"], bound["payload_digest"])

    def final_for(self, assignment, **over):
        binding = self.binding_of_review(assignment)
        carried = binding["carried"]["assumptions"] + binding["carried"]["critical_flags"]
        final = {"result": "completed", "bundle_id": binding["bundle_id"], "manifest_digest": binding["manifest_digest"],
                 "lenses_run": list(binding["lenses"]),
                 "bundle_read": [{"path": item["path"], "sha256": item["digest"]} for item in binding["sealed_files"]],
                 "findings": [], "dispositions": [{"contributor_id": item["contributor_id"], "kind": item["kind"],
                                                   "index": item["index"], "severity": "low"} for item in carried]}
        final.update(over)
        return final

    def run_review(self, assignment, thread="thread-review", final=None, **env):
        return self.run_it(assignment, FAKE_THREAD=thread, FAKE_EXTRA=json.dumps(final or self.final_for(assignment)),
                           **env)

    def record(self, assignment):
        return review.record_bound_review(self.t.repo, self.store, self.owner, assignment)

    def reviewed(self, bundle, key="r1", thread="thread-review", **over):
        prepared = self.prepared(bundle, key, **over)
        report = self.run_review(prepared["assignment_id"], thread)
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        return prepared["assignment_id"], self.record(prepared["assignment_id"])["id"]

    def reviews(self):
        return self.store.read_snapshot()["reviews"]

    def question_update(self, partial_thread="thread-1"):
        self.env["FAKE_THREAD"] = partial_thread
        first, request = self.question_run()
        del self.env["FAKE_THREAD"]
        self.decide_request(request)
        update = self.prep("u1", **self.update_args(first, request))["assignment_id"]
        report = self.execute(update, flow(intent="update", assumptions=["EU only"]),
                              [(PRD_FILE, "# PRD v2\n"), (MEMLOG, "log\nchange\n")])
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        return first, update

    def arch_spine(self):
        workspace, _, memlog = paths("gitops", "architecture")
        return workspace, f"{workspace}/ARCHITECTURE-SPINE.md", memlog

    def accepted_prd(self):
        self.authored()
        bundle = self.bundled()["id"]
        _, review_id = self.reviewed(bundle)
        return bundle, review.accept_bound(self.store, self.owner, bundle, review_id)["id"]

    def architecture(self, acceptance, key="a1", **over):
        data = dict(skill=ARCH, altitude="feature", purpose="build-substrate", inputs=[], input_roles={},
                    prd_acceptance=acceptance)
        data.update(over)
        assignment = self.prep(key, **data)["assignment_id"]
        workspace, spine, memlog = self.arch_spine()
        workflow = {"status": "complete", "intent": "create", "spine": spine, "memlog": memlog,
                    "open_questions": [], "assumptions": [], "conflicts_with_prior_decisions": []}
        report = self.execute(assignment, workflow, [(spine, "# Spine\n"), (memlog, "log\n")], skill=ARCH)
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        return assignment, workspace


class BundleTests(ReviewCase):
    def test_bundle_enumerates_observed_workspace_files_and_names_all_contributors(self):
        first, update = self.question_update()
        bundle = self.bundled()
        self.assertEqual(sorted([PRD_FILE, MEMLOG, f"{WS}/notes.md"]), bundle["artifact_paths"])
        self.assertEqual(sorted([first, update]), bundle["source_assignment_ids"])
        detail = self.coordinator.detail("bundle", bundle["id"])
        self.assertEqual(bundle["source_assignment_ids"], sorted(detail["source_assignments"]))
        self.assertEqual(bundle, self.bundled())  # idempotent
        # a file no observed attempt claimed and no contributor was given is not part of the work
        write(self.t.path, f"{WS}/unclaimed.md", "never returned by an observed attempt\n")
        self.assertNotIn(f"{WS}/unclaimed.md", self.bundled()["artifact_paths"])
        self.code("BLOCKED_EVIDENCE", review.bundle_for_workspace, self.t.repo, self.store, self.owner,
                  review.BundleRequest(self.fid, "nowhere"))
        self.code("INVALID_INPUT", self.bundled, dependency_paths=[f"{WS}/notes.md"])
        # a claimed file is never silently dropped: edited or missing, it refuses the bundle by name
        notes = self.t.path / WS / "notes.md"
        notes.write_text("edited by hand after the observed return\n")
        self.assertIn(f"{WS}/notes.md", self.code("BLOCKED_EVIDENCE", self.bundled).message)
        notes.unlink()
        self.assertIn(f"{WS}/notes.md", self.code("BLOCKED_EVIDENCE", self.bundled).message)

    def test_bundle_refuses_hand_edited_claim_or_unclaimed_workspace_source(self):
        author = self.authored()
        live = self.t.path / PRD_FILE
        original = live.read_bytes()
        live.write_text("# PRD edited by hand\n")
        self.assertIn(PRD_FILE, self.code("BLOCKED_EVIDENCE", self.bundled).message)
        self.code("BLOCKED_EVIDENCE", self.coordinator.capture_bundle, self.owner, BundleInput(
            self.fid, [PRD_FILE, MEMLOG], [], [author], self.contracts(author)))
        live.write_bytes(original)
        write(self.t.path, f"{WS}/by-hand.md", "added by hand, never claimed\n")  # becomes a correction source
        update = self.prep("u1", intent="update", update_of=author, change_signal="tighten wording", slug=None,
                           inputs=[], input_roles={})["assignment_id"]
        report = self.execute(update, flow(intent="update"), [(PRD_FILE, "# PRD v2\n"), (MEMLOG, "log\nchange\n")])
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        self.assertIn(f"{WS}/by-hand.md", self.code("BLOCKED_EVIDENCE", self.bundled).message)
        self.assertEqual([], self.store.read_snapshot()["bundles"])

    def test_required_dependency_bundled_as_artifact_keeps_its_frozen_bytes(self):
        author = self.authored()
        arch, _ = self.architecture(None, inputs=[PRD_FILE], input_roles={PRD_FILE: "authority"})  # PRD@X frozen
        _, spine, arch_memlog = self.arch_spine()
        parent = review.BundleRequest(self.fid, WS.rsplit("/", 1)[0])  # holds both workspaces
        result = review.bundle_for_workspace(self.t.repo, self.store, self.owner, parent)
        self.assertEqual(sorted([PRD_FILE, MEMLOG, spine, arch_memlog]), result["artifact_paths"])
        self.assertEqual([], result["dependency_paths"])
        _, review_id = self.reviewed(result["id"])
        acceptance = review.accept_bound(self.store, self.owner, result["id"], review_id)["id"]
        update = self.prep("u1", intent="update", update_of=author, change_signal="PRD revised", slug=None,
                           inputs=[], input_roles={})["assignment_id"]
        report = self.execute(update, flow(intent="update"), [(PRD_FILE, "# PRD v2\n"), (MEMLOG, "log\nchange\n")])
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        checked = {item["acceptance_id"]: item["current"] for item in review.verify_bmad(self.store)["checked"]}
        self.assertEqual("stale", checked[acceptance])
        differs = f"project dependency differs from the bytes frozen for the contributor: {PRD_FILE}"
        self.assertIn(differs, self.code("BLOCKED_EVIDENCE", review.bundle_for_workspace, self.t.repo, self.store,
                                         self.owner, parent).message)
        self.assertIn(differs, self.code("BLOCKED_EVIDENCE", self.coordinator.capture_bundle, self.owner, BundleInput(
            self.fid, [PRD_FILE, MEMLOG, spine, arch_memlog], [], [author, update, arch], self.contracts(arch))).message)
        # the accept/verify branch compares an artifact's manifest digest with the frozen bytes too
        prd_only = self.bundled()["id"]  # PRD@Z alone is fine for its own contributors
        prd_only = next(row for row in self.store.read_snapshot()["bundles"] if row["id"] == prd_only)
        row = next(row for row in self.store.read_snapshot()["assignments"] if row["id"] == arch)
        ctx = {"stage": "accept", "bundle": prd_only, "assignment": row, "binding": self.binding_of(arch),
               "store": self.store}
        self.assertEqual([differs], planning._dependency_reasons(ctx))

    def test_dependency_refrozen_by_a_later_update_in_the_chain_supersedes_earlier_bytes(self):
        author = self.prep("p1", inputs=["docs/input.md", "docs/authority.md"],
                           input_roles={"docs/input.md": "driving-brief", "docs/authority.md": "authority"})
        author = author["assignment_id"]
        self.assertEqual("completed-claim", self.execute(author, flow(), self.first_files()).state)
        write(self.t.path, "docs/authority.md", "Authority input, revised.\n")
        differs = "differs from the bytes frozen for the contributor: docs/authority.md"
        self.assertIn(differs, self.code("BLOCKED_EVIDENCE", self.bundled).message)
        plain = self.prep("u1", intent="update", update_of=author, change_signal="reword", slug=None, inputs=[],
                          input_roles={})["assignment_id"]
        report = self.execute(plain, flow(intent="update"), [(PRD_FILE, "# PRD v2\n"), (MEMLOG, "log\nchange\n")])
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        self.assertIn(differs, self.code("BLOCKED_EVIDENCE", self.bundled).message)  # nothing re-froze it
        refrozen = self.prep("u2", intent="update", update_of=plain, change_signal="authority revised", slug=None,
                             inputs=["docs/authority.md"], input_roles={"docs/authority.md": "authority"})
        refrozen = refrozen["assignment_id"]
        report = self.execute(refrozen, flow(intent="update"), [(MEMLOG, "log\nchange\nagain\n")])
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        result = self.bundled()
        self.assertEqual((["docs/authority.md"], sorted([author, plain, refrozen])),
                         (result["dependency_paths"], result["source_assignment_ids"]))
        dependency = next(item for item in self.coordinator.detail("bundle", result["id"])["files"]
                          if item["kind"] == "dependency")
        self.assertEqual(digest(b"Authority input, revised.\n"), dependency["digest"])

    def test_plain_bundle_capture_refuses_customization_mismatch_drift_stale_or_mixed_contributors(self):
        author = self.authored()

        def capture(*ids, files=(PRD_FILE, MEMLOG)):
            return self.coordinator.capture_bundle(self.owner, BundleInput(
                self.fid, list(files), [], list(ids), self.contracts(author)))

        live, original = self.t.path / PRD_FILE, (self.t.path / PRD_FILE).read_bytes()
        live.write_text("# PRD edited after the observed return\n")  # stale bytes: no observed claim covers them
        self.code("BLOCKED_EVIDENCE", capture, author)
        live.write_bytes(original)
        terminal = run_root(self.t.repo) / run_name(author, self.detail(author)["request_fingerprint"]) / "return.json"
        sealed = terminal.read_bytes()
        value = json.loads(sealed)
        value["checks"] = ["edited after the run"]
        terminal.write_bytes(canonical(value))  # stale proof: the contributor receipt no longer validates
        self.assertIn("historical validation", self.code("BLOCKED_EVIDENCE", capture, author).message)
        terminal.write_bytes(sealed)
        write(self.t.path, "_bmad/custom/bmad-prd.toml", "")  # contract drift since binding
        self.code("STALE_INPUT", capture, author)
        (self.t.path / "_bmad/custom/bmad-prd.toml").unlink()
        mismatch = self.prep("p2", slug="other")["assignment_id"]
        report = self.execute(mismatch, flow(slug="other"), self.first_files("other"),
                              custom=[{"skill": "bmad-prd", "sha256": "e" * 64}])
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        error = self.code("BLOCKED_EVIDENCE", capture, mismatch, files=paths("other")[1:])
        self.assertIn("customization", error.message)
        extra, contracts = f"{WS}/extra.md", self.contracts(author)
        generic = self.coordinator.create_assignment(self.owner, self.spec(
            self.t, feature_id=self.fid, idempotency_key="generic", writable_paths=[extra],
            author_id="generic-worker", contract_files=contracts, contract_digest=digest(canonical(contracts))))
        report = self.run_it(generic, FAKE_WRITE=writes((extra, "generic\n")))
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        error = self.code("BLOCKED_EVIDENCE", capture, author, generic, files=(PRD_FILE, MEMLOG, extra))
        self.assertIn("mix unbound and bound", error.message)
        self.assertIn("mix unbound and bound", self.code("BLOCKED_EVIDENCE", self.bundled).message)
        self.assertEqual([], self.store.read_snapshot()["bundles"])
        # an all-generic bundle is captured as before, but it is never reviewable as bound BMAD work
        plain = capture(generic, files=(extra,))
        self.assertIn("bound bmad-planning", self.code("BLOCKED_EVIDENCE", self.prepared, plain).message)

    def test_required_dependencies_are_unioned_and_plain_capture_cannot_omit_them(self):
        accepted_bundle, acceptance = self.accepted_prd()
        assignment, workspace = self.architecture(acceptance)
        files = [self.arch_spine()[1], self.arch_spine()[2]]
        for omitted in ([], [PRD_FILE]):
            error = self.code("BLOCKED_EVIDENCE", self.coordinator.capture_bundle, self.owner, BundleInput(
                self.fid, files, omitted, [assignment], self.contracts(assignment)))
            self.assertIn("required project dependency is omitted", error.message)
        # a workspace file is an artifact, never a caller-declared dependency
        self.code("INVALID_INPUT", review.bundle_for_workspace, self.t.repo, self.store, self.owner,
                  review.BundleRequest(self.fid, workspace, [files[0]]))
        result = review.bundle_for_workspace(self.t.repo, self.store, self.owner,
                                             review.BundleRequest(self.fid, workspace, [PRD_FILE]))
        self.assertEqual(sorted([PRD_FILE, MEMLOG]), result["dependency_paths"])  # caller path unioned once
        self.assertEqual(sorted(files), result["artifact_paths"])
        manifest = self.coordinator.detail("bundle", result["id"])["files"]
        self.assertEqual({PRD_FILE, MEMLOG}, {item["path"] for item in manifest if item["kind"] == "dependency"})

    def test_authority_inputs_and_parent_spine_are_required_dependencies(self):
        write(self.t.path, "docs/parent-spine.md", "# parent\n")
        assignment = self.prep("auth", skill=ARCH, altitude="feature", purpose="build-substrate",
                               inputs=["docs/authority.md", "docs/input.md"],
                               input_roles={"docs/authority.md": "authority", "docs/input.md": "driving-brief"},
                               parent_spine="docs/parent-spine.md")["assignment_id"]
        workspace, spine, memlog = self.arch_spine()
        workflow = {"status": "complete", "intent": "create", "spine": spine, "memlog": memlog,
                    "open_questions": [], "assumptions": [], "conflicts_with_prior_decisions": []}
        self.execute(assignment, workflow, [(spine, "# Spine\n"), (memlog, "log\n")], skill=ARCH)
        self.code("BLOCKED_EVIDENCE", self.coordinator.capture_bundle, self.owner, BundleInput(
            self.fid, [spine, memlog], ["docs/authority.md"], [assignment], self.contracts(assignment)))
        result = review.bundle_for_workspace(self.t.repo, self.store, self.owner,
                                             review.BundleRequest(self.fid, workspace))
        self.assertEqual(["docs/authority.md", "docs/parent-spine.md"], result["dependency_paths"])
        # a frozen dependency edited after the run no longer matches what the contributor was given
        write(self.t.path, "docs/authority.md", "Authority input, edited.\n")
        error = self.code("BLOCKED_EVIDENCE", self.coordinator.capture_bundle, self.owner, BundleInput(
            self.fid, [spine, memlog], ["docs/authority.md", "docs/parent-spine.md"], [assignment],
            self.contracts(assignment)))
        self.assertIn("differs from the bytes frozen", error.message)

    def test_required_dependency_rules_exclude_brief_contract_and_earlier_workspace_sources(self):
        digest = "a" * 64
        sources = [{"path": path, "digest": digest} for path in ("brief.md", "contract.md", f"{WS}/prd.md", "auth.md",
                                                                  "spine.md", "driving.md", "prd-a.md")]
        row = {"source_digests_json": json.dumps(sources), "paths_json": json.dumps([WS + "/"]),
               "contract_files_json": json.dumps([{"path": "contract.md", "digest": digest}]),
               "brief_ref": "brief.md"}
        roles = {"auth.md": "Authority", f"{WS}/prd.md": "authority", "contract.md": "authority",
                 "brief.md": "authority", "driving.md": "driving-brief", "prd-a.md": "accepted-prd",
                 "gone.md": "authority"}
        found = planning.required_dependencies(row, {"input_roles": roles, "parent_spine": "spine.md"})
        self.assertEqual({"auth.md", "prd-a.md", "spine.md"}, set(found))


class PrepareTests(ReviewCase):
    def test_review_prepare_anchors_binding_before_reservation_and_brief_lists_sealed_paths(self):
        self.authored()
        bundle = self.bundled()["id"]
        prepared = self.prepared(bundle)
        assignment = prepared["assignment_id"]
        self.assertEqual(["bmad-review-v1"], [row["source_id"] for row in self.events("assignment.prepared", assignment)])
        self.assertEqual(1, len(self.events("assignment.bound", assignment)))
        self.assertEqual("prepared", self.state(assignment))
        binding = self.binding_of_review(assignment)
        brief = (self.t.path / prepared["brief_ref"]).read_text()
        for item in binding["sealed_files"]:
            self.assertIn(item["sealed_path"], brief)
            self.assertNotIn(item["digest"], brief)  # attestation digests are computed by the reviewer
        for text in ("adversarial", "edge-case-hunter", "read-only", "Sealed bundle", "bmad-review/SKILL.md", "claims"):
            self.assertIn(text, brief)
        detail = self.detail(assignment)
        self.assertEqual(([], "read-only"), (detail["paths"], detail["permission_mode"]))
        self.assertEqual(["independent-reviewer"], [detail["author_id"]])
        self.assertTrue({PRD_FILE, MEMLOG} <= {item["path"] for item in detail["source_digests"]})
        self.assertEqual(prepared, self.prepared(bundle))  # idempotent on the key
        self.code("IDEMPOTENCY_CONFLICT", self.prepared, bundle, reviewer="someone-else")
        self.assertEqual(sorted([PRD_FILE, MEMLOG]), sorted(item["path"] for item in binding["sealed_files"]))
        self.assertEqual([self.author_thread], binding["contributor_source_ids"])

    def test_half_finished_prepare_is_completed_and_unbound_assignment_cannot_run(self):
        self.authored()
        bundle = self.bundled()["id"]
        from unittest.mock import patch
        with patch.object(Coordinator, "bind_assignment", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self.prepared(bundle)
        orphan = next(row for row in self.store.read_snapshot()["assignments"] if row["idempotency_key"] == "r1")
        self.code("BLOCKED_EVIDENCE", self.run_it, orphan["id"])
        self.assertFalse(self.count.exists() and "spawn" in self.count.read_text().splitlines()[1:])
        done = self.prepared(bundle)
        self.assertEqual(orphan["id"], done["assignment_id"])
        self.assertEqual(1, len(self.events("assignment.bound", orphan["id"])))

    def test_brief_carries_every_contributor_assumption_and_critical_partial_flag(self):
        first, update = self.question_update()
        bundle = self.bundled()["id"]
        prepared = self.prepared(bundle)
        brief = (self.t.path / prepared["brief_ref"]).read_text()
        carried = self.binding_of_review(prepared["assignment_id"])["carried"]
        self.assertEqual([(update, "assumption", 0)],
                         [(i["contributor_id"], i["kind"], i["index"]) for i in carried["assumptions"]])
        self.assertEqual([(first, "partial", 0)],
                         [(i["contributor_id"], i["kind"], i["index"]) for i in carried["critical_flags"]])
        for text in (first, update, "EU only", "kind `partial`", "kind `assumption`"):
            self.assertIn(text, brief)
        self.assertEqual(sorted([first, update]), sorted(self.binding_of_review(prepared["assignment_id"])
                                                         ["contributor_assignment_ids"]))

    def test_review_prepare_refuses_when_live_bytes_or_sealed_copy_differ(self):
        self.authored()
        bundle = self.bundled()
        copy = Path(self.coordinator.detail("bundle", bundle["id"])["path"]) / "artifact" / PRD_FILE
        copy.chmod(0o644)
        copy.write_text("tampered sealed copy\n")
        self.code("UNKNOWN_EVIDENCE", self.prepared, bundle["id"])
        copy.write_text("# PRD\n")
        copy.chmod(0o444)
        (self.t.path / PRD_FILE).write_text("# PRD edited live\n")
        self.code("STALE_INPUT", self.prepared, bundle["id"])
        self.assertFalse([row for row in self.store.read_snapshot()["assignments"] if row["idempotency_key"] == "r1"])

    def test_review_prepare_refuses_to_write_a_brief_while_an_attempt_may_run(self):
        self.authored()
        bundle = self.bundled()["id"]
        held = self.prep("p2", slug="other")["assignment_id"]
        self.coordinator.reserve_launch(self.owner, held)  # launch-pending holds the attempt fence
        self.assertIn(held, self.code("BLOCKED_EVIDENCE", self.prepared, bundle).message)
        self.assertFalse((self.t.path / "_bmad-output/coordinator/briefs/r1.md").exists())
        self.assertEqual([], [row for row in self.store.read_snapshot()["assignments"] if row["idempotency_key"] == "r1"])

    def test_prepare_refuses_author_label_unjudged_contract_and_unbound_contributors(self):
        self.authored()
        bundle = self.bundled()["id"]
        self.code("INVALID_INPUT", self.prepared, bundle, reviewer="prd-worker")
        self.code("INVALID_INPUT", self.prepared, bundle, lenses=[])
        self.code("INVALID_INPUT", self.prepared, bundle, lenses=["Bad Lens"])
        self.code("BLOCKED_EVIDENCE", self.prepared, bundle, compat_decision_id="missing")
        self.code("NOT_FOUND", self.prepared, "no-such-bundle")
        self.coordinator.create_assignment(self.owner, self.spec(
            self.t, idempotency_key="taken", writable_paths=[], permission_mode="read-only", author_id="generic"))
        self.code("IDEMPOTENCY_CONFLICT", self.prepared, bundle, key="taken")  # a generic key is never finished
        write(self.t.path, "_bmad/custom/bmad-review.toml", "")  # drift since the contributors were bound
        self.assertIn("contributors were bound", self.code("STALE_INPUT", self.prepared, bundle).message)
        self.assertEqual([], [row for row in self.store.read_snapshot()["assignments"]
                              if row["idempotency_key"] == "r1"])

    def test_planning_review_prepare_still_needs_a_compatibility_decision(self):
        """Design D22 keeps the planning contract: a planning bundle's review refuses an absent decision."""
        self.authored()
        bundle = self.bundled()
        caught = self.code("INVALID_INPUT", review.review_prepare, self.t.repo, self.store, self.owner,
                           review.ReviewPrepare(bundle["id"], "reviewer-1", "r-none"))
        self.assertEqual("A planning review needs a compatibility decision", caught.message)


class RecordTests(ReviewCase):
    def test_review_run_with_fake_client_is_observed_only_with_distinct_thread(self):
        self.authored()
        bundle = self.bundled()["id"]
        assignment, review_id = self.reviewed(bundle)
        row = self.coordinator.detail("review", review_id)
        returned = self.detail(assignment)["return"]
        self.assertEqual(("observed", 0, "independent-reviewer"),
                         (row["provenance_class"], row["critical_open"], row["reviewer_id"]))
        self.assertEqual(returned["event_ref"], row["provenance_ref"])
        self.assertEqual("codex-cli:thread-review", row["finding"]["source_id"])
        self.assertNotEqual(self.author_thread, row["finding"]["source_id"])
        self.assertEqual(2, self.count.read_text().count("spawn"))
        self.assertEqual(["adversarial", "edge-case-hunter"], row["finding"]["finding"]["lenses_run"])

    def test_same_thread_as_author_with_different_label_is_refused(self):
        self.authored()
        bundle = self.bundled()["id"]
        prepared = self.prepared(bundle, reviewer="another-label")
        report = self.run_review(prepared["assignment_id"], thread="thread-1")  # the author's thread
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        error = self.code("BLOCKED_EVIDENCE", self.record, prepared["assignment_id"])
        self.assertIn("thread", error.message)
        self.assertEqual([], self.reviews())

    def test_correction_history_threads_are_all_contributor_threads(self):
        self.question_update(partial_thread="thread-partial")  # the later update runs as thread-1
        prepared = self.prepared(self.bundled()["id"])
        self.assertEqual(["codex-cli:thread-1", "codex-cli:thread-partial"],
                         self.binding_of_review(prepared["assignment_id"])["contributor_source_ids"])
        # reusing the earlier partial attempt's thread (not the latest contributor's) is refused too
        self.assertEqual("completed-claim", self.run_review(prepared["assignment_id"], thread="thread-partial").state)
        self.assertIn("thread", self.code("BLOCKED_EVIDENCE", self.record, prepared["assignment_id"]).message)
        self.assertEqual([], self.reviews())

    def test_malformed_review_items_are_an_invalid_return_not_a_crash(self):
        self.authored()
        prepared = self.prepared(self.bundled()["id"])
        final = self.final_for(prepared["assignment_id"], lenses_run=[1, "adversarial"])
        report = self.run_review(prepared["assignment_id"], final=final)
        self.assertEqual("unknown", report.state)
        self.assertIn("lenses run differ", " ".join(report.evidence_needs))
        self.code("BLOCKED_EVIDENCE", self.record, prepared["assignment_id"])

    def test_reviewer_label_equal_to_author_is_refused(self):
        self.authored()
        bundle = self.bundled()["id"]
        assignment, review_id = self.reviewed(bundle)
        row = self.coordinator.detail("review", review_id)
        forged = ReviewEvidence(bundle, "prd-worker", row["finding"]["source_id"], row["provenance_ref"],
                                row["finding"]["finding"], False)
        source = review.BoundReviewSource(self.t.repo, self.store, assignment)
        with self.assertRaises(DomainError):
            Coordinator(self.store, source).record_review(self.owner, forged)
        self.assertEqual(1, len(self.reviews()))

    def test_review_without_binding_or_edited_receipt_or_anchor_is_not_observed(self):
        self.authored()
        bundle = self.bundled()["id"]
        plain = self.coordinator.create_assignment(self.owner, self.spec(self.t, idempotency_key="plain", writable_paths=[],
                                                                          permission_mode="read-only",
                                                                          author_id="plain-reviewer"))
        self.code("BLOCKED_EVIDENCE", self.record, plain)  # unbound
        prepared = self.prepared(bundle)
        assignment = prepared["assignment_id"]
        self.code("BLOCKED_EVIDENCE", self.record, assignment)  # prepared, never ran
        self.run_review(assignment)
        good = ReviewEvidence(bundle, "independent-reviewer", "codex-cli:thread-review",
                              self.detail(assignment)["return"]["event_ref"], {}, False)
        source = review.BoundReviewSource(self.t.repo, self.store, assignment)
        for forged in (ReviewEvidence(bundle, "independent-reviewer", "codex-cli:other", good.source_ref, {}, False),
                       ReviewEvidence(bundle, "independent-reviewer", good.source_id, "runs/" + "0" * 64 + "/return.json",
                                      {}, False),
                       ReviewEvidence(bundle, "independent-reviewer", good.source_id, good.source_ref, {}, False)):
            with self.assertRaises(DomainError):
                Coordinator(self.store, source).record_review(self.owner, forged)
        self.assertEqual([], self.reviews())
        terminal = run_root(self.t.repo) / run_name(assignment, self.detail(assignment)["request_fingerprint"]) / "return.json"
        value = json.loads(terminal.read_text())
        value["checks"] = ["edited after the fact"]
        terminal.write_bytes(canonical(value))
        self.code("UNKNOWN_EVIDENCE", self.record, assignment)
        self.assertEqual([], self.reviews())

    def test_missing_sidecar_is_unknown_not_a_review(self):
        self.authored()
        prepared = self.prepared(self.bundled()["id"])
        self.run_review(prepared["assignment_id"])
        shutil.rmtree(run_root(self.t.repo) / run_name(prepared["assignment_id"],
                                                       self.detail(prepared["assignment_id"])["request_fingerprint"]))
        self.code("UNKNOWN_EVIDENCE", self.record, prepared["assignment_id"])
        self.assertEqual([], self.reviews())

    def test_forged_binding_cannot_launch(self):
        self.authored()
        bundle = self.bundled()["id"]
        prepared = self.prepared(bundle)
        # a second review assignment sealed with a hand-written binding that names no real bundle
        extra = self.coordinator.create_assignment(
            self.owner, self.spec(self.t, idempotency_key="forged", writable_paths=[], permission_mode="read-only",
                                  author_id="forger"), required_profile=review.REVIEW)
        value = {"kind": "binding", "format": 1, "profile": review.REVIEW, "assignment_id": extra,
                 "request_fingerprint": self.detail(extra)["request_fingerprint"], "worktree": str(self.t.path),
                 "common_dir": str(self.t.repo.common_dir), "bundle_id": "none"}
        self.coordinator.bind_assignment(self.owner, extra, seal_binding(self.base_dir, value))
        self.code("BLOCKED_EVIDENCE", self.run_it, extra)
        self.code("BLOCKED_EVIDENCE", self.coordinator.reserve_launch, self.owner, extra)
        self.assertFalse(self.count.exists() and self.count.read_text().count("spawn") > 1)
        self.assertEqual("prepared", self.state(prepared["assignment_id"]))

    def test_reviewer_blocked_or_needs_decision_is_never_a_review(self):
        self.authored()
        bundle = self.bundled()["id"]
        first = self.prepared(bundle)["assignment_id"]
        report = self.run_review(first, final=self.final_for(first, result="blocked"))
        self.assertEqual("blocked", report.state, report.evidence_needs)
        self.code("BLOCKED_EVIDENCE", self.record, first)
        second = self.prepared(bundle, key="r2")["assignment_id"]
        report = self.run_review(second, final=self.final_for(second, result="needs-decision",
                                                              unresolved_question="Which lens?"))
        self.assertEqual("needs-decision", report.state, report.evidence_needs)
        self.code("BLOCKED_EVIDENCE", self.record, second)
        self.assertEqual([], self.reviews())

    def test_bundle_read_digests_must_match_sealed_manifest_and_limit_is_documented_as_claim(self):
        self.authored()
        prepared = self.prepared(self.bundled()["id"])
        assignment = prepared["assignment_id"]
        bad = self.final_for(assignment)
        bad["bundle_read"][0]["sha256"] = "0" * 64
        report = self.run_review(assignment, final=bad)
        self.assertEqual("unknown", report.state)
        self.assertIn("per-file digest attestations", " ".join(report.evidence_needs))
        self.assertEqual("running", self.state(assignment))
        # even a matching attestation is only what the model said: the brief states that limit
        self.assertIn("claims, never proof", (self.t.path / prepared["brief_ref"]).read_text())
        self.code("BLOCKED_EVIDENCE", self.record, assignment)

    def test_critical_flag_derived_and_lower_caller_value_refused(self):
        self.authored()
        bundle = self.bundled()["id"]
        prepared = self.prepared(bundle)
        assignment = prepared["assignment_id"]
        final = self.final_for(assignment, findings=[finding("critical")])
        self.assertEqual("completed-claim", self.run_review(assignment, final=final).state)
        recorded = self.record(assignment)
        self.assertTrue(recorded["critical_open"])
        row = self.coordinator.detail("review", recorded["id"])
        self.assertEqual(1, row["critical_open"])
        self.assertEqual(recorded["id"], self.record(assignment)["id"])
        lowered = ReviewEvidence(bundle, "independent-reviewer", row["finding"]["source_id"], row["provenance_ref"],
                                 row["finding"]["finding"], False)
        with self.assertRaises(DomainError) as caught:
            Coordinator(self.store, review.BoundReviewSource(self.t.repo, self.store, assignment)).record_review(
                self.owner, lowered)
        self.assertEqual("BLOCKED_EVIDENCE", caught.exception.code)
        self.code("BLOCKED_EVIDENCE", self.coordinator.accept, self.owner, bundle, recorded["id"])

    def test_critical_disposition_of_a_carried_assumption_blocks_acceptance(self):
        self.authored()
        bundle = self.bundled()["id"]
        assignment = self.prepared(bundle)["assignment_id"]
        final = self.final_for(assignment)
        final["dispositions"][0]["severity"] = "critical"
        self.run_review(assignment, final=final)
        recorded = self.record(assignment)
        self.assertTrue(recorded["critical_open"])
        error = self.code("BLOCKED_EVIDENCE", review.accept_bound, self.store, self.owner, bundle, recorded["id"])
        self.assertIn("critical", error.message)

    def test_generic_review_add_and_imported_review_stay_reported_unknown(self):
        self.authored()
        bundle = self.bundled()["id"]
        manual = self.coordinator.record_review(self.owner, ReviewEvidence(
            bundle, "human-reviewer", "operator-message", "message:1", "looks fine", False))
        self.assertEqual("reported", self.coordinator.detail("review", manual)["provenance_class"])
        self.code("BLOCKED_EVIDENCE", self.coordinator.accept, self.owner, bundle, manual)
        data = dict(bundle_id=bundle, reviewer_id="cli-reviewer", source_id="s", source_ref="r", finding={},
                    critical_open=False)
        created = json.loads(self.cli("review", "add", "--token-file", str(self.token), data=data).stdout)["id"]
        self.assertEqual("reported", self.coordinator.detail("review", created)["provenance_class"])
        self.code("BLOCKED_EVIDENCE", self.coordinator.accept, self.owner, bundle, created)
        self.assertEqual([], self.store.read_snapshot()["acceptances"])
        # an imported checkpoint carries rows, not sidecars: the bound proof is unknown there
        assignment, bound = self.reviewed(bundle)
        review.accept_bound(self.store, self.owner, bundle, bound)
        target, clone = self.root / "exports", self.root / "imported"
        target.mkdir()
        receipt = export_checkpoint(self.store, self.owner, target)
        git(self.root, "clone", "-q", str(self.main), str(clone))
        repo = resolve_repo(clone)
        import_checkpoint(repo, receipt.directory)
        imported = open_store(repo)
        checked = review.verify_bmad(imported)
        self.assertEqual(("unknown", ["imported-evidence"]), (checked["current"], checked["checked"][0]["reasons"]))
        owner = Coordinator(imported).claim("importer", self.root / "importer.token")
        self.code("BLOCKED_EVIDENCE", review.record_bound_review, repo, imported, owner, assignment)
        self.code("UNKNOWN_EVIDENCE", review.accept_bound, imported, owner, bundle, bound)  # binding bytes absent
        self.assertEqual(1, len(imported.read_snapshot()["acceptances"]))

    def test_record_review_idempotent_across_crash_windows(self):
        from unittest.mock import patch
        self.authored()
        bundle = self.bundled()["id"]
        assignment = self.prepared(bundle)["assignment_id"]
        self.run_review(assignment)
        with patch.object(review.BoundReviewSource, "classify_review", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self.record(assignment)
        self.assertEqual([], self.reviews())  # crash before the row: nothing recorded
        first = self.record(assignment)["id"]
        self.assertEqual(first, self.record(assignment)["id"])  # crash after the row: retry returns it
        row = self.coordinator.detail("review", first)
        again = Coordinator(self.store, review.BoundReviewSource(self.t.repo, self.store, assignment)).record_review(
            self.owner, ReviewEvidence(bundle, "independent-reviewer", row["finding"]["source_id"],
                                       row["provenance_ref"], row["finding"]["finding"], False))
        self.assertEqual(first, again)  # plain core path too
        self.assertEqual(1, len(self.reviews()))
        self.assertEqual(1, len(self.events("review.recorded")))

    def test_live_mutation_after_binding_blocks_launch(self):
        self.authored()
        prepared = self.prepared(self.bundled()["id"])
        (self.t.path / PRD_FILE).write_text("# PRD edited after binding\n")
        self.code("STALE_INPUT", self.run_review, prepared["assignment_id"])
        self.assertEqual("prepared", self.state(prepared["assignment_id"]))
        self.assertEqual(1, self.count.read_text().count("spawn"))

    def test_live_mutation_during_review_leaves_no_observed_review(self):
        self.authored()
        prepared = self.prepared(self.bundled()["id"])
        report = self.run_review(prepared["assignment_id"], FAKE_WRITE=writes((PRD_FILE, "# changed by reviewer\n")))
        self.assertEqual("unknown", report.state)
        self.code("BLOCKED_EVIDENCE", self.record, prepared["assignment_id"])
        self.assertEqual([], self.reviews())

    def test_live_mutation_after_review_blocks_record(self):
        self.authored()
        prepared = self.prepared(self.bundled()["id"])
        self.assertEqual("completed-claim", self.run_review(prepared["assignment_id"]).state)
        (self.t.path / PRD_FILE).write_text("# PRD edited after the review\n")
        self.code("STALE_INPUT", self.record, prepared["assignment_id"])
        self.assertEqual([], self.reviews())


class AcceptTests(ReviewCase):
    def accepted(self):
        self.authored()
        bundle = self.bundled()["id"]
        assignment, review_id = self.reviewed(bundle)
        return bundle, assignment, review_id, review.accept_bound(self.store, self.owner, bundle, review_id)["id"]

    def checked(self):
        return review.verify_bmad(self.store)["checked"][0]

    def test_plain_accept_and_verify_apply_bound_gates(self):
        self.authored()
        bundle = self.bundled()["id"]
        fake = Coordinator(self.store, Observed()).record_review(self.owner, ReviewEvidence(
            bundle, "somebody", "codex-cli:fake", "runs/x/return.json", {}, False))
        error = self.code("BLOCKED_EVIDENCE", self.coordinator.accept, self.owner, bundle, fake)
        self.assertIn("bound review", error.message)  # an observed review not from a bound assignment
        self.cli("accept", "--token-file", str(self.token), data={"bundle_id": bundle, "review_id": fake}, code=4)
        _, review_id = self.reviewed(bundle)
        acceptance = json.loads(self.cli("accept", "--token-file", str(self.token),
                                         data={"bundle_id": bundle, "review_id": review_id}).stdout)["id"]
        self.assertEqual("current", self.coordinator.verify().current)
        plain = json.loads(self.cli("verify").stdout)
        bmad = json.loads(self.cli("bmad", "verify").stdout)
        self.assertEqual((plain["current"], plain["checked"][0]["acceptance_id"]), ("current", acceptance))
        self.assertEqual(("current", []), (bmad["current"], bmad["checked"][0]["reasons"]))

    def test_accept_depends_on_bundle_proof_contract_digests_not_unrelated_worktree_changes(self):
        self.accepted()
        write(self.t.path, "docs/unrelated.md", "later unrelated work\n")
        git(self.t.path, "add", "docs/unrelated.md")
        git(self.t.path, "-c", "user.name=T", "-c", "user.email=t@example.invalid", "commit", "-qm", "unrelated")
        write(self.t.path, "docs/more.md", "untracked later work\n")
        self.assertEqual("current", self.coordinator.verify().current)
        self.assertEqual([], self.checked()["reasons"])

    def test_changed_bundle_file_or_mandate_makes_acceptance_stale_with_reasons(self):
        self.accepted()
        (self.t.path / PRD_FILE).write_text("# PRD changed after acceptance\n")
        report = review.verify_bmad(self.store)
        self.assertEqual("stale", report["current"])
        self.assertEqual(["file-digest"], report["checked"][0]["reasons"])
        self.assertEqual("stale", json.loads(self.cli("bmad", "verify").stdout)["current"])

    def test_mandate_change_makes_acceptance_stale(self):
        self.accepted()
        self.coordinator.revise_mandate(self.owner, self.fid, 1, {"outcome": "changed"}, self.compat)
        self.assertEqual(["mandate"], self.checked()["reasons"])

    def test_added_override_blocks_accept_and_marks_verify_stale(self):
        self.authored()
        bundle = self.bundled()["id"]
        _, review_id = self.reviewed(bundle)
        write(self.t.path, "_bmad/custom/bmad-prd.toml", "")
        self.code("STALE_INPUT", review.accept_bound, self.store, self.owner, bundle, review_id)
        self.assertEqual([], self.store.read_snapshot()["acceptances"])
        (self.t.path / "_bmad/custom/bmad-prd.toml").unlink()
        review.accept_bound(self.store, self.owner, bundle, review_id)
        write(self.t.path, "_bmad/custom/bmad-review.toml", "")
        self.assertEqual("stale", review.verify_bmad(self.store)["current"])

    def test_missing_compat_decision_blocks_accept(self):
        self.authored()
        bundle = self.bundled()["id"]
        _, review_id = self.reviewed(bundle)
        other = self.coordinator.record_decision(self.owner, DecisionInput(
            self.fid, 1, "coordinator", "clause:2", "mandate edit", {"x": 2}))
        self.coordinator.revise_mandate(self.owner, self.fid, 1, {"outcome": "changed"}, other)
        self.assertEqual(1, len(self.store.read_snapshot()["bundles"]))
        error = self.code("BLOCKED_EVIDENCE", review.accept_bound, self.store, self.owner, bundle, review_id)
        self.assertIn("compatibility decision", error.message)

    def test_tampered_proof_makes_verify_unknown_with_reasons(self):
        bundle, assignment, review_id, acceptance = self.accepted()
        fingerprint = self.detail(assignment)["request_fingerprint"]
        terminal = run_root(self.t.repo) / run_name(assignment, fingerprint) / "return.json"
        value = json.loads(terminal.read_text())
        value["checks"] = ["edited after acceptance"]
        terminal.write_bytes(canonical(value))
        report = review.verify_bmad(self.store)
        self.assertEqual("unknown", report["current"])
        self.assertIn("bound review proof failed", " ".join(report["checked"][0]["reasons"]))

    def test_tampered_contributor_receipt_makes_verify_unknown(self):
        author = self.authored()
        bundle = self.bundled()["id"]
        _, review_id = self.reviewed(bundle)
        review.accept_bound(self.store, self.owner, bundle, review_id)
        terminal = run_root(self.t.repo) / run_name(author, self.detail(author)["request_fingerprint"]) / "return.json"
        value = json.loads(terminal.read_text())
        value["checks"] = ["edited"]
        terminal.write_bytes(canonical(value))
        report = review.verify_bmad(self.store)
        self.assertEqual("unknown", report["current"])
        self.assertIn("historical validation", " ".join(report["checked"][0]["reasons"]))

    def test_prd_acceptance_requires_accepted_bound_prd_work_that_includes_its_prd_file(self):
        author = self.authored()
        partial = self.coordinator.capture_bundle(self.owner, BundleInput(
            self.fid, [MEMLOG], [], [author], self.contracts(author)))  # plain capture of a subset is allowed
        _, review_id = self.reviewed(partial)
        memlog_only = review.accept_bound(self.store, self.owner, partial, review_id)["id"]
        arch = dict(skill=ARCH, altitude="feature", purpose="build-substrate", inputs=[], input_roles={})
        error = self.code("BLOCKED_EVIDENCE", self.prep, "a0", prd_acceptance=memlog_only, **arch)
        self.assertIn(PRD_FILE, error.message)
        _, workspace = self.architecture(None, inputs=["docs/authority.md"], input_roles={"docs/authority.md": "authority"})
        arch_bundle = review.bundle_for_workspace(self.t.repo, self.store, self.owner,
                                                  review.BundleRequest(self.fid, workspace))["id"]
        _, arch_review = self.reviewed(arch_bundle, key="r2")
        arch_acceptance = review.accept_bound(self.store, self.owner, arch_bundle, arch_review)["id"]
        self.assertIn("bmad-prd", self.code("INVALID_INPUT", self.prep, "a2", slug="other",
                                            prd_acceptance=arch_acceptance, **arch).message)

    def test_changed_prd_invalidates_architecture_acceptance(self):
        _, prd_acceptance = self.accepted_prd()
        assignment, workspace = self.architecture(prd_acceptance)
        result = review.bundle_for_workspace(self.t.repo, self.store, self.owner, review.BundleRequest(self.fid, workspace))
        self.assertEqual(sorted([PRD_FILE, MEMLOG]), result["dependency_paths"])
        _, review_id = self.reviewed(result["id"], key="r2")
        acceptance = review.accept_bound(self.store, self.owner, result["id"], review_id)["id"]
        states = {item["acceptance_id"]: item["current"] for item in self.coordinator.verify().checked}
        self.assertEqual({prd_acceptance: "current", acceptance: "current"}, states)
        (self.t.path / PRD_FILE).write_text("# PRD changed after both acceptances\n")
        report = review.verify_bmad(self.store)
        states = {item["acceptance_id"]: item["current"] for item in report["checked"]}
        self.assertEqual({prd_acceptance: "stale", acceptance: "stale"}, states)
        self.assertEqual("stale", report["current"])

    def test_architecture_capture_refuses_omitting_the_prd_even_if_it_was_accepted_elsewhere(self):
        _, prd_acceptance = self.accepted_prd()
        assignment, workspace = self.architecture(prd_acceptance)
        files = [self.arch_spine()[1], self.arch_spine()[2]]
        self.code("BLOCKED_EVIDENCE", self.coordinator.capture_bundle, self.owner, BundleInput(
            self.fid, files, [], [assignment], self.contracts(assignment)))
        self.assertEqual(1, len(self.store.read_snapshot()["bundles"]))


class CliTests(ReviewCase):
    def test_cli_bundle_review_record_accept_verify(self):
        self.authored()
        token = ("--token-file", str(self.token))
        bundle = json.loads(self.cli("bmad", "bundle", *token, data={"feature_id": self.fid, "workspace": WS}).stdout)
        self.assertEqual({"id", "artifact_paths", "dependency_paths", "source_assignment_ids"}, set(bundle))
        spec = dict(bundle_id=bundle["id"], reviewer_id="cli-reviewer", key="rcli", compat_decision_id=self.compat)
        prepared = json.loads(self.cli("bmad", "review", "prepare", *token, data=spec).stdout)
        self.assertEqual({"assignment_id", "brief_ref", "binding_sha256"}, set(prepared))
        self.assertEqual(prepared, json.loads(self.cli("bmad", "review", "prepare", *token, data=spec).stdout))
        self.cli("bmad", "review", "record", prepared["assignment_id"], *token, code=4)  # not run yet
        self.env.update(FAKE_THREAD="thread-cli-review",
                        FAKE_EXTRA=json.dumps(self.final_for(prepared["assignment_id"])))
        ran = json.loads(self.cli("bmad", "run", prepared["assignment_id"], *token, "--model", "m",
                                  "--timeout", "5").stdout)
        self.assertEqual(("completed-claim", None), (ran["state"], ran["verdict"]))
        recorded = json.loads(self.cli("bmad", "review", "record", prepared["assignment_id"], *token).stdout)
        self.assertEqual(("observed", False), (recorded["provenance_class"], recorded["critical_open"]))
        self.cli("bmad", "accept", *token, data={"bundle_id": bundle["id"], "review_id": recorded["id"],
                                                  "extra": 1}, code=2)
        accepted = json.loads(self.cli("bmad", "accept", *token, data={"bundle_id": bundle["id"],
                                                                         "review_id": recorded["id"]}).stdout)
        self.assertEqual("current", json.loads(self.cli("bmad", "verify").stdout)["current"])
        self.assertEqual(accepted["id"], json.loads(self.cli("bmad", "verify").stdout)["checked"][0]["acceptance_id"])
        self.cli("bmad", "bundle", *token, data={"feature_id": self.fid}, code=2)
        self.cli("bmad", "review", "prepare", *token, data={**spec, "key": "r2", "lenses": "adversarial"}, code=2)


class SealedDecisionTests(ReviewCase):
    """Attempt-4 contract fixes: the review brief prints every value the check compares verbatim, and the bundle
    seals the contributors' resolving decisions (stored rows only) so acceptance ties to them."""

    def decided_bundle(self):
        self.env["FAKE_THREAD"] = "thread-1"
        first, request = self.question_run()
        del self.env["FAKE_THREAD"]
        decision = self.decide_request(request)
        caller = {"id": decision, "text": "CALLER SUMMARY, never sealed", "source_kind": "coordinator",
                  "source_ref": "clause:delegated-1"}
        update = self.prep("u1", **self.update_args(first, request), decisions=[caller])["assignment_id"]
        report = self.execute(update, flow(intent="update", assumptions=["EU only"]),
                              [(PRD_FILE, "# PRD v2\n"), (MEMLOG, "log\nchange\n")])
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        self.assertIn(caller["text"], self.brief_of({"brief_ref": self.detail(update)["brief_ref"]}))
        row = next(item for item in self.store.read_snapshot()["decisions"] if item["id"] == decision)
        return self.bundled()["id"], row

    def test_review_brief_prints_the_values_the_check_compares(self):
        self.authored()
        bundle = self.bundled()["id"]
        prepared = self.prepared(bundle)
        binding = self.binding_of_review(prepared["assignment_id"])
        text = (self.t.path / prepared["brief_ref"]).read_text()
        for line in (f"- bundle_id: `{binding['bundle_id']}`", f"- manifest_digest: `{binding['manifest_digest']}`",
                     f"- sealed root: `{binding['sealed_root']}`", f"- lenses: {', '.join(binding['lenses'])}"):
            self.assertIn(line + "\n", text)
        for item in binding["sealed_files"]:
            self.assertIn(f"`{item['path']}` at `{item['sealed_path']}`", text)
        manifest = json.loads((Path(self.coordinator.detail("bundle", bundle)["path"]) / "manifest.json").read_bytes())
        self.assertNotIn("decisions", manifest)  # no resolved question: the manifest shape (and bundle id) is unchanged
        self.assertIn("## Recorded decisions (coordinator- or operator-recorded facts)\n", text)
        self.assertIn("their mere existence is not a finding.\n- none\n", text)
        final = self.final_for(prepared["assignment_id"])  # exactly what the brief shows passes the check
        self.assertTrue(review.check(final | {"result": "completed"}, {}, binding, None, [])["valid"])

    def test_review_brief_heading_distinguishes_resolving_from_prepare_decisions(self):
        self.authored()
        text = (self.t.path / self.prepared(self.bundled()["id"])["brief_ref"]).read_text()
        self.assertIn("These decisions resolved a contributor's question or were given to a contributor at "
                      "prepare.", text)
        self.assertNotIn("resolved the contributors' questions", text)

    def test_architecture_bundle_inherits_decisions_sealed_in_its_accepted_prd_bundle(self):
        import sqlite3
        from contextlib import closing
        from coordinator_core.service import decision_digest
        decision = self.coordinator.record_decision(self.owner, DecisionInput(
            self.fid, 1, "operator-reported", "operator:prd-scope-1", "scope ruling",
            {"answer": "STORED CHOICE: FR-5 to FR-8 stay in scope"}))
        caller = {"id": decision, "text": "CALLER TEXT never inherited", "source_kind": "operator-reported",
                  "source_ref": "operator:prd-scope-1"}
        prd = self.prep("p1", decisions=[caller])["assignment_id"]
        self.assertEqual("completed-claim", self.execute(prd, flow(), self.first_files()).state)
        prd_bundle = self.bundled()["id"]
        _, review_id = self.reviewed(prd_bundle)
        acceptance = review.accept_bound(self.store, self.owner, prd_bundle, review_id)["id"]
        row = next(item for item in self.store.read_snapshot()["decisions"] if item["id"] == decision)
        sealed = [{"id": decision, "digest": decision_digest(row)}]
        architect, workspace = self.architecture(acceptance)
        binding = self.binding_of(architect)
        self.assertEqual((sealed, [{"id": decision, "bundle_id": prd_bundle}]),
                         (binding["decisions"], binding["inherited_decisions"]))
        bundle = review.bundle_for_workspace(self.t.repo, self.store, self.owner,
                                             review.BundleRequest(self.fid, workspace))["id"]
        manifest = json.loads((Path(self.coordinator.detail("bundle", bundle)["path"]) / "manifest.json").read_bytes())
        self.assertEqual(sealed, manifest["decisions"])
        text = (self.t.path / self.prepared(bundle, "r2")["brief_ref"]).read_text()
        recorded = text.partition("## Recorded decisions")[2].partition("\n## ")[0]
        self.assertIn("their mere existence is not a finding.\n- none\n", recorded)  # neither resolving nor passed
        inherited = text.partition("## Inherited decisions")[2].partition("\n## ")[0]
        self.assertIn(f"- `{decision}` (operator-reported, `operator:prd-scope-1`, mandate revision 1; accepted "
                      f"bundle `{prd_bundle}`): {row['choice_json']}\n", inherited)
        self.assertNotIn("CALLER TEXT", text)
        _, arch_review = self.reviewed(bundle, "r3")
        review.accept_bound(self.store, self.owner, bundle, arch_review)
        with closing(sqlite3.connect(self.store.path)) as conn, conn:
            conn.execute("UPDATE decisions SET reason=? WHERE id=?", ("altered", decision))
        self.assertEqual({"stale"}, {item["current"] for item in self.coordinator.verify().checked})
        self.assertEqual({"decision-record"}, {item["reason"] for item in self.coordinator.verify().checked})
        self.code("STALE_INPUT", self.prepared, bundle, "r4")
        self.code("STALE_INPUT", review.bundle_for_workspace, self.t.repo, self.store, self.owner,
                  review.BundleRequest(self.fid, workspace))

    def test_inherited_decisions_fail_closed_at_prepare_and_unrelated_unreadable_bundle_never_blocks(self):
        import sqlite3
        from contextlib import closing
        from coordinator_core.service import decision_digest
        decision = self.coordinator.record_decision(self.owner, DecisionInput(
            self.fid, 1, "operator-reported", "operator:prd-scope-2", "scope ruling", {"answer": "stored"}))
        caller = {"id": decision, "text": "caller", "source_kind": "operator-reported",
                  "source_ref": "operator:prd-scope-2"}
        prd = self.prep("p1", decisions=[caller])["assignment_id"]
        self.assertEqual("completed-claim", self.execute(prd, flow(), self.first_files()).state)
        prd_bundle = self.bundled()["id"]
        _, review_id = self.reviewed(prd_bundle)
        acceptance = review.accept_bound(self.store, self.owner, prd_bundle, review_id)["id"]
        _, workspace = self.architecture(None, key="a0", inputs=["docs/authority.md"],
                                         input_roles={"docs/authority.md": "authority"})
        unrelated = review.bundle_for_workspace(self.t.repo, self.store, self.owner,
                                                review.BundleRequest(self.fid, workspace))["id"]
        _, other_review = self.reviewed(unrelated, "r2")
        review.accept_bound(self.store, self.owner, unrelated, other_review)
        path = Path(self.coordinator.detail("bundle", unrelated)["path"])
        path.chmod(0o755)
        (path / "manifest.json").chmod(0o644)
        (path / "manifest.json").write_text("unreadable\n")  # an accepted bundle no contributor consumes
        arch = dict(skill=ARCH, altitude="feature", purpose="build-substrate")
        explicit = dict(inputs=[PRD_FILE], input_roles={PRD_FILE: "accepted-prd"})
        row = next(item for item in self.store.read_snapshot()["decisions"] if item["id"] == decision)
        sealed, record = [{"id": decision, "digest": decision_digest(row)}], [{"id": decision, "bundle_id": prd_bundle}]
        for key, over in (("a1", explicit), ("a2", {"prd_acceptance": acceptance, "inputs": [], "input_roles": {}})):
            with self.subTest(key=key):
                binding = self.binding_of(self.prep(key, slug="later-" + key, **arch, **over)["assignment_id"])
                self.assertEqual((sealed, record), (binding["decisions"], binding["inherited_decisions"]))
        passed = self.binding_of(self.prep("a3", slug="later-a3", decisions=[caller], **arch, **explicit)
                                 ["assignment_id"])  # also passed at prepare: its own provenance, not inherited
        self.assertEqual(sealed, passed["decisions"])
        self.assertNotIn("inherited_decisions", passed)
        with closing(sqlite3.connect(self.store.path)) as conn, conn:
            conn.execute("UPDATE decisions SET reason=? WHERE id=?", ("altered", decision))
        self.code("STALE_INPUT", self.prep, "a4", slug="later-a4", **arch, **explicit)
        with closing(sqlite3.connect(self.store.path)) as conn, conn:
            conn.execute("DELETE FROM decisions WHERE id=?", (decision,))
        self.code("UNKNOWN_EVIDENCE", self.prep, "a5", slug="later-a5", **arch, **explicit)
        self.assertEqual([], [item for item in self.store.read_snapshot()["assignments"]
                              if item["idempotency_key"] in ("a4", "a5")])

    def test_architecture_author_brief_lists_inherited_decisions_from_stored_rows_only(self):
        """Smoke attempt 8: the architecture review listed the accepted PRD's decisions; the author never saw them."""
        decision = self.coordinator.record_decision(self.owner, DecisionInput(
            self.fid, 1, "operator-reported", "operator:prd-scope-5", "STORED REASON",
            {"answer": "STORED CHOICE: FR-5 to FR-8 stay in scope"}))
        caller = {"id": decision, "text": "CALLER TEXT never inherited", "source_kind": "operator-reported",
                  "source_ref": "operator:prd-scope-5"}
        prd = self.prep("p1", decisions=[caller])
        self.assertEqual("completed-claim", self.execute(prd["assignment_id"], flow(), self.first_files()).state)
        self.assertNotIn("## Inherited decisions", self.brief_of(prd))  # nothing accepted is consumed
        prd_bundle = self.bundled()["id"]
        _, review_id = self.reviewed(prd_bundle)
        acceptance = review.accept_bound(self.store, self.owner, prd_bundle, review_id)["id"]
        row = next(item for item in self.store.read_snapshot()["decisions"] if item["id"] == decision)
        line = (f"- `{decision}` (operator-reported, operator:prd-scope-5, mandate revision 1; accepted bundle "
                f"`{prd_bundle}`); reason: STORED REASON; choice: {row['choice_json']}")
        arch = dict(skill=ARCH, altitude="feature", purpose="build-substrate")
        explicit = dict(inputs=[PRD_FILE], input_roles={PRD_FILE: "accepted-prd"})
        through = dict(prd_acceptance=acceptance, inputs=[], input_roles={})
        for key, client, over in (("a1", "codex-cli", explicit), ("a2", "codex-cli", through),
                                  ("a3", "claude-code", through), ("a4", "copilot", through)):
            with self.subTest(key=key, client=client):
                text = self.brief_of(self.prep(key, slug="later-" + key, client=client, **arch, **over))
                section = text.partition("\n## Inherited decisions (sealed in an accepted bundle whose artifact you "
                                         "are given)\n")[2].partition("\n## ")[0]
                self.assertEqual(["These decisions were sealed in the accepted bundle named on each line, whose "
                                  "artifact is one of your `accepted-prd` inputs. Honour them; if your work must "
                                  "contradict one, list that in `conflicts_with_prior_decisions` instead of changing "
                                  "it silently.", line], section.splitlines())
                self.assertNotIn("CALLER TEXT", text)
                self.assertNotIn("## Recorded decisions", text)
        passed = self.brief_of(self.prep("a5", slug="later-a5", decisions=[caller], **arch, **explicit))
        self.assertNotIn("## Inherited decisions", passed)  # also passed at prepare: its own provenance only
        self.assertIn(f"- `{decision}` (operator-reported, operator:prd-scope-5); reason: STORED REASON;", passed)

    def accepted_prd_deciding(self, ref):
        """A PRD author given one stored decision at prepare, bundled, reviewed and accepted."""
        decision = self.coordinator.record_decision(self.owner, DecisionInput(
            self.fid, 1, "operator-reported", ref, "scope ruling", {"answer": "stored"}))
        caller = {"id": decision, "text": "caller", "source_kind": "operator-reported", "source_ref": ref}
        prd = self.prep("p1", decisions=[caller])["assignment_id"]
        self.assertEqual("completed-claim", self.execute(prd, flow(), self.first_files()).state)
        prd_bundle = self.bundled()["id"]
        _, review_id = self.reviewed(prd_bundle)
        acceptance = review.accept_bound(self.store, self.owner, prd_bundle, review_id)["id"]
        row = next(item for item in self.store.read_snapshot()["decisions"] if item["id"] == decision)
        return prd, prd_bundle, acceptance, row

    def test_decision_both_passed_and_inherited_is_listed_in_both_review_sections(self):
        _, prd_bundle, acceptance, row = self.accepted_prd_deciding("operator:prd-scope-3")
        self.architecture(acceptance)  # inherits the decision the PRD author was passed at prepare
        parent = review.BundleRequest(self.fid, WS.rsplit("/", 1)[0])  # holds both workspaces and contributors
        bundle = review.bundle_for_workspace(self.t.repo, self.store, self.owner, parent)["id"]
        text = (self.t.path / self.prepared(bundle, "r2")["brief_ref"]).read_text()
        recorded = text.partition("## Recorded decisions")[2].partition("\n## ")[0]
        inherited = text.partition("## Inherited decisions")[2].partition("\n## ")[0]
        line = f"- `{row['id']}` (operator-reported, `operator:prd-scope-3`, mandate revision 1"
        self.assertEqual(1, text.count(f"{line}): {row['choice_json']}\n"))
        self.assertIn(f"{line}): {row['choice_json']}\n", recorded)  # passed to the PRD author: own provenance
        self.assertIn(f"{line}; accepted bundle `{prd_bundle}`): {row['choice_json']}\n", inherited)

    def test_unreadable_contributor_binding_falls_back_to_the_manifest_and_unreadable_manifest_refuses(self):
        from coordinator_core.service import decision_digest
        prd, prd_bundle, _, row = self.accepted_prd_deciding("operator:prd-scope-4")
        _, bound = binding_anchor(self.store.read_snapshot()["events"], prd)
        (self.base_dir / bound["evidence_ref"]).chmod(0o644)  # no longer a private file: unreadable as a binding
        self.code("UNKNOWN_EVIDENCE", self.binding_of, prd)
        arch = dict(skill=ARCH, altitude="feature", purpose="build-substrate", inputs=[PRD_FILE],
                    input_roles={PRD_FILE: "accepted-prd"})
        binding = self.binding_of(self.prep("a1", slug="later-a1", **arch)["assignment_id"])
        self.assertEqual(([{"id": row["id"], "digest": decision_digest(row)}],
                          [{"id": row["id"], "bundle_id": prd_bundle}]),
                         (binding["decisions"], binding["inherited_decisions"]))  # the sealed manifest decided
        path = Path(self.coordinator.detail("bundle", prd_bundle)["path"])
        path.chmod(0o755)
        (path / "manifest.json").chmod(0o644)
        (path / "manifest.json").write_text("unreadable\n")
        self.code("UNKNOWN_EVIDENCE", self.prep, "a2", slug="later-a2", **arch)  # neither can prove it unrelated
        self.assertEqual([], [item for item in self.store.read_snapshot()["assignments"]
                              if item["idempotency_key"] == "a2"])

    def test_review_brief_announces_every_refusal_of_the_review_check(self):
        import inspect
        import re
        self.authored()
        bundle = self.bundled()["id"]
        texts = [(self.t.path / self.prepared(bundle, key, client=client)["brief_ref"]).read_text()
                 for key, client in (("r-codex", "codex-cli"), ("r-claude", "claude-code"))]
        source = inspect.getsource(review.check)
        labels = sorted(set(re.findall(r'_exact\(item, (?:\([^)]*\)|[A-Z_]+), "([a-z_]+)"\)', source)))
        self.assertEqual(["bundle_read", "disposition", "finding"], labels)
        refusals = re.findall(r'_bad\("([^"]+)"\)', source) + [f"{label} entry has the wrong fields" for label in labels]
        self.assertEqual(12, len(refusals))
        for text in texts:
            for message in refusals:
                self.assertIn(message, text)
            self.assertIn("its `lens` is one of the profile line's lenses, verbatim", text)
            self.assertIn("`kind` one of `assumption`, `partial`", text)
            self.assertIn("`severity` one of `critical`, `high`, `medium`, `low`", text)

    def test_severity_rubric_makes_a_mandate_contradiction_critical_for_every_review_client(self):
        """Smoke attempt 8: a defect tied to a decided RFC 8259 rule was rated `high`; the contract now names it."""
        self.authored()
        bundle = self.bundled()["id"]
        for key, client in (("r-codex", "codex-cli"), ("r-claude", "claude-code")):
            with self.subTest(client=client):
                text = (self.t.path / self.prepared(bundle, key, client=client)["brief_ref"]).read_text()
                rubric = text.partition("\n## Severity rubric\n")[2].partition("\n## ")[0]
                self.assertIn("`critical` = unsafe for downstream use or contradicts the mandate; `high`, `medium`, "
                              "`low` otherwise by consequence. BMAD review has no severity; you assign it here. Never "
                              "lower a severity to make work pass.\n", rubric)  # the rest of the rubric is kept
                self.assertIn("- A finding that contradicts a mandate clause (for example a rule in a sealed "
                              "`[dependency]` file, or a recorded or inherited decision above), or that makes an "
                              "artifact's output invalid under a standard the mandate requires, IS `critical`, however "
                              "narrow its trigger. Cite that clause id (the rule's id or heading, or the decision id) "
                              "in the finding's `potential_consequence`.\n", rubric)
                self.assertIn("- A `critical` disposition of a carried item counts like a `critical` finding: either "
                              "one sets the review's `critical_open`, which the coordinator derives (you never return "
                              "it) and which blocks acceptance.", rubric)
        disposition = {"contributor_id": "c", "kind": "assumption", "index": 0, "severity": "critical"}
        self.assertTrue(review.critical_open({"findings": [], "dispositions": [disposition]}))  # what the brief states

    def test_bundle_seals_resolving_decisions_and_the_brief_renders_stored_rows_only(self):
        bundle, row = self.decided_bundle()
        detail = self.coordinator.detail("bundle", bundle)
        manifest = json.loads((Path(detail["path"]) / "manifest.json").read_bytes())
        from coordinator_core.service import decision_digest
        self.assertEqual([{"id": row["id"], "digest": decision_digest(row)}], manifest["decisions"])
        text = (self.t.path / self.prepared(bundle)["brief_ref"]).read_text()
        self.assertIn("## Recorded decisions (coordinator- or operator-recorded facts)", text)
        self.assertIn("their mere existence is not a finding", text)
        self.assertIn(f"- `{row['id']}` ({row['source_kind']}, `{row['source_ref']}`, mandate revision "
                      f"{row['mandate_revision']}): {row['choice_json']}\n", text)
        self.assertNotIn("CALLER SUMMARY", text)

    def test_prepare_decisions_are_sealed_and_reviewed_from_stored_rows(self):
        decision = self.coordinator.record_decision(self.owner, DecisionInput(
            self.fid, 1, "operator-reported", "operator:review-correction-1", "correction after review",
            {"answer": "STORED CHOICE: keep leading zeros as strings"}))
        caller = {"id": decision, "text": "CALLER TEXT never sealed", "source_kind": "operator-reported",
                  "source_ref": "operator:review-correction-1"}
        prepared = self.prep("p1", decisions=[caller])
        row = next(item for item in self.store.read_snapshot()["decisions"] if item["id"] == decision)
        from coordinator_core.service import decision_digest
        sealed = [{"id": decision, "digest": decision_digest(row)}]
        self.assertEqual(sealed, self.binding_of(prepared["assignment_id"])["decisions"])
        self.assertIn(caller["text"], self.brief_of(prepared))  # the author brief keeps its caller summary
        report = self.execute(prepared["assignment_id"], flow(), self.first_files())
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        bundle = self.bundled()["id"]
        manifest = json.loads((Path(self.coordinator.detail("bundle", bundle)["path"]) / "manifest.json").read_bytes())
        self.assertEqual(sealed, manifest["decisions"])
        text = (self.t.path / self.prepared(bundle)["brief_ref"]).read_text()
        self.assertIn(f"- `{decision}` (operator-reported, `operator:review-correction-1`, mandate revision 1): "
                      f"{row['choice_json']}\n", text)
        self.assertNotIn("CALLER TEXT", text)
        _, review_id = self.reviewed(bundle, "r2")
        review.accept_bound(self.store, self.owner, bundle, review_id)
        self.assertEqual("current", self.coordinator.verify().current)
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(self.store.path)) as conn, conn:
            conn.execute("UPDATE decisions SET reason=? WHERE id=?", ("altered", decision))
        report = self.coordinator.verify()
        self.assertEqual(("stale", "decision-record"), (report.current, report.checked[0]["reason"]))
        self.code("STALE_INPUT", self.bundled)  # a re-capture refuses the changed bound decision too

    def test_bound_decision_changed_or_missing_before_capture_or_accept_fails_closed(self):
        import sqlite3
        from contextlib import closing
        decision = self.coordinator.record_decision(self.owner, DecisionInput(
            self.fid, 1, "coordinator", "clause:correction-2", "correction", {"answer": "stored"}))
        prepared = self.prep("p1", decisions=[{"id": decision, "text": "caller", "source_kind": "coordinator",
                                               "source_ref": "clause:correction-2"}])
        report = self.execute(prepared["assignment_id"], flow(), self.first_files())
        self.assertEqual("completed-claim", report.state, report.evidence_needs)

        def sql(statement, *args):
            with closing(sqlite3.connect(self.store.path)) as conn, conn:
                conn.execute(statement, args)

        sql("UPDATE decisions SET reason=? WHERE id=?", "changed between prepare and capture", decision)
        self.code("STALE_INPUT", self.bundled)  # the bound digest no longer matches: refused, never sealed
        self.assertEqual([], self.store.read_snapshot()["bundles"])
        row = next(item for item in self.store.read_snapshot()["decisions"] if item["id"] == decision)
        sql("DELETE FROM decisions WHERE id=?", decision)
        self.code("UNKNOWN_EVIDENCE", self.bundled)  # a missing bound decision
        sql("INSERT INTO decisions(id, feature_id, mandate_revision, source_kind, source_ref, reason, choice_json, "
            "request_id) VALUES (?,?,?,?,?,?,?,?)", row["id"], row["feature_id"], row["mandate_revision"],
            row["source_kind"], row["source_ref"], "correction", row["choice_json"], row["request_id"])
        bundle = self.bundled()["id"]  # the restored row matches the prepare-time digest again
        _, review_id = self.reviewed(bundle)
        sql("UPDATE decisions SET reason=? WHERE id=?", "changed before accept", decision)
        self.code("STALE_INPUT", review.accept_bound, self.store, self.owner, bundle, review_id)
        self.assertEqual([], self.store.read_snapshot()["acceptances"])

    def test_changed_or_missing_sealed_decision_makes_review_prepare_and_verify_stale_or_unknown(self):
        bundle, row = self.decided_bundle()
        _, review_id = self.reviewed(bundle)
        review.accept_bound(self.store, self.owner, bundle, review_id)
        self.assertEqual("current", self.coordinator.verify().current)
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(self.store.path)) as conn, conn:
            conn.execute("UPDATE decisions SET choice_json=? WHERE id=?", ('{"answer":"altered"}', row["id"]))
        report = self.coordinator.verify()
        self.assertEqual(("stale", "decision-record"), (report.current, report.checked[0]["reason"]))
        self.code("STALE_INPUT", self.prepared, bundle, "r-after")
        from coordinator_core.service import sealed_decisions_state
        manifest = json.loads((Path(self.coordinator.detail("bundle", bundle)["path"]) / "manifest.json").read_bytes())
        self.assertEqual("unknown", sealed_decisions_state(manifest, []))  # a missing row
        self.assertEqual("current", sealed_decisions_state({"files": []}, []))


class FindingCountsTests(unittest.TestCase):
    def test_finding_counts_count_findings_and_dispositions_by_severity_with_every_key(self):
        view = {"finding": {"lenses_run": [], "bundle_read": [],
                            "findings": [{"severity": "medium"}, {"severity": "medium"}, {"severity": "critical"}],
                            "dispositions": [{"severity": "low"}, {"severity": "high"}]}}
        self.assertEqual({"critical": 1, "high": 1, "medium": 2, "low": 1}, finding_counts(json.dumps(view)))
        self.assertEqual({"critical": 0, "high": 0, "medium": 0, "low": 0}, finding_counts(json.dumps({"finding": {}})))
        for bad in (None, "", "not json", json.dumps([]), json.dumps({"finding": {"findings": [{"severity": "odd"}]}})):
            with self.subTest(bad=bad):
                self.assertEqual({"critical": 0, "high": 0, "medium": 0, "low": 0}, finding_counts(bad))


if __name__ == "__main__":
    unittest.main()
