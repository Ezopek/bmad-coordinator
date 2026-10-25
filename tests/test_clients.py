"""Client registry, recorded client, preflight, D16 integrity, atomic publication, isolated run and cross-client
independence (plan Task 1). The fakes prove coordinator logic only; no test reaches a real client."""

import hashlib
import io
import itertools
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from contextlib import closing, redirect_stderr, redirect_stdout
from dataclasses import asdict, fields, replace
from pathlib import Path
from unittest.mock import patch

from coordinator_core import __main__ as cli
from coordinator_core import bmad_planning as planning
from coordinator_core import bmad_review as review
from coordinator_core import clients, worker
from coordinator_core.exporter import export_checkpoint, import_checkpoint
from coordinator_core.model import DomainError
from coordinator_core.repo import resolve_repo
from coordinator_core.run_receipt import canonical, private_file, run_root
from coordinator_core.store import open_store
from coordinator_core.worker import historical_return, inspect_run, reconcile_run
from tests.fake_clients import ALT_INTERNAL, ALT_REQUESTED, Harness, golden_normalize, safe_path
from tests.test_bmad_core import PLAN, writes
from tests.test_bmad_planning import PRD, PlanningCase, flow

GOLDEN = Path(__file__).parent / "fixtures" / "codex-322752a"
CODEX_ENV_KEYS = ("env_names", "env_unset")   # issue #5: CODEX_SQLITE_HOME, after the 322752a golden


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git(path, *args):
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


class RegistryTests(unittest.TestCase):
    def test_registry_is_closed_and_unknown_client_is_invalid_input(self):
        self.assertEqual({"codex-cli", "claude-code", "copilot"}, set(clients.REGISTRY))
        self.assertEqual("codex-cli", clients.DEFAULT)
        self.assertIs(clients.CODEX, clients.get(None))
        self.assertIs(clients.CODEX, clients.get("codex-cli"))
        self.assertIs(clients.CLAUDE, clients.get("claude-code"))
        self.assertIs(clients.COPILOT, clients.get("copilot"))
        for name in ("claude", "copilot-cli", "alt-stdout", "", "CODEX-CLI", "codex", 3, ["codex-cli"]):
            with self.subTest(name=name), self.assertRaises(DomainError) as caught:
                clients.get(name)
            self.assertEqual("INVALID_INPUT", caught.exception.code)
        self.assertEqual("copilot", clients.client_of("copilot:session"))
        self.assertIsNone(clients.client_of("copilot-cli:session"))
        self.assertEqual("claude-code", clients.client_of("claude-code:session"))
        self.assertIsNone(clients.client_of("codex-cli:"))
        self.assertEqual("codex-cli", clients.client_of(clients.source_id("codex-cli", "thread")))


class GoldenCodexTests(PlanningCase):
    """The capture flows of fixtures/codex-322752a/README.txt, re-run on the current code with the same fake."""

    def setUp(self):
        umask = os.umask(0o022)   # the golden inventories record file modes, so the fixture's own mask is fixed
        self.addCleanup(os.umask, umask)
        counter = itertools.count(1)
        patcher = patch("uuid.uuid4", side_effect=lambda: uuid.UUID(int=next(counter)))
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()

    @staticmethod
    def shape(data: bytes, drop=(), mask=()) -> dict:
        """Receipt values except the documented new keys, argv[0], digests over root- or package-dependent bytes
        and installed-package inventory entries."""
        value = json.loads(data)
        for key in drop:
            value.pop(key)
        for key in mask:
            value[key] = "<MASKED>"
        for key in ("before", "after"):
            if key in value:
                value[key] = {path: item for path, item in value[key].items()
                              if not path.startswith(".bmad-coordinator/")}
        if "argv" in value:
            value["argv"] = value["argv"][1:]
        return value

    def compare(self, name, run_dir, fingerprint, extra=None) -> dict:
        golden = GOLDEN / name
        launch = json.loads((run_dir / "launch.json").read_bytes())
        literals = {str(self.root.resolve()): "<ROOT>", str(self.root): "<ROOT>", run_dir.name: "<RUN>",
                    fingerprint: "<FINGERPRINT>", **(extra or {})}

        def norm(data):
            return golden_normalize(data, literals, (int(launch["pid"]),))

        self.assertEqual("codex\n", (golden / "argv0.txt").read_text())
        self.assertEqual(str(self.fake_dir / "codex"), launch["argv"][0])  # the documented absolute argv[0]
        tail = json.dumps(launch["argv"][1:], indent=1).encode() + b"\n"
        self.assertEqual((golden / "argv-tail.json").read_bytes(), norm(tail))
        for item in ("events.jsonl", "last.json", "return.schema.json"):
            self.assertEqual((golden / item).read_bytes(), norm((run_dir / item).read_bytes()), item)
        terminal = json.loads((run_dir / "return.json").read_bytes())
        keys = set(json.loads((golden / "launch-keys.json").read_text()))
        self.assertEqual(keys | {"client", "binary", *CODEX_ENV_KEYS}, set(launch))
        self.assertEqual(set(json.loads((golden / "return-keys.json").read_text())) | {"client"}, set(terminal))
        masks = {"launch.json": ("envelope_sha256", "binding_sha256"), "return.json": ("launch_sha256", "last_sha256"),
                 "envelope.json": ("initial_inventory_digest",)}
        drops = {"launch.json": ("client", "binary", *CODEX_ENV_KEYS), "return.json": ("client",), "envelope.json": ()}
        for item, mask in masks.items():
            self.assertEqual(self.shape((golden / item).read_bytes(), mask=mask),
                             self.shape(norm((run_dir / item).read_bytes()), drops[item], mask), item)
        return literals

    def test_codex_argv_tail_and_brief_binding_spec_digest_match_golden_322752a(self):
        self.assertEqual(self.fake_dir, Path(shutil.which("codex")).parent)
        generic = self.assign()
        report = self.run_it(generic, FAKE_WRITE=writes(("result.md", "r")))
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        self.compare("generic", Path(report.run_dir), self.detail(generic)["request_fingerprint"])
        prepared = self.prep()
        assignment = prepared["assignment_id"]
        row = self.detail(assignment)
        brief_digest = next(item["digest"] for item in row["source_digests"] if item["path"] == prepared["brief_ref"])
        report = self.execute(assignment, flow(), self.first_files())
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        literals = self.compare("planning", Path(report.run_dir), row["request_fingerprint"],
                                {brief_digest: "<BRIEF-SHA256>"})
        golden = GOLDEN / "planning"
        self.assertEqual((golden / "brief_ref.txt").read_text(), prepared["brief_ref"] + "\n")
        self.assertEqual((golden / "brief.md").read_bytes(),
                         golden_normalize((self.t.path / prepared["brief_ref"]).read_bytes(), literals))
        binding = self.base_dir / "bindings" / (prepared["binding_sha256"] + ".json")
        self.assertEqual((golden / "binding.json").read_bytes(), golden_normalize(binding.read_bytes(), literals))
        self.assertEqual((golden / "spec_sha256.txt").read_text().strip(), json.loads(binding.read_bytes())["spec_sha256"])


class CodexUsageTests(unittest.TestCase):
    """Bill design B2: the Codex terminal records the turn's `usage` as given, and nothing when the event has none."""

    def terminal(self, completed: dict) -> clients.Terminal:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "events.jsonl").write_text(json.dumps({"type": "thread.started", "thread_id": "t1"}) + "\n" +
                                                  json.dumps({"type": "turn.completed", **completed}) + "\n")
            (run_dir / "last.json").write_bytes(b'{"result": "completed"}')
            return clients._codex_terminal(run_dir, {})

    def test_turn_usage_recorded_as_given_or_absent(self):
        usage = {"input_tokens": 7, "cached_input_tokens": 2, "output_tokens": 3}
        self.assertEqual(("t1", {"usage": usage}), (lambda t: (t.thread_id, t.facts))(self.terminal({"usage": usage})))
        self.assertEqual({}, self.terminal({}).facts)
        self.assertEqual({}, self.terminal({"usage": "not an object"}).facts)


class ClientTests(Harness):
    def set_reserved(self, assignment, source):
        with closing(sqlite3.connect(self.store.path)) as conn, conn:
            conn.execute("UPDATE events SET source_id=? WHERE subject_id=? AND kind='assignment.launch-reserved'",
                         (source, assignment))

    def reseal(self, assignment, launch=None, terminal=None, t=None):
        """Rewrite receipts and re-anchor their digests in the DB, as a forger with DB access could."""
        directory = self.run_dir(assignment, t)
        with closing(sqlite3.connect(self.store.path)) as conn, conn:
            if launch is not None:
                data = canonical(launch)
                (directory / "launch.json").write_bytes(data)
                conn.execute("UPDATE events SET payload_digest=? WHERE subject_id=? AND "
                             "kind='assignment.launch-observed'", (sha(data), assignment))
            if terminal is not None:
                data = canonical(terminal)
                (directory / "return.json").write_bytes(data)
                conn.execute("UPDATE events SET payload_digest=?, source_id=? WHERE subject_id=? AND kind IN "
                             "('assignment.return-sealed', 'assignment.return-observed')",
                             (sha(data), terminal["source_id"], assignment))
                stored = conn.execute("SELECT return_json FROM assignments WHERE id=?", (assignment,)).fetchone()[0]
                if stored is not None:  # an attempt that crashed before record_return has only its anchor
                    stored = {**json.loads(stored), "payload_digest": sha(data), "source_id": terminal["source_id"]}
                    conn.execute("UPDATE assignments SET return_json=? WHERE id=?", (json.dumps(stored), assignment))

    def completed(self, t=None, key="one", **kwargs):
        assignment = self.assign(t, idempotency_key=key)
        report = self.go(assignment, t, **{"FAKE_WRITE": writes(("result.md", key)), **kwargs})
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        return assignment, report

    def isolated(self, name, root=None, cwd=None, receipt_dir=None, client=None, init=True, timeout=5, target=None,
                 **env):
        root = root or self.root / "isolated"
        root.mkdir(parents=True, exist_ok=True)
        if cwd is None:
            cwd = root / (name + "-cwd")
            cwd.mkdir()
            if init:
                git(cwd, "init", "-q")
        envelope = {"role": "judge", "assignment_id": "judge-" + name, "request_fingerprint": "f" * 64}
        with patch.dict(os.environ, {**self.env, **env}):
            self.assert_fakes()
            return clients.run_isolated(client or self.CLIENT, target or self.t.repo, root, cwd,
                                        receipt_dir or root / (name + "-receipts"), envelope, worker._schema(),
                                        "fake-model", timeout)

    def untouched(self, assignment, t=None):
        self.assertEqual("prepared", self.state(assignment))
        self.assertEqual([], self.reserved(assignment))
        self.assertFalse(os.path.lexists(self.run_dir(assignment, t)))

    def test_failed_version_probe_refused_before_reservation_without_event_or_sidecar(self):
        assignment = self.assign()
        self.code("BLOCKED_EVIDENCE", self.go, assignment, FAKE_VERSION_FAIL="exit")
        self.untouched(assignment)
        result = subprocess.run([sys.executable, str(self.t.path / ".bmad-coordinator" / "run.py"), "--repo",
                                 str(self.t.path), "worker", "run", assignment, "--token-file", str(self.token),
                                 "--model", "m", "--timeout", "2"], capture_output=True, text=True,
                                env={**os.environ, **self.env, "FAKE_VERSION_FAIL": "empty"})
        self.assertEqual(4, result.returncode, result.stderr)
        self.assertEqual("BLOCKED_EVIDENCE", json.loads(result.stderr)["error"])
        self.untouched(assignment)
        self.assertFalse(self.count.exists())

    def test_launch_and_return_receipts_record_client_binary_and_version(self):
        _, report = self.completed()
        launch, terminal = self.receipt(report, "launch.json"), self.receipt(report)
        self.assertEqual(("codex-cli", str(self.fake_dir / "codex"), "codex-cli fake 1", "codex-cli:process"),
                         (launch["client"], launch["binary"], launch["cli_version"], launch["source_id"]))
        self.assertEqual(launch["binary"], launch["argv"][0])
        self.assertEqual(("codex-cli", "codex-cli:" + terminal["thread_id"]), (terminal["client"], terminal["source_id"]))
        self.assertEqual({"usage": {"input_tokens": 1000, "cached_input_tokens": 400, "cache_write_input_tokens": 0,
                                    "output_tokens": 50, "reasoning_output_tokens": 5}, "tool_check": "not-checked"},
                         terminal["client_facts"])   # bill design B2: Codex's own counts from turn.completed
        self.assertEqual("not-checked", clients.recorded_tool_check(terminal))
        t = self.tree("alt")
        _, report = self.completed(t, "alt", client="alt-stdout")
        launch, terminal = self.receipt(report, "launch.json"), self.receipt(report)
        self.assertEqual(("alt-stdout", str(self.fake_dir / "alt-stdout"), "alt-stdout fake 1", 7, "tool-policy"),
                         (launch["client"], launch["binary"], launch["cli_version"], launch["max_turns"],
                          launch["confinement"]))
        self.assertEqual("alt-stdout:" + launch["session_id"], terminal["source_id"])
        self.assertEqual({"tools": ["Read"], "tool_check": "passed"}, terminal["client_facts"])

    def test_reservation_event_records_client_and_null_reads_as_codex(self):
        assignment, _ = self.completed()
        self.assertEqual(["codex-cli"], self.reserved(assignment))
        self.set_reserved(assignment, None)
        self.assertEqual("completed-claim", inspect_run(self.t.repo, self.store, assignment).state)
        t = self.tree("alt")
        alt, _ = self.completed(t, "alt", client="alt-stdout")
        self.assertEqual(["alt-stdout"], self.reserved(alt))
        self.set_reserved(alt, None)
        self.assertEqual("unknown", inspect_run(t.repo, self.store, alt).state)

    def test_plain_reserve_launch_resolves_binding_client_and_refuses_conflict_or_unknown(self):
        self.use()
        accepted = [("plain", None, None, "codex-cli"), ("flagged", None, "alt-stdout", "alt-stdout"),
                    ("bound", "alt-stdout", None, "alt-stdout"), ("bound-flag", "alt-stdout", "alt-stdout", "alt-stdout"),
                    ("bound-default", clients.DEFAULT, None, "codex-cli")]
        refused = [("unknown", None, "nope"), ("conflict", "alt-stdout", "codex-cli"),
                   ("default-conflict", clients.DEFAULT, "alt-stdout")]
        for name, bound, flag, *expected in accepted + refused:
            with self.subTest(name=name):
                t = self.tree(name)
                assignment = self.assign(t, PLAN if bound else None, idempotency_key=name)
                if bound:
                    self.bind(assignment, t, **({} if bound == clients.DEFAULT else {"client": bound}))
                if expected:
                    self.coordinator.reserve_launch(self.owner, assignment, flag)
                    self.assertEqual(expected, self.reserved(assignment))
                else:
                    self.code("INVALID_INPUT", self.coordinator.reserve_launch, self.owner, assignment, flag)
                    self.untouched(assignment, t)
        t = self.tree("cli")
        assignment = self.assign(t, idempotency_key="cli")
        self.cli("assignment", "reserve-launch", assignment, "--token-file", str(self.token), "--client", "nope",
                 t=t, code=2)
        self.cli("assignment", "reserve-launch", assignment, "--token-file", str(self.token), "--client",
                 "codex-cli", t=t)
        self.assertEqual(["codex-cli"], self.reserved(assignment))

    def test_bound_assignment_takes_binding_client_and_conflicting_flag_is_refused(self):
        self.use()
        t = self.tree("bound")
        assignment = self.assign(t, PLAN, idempotency_key="bound")
        self.bind(assignment, t, client="alt-stdout")
        report = self.run_it(assignment, t, FAKE_EXTRA=json.dumps({"tag": "x"}))
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        self.assertEqual("alt-stdout", self.receipt(report, "launch.json")["client"])
        self.assertEqual(["alt-stdout"], self.reserved(assignment))
        t = self.tree("conflict")
        other = self.assign(t, PLAN, idempotency_key="conflict")
        self.bind(other, t, client="alt-stdout")
        self.code("INVALID_INPUT", self.go, other, t, client="codex-cli")
        self.code("INVALID_INPUT", self.go, other, t, client="nope")
        self.untouched(other, t)
        self.assertEqual(1, self.version_count())

    def test_non_default_client_is_sealed_in_binding_and_changes_spec_digest(self):
        self.planning()

        def spec(key, slug, **over):
            return planning.PlanningPrepare(**{**dict(
                feature_id=self.fid, key=key, skill=PRD, intent="create", author_id="prd-worker",
                compat_decision_id=self.compat, slug=slug, inputs=["docs/input.md"],
                input_roles={"docs/input.md": "driving-brief"}), **over})

        default, other = self.prep("p-default", slug="one"), self.prep("p-alt", slug="two", client="alt-stdout")
        plain, alt = self.binding_of(default["assignment_id"]), self.binding_of(other["assignment_id"])
        self.assertNotIn("client", plain)
        self.assertEqual("alt-stdout", alt["client"])
        unset = ("corrects_review", "finding_ids",  # an unset correction stays out of the digest (correction round)
                 "boundary")   # and so does an unset launch boundary (launch-boundary L1)
        legacy = {key: value for key, value in asdict(spec("p-default", "one")).items()
                  if key not in ("client", *unset)}
        self.assertEqual(sha(canonical(legacy)), plain["spec_sha256"])
        self.assertEqual(sha(canonical({key: value for key, value in
                                        asdict(spec("p-alt", "two", client="alt-stdout")).items()
                                        if key not in unset})), alt["spec_sha256"])
        self.assertNotEqual(planning.spec_digest(spec("p-alt", "two")), alt["spec_sha256"])
        self.code("IDEMPOTENCY_CONFLICT", self.prep, "p-default", slug="one", client="alt-stdout")
        self.code("INVALID_INPUT", self.prep, "p-ghost", slug="three", client="ghost")
        self.assertIn(planning.CODEX_LABEL, self.brief_of(default))
        self.assertNotIn("Codex", self.brief_of(other))
        self.assertIn("The alt-stdout client does not auto-discover the skill", self.brief_of(other))
        reviewed = review.ReviewPrepare("bundle", "reviewer", "r1", self.compat)
        self.assertEqual(sha(canonical({key: value for key, value in asdict(reviewed).items()
                                        if key not in ("client", "boundary", "mode")})),   # an unset mode, too
                         planning.spec_digest(reviewed))
        self.assertNotEqual(planning.spec_digest(reviewed), planning.spec_digest(replace(reviewed, client="alt-stdout")))

    def test_legacy_receipt_without_client_field_replays_as_codex(self):
        assignment = self.assign()
        # The process ends and the return is anchored, but the crash precedes record_return: the assignment stays
        # `running`, so reconcile below really replays the legacy receipt and records it.
        with patch("coordinator_core.worker.Coordinator.record_return", side_effect=DomainError("STOP", "crash")):
            report = self.go(assignment, FAKE_WRITE=writes(("result.md", "legacy")))
        self.assertEqual(("unknown", "running"), (report.state, self.state(assignment)))
        directory = Path(report.run_dir)
        launch, terminal = self.receipt(report, "launch.json"), self.receipt(report)
        legacy_launch = {key: value for key, value in launch.items()
                         if key not in ("client", "binary", *CODEX_ENV_KEYS)}
        legacy_launch["argv"] = ["codex", *launch["argv"][1:]]
        legacy_return = {key: value for key, value in terminal.items() if key not in ("client", "client_facts")}
        legacy_return["launch_sha256"] = sha(canonical(legacy_launch))
        self.assertEqual(set(json.loads((GOLDEN / "generic" / "launch-keys.json").read_text())), set(legacy_launch))
        self.assertEqual(set(json.loads((GOLDEN / "generic" / "return-keys.json").read_text())), set(legacy_return))
        for name in ("integrity.json", "launch-intent.json"):  # a pre-registry run directory has neither
            (directory / name).unlink()
        self.reseal(assignment, legacy_launch, legacy_return)
        self.set_reserved(assignment, None)
        self.assertEqual("completed-claim", reconcile_run(self.t.repo, self.store, self.owner, assignment).state)
        row = self.coordinator.detail("assignment", assignment)
        self.assertEqual(("completed-claim", "observed", sha(canonical(legacy_return))),
                         (row["state"], row["return"]["provenance_class"], row["return"]["payload_digest"]))
        self.assertEqual("completed-claim", inspect_run(self.t.repo, self.store, assignment).state)
        self.assertEqual(sha(canonical(legacy_return)),
                         historical_return(self.t.repo, self.store, assignment).payload_digest)
        # A new-format receipt (with client) still needs its integrity record.
        t = self.tree("current")
        current, report = self.completed(t, "current")
        (Path(report.run_dir) / "integrity.json").unlink()
        self.assertEqual("unknown", inspect_run(t.repo, self.store, current).state)

    def test_receipt_client_disagreeing_with_reservation_binding_or_prefix_is_unknown(self):
        assignment, report = self.completed()
        self.set_reserved(assignment, "alt-stdout")
        self.assertEqual("unknown", inspect_run(self.t.repo, self.store, assignment).state)
        self.set_reserved(assignment, "codex-cli")
        self.assertEqual("completed-claim", inspect_run(self.t.repo, self.store, assignment).state)
        terminal = self.receipt(report)
        self.reseal(assignment, terminal={**terminal, "source_id": "alt-stdout:" + terminal["thread_id"]})
        self.assertEqual("unknown", inspect_run(self.t.repo, self.store, assignment).state)
        self.reseal(assignment, terminal={**terminal, "client": "alt-stdout"})
        self.assertEqual("unknown", inspect_run(self.t.repo, self.store, assignment).state)
        self.reseal(assignment, terminal=terminal)
        self.assertEqual("completed-claim", inspect_run(self.t.repo, self.store, assignment).state)
        events = self.store.read_snapshot()["events"]
        launch = self.receipt(report, "launch.json")
        self.assertIs(clients.CODEX, worker._recorded_client(launch, terminal, None, events, assignment))
        self.assertIs(clients.CODEX, worker._recorded_client(launch, terminal, {}, events, assignment))
        for binding, value, recorded in (({"client": "alt-stdout"}, terminal, launch),
                                         (None, {**terminal, "client": "alt-stdout"}, launch),
                                         (None, terminal, {**launch, "client": "ghost"})):
            with self.subTest(binding=binding, value=value.get("client"), launch=recorded.get("client")):
                self.code("UNKNOWN_EVIDENCE", worker._recorded_client, recorded, value, binding, events, assignment)

    def test_max_turns_refused_for_codex(self):
        assignment = self.assign()
        self.code("INVALID_INPUT", self.go, assignment, max_turns=3)
        self.code("INVALID_INPUT", self.go, assignment, client="alt-stdout", max_turns=0)
        self.cli("worker", "run", assignment, "--token-file", str(self.token), "--model", "m", "--timeout", "2",
                 "--max-turns", "3", code=2)
        self.untouched(assignment)
        self.assertEqual(0, self.version_count())
        t = self.tree("alt")
        alt = self.assign(t, idempotency_key="alt")
        report = self.go(alt, t, client="alt-stdout", max_turns=3, FAKE_WRITE=writes(("result.md", "alt")))
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        self.assertEqual(3, self.receipt(report, "launch.json")["max_turns"])

    def test_doctor_client_flag_reports_binary_version_and_readiness(self):
        def doctor(*args, code=0, **env):
            result = subprocess.run([sys.executable, str(self.t.path / ".bmad-coordinator" / "run.py"), "--repo",
                                     str(self.t.path), "doctor", "--base", self.base, *args], capture_output=True,
                                    text=True, env={**os.environ, **self.env, **env})
            self.assertEqual(code, result.returncode, result.stderr)
            return json.loads(result.stdout) if code == 0 else json.loads(result.stderr)

        plain = doctor()
        self.assertNotIn("client", plain)
        self.assertTrue(plain["ready"], plain)
        self.assertEqual({"name": "codex-cli", "binary": str(self.fake_dir / "codex"), "version": "codex-cli fake 1",
                          "ready": True, "reasons": []}, doctor("--client", "codex-cli")["client"])
        failing = doctor("--client", "codex-cli", FAKE_VERSION_FAIL="exit")
        self.assertEqual((False, str(self.fake_dir / "codex"), None, False),
                         (failing["client"]["ready"], failing["client"]["binary"], failing["client"]["version"],
                          failing["ready"]))
        self.assertTrue(failing["client"]["reasons"])
        empty = self.root / "empty-bin"
        empty.mkdir()
        self.assertIsNone(shutil.which("codex", path=safe_path(empty)))
        missing = doctor("--client", "codex-cli", PATH=safe_path(empty))
        self.assertEqual((None, False), (missing["client"]["binary"], missing["ready"]))
        self.assertEqual("INVALID_INPUT", doctor("--client", "nope", code=2)["error"])

    def test_concurrent_other_worktree_attempt_and_operator_cli_write_during_run_are_not_unknown(self):
        barrier = self.root / "barrier"
        self.completed(self.tree("earlier"), "earlier")  # sealed evidence for both baselines
        self.count.unlink()
        first = self.assign(idempotency_key="first")
        t = self.tree("second")
        second = self.assign(t, idempotency_key="second")
        outcome = []
        thread = threading.Thread(target=lambda: outcome.append(self.go(
            first, timeout=20, FAKE_WAIT_FOR=str(barrier), FAKE_WRITE=writes(("result.md", "first")))))
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not (self.count.exists() and self.count.read_text()) and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertEqual("spawn\n", self.count.read_text())
            report = self.go(second, t, FAKE_WAIT_FOR="", FAKE_WRITE=writes(("result.md", "second")))
            self.assertEqual("completed-claim", report.state, report.evidence_needs)
            during = self.receipt(report, "integrity.json")
            self.cli("decision", "add", "--token-file", str(self.token), data={
                "feature_id": self.t.feature_id, "mandate_revision": 1, "source_kind": "operator-reported",
                "source_ref": "operator:1", "reason": "during a run", "choice": {"x": 1}})
            inbox = self.base_dir / "inbox"
            inbox.mkdir(exist_ok=True)
            (inbox / "note.json").write_text("{}")
        finally:
            barrier.write_text("go")
            thread.join(timeout=30)
        self.assertFalse(thread.is_alive())
        self.assertEqual("completed-claim", outcome[0].state, outcome[0].evidence_needs)
        record = self.receipt(outcome[0], "integrity.json")
        self.assertTrue(clients.integrity_clean(record) and clients.integrity_clean(during))
        self.assertGreater(record["checked"], 0)
        self.assertGreater(during["checked"], record["checked"])  # includes the running attempt's sealed files

    def test_integrity_record_written_on_clean_timeout_and_interrupt_paths(self):
        keys = {"checked", "modified", "deleted", "group_alive_after_exit", "group_signals", "group_terminated",
                "git_metadata_changed"}   # F24
        _, report = self.completed()
        self.assertEqual(keys, set(self.receipt(report, "integrity.json")))
        self.assertTrue(clients.integrity_clean(self.receipt(report, "integrity.json")))
        t = self.tree("timeout")
        assignment = self.assign(t, idempotency_key="timeout")
        report = self.go(assignment, t, 1, FAKE_MODE="sleep")
        self.assertEqual("unknown", report.state)
        self.assertEqual(keys, set(self.receipt(report, "integrity.json")))
        for name, error in (("interrupt", KeyboardInterrupt), ("oserror", OSError("receipt failure"))):
            with self.subTest(name=name):
                t = self.tree(name)
                assignment = self.assign(t, idempotency_key=name)
                with patch("coordinator_core.worker.seal", side_effect=error):
                    if name == "interrupt":
                        with self.assertRaises(KeyboardInterrupt):
                            self.go(assignment, t, FAKE_MODE="sleep")
                    else:
                        self.assertEqual("unknown", self.go(assignment, t, FAKE_MODE="sleep").state)
                directory = self.run_dir(assignment, t)
                self.assertTrue((directory / "interrupted.json").exists())
                record = json.loads((directory / "integrity.json").read_bytes())
                self.assertEqual(keys, set(record))
                self.assertTrue(record["group_terminated"])

    def test_group_signal_permission_error_still_writes_markers_and_integrity_and_is_unknown(self):
        """M1: a denied killpg (EPERM, e.g. a setuid member) never skips end, timeout or interrupted markers or the
        integrity record; it is recorded as group_signal_error with group_terminated false."""
        cases = [("clean", {}, None, ["end.json"]),
                 ("timeout", {"FAKE_MODE": "sleep"}, None, ["timeout.json", "end.json"]),
                 ("interrupt", {"FAKE_MODE": "sleep"}, OSError("receipt failure"), ["interrupted.json"])]
        for name, env, seal_error, markers in cases:
            with self.subTest(name=name):
                t = self.tree("denied-" + name)
                assignment = self.assign(t, idempotency_key=name)
                directory = self.run_dir(assignment, t)
                with patch.object(clients, "GROUP_WAIT", .1), \
                     patch("coordinator_core.clients.os.killpg", side_effect=PermissionError(1, "denied")), \
                     patch("coordinator_core.worker.seal", side_effect=seal_error or worker.seal):
                    report = self.go(assignment, t, 1, **{"FAKE_WRITE": writes(("result.md", name)), **env})
                self.assertEqual(("unknown", "running" if seal_error is None else "launch-pending"),
                                 (report.state, self.state(assignment)))
                for marker in markers:
                    self.assertTrue((directory / marker).exists(), marker)
                if name != "clean":  # a denied group signal still leaves the runner's own child stopped and reaped
                    stopped = json.loads((directory / markers[0]).read_bytes())
                    self.assertIsNotNone(stopped["exit_code"])
                    if name == "timeout":  # the sleeping leader needed the direct kill
                        self.assertEqual("SIGKILL:leader", stopped["signals"][-1])
                record = json.loads((directory / "integrity.json").read_bytes())
                self.assertFalse(record["group_terminated"])
                self.assertTrue(record["group_signal_error"])
                self.assertTrue(all(item.endswith(":denied") for item in record["group_signal_error"]))

    def test_baseline_taken_mid_publication_of_another_runs_receipt_is_not_unknown(self):
        other = run_root(self.t.repo) / ("e" * 64)  # an ended run whose runner is still publishing its return
        other.mkdir(parents=True, mode=0o700)
        private_file(other / "envelope.json", b"{}")
        private_file(other / "end.json", b"{}")
        barrier = self.root / "barrier"
        staged, release = threading.Event(), threading.Event()
        real_link = os.link

        def paused(source, target, *args, **kwargs):
            if Path(target) == other / "return.json":
                staged.set()
                release.wait(20)
            return real_link(source, target, *args, **kwargs)

        def releaser():
            deadline = time.monotonic() + 10
            while not (self.count.exists() and self.count.read_text()) and time.monotonic() < deadline:
                time.sleep(.02)
            release.set()  # the attempt's baseline was taken before its spawn
            barrier.write_text("go")

        assignment = self.assign()
        with patch("coordinator_core.run_receipt.os.link", side_effect=paused):
            publisher = threading.Thread(target=private_file, args=(other / "return.json", b'{"late":true}'))
            publisher.start()
            self.assertTrue(staged.wait(10))
            self.assertTrue(any(name.startswith(".return.json.") for name in os.listdir(other)))
            helper = threading.Thread(target=releaser)
            helper.start()
            report = self.go(assignment, timeout=20, FAKE_WAIT_FOR=str(barrier), FAKE_WRITE=writes(("result.md", "r")))
            helper.join(timeout=20)
            publisher.join(timeout=20)
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        self.assertEqual(b'{"late":true}', (other / "return.json").read_bytes())
        self.assertEqual(["end.json", "envelope.json", "return.json"], sorted(os.listdir(other)))
        self.assertTrue(clients.integrity_clean(self.receipt(report, "integrity.json")))

    def test_private_file_publishes_atomically_and_existing_final_name_still_refused(self):
        directory = self.root / "publish"
        directory.mkdir()
        final = directory / "receipt.json"
        seen = []
        real_link = os.link

        def observe(source, target, *args, **kwargs):
            seen.append((Path(source).name, Path(source).read_bytes(), os.path.lexists(target),
                         stat.S_IMODE(os.lstat(source).st_mode)))
            return real_link(source, target, *args, **kwargs)

        with patch("coordinator_core.run_receipt.os.link", side_effect=observe):
            self.assertEqual(sha(b"data"), private_file(final, b"data"))
        name, content, existed, mode = seen[0]
        self.assertTrue(name.startswith(".receipt.json.") and name.endswith(".tmp"))
        self.assertEqual((b"data", False, 0o600), (content, existed, mode))
        info = final.stat()
        self.assertEqual((b"data", 0o600, 1), (final.read_bytes(), stat.S_IMODE(info.st_mode), info.st_nlink))
        with self.assertRaises(FileExistsError):
            private_file(final, b"other")
        (directory / "dangling").symlink_to(directory / "nowhere")
        with self.assertRaises(FileExistsError):
            private_file(directory / "dangling", b"other")
        with self.assertRaises(FileNotFoundError):
            private_file(directory / "missing" / "x.json", b"other")
        self.assertEqual(b"data", final.read_bytes())
        self.assertFalse((directory / "nowhere").exists())
        self.assertEqual(["dangling", "receipt.json"], sorted(os.listdir(directory)))

    def test_run_directory_exists_before_spawn_and_launch_intent_is_first_post_spawn_write(self):
        assignment = self.assign()
        directory = self.run_dir(assignment)
        order = []
        real_popen, real_seal, real_private = subprocess.Popen, worker.seal, worker.private_file

        def spawn(*args, **kwargs):
            if kwargs.get("start_new_session"):
                order.append(("spawn", directory.is_dir(), sorted(os.listdir(directory))))
            return real_popen(*args, **kwargs)

        def sealed(path, value):
            order.append(("write", Path(path).name))
            return real_seal(path, value)

        def written(path, data):
            order.append(("write", Path(path).name))
            return real_private(path, data)

        with patch("coordinator_core.worker.subprocess.Popen", side_effect=spawn), \
             patch("coordinator_core.worker.seal", side_effect=sealed), \
             patch("coordinator_core.worker.private_file", side_effect=written):
            report = self.go(assignment, FAKE_WRITE=writes(("result.md", "r")))
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        index = next(i for i, item in enumerate(order) if item[0] == "spawn")
        self.assertEqual(("spawn", True, ["codex-sqlite", "envelope.json", "events.jsonl", "last.json",
                                          "return.schema.json", "stderr.log"]), order[index])
        self.assertEqual(("write", "launch-intent.json"), order[index + 1])
        self.assertEqual(("write", "launch.json"), order[index + 2])
        intent, launch = self.receipt(report, "launch-intent.json"), self.receipt(report, "launch.json")
        self.assertEqual({"pid", "pgid", "start_time", "boot_id", "run_dir", "observed_at"}, set(intent))
        self.assertEqual((launch["pid"], launch["pgid"], str(directory)),
                         (str(intent["pid"]), intent["pgid"], intent["run_dir"]))
        self.assertIsInstance(intent["start_time"], int)
        t = self.tree("alt")
        _, report = self.completed(t, "alt", client="alt-stdout")
        intent = self.receipt(report, "launch-intent.json")
        self.assertEqual(self.receipt(report, "launch.json")["session_id"], intent["session_id"])

    def test_export_import_roundtrip_keeps_non_default_reservation_source(self):
        assignment = self.assign()
        self.coordinator.reserve_launch(self.owner, assignment, "alt-stdout")
        (self.root / "export").mkdir()
        receipt = export_checkpoint(self.store, self.owner, self.root / "export")
        clone = self.root / "fresh"
        subprocess.run(["git", "clone", "-q", str(self.main), str(clone)], check=True)
        repo = resolve_repo(clone)
        import_checkpoint(repo, receipt.directory)
        rows = [row for row in open_store(repo).read_snapshot()["events"]
                if row["kind"] == "assignment.launch-reserved" and row["subject_id"] == assignment]
        self.assertEqual(["alt-stdout"], [row["source_id"] for row in rows])

    def test_isolated_run_refuses_root_or_cwd_inside_target_worktrees_or_common_dir_or_cwd_that_is_a_target_worktree(self):
        nested = self.t.path / "nested"
        nested.mkdir()
        git(nested, "init", "-q")
        inside_common = self.t.repo.common_dir / "judge"
        inside_common.mkdir()
        cases = [("root-in-worktree", self.t.path, nested, self.t.path / "receipts"),
                 ("root-in-common", inside_common, inside_common, inside_common / "receipts"),
                 ("cwd-is-worktree", self.root, self.t.path, self.root / "receipts-a"),
                 ("cwd-in-worktree", self.root, nested, self.root / "receipts-b"),
                 ("receipts-in-common", self.root, None, self.t.repo.common_dir / "receipts")]
        for name, root, cwd, receipt_dir in cases:
            with self.subTest(name=name):
                self.code("UNSAFE_PATH", self.isolated, name, root=root, cwd=cwd, receipt_dir=receipt_dir)
                self.assertFalse(receipt_dir.exists())
        self.assertFalse(self.count.exists())
        self.assertEqual(0, self.version_count())

    def test_isolated_run_allows_unrelated_ancestor_repository_and_refuses_non_git_cwd(self):
        outer = self.root / "outer"
        outer.mkdir()
        git(outer, "init", "-q")  # an unrelated ancestor repository; this whole temp tree also lies in one
        run = self.isolated("ancestor", root=outer / "judge")
        self.assertEqual("observed", run.state, run.needs)
        self.code("INVALID_INPUT", self.isolated, "plain", root=outer / "judge", init=False)
        self.assertFalse((outer / "judge" / "plain-receipts").exists())

    def test_isolated_run_refuses_existing_or_inside_cwd_receipt_dir(self):
        root = self.root / "isolated"
        (root / "taken").mkdir(parents=True)
        cwd = root / "work"
        cwd.mkdir()
        git(cwd, "init", "-q")
        (root / "link").symlink_to(root / "work")
        self.code("INVALID_INPUT", self.isolated, "a", cwd=cwd, receipt_dir=root / "taken")
        self.code("UNSAFE_PATH", self.isolated, "b", cwd=cwd, receipt_dir=cwd / "receipts")
        self.code("UNSAFE_PATH", self.isolated, "c", cwd=cwd, receipt_dir=self.root / "outside-root")
        self.code("UNSAFE_PATH", self.isolated, "d", cwd=cwd, receipt_dir=Path("relative-receipts"))
        self.code("UNSAFE_PATH", self.isolated, "e", cwd=root / "link", receipt_dir=root / "e-receipts")
        self.assertEqual(0, self.version_count())

    def test_isolated_run_is_read_only_and_creates_no_coordinator_state(self):
        before = self.store.read_snapshot()
        listing = sorted(str(path.relative_to(self.base_dir)) for path in self.base_dir.rglob("*"))
        run = self.isolated("judge")
        self.assertEqual("observed", run.state, run.needs)
        launch = json.loads((Path(run.receipt_dir) / "launch.json").read_bytes())
        index = launch["argv"].index("--sandbox")
        self.assertEqual(["--sandbox", "read-only", "--skip-git-repo-check"], launch["argv"][index:index + 3])
        self.assertEqual(("read-only", str(self.root / "isolated" / "judge-cwd")), (launch["requested_mode"], launch["cwd"]))
        self.assertEqual(before, self.store.read_snapshot())
        self.assertEqual(listing, sorted(str(path.relative_to(self.base_dir)) for path in self.base_dir.rglob("*")))

    def test_isolated_run_cwd_write_is_unknown(self):
        run = self.isolated("writer", FAKE_WRITE=writes(("note.md", "x")))
        self.assertEqual(("unknown", None), (run.state, run.return_sha256))
        self.assertIn("cwd", run.needs[0])

    def test_review_independence_compares_whole_source_ids_across_clients(self):
        self.planning()
        author = self.prep("p1")["assignment_id"]
        self.assertEqual("completed-claim", self.execute(author, flow(), self.first_files()).state)
        bundle = self.bundled()["id"]
        reviewer = self.prepared(bundle, "r1", client="alt-stdout")["assignment_id"]
        report = self.run_it(reviewer, FAKE_EXTRA=json.dumps(self.final_for(reviewer)))
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        stored = self.coordinator.detail("assignment", reviewer)["return"]
        author_source = self.coordinator.detail("assignment", author)["return"]["source_id"]
        thread = stored["source_id"].split(":", 1)[1]
        forged = [author_source, "codex-cli:" + thread, "ghost:" + thread]
        with patch("coordinator_core.bmad_review.historical_return"):
            for source in forged:
                with self.subTest(source=source):
                    with closing(sqlite3.connect(self.store.path)) as conn, conn:
                        conn.execute("UPDATE assignments SET return_json=? WHERE id=?",
                                     (json.dumps({**stored, "source_id": source}), reviewer))
                    self.code("BLOCKED_EVIDENCE", self.record, reviewer)
            with closing(sqlite3.connect(self.store.path)) as conn, conn:
                conn.execute("UPDATE assignments SET return_json=? WHERE id=?", (json.dumps(stored), reviewer))
        self.assertEqual([], self.store.read_snapshot()["reviews"])
        self.assertEqual("observed", self.record(reviewer)["provenance_class"])
        self.assertEqual(("codex-cli", "alt-stdout"), (clients.client_of(author_source),
                                                       clients.client_of(stored["source_id"])))

    def test_none_client_is_refused_by_prepare_and_malformed_bound_client_before_reservation(self):
        self.planning()

        def assignments():
            return len(self.store.read_snapshot()["assignments"])

        before = assignments()
        self.code("INVALID_INPUT", self.prep, "p-none", slug="none", client=None)
        self.assertEqual(before, assignments())
        self.assertFalse((self.t.path / "_bmad-output/coordinator/briefs/p-none.md").exists())
        author = self.prep("p1")["assignment_id"]
        self.assertEqual("completed-claim", self.execute(author, flow(), self.first_files()).state)
        bundle = self.bundled()["id"]
        before = assignments()
        self.code("INVALID_INPUT", self.prepared, bundle, "r-none", client=None)
        self.assertEqual(before, assignments())
        self.assertFalse((self.t.path / "_bmad-output/coordinator/briefs/r-none.md").exists())
        # Plain API: only a directly sealed binding can hold a malformed client; it is refused, never read as unbound.
        self.use()
        versions = self.version_count()
        for name, value in (("null", None), ("list", ["alt-stdout"]), ("ghost", "ghost")):
            with self.subTest(client=value):
                t = self.tree("sealed-" + name)
                assignment = self.assign(t, PLAN, idempotency_key="sealed-" + name)
                self.bind(assignment, t, client=value)
                for flag in (None, "alt-stdout", "codex-cli"):
                    self.code("INVALID_INPUT", self.coordinator.reserve_launch, self.owner, assignment, flag)
                self.code("INVALID_INPUT", self.run_it, assignment, t)
                for flag in ("alt-stdout", "codex-cli"):
                    self.code("INVALID_INPUT", self.go, assignment, t, client=flag)
                self.untouched(assignment, t)
        self.assertEqual(versions, self.version_count())

    def test_isolated_run_refuses_paths_inside_a_separate_target_common_dir(self):
        work, gitdir = self.root / "separate-work", self.root / "separate.git"
        subprocess.run(["git", "init", "-q", "--separate-git-dir", str(gitdir), str(work)], check=True)
        target = resolve_repo(work)
        self.assertEqual(gitdir, target.common_dir)
        self.assertFalse(target.common_dir.is_relative_to(target.worktree))
        real = clients._git_lines

        def own_worktree_only(path, *args):
            # Git derives its main-worktree entry from the common dir (here separate.git itself), so the real listing
            # already covers it; listing only `work` isolates the common-dir clause.
            return [f"worktree {work}"] if args[:1] == ("worktree",) else real(path, *args)

        for listing in ("git", "own-worktree-only"):
            judge = gitdir / ("judge-" + listing)
            judge.mkdir()
            cases = [("root-in-common", judge, judge / "cwd", judge / "receipts"),
                     ("cwd-in-common", self.root, gitdir / ("cwd-" + listing), self.root / ("receipts-" + listing)),
                     ("receipts-in-common", self.root, None, gitdir / ("receipts-" + listing))]
            for name, root, cwd, receipt_dir in cases:
                lister = real if listing == "git" else own_worktree_only
                with self.subTest(listing=listing, name=name), \
                     patch("coordinator_core.clients._git_lines", side_effect=lister):
                    if cwd is not None:
                        cwd.mkdir()
                    self.code("UNSAFE_PATH", self.isolated, f"{listing}-{name}", root=root, cwd=cwd,
                              receipt_dir=receipt_dir, target=target)
                    self.assertFalse(receipt_dir.exists())
        self.assertEqual(0, self.version_count())

    def test_cross_client_reviewer_with_equal_thread_part_is_independent_and_equal_source_id_is_refused(self):
        """D6: independence compares whole source ids. codex-cli:T and alt-stdout:T name different processes;
        codex-cli:T twice names one."""
        self.planning()
        thread = uuid.UUID(int=7)
        author = self.prep("p1")["assignment_id"]
        self.env["FAKE_THREAD"] = str(thread)
        try:
            self.assertEqual("completed-claim", self.execute(author, flow(), self.first_files()).state)
            bundle = self.bundled()["id"]
            same = self.prepared(bundle, "r-same")["assignment_id"]  # codex-cli reviewer, same thread: refused
            self.assertEqual("completed-claim", self.run_it(same, FAKE_EXTRA=json.dumps(self.final_for(same))).state)
        finally:
            del self.env["FAKE_THREAD"]
        cross = self.prepared(bundle, "r-cross", client="alt-stdout")["assignment_id"]
        with patch("coordinator_core.worker.uuid") as preset:  # the runner's preset session only
            preset.uuid4.return_value = thread
            report = self.run_it(cross, FAKE_EXTRA=json.dumps(self.final_for(cross)))
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        other = self.prepared(bundle, "r-other", client="alt-stdout")["assignment_id"]
        self.assertEqual("completed-claim", self.run_it(other, FAKE_EXTRA=json.dumps(self.final_for(other))).state)
        sources = {key: self.coordinator.detail("assignment", key)["return"]["source_id"]
                   for key in (author, same, cross, other)}
        self.assertEqual(("codex-cli:" + str(thread), "codex-cli:" + str(thread), "alt-stdout:" + str(thread)),
                         (sources[author], sources[same], sources[cross]))
        self.assertNotIn(str(thread), sources[other])
        self.code("BLOCKED_EVIDENCE", self.record, same)
        self.assertEqual("observed", self.record(cross)["provenance_class"])
        self.assertEqual("observed", self.record(other)["provenance_class"])

    def test_client_entry_exposes_only_contract_fields(self):
        self.assertEqual(["name", "executable", "confinement", "presets_session", "return_source", "envelope_via",
                          "max_envelope_bytes", "write_free_readonly", "max_turns_default", "env", "env_unset",
                          "run_subdirs", "private_subdirs", "observation_name", "internal_tools", "run_env",
                          "allowed_tools", "argv", "launch_facts", "observe", "terminal", "env_keep", "session_scrub",
                          "script_launcher", "reports_cost", "boundary_state", "read_mode"],
                         [item.name for item in fields(clients.Client)])
        self.assertEqual(["binary", "mode", "worktree", "run_dir", "schema", "model", "max_turns", "session_id",
                          "read_dirs", "commands", "isolated", "return_path", "web"],
                         [item.name for item in fields(clients.LaunchSpec)])
        self.assertEqual(["thread_id", "final", "facts", "stop"], [item.name for item in fields(clients.Terminal)])
        self.assertEqual(["state", "source_id", "final", "receipt_dir", "launch_sha256", "return_sha256", "needs"],
                         [item.name for item in fields(clients.IsolatedRun)])
        codex = clients.CODEX
        self.assertEqual(("codex-cli", "codex", "client-file", "stdin", None, True, False, None, {}, frozenset(),
                          ("codex-sqlite",), ("codex-sqlite",),
                          None, None, None, frozenset(), "os-sandbox-requested"),
                         (codex.name, codex.executable, codex.return_source, codex.envelope_via,
                          codex.max_envelope_bytes, codex.write_free_readonly, codex.presets_session,
                          codex.max_turns_default, codex.env, codex.env_unset, codex.run_subdirs, codex.private_subdirs,
                          codex.observation_name, codex.observe, codex.allowed_tools, codex.internal_tools,
                          codex.confinement))
        self.assertEqual((frozenset(), clients.PARENT_MARKERS), (codex.env_keep, codex.session_scrub))  # ruling (ii)
        self.assertEqual((False, False, True), (codex.script_launcher, clients.CLAUDE.script_launcher,
                                                clients.COPILOT.script_launcher))  # Copilot live probe 1, ruling R3
        spec = clients.LaunchSpec("/bin/codex", "workspace-write", Path("/w"), Path("/r"), {}, "m", None, None, (), ())
        self.assertEqual(({"CODEX_SQLITE_HOME": "/r/codex-sqlite"}, {}),
                         (codex.run_env(spec), codex.launch_facts(spec)))
        self.assertEqual({"env_names": ["CODEX_SQLITE_HOME"], "env_unset": []},   # issue #5: names, recorded
                         clients.launch_env(codex, codex.run_env(spec)))
        self.assertEqual(["/bin/codex", "exec", "-", "-C", "/w", "--sandbox", "workspace-write", "-c",
                          "approval_policy=never", "--json", "--output-schema", "/r/return.schema.json",
                          "--output-last-message", "/r/last.json", "-m", "m"], codex.argv(spec))
        web = replace(spec, web=True)
        self.assertEqual(["/bin/codex", "exec", "-", "-C", "/w", "--sandbox", "workspace-write", "-c",
                          "approval_policy=never", "-c", "sandbox_workspace_write.network_access=true", "--json",
                          "--output-schema", "/r/return.schema.json", "--output-last-message", "/r/last.json",
                          "-m", "m"], codex.argv(web))
        self.assertEqual({"web": True}, codex.launch_facts(web))
        self.assertEqual(codex.argv(replace(spec, isolated=True)), codex.argv(replace(web, isolated=True)))
        self.assertEqual(["--sandbox", "read-only", "--skip-git-repo-check", "-c"],
                         codex.argv(replace(spec, isolated=True))[5:9])

    def test_read_worker_return_requires_only_return_json_regular_single_link_no_follow_and_size_limit(self):
        def case(name):
            run = self.root / "returns" / name
            (run / clients.WORKER_RETURN).mkdir(parents=True)
            return run, run / clients.WORKER_RETURN / "return.json"

        run, path = case("good")
        path.write_text('{"b": 1, "a": [true, null]}')
        self.assertEqual(b'{"a":[true,null],"b":1}', clients.read_worker_return(run))
        self.assertEqual(sha(path.read_bytes()), clients.worker_return_sha256(run))
        bad = []
        run, path = case("extra")
        path.write_text("{}")
        (path.parent / "other.json").write_text("{}")
        bad.append(run)
        run, path = case("symlink-file")
        (run / "real.json").write_text("{}")
        path.symlink_to(run / "real.json")
        bad.append(run)
        run, path = case("hardlink")
        path.write_text("{}")
        os.link(path, run / "second.json")
        bad.append(run)
        run, path = case("directory")
        path.mkdir()
        bad.append(run)
        run, path = case("fifo")
        os.mkfifo(path)
        bad.append(run)
        run, path = case("large")
        path.write_text('{"a": "' + "x" * clients.MAX_RETURN_BYTES + '"}')
        bad.append(run)
        run, path = case("list")
        path.write_text("[]")
        bad.append(run)
        run = self.root / "returns" / "symlink-dir"
        run.mkdir()
        (run / "real").mkdir()
        (run / "real" / "return.json").write_text("{}")
        (run / clients.WORKER_RETURN).symlink_to(run / "real")
        bad.append(run)
        missing = self.root / "returns" / "missing"
        missing.mkdir()
        bad.append(missing)
        for run in bad:
            with self.subTest(run=run.name):
                self.code("UNKNOWN_EVIDENCE", clients.read_worker_return, run)

    def test_preflight_uses_static_env_only(self):
        out = self.root / "version-env.json"
        with patch.dict(os.environ, {**self.env, "ALT_ALLOW_ALL": "1", "FAKE_VERSION_ENV_OUT": str(out)}):
            binary, version = clients.preflight(clients.get("alt-file"))
        names = set(json.loads(out.read_text()))
        self.assertEqual((str(self.fake_dir / "alt-file"), "alt-file fake 1"), (binary, version))
        self.assertIn("ALT_STRICT", names)
        self.assertFalse({"ALT_ALLOW_ALL", "ALT_HOME"} & names)
        self.assertEqual(1, self.version_count())

    def test_allowed_tools_per_mode_is_requested_plus_internal_tools(self):
        alt = clients.get("alt-stdout")
        for mode in ("read-only", "workspace-write"):
            self.assertEqual(ALT_REQUESTED[mode] | ALT_INTERNAL, alt.allowed_tools(mode))
            self.assertLessEqual(alt.internal_tools, alt.allowed_tools(mode))
        self.assertIs(ALT_INTERNAL, alt.internal_tools)
        self.assertEqual((None, frozenset()), (clients.CODEX.allowed_tools, clients.CODEX.internal_tools))
        self.assertEqual("not-checked", clients.tool_check(clients.CODEX, "read-only", {"tools": ["Anything"]}))
        self.assertEqual("blocked", clients.tool_check(alt, "read-only", {}))
        self.assertEqual("passed", clients.tool_check(alt, "read-only", {"tools": ["Read", "Note"]}))
        for tools in (["Write"], ["Read", 3], "Read"):
            self.code("UNKNOWN_EVIDENCE", clients.tool_check, alt, "read-only", {"tools": tools})

    def test_read_worker_return_deep_nesting_or_bad_utf8_is_unknown_not_crash(self):
        def run_with(data: bytes) -> Path:
            run = self.root / "deep" / sha(data)[:12]
            (run / clients.WORKER_RETURN).mkdir(parents=True)
            (run / clients.WORKER_RETURN / "return.json").write_bytes(data)
            return run

        def nested(depth):
            return ('{"a": ' + "[" * (depth - 1) + "]" * (depth - 1) + "}").encode()

        self.assertEqual(canonical(json.loads(nested(clients.MAX_DEPTH))),
                         clients.read_worker_return(run_with(nested(clients.MAX_DEPTH))))
        self.assertEqual(b'{"a":"' + b"[" * 200 + b'"}', clients.read_worker_return(run_with(b'{"a": "' + b"[" * 200 + b'"}')))
        for data in (nested(clients.MAX_DEPTH + 1), nested(100000), b'{"a": "\xff"}', b'{"a": NaN}', b"{",
                     b'{"a": "\\ud800"}x'):
            with self.subTest(data=data[:12]):
                self.code("UNKNOWN_EVIDENCE", clients.read_worker_return, run_with(data))
        with patch("coordinator_core.clients._check_depth"):
            self.code("UNKNOWN_EVIDENCE", clients.read_worker_return, run_with(nested(200000)))

    def test_unsupported_client_mode_precedes_preflight(self):
        reader = self.assign(permission_mode="read-only", writable_paths=[])
        self.code("UNSUPPORTED_CLIENT_MODE", self.go, reader, client="alt-file")
        self.untouched(reader)
        self.assertEqual(0, self.version_count())

    def test_plain_reserve_launch_of_readonly_assignment_refuses_non_write_free_client(self):
        reader = self.assign(permission_mode="read-only", writable_paths=[])
        self.code("UNSUPPORTED_CLIENT_MODE", self.coordinator.reserve_launch, self.owner, reader, "alt-file")
        self.untouched(reader)
        self.coordinator.reserve_launch(self.owner, reader, "alt-stdout")
        self.assertEqual(["alt-stdout"], self.reserved(reader))

    def test_bmad_review_prepare_refuses_non_write_free_client(self):
        self.planning()
        author = self.prep("p1")["assignment_id"]
        self.assertEqual("completed-claim", self.execute(author, flow(), self.first_files()).state)
        bundle = self.bundled()["id"]
        count = len(self.store.read_snapshot()["assignments"])
        self.code("UNSUPPORTED_CLIENT_MODE", self.prepared, bundle, "r1", client="alt-file")
        self.code("INVALID_INPUT", self.prepared, bundle, "r2", client="ghost")
        self.assertEqual(count, len(self.store.read_snapshot()["assignments"]))
        self.assertFalse((self.t.path / "_bmad-output/coordinator/briefs/r1.md").exists())

    def test_ended_run_private_subdirs_excluded_from_other_baselines_but_worker_return_included(self):
        _, report = self.completed(key="first", client="alt-file")
        directory = Path(report.run_dir)
        prefix = f"runs/{directory.name}/"
        baseline = clients.sealed_evidence(self.t.repo, "0" * 64)
        self.assertIn(prefix + "worker-return/return.json", baseline)
        self.assertIn(prefix + "return.json", baseline)
        self.assertFalse(any(key.startswith(prefix + "alt-home/") for key in baseline))
        session = next((directory / "alt-home").iterdir())
        t = self.tree("private")
        _, report = self.completed(t, "private", FAKE_TAMPER=json.dumps([["modify", str(session)]]))
        self.assertTrue(clients.integrity_clean(self.receipt(report, "integrity.json")))
        t = self.tree("public")
        assignment = self.assign(t, idempotency_key="public")
        report = self.go(assignment, t, FAKE_TAMPER=json.dumps(
            [["modify", str(directory / clients.WORKER_RETURN / "return.json")]]), FAKE_WRITE=writes(("result.md", "p")))
        self.assertEqual("unknown", report.state)
        self.assertEqual([prefix + "worker-return/return.json"], self.receipt(report, "integrity.json")["modified"])

    def test_run_without_launch_json_legacy_or_unregistered_client_excludes_nothing_and_never_raises(self):
        runs = run_root(self.t.repo)
        cases = {"a" * 64: None, "b" * 64: canonical({"kind": "launch"}), "c" * 64: canonical({"client": "ghost"}),
                 "d" * 64: b"\xff not json", "e" * 64: "directory", "f" * 64: canonical({"client": ["alt-file"]}),
                 "1" * 64: canonical([]), "2" * 64: "symlink", "0" * 64: canonical({"client": "alt-file"})}
        for name, launch in cases.items():
            run = runs / name
            (run / "alt-home").mkdir(parents=True)
            (run / "alt-home" / "state").write_text("x")
            (run / "end.json").write_text("{}")
            if launch == "directory":
                (run / "launch.json").mkdir()
            elif launch == "symlink":
                (run / "launch.json").symlink_to(runs / ("0" * 64) / "launch.json")
            elif launch is not None:
                (run / "launch.json").write_bytes(launch)
        baseline = clients.sealed_evidence(self.t.repo, "9" * 64)
        for name in cases:
            with self.subTest(name=name[0]):
                excluded = name == "0" * 64
                self.assertEqual(("alt-home",) if excluded else (), clients.private_subdirs_of(runs / name))
                self.assertEqual(not excluded, f"runs/{name}/alt-home/state" in baseline)
                self.assertIn(f"runs/{name}/end.json", baseline)

    def test_unsupported_client_mode_exit_code_is_4(self):
        reader = self.assign(permission_mode="read-only", writable_paths=[])

        def main(*argv):
            out, err = io.StringIO(), io.StringIO()
            with patch.object(sys, "argv", ["coordinator", "--repo", str(self.t.path), *argv]), \
                 patch.dict(os.environ, self.env), redirect_stdout(out), redirect_stderr(err):
                return cli.main(), json.loads(err.getvalue())["error"]

        token = ("--token-file", str(self.token), "--client", "alt-file")
        self.assertEqual((4, "UNSUPPORTED_CLIENT_MODE"),
                         main("worker", "run", reader, "--model", "m", "--timeout", "2", *token))
        self.assertEqual((4, "UNSUPPORTED_CLIENT_MODE"), main("assignment", "reserve-launch", reader, *token))
        self.assertEqual((4, "UNSUPPORTED_CLIENT_MODE"),
                         main("bmad", "run", reader, "--model", "m", "--timeout", "2", *token))
        self.untouched(reader)
        self.assertEqual(0, self.version_count())


if __name__ == "__main__":
    unittest.main()
