"""Copilot's read mode (Copilot reviewer design revision 6, V1 to V7): an isolated launch under the launch boundary
with only view, grep and glob, the verdict from the final answer. A fake Copilot emits the record shapes the 1.0.91
probe showed; the tests need a usable bwrap (COORDINATOR_REQUIRE_BWRAP=1 makes its absence a failure)."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from coordinator_core import clients
from coordinator_core.model import DomainError
from coordinator_core.repo import resolve_repo
from tests.test_boundary import REASON

ROOT = Path(__file__).parents[1]
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
assert "--allow-tool" not in args and "--add-dir" not in args and "--deny-tool" not in args, args
session, cwd, home = flag("--session-id"), pathlib.Path(flag("-C")), pathlib.Path(os.environ["COPILOT_HOME"])
state = home / "session-state" / session
state.mkdir(parents=True)
stamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
(state / "workspace.yaml").write_text("id: %s\ncwd: %s\ncreated_at: %s\n" % (session, cwd, stamp))
(state / "events.jsonl").write_text(json.dumps({"type": "session.shutdown", "data": {"requests": {"m": 1}}}) + "\n")
def emit(kind, data=None, **top):
    print(json.dumps({"type": kind, "data": data or {}, **top}), flush=True)
emit("session.mcp_servers_loaded", {"servers": [{"name": "github-mcp-server", "status": "disabled"}]})
offered = json.loads(os.environ.get("FAKE_READ_OFFER", '["view", "grep", "glob"]'))
emit("session.usage_checkpoint", {"promptCacheBreakState": [{"models": {"m": {"tools": [{"name": n} for n in offered]}}}]})
for index, (tool, path) in enumerate(json.loads(os.environ.get("FAKE_READ_VIEWS", "[]"))):
    target = path if os.path.isabs(path) else str(cwd / path)
    emit("tool.execution_start", {"toolCallId": "c%d" % index, "toolName": tool, "arguments": {"path": target}})
    content = pathlib.Path(target).read_text() if os.path.isfile(target) else ""
    emit("tool.execution_complete", {"toolCallId": "c%d" % index, "success": True, "result": {"content": content}})
for kind, data in json.loads(os.environ.get("FAKE_READ_EXTRA", "[]")):
    emit(kind, data)
emit("assistant.message", {"content": os.environ.get("FAKE_READ_ANSWER", '{"answer": "yes"}')})
emit("result", None, sessionId=session, exitCode=0, usage={"premiumRequests": 1})
'''
SCHEMA = {"type": "object", "required": ["answer"], "additionalProperties": False,
          "properties": {"answer": {"type": "string"}}}
READS = json.dumps([["view", "question.json"], ["view", "mandate.json"]])


class RoleTests(unittest.TestCase):
    """V1 in the pipeline: a Copilot judgment needs mode read and boundary bwrap; the reviewer's read mode is not
    implemented yet."""

    def filled(self, worker=None, judgment=None, reviewer=None):
        from coordinator_core import drive_epic
        template = json.loads((ROOT / "examples" / "greetings" / "pipeline.json").read_text())
        for name, extra in (("worker", worker), ("judgment", judgment), ("reviewer", reviewer)):
            if extra is not None:
                template["roles"][name] = {**template["roles"][name], **extra}
        return drive_epic.fill_template(template, "0" * 32, "1-2-farewell")

    def test_a_copilot_judgment_in_read_mode(self):
        copilot = {"client": "copilot", "model": "claude-sonnet-5", "mode": "read", "boundary": "bwrap"}
        self.assertEqual("read", self.filled(judgment=copilot)["roles"]["judgment"]["mode"])
        for extra, code in (({**copilot, "mode": "write"}, "INVALID_INPUT"),
                            ({k: v for k, v in copilot.items() if k != "boundary"}, "INVALID_INPUT"),
                            ({k: v for k, v in copilot.items() if k != "mode"}, "UNSUPPORTED_STEP"),
                            ({**copilot, "client": "claude-code", "model": "sonnet"}, "UNSUPPORTED_STEP")):
            with self.subTest(extra=extra), self.assertRaises(DomainError) as caught:
                self.filled(judgment=extra)
            self.assertEqual(code, caught.exception.code)
        for role, code in (("reviewer", "UNSUPPORTED_STEP"), ("worker", "INVALID_INPUT")):
            with self.subTest(role=role), self.assertRaises(DomainError) as caught:
                self.filled(**{role: copilot})
            self.assertEqual(code, caught.exception.code)


@unittest.skipIf(REASON and os.environ.get("COORDINATOR_REQUIRE_BWRAP") != "1", f"bwrap unusable: {REASON}")
class ReadModeTests(unittest.TestCase):
    def setUp(self):
        if REASON:
            self.fail(f"COORDINATOR_REQUIRE_BWRAP=1 but bwrap is unusable: {REASON}")
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.tmp.cleanup)
        base = self.base = Path(self.tmp.name).resolve()
        project = base / "project"
        subprocess.run(["git", "init", "-q", str(project)], check=True)
        subprocess.run(["git", "-C", str(project), "-c", "user.name=T", "-c", "user.email=t@example.invalid", "commit",
                        "-q", "--allow-empty", "-m", "a"], check=True)
        self.target = resolve_repo(project)
        bin_dir = base / "bin"
        bin_dir.mkdir()
        (bin_dir / "copilot").write_text(FAKE.replace("__PYTHON__", sys.executable))
        (bin_dir / "copilot").chmod(0o755)
        self.env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "COPILOT_GITHUB_TOKEN": "fake-token",
                    "COORDINATOR_BOUNDARY_PASS": "FAKE_"}
        self.count = 0

    def isolated(self, qualified=False, required=("question.json", "mandate.json"), boundary_kind="bwrap", **env):
        self.count += 1
        root = self.base / f"isolated-{self.count}"
        cwd = root / "cwd"
        cwd.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(cwd)], check=True)
        for name, text in (("question.json", '{"q": 1}\n'), ("mandate.json", '{"m": 2}\n'), ("brief.md", "Brief.\n")):
            (cwd / name).write_text(text)
        envelope = {"kind": "coordinator-judgment-request", "brief": "Brief."}
        with patch.dict(os.environ, {**self.env, **env}), \
             patch.dict(clients.READ_QUALIFIED, {"9.9.9": clients.READ_CONTRACT} if qualified else {}, clear=True):
            return root, clients.run_isolated("copilot", self.target, root, cwd, root / "receipts", envelope, SCHEMA,
                                              "fake-model", 30, boundary_kind=boundary_kind, mode="read",
                                              required_reads=required)

    def test_a_qualification_run_passes_every_check_and_is_never_observed(self):
        root, result = self.isolated(COORDINATOR_QUALIFY_READ="1", FAKE_READ_VIEWS=READS)
        self.assertEqual("unknown", result.state)
        self.assertIn("qualification run", " ".join(result.needs))
        self.assertEqual({"answer": "yes"}, result.final)
        launch = json.loads((root / "receipts" / "launch.json").read_text())
        self.assertEqual((True, "read", ["brief.md", "mandate.json", "question.json"]),
                         (launch["qualification"], launch["requested_mode"], launch["read_paths"]))
        self.assertIn("boundary", launch)
        facts = json.loads((root / "receipts" / "return.json").read_text())["client_facts"]
        self.assertEqual((["glob", "grep", "view"], "passed"), (facts["offered_tools"], facts["tool_check"]))

    def test_a_qualified_version_is_observed(self):
        root, result = self.isolated(qualified=True, FAKE_READ_VIEWS=READS)
        self.assertEqual(("observed", []), (result.state, result.needs))
        self.assertEqual(b'{"answer": "yes"}', (root / "receipts" / "last.json").read_bytes())

    def test_the_stream_rules_fail_closed(self):
        outside = self.base / "outside.txt"
        outside.write_text("secret\n")
        cases = {
            "outside the read set": dict(FAKE_READ_VIEWS=json.dumps([["bash", "question.json"]])),
            "reads outside the staged inputs": dict(FAKE_READ_VIEWS=json.dumps([["view", str(outside)]])),
            "did not read mandate.json in full": dict(FAKE_READ_VIEWS=json.dumps([["view", "question.json"]])),
            "offered tools were not observed": dict(FAKE_READ_VIEWS=READS,
                                                    FAKE_READ_OFFER='["view", "grep", "glob", "bash"]'),
            "Expecting value": dict(FAKE_READ_VIEWS=READS, FAKE_READ_ANSWER='Sure: {"answer": "yes"}'),
            "record types outside the read contract": dict(FAKE_READ_VIEWS=READS,
                                                           FAKE_READ_EXTRA='[["session.new_thing", {}]]'),
            "attributed to a subagent": dict(FAKE_READ_VIEWS=READS,
                                             FAKE_READ_EXTRA='[["assistant.message", {"parentToolCallId": "x"}]]'),
            "staged inputs: /": dict(FAKE_READ_VIEWS=json.dumps([*json.loads(READS), ["grep", "."]])),   # the cwd
            "repeats a key": dict(FAKE_READ_VIEWS=READS, FAKE_READ_ANSWER='{"answer": "no", "answer": "yes"}'),
            "NaN, which is not JSON": dict(FAKE_READ_VIEWS=READS, FAKE_READ_ANSWER='{"answer": NaN}'),
        }
        for reason, env in cases.items():
            with self.subTest(reason):
                _, result = self.isolated(qualified=True, **env)
                self.assertEqual("unknown", result.state, reason)
                self.assertIn(reason, " ".join(result.needs))

    def test_the_mode_needs_the_boundary_and_a_qualified_version(self):
        for kwargs, why in (({"boundary_kind": None}, "outside the launch boundary"), ({}, "not qualified")):
            with self.subTest(why), self.assertRaises(DomainError) as caught:
                self.isolated(FAKE_READ_VIEWS=READS, **kwargs)
            self.assertEqual("UNSUPPORTED_CLIENT_MODE", caught.exception.code)
            self.assertIn(why, caught.exception.message)


if __name__ == "__main__":
    unittest.main()
