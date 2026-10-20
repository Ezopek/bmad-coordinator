"""One conformance mixin per client (design section 10). Each subclass runs the client's fake; each test asserts the
client's declared behaviour or the declared absence of a feature. Fakes prove coordinator logic only."""

import hashlib
import json
import os
import shutil
import stat
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from coordinator_core import bmad_review as review
from coordinator_core import clients, worker
from coordinator_core.model import BundleInput
from coordinator_core.run_receipt import canonical, private_file, seal_binding
from coordinator_core.worker import historical_return, inspect_run, reconcile_run
from tests.fake_clients import FORBIDDEN, TEST_CLIENTS, Harness, safe_path
from tests.test_bmad_core import writes
from tests.test_bmad_planning import flow, paths

IDENTITY_MODES = {mode: mode for mode in ("completed", "sleep", "blocked", "needs-decision", "wrong-id",
                                          "bad-schema", "truncated", "exit-1")}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ClientConformance:
    CLIENT: str
    MODES: dict[str, str]
    VERSION: str | None = None                   # the fake's --version text; None: "<client> fake 1"
    FINAL_KEY = "final"                          # stdout clients: the terminal record key holding the final object
    TOOLS_ENV: dict = {}                         # fake knobs of an author run that reports tools (default: none needed)

    def author(self, t=None, key="one", **env):
        assignment = self.assign(t, idempotency_key=key)
        return assignment, self.go(assignment, t, **{"FAKE_WRITE": writes(("result.md", key)), **env})

    def assert_unknown_held(self, report, assignment):
        self.assertEqual("unknown", report.state, report.evidence_needs)
        self.assertEqual("running", self.state(assignment))

    def assert_untouched(self, assignment):
        self.assertEqual("prepared", self.state(assignment))
        self.assertEqual([], self.reserved(assignment))
        self.assertFalse(os.path.lexists(self.run_dir(assignment)))

    def failing(self, mode, timeout=5):
        t = self.tree(mode)
        assignment = self.assign(t, idempotency_key=mode)
        return assignment, self.go(assignment, t, timeout, FAKE_MODE=self.MODES[mode])

    def isolated(self, name, timeout=5, **env):
        root = self.root / "isolated"
        root.mkdir(exist_ok=True)
        cwd = root / (name + "-cwd")
        cwd.mkdir()
        subprocess.run(["git", "-C", str(cwd), "init", "-q"], check=True)
        envelope = {"role": "judge", "assignment_id": "judge-" + name, "request_fingerprint": "f" * 64}
        with patch.dict(os.environ, {**self.env, **env}):
            self.assertEqual(self.fake_dir, Path(shutil.which(self.client.executable)).parent)
            return clients.run_isolated(self.CLIENT, self.t.repo, root, cwd, root / (name + "-receipts"), envelope,
                                        worker._schema(), "fake-model", timeout)

    def test_completed_author_return_is_observed_and_replayable_by_inspect_and_reconcile(self):
        assignment, report = self.author()
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        row = self.coordinator.detail("assignment", assignment)
        self.assertEqual("observed", row["return"]["provenance_class"])
        self.assertEqual(self.CLIENT, clients.client_of(row["return"]["source_id"]))
        launch, terminal = self.receipt(report, "launch.json"), self.receipt(report)
        self.assertEqual((self.CLIENT, self.CLIENT), (launch["client"], terminal["client"]))
        self.assertEqual([self.CLIENT], self.reserved(assignment))
        self.assertTrue(clients.integrity_clean(self.receipt(report, "integrity.json")))
        self.assertEqual("completed-claim", inspect_run(self.t.repo, self.store, assignment).state)
        self.assertEqual("completed-claim", reconcile_run(self.t.repo, self.store, self.owner, assignment).state)
        self.assertEqual(row["return"]["source_id"], historical_return(self.t.repo, self.store, assignment).source_id)

    def test_each_attempt_gets_a_fresh_prefixed_thread_id(self):
        ids = []
        for key in ("one", "two"):
            _, report = self.author(key=key)
            self.assertEqual("completed-claim", report.state, report.evidence_needs)
            ids.append(self.receipt(report)["source_id"])
        self.assertNotEqual(ids[0], ids[1])
        self.assertTrue(all(item.startswith(self.CLIENT + ":") and len(item) > len(self.CLIENT) + 1 for item in ids))

    def test_readonly_reviewer_zero_delta_observed_and_any_write_is_unknown(self):
        reader = self.assign(idempotency_key="reader", permission_mode="read-only", writable_paths=[])
        if not self.client.write_free_readonly:
            self.code("UNSUPPORTED_CLIENT_MODE", self.go, reader)
            self.assert_untouched(reader)
            self.assertEqual(0, self.version_count())
            return
        report = self.go(reader)
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        self.assertEqual([], self.coordinator.detail("assignment", reader)["return"]["changed_paths"])
        t = self.tree("writer")
        writer = self.assign(t, idempotency_key="writer", permission_mode="read-only", writable_paths=[])
        self.assert_unknown_held(self.go(writer, t, FAKE_WRITE=writes(("result.md", "w"))), writer)

    def test_timeout_kills_group_records_unknown_and_keeps_fence(self):
        assignment = self.assign()
        report = self.go(assignment, timeout=1, FAKE_MODE=self.MODES["sleep"])
        self.assert_unknown_held(report, assignment)
        self.assertIn("SIGTERM", self.receipt(report, "timeout.json")["signals"])
        self.assertTrue(self.receipt(report, "integrity.json")["group_terminated"])
        other = self.assign(idempotency_key="other")
        self.code("BLOCKED_EVIDENCE", self.coordinator.reserve_launch, self.owner, other, self.CLIENT)

    def test_schema_invalid_return_is_unknown_and_keeps_fence(self):
        self.assert_unknown_held(*reversed(self.failing("bad-schema")))

    def test_truncated_or_missing_terminal_is_unknown_and_keeps_fence(self):
        self.assert_unknown_held(*reversed(self.failing("truncated")))

    def test_nonzero_exit_is_unknown(self):
        assignment, report = self.failing("exit-1")
        self.assert_unknown_held(report, assignment)
        self.assertEqual(1, self.receipt(report, "end.json")["exit_code"])
        # design D25: the stream is read first, but a run that is no client stop keeps the exit code as its cause
        self.assertEqual(["Worker process exited unsuccessfully"], report.evidence_needs)

    def test_wrong_assignment_identity_is_unknown(self):
        self.assert_unknown_held(*reversed(self.failing("wrong-id")))

    def test_missing_binary_refused_before_reservation(self):
        empty = self.root / "empty-bin"
        empty.mkdir()
        path = safe_path(empty)
        self.assertIsNone(shutil.which(self.client.executable, path=path))
        assignment = self.assign()
        with patch.dict(os.environ, {**self.env, "PATH": path}):
            self.code("BLOCKED_EVIDENCE", worker.run_assignment, self.t.repo, self.store, self.owner, assignment,
                      "fake-model", 5, self.CLIENT)
        self.assert_untouched(assignment)

    def test_failing_version_probe_refused_before_reservation(self):
        for failure in ("exit", "empty"):
            with self.subTest(failure=failure):
                assignment = self.assign(idempotency_key=failure)
                self.code("BLOCKED_EVIDENCE", self.go, assignment, FAKE_VERSION_FAIL=failure)
                self.assert_untouched(assignment)
        self.assertFalse(self.count.exists())

    def test_edited_raw_output_after_anchor_makes_inspect_unknown(self):
        assignment, report = self.author()
        directory = Path(report.run_dir)
        raw = ["events.jsonl", "last.json"]
        raw += [clients.WORKER_RETURN + "/return.json"] if self.client.return_source == "worker-file" else []
        raw += [self.client.observation_name] if self.client.observation_name else []
        for name in raw:
            with self.subTest(name=name):
                original = (directory / name).read_bytes()
                (directory / name).write_bytes(original.replace(b"}", b" }", 1))
                self.assertEqual("unknown", inspect_run(self.t.repo, self.store, assignment).state)
                (directory / name).write_bytes(original)
                self.assertEqual("completed-claim", inspect_run(self.t.repo, self.store, assignment).state)

    def test_argv_carries_no_resume_continue_or_bypass_flag(self):
        _, report = self.author()
        self.assertEqual("completed-claim", report.state, report.evidence_needs)  # the fake exits 3 on one
        self.assertFalse(set(self.receipt(report, "launch.json")["argv"]) & set(FORBIDDEN[self.CLIENT]))

    def test_launch_binary_equals_argv0(self):
        _, report = self.author()
        launch = self.receipt(report, "launch.json")
        self.assertEqual(launch["binary"], launch["argv"][0])
        self.assertEqual(str(self.fake_dir / self.client.executable), launch["binary"])
        self.assertEqual(self.VERSION or self.CLIENT + " fake 1", launch["cli_version"])

    def test_worker_created_last_or_end_file_is_unknown(self):
        names = ["end.json"] + ([] if self.client.return_source == "client-file" else ["last.json"])
        for name in names:
            with self.subTest(name=name):
                t = self.tree("made-" + name.split(".")[0])
                assignment = self.assign(t, idempotency_key=name)
                report = self.go(assignment, t, FAKE_RUN_FILE=str(self.run_dir(assignment, t) / name),
                                 FAKE_WRITE=writes(("result.md", name)))
                self.assert_unknown_held(report, assignment)

    def test_modifying_or_deleting_sealed_receipt_or_binding_during_run_is_unknown_and_keeps_fence(self):
        first, report = self.author(key="first")
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        bundle = self.coordinator.capture_bundle(self.owner, BundleInput(
            self.t.feature_id, ["result.md"], [], [first], [self.entry(self.t, "contract.md")]))
        binding = seal_binding(self.base_dir, {"kind": "binding", "profile": "other", "assignment_id": "x",
                                               "request_fingerprint": "y"})
        targets = [("modify", Path(report.run_dir) / "return.json"), ("delete", self.base_dir / binding.ref),
                   ("modify", Path(self.coordinator.detail("bundle", bundle)["path"]) / "artifact" / "result.md")]
        for folder, action in (("pipelines", "modify"), ("drives/lease-1", "delete"), ("resolutions", "modify")):
            (self.base_dir / folder).mkdir(mode=0o700, parents=True, exist_ok=True)  # the driver's sealed folders
            private_file(self.base_dir / folder / "sealed.json", b'{"kind":"driver"}')
            targets.append((action, self.base_dir / folder / "sealed.json"))
        for index, (action, path) in enumerate(targets):
            with self.subTest(action=action, path=path.name):
                t = self.tree(f"tamper-{index}")
                assignment = self.assign(t, idempotency_key=f"tamper-{index}")
                result = self.go(assignment, t, FAKE_TAMPER=json.dumps([[action, str(path)]]),
                                 FAKE_WRITE=writes(("result.md", "t")))
                self.assert_unknown_held(result, assignment)
                record = self.receipt(result, "integrity.json")
                self.assertEqual([path.relative_to(self.base_dir).as_posix()],
                                 record["modified" if action == "modify" else "deleted"])
        t = self.tree("driver-addition")  # an addition under a driver folder during a run is legal
        assignment = self.assign(t, idempotency_key="driver-addition")
        added = self.go(assignment, t, FAKE_RUN_FILE=str(self.base_dir / "drives" / "lease-1" / "new.json"),
                        FAKE_WRITE=writes(("result.md", "a")))
        self.assertEqual("completed-claim", added.state, added.evidence_needs)

    def test_tamper_then_timeout_or_nonzero_exit_still_writes_integrity_record(self):
        _, report = self.author(key="first")
        target = Path(report.run_dir) / "return.json"
        for mode, timeout in (("sleep", 1), ("exit-1", 5)):
            with self.subTest(mode=mode):
                t = self.tree("tamper-" + mode)
                assignment = self.assign(t, idempotency_key=mode)
                result = self.go(assignment, t, timeout, FAKE_MODE=self.MODES[mode],
                                 FAKE_TAMPER=json.dumps([["modify", str(target)]]))
                self.assert_unknown_held(result, assignment)
                self.assertIn(target.relative_to(self.base_dir).as_posix(),
                              self.receipt(result, "integrity.json")["modified"])

    def test_reviewer_of_other_client_authors_is_independent_by_whole_source_id(self):
        self.planning()
        other = "alt-stdout" if self.CLIENT == clients.DEFAULT else clients.DEFAULT
        pairs = [(self.CLIENT, other)] + ([(other, self.CLIENT)] if self.client.write_free_readonly else [])
        for index, (author, reviewer) in enumerate(pairs):
            with self.subTest(author=author, reviewer=reviewer):
                slug = f"pair{index}"
                assignment = self.prep(f"p{index}", slug=slug, client=author)["assignment_id"]
                report = self.execute(assignment, flow(slug=slug), self.first_files(slug))
                self.assertEqual("completed-claim", report.state, report.evidence_needs)
                bundle = review.bundle_for_workspace(self.t.repo, self.store, self.owner,
                                                     review.BundleRequest(self.fid, paths(slug)[0]))["id"]
                prepared = self.prepared(bundle, f"r{index}", client=reviewer)["assignment_id"]
                report = self.run_it(prepared, FAKE_EXTRA=json.dumps(self.final_for(prepared)))
                self.assertEqual("completed-claim", report.state, report.evidence_needs)
                self.assertEqual("observed", self.record(prepared)["provenance_class"])
                sources = [self.coordinator.detail("assignment", item)["return"]["source_id"]
                           for item in (assignment, prepared)]
                self.assertEqual([author, reviewer], [clients.client_of(item) for item in sources])
                self.assertNotEqual(*sources)

    def test_isolated_run_returns_fresh_prefixed_id_and_seals_receipts(self):
        if not self.client.write_free_readonly:
            self.code("UNSUPPORTED_CLIENT_MODE", self.isolated, "refused")
            self.assertEqual(0, self.version_count())
            return
        runs = [self.isolated("one"), self.isolated("two")]
        for run in runs:
            self.assertEqual("observed", run.state, run.needs)
            self.assertEqual(self.CLIENT, clients.client_of(run.source_id))
            directory = Path(run.receipt_dir)
            for name in ("envelope.json", "launch-intent.json", "launch.json", "events.jsonl", "last.json",
                         "end.json", "return.json"):
                self.assertEqual(0o600, stat.S_IMODE((directory / name).stat().st_mode), name)
            self.assertEqual("isolated-launch", json.loads((directory / "launch.json").read_bytes())["kind"])
            self.assertEqual("isolated-return", json.loads((directory / "return.json").read_bytes())["kind"])
            self.assertEqual((run.launch_sha256, run.return_sha256),
                             (sha((directory / "launch.json").read_bytes()), sha((directory / "return.json").read_bytes())))
            self.assertEqual(run.final, json.loads((directory / "last.json").read_bytes()))
        self.assertNotEqual(runs[0].source_id, runs[1].source_id)

    def test_isolated_run_kill_on_timeout_is_unknown(self):
        if not self.client.write_free_readonly:
            self.code("UNSUPPORTED_CLIENT_MODE", self.isolated, "sleep", 1, FAKE_MODE=self.MODES["sleep"])
            return
        run = self.isolated("sleep", 1, FAKE_MODE=self.MODES["sleep"])
        self.assertEqual(("unknown", None), (run.state, run.source_id))
        self.assertIn("SIGTERM", json.loads((Path(run.receipt_dir) / "timeout.json").read_bytes())["signals"])

    def test_isolated_run_schema_invalid_is_unknown(self):
        if not self.client.write_free_readonly:
            self.code("UNSUPPORTED_CLIENT_MODE", self.isolated, "bad", FAKE_MODE=self.MODES["bad-schema"])
            return
        run = self.isolated("bad", FAKE_MODE=self.MODES["bad-schema"])
        self.assertEqual(("unknown", None, None), (run.state, run.final, run.return_sha256))

    def test_return_source_variant_reaches_last_json_and_replays(self):
        assignment, report = self.author()
        directory = Path(report.run_dir)
        last, terminal = (directory / "last.json").read_bytes(), self.receipt(report)
        envelope = json.loads((directory / "envelope.json").read_bytes())
        source = self.client.return_source
        self.assertIn(source, ("client-file", "stdout", "worker-file"))
        if source == "worker-file":
            self.assertEqual(clients.read_worker_return(directory), last)
            self.assertEqual(clients.worker_return_sha256(directory), terminal["worker_return_sha256"])
            self.assertEqual(str(directory / clients.WORKER_RETURN / "return.json"), envelope["return_path"])
        else:
            self.assertNotIn("worker_return_sha256", terminal)
            self.assertNotIn("return_path", envelope)
        if source == "stdout":
            self.assertEqual(canonical(clients.jsonl_records(directory / "events.jsonl")[-1][self.FINAL_KEY]), last)
        self.assertEqual(last, self.client.terminal(directory, self.receipt(report, "launch.json")).final)
        self.assertEqual(sha(last), terminal["last_sha256"])
        self.assertEqual("completed-claim", inspect_run(self.t.repo, self.store, assignment).state)

    def test_envelope_via_variant_delivers_envelope_and_records_reference_for_argv(self):
        received = self.root / "envelope.sha256"
        _, report = self.author(FAKE_ENVELOPE_OUT=str(received))
        envelope = (Path(report.run_dir) / "envelope.json").read_bytes()
        launch = self.receipt(report, "launch.json")
        self.assertEqual(sha(envelope), received.read_text())
        self.assertEqual(sha(envelope), launch["envelope_sha256"])
        if self.client.envelope_via == "argv":
            self.assertIn(clients.ENVELOPE_ARG, launch["argv"])
            self.assertNotIn(envelope.decode(), launch["argv"])
        else:
            self.assertEqual("stdin", self.client.envelope_via)
            self.assertNotIn(clients.ENVELOPE_ARG, launch["argv"])

    def test_oversized_envelope_refused_before_preflight_and_reservation_without_event_or_sidecar(self):
        limit = self.client.max_envelope_bytes
        size = max(20000, (limit or 0) + 1)  # above any declared limit
        (self.t.path / "big.md").write_text("x" * size)
        assignment = self.assign(idempotency_key="big", brief="big.md")
        if limit is None:  # declared absence: a large envelope still runs
            self.assertEqual("completed-claim", self.go(assignment, FAKE_WRITE=writes(("result.md", "big"))).state)
            return
        self.assertLess(limit, size)
        self.code("INVALID_INPUT", self.go, assignment)
        self.assert_untouched(assignment)
        self.assertEqual(0, self.version_count())

    def test_readonly_review_and_isolated_run_refused_when_not_write_free(self):
        reader = self.assign(idempotency_key="reader", permission_mode="read-only", writable_paths=[])
        if self.client.write_free_readonly:
            self.assertEqual("completed-claim", self.go(reader).state)
            self.assertEqual("observed", self.isolated("allowed").state)
            return
        self.code("UNSUPPORTED_CLIENT_MODE", self.go, reader)
        self.code("UNSUPPORTED_CLIENT_MODE", self.isolated, "refused")
        self.assert_untouched(reader)
        self.assertFalse((self.root / "isolated" / "refused-receipts").exists())
        self.assertEqual(0, self.version_count())

    def test_run_subdirs_created_empty_and_run_env_and_env_unset_applied(self):
        out = self.root / "env.json"
        _, report = self.author(FAKE_ENV_OUT=str(out), ALT_ALLOW_ALL="1")
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        info, launch, directory = json.loads(out.read_text()), self.receipt(report, "launch.json"), Path(report.run_dir)
        spec = clients.LaunchSpec("x", "workspace-write", self.t.path, directory, {}, "m", None, None, (), ())
        run_env = self.client.run_env(spec)
        for name in self.client.run_subdirs:
            self.assertEqual(0o700, stat.S_IMODE((directory / name).stat().st_mode))
        if "alt-home" in self.client.run_subdirs:
            self.assertEqual({"entries": [], "mode": 0o700}, info["alt_home"])
        if clients.WORKER_RETURN in self.client.run_subdirs:
            self.assertEqual({"entries": [], "mode": 0o700}, info["worker_return"])
        for key, value in {**self.client.env, **run_env}.items():
            self.assertEqual(value, info["env"][key])
        self.assertEqual(None if "ALT_ALLOW_ALL" in self.client.env_unset else "1", info["env"]["ALT_ALLOW_ALL"])
        names = sorted(set(self.client.env) | set(run_env))
        if names or self.client.env_unset:
            self.assertEqual((names, sorted(self.client.env_unset)), (launch["env_names"], launch["env_unset"]))
        else:
            self.assertEqual((), self.client.run_subdirs)
            self.assertNotIn("env_names", launch)

    def test_tool_check_follows_client_declaration(self):
        _, report = self.author(**self.TOOLS_ENV)
        if self.client.allowed_tools is None:
            self.assertEqual("not-checked", clients.recorded_tool_check(self.receipt(report)))
            return
        if not self.client.allowed_tools("workspace-write"):  # declared empty allowlist: no reported tool passes
            self.assertEqual("blocked", clients.recorded_tool_check(self.receipt(report)))
            t = self.tree("bad-tool")
            assignment, outside = self.author(t, "bad-tool", FAKE_TOOLS=json.dumps([["Bash", "rm -rf"]]))
            self.assert_unknown_held(outside, assignment)
            return
        self.assertEqual("passed", clients.recorded_tool_check(self.receipt(report)))
        t = self.tree("no-tools")
        _, quiet = self.author(t, "no-tools", FAKE_TOOLS="[]")
        self.assertEqual("completed-claim", quiet.state, quiet.evidence_needs)
        self.assertEqual("blocked", clients.recorded_tool_check(self.receipt(quiet)))
        t = self.tree("bad-tool")
        assignment, outside = self.author(t, "bad-tool", FAKE_TOOLS=json.dumps([["Bash", "rm -rf"]]))
        self.assert_unknown_held(outside, assignment)

    def test_replay_is_pure_function_of_sealed_bytes_after_live_folders_and_home_change(self):
        assignment, report = self.author()
        directory = Path(report.run_dir)
        for name in self.client.run_subdirs:
            if name != clients.WORKER_RETURN:
                (directory / name / "later.session").write_text("changed after return\n")
        home = self.root / "other-home"
        home.mkdir()
        with patch.dict(os.environ, {"HOME": str(home)}):
            self.assertEqual("completed-claim", inspect_run(self.t.repo, self.store, assignment).state)
            self.assertEqual("completed-claim", reconcile_run(self.t.repo, self.store, self.owner, assignment).state)
            historical_return(self.t.repo, self.store, assignment)

    def test_lingering_group_member_write_after_exit_is_caught_by_delta_and_group_terminated(self):
        for linger, signals in (("write", []), ("hang", ["SIGTERM"])):
            with self.subTest(linger=linger):
                t = self.tree("linger-" + linger)
                assignment, report = self.author(t, linger, FAKE_LINGER=linger)
                self.assert_unknown_held(report, assignment)
                record = self.receipt(report, "integrity.json")
                self.assertEqual((True, signals, True, [], []),
                                 (record["group_alive_after_exit"], record["group_signals"],
                                  record["group_terminated"], record["modified"], record["deleted"]))
                self.assertEqual("late\n", (t.path / "linger.md").read_text())

    def test_end_receipt_written_only_after_lingering_child_is_gone(self):
        assignment = self.assign()
        marker = self.root / "end-seen.log"
        report = self.go(assignment, FAKE_WRITE=writes(("result.md", "r")), FAKE_LINGER="hang",
                         FAKE_LINGER_WATCH=str(self.run_dir(assignment) / "end.json"),
                         FAKE_LINGER_MARKER=str(marker))
        self.assertTrue((Path(report.run_dir) / "end.json").exists())
        self.assertFalse(marker.exists())
        self.assertEqual(["SIGTERM"], self.receipt(report, "integrity.json")["group_signals"])

    def test_group_surviving_sigkill_is_unknown_and_keeps_fence(self):
        assignment = self.assign()
        with patch.object(clients, "GROUP_WAIT", .1), \
             patch("coordinator_core.clients.os.killpg", return_value=None):
            report = self.go(assignment, FAKE_WRITE=writes(("result.md", "r")))
        self.assert_unknown_held(report, assignment)
        record = self.receipt(report, "integrity.json")
        self.assertEqual((False, ["SIGTERM", "SIGKILL"]), (record["group_terminated"], record["group_signals"]))


class CodexConformance(ClientConformance, Harness):
    CLIENT = clients.DEFAULT
    MODES = IDENTITY_MODES


class AltStdoutConformance(ClientConformance, Harness):
    CLIENT = "alt-stdout"
    MODES = IDENTITY_MODES


class AltFileConformance(ClientConformance, Harness):
    CLIENT = "alt-file"
    MODES = IDENTITY_MODES


class ClaudeConformance(ClientConformance, Harness):
    CLIENT = "claude-code"
    MODES = IDENTITY_MODES
    VERSION = "9.9.9 (Claude Code fake)"
    FINAL_KEY = "structured_output"


class CopilotConformance(ClientConformance, Harness):
    CLIENT = "copilot"
    MODES = {**IDENTITY_MODES, "exit-1": "exit-code-1"}
    VERSION = "GitHub Copilot CLI 9.9.9."
    # The fake reports no tool events by default (tool_check `blocked`); since live probe 1 the allowlist is the P0c
    # vocabulary (ruling R2), so the reporting author writes the return with `create` as observed live.
    TOOLS_ENV = {"FAKE_TOOL_EVENTS": json.dumps([["tool.execution_start", {
        "toolCallId": "w1", "toolName": "create", "arguments": {"path": "@return"}}]])}

    def setUp(self):
        super().setUp()
        (self.root / "home").mkdir()
        self.env["HOME"] = str(self.root / "home")  # the $HOME session-leak check never sees the real home


class GuardTests(unittest.TestCase):
    def test_every_registered_client_has_a_conformance_subclass(self):
        covered = {item.CLIENT for item in globals().values()
                   if isinstance(item, type) and issubclass(item, ClientConformance) and item is not ClientConformance}
        self.assertLessEqual(set(clients.REGISTRY) | set(TEST_CLIENTS), covered)


if __name__ == "__main__":
    unittest.main()
